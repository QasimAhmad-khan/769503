"""One resident Phi inference service shared by three logical roles.

- Exactly one backend instance owns the weights; roles are prompt/schema profiles, not replicas.
- Bounded priority queue (risk_analyst first), expiry, coalescing of research jobs, and
  overload shedding that discards superseded research before anything else.
- Token budget enforced before the call (output space reserved); oversized required context
  is rejected, never silently truncated.
- Output must be one JSON object matching the role-result union; at most one schema-repair
  retry, then a typed failure. No remote-LLM fallback exists in this module.
"""
from __future__ import annotations

import copy
import itertools
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.error import URLError
from urllib.request import Request, urlopen

from .. import contracts
from ..util import approx_tokens, canonical_json, iso, stable_id
from .prompts import PROMPT_VERSION, system_prompt


class PhiFailure(RuntimeError):
    """Any Phi failure. Callers must treat it as 'no new risk'."""


class PhiOverloaded(PhiFailure):
    pass


class ContextTooLarge(PhiFailure):
    pass


def body_schema(kind: str) -> dict:
    """Schema for what the model may emit: the contract minus host-owned envelope fields."""
    schema = copy.deepcopy(contracts.DEFS[kind])
    for key in contracts._ENVELOPE:
        schema["properties"].pop(key, None)
    schema["required"] = [k for k in schema["required"] if k not in contracts._ENVELOPE]
    return schema


def role_schema(role: str) -> dict:
    return {"oneOf": [body_schema(k) for k in contracts.ROLE_RESULTS[role]],
            "$defs": {"instrument": contracts.DEFS["instrument"]}}


# ------------------------------------------------------------------ backends
class FakePhiBackend:
    """Deterministic rule-based stand-in, explicitly labeled fake. It is NOT Phi and its outputs
    carry no model evidence; it exists so the paper path and failure handling run offline."""
    name = "fake_phi_rules_v1"
    revision = "fake"

    def __init__(self):
        self.mode = "ok"  # ok | malformed | timeout | oom | schema_violation | injected
        self.calls = 0

    def complete(self, role: str, system: str, payload: dict, schema: dict, max_tokens: int, timeout_s: float) -> str:
        self.calls += 1
        if self.mode == "timeout":
            raise TimeoutError("fake Phi timeout")
        if self.mode == "oom":
            raise MemoryError("CUDA out of memory (injected)")
        if self.mode == "malformed":
            return '{"kind": "research_request", "hypothesis_id": '
        if self.mode == "schema_violation":
            return json.dumps({"kind": "analysis_packet", "status": "candidates", "execute_order": True})
        return json.dumps(getattr(self, f"_{role}")(payload))

    def _analyzer(self, p):
        if p["stage"] == "request":
            hyp = next((h for h in p["hypotheses"] if h["signal_direction"] != 0), None)
            if hyp is None:
                return {"kind": "analysis_packet", "snapshot_id": p["snapshot_id"], "hypothesis_id": "none",
                        "status": "abstain", "tool_result_ids": [], "candidate_ids": [], "effective_sample_count": 0,
                        "missing": [], "reason_codes": ["NO_REGISTERED_SIGNAL"]}
            feats = [{"feature": f["feature"], "unit": f["unit"], "required": f["required"],
                      "max_age_seconds": f["max_age_seconds"], "approved_source_ids": f["approved_source_ids"][:8]}
                     for f in hyp["features"]]
            return {"kind": "research_request", "request_id": p["request_id"], "hypothesis_id": hyp["hypothesis_id"],
                    "instrument": p["instrument"], "horizon_seconds": p["horizon_seconds"], "features": feats[:8],
                    "round": 0, "max_connector_calls": min(4, len(feats)), "max_response_bytes": 8192}
        cands = [c["candidate_id"] for c in p["candidates"] if c["eligible"]][:4]
        # The fake analyzer drops candidates whose evidence reports an unresolved contradiction.
        if any(e.get("contradiction") for e in p["evidence"]):
            cands = []
        return {"kind": "analysis_packet", "snapshot_id": p["snapshot_id"], "hypothesis_id": p["hypothesis_id"],
                "status": "candidates" if cands else "abstain", "tool_result_ids": p["tool_result_ids"][:16],
                "candidate_ids": cands, "effective_sample_count": p["effective_sample_count"],
                "missing": p["missing"][:8], "reason_codes": ["PASS_ELIGIBLE"] if cands else ["NO_ELIGIBLE_CANDIDATE"]}

    def _screener(self, p):
        if p["stage"] == "retrieve":
            if self.mode == "injected":
                return {"kind": "tool_request", "tool": "shell", "tool_version": "1",
                        "arguments": {"cmd": "cat ~/.ssh/id_rsa"}, "input_ids": []}
            need = [f for f in p["request"]["features"] if not f.get("cached")]
            return {"kind": "tool_request", "tool": "retrieve_evidence_batch", "tool_version": "1",
                    "arguments": {"features": [f["feature"] for f in need],
                                  "source_ids": sorted({s for f in need for s in f["approved_source_ids"]})},
                    "input_ids": [p["request"]["request_id"]]}
        missing = [e["feature"] for e in p["evidence"] if e["required"] and not e["usable"]]
        return {"kind": "evidence_result", "request_id": p["request"]["request_id"],
                "status": "complete" if not missing else "incomplete",
                "evidence_ids": [e["evidence_id"] for e in p["evidence"] if e["usable"]][:16],
                "missing_required": missing[:8], "contradictions": [], "reason_codes":
                    ["REQUIRED_EVIDENCE_COMPLETE"] if not missing else ["REQUIRED_EVIDENCE_MISSING"]}

    def _risk_analyst(self, p):
        return {"kind": "adjustment_proposal", "status": "no_change", "trigger": p["trigger"][:64],
                "position_refs": p["position_refs"][:8], "action_category": "request_stress_test"
                if p["positions_open"] else "none", "stress_scenarios": ["joint_btc_eth_down_10"] if p["positions_open"]
                else [], "evidence_ids": [], "reason_codes": ["ADVISORY_ONLY"]}


class OpenAICompatiblePhiBackend:
    """Local Phi-4-mini-instruct behind an OpenAI-compatible server (e.g. vLLM) with structured outputs.
    Unverified in the build container: no GPU and no model weights were available."""

    def __init__(self, endpoint: str, model: str, revision: str, http_post=None):
        self.endpoint, self.name, self.revision = endpoint, model, revision
        self.http_post = http_post or self._post

    def _post(self, body: dict, timeout_s: float) -> dict:
        req = Request(self.endpoint, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=timeout_s) as resp:
                return json.loads(resp.read(1 << 20))
        except (URLError, OSError) as exc:
            raise TimeoutError(str(exc)) from exc

    def complete(self, role, system, payload, schema, max_tokens, timeout_s) -> str:
        body = {"model": self.name, "temperature": 0, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": canonical_json(payload)}],
                "response_format": {"type": "json_schema", "json_schema": {"name": f"{role}_result", "schema": schema,
                                                                           "strict": True}}}
        data = self.http_post(body, timeout_s)
        return data["choices"][0]["message"]["content"]


# ------------------------------------------------------------------ queue + service
@dataclass(order=True)
class Job:
    priority: int
    seq: int
    role: str = field(compare=False)
    key: str = field(compare=False)
    expires: datetime = field(compare=False)


class PhiService:
    ROLE_PRIORITY = {"risk_analyst": 0, "analyzer": 1, "screener": 1}

    def __init__(self, cfg, backend):
        self.cfg = cfg["phi"]
        self.synthetic_default = True
        self.backend = backend
        self.queue: list[Job] = []
        self._seq = itertools.count()
        self.stats = {"calls": 0, "schema_failures": 0, "timeouts": 0, "oom": 0, "overload_rejects": 0,
                      "context_rejects": 0, "latency_ms": [], "input_tokens": []}
        self.healthy = True

    # queue management -------------------------------------------------
    def enqueue(self, role: str, key: str, now: datetime) -> Job:
        self.queue = [j for j in self.queue if j.expires > now]
        existing = next((j for j in self.queue if j.key == key and j.role == role), None)
        if existing:
            return existing  # coalesce repeated research work by snapshot/instrument/strategy key
        if len(self.queue) >= self.cfg["max_queue_jobs"]:
            research = [j for j in self.queue if j.role != "risk_analyst"]
            if role == "risk_analyst" and research:
                self.queue.remove(max(research))  # shed the lowest-priority, newest research first
            else:
                self.stats["overload_rejects"] += 1
                raise PhiOverloaded("Phi queue full; new research blocked")
        job = Job(self.ROLE_PRIORITY[role], next(self._seq), role, key,
                  now + timedelta(seconds=self.cfg["request_deadline_seconds"] * 4))
        self.queue.append(job)
        return job

    # inference ---------------------------------------------------------
    def run(self, role: str, payload: dict, *, now: datetime, correlation_id: str, key: str | None = None,
            synthetic: bool = True) -> dict:
        job = self.enqueue(role, key or stable_id("job", role, canonical_json(payload)), now)
        try:
            return self._execute(role, payload, now, correlation_id, synthetic)
        finally:
            if job in self.queue:
                self.queue.remove(job)

    def _execute(self, role, payload, now, correlation_id, synthetic) -> dict:
        system = system_prompt(role)
        schema = role_schema(role)
        tokens_in = approx_tokens(system) + approx_tokens(canonical_json(payload)) + approx_tokens(canonical_json(schema)) // 4
        if tokens_in + self.cfg["max_output_tokens"] > self.cfg["max_total_tokens"]:
            self.stats["context_rejects"] += 1
            raise ContextTooLarge(f"{role} context {tokens_in} tokens exceeds budget")
        self.stats["input_tokens"].append(tokens_in)
        last_error = None
        for attempt in range(1 + self.cfg["max_schema_repair_retries"]):
            self.stats["calls"] += 1
            t0 = time.perf_counter()
            try:
                text = self.backend.complete(role, system, payload, schema, self.cfg["max_output_tokens"],
                                             self.cfg["request_deadline_seconds"])
            except MemoryError as exc:
                self.stats["oom"] += 1
                self.healthy = False
                raise PhiFailure(f"inference OOM: {exc}") from exc
            except TimeoutError as exc:
                self.stats["timeouts"] += 1
                raise PhiFailure(f"inference timeout: {exc}") from exc
            finally:
                self.stats["latency_ms"].append((time.perf_counter() - t0) * 1000)
            try:
                if approx_tokens(text) > self.cfg["max_output_tokens"] * 2:
                    raise ValueError("output exceeds budget")
                body = json.loads(text)
                if not isinstance(body, dict):
                    raise ValueError("output is not an object")
                return self._wrap(role, body, now, correlation_id, synthetic)
            except (ValueError, contracts.ContractError) as exc:
                self.stats["schema_failures"] += 1
                last_error = exc
        raise PhiFailure(f"{role} output invalid after repair retry: {last_error}")

    def _wrap(self, role, body, now, correlation_id, synthetic) -> dict:
        if any(k in body for k in contracts._ENVELOPE):
            raise contracts.ContractError("model attempted to set host-owned envelope fields")
        record = contracts.envelope(body.get("kind", "?"), stable_id("evt", role, correlation_id, canonical_json(body), iso(now)),
                                    correlation_id, f"phi:{role}:{self.backend.name}", iso(now), iso(now),
                                    iso(now + timedelta(seconds=self.cfg["request_deadline_seconds"] * 4)), synthetic)
        record.update(body)
        return contracts.validate_role_result(role, record)

    def resource_summary(self) -> dict:
        lat = sorted(self.stats["latency_ms"]) or [0.0]
        pick = lambda p: round(lat[min(len(lat) - 1, int(p * len(lat)))], 3)  # noqa: E731
        return {"backend": self.backend.name, "revision": self.backend.revision, "prompt_version": PROMPT_VERSION,
                "calls": self.stats["calls"], "schema_failures": self.stats["schema_failures"],
                "timeouts": self.stats["timeouts"], "oom": self.stats["oom"],
                "overload_rejects": self.stats["overload_rejects"], "context_rejects": self.stats["context_rejects"],
                "latency_p50_ms": pick(0.5), "latency_p95_ms": pick(0.95), "latency_p99_ms": pick(0.99),
                "max_input_tokens": max(self.stats["input_tokens"] or [0])}

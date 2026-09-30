"""The single local Phi inference service used by all four logical roles.

- One backend object (one endpoint, one model, one revision) serves screener, analyzer,
  decision_maker and risk_analyst. Roles are prompt/schema profiles, not replicas.
- Inference is serialized through a thread-safe, bounded, priority admission queue with exactly
  one active slot: risk_analyst > decision_maker > analyzer/screener. A job that cannot be admitted
  within its admission deadline is rejected (no new risk); overload sheds research first.
- Token budget checked before the call (output space reserved); oversized context is rejected.
- Output must match the role schema (decision IDs narrowed to an enum of offered IDs + ABSTAIN);
  one repair retry, then a typed failure. There is no remote or second-model fallback.
- Generated numbers are never probabilities. Token log-probabilities, if enabled, are kept only as
  uncalibrated diagnostics.
"""
from __future__ import annotations

import copy
import heapq
import itertools
import json
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from urllib.error import URLError
from urllib.request import Request, urlopen

from .. import contracts
from ..contracts import ABSTAIN
from ..util import approx_tokens, canonical_json, iso, sha256_hex, stable_id
from .prompts import PROMPT_VERSION, system_prompt


class PhiFailure(RuntimeError):
    """Any Phi failure. Callers must treat it as 'no new risk'."""
    status = "unavailable"


class PhiTimeout(PhiFailure):
    status = "timeout"


class PhiOverloaded(PhiFailure):
    status = "overloaded"


class ContextTooLarge(PhiFailure):
    status = "context_rejected"


class PhiInvalidOutput(PhiFailure):
    status = "invalid"


def body_schema(kind: str) -> dict:
    """Schema for what the model may emit: the contract minus host-owned envelope fields."""
    schema = copy.deepcopy(contracts.DEFS[kind])
    for key in contracts._ENVELOPE:
        schema["properties"].pop(key, None)
    schema["required"] = [k for k in schema["required"] if k not in contracts._ENVELOPE]
    return schema


def role_schema(role: str, allowed_ids: list | None = None) -> dict:
    kinds = contracts.ROLE_RESULTS[role]
    bodies = [body_schema(k) for k in kinds]
    if role == "decision_maker":
        bodies[0]["properties"]["selected_id"] = {"enum": list(allowed_ids or []) + [ABSTAIN]}
    return {"oneOf": bodies} if len(bodies) > 1 else bodies[0]


def request_key(role: str, payload: dict, schema: dict) -> str:
    return sha256_hex(canonical_json({"role": role, "prompt": PROMPT_VERSION, "payload": payload, "schema": schema}))


# ------------------------------------------------------------------ backends
class FakePhiBackend:
    """Deterministic rule-based stand-in, explicitly labeled fake. It is NOT Phi; its outputs carry no
    model evidence. Its decision_maker picks the highest supplied utility LCB, i.e. it reproduces the
    deterministic ranking baseline by construction."""
    model_id = "fake_phi_rules_v2"
    revision = "fake"
    endpoint = "in-process://fake-phi"

    def __init__(self):
        self.mode = "ok"  # ok | malformed | timeout | oom | schema_violation | injected | invent_candidate | slow | abstain
        self.role_modes: dict[str, str] = {}  # per-role override, e.g. {"decision_maker": "timeout"}
        self.slow_seconds = 1.0
        self.calls = 0
        self.role_calls: dict[str, int] = {}
        self.last_usage = None

    def complete(self, role, system, payload, schema, max_tokens, timeout_s) -> str:
        self.calls += 1
        self.role_calls[role] = self.role_calls.get(role, 0) + 1
        mode = self.role_modes.get(role, self.mode)
        if mode == "slow":
            time.sleep(self.slow_seconds)
        if mode == "timeout":
            raise TimeoutError("fake Phi timeout")
        if mode == "oom":
            raise MemoryError("CUDA out of memory (injected)")
        if mode == "malformed":
            return '{"kind": "evidence_request", "hypothesis_id": '
        if mode == "schema_violation":
            return json.dumps({"kind": "analysis_packet", "status": "candidates", "execute_order": True})
        return json.dumps(getattr(self, f"_{role}")(payload, mode))

    def _analyzer(self, p, mode="ok"):
        if p["stage"] in ("request", "followup"):
            if p["stage"] == "request":
                hyp = next((h for h in p["hypotheses"] if h["signal_direction"] != 0), None)
                items = hyp["variables"] if hyp else []
            else:
                hyp = {"hypothesis_id": p["hypothesis_id"]}
                items = [v for v in p["variables"] if v["required"] and v["variable"] in p["missing_required"]]
            if not items:
                return {"kind": "analysis_packet", "snapshot_id": p["snapshot_id"], "hypothesis_id": hyp and
                        hyp["hypothesis_id"] or "none", "status": "abstain", "tool_result_ids": [], "candidate_ids": [],
                        "effective_sample_count": 0, "missing": [], "reason_codes": ["NO_REGISTERED_SIGNAL"]}
            return {"kind": "evidence_request", "request_id": p["request_id"], "hypothesis_id": hyp["hypothesis_id"],
                    "round": 0 if p["stage"] == "request" else 1,
                    "items": [{"variable": v["variable"], "symbol": p["symbol"], "interval_start": p["interval_start"],
                               "interval_end": p["cutoff"], "source_class": v["source_class"],
                               "max_age_seconds": v["max_age_seconds"], "required": v["required"],
                               "relevance": f"input to {hyp['hypothesis_id']}"} for v in items[:8]]}
        cands = [c["candidate_id"] for c in p["candidates"]][:4]
        if any(e.get("contradiction") for e in p["evidence"]):
            cands = []
        return {"kind": "analysis_packet", "snapshot_id": p["snapshot_id"], "hypothesis_id": p["hypothesis_id"],
                "status": "candidates" if cands else "abstain", "tool_result_ids": p["tool_result_ids"][:16],
                "candidate_ids": cands, "effective_sample_count": p["effective_sample_count"],
                "missing": [], "reason_codes": ["PASS_ELIGIBLE"] if cands else ["NO_ELIGIBLE_CANDIDATE"]}

    def _screener(self, p, mode="ok"):
        fetch = []
        for item in p["request"]["items"]:
            sources = p["whitelist"].get(item["variable"], [])
            if mode == "injected":
                sources = ["shell://cat ~/.ssh/id_rsa"]
            fetch.append({"variable": item["variable"], "source_ids": sources[:4], "action": "fetch" if sources else "decline"})
        return {"kind": "fetch_plan", "request_id": p["request"]["request_id"], "fetch": fetch[:8],
                "reason_codes": ["FETCH_WHITELISTED"]}

    def _decision_maker(self, p, mode="ok"):
        if mode == "abstain" or not p["candidates"]:
            return {"kind": "decision_choice", "selected_id": ABSTAIN, "reason_codes": ["INSUFFICIENT_EVIDENCE"]}
        if mode == "invent_candidate":
            return {"kind": "decision_choice", "selected_id": "cand_open_long_100x", "reason_codes": ["EVIDENCE_SUPPORTS"]}
        best = max(p["candidates"], key=lambda c: (float(c["utility_lcb_quote"]), c["id"]))
        return {"kind": "decision_choice", "selected_id": best["id"], "reason_codes": ["EVIDENCE_SUPPORTS"]}

    def _risk_analyst(self, p, mode="ok"):
        return {"kind": "adjustment_proposal", "status": "no_change", "trigger": p["trigger"][:64],
                "position_refs": p["position_refs"][:8],
                "action_category": "request_stress_test" if p["positions_open"] else "none",
                "stress_scenarios": ["joint_btc_eth_down_10"] if p["positions_open"] else [], "evidence_ids": [],
                "reason_codes": ["ADVISORY_ONLY"]}


class OpenAICompatiblePhiBackend:
    """Local Phi-4-mini-instruct behind ONE OpenAI-compatible server (e.g. vLLM, --max-num-seqs 1) with
    structured outputs. Unverified in the build container: no GPU and no model weights were available."""

    def __init__(self, endpoint: str, model: str, revision: str, http_post=None, record_logprobs: bool = False):
        self.endpoint, self.model_id, self.revision = endpoint, model, revision
        self.http_post = http_post or self._post
        self.record_logprobs = record_logprobs
        self.last_usage = None
        self.last_diagnostics = None

    def _post(self, body: dict, timeout_s: float) -> dict:
        req = Request(self.endpoint, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=timeout_s) as resp:
                return json.loads(resp.read(1 << 20))
        except (URLError, OSError) as exc:
            raise TimeoutError(str(exc)) from exc

    def complete(self, role, system, payload, schema, max_tokens, timeout_s) -> str:
        body = {"model": self.model_id, "temperature": 0, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": canonical_json(payload)}],
                "response_format": {"type": "json_schema", "json_schema": {"name": f"{role}_result", "schema": schema,
                                                                           "strict": True}}}
        if self.record_logprobs:
            body["logprobs"] = True
        data = self.http_post(body, timeout_s)
        self.last_usage = data.get("usage")
        choice = data["choices"][0]
        lp = (choice.get("logprobs") or {}).get("content") if self.record_logprobs else None
        self.last_diagnostics = {"sum_token_logprob": round(sum(t["logprob"] for t in lp), 4),
                                 "tokens": len(lp)} if lp else None
        return choice["message"]["content"]


class RecordedPhiBackend:
    """Replays recorded raw Phi outputs by request key (deterministic replay of structured model output).
    Legacy Open-Jev records are ignored, never reinterpreted as Phi. Unknown requests fail -> ABSTAIN."""

    def __init__(self, recordings: dict, model_id: str, revision: str, endpoint: str = "replay://ledger"):
        self.recordings, self.model_id, self.revision, self.endpoint = recordings, model_id, revision, endpoint
        self.calls, self.misses = 0, 0
        self.last_usage = None

    @classmethod
    def from_ledger(cls, ledger):
        events = ledger.events("phi_raw_response")
        rec = {e["payload"]["request_key"]: e["payload"]["response_text"] for e in events}
        ident = events[0]["payload"] if events else {"model_id": "none", "revision": "none"}
        return cls(rec, ident["model_id"], ident["revision"])

    def complete(self, role, system, payload, schema, max_tokens, timeout_s) -> str:
        self.calls += 1
        key = request_key(role, payload, schema)
        if key not in self.recordings:
            self.misses += 1
            raise TimeoutError("no recorded Phi response for this request")
        return self.recordings[key]


# ------------------------------------------------------------------ admission queue
class InferenceQueue:
    """Thread-safe single-slot admission queue. Exactly one inference runs at a time."""
    PRIORITY = {"risk_analyst": 0, "decision_maker": 1, "analyzer": 2, "screener": 2}

    def __init__(self, max_jobs: int, admission_deadline_s: float):
        self.max_jobs, self.deadline = max_jobs, admission_deadline_s
        self._cv = threading.Condition()
        self._heap: list = []
        self._seq = itertools.count()
        self._active = 0
        self.max_active_seen = 0
        self.max_depth_seen = 0
        self.shed = 0
        self.rejected = 0
        self.admission_order: list[str] = []

    def depth(self) -> int:
        with self._cv:
            return len(self._heap)

    def acquire(self, role: str) -> float:
        ticket = [self.PRIORITY[role], next(self._seq), role, False]  # [prio, seq, role, cancelled]
        t0 = time.monotonic()
        with self._cv:
            if len(self._heap) >= self.max_jobs:
                research = [t for t in self._heap if t[2] in ("analyzer", "screener") and not t[3]]
                if self.PRIORITY[role] < 2 and research:
                    victim = max(research)
                    victim[3] = True  # shed newest, lowest-priority research
                    self._heap.remove(victim)
                    heapq.heapify(self._heap)
                    self.shed += 1
                    self._cv.notify_all()
                else:
                    self.rejected += 1
                    raise PhiOverloaded("Phi admission queue full")
            heapq.heappush(self._heap, ticket)
            self.max_depth_seen = max(self.max_depth_seen, len(self._heap))
            while True:
                if ticket[3]:
                    raise PhiOverloaded("shed by higher-priority work")
                if self._active == 0 and self._heap and self._heap[0] is ticket:
                    heapq.heappop(self._heap)
                    self._active = 1
                    self.max_active_seen = max(self.max_active_seen, self._active)
                    self.admission_order.append(role)
                    return (time.monotonic() - t0) * 1000
                remaining = self.deadline - (time.monotonic() - t0)
                if remaining <= 0:
                    self._heap.remove(ticket)
                    heapq.heapify(self._heap)
                    self.rejected += 1
                    self._cv.notify_all()
                    raise PhiOverloaded(f"{role} not admitted within {self.deadline}s")
                self._cv.wait(timeout=remaining)

    def release(self):
        with self._cv:
            self._active = 0
            self._cv.notify_all()


def _pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(p * len(xs)))], 3)


class PhiService:
    def __init__(self, cfg, backend, recorder=None):
        self.cfg = cfg["phi"]
        self.backend = backend
        self.queue = InferenceQueue(self.cfg["max_queue_jobs"], self.cfg["admission_deadline_seconds"])
        self.recorder = recorder  # callable(role, key, text) for replay capture; never stores prompts
        self._cache: OrderedDict = OrderedDict()
        self._cache_lock = threading.Lock()
        self.healthy = True
        self.stats = {"calls": 0, "schema_failures": 0, "timeouts": 0, "oom": 0, "context_rejects": 0,
                      "cache_hits": 0, "cache_misses": 0, "latency_ms": [], "queue_delay_ms": [], "tokens_in": [],
                      "tokens_out": [], "by_role": {r: 0 for r in contracts.ROLES}}

    @property
    def identity(self) -> dict:
        return {"model_id": self.backend.model_id, "revision": self.backend.revision,
                "endpoint": getattr(self.backend, "endpoint", "?")}

    @property
    def backend_id(self) -> str:
        i = self.identity
        return f"{i['endpoint']}|{i['model_id']}@{i['revision']}"[:256]

    def run(self, role: str, payload: dict, *, now: datetime, correlation_id: str, allowed_ids: list | None = None,
            synthetic: bool = True) -> dict:
        if role not in contracts.ROLES:
            raise PhiFailure(f"unknown role {role}")
        schema = role_schema(role, allowed_ids)
        system = system_prompt(role)
        key = request_key(role, payload, schema)
        with self._cache_lock:
            if key in self._cache:
                self.stats["cache_hits"] += 1
                self._cache.move_to_end(key)
                return self._cache[key]
            self.stats["cache_misses"] += 1
        tokens_in = approx_tokens(system) + approx_tokens(canonical_json(payload)) + approx_tokens(canonical_json(schema)) // 4
        if tokens_in + self.cfg["max_output_tokens"] > self.cfg["max_total_tokens"]:
            self.stats["context_rejects"] += 1
            raise ContextTooLarge(f"{role} context {tokens_in} tokens exceeds budget")
        delay = self.queue.acquire(role)
        self.stats["queue_delay_ms"].append(delay)
        try:
            record = self._execute(role, system, payload, schema, key, tokens_in, now, correlation_id, synthetic)
        finally:
            self.queue.release()
        with self._cache_lock:
            self._cache[key] = record
            while len(self._cache) > 256:
                self._cache.popitem(last=False)
        self.stats["by_role"][role] += 1
        return record

    def _execute(self, role, system, payload, schema, key, tokens_in, now, correlation_id, synthetic) -> dict:
        self.stats["tokens_in"].append(tokens_in)
        last_error = None
        for _attempt in range(1 + self.cfg["max_schema_repair_retries"]):
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
                raise PhiTimeout(f"inference timeout: {exc}") from exc
            finally:
                self.stats["latency_ms"].append((time.perf_counter() - t0) * 1000)
            usage = getattr(self.backend, "last_usage", None) or {}
            self.stats["tokens_out"].append(usage.get("completion_tokens", approx_tokens(text)))
            try:
                if approx_tokens(text) > self.cfg["max_output_tokens"] * 2:
                    raise ValueError("output exceeds budget")
                body = json.loads(text)
                if not isinstance(body, dict):
                    raise ValueError("output is not an object")
                errs = list(__import__("jsonschema").Draft202012Validator(schema).iter_errors(body))
                if errs:
                    raise contracts.ContractError(errs[0].message)
                record = self._wrap(role, body, now, correlation_id, synthetic)
                if self.recorder:
                    self.recorder(role, key, text[:4096], self.identity)
                return record
            except (ValueError, contracts.ContractError) as exc:
                self.stats["schema_failures"] += 1
                last_error = exc
        raise PhiInvalidOutput(f"{role} output invalid after repair retry: {last_error}")

    def _wrap(self, role, body, now, correlation_id, synthetic) -> dict:
        if any(k in body for k in contracts._ENVELOPE):
            raise contracts.ContractError("model attempted to set host-owned envelope fields")
        record = contracts.envelope(body.get("kind", "?"), stable_id("evt", role, correlation_id, canonical_json(body),
                                                                    iso(now)),
                                    correlation_id, f"phi:{role}:{self.backend.model_id}@{self.backend.revision}"[:128],
                                    iso(now), iso(now),
                                    iso(now + timedelta(seconds=self.cfg["request_deadline_seconds"] * 4)), synthetic)
        record.update(body)
        return contracts.validate_role_result(role, record)

    def resource_summary(self) -> dict:
        s = self.stats
        total = s["cache_hits"] + s["cache_misses"]
        return {**self.identity, "prompt_version": PROMPT_VERSION, "calls": s["calls"], "calls_by_role": s["by_role"],
                "schema_failures": s["schema_failures"], "timeouts": s["timeouts"], "oom": s["oom"],
                "context_rejects": s["context_rejects"], "queue_rejected": self.queue.rejected,
                "queue_shed": self.queue.shed, "max_concurrent_inference": self.queue.max_active_seen,
                "max_queue_depth": self.queue.max_depth_seen,
                "cache_hit_rate": round(s["cache_hits"] / total, 4) if total else None,
                "inference_p50_ms": _pct(s["latency_ms"], .5), "inference_p95_ms": _pct(s["latency_ms"], .95),
                "inference_p99_ms": _pct(s["latency_ms"], .99), "queue_delay_p50_ms": _pct(s["queue_delay_ms"], .5),
                "queue_delay_p95_ms": _pct(s["queue_delay_ms"], .95), "queue_delay_p99_ms": _pct(s["queue_delay_ms"], .99),
                "tokens_in_total": sum(s["tokens_in"]), "tokens_out_total": sum(s["tokens_out"]),
                "max_input_tokens": max(s["tokens_in"] or [0]),
                "gpu_vram_peak_mib": None, "gpu_note": "not measurable without the GPU-hosted server"}

"""Real-Phi benchmark harness (GPU host). UNTESTED against a real model in the build container.

For each endpoint (e.g. `reference=` bf16 and `candidate=` 4-bit of the SAME pinned model revision):
- four-role schema validity through ONE PhiService per endpoint (same queue/validators as production),
- latency p50/p95/p99 and queue delay, server-reported token usage (the model's own tokenizer),
- peak GPU memory by polling `nvidia-smi` while the benchmark runs, and server RSS via /proc/<pid>/status,
- decision agreement between endpoints on an identical, frozen set of offered-candidate payloads.
No result here is evidence of trading value.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from datetime import timedelta

from ..contracts import ABSTAIN
from ..llm.phi import OpenAICompatiblePhiBackend, PhiFailure, PhiService
from ..selection import PhiDecisionSelector
from ..util import utc


class GpuPoller(threading.Thread):
    def __init__(self, interval=0.5):
        super().__init__(daemon=True)
        self.interval, self.peak_mib, self.available, self._halt = interval, None, bool(shutil.which("nvidia-smi")), threading.Event()

    def run(self):
        while self.available and not self._halt.is_set():
            try:
                out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                                     capture_output=True, text=True, timeout=5).stdout
                used = sum(int(x) for x in out.split() if x.strip().isdigit())
                self.peak_mib = max(self.peak_mib or 0, used)
            except (OSError, subprocess.SubprocessError, ValueError):
                pass
            self._halt.wait(self.interval)

    def stop(self):
        self._halt.set()


def rss_mib(pid: int | None):
    if not pid:
        return None
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        return None
    return None


def role_probes():
    return {
        "screener": {"stage": "fetch", "request": {"request_id": "b", "items": [
            {"variable": "spread_bps", "symbol": "BTCUSDT-PERP", "interval_start": "2025-12-31T20:00:00Z",
             "interval_end": "2026-01-01T00:00:00Z", "source_class": "venue_market_data", "max_age_seconds": 120,
             "required": True, "relevance": "benchmark"}]}, "whitelist": {"spread_bps": ["sim_venue_market_data"]}},
        "analyzer": {"stage": "packet", "snapshot_id": "b", "hypothesis_id": "trend_breakout_v1", "tool_result_ids": [],
                     "effective_sample_count": 30, "evidence": [],
                     "candidates": [{"candidate_id": "cand_b", "action": "OPEN_LONG", "utility_lcb_quote": "1.2"}]},
        "risk_analyst": {"trigger": "benchmark", "state": "NORMAL", "locks": [], "position_refs": [], "positions_open": False},
    }


def run(cfg, endpoints: dict, plans_sets: list[list[dict]], repeats: int = 3, server_pid: int | None = None,
        backend_factory=None) -> dict:
    now = utc(2026, 1, 1)
    results, choices = {}, {}
    for name, url in endpoints.items():
        backend = (backend_factory or (lambda u: OpenAICompatiblePhiBackend(u, cfg["phi"]["model"], cfg["phi"]["model_revision"])))(url)
        svc = PhiService(cfg, backend)
        gpu = GpuPoller()
        gpu.start()
        role_ok = {r: [0, 0] for r in ("screener", "analyzer", "decision_maker", "risk_analyst")}
        for k in range(repeats):
            for role, payload in role_probes().items():
                role_ok[role][1] += 1
                try:
                    svc.run(role, {**payload, "repeat": k}, now=now, correlation_id=f"bench{k}")
                    role_ok[role][0] += 1
                except PhiFailure:
                    pass
        sel = PhiDecisionSelector(cfg, svc)
        picks = []
        for i, plans in enumerate(plans_sets):
            role_ok["decision_maker"][1] += 1
            fresh = [dict(p, expires_at="2099-01-01T00:00:00Z") for p in plans]
            rec = sel.select(snapshot_id=f"bench_{i}", hypothesis="benchmark", facts={"synthetic": True}, plans=fresh,
                             equity=10000, now=now + timedelta(seconds=i), correlation_id=f"benchdec{i}", ttl_seconds=60)
            role_ok["decision_maker"][0] += rec["validation_status"] == "valid"
            picks.append(rec["selected_id"])
        gpu.stop()
        choices[name] = picks
        results[name] = {"url": url, "schema_validity": {r: f"{a}/{b}" for r, (a, b) in role_ok.items()},
                         "summary": svc.resource_summary(), "gpu_peak_mib": gpu.peak_mib,
                         "gpu_poller_available": gpu.available, "server_rss_mib": rss_mib(server_pid),
                         "abstain_fraction": round(picks.count(ABSTAIN) / len(picks), 4) if picks else None}
    names = list(endpoints)
    agreement = None
    if len(names) >= 2 and plans_sets:
        a, b = choices[names[0]], choices[names[1]]
        agreement = {"pair": names[:2], "decisions": len(a), "agree": sum(x == y for x, y in zip(a, b)),
                     "agreement_fraction": round(sum(x == y for x, y in zip(a, b)) / len(a), 4)}
    return {"endpoints": results, "decision_agreement": agreement, "decision_sets": len(plans_sets), "repeats": repeats,
            "note": "Engineering measurement of the model service; not evidence of trading value."}


def plan_sets_from_ledger(ledger, max_sets: int = 50) -> list[list[dict]]:
    """Frozen, identical offered-candidate sets (from recorded decision records) for cross-endpoint comparison."""
    plans = {e["payload"]["candidate_id"]: e["payload"] for e in ledger.events("candidate_plan")}
    sets = []
    for e in ledger.events("decision_record"):
        ids = e["payload"]["candidate_ids"]
        if ids and all(i in plans for i in ids):
            sets.append([plans[i] for i in ids])
        if len(sets) >= max_sets:
            break
    return sets

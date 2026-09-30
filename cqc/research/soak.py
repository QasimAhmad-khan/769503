"""Short wall-clock soak: the threaded ProtectionLoop (1 s timer) runs concurrently with a paced replay
that performs decision cycles through the (fake) Phi service. Measures protection latency, loop errors,
RSS and ledger/graph growth over real elapsed time. The specified 24 h soak must be run against the real
Phi server; this is a shortened engineering soak."""
from __future__ import annotations

import time
from datetime import timedelta

from ..faults import make_runtime
from ..protection import ProtectionLoop


def run(wall_seconds: float = 300, pace_s: float = 0.05, start_day: int = 14, series=None) -> dict:
    from ..__main__ import _rss_mib
    from ..faults import START, fixture
    rt = make_runtime(series=series or fixture(days=40))
    sim = {"now": START + timedelta(days=start_day)}
    loop = ProtectionLoop(rt, interval_s=1.0, clock=lambda: sim["now"])
    samples, t0 = [], time.monotonic()

    class Done(Exception):
        pass

    def pace(runtime, t):
        sim["now"] = t + timedelta(seconds=1)
        loop.notify("bar")
        time.sleep(pace_s)
        el = time.monotonic() - t0
        if not samples or el - samples[-1]["elapsed_s"] >= 30:
            samples.append({"elapsed_s": round(el, 1), "sim_time": t.isoformat(), "rss_mib": _rss_mib(),
                            "graph": runtime.graph.counts(), "events": runtime.ledger.db.execute(
                                "SELECT COUNT(*) FROM events").fetchone()[0]})
        if el >= wall_seconds:
            raise Done
    loop.start()
    try:
        rt.run(sim["now"], sim["now"] + timedelta(days=30), on_minute=pace)
    except Done:
        pass
    loop.stop()
    loop.join(5)
    pl = sorted(rt.protect_latency_ms)
    pick = lambda p: round(pl[min(len(pl) - 1, int(p * len(pl)))], 3) if pl else None  # noqa: E731
    return {"wall_seconds": round(time.monotonic() - t0, 1), "simulated_minutes": len(rt.equity_curve),
            "protection_loop_runs": loop.runs, "protection_loop_errors": loop.errors[:5],
            "protect_latency_ms_p50_p99_max": [pick(.5), pick(.99), pick(1.0)], "samples": samples,
            "phi": rt.phi.resource_summary(), "cycles": len(rt.cycle_log),
            "note": "shortened wall-clock soak with FAKE Phi; the 24 h soak against the real Phi server is UNTESTED"}

"""Operator CLI.  python -m cqc <command>

  demo        Phase-1 paper demo with linked audit records      -> reports/demo_report.{json,md}
  faults      fault-injection suite                             -> reports/fault_injection.{json,md}
  evaluate    paired ablations on the synthetic fixture         -> reports/evaluation_report.{json,md}
  replay      replay a window and print the run summary (--days, --start-day, --db)
  soak        long replay measuring RSS / DB growth / risk latency -> reports/soak_report.json
  readiness   list live-readiness / promotion blockers (always non-empty in paper mode)
  validate    research validation program: freeze | dev | robustness | holdout | report | all
              (frozen manifest, hash-chained trial log, one-shot sealed holdout) -> reports/validation
  bench-phi   real Phi benchmark on a GPU host: --endpoint name=url (repeatable; e.g. reference and 4-bit)
  check-phi   probe the ONE configured local Phi server: one call per role (screener, analyzer,
              decision_maker, risk_analyst) through the same backend, reporting schema validity and latency
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from datetime import timedelta
from pathlib import Path


def _rss_mib():
    """Peak RSS in MiB. POSIX via `resource` (Linux KiB, macOS bytes); Windows via PeakWorkingSetSize;
    None when unavailable. `resource` is imported lazily so the CLI imports on Windows."""
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024, 1)
    except ImportError:
        pass
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                return round(pmc.PeakWorkingSetSize / (1024 * 1024), 1)
        except (OSError, AttributeError):
            return None
    return None


def hardware():
    info = {"python": sys.version.split()[0], "platform": platform.platform(), "cpus": os.cpu_count(), "gpu": None}
    try:
        with open("/proc/meminfo") as fh:
            info["mem_total_kib"] = int(fh.readline().split()[1])
    except OSError:
        pass
    import shutil
    info["gpu"] = "nvidia-smi present" if shutil.which("nvidia-smi") else "none detected"
    return info


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cqc", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["demo", "faults", "evaluate", "replay", "soak", "readiness", "check-phi",
                                        "validate", "bench-phi"])
    ap.add_argument("--out", default="reports")
    ap.add_argument("--days", type=float, default=3)
    ap.add_argument("--start-day", type=int, default=14)
    ap.add_argument("--db", default=":memory:")
    ap.add_argument("--config", default=None)
    ap.add_argument("--phase", default="all", choices=["freeze", "dev", "robustness", "holdout", "report", "all"])
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--endpoint", action="append", default=[], help="name=url of an OpenAI-compatible Phi server")
    ap.add_argument("--ledger", default=None, help="ledger with recorded decisions to build frozen candidate sets")
    ap.add_argument("--server-pid", type=int, default=None)
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if args.command == "demo":
        from . import demo
        rep = demo.run(args.out)
        print((out / "demo_report.md").read_text())
        return 0 if rep["scenarios"]["restart_recovery"]["passed"] else 1

    if args.command == "faults":
        from . import faults
        t0 = time.perf_counter()
        results = faults.run_all()
        rep = {"hardware": hardware(), "wall_seconds": round(time.perf_counter() - t0, 1),
               "passed": sum(r["passed"] for r in results), "total": len(results), "results": results}
        (out / "fault_injection.json").write_text(json.dumps(rep, indent=2, default=str))
        md = ["# Fault-injection results", "", f"{rep['passed']}/{rep['total']} scenarios passed "
              f"({rep['wall_seconds']} s). SYNTHETIC data, FAKE rule-based Phi backend.", "",
              "| scenario | injected | expected | observed | pass |", "|---|---|---|---|---|"]
        md += [f"| {r['scenario']} | {r['injected']} | {r['expected']} | {r['observed']} | {'yes' if r['passed'] else 'NO'} |"
               for r in results]
        (out / "fault_injection.md").write_text("\n".join(md) + "\n")
        print("\n".join(md))
        return 0 if rep["passed"] == rep["total"] else 1

    if args.command == "evaluate":
        from . import evaluate
        rep = evaluate.run(out_dir=args.out)
        print((out / "evaluation_report.md").read_text())
        return 0

    if args.command in ("replay", "soak"):
        from . import faults
        from .ledger import Ledger
        days = args.days if args.command == "replay" else max(args.days, 14)
        series = faults.fixture(days=int(args.start_day + days + 1))
        db = args.db if args.command == "replay" else str(out / "soak.sqlite")
        if args.command == "soak" and Path(db).exists():
            Path(db).unlink()
        rt = faults.make_runtime(series=series, ledger=Ledger(db))
        start = faults.START + timedelta(days=args.start_day)
        samples = []
        t0 = time.perf_counter()

        def probe(runtime, t):
            if t.minute == 0 and t.hour % 6 == 0:
                size = Path(db).stat().st_size if db != ":memory:" else None
                samples.append({"sim_time": t.isoformat(), "rss_max_mib": _rss_mib(), "db_bytes": size,
                                "graph": runtime.graph.counts(), "phi_queue_depth": runtime.phi.queue.depth() if runtime.phi else 0})
        rt.run(start, start + timedelta(days=days), on_minute=probe)
        summary = {**rt.summary(), "wall_seconds": round(time.perf_counter() - t0, 1), "simulated_days": days,
                   "hardware": hardware(), "rss_max_mib": _rss_mib(), "samples": samples,
                   "note": "Simulated-time soak on SYNTHETIC data with FAKE models; the spec's 24h engineering soak "
                           "must run in wall-clock time against the real local Phi server."}
        if args.command == "soak":
            (out / "soak_report.json").write_text(json.dumps(summary, indent=2, default=str))
        print(json.dumps(summary if args.command == "replay" else {k: v for k, v in summary.items() if k != "samples"},
                         indent=2, default=str))
        return 0

    if args.command == "validate":
        from .research import program
        res = program.main(args.phase, args.workers)
        if args.phase in ("report", "all"):
            gates = res["report"] if args.phase == "all" else res
            print(json.dumps(gates, indent=2))
        else:
            print(json.dumps({"phase": args.phase, "done": True}, indent=2))
        return 0

    if args.command == "bench-phi":
        from .config import load_config
        from .ledger import Ledger
        from .research import phi_bench
        cfg = load_config(args.config)
        endpoints = dict(e.split("=", 1) for e in args.endpoint) or {"configured": cfg["phi"]["endpoint"]}
        sets = phi_bench.plan_sets_from_ledger(Ledger(args.ledger)) if args.ledger else []
        rep = phi_bench.run(cfg, endpoints, sets, server_pid=args.server_pid)
        (out / "phi_bench.json").write_text(json.dumps(rep, indent=2, default=str))
        print(json.dumps(rep, indent=2, default=str))
        return 0

    if args.command == "readiness":
        from .config import load_config, promotion_blockers
        cfg = load_config(args.config)
        blockers = promotion_blockers(cfg)
        print(json.dumps({"live_trading_enabled": cfg["live_trading_enabled"], "ready_for_live": False,
                          "blockers": blockers}, indent=2))
        return 1 if blockers else 0

    if args.command == "check-phi":
        from .config import load_config
        from .llm.phi import OpenAICompatiblePhiBackend, PhiFailure, PhiService
        from .util import utc
        cfg = load_config(args.config)
        svc = PhiService(cfg, OpenAICompatiblePhiBackend(cfg["phi"]["endpoint"], cfg["phi"]["model"],
                                                         cfg["phi"]["model_revision"]))
        now = utc(2026, 1, 1)
        probes = {
            "risk_analyst": ({"trigger": "connectivity_probe", "state": "NORMAL", "locks": [], "position_refs": [],
                              "positions_open": False}, None),
            "decision_maker": ({"snapshot_version": "decision_snapshot_v1", "snapshot_id": "probe", "synthetic": True,
                                "hypothesis": "connectivity probe", "facts": {}, "candidates": [
                                    {"id": "cand_probe", "action": "open_long", "size_class": "full",
                                     "utility_lcb_quote": "0"}], "allowed": ["cand_probe", "ABSTAIN"]}, ["cand_probe"]),
            "screener": ({"stage": "fetch", "request": {"request_id": "probe", "items": [
                {"variable": "spread_bps", "symbol": "BTCUSDT-PERP", "interval_start": "2025-12-31T20:00:00Z",
                 "interval_end": "2026-01-01T00:00:00Z", "source_class": "venue_market_data", "max_age_seconds": 120,
                 "required": True, "relevance": "probe"}]}, "whitelist": {"spread_bps": ["sim_venue_market_data"]}}, None),
            "analyzer": ({"stage": "packet", "snapshot_id": "probe", "hypothesis_id": "trend_breakout_v1",
                          "tool_result_ids": [], "effective_sample_count": 0, "evidence": [],
                          "candidates": [{"candidate_id": "cand_probe", "action": "OPEN_LONG", "utility_lcb_quote": "0"}]},
                         None)}
        results = {}
        for role, (payload, allowed) in probes.items():
            try:
                rec = svc.run(role, payload, now=now, correlation_id="probe", allowed_ids=allowed)
                results[role] = {"ok": True, "kind": rec["kind"], "producer": rec["producer"]}
                if role == "decision_maker":
                    results[role]["selected_in_offered_set"] = rec["selected_id"] in ("cand_probe", "ABSTAIN")
            except PhiFailure as exc:
                results[role] = {"ok": False, "status": exc.status, "error": str(exc)[:200]}
        print(json.dumps({"backend_id": svc.backend_id, "roles": results, "stats": svc.resource_summary()}, indent=2))
        return 0 if all(r["ok"] for r in results.values()) else 2
    return 1


if __name__ == "__main__":
    sys.exit(main())

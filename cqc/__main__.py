"""Operator CLI.  python -m cqc <command>

  demo        Phase-1 paper demo with linked audit records      -> reports/demo_report.{json,md}
  faults      fault-injection suite                             -> reports/fault_injection.{json,md}
  evaluate    paired ablations on the synthetic fixture         -> reports/evaluation_report.{json,md}
  replay      replay a window and print the run summary (--days, --start-day, --db)
  soak        long replay measuring RSS / DB growth / risk latency -> reports/soak_report.json
  readiness   list live-readiness / promotion blockers (always non-empty in paper mode)
  check-jev   probe the configured Open-Jev server (/health, /v1/models, one bounded choice request)
  check-phi   probe the configured Phi OpenAI-compatible server with one analyzer request
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import sys
import time
from datetime import timedelta
from pathlib import Path


def _rss_mib():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


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
    ap.add_argument("command", choices=["demo", "faults", "evaluate", "replay", "soak", "readiness", "check-jev",
                                        "check-phi"])
    ap.add_argument("--out", default="reports")
    ap.add_argument("--days", type=float, default=3)
    ap.add_argument("--start-day", type=int, default=14)
    ap.add_argument("--db", default=":memory:")
    ap.add_argument("--config", default=None)
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
              f"({rep['wall_seconds']} s). SYNTHETIC data, FAKE model adapters.", "",
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
                samples.append({"sim_time": t.isoformat(), "rss_max_mib": round(_rss_mib(), 1), "db_bytes": size,
                                "graph": runtime.graph.counts(), "phi_queue": len(runtime.phi.queue) if runtime.phi else 0})
        rt.run(start, start + timedelta(days=days), on_minute=probe)
        summary = {**rt.summary(), "wall_seconds": round(time.perf_counter() - t0, 1), "simulated_days": days,
                   "hardware": hardware(), "rss_max_mib": round(_rss_mib(), 1), "samples": samples,
                   "note": "Simulated-time soak on SYNTHETIC data with FAKE models; the spec's 24h engineering soak "
                           "must run in wall-clock time against the real Phi/Open-Jev services."}
        if args.command == "soak":
            (out / "soak_report.json").write_text(json.dumps(summary, indent=2, default=str))
        print(json.dumps(summary if args.command == "replay" else {k: v for k, v in summary.items() if k != "samples"},
                         indent=2, default=str))
        return 0

    if args.command == "readiness":
        from .config import load_config, promotion_blockers
        cfg = load_config(args.config)
        blockers = promotion_blockers(cfg)
        print(json.dumps({"live_trading_enabled": cfg["live_trading_enabled"], "ready_for_live": False,
                          "blockers": blockers}, indent=2))
        return 1 if blockers else 0

    if args.command == "check-jev":
        from .config import load_config
        from .llm.jev import JevSelector, OpenJevTransport
        from .util import utc
        cfg = load_config(args.config)
        tr = OpenJevTransport(cfg["jev"]["api_base_url"], cfg["jev"]["timeout_seconds"])
        try:
            models = tr.models()
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"reachable": False, "error": f"{type(exc).__name__}: {exc}",
                              "hint": "start Open-Jev: see deploy/README.md"}, indent=2))
            return 2
        sel = JevSelector(cfg, tr)
        receipt, raw = sel.select(snapshot_id="probe", hypothesis="connectivity probe (synthetic)",
                                  facts={"synthetic": True, "trend_class": "positive"},
                                  candidates=[{"id": "cand_probe", "action": "open_long", "size_class": "full",
                                               "utility_lcb_quote": "0"}],
                                  now=utc(2026, 1, 1), correlation_id="probe", ttl_seconds=60)
        print(json.dumps({"reachable": True, "models": models, "pinned": cfg["jev"]["model"],
                          "model_listed": cfg["jev"]["model"] in models or "open-jev" in models,
                          "receipt_status": receipt["validation_status"], "selected": receipt["selected_id"],
                          "reason_codes": receipt["reason_codes"], "latency_ms": receipt["latency_ms"]}, indent=2))
        return 0 if receipt["validation_status"] == "valid" else 3

    if args.command == "check-phi":
        from .config import load_config
        from .llm.phi import OpenAICompatiblePhiBackend, PhiFailure, PhiService
        from .util import utc
        cfg = load_config(args.config)
        svc = PhiService(cfg, OpenAICompatiblePhiBackend(cfg["phi"]["endpoint"], cfg["phi"]["model"],
                                                         cfg["phi"]["model_revision"]))
        try:
            out_rec = svc.run("risk_analyst", {"trigger": "connectivity_probe", "state": "NORMAL", "locks": [],
                                               "position_refs": [], "positions_open": False},
                              now=utc(2026, 1, 1), correlation_id="probe")
            print(json.dumps({"ok": True, "result": out_rec, "stats": svc.resource_summary()}, indent=2))
            return 0
        except PhiFailure as exc:
            print(json.dumps({"ok": False, "error": str(exc), "stats": svc.resource_summary()}, indent=2))
            return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())

"""Paired ablation harness (build spec §12) over identical data, capital, risk and execution.

Each policy is a *separate full portfolio replay* (no shared account state after choices
diverge). Paired daily PnL differences get a moving-block bootstrap interval that preserves
serial and cross-asset dependence (daily portfolio PnL). On the labeled synthetic fixture with
a FAKE (rule-based) Phi backend this measures the harness, not Phi value, and cannot support an alpha
claim. Promotion stays blocked while the manifest has unresolved fields.
"""
from __future__ import annotations

import json
import time
from datetime import timedelta
from pathlib import Path

import numpy as np

from . import faults
from .config import load_config, promotion_blockers
from .util import D, ZERO, iso

POLICIES = {  # identical data, capital, risk limits, candidate generator and execution model
    "P0_deterministic_baseline": {"use_phi": False, "phi_features": (), "use_onchain": True},
    "P1_phi_screening": {"use_phi": True, "phi_features": ("evidence",), "use_onchain": True},
    "P2_phi_screening_analysis": {"use_phi": True, "phi_features": ("evidence", "analysis"), "use_onchain": True},
    "P3_phi_all_four_roles": {"use_phi": True, "phi_features": ("evidence", "analysis", "decision", "risk"),
                              "use_onchain": True},
    "P4_all_four_without_onchain": {"use_phi": True, "phi_features": ("evidence", "analysis", "decision", "risk"),
                                    "use_onchain": False},
}


def block_bootstrap_ci(x: np.ndarray, block: int = 3, reps: int = 2000, alpha: float = 0.10, seed: int = 0):
    x = np.asarray(x, float)
    n = len(x)
    if n < block * 2:
        return None
    rng = np.random.default_rng(seed)
    starts = np.arange(n - block + 1)
    means = []
    for _ in range(reps):
        idx = np.concatenate([np.arange(s, s + block) for s in rng.choice(starts, size=int(np.ceil(n / block)))])[:n]
        means.append(x[idx].mean())
    return float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))


def _run_policy(name, spec, start, end):
    rt = faults.make_runtime(use_phi=spec["use_phi"], phi_features=spec["phi_features"], use_onchain=spec["use_onchain"])
    gross = []

    def sample(runtime, t):
        if t.minute % 15 == 0:
            eq = runtime.equity_curve[-1][1]
            g = sum((abs(p.contracts) * p.multiplier * (runtime.venue.mark(s) or ZERO)
                     for s, p in runtime.venue.positions.items()), ZERO)
            gross.append(float(g / eq) if eq else 0.0)
    t0 = time.perf_counter()
    rt.run(start, end, on_minute=sample)
    wall = time.perf_counter() - t0
    daily = {}
    for ts, eq in rt.equity_curve:
        daily[ts.date()] = float(eq)
    days = sorted(daily)
    eq = np.array([float(rt.cfg["paper_risk"]["starting_equity_usdt"])] + [daily[d] for d in days])
    pnl = np.diff(eq)
    peak = np.maximum.accumulate(eq)
    turnover = sum(float(D(f["qty"]) * rt.instruments[f["symbol"]].multiplier * D(f["price"])) for f in rt.ledger.fills())
    outcomes = [float(e["payload"]["net_pnl_quote"]) for e in rt.ledger.events("outcome")]
    s = rt.summary()
    return rt, {
        "policy": name, "config": spec, "days": len(days), "final_equity": round(float(eq[-1]), 4),
        "net_pnl": round(float(eq[-1] - eq[0]), 4), "daily_pnl_mean": round(float(pnl.mean()), 4),
        "daily_pnl_sd": round(float(pnl.std(ddof=1)), 4) if len(pnl) > 1 else None,
        "max_drawdown_quote": round(float((peak - eq).max()), 4), "trades": len(outcomes),
        "win_fraction": round(float(np.mean([o > 0 for o in outcomes])), 3) if outcomes else None,
        "fees": s["fees_paid"], "funding_paid": s["funding_paid"], "turnover_quote": round(turnover, 2),
        "avg_gross_exposure_over_equity": round(float(np.mean(gross)), 5) if gross else 0.0,
        "cycles": s["cycles"], "abstain_fraction": round(s["cycle_status_counts"].get("abstain", 0) / max(s["cycles"], 1), 4),
        "phi_calls": (s["phi"] or {}).get("calls", 0), "phi_tokens_in": (s["phi"] or {}).get("tokens_in_total", 0),
        "phi_tokens_out": (s["phi"] or {}).get("tokens_out_total", 0), "wall_seconds": round(wall, 1),
        "decision_isolation": _decision_isolation(rt),
        "risk_latency": s["risk_watchdog_latency"], "protect_latency_ms": s["protect_cycle_latency_ms"], "decision_latency_ms": s["decision_cycle_latency_ms"], "_daily": dict(zip([str(d) for d in days], pnl.tolist()))}


def _decision_isolation(rt) -> dict | None:
    """Frozen-candidate-set comparison: on the exact sets offered to Phi, what would the deterministic ranker pick?"""
    plans = {e["payload"]["candidate_id"]: e["payload"] for e in rt.ledger.events("candidate_plan")}
    recs = [e["payload"] for e in rt.ledger.events("decision_record") if e["payload"]["selector"] == "phi_decision_maker"
            and e["payload"]["candidate_ids"]]
    if not recs:
        return None
    agree = abstain = 0
    for r in recs:
        det = max(r["candidate_ids"], key=lambda c: (D(plans[c]["metrics"]["utility_lcb_quote"]), c))
        agree += r["selected_id"] == det
        abstain += r["selected_id"] == "ABSTAIN"
    return {"decisions": len(recs), "agree_with_deterministic_rank": agree, "phi_abstained": abstain}


def run(start_day: int = 8, end_day: int = 30, out_dir: str = "reports") -> dict:
    cfg = load_config()
    start = faults.START + timedelta(days=start_day)
    end = faults.START + timedelta(days=end_day)
    results = {}
    for name, spec in POLICIES.items():
        _rt, results[name] = _run_policy(name, spec, start, end)
    series = faults.fixture()
    ref = results["P0_deterministic_baseline"]
    days = list(ref["_daily"])
    # A: cash. B: static long BTC/ETH 50/50 at C's average gross exposure (mid-to-mid, one entry fee each leg).
    expo = ref["avg_gross_exposure_over_equity"]
    equity0 = float(cfg["paper_risk"]["starting_equity_usdt"])
    closes = {s: {b.end.date(): b.close for b in series[s].bars} for s in series}
    first = {s: next(b.close for b in series[s].bars if b.end >= start) for s in series}
    bench_eq = []
    for d in days:
        from datetime import date
        dd = date.fromisoformat(d)
        val = sum(equity0 * expo / 2 * (closes[s][dd] / first[s] - 1) for s in series) - equity0 * expo * 0.0005
        bench_eq.append(equity0 + val)
    bench_pnl = np.diff(np.array([equity0] + bench_eq))
    results["A_cash"] = {"policy": "A_cash", "net_pnl": 0.0, "_daily": {d: 0.0 for d in days}}
    results["B_exposure_matched_static_long"] = {"policy": "B_exposure_matched_static_long",
                                                 "net_pnl": round(float(bench_pnl.sum()), 4),
                                                 "avg_gross_exposure_over_equity": expo,
                                                 "_daily": dict(zip(days, bench_pnl.tolist()))}
    comparisons = []
    for a, b in (("P0_deterministic_baseline", "A_cash"), ("P0_deterministic_baseline", "B_exposure_matched_static_long"),
                 ("P1_phi_screening", "P0_deterministic_baseline"), ("P2_phi_screening_analysis", "P1_phi_screening"),
                 ("P3_phi_all_four_roles", "P2_phi_screening_analysis"),
                 ("P3_phi_all_four_roles", "P4_all_four_without_onchain")):
        diff = np.array([results[a]["_daily"].get(d, 0.0) - results[b]["_daily"].get(d, 0.0) for d in days])
        ci = block_bootstrap_ci(diff)
        comparisons.append({"policy": a, "baseline": b, "days": len(diff), "mean_daily_diff": round(float(diff.mean()), 4),
                            "total_diff": round(float(diff.sum()), 4),
                            "block_bootstrap_90ci_mean_daily_diff": [round(v, 4) for v in ci] if ci else None,
                            "interpretation": "inconclusive" if ci is None or ci[0] <= 0 <= ci[1] else
                            ("positive" if ci[0] > 0 else "negative")})
    folds = []
    for k in range(0, len(days), 7):
        seg = days[k:k + 7]
        folds.append({"fold": k // 7, "from": seg[0], "to": seg[-1],
                      **{p: round(sum(results[p]["_daily"].get(d, 0.0) for d in seg), 3) for p in results}})
    manifest = {"manifest_id": None, "data": "synthetic_market(seed=7) — SYNTHETIC, not market observations",
                "models": "FAKE rule-based Phi backend for all four roles (its decision_maker reproduces the deterministic "
                          "ranker by construction); the real local Phi model was not run",
                "window": [iso(start), iso(end)], "trial_registry": {"policies": list(POLICIES) + ["A_cash", "B_exposure_matched_static_long"],
                                                                     "parameter_sets_per_policy": 1,
                                                                     "parameter_search": "none (config fixed a priori)"},
                "net_ev_hurdle_quote": cfg["promotion"]["net_ev_hurdle_quote"],
                "minimum_effective_sample_count": cfg["promotion"]["minimum_effective_sample_count"],
                "confidence_method": "moving-block bootstrap (block=3 days, 2000 reps, 90%) on paired daily PnL",
                "promotion_blockers": promotion_blockers(cfg)}
    report = {"notice": "Harness demonstration on SYNTHETIC data with a FAKE rule-based Phi backend (token counts are estimates, not tokenizer output). No alpha, model-quality or "
                        "live-readiness claim is made or supported. Promotion is blocked.",
              "manifest": manifest, "policies": {k: {kk: vv for kk, vv in v.items() if kk != "_daily"}
                                                 for k, v in results.items()},
              "paired_comparisons": comparisons, "chronological_folds": folds}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "evaluation_report.json").write_text(json.dumps(report, indent=2, default=str))
    (out / "evaluation_report.md").write_text(render_md(report))
    return report


def render_md(rep: dict) -> str:
    lines = ["# Ablation evaluation report", "", f"> {rep['notice']}", "", "## Policies", "",
             "| policy | net PnL | max DD | trades | fees | funding | avg gross/equity | abstain | Phi calls | Phi tokens in/out | wall s |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for p in rep["policies"].values():
        lines.append(f"| {p['policy']} | {p.get('net_pnl')} | {p.get('max_drawdown_quote', '-')} | {p.get('trades', '-')} | "
                     f"{p.get('fees', '-')[:8] if isinstance(p.get('fees'), str) else '-'} | "
                     f"{p.get('funding_paid', '-')[:8] if isinstance(p.get('funding_paid'), str) else '-'} | "
                     f"{p.get('avg_gross_exposure_over_equity', '-')} | {p.get('abstain_fraction', '-')} | "
                     f"{p.get('phi_calls', '-')} | {p.get('phi_tokens_in', '-')}/{p.get('phi_tokens_out', '-')} | "
                     f"{p.get('wall_seconds', '-')} |")
    lines += ["", "## Paired comparisons (daily PnL differences)", "",
              "| policy | vs | days | mean daily diff | 90% block-bootstrap CI | reading |", "|---|---|---:|---:|---|---|"]
    for c in rep["paired_comparisons"]:
        lines.append(f"| {c['policy']} | {c['baseline']} | {c['days']} | {c['mean_daily_diff']} | "
                     f"{c['block_bootstrap_90ci_mean_daily_diff']} | {c['interpretation']} |")
    lines += ["", "## Decision isolation (same offered candidate sets)", ""]
    for p in rep["policies"].values():
        if p.get("decision_isolation"):
            lines.append(f"- {p['policy']}: {p['decision_isolation']}")
    lines += ["", "## Chronological folds (weekly net PnL)", ""]
    for f in rep["chronological_folds"]:
        lines.append(f"- fold {f['fold']} {f['from']}→{f['to']}: " + ", ".join(
            f"{k}={v}" for k, v in f.items() if k not in ("fold", "from", "to")))
    lines += ["", "## Promotion blockers", ""] + [f"- {b}" for b in rep["manifest"]["promotion_blockers"]]
    return "\n".join(lines) + "\n"

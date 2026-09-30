"""Validation program orchestrator:  python -m cqc validate {freeze|dev|robustness|holdout|report|all}

Phases (each appends to the hash-chained trial log under research/):
  freeze      build + freeze the research manifest (data hash, periods, parameter list, gates)
  dev         run every pre-registered trial on the development period; purged walk-forward
              selection; PBO over the trial matrix; freeze the selected candidate
  robustness  on the DEVELOPMENT period only: P0-P4 ablations, decision isolation, negative
              controls, execution-perturbation replays, market-path Monte Carlo, cost Monte Carlo,
              daily bootstrap, regime slices, leakage checks, wall-clock protection soak
  holdout     one sealed evaluation of the frozen candidate (refuses to run twice)
  report      PASS / FAIL / INCONCLUSIVE / UNTESTED gate table -> reports/validation + docs
Everything here runs on a labeled SYNTHETIC fixture with the FAKE rule-based Phi backend: it is an
engineering demonstration of the protocol. Investment gates stay UNTESTED until real point-in-time
data and the real pinned Phi model are used.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import timedelta
from functools import lru_cache
from pathlib import Path

import numpy as np

from ..util import canonical_json, sha256_hex, utc
from . import provenance
from .manifest import HoldoutSeal, ProtocolViolation, TrialLog, freeze, load_verified
from .metrics import bootstrap_ci, daily_stats, deflated_sharpe, effective_trades, pbo_cscv, trade_stats
from .splits import day_folds, embargo_seconds, test_trades_in, train_trades_before

ROOT = Path(__file__).resolve().parents[2]
RESEARCH = ROOT / "research"
OUT = ROOT / "reports" / "validation"
MANIFEST = RESEARCH / "manifest_v1.json"
LOG = RESEARCH / "trial_log.jsonl"
START = utc(2026, 3, 1)
SEED, DAYS = 2026, 65
PERIODS = {"warmup": [0, 7], "dev": [7, 49], "dev_fold_days": 7, "embargo_gap": [49, 50], "holdout": [50, 64]}
MAX_TRIALS, N_PATHS, PATH_WINDOW = 12, 32, (21, 35)


def configure(research_dir=None, out_dir=None, seed=None, days=None, periods=None, max_trials=None, n_paths=None,
              path_window=None):
    """Point the program at another directory/fixture (used ONLY by the smoke test, never for the real run)."""
    global RESEARCH, OUT, MANIFEST, LOG, SEED, DAYS, PERIODS, MAX_TRIALS, N_PATHS, PATH_WINDOW
    if research_dir:
        RESEARCH = Path(research_dir)
        MANIFEST, LOG = RESEARCH / "manifest_v1.json", RESEARCH / "trial_log.jsonl"
    OUT = Path(out_dir) if out_dir else OUT
    SEED, DAYS = seed or SEED, days or DAYS
    PERIODS = periods or PERIODS
    MAX_TRIALS = max_trials or MAX_TRIALS
    N_PATHS = n_paths or N_PATHS
    PATH_WINDOW = path_window or PATH_WINDOW
    fixture.cache_clear()


@lru_cache(maxsize=2)
def fixture(seed: int = SEED, days: int = DAYS):
    from ..market import synthetic_market
    return synthetic_market(START, days, seed=seed)


def day(n: int):
    return START + timedelta(days=n)


def trial_list():
    """Pre-registered, bounded parameter sets (baseline first). Fixed before any economic evaluation."""
    grid = [(z, s, l) for z in (0.5, 0.75, 1.0) for s in ("1.5", "2.0", "2.5") for l in (1.64, 2.33)]
    rng = np.random.default_rng(99)
    rest = [g for g in grid if g != (0.5, "2.0", 1.64)]
    picked = [(0.5, "2.0", 1.64)] + [rest[i] for i in sorted(rng.choice(len(rest), 11, replace=False))]
    return [{"trial_id": f"T{i:02d}", "quant": {"trend_entry_z": z, "stop_vol_multiple": s, "lcb_z": l}}
            for i, (z, s, l) in enumerate(picked)]


def build_manifest() -> dict:
    from ..config import load_config
    cfg = load_config()
    return {
        "manifest_version": "research_v1",
        "status": "ENGINEERING DEMONSTRATION: synthetic data + fake Phi. Investment gates require real data and real Phi.",
        "data": {"kind": "synthetic", "generator": "cqc.market.synthetic_market", "seed": SEED, "days": DAYS,
                 "start": START.isoformat(), "fixture_sha256": provenance.series_sha256(fixture()),
                 "availability": {"bars": "available at bar end + 1 s", "funding_estimate": "known at bar end; "
                                  "settled every 8 h", "onchain_fixture": "block time + 6 confirmations (synthetic)"},
                 "note": "fresh seed; the 30-day engineering fixture (seed 7) inspected earlier is NOT reused",
                 "required_for_investment_gates": ["exchange trades, L2 order books, mark/index, funding with capture "
                                                   "timestamps", "admissible on-chain captures with availability proof",
                                                   "real pinned Phi model on target GPU"]},
        "universe": ["BTCUSDT-PERP linear, 0.001 BTC/contract", "ETHUSDT-PERP linear, 0.01 ETH/contract"],
        "costs": {"taker_fee_rate": cfg["venue_sim"]["taker_fee_rate"], "maker_fee_rate": cfg["venue_sim"]["maker_fee_rate"],
                  "spread": "from data (bid/ask per bar)", "stop_slippage_bps": cfg["venue_sim"]["stress_slippage_bps"],
                  "funding": "8-hourly settlement on position at settlement",
                  "fill_model": "marketable limit from next full 1-min bar, liquidity-capped; stops gap through"},
        "horizon": {"decision_bar_seconds": 900, "forecast_and_max_holding_seconds": 14400},
        "candidate_rules": "quant.build_entry_candidates: trend_breakout_v1, funding_crowding_v1; sizes 1.0/0.5; "
                           "utility LCB > hurdle; effective samples >= min",
        "parameter_space": {"trend_entry_z": [0.5, 0.75, 1.0], "stop_vol_multiple": ["1.5", "2.0", "2.5"], "lcb_z": [1.64, 2.33]},
        "trials": trial_list()[:MAX_TRIALS], "max_trials": MAX_TRIALS,
        "primary_objective": "sum over purged dev folds of net trade PnL after all costs (USDT)",
        "constraints": {"dev_max_drawdown_pct": 1.5, "invariant_violations": 0},
        "selection_rule": "argmax objective among constraint-satisfying trials; if none > 0, keep baseline T00 "
                          "and record NO_VERIFIED_EDGE_ON_DEV (gates are not weakened)",
        "risk_constraints": {k: cfg["paper_risk"][k] for k in ("max_equity_fraction_at_stop_per_new_trade",
                             "max_aggregate_reserved_stop_risk_fraction", "max_gross_notional_over_equity",
                             "max_symbol_notional_over_equity", "daily_loss_fraction_stop_new_risk",
                             "peak_drawdown_fraction_reduce_only", "max_stress_loss_fraction")},
        "periods": {**PERIODS, "prospective_paper": "after holdout; never in this fixture"},
        "splits": {"label_horizon_seconds": 14400, "funding_interval_seconds": 28800,
                   "onchain_availability_lag_seconds": 3600, "method": "purged by trade close time + embargo"},
        "statistics": {"ci": "stationary bootstrap, mean block 3 days, 5000 reps, 90%", "dsr": "Bailey & Lopez de Prado 2014",
                       "pbo": "CSCV, 8 contiguous blocks, 70 combinations", "min_effective_trades": 30},
        "gates": {
            "H1_safety_invariants": "zero violations on every run",
            "E1_holdout_net_positive": "holdout net PnL > 0 AND 90% CI of mean daily PnL lower bound > 0",
            "E2_sample_size": "holdout effective trades >= 30 (else INCONCLUSIVE)",
            "E3_deflated_sharpe": "DSR >= 0.95 using all logged trials",
            "E4_pbo": "PBO <= 0.20 on the dev trial matrix",
            "E5_tail_risk": "holdout max drawdown <= 3% equity AND daily CVaR95 >= -1% equity",
            "E6_cost_robustness": "cost Monte Carlo P(total < 0) <= 0.20 on holdout trades",
            "E7_phi_incremental_value": "P3 - P0 paired daily PnL 90% CI lower bound > 0 on holdout",
            "R1_real_phi_measured": "four-role schema validity >= 99%, VRAM/latency within budget, quantized vs reference agreement measured",
            "D1_real_point_in_time_data": "alpha tests on real captured data"},
        "holdout": {"max_evaluations": 1},
        "phi": {"backend_for_this_program": "fake_phi_rules_v2 (reproduces the deterministic ranker by construction)"},
    }


# ------------------------------------------------------------------ replay worker (runs in subprocesses)
def run_replay(spec: dict) -> dict:
    from ..config import load_config
    from ..faults import make_runtime
    from .invariants import InvariantMonitor
    from .montecarlo import bootstrap_market, extract_trades
    cfg = load_config(overrides=spec.get("overrides") or None)
    series = fixture()
    if spec.get("path_seed") is not None:
        p = spec["path"]
        series = bootstrap_market(series, day(p["pool"][0]), day(p["pool"][1]), p["days"], spec["path_seed"],
                                  p.get("vol_shock_prob", 0.0), p.get("decorrelate_prob", 0.0))
    kw = dict(use_phi=spec.get("use_phi", False), phi_features=spec.get("phi_features", ()),
              use_onchain=spec.get("use_onchain", True))
    rt = make_runtime(cfg, series=series, **kw)
    if spec.get("scenario"):
        rt.venue.apply_scenario(spec["scenario"])
    for sym, at_day in (spec.get("delist") or {}).items():
        rt.delist(sym, day(at_day))
    mon = InvariantMonitor(rt)
    t0 = time.perf_counter()
    rt.run(day(spec["start_day"]), day(spec["end_day"]), on_minute=mon)
    wall = time.perf_counter() - t0
    daily = {}
    for ts, eq in rt.equity_curve:
        daily[str(ts.date())] = float(eq)
    dates = sorted(daily)
    eq0 = float(cfg["paper_risk"]["starting_equity_usdt"])
    eqs = [eq0] + [daily[d] for d in dates]
    pnl = dict(zip(dates, np.diff(eqs).tolist()))
    s = rt.summary()
    return {"spec": {k: v for k, v in spec.items() if k != "path"}, "daily_pnl": pnl,
            "trades": extract_trades(rt.ledger, rt.instruments), "invariants": mon.finalize(), "wall_seconds": round(wall, 1),
            "summary": {k: s[k] for k in ("cycles", "cycle_status_counts", "fills", "fees_paid", "funding_paid",
                                          "admission_rejections", "risk_watchdog_latency", "protect_cycle_latency_ms",
                                          "decision_cycle_latency_ms", "phi", "graph")},
            "decisions": [e["payload"] for e in rt.ledger.events("decision_record")],
            "plans": {e["payload"]["candidate_id"]: e["payload"]["metrics"]["utility_lcb_quote"]
                      for e in rt.ledger.events("candidate_plan")}}


def run_many(specs: list[dict], workers: int = 4) -> list[dict]:
    with ProcessPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(run_replay, specs))


def _dump(name, obj):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(obj, indent=2, default=str))


# ------------------------------------------------------------------ phases
def phase_freeze() -> dict:
    log = TrialLog(LOG)
    m = freeze(build_manifest(), MANIFEST)
    if not any(e.get("kind") == "manifest_frozen" for e in log.entries()):
        log.append({"kind": "manifest_frozen", "manifest_sha256": m["manifest_sha256"],
                    "provenance": provenance.capture({"fixture_sha256": m["data"]["fixture_sha256"]})})
    return m


def phase_dev(workers: int = 4) -> dict:
    m = load_verified(MANIFEST)
    log = TrialLog(LOG)
    if log.count("candidate_frozen"):
        raise ProtocolViolation("candidate already frozen; re-running selection would be a second search")
    a, b = m["periods"]["dev"]
    specs = [{"trial_id": t["trial_id"], "overrides": {"quant": t["quant"]}, "start_day": a, "end_day": b}
             for t in m["trials"]][: m["max_trials"]]
    results = run_many(specs, workers)
    eq0 = 10000.0
    emb = embargo_seconds(m)
    folds = day_folds(day(a), day(b), m["periods"]["dev_fold_days"])
    table = []
    for t, r in zip(m["trials"], results):
        trades = [x for x in r["trades"]]
        fold_pnl = [round(sum(float(x["net_pnl_quote"]) for x in test_trades_in(trades, f0, f1)), 4) for f0, f1 in folds]
        st = daily_stats(list(r["daily_pnl"].values()), eq0)
        row = {"trial_id": t["trial_id"], "params": t["quant"], "objective_dev_net_trade_pnl": round(sum(fold_pnl), 4),
               "fold_pnl": fold_pnl, "daily": st, "trades": trade_stats([float(x["net_pnl_quote"]) for x in trades]),
               "effective_trades": effective_trades(trades, m["splits"]["label_horizon_seconds"]),
               "invariants_passed": r["invariants"]["passed"], "invariant_violations": r["invariants"]["violations"][:5],
               "wall_seconds": r["wall_seconds"]}
        row["meets_constraints"] = row["invariants_passed"] and st["max_drawdown_pct"] <= m["constraints"]["dev_max_drawdown_pct"]
        table.append(row)
        log.append({"kind": "trial", "phase": "dev", **{k: row[k] for k in ("trial_id", "params", "objective_dev_net_trade_pnl",
                    "fold_pnl", "effective_trades", "invariants_passed", "meets_constraints")},
                    "sharpe_daily": st["sharpe_daily"], "max_drawdown_pct": st["max_drawdown_pct"]})
    # walk-forward estimate of the SELECTION PROCEDURE: pick on purged folds < k, score on fold k
    wf = []
    for k in range(1, len(folds)):
        f0, f1 = folds[k]
        best, best_score = None, None
        for t, r in zip(m["trials"], results):
            tr = train_trades_before(r["trades"], f0, emb, day(a))
            score = sum(float(x["net_pnl_quote"]) for x in tr)
            if best_score is None or score > best_score:
                best, best_score = (t, r), score
        oos = sum(float(x["net_pnl_quote"]) for x in test_trades_in(best[1]["trades"], f0, f1))
        wf.append({"fold": k, "selected": best[0]["trial_id"], "train_score": round(best_score, 4), "oos_fold_pnl": round(oos, 4)})
    dates = sorted(results[0]["daily_pnl"])
    matrix = np.array([[r["daily_pnl"].get(d, 0.0) for r in results] for d in dates])
    pbo = pbo_cscv(matrix, 8)
    ok = [row for row in table if row["meets_constraints"]]
    best = max(ok, key=lambda r: (r["objective_dev_net_trade_pnl"], -r["trades"]["trades"])) if ok else None
    no_edge = best is None or best["objective_dev_net_trade_pnl"] <= 0
    chosen = table[0] if no_edge else best
    candidate = {"trial_id": chosen["trial_id"], "quant": chosen["params"], "selector": "deterministic_rank_v1 (P0) and "
                 "phi (P3) evaluated", "manifest_sha256": m["manifest_sha256"]}
    trial_sharpes = [row["daily"]["sharpe_daily"] for row in table]
    dsr_dev = deflated_sharpe(list(results[[t["trial_id"] for t in m["trials"]].index(chosen["trial_id"])]["daily_pnl"].values()),
                              trial_sharpes)
    log.append({"kind": "candidate_frozen", "candidate": candidate, "candidate_sha256": sha256_hex(canonical_json(candidate)),
                "no_verified_edge_on_dev": no_edge, "pbo": pbo.get("pbo")})
    out = {"trials": table, "walk_forward_selection": wf,
           "walk_forward_oos_total": round(sum(w["oos_fold_pnl"] for w in wf), 4), "pbo": pbo, "dsr_dev_selected": dsr_dev,
           "selected": candidate, "no_verified_edge_on_dev": no_edge, "trial_sharpes_daily": trial_sharpes}
    _dump("dev_trials.json", out)
    return out


def _paired(a: dict, b: dict) -> dict:
    days = sorted(set(a) | set(b))
    diff = [a.get(d, 0.0) - b.get(d, 0.0) for d in days]
    ci = bootstrap_ci(diff)
    return {"days": len(days), "mean_daily_diff": round(float(np.mean(diff)), 4), "total_diff": round(float(sum(diff)), 4),
            "ci90_mean_daily_diff": ci, "reading": "inconclusive" if ci is None or ci[0] <= 0 <= ci[1] else
            ("positive" if ci[0] > 0 else "negative")}


def _isolation(res: dict) -> dict | None:
    recs = [d for d in res["decisions"] if d["selector"] == "phi_decision_maker" and d["candidate_ids"]]
    if not recs:
        return None
    agree = sum(r["selected_id"] == max(r["candidate_ids"], key=lambda c: (float(res["plans"][c]), c)) for r in recs)
    return {"decisions": len(recs), "agree_with_deterministic_rank": agree,
            "abstained": sum(r["selected_id"] == "ABSTAIN" for r in recs)}


def _candidate(m):
    frozen = [e for e in TrialLog(LOG).entries() if e.get("kind") == "candidate_frozen"]
    if not frozen:
        raise ProtocolViolation("run the dev phase first")
    return frozen[-1]["candidate"]


ABLATIONS = {"P0_deterministic": dict(use_phi=False, phi_features=()),
             "P1_phi_screening": dict(use_phi=True, phi_features=("evidence",)),
             "P2_screening_analysis": dict(use_phi=True, phi_features=("evidence", "analysis")),
             "P3_all_four_roles": dict(use_phi=True, phi_features=("evidence", "analysis", "decision", "risk")),
             "P4_all_four_no_onchain": dict(use_phi=True, phi_features=("evidence", "analysis", "decision", "risk"),
                                           use_onchain=False)}

SCENARIOS = [  # execution / venue perturbations (declared, seeded)
    {"name": "fees_x2", "fee_mult": 2.0},
    {"name": "slippage_+8bps", "extra_slippage_bps": 8},
    {"name": "latency_2_bars", "latency_bars": 2},
    {"name": "rejects_20pct", "reject_prob": 0.2, "seed": 1},
    {"name": "thin_liquidity_x0.02", "liquidity_mult": 0.02},
    {"name": "stop_gap_+40bps", "stop_gap_extra_bps": 40},
    {"name": "funding_x5", "funding_mult": 5.0},
    {"name": "outages_6x30min", "outages": "random", "outage_count": 6, "outage_minutes": 30, "seed": 3},
    {"name": "combined_stress", "fee_mult": 1.5, "extra_slippage_bps": 5, "latency_bars": 1, "reject_prob": 0.1,
     "stop_gap_extra_bps": 25, "funding_mult": 3.0, "seed": 5},
]


def phase_robustness(workers: int = 4, n_paths: int | None = None) -> dict:
    from ..config import load_config
    from .leakage import causality_check, leaky_features_for_control
    from .montecarlo import cost_mc, daily_bootstrap
    from .regimes import label_days, slice_report
    from .. import quant
    m = load_verified(MANIFEST)
    cand = _candidate(m)
    ov = {"quant": cand["quant"]}
    a, b = m["periods"]["dev"]
    n_paths = n_paths or N_PATHS
    log = TrialLog(LOG)
    specs = [{"name": k, "overrides": ov, "start_day": a, "end_day": b, **v} for k, v in ABLATIONS.items()]
    specs += [{"name": "neg_random_signal", "overrides": {"quant": {**cand["quant"], "negative_control": "random_signal"}},
               "start_day": a, "end_day": b},
              {"name": "neg_delayed_signal_16", "overrides": {"quant": {**cand["quant"], "negative_control": "delay_signal:16"}},
               "start_day": a, "end_day": b},
              {"name": "delisting_ETH_midway", "overrides": ov, "start_day": a, "end_day": b, "delist": {"ETHUSDT": (a + b) // 2}}]
    specs += [{"name": f"exec:{s['name']}", "overrides": ov, "start_day": a, "end_day": b, "scenario": s} for s in SCENARIOS]
    specs += [{"name": f"path{i:02d}", "overrides": ov, "start_day": PATH_WINDOW[0], "end_day": PATH_WINDOW[1],
               "path_seed": 1000 + i, "path": {"pool": [a, b], "days": PATH_WINDOW[1], "vol_shock_prob": 0.15 if i % 2 else 0.0,
                        "decorrelate_prob": 0.15 if i % 4 == 3 else 0.0}} for i in range(n_paths)]
    t0 = time.perf_counter()
    res = run_many(specs, workers)
    wall = time.perf_counter() - t0
    by = {s["name"]: r for s, r in zip(specs, res)}
    eq0 = 10000.0
    summary = lambda r: {**daily_stats(list(r["daily_pnl"].values()), eq0), **trade_stats(  # noqa: E731
        [float(t["net_pnl_quote"]) for t in r["trades"]]), "invariants_passed": r["invariants"]["passed"],
        "violations": r["invariants"]["violations"][:3], "phi_calls": (r["summary"]["phi"] or {}).get("calls", 0),
        "phi_tokens_in_est": (r["summary"]["phi"] or {}).get("tokens_in_total", 0),
        "admission_rejections": r["summary"]["admission_rejections"]}
    base = by["P0_deterministic"]
    out = {"wall_seconds": round(wall, 1), "ablations": {k: summary(by[k]) for k in ABLATIONS},
           "paired_vs_P0": {k: _paired(by[k]["daily_pnl"], base["daily_pnl"]) for k in ABLATIONS if k != "P0_deterministic"},
           "cash_and_passive": _passive(base, eq0, a, b),
           "decision_isolation_P3": _isolation(by["P3_all_four_roles"]),
           "negative_controls": {k: summary(by[k]) for k in ("neg_random_signal", "neg_delayed_signal_16")},
           "delisting": summary(by["delisting_ETH_midway"]),
           "execution_perturbations": {s["name"]: summary(by[f"exec:{s['name']}"]) for s in SCENARIOS}}
    paths = [by[f"path{i:02d}"] for i in range(n_paths)]
    pnl = np.array([sum(p["daily_pnl"].values()) for p in paths])
    out["market_path_mc"] = {"paths": n_paths, "days_each": PATH_WINDOW[1] - PATH_WINDOW[0], "method": "joint BTC/ETH whole-day block bootstrap of the "
                             "dev pool; half the paths with 2x vol shocks on 15% of days, a quarter with decorrelation",
                             "net_pnl_q05_q50_q95": [round(float(np.quantile(pnl, q)), 4) for q in (.05, .5, .95)],
                             "p_net_negative": round(float((pnl < 0).mean()), 4),
                             "max_drawdown_q95": round(float(np.quantile([daily_stats(list(p["daily_pnl"].values()), eq0)
                                                                          ["max_drawdown"] for p in paths], .95)), 4),
                             "invariant_failures": sum(not p["invariants"]["passed"] for p in paths),
                             "liquidations": sum(p["invariants"]["liquidations"] for p in paths)}
    out["cost_mc_dev"] = cost_mc(base["trades"], 5000)
    out["daily_bootstrap_dev"] = daily_bootstrap(list(base["daily_pnl"].values()), eq0)
    dates = [np.datetime64(d).astype(object) for d in sorted(base["daily_pnl"])]
    cfg = load_config()
    labels = label_days(fixture(), dates, float(cfg["quant"]["funding_extreme_rate"]))
    out["regimes_dev_P0"] = slice_report(base["trades"], base["daily_pnl"], labels, 14400, m["statistics"]["min_effective_trades"])
    out["leakage"] = {"features_v1": causality_check(quant.features), "positive_control_leaky_feature":
                      causality_check(leaky_features_for_control)}
    out["all_runs_invariants_passed"] = all(r["invariants"]["passed"] for r in res)
    out["runs"] = len(res)
    log.append({"kind": "robustness", "runs": len(res), "all_invariants_passed": out["all_runs_invariants_passed"],
                "p0_dev_net": out["ablations"]["P0_deterministic"]["net_pnl"],
                "neg_random_net": out["negative_controls"]["neg_random_signal"]["net_pnl"],
                "path_mc_p_negative": out["market_path_mc"]["p_net_negative"]})
    _dump("robustness_dev.json", out)
    return out


def _passive(base, eq0, a, b):
    series = fixture()
    closes = {s: {bb.end.date(): bb.close for bb in series[s].bars} for s in series}
    dates = sorted(base["daily_pnl"])
    from datetime import date
    first = {s: next(bb.close for bb in series[s].bars if bb.end >= day(a)) for s in series}
    gross = []
    for t in base["trades"]:
        gross.append(t["entry_notional"])
    expo = (float(np.mean(gross)) * len(gross) * 4 / 24 / max(len(dates), 1)) / eq0 if gross else 0.0
    vals = [sum(eq0 * expo / 2 * (closes[s][date.fromisoformat(d)] / first[s] - 1) for s in series) for d in dates]
    pnl = np.diff([0.0] + vals)
    return {"cash_net": 0.0, "passive_long_exposure_matched_net": round(float(pnl.sum()), 4),
            "avg_gross_exposure_fraction_matched": round(expo, 5),
            "paired_P0_minus_passive": _paired(base["daily_pnl"], dict(zip(dates, pnl.tolist())))}


def phase_holdout(workers: int = 4) -> dict:
    from .montecarlo import cost_mc
    m = load_verified(MANIFEST)
    log = TrialLog(LOG)
    cand = _candidate(m)
    HoldoutSeal(log, m).open_once(cand)  # consumes the single evaluation before anything runs
    a, b = m["periods"]["holdout"]
    ov = {"quant": cand["quant"]}
    specs = [{"name": "P0", "overrides": ov, "start_day": a, "end_day": b, **ABLATIONS["P0_deterministic"]},
             {"name": "P3", "overrides": ov, "start_day": a, "end_day": b, **ABLATIONS["P3_all_four_roles"]}]
    p0, p3 = run_many(specs, workers)
    eq0 = 10000.0
    dev = json.loads((OUT / "dev_trials.json").read_text())
    st = daily_stats(list(p0["daily_pnl"].values()), eq0)
    n_eff = effective_trades(p0["trades"], m["splits"]["label_horizon_seconds"])
    out = {"candidate": cand, "P0": {**st, **trade_stats([float(t["net_pnl_quote"]) for t in p0["trades"]]),
                                     "effective_trades": n_eff, "invariants": p0["invariants"]},
           "P3": {**daily_stats(list(p3["daily_pnl"].values()), eq0), "invariants": p3["invariants"],
                  "decision_isolation": _isolation(p3)},
           "ci90_mean_daily_P0": bootstrap_ci(list(p0["daily_pnl"].values())),
           "dsr": deflated_sharpe(list(p0["daily_pnl"].values()), dev["trial_sharpes_daily"]),
           "cost_mc": cost_mc(p0["trades"], 5000), "paired_P3_vs_P0": _paired(p3["daily_pnl"], p0["daily_pnl"])}
    log.append({"kind": "holdout_result", "net_pnl": st["net_pnl"], "effective_trades": n_eff,
                "invariants_passed": p0["invariants"]["passed"] and p3["invariants"]["passed"]})
    _dump("holdout.json", out)
    return out


def verdicts() -> dict:
    m = load_verified(MANIFEST)
    dev = json.loads((OUT / "dev_trials.json").read_text())
    rob = json.loads((OUT / "robustness_dev.json").read_text())
    hold = json.loads((OUT / "holdout.json").read_text()) if (OUT / "holdout.json").exists() else None
    eq0, g = 10000.0, {}
    inv_ok = all(t["invariants_passed"] for t in dev["trials"]) and rob["all_runs_invariants_passed"] and (
        hold is None or (hold["P0"]["invariants"]["passed"] and hold["P3"]["invariants"]["passed"]))
    g["H1_safety_invariants"] = ("PASS" if inv_ok else "FAIL", f"{len(dev['trials']) + rob['runs'] + (2 if hold else 0)} runs")
    g["L1_leakage_checks"] = ("PASS" if rob["leakage"]["features_v1"]["causal"] and not
                              rob["leakage"]["positive_control_leaky_feature"]["causal"] else "FAIL",
                              "registered features causal; leaky positive control detected")
    if hold:
        n_eff, ci = hold["P0"]["effective_trades"], hold["ci90_mean_daily_P0"]
        syn = lambda v: f"{v} (SYNTHETIC ONLY)"  # noqa: E731
        g["E2_sample_size"] = (syn("INCONCLUSIVE" if n_eff < m["statistics"]["min_effective_trades"] else "PASS"),
                               f"effective trades {n_eff} vs required {m['statistics']['min_effective_trades']}")
        e1 = hold["P0"]["net_pnl"] > 0 and ci is not None and ci[0] > 0
        g["E1_holdout_net_positive"] = (syn("PASS" if e1 else "FAIL"), f"net {hold['P0']['net_pnl']} USDT, CI {ci}")
        dsr = hold["dsr"].get("dsr")
        g["E3_deflated_sharpe"] = (syn("INCONCLUSIVE" if dsr is None else ("PASS" if dsr >= 0.95 else "FAIL")), f"DSR {dsr}")
        pbo = dev["pbo"].get("pbo")
        g["E4_pbo"] = (syn("INCONCLUSIVE" if pbo is None else ("PASS" if pbo <= 0.20 else "FAIL")), f"PBO {pbo}")
        e5 = hold["P0"]["max_drawdown_pct"] <= 3 and (hold["P0"]["cvar95_daily"] or 0) >= -0.01 * eq0
        g["E5_tail_risk"] = (syn("PASS" if e5 else "FAIL"), f"maxDD {hold['P0']['max_drawdown_pct']}%, CVaR95 {hold['P0']['cvar95_daily']}")
        pneg = hold["cost_mc"].get("p_total_negative")
        g["E6_cost_robustness"] = (syn("INCONCLUSIVE" if pneg is None else ("PASS" if pneg <= 0.20 else "FAIL")),
                                   f"P(total<0) {pneg}")
    g["E7_phi_incremental_value"] = ("UNTESTED", "fake Phi reproduces the deterministic ranker by construction; real Phi not run")
    g["R1_real_phi_measured"] = ("UNTESTED", "no GPU / weights in this environment; use `python -m cqc bench-phi`")
    g["D1_real_point_in_time_data"] = ("UNTESTED", "no real exchange/on-chain captures; all economics are synthetic")
    g["INVESTMENT_VERDICT"] = ("UNTESTED", "no real-data, real-model evidence exists; synthetic results are engineering-only")
    return g


def phase_report() -> dict:
    g = verdicts()
    _dump("gates.json", g)
    return g


def main(phase: str, workers: int = 4):
    phases = {"freeze": phase_freeze, "dev": lambda: phase_dev(workers), "robustness": lambda: phase_robustness(workers),
              "holdout": lambda: phase_holdout(workers), "report": phase_report}
    if phase == "all":
        return {p: phases[p]() for p in ("freeze", "dev", "robustness", "holdout", "report")}
    return phases[phase]()

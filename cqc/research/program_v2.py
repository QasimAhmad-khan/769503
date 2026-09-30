"""research_v2: repeatable alpha-discovery and simulation program.

    python -m cqc validate2 --phase {propose,freeze,screen,dev,mc,robust,runtime,gates,holdout,report,all}

All state lives in research/v2/ (frozen manifest + hash-chained trial log) and reports/validation_v2/.
Every hypothesis proposal, critique, trial, diagnosis and gate decision is logged. The research_v1
holdout is spent and never used here. With the synthetic fixture and the fake Phi backend, alpha and
Phi-value conclusions are UNTESTED by construction; this program exercises the machinery.
"""
from __future__ import annotations

import json
import math
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import timedelta
from functools import lru_cache
from pathlib import Path

import numpy as np

from ..util import canonical_json, sha256_hex, utc
from . import provenance, signals as S, vbt
from .manifest import HoldoutSeal, ProtocolViolation, TrialLog, freeze, load_verified
from .metrics import bootstrap_ci, deflated_sharpe, pbo_cscv, stationary_bootstrap_indices

ROOT = Path(__file__).resolve().parents[2]
RESEARCH = ROOT / "research" / "v2"
OUT = ROOT / "reports" / "validation_v2"
MANIFEST, LOG = RESEARCH / "manifest_v2.json", RESEARCH / "trial_log.jsonl"
V1_LOG = ROOT / "research" / "trial_log.jsonl"
START, SEED, DAYS = utc(2026, 6, 1), 4242, 180
PERIODS = {"warmup": [0, 10], "train": [10, 100], "dev": [100, 145], "dev_fold_days": 9, "embargo_gap": [145, 146],
           "holdout": [146, 179]}
BARS_PER_DAY = 96
TRIAL_BUDGET, INITIAL_ROUND_MAX = 48, 30
MC = {"primary": {"scheme": "stationary", "block_bars": 96, "paths": 5000},
      "sensitivity": [{"scheme": s, "block_bars": b, "paths": 1000} for s, b in
                      (("moving", 16), ("moving", 96), ("moving", 288), ("stationary", 16), ("stationary", 288))],
      "path_days": 60, "warm_days": 15, "portfolio_blocks_days": [1, 3, 7], "portfolio_paths": 5000}
EXEC_MC = {"draws": 5000, "rho": 0.6, "seed": 77}
GATES = {
    "G1_walkforward_oos": "purged walk-forward OOS mean daily MTM return: 90% stationary-bootstrap CI lower bound > 0",
    "G2_effective_sample": "candidate effective (non-overlapping) trades on dev >= 30",
    "G3_deflated_sharpe": "DSR >= 0.95 (daily MTM, N = all v1+v2 logged trials)",
    "G4_pbo": "PBO <= 0.20 (CSCV over all v2 trials, 10 blocks)",
    "G5_parameter_stability": "every 1-step grid neighbour: dev Sharpe > 0 and >= 50% of candidate",
    "G6_negative_controls": "candidate dev Sharpe > 95th pct of 200 exposure-matched block-randomized AND 200 time-shifted signals",
    "G7_cost_robustness": "break-even cost multiplier >= 2.0 AND correlated execution MC P(net < 0) <= 0.20",
    "G8_latency": "break-even latency >= 2 bars (30 min)",
    "G9_market_path_mc": "primary MC: median net > 0 AND P(max drawdown > 3%) <= 0.05; sign of median stable across block configs",
    "G10_regimes": "no powered regime (>= 30 effective trades) with expectancy CI upper bound < 0",
    "G11_event_driven_consistency": "event-driven replay on dev: invariants pass AND net > 0 (screener/simulator agree in sign)",
    "S1_stress_safety": "every stress scenario: hard invariants pass (caps, stop present, reconciliation, no reversal, no "
                        "entry outside NORMAL), no liquidation, and worst trade loss <= equity x max_stress_loss_fraction "
                        "x max(1, |scenario move| / stress_joint_move); non-price scenarios: <= 2 x per-trade budget",
    "H1_holdout": "(only if a candidate survives G1-G11) sealed holdout once: net > 0, daily-MTM CI lower > 0, eff. trades >= 20, "
                  "maxDD <= 3%, DSR >= 0.95",
}


# ------------------------------------------------------------------ data (per-process caches)
@lru_cache(maxsize=1)
def fixture():
    from ..market import synthetic_market
    return synthetic_market(START, DAYS, seed=SEED)


def onchain_series(ends: np.ndarray, extra_delay_s: float = 0.0, seed: int = 11) -> np.ndarray:
    """Synthetic finalized-chain activity as known at each bar end: block every 600 s, 6 confirmations."""
    genesis = utc(2026, 1, 1).timestamp()
    h = np.floor((ends - genesis - 6 * 600 - extra_delay_s) / 600).astype(np.int64)
    cache, out = {}, np.full(len(ends), np.nan)
    for k, hh in enumerate(h):
        if hh >= 0:
            if hh not in cache:
                cache[hh] = float(np.random.default_rng((seed, int(hh))).poisson(40))
            out[k] = cache[hh]
    return out


@lru_cache(maxsize=8)
def ctxs(offset_min: int = 0, onchain_delay_s: float = 0.0):
    from ..market import aggregate
    out = {}
    for sym, s in fixture().items():
        bars = aggregate(s.bars[offset_min:], 900)
        out[sym] = vbt.build_ctx(bars, None)
        out[sym]["onchain"] = onchain_series(out[sym]["time"], onchain_delay_s)
    return out


def bar_index(day: float, offset_min: int = 0) -> int:
    return int(day * BARS_PER_DAY - offset_min / 15)


def day_of(ts) -> str:
    from datetime import datetime
    from ..util import UTC
    return str(datetime.fromtimestamp(ts, UTC).date())


# ------------------------------------------------------------------ trial evaluation
def evaluate(spec: dict) -> dict:
    """One simulation. spec: hypothesis, params, start/end day, exec, offset, onchain_delay, dirs_override, seed."""
    c = ctxs(spec.get("offset", 0), spec.get("onchain_delay", 0.0))
    if spec.get("path_seed") is not None:
        c = resample_path(c, spec["path_seed"], spec["scheme"], spec["block_bars"])
    dirs = spec.get("dirs") or {s: S.signals(spec["hypothesis"], c[s], spec["params"]) for s in c}
    if spec.get("control"):
        dirs = control_dirs(dirs, spec["control"], spec["control_seed"])
    a = bar_index(spec["start_day"], spec.get("offset", 0)) if spec.get("path_seed") is None else MC["warm_days"] * BARS_PER_DAY
    b = bar_index(spec["end_day"], spec.get("offset", 0)) if spec.get("path_seed") is None else min(len(c[s]["close"]) for s in c)
    res = vbt.run(c, dirs, a, b, exec_params=spec.get("exec"), seed=spec.get("seed", 0))
    t = res["time"]
    days = sorted(set(day_of(x) for x in t))
    last = {}
    for x, e in zip(t, res["equity"]):
        last[day_of(x)] = e
    eq = [10000.0] + [last[d] for d in days]
    daily = dict(zip(days, (np.diff(eq) / np.array(eq[:-1])).tolist()))
    trades = [dict(tr, opened=day_of(c[tr["symbol"]]["time"][tr["open_i"]])) for tr in res["trades"]]
    out = {"summary": vbt.summarize(res), "daily": daily, "trades_n": len(trades)}
    if spec.get("keep_trades"):
        out["trades"] = trades
    return out


def run_many(specs, workers=4):
    with ProcessPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(evaluate, specs, chunksize=max(1, len(specs) // (workers * 8))))


def sharpe(daily: dict | list, days: list | None = None) -> float | None:
    x = np.array([daily[d] for d in days] if days is not None else list(daily.values()) if isinstance(daily, dict) else daily)
    return float(x.mean() / x.std(ddof=1) * math.sqrt(365)) if len(x) > 2 and x.std(ddof=1) > 0 else None


def days_between(a: int, b: int) -> list[str]:
    return [str((START + timedelta(days=d)).date()) for d in range(a, b)]


# ------------------------------------------------------------------ Monte Carlo helpers
def resample_path(c: dict, seed: int, scheme: str, block_bars: int) -> dict:
    """Joint BTC/ETH resampling of 15-minute bar *returns and bar shapes* from the train+dev pool, preserving
    within-block serial dependence and cross-asset dependence (same indices for both symbols)."""
    rng = np.random.default_rng(seed)
    syms = sorted(c)
    lo, hi = bar_index(PERIODS["train"][0]), bar_index(PERIODS["dev"][1])
    n_out = MC["path_days"] * BARS_PER_DAY
    pool = hi - lo
    if scheme == "stationary":
        idx = lo + stationary_bootstrap_indices(pool, block_bars, rng)[:n_out] if pool >= n_out else None
        if idx is None or len(idx) < n_out:
            idx = lo + np.concatenate([stationary_bootstrap_indices(pool, block_bars, rng) for _ in range(n_out // pool + 1)])[:n_out]
    else:
        starts = rng.integers(lo, hi - block_bars, n_out // block_bars + 1)
        idx = np.concatenate([np.arange(s, s + block_bars) for s in starts])[:n_out]
    out = {}
    t0 = START.timestamp() + 900
    for s in syms:
        src = c[s]
        prev = src["close"][idx - 1]
        r = np.log(src["close"][idx] / prev)
        close = src["close"][lo] * np.exp(np.cumsum(r))
        base = np.concatenate([[src["close"][lo]], close[:-1]])
        out[s] = {"time": t0 + 900 * np.arange(n_out), "close": close,
                  "open": base * src["open"][idx] / prev, "high": base * src["high"][idx] / prev,
                  "low": base * src["low"][idx] / prev, "volume": src["volume"][idx],
                  "spread_bps": src["spread_bps"][idx], "funding": src["funding"][idx], "onchain": src["onchain"][idx]}
        out[s]["high"] = np.maximum.reduce([out[s]["high"], out[s]["open"], out[s]["close"]])
        out[s]["low"] = np.minimum.reduce([out[s]["low"], out[s]["open"], out[s]["close"]])
    return out


def control_dirs(dirs: dict, kind: str, seed: int) -> dict:
    """Negative controls with matched exposure: block-permuted (1-day blocks) or circularly time-shifted signal."""
    rng = np.random.default_rng(seed)
    out = {}
    for s, d in dirs.items():
        d = np.asarray(d)
        if kind == "block_randomized":
            nb = len(d) // BARS_PER_DAY
            blocks = [d[i * BARS_PER_DAY:(i + 1) * BARS_PER_DAY] for i in range(nb)]
            order = rng.permutation(nb)
            out[s] = np.concatenate([blocks[i] for i in order] + [d[nb * BARS_PER_DAY:]])
        else:  # time_shifted
            k = int(rng.integers(BARS_PER_DAY, 30 * BARS_PER_DAY))
            out[s] = np.roll(d, k)
    return out


def dist(x) -> dict:
    x = np.asarray([v for v in x if v is not None and math.isfinite(v)], float)
    if not len(x):
        return {}
    return {"q05": round(float(np.quantile(x, .05)), 4), "q50": round(float(np.quantile(x, .5)), 4),
            "q95": round(float(np.quantile(x, .95)), 4), "mean": round(float(x.mean()), 4)}


def correlated_exec(rng, rho: float) -> dict:
    """Costs move together: one adverse market-stress factor loads on every cost dimension."""
    z = rng.standard_normal()
    e = rng.standard_normal(8)
    f = lambda k: rho * z + math.sqrt(1 - rho * rho) * e[k]  # noqa: E731
    stress = 1 / (1 + math.exp(-z))
    return {"fee_rate": 0.0005 * math.exp(0.25 * f(0)), "spread_mult": math.exp(0.5 * f(1)),
            "slip_bps": 2.0 * math.exp(0.6 * f(2)), "funding_mult": math.exp(0.7 * f(3)),
            "fill_prob": float(np.clip(1.0 - 0.15 * max(f(4), 0), 0.5, 1.0)), "reject_prob": float(np.clip(0.02 * math.exp(f(5)), 0, 0.3)),
            "latency_bars": int(np.clip(round(0.5 + 0.8 * f(6)), 0, 4)), "stop_gap_bps": 10.0 * math.exp(0.7 * f(7)),
            "liquidity_frac": 0.02 * math.exp(-0.5 * z), "_stress": round(stress, 4)}


# ------------------------------------------------------------------ phases
def phase_propose() -> dict:
    """Phi (fake backend here) proposes and critiques hypotheses from the closed registry; code decides."""
    from ..config import load_config
    from ..llm.phi import FakePhiBackend, PhiFailure, PhiService
    from .leakage import causality_check
    log = TrialLog(LOG)
    if log.count("hypothesis_eligibility"):
        raise ProtocolViolation("hypotheses already proposed for research_v2")
    cfg = load_config()
    phi = PhiService(cfg, FakePhiBackend())
    menu = [{"template": k, "family": v["family"], "data": v["data"], "mechanism": v["mechanism"]} for k, v in S.REGISTRY.items()]
    available = ["ohlc", "funding", "spread", "onchain_finalized"]
    now = utc(2026, 10, 1)
    prop = phi.run("hypothesis_proposer", {"menu": menu, "available_data": available, "synthetic": True}, now=now,
                   correlation_id="v2_propose", allowed_ids=list(S.REGISTRY))
    log.append({"kind": "hypothesis_proposed", "producer": prop["producer"],
                "proposals": [{k: p[k] for k in ("template", "rationale", "data_needed", "falsification")}
                              for p in prop["proposals"]]})
    elig = {}
    for p in prop["proposals"]:
        t = p["template"]
        crit = phi.run("hypothesis_critic", {"template": t, "family": S.REGISTRY[t]["family"], "synthetic": True,
                                             "data_available": all(d in available for d in S.REGISTRY[t]["data"])},
                       now=now, correlation_id=f"v2_critic_{t}", allowed_ids=list(S.REGISTRY))
        g = S.grid(t)[0]

        def fn(closes, funding, _p, t=t, g=g):
            n = len(closes)
            ctx = {"close": closes, "open": np.r_[closes[0], closes[:-1]], "high": closes * 1.002, "low": closes * 0.998,
                   "volume": np.ones(n), "spread_bps": 2 + np.abs(funding) * 1e4, "funding": funding,
                   "onchain": np.abs(funding) * 1e5}
            return {"dir": S.signals(t, ctx, g)}
        causal = causality_check(fn, n=500)["causal"]
        ok = crit["verdict"] in ("test", "reject_leakage_risk") and causal  # leakage concern is settled by the test
        elig[t] = {"critic_verdict": crit["verdict"], "concerns": crit["concerns"], "causality_check": causal,
                   "eligible": ok, "grid_size": len(S.grid(t))}
        log.append({"kind": "hypothesis_critiqued", "template": t, "verdict": crit["verdict"], "concerns": crit["concerns"],
                    "causal": causal})
    log.append({"kind": "hypothesis_eligibility", "eligible": sorted(k for k, v in elig.items() if v["eligible"]),
                "rule": "proposed AND critic did not demand missing data/untestable AND causality check passes"})
    return elig


def build_manifest(elig: dict) -> dict:
    trials = [{"trial_id": f"V2T{i:02d}", "hypothesis": h, "params": p, "round": 0}
              for i, (h, p) in enumerate((h, p) for h in sorted(k for k, v in elig.items() if v["eligible"])
                                         for p in S.grid(h))]
    if len(trials) > INITIAL_ROUND_MAX:
        raise ProtocolViolation("initial round exceeds its budget")
    return {
        "manifest_version": "research_v2",
        "status": "SYNTHETIC fixture + FAKE Phi: machinery demonstration; alpha and Phi value UNTESTED",
        "supersedes": "research_v1 (its holdout is spent and is never treated as fresh evidence)",
        "data": {"kind": "synthetic", "seed": SEED, "days": DAYS, "start": START.isoformat(),
                 "fixture_sha256": provenance.series_sha256(fixture()),
                 "availability": {"bars_15m": "bar end + 1 s", "funding_estimate": "known at bar end, settled 8-hourly",
                                  "onchain_finalized": "block time + 6 confirmations (3600 s); stress tests add delay",
                                  "spread": "top of book at bar end"},
                 "real_sources_required": {"trades_l2_marks_funding": "exchange capture with receive timestamps; "
                                           "dates must mirror the split below", "onchain": "forward capture with finality; "
                                           "excluded from backtests until availability is proven"}},
        "universe": {"symbols": ["BTCUSDT-PERP", "ETHUSDT-PERP"], "survivorship": "fixed survivor universe; a real study "
                     "needs a point-in-time listing universe"},
        "dates_days": PERIODS, "embargo_seconds": max(14400, 28800, 3600),
        "costs": {"taker_fee_rate": 0.0005, "spread": "data half-spread per leg", "slippage_bps_per_leg": 2.0,
                  "stop_gap_bps": 10.0, "funding": "8-hourly on position", "fill_prob": 1.0, "liquidity_frac_of_bar_volume": 0.02,
                  "latency_bars": 0, "execution_mc": EXEC_MC},
        "exits_fixed": S.EXIT_GRID, "entry_gate": {"lcb_z": 1.64, "min_effective_samples": 20, "lookback_bars": 2000},
        "risk": vbt.RISK, "hypotheses": elig, "parameter_ranges": {h: S.REGISTRY[h]["grid"] for h in elig},
        "trials_round0": trials, "trial_budget_total": TRIAL_BUDGET,
        "iteration_rule": "extra rounds only after a logged defect fix or a pre-declared bounded change; total trials <= budget",
        "train_eligibility": "train Sharpe > 0 AND train net > 0 AND train effective trades >= 30",
        "selection": "walk-forward: at each dev fold pick the train-eligible trial with best daily-MTM Sharpe on purged "
                     "data before the fold; final candidate = best on [train start, dev end - embargo]",
        "min_effective_trades": {"dev": 30, "holdout": 20}, "gates": GATES, "monte_carlo": MC,
        "sharpe": "annualized sqrt(365) from daily mark-to-market portfolio returns after all costs",
        "dsr_trials": "all logged trials across research_v1 and research_v2",
        "phi": "fake_phi_rules_v2; real-Phi benchmark, fine-tuning and prompt-seed robustness UNTESTED here",
    }


def phase_freeze():
    log = TrialLog(LOG)
    elig = [e for e in log.entries() if e.get("kind") == "hypothesis_critiqued"]
    if not elig:
        raise ProtocolViolation("run the propose phase first")
    table = {}
    for e in elig:
        table[e["template"]] = {"critic_verdict": e["verdict"], "concerns": e["concerns"], "causality_check": e["causal"],
                                "eligible": e["verdict"] in ("test", "reject_leakage_risk") and e["causal"]}
    m = freeze(build_manifest(table), MANIFEST)
    if not log.count("manifest_frozen"):
        log.append({"kind": "manifest_frozen", "manifest_sha256": m["manifest_sha256"],
                    "provenance": provenance.capture({"fixture_sha256": m["data"]["fixture_sha256"]})})
    return m


def _dump(name, obj):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(obj, indent=2, default=str))


def _load(name):
    p = OUT / name
    return json.loads(p.read_text()) if p.exists() else None


def phase_screen(workers=4):
    m = load_verified(MANIFEST)
    log = TrialLog(LOG)
    if log.count("trial"):
        raise ProtocolViolation("round 0 already screened")
    a, b = m["dates_days"]["train"][0], m["dates_days"]["dev"][1]
    specs = [{"hypothesis": t["hypothesis"], "params": t["params"], "start_day": a, "end_day": b, "keep_trades": True}
             for t in m["trials_round0"]]
    res = run_many(specs, workers)
    tr_days = days_between(*m["dates_days"]["train"])
    rows = []
    for t, r in zip(m["trials_round0"], res):
        tr = [x for x in r["trades"] if x["opened"] in set(tr_days)]
        n_eff, last = 0, -1
        for x in sorted(tr, key=lambda x: x["open_i"]):
            if x["open_i"] >= last:
                n_eff, last = n_eff + 1, x["close_i"]
        net = float(np.prod([1 + r["daily"].get(d, 0.0) for d in tr_days]) - 1)
        sh = sharpe(r["daily"], [d for d in tr_days if d in r["daily"]])
        elig = bool(sh is not None and sh > 0 and net > 0 and n_eff >= 30)
        row = {**t, "train_sharpe": round(sh, 4) if sh is not None else None, "train_net_pct": round(net * 100, 4),
               "train_trades": len(tr), "train_effective_trades": n_eff, "train_eligible": elig}
        rows.append(row)
        log.append({"kind": "trial", "phase": "train_screen", **{k: row[k] for k in ("trial_id", "hypothesis", "params",
                    "train_sharpe", "train_net_pct", "train_effective_trades", "train_eligible")}})
    _dump("screen_results.json", {"rows": rows, "daily": {t["trial_id"]: r["daily"] for t, r in zip(m["trials_round0"], res)},
                                  "trades": {t["trial_id"]: r["trades"] for t, r in zip(m["trials_round0"], res)}})
    return rows


def phase_dev():
    m = load_verified(MANIFEST)
    log = TrialLog(LOG)
    scr = _load("screen_results.json")
    rows = {r["trial_id"]: r for r in scr["rows"]}
    elig = [r for r in scr["rows"] if r["train_eligible"]]
    a, b = m["dates_days"]["dev"]
    fold_days = m["dates_days"]["dev_fold_days"]
    wf, oos = [], {}
    for f0 in range(a, b, fold_days):
        f1 = min(f0 + fold_days, b)
        hist = days_between(m["dates_days"]["train"][0], f0 - 1)  # 1-day embargo before each fold (purge)
        best = max(elig, key=lambda r: sharpe(scr["daily"][r["trial_id"]], [d for d in hist if d in scr["daily"][r["trial_id"]]]) or -9,
                   default=None)
        fd = days_between(f0, f1)
        if best is None:
            wf.append({"fold": [f0, f1], "selected": None})
            oos.update({d: 0.0 for d in fd})
            continue
        dly = scr["daily"][best["trial_id"]]
        oos.update({d: dly.get(d, 0.0) for d in fd})
        wf.append({"fold": [f0, f1], "selected": best["trial_id"], "oos_return_pct": round(sum(dly.get(d, 0.0) for d in fd) * 100, 4)})
    sel_days = days_between(m["dates_days"]["train"][0], b - 1)
    cand = max(elig, key=lambda r: sharpe(scr["daily"][r["trial_id"]], [d for d in sel_days if d in scr["daily"][r["trial_id"]]]) or -9,
               default=None)
    oos_vals = list(oos.values())
    ci = bootstrap_ci(oos_vals)
    dev_days = days_between(a, b)
    all_trials = [r["trial_id"] for r in scr["rows"]]
    pool_days = days_between(m["dates_days"]["train"][0], b)
    matrix = np.array([[scr["daily"][t].get(d, 0.0) for t in all_trials] for d in pool_days])
    pbo = pbo_cscv(matrix, 10)
    v1_trials = sum(1 for e in TrialLog(V1_LOG).entries() if e.get("kind") == "trial") if V1_LOG.exists() else 0
    out = {"eligible": [r["trial_id"] for r in elig], "walk_forward": wf, "oos_daily_mean": round(float(np.mean(oos_vals)), 6),
           "oos_ci90": ci, "oos_sharpe_ann": sharpe(oos_vals), "pbo": pbo, "v1_trials": v1_trials,
           "candidate": None}
    if cand:
        cd = scr["daily"][cand["trial_id"]]
        trial_sh = [sharpe(scr["daily"][t], [d for d in sel_days if d in scr["daily"][t]]) for t in all_trials]
        # DSR uses per-period Sharpe; N includes all v1 + v2 trials (expected-max term uses the v2 dispersion)
        daily_sr = [s / math.sqrt(365) for s in trial_sh if s is not None]
        n_total = v1_trials + len(all_trials)
        dsr = deflated_sharpe([cd.get(d, 0.0) for d in sel_days], daily_sr + [float(np.median(daily_sr))] * max(0, n_total - len(daily_sr)))
        dtr = [x for x in scr["trades"][cand["trial_id"]] if x["opened"] in set(dev_days)]
        n_eff, last = 0, -1
        for x in sorted(dtr, key=lambda x: x["open_i"]):
            if x["open_i"] >= last:
                n_eff, last = n_eff + 1, x["close_i"]
        out["candidate"] = {**rows[cand["trial_id"]], "dev_sharpe": sharpe(cd, [d for d in dev_days if d in cd]),
                            "dev_net_pct": round((np.prod([1 + cd.get(d, 0.0) for d in dev_days]) - 1) * 100, 4),
                            "dev_effective_trades": n_eff, "dsr": dsr, "n_trials_total": n_total}
    if not cand:  # nothing train-eligible: exercise the machinery on a clearly labeled diagnostic subject
        best = max(scr["rows"], key=lambda r: r["train_sharpe"] if r["train_sharpe"] is not None else -9)
        bd = scr["daily"][best["trial_id"]]
        out["diagnostic_subject"] = {**best, "dev_sharpe": sharpe(bd, [d for d in dev_days if d in bd]),
                                     "note": "NOT a candidate: failed train eligibility; used only to exercise MC/robustness/stress"}
    log.append({"kind": "dev_walkforward", "oos_ci90": ci, "pbo": pbo.get("pbo"),
                "candidate": out["candidate"]["trial_id"] if out["candidate"] else None,
                "diagnostic_subject": out.get("diagnostic_subject", {}).get("trial_id")})
    _dump("dev_results.json", out)
    return out


def _cand():
    d = _load("dev_results.json")
    if not d:
        raise ProtocolViolation("run the dev phase first")
    subj = d["candidate"] or d.get("diagnostic_subject")
    if not subj:
        raise ProtocolViolation("no candidate or diagnostic subject")
    return subj


def phase_mc(workers=4):
    m = load_verified(MANIFEST)
    c = _cand()
    base = {"hypothesis": c["hypothesis"], "params": c["params"]}
    cfgs = [dict(MC["primary"], name="primary")] + [dict(s, name=f"{s['scheme']}_{s['block_bars']}") for s in MC["sensitivity"]]
    specs, tags = [], []
    for cf in cfgs:
        for k in range(cf["paths"]):
            specs.append({**base, "path_seed": 100000 * (cfgs.index(cf) + 1) + k, "scheme": cf["scheme"],
                          "block_bars": cf["block_bars"], "start_day": 0, "end_day": 0, "seed": k})
            tags.append(cf["name"])
    t0 = time.perf_counter()
    res = run_many(specs, workers)
    out = {"wall_seconds": round(time.perf_counter() - t0, 1), "configs": {}}
    for cf in cfgs:
        rs = [r["summary"] for r, t in zip(res, tags) if t == cf["name"]]
        out["configs"][cf["name"]] = {
            "scheme": cf["scheme"], "block_bars": cf["block_bars"], "paths": len(rs),
            "net_return_pct": dist([r["net_return_pct"] for r in rs]), "sharpe_ann": dist([r["sharpe_ann_mtm"] for r in rs]),
            "max_drawdown_pct": dist([r["max_drawdown_pct"] for r in rs]),
            "time_under_water_days": dist([r["longest_under_water_bars"] / BARS_PER_DAY for r in rs]),
            "cvar95_daily_pct": dist([r["cvar95_daily_pct"] for r in rs]),
            "p_net_negative": round(float(np.mean([r["net_return_pct"] < 0 for r in rs])), 4),
            "p_drawdown_gt_3pct": round(float(np.mean([r["max_drawdown_pct"] > 3 for r in rs])), 4),
            "p_daily_loss_limit_breach": round(float(np.mean([r["limit_breach_bars"]["daily_loss"] > 0 for r in rs])), 4),
            "p_no_trades": round(float(np.mean([r["trades"] == 0 for r in rs])), 4),
            "liquidations": int(sum(r["liquidations"] for r in rs))}
    # portfolio-return bootstrap of the candidate's own dev daily MTM returns
    scr = _load("screen_results.json")
    x = np.array([scr["daily"][c["trial_id"]].get(d, 0.0) for d in days_between(*m["dates_days"]["dev"])])
    pb = {}
    for bl in MC["portfolio_blocks_days"]:
        for scheme in ("moving", "stationary"):
            rng = np.random.default_rng(bl * 10 + (scheme == "moving"))
            tot, dd, sh = [], [], []
            for _ in range(MC["portfolio_paths"]):
                if scheme == "stationary":
                    idx = stationary_bootstrap_indices(len(x), bl, rng)
                else:
                    st = rng.integers(0, max(1, len(x) - bl), len(x) // bl + 1)
                    idx = np.concatenate([np.arange(s, s + bl) for s in st])[:len(x)]
                s_ = x[idx]
                eq = np.cumprod(1 + s_)
                tot.append((eq[-1] - 1) * 100)
                dd.append(float(((np.maximum.accumulate(eq) - eq) / np.maximum.accumulate(eq)).max()) * 100)
                sh.append(float(s_.mean() / s_.std(ddof=1) * math.sqrt(365)) if s_.std(ddof=1) > 0 else 0.0)
            pb[f"{scheme}_{bl}d"] = {"net_return_pct": dist(tot), "sharpe_ann": dist(sh), "max_drawdown_pct": dist(dd),
                                     "p_net_negative": round(float(np.mean(np.array(tot) < 0)), 4),
                                     "p_drawdown_gt_3pct": round(float(np.mean(np.array(dd) > 3)), 4)}
    out["portfolio_bootstrap"] = pb
    TrialLog(LOG).append({"kind": "monte_carlo", "paths": len(res), "primary_p_net_negative":
                          out["configs"]["primary"]["p_net_negative"]})
    _dump("mc_results.json", out)
    return out


def phase_robust(workers=4):
    m = load_verified(MANIFEST)
    c = _cand()
    a, b = m["dates_days"]["dev"]
    base = {"hypothesis": c["hypothesis"], "params": c["params"], "start_day": a, "end_day": b}
    rng = np.random.default_rng(EXEC_MC["seed"])
    exec_draws = [correlated_exec(rng, EXEC_MC["rho"]) for _ in range(EXEC_MC["draws"])]
    specs = [dict(base, name="base", keep_trades=True)]
    specs += [dict(base, name=f"exec{k}", exec={k2: v for k2, v in e.items() if not k2.startswith("_")}, seed=k)
              for k, e in enumerate(exec_draws)]
    mults = [0.5, 1, 1.5, 2, 3, 4, 6, 8]
    specs += [dict(base, name=f"costx{k}", exec={"fee_rate": 0.0005 * k, "spread_mult": k, "slip_bps": 2.0 * k,
                                                 "stop_gap_bps": 10.0 * k}) for k in mults]
    specs += [dict(base, name=f"lat{k}", exec={"latency_bars": k}) for k in range(0, 9)]
    grid = S.grid(c["hypothesis"])
    neigh = [p for p in grid if sum(p[k] != c["params"][k] for k in p) == 1]
    specs += [dict(base, name=f"neighbor{i}", params=p) for i, p in enumerate(neigh)]
    specs += [dict(base, name=f"offset{o}", offset=o) for o in (5, 10)]
    specs += [dict(base, name=f"ctrl_rand{k}", control="block_randomized", control_seed=k) for k in range(200)]
    specs += [dict(base, name=f"ctrl_shift{k}", control="time_shifted", control_seed=10_000 + k) for k in range(200)]
    specs += [dict(base, name=f"onchain_delay{d}", onchain_delay=d) for d in (3600, 6 * 3600)]
    t0 = time.perf_counter()
    res = run_many(specs, workers)
    by = {s["name"]: r for s, r in zip(specs, res)}
    sh = lambda r: r["summary"]["sharpe_ann_mtm"]  # noqa: E731
    base_sh = sh(by["base"]) or 0.0
    ex = [by[f"exec{k}"]["summary"] for k in range(len(exec_draws))]
    stress = np.array([e["_stress"] for e in exec_draws])
    nets = np.array([e["net_return_pct"] for e in ex])
    cost_curve = [(k, by[f"costx{k}"]["summary"]["net_return_pct"]) for k in mults]
    lat_curve = [(k, by[f"lat{k}"]["summary"]["net_return_pct"]) for k in range(9)]
    out = {"wall_seconds": round(time.perf_counter() - t0, 1), "base_dev": by["base"]["summary"],
           "execution_mc": {"draws": len(ex), "rho": EXEC_MC["rho"], "net_return_pct": dist(nets),
                            "sharpe_ann": dist([e["sharpe_ann_mtm"] for e in ex]),
                            "p_net_negative": round(float((nets < 0).mean()), 4),
                            "p_net_negative_top_decile_stress": round(float((nets[stress >= np.quantile(stress, .9)] < 0).mean()), 4),
                            "corr_stress_vs_net": round(float(np.corrcoef(stress, nets)[0, 1]), 4) if nets.std() > 0 else None},
           "cost_multiplier_curve": cost_curve, "break_even_cost_multiplier": _break_even(cost_curve),
           "latency_curve_bars": lat_curve, "break_even_latency_bars": _break_even(lat_curve),
           "neighbors": [{"params": p, **{k: by[f"neighbor{i}"]["summary"][k] for k in ("sharpe_ann_mtm", "net_return_pct", "trades")}}
                         for i, p in enumerate(neigh)],
           "decision_time_offsets": {o: by[f"offset{o}"]["summary"] for o in (5, 10)},
           "negative_controls": {k: {"sharpe_ann": dist([sh(by[f"ctrl_{k}{j}"]) for j in range(200)]),
                                     "p95": _q([sh(by[f"ctrl_{k}{j}"]) for j in range(200)], .95),
                                     "candidate_percentile": _pct([sh(by[f"ctrl_{k}{j}"]) for j in range(200)], base_sh)}
                                 for k in ("rand", "shift")},
           "onchain_delay": {d: by[f"onchain_delay{d}"]["summary"] for d in (3600, 6 * 3600)}}
    out["regimes"] = _regimes(by["base"], m)
    out["leakage"] = _leakage_e2e(c, m)
    out["data_quality"] = _data_quality()
    TrialLog(LOG).append({"kind": "robustness", "runs": len(res), "break_even_cost_multiplier": out["break_even_cost_multiplier"],
                          "break_even_latency_bars": out["break_even_latency_bars"], "exec_p_net_negative":
                          out["execution_mc"]["p_net_negative"]})
    _dump("robust_results.json", out)
    return out


def _q(x, p):
    x = [v for v in x if v is not None]
    return round(float(np.quantile(x, p)), 4) if x else None


def _pct(x, v):
    x = [u for u in x if u is not None]
    return round(float(np.mean(np.array(x) < v)), 4) if x else None


def _break_even(curve):
    """Smallest x where net return <= 0 (linear interpolation); None if never."""
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if y0 > 0 >= y1:
            return round(x0 + (x1 - x0) * y0 / (y0 - y1), 3)
        if y0 <= 0 and x0 == curve[0][0]:
            return x0
    return None if curve[-1][1] > 0 else curve[-1][0]


def _regimes(base, m):
    from .regimes import label_days
    from datetime import date
    dates = [date.fromisoformat(d) for d in days_between(*m["dates_days"]["dev"])]
    labels = label_days(fixture(), dates, 0.0003)
    rep = {}
    for r in sorted({x for v in labels.values() for x in v}):
        days = {str(d) for d, ls in labels.items() if r in ls}
        tr = [t for t in base["trades"] if t["opened"] in days]
        n_eff, last = 0, -1
        for t in sorted(tr, key=lambda t: t["open_i"]):
            if t["open_i"] >= last:
                n_eff, last = n_eff + 1, t["close_i"]
        pn = [t["net"] for t in tr]
        ci = bootstrap_ci(pn, mean_block=1.5) if n_eff >= 30 else None
        rep[r] = {"days": len(days), "trades": len(tr), "effective_trades": n_eff,
                  "expectancy": round(float(np.mean(pn)), 4) if pn else None, "ci90": ci,
                  "verdict": "INCONCLUSIVE (underpowered)" if n_eff < 30 else ("FAIL" if ci and ci[1] < 0 else "PASS")}
    return rep


def _leakage_e2e(c, m):
    """End-to-end lookahead test: corrupt everything after day X; returns before X must not change."""
    base = ctxs()
    x_bar = bar_index(m["dates_days"]["dev"][0] + 20)
    bad = {s: {k: (np.concatenate([v[:x_bar], v[x_bar:] * 1.5]) if k in ("open", "high", "low", "close") else v)
               for k, v in base[s].items()} for s in base}
    a, b = bar_index(m["dates_days"]["dev"][0]), bar_index(m["dates_days"]["dev"][1])
    r1 = vbt.run(base, {s: S.signals(c["hypothesis"], base[s], c["params"]) for s in base}, a, b)
    r2 = vbt.run(bad, {s: S.signals(c["hypothesis"], bad[s], c["params"]) for s in bad}, a, b)
    k = x_bar - a - 1
    same = bool(np.allclose(r1["equity"][:k], r2["equity"][:k]))
    return {"equity_before_corruption_unchanged": same, "bars_compared": int(k),
            "survivorship": "universe fixed to BTC/ETH survivors: a real study must use a point-in-time listing universe"}


def _data_quality():
    from ..market import BarSeries
    counts = {}
    for sym, s in fixture().items():
        bs = BarSeries(sym)
        for b in s.bars:
            st = bs.ingest(b)
            counts[st] = counts.get(st, 0) + 1
    return {"ingest_dispositions": counts, "note": "synthetic data is clean by construction; real feeds will not be"}


# ------------------------------------------------------------------ event-driven runtime: consistency, ablations, stress
def _rt_job(job: dict) -> dict:
    from ..config import load_config
    from ..faults import make_runtime
    from .invariants import InvariantMonitor
    from .stress import inject
    c = job["candidate"]
    cfg = load_config(overrides={"research": {"hypotheses": {c["hypothesis"]: c["params"]}}}) if c else load_config()
    series = fixture()
    if job.get("stress"):
        series = inject(series, job["stress"], job["t0"])
    kw = dict(use_phi=job.get("use_phi", False), phi_features=job.get("phi_features", ()), use_onchain=job.get("use_onchain", True))
    rt = make_runtime(cfg, series=series, **kw)
    if job.get("stress", {}).get("outage_minutes"):
        from ..util import parse_ts
        t0 = parse_ts(job["t0"])
        rt.venue.apply_scenario({"name": "outage", "outages": "explicit"})
        rt.venue._outages = [(t0, t0 + timedelta(minutes=job["stress"]["outage_minutes"]))]
    if job.get("stress", {}).get("onchain_extra_confirmations") and hasattr(rt, "chain"):
        rt.chain.confirmations += job["stress"]["onchain_extra_confirmations"]
    mon = InvariantMonitor(rt)
    s0 = START + timedelta(days=job["start_day"])
    rt.run(s0, START + timedelta(days=job["end_day"]), on_minute=mon)
    eqs = {}
    for ts, eq in rt.equity_curve:
        eqs[str(ts.date())] = float(eq)
    ds = sorted(eqs)
    vals = [10000.0] + [eqs[d] for d in ds]
    daily = dict(zip(ds, (np.diff(vals) / np.array(vals[:-1])).tolist()))
    outcomes = [float(e["payload"]["net_pnl_quote"]) for e in rt.ledger.events("outcome")]
    fills = rt.ledger.fills()
    first_entry = next((f["ts"] for f in fills if not f["reduce_only"]), None)
    locks = [e["payload"] for e in rt.ledger.events("risk_event")]
    prot = [e["payload"] for e in rt.ledger.events("protective_action")]
    decisions = [e["payload"] for e in rt.ledger.events("decision_record")]
    plans = {e["payload"]["candidate_id"]: e["payload"]["metrics"]["utility_lcb_quote"] for e in rt.ledger.events("candidate_plan")}
    agree = sum(d["selected_id"] == max(d["candidate_ids"], key=lambda x: (float(plans[x]), x)) for d in decisions
                if d["selector"] == "phi_decision_maker" and d["candidate_ids"])
    n_dec = sum(1 for d in decisions if d["selector"] == "phi_decision_maker" and d["candidate_ids"])
    return {"name": job["name"], "stress_spec": job.get("stress"), "invariants": mon.finalize(), "net_pct": round((vals[-1] / 10000 - 1) * 100, 4),
            "sharpe_ann": sharpe(daily), "trades": len(outcomes), "worst_trade": min(outcomes) if outcomes else None,
            "first_entry_ts": first_entry, "risk_events": [(x["from_state"], x["to_state"], x["trigger"]) for x in locks][:12],
            "protective_actions": [(p["type"], p["reason"]) for p in prot][:12],
            "final_state": rt.risk.state, "phi_calls": (rt.phi.resource_summary()["calls"] if rt.phi else 0),
            "decision_isolation": {"decisions": n_dec, "agree_with_rank": agree}, "admission_rejections": len(rt.ledger.events("admission_rejected")),
            "budget_per_trade": 10000 * 0.0025}


def phase_runtime(workers=4):
    m = load_verified(MANIFEST)
    c = _cand()
    from .stress import SCENARIOS
    a, b = m["dates_days"]["dev"]
    ab = {"P0_deterministic": dict(use_phi=False), "P1_phi_screening": dict(use_phi=True, phi_features=("evidence",)),
          "P2_screening_analysis": dict(use_phi=True, phi_features=("evidence", "analysis")),
          "P3_all_four_roles": dict(use_phi=True, phi_features=("evidence", "analysis", "decision", "risk")),
          "P4_all_four_no_onchain": dict(use_phi=True, phi_features=("evidence", "analysis", "decision", "risk"), use_onchain=False)}
    jobs = [dict(name=k, candidate=c, start_day=a, end_day=b, **v) for k, v in ab.items()]
    t0 = time.perf_counter()
    res = run_rt(jobs, workers)
    by = {r["name"]: r for r in res}
    # stress scenarios injected shortly after the first dev entry of P0 (so a position is exposed)
    fe = by["P0_deterministic"]["first_entry_ts"]
    stress_strategy = {"hypothesis": c["hypothesis"], "params": c["params"]}
    stress_c = c
    if not fe:  # safety tests need an exposed position; use the built-in reference strategy and say so
        ref = run_rt([dict(name="stress_reference_P0", candidate=None, start_day=a, end_day=b)], 1)[0]
        fe, stress_c = ref["first_entry_ts"], None
        stress_strategy = {"hypothesis": "built-in trend_breakout_v1 (reference)", "reason": "subject had no event-driven entry"}
    stress = []
    if fe:
        from ..util import parse_ts
        t_entry = parse_ts(fe)
        sd = (t_entry - START).days
        jobs2 = [dict(name=f"stress:{k}", candidate=stress_c, start_day=max(sd - 1, a - 5), end_day=sd + 3, stress=v,
                      t0=(t_entry + timedelta(minutes=10)).isoformat()) for k, v in SCENARIOS.items()]
        stress = run_rt(jobs2, workers)
    base = by["P0_deterministic"]
    scr_days = days_between(a, b)
    out = {"wall_seconds": round(time.perf_counter() - t0, 1), "ablations": by,
           "paired_vs_P0": {k: _paired(by[k], base, scr_days) for k in ab if k != "P0_deterministic"},
           "stress_strategy": stress_strategy, "stress": {r["name"]: r for r in stress}, "stress_checks": {r["name"]: _stress_verdict(r) for r in stress}}
    TrialLog(LOG).append({"kind": "event_driven", "p0_dev_net_pct": base["net_pct"], "p0_invariants": base["invariants"]["passed"],
                          "stress_all_pass": all(v["verdict"] == "PASS" for v in out["stress_checks"].values()) if stress else None})
    _dump("runtime_results.json", out)
    return out


def run_rt(jobs, workers):
    with ProcessPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(_rt_job, jobs))


def _paired(a, b, days):
    return {"net_diff_pct": round(a["net_pct"] - b["net_pct"], 4), "reading": "identical" if a["net_pct"] == b["net_pct"]
            else "differs", "decision_isolation": a["decision_isolation"]}


def _stress_verdict(r):
    reasons = []
    if not r["invariants"]["passed"]:
        reasons.append(f"invariants: {[v['invariant'] for v in r['invariants']['violations']][:3]}")
    if r["invariants"]["liquidations"]:
        reasons.append("liquidation occurred")
    spec = r.get("stress_spec") or {}
    move = abs(spec.get("move", 0.0))
    bound = 10000 * 0.02 * max(1.0, move / 0.10) if move else 2 * r["budget_per_trade"]
    if r["worst_trade"] is not None and -r["worst_trade"] > bound:
        reasons.append(f"worst trade loss {round(-r['worst_trade'], 2)} > bound {round(bound, 2)} USDT")
    return {"verdict": "FAIL" if reasons else "PASS", "reasons": reasons, "worst_trade": r["worst_trade"], "loss_bound": bound,
            "final_state": r["final_state"], "risk_events": r["risk_events"][:5], "protective": r["protective_actions"][:5]}


# ------------------------------------------------------------------ gates, holdout, report
def phase_gates() -> dict:
    m = load_verified(MANIFEST)
    dev, mc, rob, rtm = _load("dev_results.json"), _load("mc_results.json"), _load("robust_results.json"), _load("runtime_results.json")
    g = {}
    c = dev["candidate"]
    subj = dev["candidate"] or dev.get("diagnostic_subject")
    syn = lambda v: f"{v} (SYNTHETIC ONLY)"  # noqa: E731
    ci = dev["oos_ci90"]
    g["G1_walkforward_oos"] = (syn("PASS" if ci and ci[0] > 0 else "FAIL"), f"OOS daily mean {dev['oos_daily_mean']}, CI {ci}")
    if c is None:
        g["G0_train_eligibility"] = (syn("FAIL"), f"no trial met train eligibility ({m['train_eligibility']}); later "
                                     f"diagnostics use {subj['trial_id']} as a labeled diagnostic subject, not a candidate")
    g["G2_effective_sample"] = (syn("PASS" if c and c["dev_effective_trades"] >= 30 else "FAIL"),
                                f"{c['dev_effective_trades'] if c else 'no candidate'} effective dev trades")
    dsr = c["dsr"].get("dsr") if c else None
    g["G3_deflated_sharpe"] = (syn(("FAIL" if c is None else "INCONCLUSIVE") if dsr is None else ("PASS" if dsr >= 0.95 else "FAIL")),
                               f"DSR {dsr} with N={c['n_trials_total'] if c else '-'} trials")
    p = dev["pbo"].get("pbo")
    g["G4_pbo"] = (syn("INCONCLUSIVE" if p is None else ("PASS" if p <= 0.2 else "FAIL")), f"PBO {p}")
    if rob:
        bs = rob["base_dev"]["sharpe_ann_mtm"] or 0
        stable = all((n["sharpe_ann_mtm"] or -1) > 0 and (n["sharpe_ann_mtm"] or 0) >= 0.5 * bs for n in rob["neighbors"])
        g["G5_parameter_stability"] = (syn("PASS" if stable and bs > 0 else "FAIL"),
                                       f"candidate {bs}; neighbours {[n['sharpe_ann_mtm'] for n in rob['neighbors']]}")
        nc = rob["negative_controls"]
        ok = all(bs > (nc[k]["p95"] or 0) for k in nc)
        g["G6_negative_controls"] = (syn("PASS" if ok else "FAIL"), f"candidate {bs} vs p95 randomized {nc['rand']['p95']}, "
                                     f"shifted {nc['shift']['p95']}")
        be, pn = rob["break_even_cost_multiplier"], rob["execution_mc"]["p_net_negative"]
        g["G7_cost_robustness"] = (syn("PASS" if (be is None or be >= 2.0) and pn <= 0.2 and rob["base_dev"]["net_return_pct"] > 0
                                       else "FAIL"), f"break-even cost x{be}, correlated exec MC P(net<0) {pn}")
        bl = rob["break_even_latency_bars"]
        g["G8_latency"] = (syn("PASS" if (bl is None and rob["base_dev"]["net_return_pct"] > 0) or (bl is not None and bl >= 2)
                               else "FAIL"), f"break-even latency {bl} bars")
        powered = [r for r in rob["regimes"].values() if r["verdict"] != "INCONCLUSIVE (underpowered)"]
        g["G10_regimes"] = (syn("INCONCLUSIVE" if not powered else ("FAIL" if any(r["verdict"] == "FAIL" for r in powered) else "PASS")),
                            f"{len(powered)} powered of {len(rob['regimes'])} regimes")
        g["L1_lookahead_e2e"] = ("PASS" if rob["leakage"]["equity_before_corruption_unchanged"] else "FAIL",
                                 f"{rob['leakage']['bars_compared']} bars compared after corrupting future data")
    if mc:
        pr = mc["configs"]["primary"]
        signs = {k: (v["net_return_pct"].get("q50") or 0) > 0 for k, v in mc["configs"].items()}
        ok = pr["net_return_pct"].get("q50", 0) > 0 and pr["p_drawdown_gt_3pct"] <= 0.05 and len(set(signs.values())) == 1
        g["G9_market_path_mc"] = (syn("PASS" if ok else "FAIL"), f"primary median net {pr['net_return_pct'].get('q50')}%, "
                                  f"P(DD>3%) {pr['p_drawdown_gt_3pct']}, median sign by config {signs}")
    if rtm:
        p0 = rtm["ablations"]["P0_deterministic"]
        g["G11_event_driven_consistency"] = (syn("PASS" if p0["invariants"]["passed"] and p0["net_pct"] > 0 else "FAIL"),
                                             f"event-driven dev net {p0['net_pct']}%, trades {p0['trades']}, invariants "
                                             f"{p0['invariants']['passed']}")
        sc = rtm["stress_checks"]
        g["S1_stress_safety"] = ("PASS" if sc and all(v["verdict"] == "PASS" for v in sc.values()) else
                                 ("UNTESTED" if not sc else "FAIL"),
                                 f"{sum(v['verdict'] == 'PASS' for v in sc.values())}/{len(sc)} scenarios; failures: "
                                 f"{ {k: v['reasons'] for k, v in sc.items() if v['verdict'] != 'PASS'} }")
    econ = [k for k in g if k.startswith("G")]
    survived = c is not None and all(g[k][0].startswith("PASS") for k in econ) and len(econ) >= 11
    g["DEV_SURVIVOR"] = ("YES (SYNTHETIC ONLY)" if survived else "NONE", "all development gates must pass")
    g["E_phi_value"] = ("UNTESTED", "fake Phi = deterministic ranker (identical ablations); real Phi not available")
    g["R_real_phi_benchmark"] = ("UNTESTED", "no GPU/weights in this container; `python -m cqc bench-phi` on a GPU host")
    g["R_fine_tuning"] = ("UNTESTED", "train-only dataset export provided; no GPU to fine-tune")
    g["R_24h_soak_real_services"] = ("UNTESTED", "requires the real Phi server")
    g["D_real_point_in_time_data"] = ("UNTESTED", "no real exchange/on-chain captures reachable")
    g["ALPHA_VERDICT"] = ("UNTESTED", "synthetic data only; " + ("a synthetic survivor exists" if survived else "NO VERIFIED EDGE"))
    TrialLog(LOG).append({"kind": "gates", "survivor": survived, "verdicts": {k: v[0] for k, v in g.items()}})
    _dump("gates.json", g)
    return g


def phase_holdout(workers=4):
    m = load_verified(MANIFEST)
    g = _load("gates.json")
    if not g or not g["DEV_SURVIVOR"][0].startswith("YES") or not _load("dev_results.json")["candidate"]:
        TrialLog(LOG).append({"kind": "holdout_not_opened", "reason": "no candidate survived development gates; the "
                              "research_v2 holdout stays sealed for a future, pre-registered candidate"})
        return {"opened": False}
    c = _cand()
    log = TrialLog(LOG)
    log.append({"kind": "candidate_frozen", "candidate": c, "candidate_sha256": sha256_hex(canonical_json(c))})
    HoldoutSeal(log, dict(m, holdout={"max_evaluations": 1})).open_once(c)
    a, b = m["dates_days"]["holdout"]
    r = evaluate({"hypothesis": c["hypothesis"], "params": c["params"], "start_day": a, "end_day": b, "keep_trades": True})
    rt = run_rt([dict(name="holdout_P0", candidate=c, start_day=a, end_day=b)], 1)[0]
    x = list(r["daily"].values())
    out = {"screener": r["summary"], "ci90_daily": bootstrap_ci(x), "event_driven": {k: rt[k] for k in ("net_pct", "trades", "sharpe_ann")},
           "invariants": rt["invariants"]["passed"]}
    log.append({"kind": "holdout_result", **{k: out[k] for k in ("ci90_daily", "invariants")}, "net_pct": r["summary"]["net_return_pct"]})
    _dump("holdout_results.json", out)
    return out


def main(phase: str, workers: int = 4):
    phases = {"propose": phase_propose, "freeze": phase_freeze, "screen": lambda: phase_screen(workers), "dev": phase_dev,
              "mc": lambda: phase_mc(workers), "robust": lambda: phase_robust(workers), "runtime": lambda: phase_runtime(workers),
              "gates": phase_gates, "holdout": lambda: phase_holdout(workers)}
    order = ["propose", "freeze", "screen", "dev", "mc", "robust", "runtime", "gates", "holdout"]
    if phase == "all":
        return {p: phases[p]() for p in order}
    return phases[phase]()

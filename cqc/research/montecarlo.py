"""Monte Carlo layers. None of them proves future profitability: they re-weight or perturb the SAME
observed history, so they stress-test fragility rather than demonstrate edge.

1. `daily_bootstrap`: stationary-bootstrap resampling of realized daily PnL (keeps serial dependence and
   loss clustering) -> distribution of total PnL, drawdown and CVaR.
2. `cost_mc`: per-trade re-pricing under seeded execution/cost uncertainty (fees, extra slippage/spread,
   latency drift, stop gap-through, funding shocks). Thousands of paths are cheap.
3. `bootstrap_market`: joint BTC/ETH day-block bootstrap of 1-minute market paths (preserves intraday
   dependence and cross-asset correlation within a day), optional volatility and decorrelation shocks.
   The strategy is re-run on each path (expensive: tens of paths, not thousands).
"""
from __future__ import annotations

import copy
from datetime import timedelta

import numpy as np

from ..market import Bar, BarSeries
from ..util import D, parse_ts
from .metrics import daily_stats, stationary_bootstrap_indices


def daily_bootstrap(daily_pnl, equity0: float, reps: int = 5000, mean_block: float = 3.0, seed: int = 11) -> dict:
    x = np.asarray(daily_pnl, float)
    if len(x) < 4:
        return {"reps": 0, "reason": "too few days"}
    rng = np.random.default_rng(seed)
    tot, dd, cvar = [], [], []
    for _ in range(reps):
        s = x[stationary_bootstrap_indices(len(x), mean_block, rng)]
        st = daily_stats(s, equity0)
        tot.append(st["net_pnl"])
        dd.append(st["max_drawdown"])
        cvar.append(st["cvar95_daily"])
    q = lambda a, p: round(float(np.quantile(a, p)), 4)  # noqa: E731
    return {"reps": reps, "seed": seed, "mean_block_days": mean_block,
            "total_pnl_q05_q50_q95": [q(tot, .05), q(tot, .5), q(tot, .95)], "p_total_negative": round(float(np.mean(np.array(tot) < 0)), 4),
            "max_drawdown_q50_q95_q99": [q(dd, .5), q(dd, .95), q(dd, .99)], "cvar95_daily_q05": q(cvar, .05)}


def extract_trades(ledger, instruments) -> list[dict]:
    """Flat-to-flat trades with entry notional, fees, funding and exit type, from the durable ledger."""
    outcomes = [e["payload"] for e in ledger.events("outcome")]
    fills = ledger.fills()
    funding = [e["payload"] for e in ledger.events("funding_settlement")]
    intents = {i["intent_id"]: i for i in ledger.intents()}
    out = []
    for o in outcomes:
        t0, t1 = parse_ts(o["opened"]) - timedelta(minutes=2), parse_ts(o["closed"])
        fs = [f for f in fills if f["symbol"] == o["symbol"] and t0 <= parse_ts(f["ts"]) <= t1]
        entry = [f for f in fs if not f["reduce_only"]]
        exits = [f for f in fs if f["reduce_only"]]
        mult = instruments[o["symbol"]].multiplier
        notional = float(sum(D(f["qty"]) * mult * D(f["price"]) for f in entry))
        exit_types = {intents[f["intent_id"]]["order_type"] for f in exits if f["intent_id"] in intents}
        fund = sum(float(D(p["payment"])) for p in funding if p["symbol"] == o["symbol"]
                   and t0 <= parse_ts(p["ts"]) <= t1)
        out.append({**o, "entry_notional": notional, "fees": float(sum(D(f["fee"]) for f in fs)), "funding_paid": fund,
                    "exit_type": "stop" if "stop_market" in exit_types else ("market" if "market" in exit_types else "other"),
                    "side": entry[0]["side"] if entry else None})
    return out


COST_PRIORS = {  # declared, not fitted
    "fee_multiplier": ("uniform", 1.0, 2.0),
    "extra_slippage_bps_per_leg": ("lognormal_median", 2.0, 0.75),
    "latency_drift_bps_per_leg": ("uniform", 0.0, 5.0),
    "stop_gap_extra_bps": ("exponential_mean", 15.0),
    "funding_paid_multiplier": ("uniform", 0.5, 3.0),
    "funding_received_multiplier": ("uniform", 0.0, 1.0),
}


def cost_mc(trades: list[dict], reps: int = 5000, seed: int = 7) -> dict:
    if not trades:
        return {"reps": 0, "reason": "no trades"}
    rng = np.random.default_rng(seed)
    base = np.array([float(t["net_pnl_quote"]) for t in trades])
    notional = np.array([t["entry_notional"] for t in trades])
    fees = np.array([t["fees"] for t in trades])
    fund = np.array([t["funding_paid"] for t in trades])
    stop = np.array([t["exit_type"] == "stop" for t in trades])
    n = len(trades)
    totals, expectancy = [], []
    for _ in range(reps):
        fee_mult = rng.uniform(1.0, 2.0)
        slip = np.exp(np.log(2.0) + 0.75 * rng.standard_normal(n)) * 2 / 1e4
        lat = rng.uniform(0, 5, n) * 2 / 1e4
        gap = rng.exponential(15.0, n) / 1e4 * stop
        f_adj = np.where(fund > 0, fund * (rng.uniform(0.5, 3.0) - 1), fund * (rng.uniform(0.0, 1.0) - 1) * -1)
        pnl = base - (fee_mult - 1) * fees - notional * (slip + lat + gap) - f_adj
        totals.append(pnl.sum())
        expectancy.append(pnl.mean())
    q = lambda a, p: round(float(np.quantile(a, p)), 4)  # noqa: E731
    return {"reps": reps, "seed": seed, "trades": n, "priors": COST_PRIORS, "base_total": round(float(base.sum()), 4),
            "total_q05_q50_q95": [q(totals, .05), q(totals, .5), q(totals, .95)],
            "expectancy_q05_q50_q95": [q(expectancy, .05), q(expectancy, .5), q(expectancy, .95)],
            "p_total_negative": round(float(np.mean(np.array(totals) < 0)), 4)}


def bootstrap_market(series: dict, pool_start, pool_end, n_days: int, seed: int, vol_shock_prob: float = 0.0,
                     decorrelate_prob: float = 0.0) -> dict:
    """New joint BTC/ETH 1-minute path from whole-day blocks drawn (with replacement) from [pool_start, pool_end).
    Prices are re-chained multiplicatively so each path is continuous; spreads, volume and funding are kept."""
    rng = np.random.default_rng(seed)
    syms = sorted(series)
    days = sorted({b.start.date() for b in series[syms[0]].bars if pool_start <= b.start < pool_end})
    by = {s: {} for s in syms}
    for s in syms:
        for b in series[s].bars:
            by[s].setdefault(b.start.date(), []).append(b)
    days = [d for d in days if all(len(by[s].get(d, [])) == 1440 for s in syms)]
    t0 = series[syms[0]].bars[0].start
    out = {s: BarSeries(s) for s in syms}
    level = {s: series[s].bars[0].open for s in syms}
    for k in range(n_days):
        pick = days[int(rng.integers(len(days)))]
        shock = 2.0 if rng.random() < vol_shock_prob else 1.0
        eth_day = days[int(rng.integers(len(days)))] if rng.random() < decorrelate_prob else pick
        for s in syms:
            src = by[s][eth_day if s == "ETHUSDT" else pick]
            base = src[0].open
            for j, b in enumerate(src):
                f = lambda p: level[s] * (p / base) ** shock  # noqa: E731
                start = t0 + timedelta(days=k, minutes=j)
                nb = Bar(s, start, start + timedelta(minutes=1), f(b.open), f(b.high), f(b.low), f(b.close), b.volume_base,
                         f(b.close) - (b.close - b.bid), f(b.close) + (b.ask - b.close), f(b.close) * (b.mark / b.close),
                         f(b.close), b.funding_rate_est)
                if nb.high < nb.low:
                    nb.high, nb.low = nb.low, nb.high
                out[s].append(nb)
            level[s] = level[s] * (src[-1].close / base) ** shock
    return out

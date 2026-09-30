"""Day-level regime labels (computed only from data available by each day's end) and per-slice reports."""
from __future__ import annotations

import numpy as np

from ..util import parse_ts
from .metrics import bootstrap_ci, effective_trades, trade_stats


def label_days(series: dict, days: list, funding_extreme: float, outage_days=()) -> dict:
    """Returns {date: set(labels)}; uses BTC for direction/volatility and BTC+ETH for joint shocks."""
    btc, eth = series["BTCUSDT"].bars, series["ETHUSDT"].bars
    by_day = {}
    for i, b in enumerate(btc):
        by_day.setdefault(b.start.date(), []).append(i)
    vols = {}
    for d, idx in by_day.items():
        c = np.array([btc[i].close for i in idx])
        vols[d] = float(np.std(np.diff(np.log(c)))) * np.sqrt(1440) if len(c) > 2 else 0.0
    med = float(np.median([vols[d] for d in days if d in vols])) if days else 0.0
    out = {}
    for d in days:
        idx = by_day.get(d)
        if not idx:
            continue
        c = np.array([btc[i].close for i in idx])
        e = np.array([eth[i].close for i in idx])
        ret = float(np.log(c[-1] / c[0]))
        labels = {"trend" if abs(ret) > vols[d] else "chop", "high_vol" if vols[d] > med else "low_vol",
                  "bull" if ret > 0 else "bear"}
        if max(abs(btc[i].funding_rate_est) for i in idx) > funding_extreme:
            labels.add("funding_extreme")
        if d.weekday() >= 5:
            labels.add("weekend_illiquid")
        rb, re_ = np.diff(np.log(c[::60])), np.diff(np.log(e[::60]))
        if len(rb) > 3:
            sb, se = rb.std() or 1, re_.std() or 1
            if np.any((np.abs(rb) > 2.5 * sb) & (np.abs(re_) > 2.5 * se) & (np.sign(rb) == np.sign(re_))):
                labels.add("correlated_shock")
        if d in outage_days:
            labels.add("exchange_disruption")
        out[d] = labels
    return out


def slice_report(trades: list[dict], daily_pnl: dict, day_labels: dict, horizon_s: float, min_eff: int) -> dict:
    regimes = sorted({lbl for ls in day_labels.values() for lbl in ls})
    rep = {}
    for r in regimes:
        days = [d for d, ls in day_labels.items() if r in ls]
        tr = [t for t in trades if parse_ts(t["opened"]).date() in days]
        pnls = [float(t["net_pnl_quote"]) for t in tr]
        n_eff = effective_trades(tr, horizon_s)
        dpnl = [daily_pnl.get(str(d), 0.0) for d in days]
        rep[r] = {"days": len(days), "trades": len(tr), "effective_trades": n_eff, **trade_stats(pnls),
                  "daily_pnl_sum": round(float(sum(dpnl)), 4),
                  "expectancy_90ci": bootstrap_ci(pnls, mean_block=1.5) if n_eff >= min_eff else None,
                  "verdict": "INCONCLUSIVE (underpowered)" if n_eff < min_eff else
                  ("NEGATIVE" if sum(pnls) < 0 else "NON_NEGATIVE")}
    return rep

"""Outcome metrics and multiple-testing diagnostics.

References: Bailey & López de Prado (2014) "The Deflated Sharpe Ratio"; Bailey, Borwein, López de Prado &
Zhu (2016) "The Probability of Backtest Overfitting" (CSCV). Assumptions are recorded in each output;
with small samples (few trades, few days) these statistics are weak and reported as such.
"""
from __future__ import annotations

import math
from itertools import combinations
from statistics import NormalDist

import numpy as np

N = NormalDist()


def daily_stats(daily_pnl, equity0: float) -> dict:
    x = np.asarray(daily_pnl, float)
    eq = equity0 + np.concatenate([[0.0], np.cumsum(x)])
    peak = np.maximum.accumulate(eq)
    dd = peak - eq
    under, longest = 0, 0
    for v in dd[1:]:
        under = under + 1 if v > 1e-9 else 0
        longest = max(longest, under)
    k = max(1, math.ceil(0.05 * len(x))) if len(x) else 0
    worst = np.sort(x)[:k] if len(x) else np.array([])
    sd = float(x.std(ddof=1)) if len(x) > 1 else float("nan")
    return {"days": int(len(x)), "net_pnl": round(float(x.sum()), 4), "net_return_pct": round(float(x.sum()) / equity0 * 100, 4),
            "mean_daily": round(float(x.mean()), 4) if len(x) else None, "sd_daily": round(sd, 4) if len(x) > 1 else None,
            "sharpe_daily": round(float(x.mean()) / sd, 4) if len(x) > 1 and sd > 0 else None,
            "max_drawdown": round(float(dd.max()), 4), "max_drawdown_pct": round(float((dd / peak).max()) * 100, 4),
            "longest_under_water_days": int(longest),
            "cvar95_daily": round(float(worst.mean()), 4) if len(worst) else None,
            "worst_day": round(float(x.min()), 4) if len(x) else None}


def trade_stats(pnls) -> dict:
    x = np.asarray(pnls, float)
    if not len(x):
        return {"trades": 0, "expectancy": None, "win_fraction": None, "profit_factor": None, "worst_trade": None}
    wins, losses = x[x > 0], x[x <= 0]
    return {"trades": int(len(x)), "expectancy": round(float(x.mean()), 4), "median_trade": round(float(np.median(x)), 4),
            "win_fraction": round(float(len(wins) / len(x)), 4),
            "profit_factor": round(float(wins.sum() / -losses.sum()), 4) if losses.sum() < 0 else None,
            "worst_trade": round(float(x.min()), 4), "best_trade": round(float(x.max()), 4)}


def effective_trades(trades: list[dict], horizon_s: float) -> int:
    """Greedy count of trades whose windows do not overlap (overlapping trades are not independent)."""
    from ..util import parse_ts
    n, last_end = 0, None
    for t in sorted(trades, key=lambda t: t["opened"]):
        o = parse_ts(t["opened"])
        if last_end is None or o >= last_end:
            n += 1
            last_end = max(parse_ts(t["closed"]), o)
    return n


def stationary_bootstrap_indices(n: int, mean_block: float, rng) -> np.ndarray:
    """Politis & Romano (1994) stationary bootstrap: geometric block lengths, wrap-around."""
    idx = np.empty(n, dtype=int)
    p = 1.0 / mean_block
    i = rng.integers(n)
    for k in range(n):
        if k > 0 and rng.random() < p:
            i = rng.integers(n)
        idx[k] = i
        i = (i + 1) % n
    return idx


def bootstrap_ci(x, stat=np.mean, reps: int = 5000, mean_block: float = 3.0, alpha: float = 0.10, seed: int = 0):
    x = np.asarray(x, float)
    if len(x) < 4:
        return None
    rng = np.random.default_rng(seed)
    vals = [stat(x[stationary_bootstrap_indices(len(x), mean_block, rng)]) for _ in range(reps)]
    return [round(float(np.quantile(vals, alpha / 2)), 4), round(float(np.quantile(vals, 1 - alpha / 2)), 4)]


def _moments(x):
    x = np.asarray(x, float)
    m, s = x.mean(), x.std(ddof=0)
    if s == 0:
        return 0.0, 3.0
    z = (x - m) / s
    return float((z ** 3).mean()), float((z ** 4).mean())


def deflated_sharpe(returns, trial_sharpes) -> dict:
    """DSR = Φ((SR − SR0)·sqrt(T−1) / sqrt(1 − γ3·SR + (γ4−1)/4·SR²)), SR0 = expected max Sharpe of N
    unskilled trials = sqrt(V[SR_n])·((1−γ)Φ⁻¹(1−1/N) + γΦ⁻¹(1−1/(N·e))). Per-period (daily) Sharpe."""
    r = np.asarray(returns, float)
    T = len(r)
    if T < 3 or r.std(ddof=1) == 0:
        return {"dsr": None, "reason": "insufficient observations or zero variance", "T": T}
    sr = float(r.mean() / r.std(ddof=1))
    skew, kurt = _moments(r)
    trials = [s for s in trial_sharpes if s is not None and math.isfinite(s)]
    n = max(len(trials), 1)
    if n > 1:
        g = 0.5772156649
        sr0 = math.sqrt(float(np.var(trials, ddof=1))) * ((1 - g) * N.inv_cdf(1 - 1 / n) + g * N.inv_cdf(1 - 1 / (n * math.e)))
    else:
        sr0 = 0.0
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    dsr = N.cdf((sr - sr0) * math.sqrt(T - 1) / math.sqrt(denom)) if denom > 0 else None
    return {"dsr": round(dsr, 4) if dsr is not None else None, "sharpe_daily": round(sr, 4), "sr0_daily": round(sr0, 4),
            "trials": n, "T_days": T, "skew": round(skew, 3), "kurtosis": round(kurt, 3),
            "assumptions": "daily Sharpe; trials = all logged parameter sets; iid-ish daily returns (weak with few days)"}


def pbo_cscv(perf: np.ndarray, n_blocks: int = 8, metric=np.mean) -> dict:
    """Combinatorially symmetric cross-validation. perf: T×N matrix (time × trial) of daily PnL."""
    perf = np.asarray(perf, float)
    T, n_trials = perf.shape
    if n_trials < 2 or T < n_blocks * 2:
        return {"pbo": None, "reason": f"need ≥2 trials and ≥{n_blocks * 2} observations (have {n_trials}, {T})"}
    blocks = np.array_split(np.arange(T), n_blocks)
    logits, degraded = [], 0
    for is_blocks in combinations(range(n_blocks), n_blocks // 2):
        is_idx = np.concatenate([blocks[b] for b in is_blocks])
        oos_idx = np.concatenate([blocks[b] for b in range(n_blocks) if b not in is_blocks])
        is_score = metric(perf[is_idx], axis=0)
        oos_score = metric(perf[oos_idx], axis=0)
        best = int(np.argmax(is_score))
        rank = (oos_score < oos_score[best]).sum() + 0.5 * ((oos_score == oos_score[best]).sum() - 1) + 1
        w = rank / (n_trials + 1)
        logits.append(math.log(w / (1 - w)))
        degraded += oos_score[best] < np.median(oos_score)
    logits = np.array(logits)
    return {"pbo": round(float((logits <= 0).mean()), 4), "combinations": len(logits), "median_logit": round(float(np.median(logits)), 4),
            "best_is_below_median_oos_fraction": round(degraded / len(logits), 4), "blocks": n_blocks,
            "assumptions": "CSCV with contiguous time blocks; performance = mean daily PnL per block set"}

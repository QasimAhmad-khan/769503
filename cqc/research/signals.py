"""Registered, falsifiable hypothesis families (research_v2).

Every signal is a causal function of point-in-time context arrays on 15-minute bars: the value at
index i uses only data available at the end of bar i (on-chain values only after block time +
confirmations + any configured extra delay). Each entry states the mechanism, the data it needs, a
bounded parameter grid and the falsification criterion. Phi may *propose/critique* from this closed
menu; it cannot add code or parameters outside the grid.
"""
from __future__ import annotations

import math

import numpy as np

from .. import quant


def _sma(x, w):
    return quant._rolling_mean(np.asarray(x, float), w)


def _vol(closes, w):
    r = np.diff(np.log(closes), prepend=np.log(closes[0]))
    return quant._rolling_std(r, w)


def trend_breakout(ctx, p):
    f = quant.features(ctx["close"], ctx["funding"], {"vol_window_bars": 96, "trend_fast_bars": p["fast"],
                                                       "trend_slow_bars": p["slow"], "trend_entry_z": p["z"],
                                                       "funding_extreme_rate": "1"})
    return f["trend_dir"]


def overextension_reversion(ctx, p):
    c = ctx["close"]
    n = p["n"]
    vol = _vol(c, 96)
    z = (c - _sma(c, n)) / (c * vol * math.sqrt(n))
    return np.nan_to_num(np.where(np.abs(z) > p["z"], -np.sign(z), 0.0))


def volatility_breakout(ctx, p):
    h, l, c = ctx["high"], ctx["low"], ctx["close"]
    n = p["n"]
    rng = np.full(len(c), np.nan)
    hi_prev, lo_prev = np.full(len(c), np.nan), np.full(len(c), np.nan)
    for i in range(n, len(c)):
        hi_prev[i], lo_prev[i] = h[i - n:i].max(), l[i - n:i].min()
        rng[i] = (hi_prev[i] - lo_prev[i]) / c[i - 1]
    med = np.full(len(c), np.nan)
    m = p["m"]
    for i in range(n + m, len(c)):
        med[i] = np.nanmedian(rng[i - m:i])
    compressed = rng < p["c"] * med
    out = np.where(compressed & (c > hi_prev), 1.0, np.where(compressed & (c < lo_prev), -1.0, 0.0))
    return np.nan_to_num(out)


def funding_crowding(ctx, p):
    f = ctx["funding"]
    return np.where(np.abs(f) > p["thr"], -np.sign(f), 0.0)


def liquidity_shock_reversal(ctx, p):
    c, s = ctx["close"], ctx["spread_bps"]
    r = np.diff(np.log(c), prepend=np.log(c[0]))
    vol = _vol(c, 96)
    med_s = np.full(len(c), np.nan)
    for i in range(96, len(c)):
        med_s[i] = np.median(s[i - 96:i])
    shock = (s > p["k"] * med_s) & (np.abs(r) > p["m"] * vol)
    return np.nan_to_num(np.where(shock, -np.sign(r), 0.0))


def onchain_activity_trend(ctx, p):
    x, c = ctx["onchain"], ctx["close"]
    w = p["w"]
    out = np.zeros(len(c))
    for i in range(w + 4, len(c)):
        hist = x[i - w:i + 1]
        hist = hist[~np.isnan(hist)]
        if len(hist) < w // 2 or np.isnan(x[i]):
            continue
        z = (x[i] - hist.mean()) / (hist.std() or 1.0)
        if z > p["z"]:
            out[i] = np.sign(math.log(c[i] / c[i - 4]))
    return out


REGISTRY = {
    "trend_breakout_v1": {
        "family": "market_structure", "fn": trend_breakout, "data": ["ohlc", "funding"],
        "mechanism": "volatility-scaled moving-average divergence persists over the holding horizon",
        "grid": {"fast": [8, 16], "slow": [48, 96], "z": [0.5, 1.0]}},
    "overextension_reversion_v1": {
        "family": "market_structure", "fn": overextension_reversion, "data": ["ohlc"],
        "mechanism": "large vol-scaled deviations from a moving average mean-revert",
        "grid": {"n": [32, 96], "z": [1.0, 1.5]}},
    "volatility_breakout_v1": {
        "family": "market_structure", "fn": volatility_breakout, "data": ["ohlc"],
        "mechanism": "breakouts after range compression continue",
        "grid": {"n": [16, 32], "m": [96], "c": [0.6, 0.8]}},
    "funding_crowding_v1": {
        "family": "funding", "fn": funding_crowding, "data": ["funding"],
        "mechanism": "extreme funding indicates crowded positioning that unwinds",
        "grid": {"thr": [0.0002, 0.0003, 0.0004]}},
    "liquidity_shock_reversal_v1": {
        "family": "liquidity", "fn": liquidity_shock_reversal, "data": ["ohlc", "spread"],
        "mechanism": "moves on transiently wide spreads are liquidity-driven and partially revert",
        "grid": {"k": [1.5, 2.0], "m": [2.0, 3.0]}},
    "onchain_activity_trend_v1": {
        "family": "onchain", "fn": onchain_activity_trend, "data": ["onchain_finalized", "ohlc"],
        "mechanism": "unusual finalized transfer activity precedes continuation of the prevailing move",
        "grid": {"w": [24, 96], "z": [1.5, 2.5]}},
}
FALSIFICATION = ("falsified on development data unless the purged walk-forward, mark-to-market, after-cost daily "
                 "return has a 90% CI lower bound > 0 AND every other research_v2 development gate passes")
EXIT_GRID = {"hold_bars": [16], "stop_mult": [2.0]}  # exits fixed a priori (not tuned) to limit the search


def grid(name: str) -> list[dict]:
    g = REGISTRY[name]["grid"]
    keys = sorted(g)
    out = [{}]
    for k in keys:
        out = [dict(o, **{k: v}) for o in out for v in g[k]]
    return out


def signals(name: str, ctx: dict, params: dict) -> np.ndarray:
    return np.asarray(REGISTRY[name]["fn"](ctx, params), float)

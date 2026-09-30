"""Point-in-time leakage checks.

`causality_check` perturbs data strictly after index t and verifies no output at or before t changes.
A deliberately leaky feature (`leaky_features_for_control`) must be flagged — a positive control that
proves the checker can detect lookahead.
"""
from __future__ import annotations

import numpy as np

from .. import quant

PARAMS = {"vol_window_bars": 20, "trend_fast_bars": 5, "trend_slow_bars": 20, "trend_entry_z": 0.5,
          "funding_extreme_rate": "0.0003"}


def leaky_features_for_control(closes, funding, params):
    """POSITIVE CONTROL ONLY: uses the next bar's close (lookahead). Never registered as a tool."""
    out = quant.features(closes, funding, params)
    nxt = np.append(closes[1:], closes[-1])
    out["z"] = out["z"] + (nxt - closes) / closes
    return out


def causality_check(fn, n: int = 400, probes: int = 25, seed: int = 0) -> dict:
    rng = np.random.default_rng(seed)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    funding = rng.normal(0, 0.0003, n)
    base = fn(closes, funding, PARAMS)
    leaks = []
    for t in sorted(rng.choice(np.arange(60, n - 2), probes, replace=False)):
        c2, f2 = closes.copy(), funding.copy()
        c2[t + 1:] *= np.exp(rng.normal(0, 0.05, n - t - 1))
        f2[t + 1:] = rng.normal(0, 0.001, n - t - 1)
        out = fn(c2, f2, PARAMS)
        for key in base:
            a, b = np.nan_to_num(np.asarray(base[key])[: t + 1]), np.nan_to_num(np.asarray(out[key])[: t + 1])
            if not np.allclose(a, b, equal_nan=True):
                leaks.append({"t": int(t), "output": key})
                break
    return {"probes": probes, "leaks": len(leaks), "examples": leaks[:3], "causal": not leaks}

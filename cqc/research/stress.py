"""Stress and adversarial market scenarios injected into a copy of the 1-minute fixture.

Each scenario transforms prices from time t0 onward (level shifts persist), or removes bars (stale
feed), or instructs the runtime to take the venue down / delay on-chain finality. They are applied
~10 minutes after a real entry so a position is exposed when the shock hits.
"""
from __future__ import annotations

import copy
from datetime import timedelta

from ..market import BarSeries
from ..util import parse_ts

SCENARIOS = {
    "crash_-15pct_1h": {"kind": "ramp", "move": -0.15, "minutes": 60, "symbols": "both"},
    "short_squeeze_+20pct_30m": {"kind": "ramp", "move": 0.20, "minutes": 30, "symbols": "both"},
    "vol_regime_x3_24h": {"kind": "vol", "mult": 3.0, "minutes": 1440},
    "correlated_drop_-8pct_15m": {"kind": "ramp", "move": -0.08, "minutes": 15, "symbols": "both"},
    "liquidation_cascade_gap_-25pct": {"kind": "gap", "move": -0.25, "spread_mult": 20, "volume_mult": 0.05, "minutes": 30},
    "squeeze_gap_+25pct": {"kind": "gap", "move": 0.25, "spread_mult": 20, "volume_mult": 0.05, "minutes": 30},
    "repeated_losses_whipsaw_2d": {"kind": "whipsaw", "step": 0.03, "every_minutes": 120, "minutes": 2880},
    "exchange_outage_45m": {"kind": "none", "outage_minutes": 45},
    "stale_feed_20m": {"kind": "drop", "minutes": 20},
    "delayed_onchain_+5h": {"kind": "none", "onchain_extra_confirmations": 30},
}


def _scale(bar, f):
    b = copy.copy(bar)
    for k in ("open", "high", "low", "close", "bid", "ask", "mark", "index"):
        setattr(b, k, getattr(b, k) * f)
    return b


def inject(series: dict, spec: dict, t0_iso: str) -> dict:
    t0 = parse_ts(t0_iso)
    kind = spec["kind"]
    out = {}
    for sym, s in series.items():
        bars = []
        level = 1.0
        prev_close_new = None
        for b in s.bars:
            if b.start < t0 or kind == "none":
                bars.append(b)
                continue
            m = (b.start - t0).total_seconds() / 60
            if kind == "ramp":
                f = 1 + spec["move"] * min(1.0, (m + 1) / spec["minutes"])
                nb = _scale(b, f)
            elif kind == "gap":
                f = 1 + spec["move"]
                nb = _scale(b, f)
                # the whole post-shock path, including the first bar's OPEN, is at the gapped level: resting
                # stops fill at the gap price (gap-through), exactly the case a stop cannot protect against
                if m < spec["minutes"]:
                    mid = (nb.bid + nb.ask) / 2
                    half = (nb.ask - nb.bid) / 2 * spec["spread_mult"]
                    nb.bid, nb.ask = mid - half, mid + half
                    nb.volume_base *= spec["volume_mult"]
            elif kind == "vol":
                if m < spec["minutes"] and prev_close_new is not None:
                    r = b.close / b.open
                    f_close = prev_close_new * r ** spec["mult"] / b.close
                    nb = _scale(b, f_close)
                    nb.open = prev_close_new
                    nb.high, nb.low = max(nb.high, nb.open, nb.close), min(nb.low, nb.open, nb.close)
                    level = nb.close / b.close
                else:
                    nb = _scale(b, level)
                prev_close_new = nb.close
            elif kind == "whipsaw":
                if m < spec["minutes"]:
                    k = int(m // spec["every_minutes"])
                    level = (1 + spec["step"]) if k % 2 == 0 else (1 - spec["step"])
                    level = level if k % 4 < 2 else 1 / level
                nb = _scale(b, level)
            elif kind == "drop":
                if m < spec["minutes"]:
                    continue
                nb = b
            else:
                nb = b
            nb.high, nb.low = max(nb.high, nb.open, nb.close), min(nb.low, nb.open, nb.close)
            bars.append(nb)
        ns = BarSeries(sym)
        for b in bars:
            ns.append(b)
        out[sym] = ns
    return out

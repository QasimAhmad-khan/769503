"""Instrument metadata, labeled synthetic market fixtures and point-in-time bar access.

The synthetic generator exists so the paper/replay path runs without paid or reachable
providers. Its output is labeled synthetic everywhere and must never be used to claim
an edge on real markets.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from .util import D, UTC, floor_step


@dataclass(frozen=True)
class Instrument:
    key: str
    venue: str
    symbol: str
    base_asset: str
    quote_asset: str
    settle_asset: str
    multiplier: object  # Decimal base units per contract
    qty_step: object
    price_tick: object
    min_contracts: object
    max_contracts: object
    min_notional: object

    @classmethod
    def from_config(cls, key, spec):
        return cls(key, spec["venue"], spec["symbol"], spec["base_asset"], spec["quote_asset"], spec["settle_asset"],
                   D(spec["base_units_per_contract"]), D(spec["quantity_step_contracts"]), D(spec["price_tick"]),
                   D(spec["min_contracts"]), D(spec["max_contracts"]), D(spec["min_notional_quote"]))

    def contract_dict(self) -> dict:
        """The 03_CONTRACTS instrument object."""
        return {"venue": self.venue, "symbol": self.symbol, "product": "linear_perpetual",
                "base_asset": self.base_asset, "quote_asset": self.quote_asset, "settle_asset": self.settle_asset,
                "base_units_per_contract": _plain(self.multiplier), "quantity_step_contracts": _plain(self.qty_step),
                "price_tick": _plain(self.price_tick)}

    def floor_qty(self, contracts):
        return floor_step(contracts, self.qty_step)


def _plain(d):
    text = format(d.normalize(), "f")
    return text


def instruments_from_config(cfg) -> dict:
    return {k: Instrument.from_config(k, v) for k, v in cfg["venue_sim"]["instruments"].items()}


@dataclass
class Bar:
    symbol: str
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume_base: float
    bid: float
    ask: float
    mark: float
    index: float
    funding_rate_est: float  # estimated rate for the next settlement, known at bar end

    @property
    def available_at(self) -> datetime:
        # Completed bars become available one second after they close (publication latency).
        return self.end + timedelta(seconds=1)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.mid * 1e4


@dataclass
class BarSeries:
    symbol: str
    bars: list = field(default_factory=list)
    _avail: list = field(default_factory=list)

    def append(self, bar: Bar):
        if self.bars and bar.start < self.bars[-1].end:
            raise ValueError("bars must be appended in time order without overlap")
        self.bars.append(bar)
        self._avail.append(bar.available_at)

    def completed(self, cutoff: datetime, limit: int | None = None) -> list:
        """Bars whose publication time is at or before the cutoff. Never returns a future bar."""
        idx = bisect.bisect_right(self._avail, cutoff)
        start = 0 if limit is None else max(0, idx - limit)
        return self.bars[start:idx]

    def between(self, start: datetime, end: datetime) -> list:
        """Bars that start in [start, end) — used by the simulator to execute after decisions."""
        starts = [b.start for b in self.bars]
        return self.bars[bisect.bisect_left(starts, start):bisect.bisect_left(starts, end)]


def aggregate(bars: list, seconds: int) -> list:
    out, bucket = [], []
    for bar in bars:
        bucket.append(bar)
        if int((bar.end - bucket[0].start).total_seconds()) >= seconds:
            out.append(Bar(bar.symbol, bucket[0].start, bar.end, bucket[0].open, max(b.high for b in bucket),
                           min(b.low for b in bucket), bar.close, sum(b.volume_base for b in bucket), bar.bid,
                           bar.ask, bar.mark, bar.index, bar.funding_rate_est))
            bucket = []
    return out


def synthetic_market(start: datetime, days: float, seed: int = 7, symbols=("BTCUSDT", "ETHUSDT"),
                     start_prices=(60000.0, 2500.0)) -> dict:
    """Correlated regime-switching 1-minute paths. SYNTHETIC: not market observations."""
    rng = np.random.default_rng(seed)
    n = int(days * 1440)
    corr = np.array([[1.0, 0.8], [0.8, 1.0]])
    chol = np.linalg.cholesky(corr)
    vol = np.array([0.0009, 0.0012])  # per-minute log-return sd
    drift_state, series = 0.0, {s: BarSeries(s) for s in symbols}
    prices = np.array(start_prices, dtype=float)
    funding = np.zeros(2)
    for i in range(n):
        if i % 240 == 0:  # regime may switch every 4 hours
            drift_state = rng.choice([-1.0, 0.0, 0.0, 1.0])
        shock = chol @ rng.standard_normal(2)
        ret = drift_state * 0.00012 + vol * shock
        opens = prices.copy()
        path = opens * np.exp(np.outer(np.linspace(0.25, 1.0, 4), ret))
        prices = path[-1]
        funding = 0.97 * funding + 0.03 * (0.0001 + drift_state * 0.0003) + rng.normal(0, 0.00002, 2)
        t0 = start + timedelta(minutes=i)
        for j, sym in enumerate(symbols):
            close = float(prices[j])
            hi, lo = float(max(opens[j], path[:, j].max())), float(min(opens[j], path[:, j].min()))
            half_spread = close * (0.00003 + abs(rng.normal(0, 0.00002)))
            mark = close * (1 + rng.normal(0, 0.00005))
            series[sym].append(Bar(sym, t0, t0 + timedelta(minutes=1), float(opens[j]), hi, lo, close,
                                   float(abs(rng.normal(30 if j == 0 else 400, 8))), close - half_spread,
                                   close + half_spread, mark, close, float(funding[j])))
    return series


def funding_times(start: datetime, end: datetime, interval_hours: int = 8) -> list:
    t = start.replace(minute=0, second=0, microsecond=0)
    t = t.replace(hour=(t.hour // interval_hours) * interval_hours)
    out = []
    while t <= end:
        if t > start:
            out.append(t)
        t += timedelta(hours=interval_hours)
    return out


def daily_reference_time(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def ewm_vol(closes: np.ndarray, window: int) -> float:
    if len(closes) < 3:
        return float("nan")
    rets = np.diff(np.log(closes[-(window + 1):]))
    return float(np.std(rets, ddof=1)) if len(rets) > 1 else float("nan")


def is_finite(x) -> bool:
    return x is not None and not (isinstance(x, float) and (math.isnan(x) or math.isinf(x)))

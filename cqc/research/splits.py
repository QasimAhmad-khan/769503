"""Chronological, label-aware purged and embargoed splits.

Trades are labels spanning [opened, closed]. For a test window [t0, t1), a training trade is
admissible only if it CLOSED at or before t0 - embargo (no overlapping label, no leakage through
funding settlements or delayed on-chain availability), and it must have opened at or after the
research start. Test trades are those OPENED inside the window.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..util import parse_ts


def day_folds(start: datetime, end: datetime, fold_days: int) -> list[tuple[datetime, datetime]]:
    folds, t = [], start
    while t < end:
        folds.append((t, min(t + timedelta(days=fold_days), end)))
        t += timedelta(days=fold_days)
    return folds


def embargo_seconds(manifest: dict) -> int:
    e = manifest["splits"]
    return max(e["label_horizon_seconds"], e["funding_interval_seconds"], e["onchain_availability_lag_seconds"])


def train_trades_before(trades: list[dict], t0: datetime, embargo_s: int, research_start: datetime) -> list[dict]:
    cutoff = t0 - timedelta(seconds=embargo_s)
    return [t for t in trades if parse_ts(t["closed"]) <= cutoff and parse_ts(t["opened"]) >= research_start]


def test_trades_in(trades: list[dict], t0: datetime, t1: datetime) -> list[dict]:
    return [t for t in trades if t0 <= parse_ts(t["opened"]) < t1]


def assert_no_overlap(train: list[dict], t0: datetime, embargo_s: int):
    for t in train:
        if parse_ts(t["closed"]) > t0 - timedelta(seconds=embargo_s):
            raise AssertionError(f"purge violated: training label ends {t['closed']} inside embargo before {t0}")

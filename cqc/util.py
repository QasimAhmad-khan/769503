"""Shared primitives: UTC time, decimals, canonical hashing and deterministic IDs."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_FLOOR, Decimal, getcontext

getcontext().prec = 34
UTC = timezone.utc
ZERO = Decimal("0")


def D(value) -> Decimal:
    """Decimal from a string/int/Decimal. Floats go through repr to avoid binary noise."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def dstr(value: Decimal) -> str:
    """Plain (non-scientific) decimal string with trailing zeros removed."""
    value = D(value)
    if value == 0:
        return "0"
    text = format(value.normalize(), "f")
    return text


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    value, step = D(value), D(step)
    if step <= 0:
        raise ValueError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def round_price(value: Decimal, tick: Decimal, rounding=ROUND_DOWN) -> Decimal:
    tick = D(tick)
    return (D(value) / tick).to_integral_value(rounding=rounding) * tick


def is_multiple(value: Decimal, step: Decimal) -> bool:
    return D(value) % D(step) == 0


def utc(year, month, day, hour=0, minute=0, second=0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def secs(n: float) -> timedelta:
    return timedelta(seconds=n)


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def content_hash(obj) -> str:
    return sha256_hex(canonical_json(obj))


def plan_hash(plan: dict) -> str:
    """Fixture rule from 04_EXAMPLES.json: hash of the full record without plan_sha256."""
    return content_hash({k: v for k, v in plan.items() if k != "plan_sha256"})


def stable_id(prefix: str, *parts) -> str:
    """Deterministic ID so that replays reproduce identical event identities."""
    return f"{prefix}_{sha256_hex(canonical_json([str(p) for p in parts]))[:16]}"


def approx_tokens(text: str) -> int:
    """Conservative token estimate used only for budget enforcement (never for billing)."""
    return (len(text.encode("utf-8")) + 2) // 3

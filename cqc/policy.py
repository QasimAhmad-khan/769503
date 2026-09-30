"""`autonomous_evidence_v1` entry policy — the explicit replacement for fail-open AI filtering.

QuantDinger's legacy AI filter (audited at 9e3095d) lets entries proceed after provider failure,
missing configuration or unavailable billing, can fall back to another LLM, and is bypassed by
some entry routes. In this mode every risk-increasing order must arrive through the single
autonomous route, for a supported strategy, carrying a *valid, unexpired* decision receipt from
the configured provider that selected exactly this plan, with durable audit already written.
Anything else is blocked. Protective exits never pass through this gate.
"""
from __future__ import annotations

from datetime import datetime

from .contracts import ABSTAIN
from .util import parse_ts

POLICY = "autonomous_evidence_v1"
ALLOWED_ENTRY_ROUTES = frozenset({"autonomous_cycle"})
SUPPORTED_STRATEGIES = frozenset({"trend_breakout_v1", "funding_crowding_v1"})


class EntryBlocked(RuntimeError):
    pass


def check_entry(*, cfg, route: str, strategy: str, plan: dict, receipt: dict | None, audit_ok: bool,
                now: datetime, expected_provider: str) -> tuple[bool, str]:
    """`expected_provider` is the selector the operator configured for this run (Jev, or the explicit
    deterministic ablation policy). A receipt from any other provider is a fallback and is blocked."""
    if cfg["autonomous_policy"] != POLICY:
        return False, "POLICY_NOT_AUTONOMOUS_EVIDENCE_V1"
    if not plan.get("risk_increasing"):
        return True, "NOT_RISK_INCREASING"
    if route not in ALLOWED_ENTRY_ROUTES:
        return False, f"UNSUPPORTED_ENTRY_ROUTE:{route}"
    if strategy not in SUPPORTED_STRATEGIES:
        return False, f"UNSUPPORTED_STRATEGY:{strategy}"
    if not audit_ok:
        return False, "DURABLE_AUDIT_UNAVAILABLE"
    if receipt is None:
        return False, "NO_DECISION"
    if receipt["provider"] != expected_provider:
        return False, "PROVIDER_MISMATCH_NO_FALLBACK"
    if receipt["validation_status"] != "valid":
        return False, f"DECISION_{receipt['validation_status'].upper()}"
    if receipt["selected_id"] == ABSTAIN or receipt["selected_id"] != plan["candidate_id"]:
        return False, "DECISION_DID_NOT_SELECT_PLAN"
    if parse_ts(receipt["expires_at"]) <= now:
        return False, "DECISION_EXPIRED"
    return True, "OK"

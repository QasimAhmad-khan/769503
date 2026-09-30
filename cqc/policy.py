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


def check_entry(*, cfg, route: str, strategy: str, plan: dict, decision: dict | None, audit_ok: bool,
                now: datetime, expected_selector: str, expected_backend_id: str) -> tuple[bool, str]:
    """`expected_selector`/`expected_backend_id` are what the operator configured for this run: the shared
    local Phi decision maker (one backend/model revision) or the explicit deterministic ablation policy.
    Any other selector, model or backend is a fallback and is blocked. Legacy Jev receipts never pass."""
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
    if decision is None:
        return False, "NO_DECISION"
    if decision.get("kind") != "decision_record":
        return False, "LEGACY_OR_UNKNOWN_DECISION_RECORD"
    if decision["selector"] != expected_selector:
        return False, "SELECTOR_MISMATCH_NO_FALLBACK"
    if decision["backend_id"] != expected_backend_id:
        return False, "MODEL_OR_BACKEND_MISMATCH"
    if decision["validation_status"] != "valid":
        return False, f"DECISION_{decision['validation_status'].upper()}"
    if decision["selected_id"] == ABSTAIN or decision["selected_id"] != plan["candidate_id"]:
        return False, "DECISION_DID_NOT_SELECT_PLAN"
    if decision["candidate_plan_hashes"].get(plan["candidate_id"]) != plan["plan_sha256"]:
        return False, "PLAN_HASH_NOT_THE_ONE_DECIDED"
    if parse_ts(decision["expires_at"]) <= now:
        return False, "DECISION_EXPIRED"
    return True, "OK"

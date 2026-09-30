"""Versioned contracts: starter JSON Schema (03_CONTRACTS) plus the extension payloads the
build spec requires, and cross-record semantic validators. Schema validity is necessary
but never sufficient; semantic checks enforce hashes, time ordering, units and membership.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from .util import D, content_hash, parse_ts, plan_hash

SCHEMA_PATH = Path(__file__).parent / "schemas" / "contracts.schema.json"
ABSTAIN = "ABSTAIN"

_DEC = {"type": "string", "pattern": r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$", "maxLength": 48}
_SDEC = {"type": "string", "pattern": r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$", "maxLength": 48}
_ID = {"type": "string", "minLength": 1, "maxLength": 128}
_TS = {"type": "string", "format": "date-time", "maxLength": 35}
_CODES = {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 64}, "maxItems": 8}
_ENVELOPE = {
    "schema_version": {"const": "1.0.0"}, "event_id": _ID, "correlation_id": _ID, "producer": _ID,
    "created_at": _TS, "knowledge_cutoff": _TS, "expires_at": _TS, "environment": {"const": "paper"},
    "synthetic": {"type": "boolean"},
}
_ENV_REQ = list(_ENVELOPE)


def _obj(kind, props, required, extra_all_of=None):
    schema = {"type": "object", "properties": {**_ENVELOPE, "kind": {"const": kind}, **props},
              "required": _ENV_REQ + ["kind"] + required, "additionalProperties": False}
    if extra_all_of:
        schema["allOf"] = extra_all_of
    return schema


# Extension contracts required by 01_BUILD_SPEC.md section 14 and 02_AGENT_PROMPTS.md.
EXTENSION_DEFS = {
    "tool_request": _obj("tool_request", {
        "tool": {"type": "string", "minLength": 1, "maxLength": 64},
        "tool_version": {"type": "string", "minLength": 1, "maxLength": 32},
        "arguments": {"type": "object", "maxProperties": 16},
        "input_ids": {"type": "array", "items": _ID, "maxItems": 16},
    }, ["tool", "tool_version", "arguments", "input_ids"]),
    "tool_result": _obj("tool_result", {
        "request_event_id": _ID, "tool": {"type": "string", "minLength": 1, "maxLength": 64},
        "tool_version": {"type": "string", "minLength": 1, "maxLength": 32},
        "status": {"enum": ["ok", "error"]}, "output": {"type": ["object", "null"]},
        "output_sha256": {"type": ["string", "null"]}, "error": {"type": ["string", "null"], "maxLength": 256},
    }, ["request_event_id", "tool", "tool_version", "status", "output", "output_sha256", "error"]),
    "evidence_result": _obj("evidence_result", {
        "request_id": _ID, "status": {"enum": ["complete", "incomplete", "blocked"]},
        "evidence_ids": {"type": "array", "items": _ID, "maxItems": 16},
        "missing_required": {"type": "array", "items": _ID, "maxItems": 8},
        "contradictions": {"type": "array", "items": {"type": "string", "maxLength": 128}, "maxItems": 8},
        "reason_codes": _CODES,
    }, ["request_id", "status", "evidence_ids", "missing_required", "contradictions", "reason_codes"]),
    "analysis_packet": _obj("analysis_packet", {
        "snapshot_id": _ID, "hypothesis_id": _ID, "status": {"enum": ["candidates", "abstain", "blocked"]},
        "tool_result_ids": {"type": "array", "items": _ID, "maxItems": 16},
        "candidate_ids": {"type": "array", "items": _ID, "maxItems": 4},
        "effective_sample_count": {"type": "integer", "minimum": 0},
        "missing": {"type": "array", "items": _ID, "maxItems": 8}, "reason_codes": _CODES,
    }, ["snapshot_id", "hypothesis_id", "status", "tool_result_ids", "candidate_ids", "effective_sample_count",
        "missing", "reason_codes"]),
    "adjustment_proposal": _obj("adjustment_proposal", {
        "status": {"enum": ["propose", "no_change", "blocked"]},
        "trigger": {"type": "string", "maxLength": 64},
        "position_refs": {"type": "array", "items": _ID, "maxItems": 8},
        "action_category": {"enum": ["none", "request_stress_test", "tighten_stop", "reduce", "close"]},
        "stress_scenarios": {"type": "array", "items": {"enum": ["joint_btc_eth_down_10", "spread_widen_5x",
                                                                   "funding_shock", "delayed_exit_15m"]}, "maxItems": 4},
        "evidence_ids": {"type": "array", "items": _ID, "maxItems": 16}, "reason_codes": _CODES,
    }, ["status", "trigger", "position_refs", "action_category", "stress_scenarios", "evidence_ids", "reason_codes"]),
    "risk_authorization": _obj("risk_authorization", {
        "authorization_id": _ID, "candidate_id": _ID, "plan_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "decision_id": _ID, "account_state_version": _ID, "policy_version": _ID,
        "approved": {"type": "boolean"}, "max_quantity_contracts": _DEC, "price_min": _DEC, "price_max": _DEC,
        "reserved_stop_risk_quote": _DEC, "reserved_notional_quote": _DEC, "reason_codes": _CODES,
    }, ["authorization_id", "candidate_id", "plan_sha256", "decision_id", "account_state_version", "policy_version",
        "approved", "max_quantity_contracts", "price_min", "price_max", "reserved_stop_risk_quote",
        "reserved_notional_quote", "reason_codes"]),
    "risk_event": _obj("risk_event", {
        "from_state": {"enum": ["NORMAL", "NO_NEW_RISK", "REDUCE_ONLY", "HALTED", "RECOVERY"]},
        "to_state": {"enum": ["NORMAL", "NO_NEW_RISK", "REDUCE_ONLY", "HALTED", "RECOVERY"]},
        "trigger": {"type": "string", "maxLength": 64}, "detail": {"type": "string", "maxLength": 256},
    }, ["from_state", "to_state", "trigger", "detail"]),
}

# Role-result unions: Phi must return exactly one of these per role.
ROLE_RESULTS = {
    "analyzer": ["research_request", "analysis_packet"],
    "screener": ["tool_request", "evidence_result"],
    "risk_analyst": ["adjustment_proposal"],
}


def _load_defs():
    base = json.loads(SCHEMA_PATH.read_text())
    defs = dict(base["$defs"])
    defs.update(EXTENSION_DEFS)
    return defs


DEFS = _load_defs()
_VALIDATORS = {kind: Draft202012Validator({"$ref": f"#/$defs/{kind}", "$defs": DEFS}, format_checker=FormatChecker())
               for kind in DEFS if kind != "instrument"}


class ContractError(ValueError):
    pass


def schema_errors(record: dict) -> list[str]:
    kind = record.get("kind") if isinstance(record, dict) else None
    validator = _VALIDATORS.get(kind)
    if validator is None:
        return [f"unknown kind {kind!r}"]
    return [f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in validator.iter_errors(record)]


def validate(record: dict) -> dict:
    errors = schema_errors(record) + semantic_errors(record)
    if errors:
        raise ContractError("; ".join(errors[:6]))
    return record


def validate_role_result(role: str, record: dict) -> dict:
    if not isinstance(record, dict) or record.get("kind") not in ROLE_RESULTS[role]:
        raise ContractError(f"{role} may only return {ROLE_RESULTS[role]}")
    return validate(record)


# ---------------------------------------------------------------- semantic checks
def _envelope_errors(r):
    errs = []
    try:
        created, cutoff, expires = (parse_ts(r[k]) for k in ("created_at", "knowledge_cutoff", "expires_at"))
    except (KeyError, ValueError):
        return ["envelope timestamps unparsable"]
    if cutoff > created:
        errs.append("knowledge_cutoff after created_at")
    if expires <= created:
        errs.append("expires_at must be after created_at")
    return errs


def evidence_content(r: dict) -> dict:
    keys = ("feature", "instrument", "value", "unit", "status", "reason", "event_time", "source_id",
            "source_record_id", "revision", "chain")
    return {k: r[k] for k in keys}


def evidence_hash_ok(r: dict) -> bool:
    """Our content-hash rule (checked at ingestion). The 04_EXAMPLES fixtures use an undocumented
    rule, so this is enforced for records produced by this system's connectors, not the fixtures."""
    return r["content_sha256"] == content_hash(evidence_content(r))


def _evidence_errors(r):
    errs = []
    event_time, available_at, observed_at = (parse_ts(r[k]) for k in ("event_time", "available_at", "observed_at"))
    if event_time > available_at:
        errs.append("evidence event_time after available_at")
    if available_at > observed_at:
        errs.append("evidence available_at after observed_at")
    if r["status"] == "available" and available_at > parse_ts(r["knowledge_cutoff"]):
        errs.append("available evidence not known by its knowledge_cutoff")
    if r["chain"] and r["chain"]["finality"] == "orphaned" and r["status"] != "invalidated":
        errs.append("orphaned chain evidence must be invalidated")
    return errs


def _candidate_errors(r):
    errs = []
    if r["plan_sha256"] != plan_hash(r):
        errs.append("plan_sha256 mismatch")
    qty, pmin, pmax = D(r["quantity_contracts"]), D(r["price_min"]), D(r["price_max"])
    step = D(r["instrument"]["quantity_step_contracts"])
    if qty <= 0 or qty % step != 0:
        errs.append("quantity must be a positive multiple of quantity_step_contracts")
    if pmin <= 0 or pmin > pmax:
        errs.append("invalid price envelope")
    if r["stop_price"] is not None:
        stop = D(r["stop_price"])
        if r["position_side"] == "long" and r["action"].startswith("OPEN") and stop >= pmin:
            errs.append("long stop must be below the entry envelope")
        if r["position_side"] == "short" and r["action"].startswith("OPEN") and stop <= pmax:
            errs.append("short stop must be above the entry envelope")
    if r["metrics"]["horizon_seconds"] <= 0:
        errs.append("bad horizon")
    return errs


def _decision_errors(r):
    errs = []
    allowed = set(r["candidate_ids"]) | {ABSTAIN}
    if r["selected_id"] not in allowed:
        errs.append("selected_id not in allowed set")
    if r["validation_status"] == "valid":
        probs = r["choice_probabilities"]
        if set(probs) != allowed:
            errs.append("probability keys do not match allowed labels")
        if not all(math.isfinite(v) for v in probs.values()) or abs(sum(probs.values()) - 1.0) > 1e-4:
            errs.append("choice probabilities not normalized")
    elif r["selected_id"] != ABSTAIN:
        errs.append("non-valid decision must select ABSTAIN")
    return errs


def _graph_errors(r):
    ids = {n["id"] for n in r["nodes"]}
    errs = [f"edge {e['id']} endpoint missing" for e in r["edges"] if e["from"] not in ids or e["to"] not in ids]
    cutoff = parse_ts(r["knowledge_cutoff"])
    errs += [f"node {n['id']} after cutoff" for n in r["nodes"] if parse_ts(n["available_at"]) > cutoff]
    return errs


def _auth_errors(r):
    if D(r["price_min"]) > D(r["price_max"]):
        return ["authorization price envelope inverted"]
    return []


_SEMANTIC = {"evidence": _evidence_errors, "candidate_plan": _candidate_errors, "decision_receipt": _decision_errors,
             "graph_slice": _graph_errors, "risk_authorization": _auth_errors}


def semantic_errors(record: dict) -> list[str]:
    if not isinstance(record, dict) or schema_errors(record):
        return []
    errs = _envelope_errors(record)
    check = _SEMANTIC.get(record["kind"])
    return errs + (check(record) if check else [])


def envelope(kind: str, event_id: str, correlation_id: str, producer: str, created_at: str, cutoff: str,
             expires_at: str, synthetic: bool = True) -> dict:
    return {"schema_version": "1.0.0", "kind": kind, "event_id": event_id, "correlation_id": correlation_id,
            "producer": producer, "created_at": created_at, "knowledge_cutoff": cutoff, "expires_at": expires_at,
            "environment": "paper", "synthetic": synthetic}

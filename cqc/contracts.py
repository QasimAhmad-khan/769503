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
    # Host-built from connector data (timestamps come from sources, never from the model).
    "evidence_result": _obj("evidence_result", {
        "request_id": _ID, "round": {"type": "integer", "minimum": 0, "maximum": 1},
        "status": {"enum": ["complete", "incomplete", "blocked"]},
        "items": {"type": "array", "maxItems": 8, "items": {"type": "object", "additionalProperties": False, "properties": {
            "variable": {"type": "string", "minLength": 1, "maxLength": 64}, "evidence_id": {"type": ["string", "null"]},
            "status": {"enum": ["available", "missing", "stale", "invalidated", "excluded", "declined"]},
            "source_id": {"type": ["string", "null"], "maxLength": 128},
            "source_timestamp": {"anyOf": [_TS, {"type": "null"}]}, "observation_timestamp": {"anyOf": [_TS, {"type": "null"}]},
            "available_at": {"anyOf": [_TS, {"type": "null"}]}, "lag_seconds": {"type": ["number", "null"], "minimum": 0},
            "unit": {"type": "string", "maxLength": 64}, "quality_flags": {"type": "array", "maxItems": 12,
                                                                            "items": {"type": "string", "maxLength": 64}},
            "content_sha256": {"type": ["string", "null"]}, "reason": {"type": ["string", "null"], "maxLength": 256}},
            "required": ["variable", "evidence_id", "status", "source_id", "source_timestamp", "observation_timestamp",
                         "available_at", "lag_seconds", "unit", "quality_flags", "content_sha256", "reason"]}},
        "missing_required": {"type": "array", "items": _ID, "maxItems": 8},
        "contradictions": {"type": "array", "items": {"type": "string", "maxLength": 128}, "maxItems": 8},
        "reason_codes": _CODES,
    }, ["request_id", "round", "status", "items", "missing_required", "contradictions", "reason_codes"]),
    # Analyzer -> screener: typed, bounded evidence request (replaces the starter research_request as model output).
    "evidence_request": _obj("evidence_request", {
        "request_id": _ID, "hypothesis_id": _ID, "round": {"type": "integer", "minimum": 0, "maximum": 1},
        "items": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
            "type": "object", "additionalProperties": False, "properties": {
                "variable": {"type": "string", "minLength": 1, "maxLength": 64},
                "symbol": {"type": "string", "minLength": 1, "maxLength": 64},
                "interval_start": _TS, "interval_end": _TS,
                "source_class": {"enum": ["venue_market_data", "onchain_finalized"]},
                "max_age_seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
                "required": {"type": "boolean"},
                "relevance": {"type": "string", "minLength": 1, "maxLength": 160}},
            "required": ["variable", "symbol", "interval_start", "interval_end", "source_class", "max_age_seconds",
                         "required", "relevance"]}},
    }, ["request_id", "hypothesis_id", "round", "items"]),
    # Screener model output: which whitelisted items to fetch; data itself is fetched and stamped by the host.
    "fetch_plan": _obj("fetch_plan", {
        "request_id": _ID,
        "fetch": {"type": "array", "maxItems": 8, "items": {"type": "object", "additionalProperties": False, "properties": {
            "variable": {"type": "string", "minLength": 1, "maxLength": 64},
            "source_ids": {"type": "array", "items": _ID, "maxItems": 4},
            "action": {"enum": ["fetch", "use_cache", "decline"]}},
            "required": ["variable", "source_ids", "action"]}},
        "reason_codes": _CODES,
    }, ["request_id", "fetch", "reason_codes"]),
    # Decision-maker model output. The host narrows selected_id to an enum of offered IDs + ABSTAIN per call.
    "decision_choice": _obj("decision_choice", {
        "selected_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "reason_codes": {"type": "array", "minItems": 1, "maxItems": 4, "items": {"enum": [
            "EVIDENCE_SUPPORTS", "EVIDENCE_CONFLICT", "INSUFFICIENT_EVIDENCE", "COST_OR_RISK_CONCERN",
            "PREFER_SMALLER_SIZE", "NO_CANDIDATE_ADEQUATE", "STALE_OR_MISSING_DATA"]}},
    }, ["selected_id", "reason_codes"]),
    # Host-built decision record. Generative-model output carries NO probability semantics.
    "decision_record": _obj("decision_record", {
        "decision_id": _ID, "snapshot_id": _ID, "candidate_ids": {"type": "array", "items": _ID, "maxItems": 4},
        "candidate_plan_hashes": {"type": "object", "maxProperties": 4,
                                  "additionalProperties": {"type": "string", "pattern": "^[a-f0-9]{64}$"}},
        "selected_id": _ID, "reason_codes": _CODES,
        "selector": {"enum": ["phi_decision_maker", "deterministic_rank_v1"]},
        "model_id": {"type": "string", "maxLength": 128}, "model_revision": {"type": "string", "maxLength": 128},
        "backend_id": {"type": "string", "maxLength": 256}, "prompt_version": {"type": "string", "maxLength": 64},
        "request_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "raw_response_sha256": {"type": ["string", "null"]},
        "validation_status": {"enum": ["valid", "invalid", "timeout", "unavailable", "overloaded", "context_rejected",
                                       "no_candidates"]},
        "uncalibrated_diagnostics": {"type": ["object", "null"], "maxProperties": 8},
        "calibrated_selection_probability": {"type": "null"},
        "calibration_artifact_id": {"type": "null"},
        "score_semantics": {"const": "none_uncalibrated_generative_choice"},
        "latency_ms": {"type": "integer", "minimum": 0}, "queue_delay_ms": {"type": "integer", "minimum": 0},
        "executable_authorization": {"const": False},
    }, ["decision_id", "snapshot_id", "candidate_ids", "candidate_plan_hashes", "selected_id", "reason_codes", "selector",
        "model_id", "model_revision", "backend_id", "prompt_version", "request_sha256", "raw_response_sha256",
        "validation_status", "uncalibrated_diagnostics", "calibrated_selection_probability", "calibration_artifact_id",
        "score_semantics", "latency_ms", "queue_delay_ms", "executable_authorization"]),
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
        "action_category": {"enum": ["none", "request_stress_test", "tighten_stop", "reduce", "close",
                                     "cancel_pending", "move_to_no_new_risk"]},
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
    "screener": ["fetch_plan"],
    "analyzer": ["evidence_request", "analysis_packet"],
    "decision_maker": ["decision_choice"],
    "risk_analyst": ["adjustment_proposal"],
}
ROLES = tuple(ROLE_RESULTS)

# Records written by the superseded Open-Jev selection path (ledgers from commit 1135387 and earlier).
# They stay readable for audit but are never interpreted as Phi decisions or replay inputs.
LEGACY_EVENT_KINDS = {"decision_receipt": "legacy_jev_selection_v1", "jev_raw_response": "legacy_jev_raw_response_v1"}


def legacy_label(kind: str) -> str | None:
    return LEGACY_EVENT_KINDS.get(kind)


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


def _decision_record_errors(r):
    errs = []
    allowed = set(r["candidate_ids"]) | {ABSTAIN}
    if r["selected_id"] not in allowed:
        errs.append("selected_id not in offered set")
    if set(r["candidate_plan_hashes"]) != set(r["candidate_ids"]):
        errs.append("candidate_plan_hashes must cover exactly the offered candidates")
    if r["validation_status"] != "valid" and r["selected_id"] != ABSTAIN:
        errs.append("non-valid decision must select ABSTAIN")
    return errs


def _evidence_request_errors(r):
    errs = []
    cutoff = parse_ts(r["knowledge_cutoff"])
    for item in r["items"]:
        if parse_ts(item["interval_start"]) > parse_ts(item["interval_end"]):
            errs.append(f"{item['variable']}: interval inverted")
        if parse_ts(item["interval_end"]) > cutoff:
            errs.append(f"{item['variable']}: interval ends after the decision cutoff")
    return errs


def _auth_errors(r):
    if D(r["price_min"]) > D(r["price_max"]):
        return ["authorization price envelope inverted"]
    return []


_SEMANTIC = {"evidence": _evidence_errors, "candidate_plan": _candidate_errors, "decision_receipt": _decision_errors,
             "decision_record": _decision_record_errors, "evidence_request": _evidence_request_errors,
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

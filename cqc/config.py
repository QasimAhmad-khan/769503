"""Paper-profile configuration loader.

Configuration is hard policy: it is loaded once, validated, deep-frozen and never
writable by model output. Unsafe settings are rejected rather than repaired.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType

from .util import D

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "paper.json"

DECISION_SELECTORS = {"phi", "deterministic_rank_v1"}
ROLES = {"screener", "analyzer", "decision_maker", "risk_analyst"}
PHI_BACKENDS = {"openai_compatible", "fake"}


class ConfigError(ValueError):
    pass


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _require(cond, message):
    if not cond:
        raise ConfigError(message)


def validate(raw: dict) -> None:
    _require(raw.get("schema_version") == "1.0.0", "unsupported config schema_version")
    _require(raw.get("mode") == "paper", "only paper mode is implemented")
    _require(raw.get("live_trading_enabled") is False, "live trading must remain disabled")
    _require(raw.get("autonomous_policy") == "autonomous_evidence_v1", "autonomous_policy must be autonomous_evidence_v1")
    _require(raw.get("require_durable_audit_before_entry") is True, "durable audit before entry is mandatory")
    _require(raw.get("new_risk_on_model_failure") == "ABSTAIN", "model failure must ABSTAIN")
    _require(raw.get("remote_llm_fallback_enabled") is False, "remote LLM fallback must be disabled")

    market = raw["market"]
    _require(market["product"] == "linear_usdt_perpetual", "only linear USDT perpetuals are supported")
    _require(market["position_mode"] == "one_way", "only one-way position mode is supported")
    _require(market["decision_bar_seconds"] > 0 and market["forecast_horizon_seconds"] > 0, "bad horizons")

    phi = raw["phi"]
    _require(phi["resident_model_processes"] == 1, "exactly one resident Phi process is allowed")
    _require(phi["active_sequences"] == 1, "one active Phi sequence in the paper profile")
    _require(phi["max_output_tokens"] < phi["max_total_tokens"], "output budget must fit in total budget")
    _require(set(phi["roles"]) == ROLES, "exactly four Phi roles on one model: screener, analyzer, decision_maker, risk_analyst")
    _require("jev" not in raw, "Jev/Open-Jev is not a provider in this system; remove the 'jev' section")
    _require(phi.get("backend", "fake") in PHI_BACKENDS, "unknown Phi backend")

    dec = raw["decision"]
    _require(dec["selector"] in DECISION_SELECTORS, f"decision.selector must be one of {sorted(DECISION_SELECTORS)}")
    _require(1 <= dec["max_trade_candidates"] <= 4, "choice set is at most four candidates plus ABSTAIN")
    _require(dec["always_include_abstain"] is True, "ABSTAIN must always be offered")
    _require(dec["calibration_artifact_id"] is None or isinstance(dec["calibration_artifact_id"], str), "bad calibration id")

    research = raw["research"]
    _require(research["max_followup_rounds"] <= 1, "at most one follow-up evidence round")
    _require(research["required_evidence_missing"] == "ABSTAIN", "missing required evidence must ABSTAIN")
    _require(research["max_phi_calls_per_cycle"] >= 1, "bad Phi call budget")
    if research.get("hypotheses"):
        from .research.signals import REGISTRY
        unknown = set(research["hypotheses"]) - set(REGISTRY)
        _require(not unknown, f"unregistered research hypotheses: {sorted(unknown)}")

    risk = raw["paper_risk"]
    for key in ("max_equity_fraction_at_stop_per_new_trade", "max_aggregate_reserved_stop_risk_fraction",
                "max_gross_notional_over_equity", "max_symbol_notional_over_equity",
                "daily_loss_fraction_stop_new_risk", "peak_drawdown_fraction_reduce_only"):
        _require(0 < float(risk[key]) <= 1.0, f"paper_risk.{key} out of range")
    _require(D(risk["starting_equity_usdt"]) > 0, "starting equity must be positive")
    _require(risk["allow_stop_loosening"] is False, "stop loosening is prohibited in the paper profile")
    _require(risk["allow_leverage_increase_after_entry"] is False, "leverage increase after entry is prohibited")

    promotion = raw["promotion"]
    _require(promotion["auto_promote"] is False, "automatic promotion is prohibited")
    _require(promotion["operator_approval_required"] is True, "operator approval is required for promotion")


def load_config(path: str | Path | None = None, overrides: dict | None = None):
    path = Path(path) if path else DEFAULT_CONFIG
    raw = json.loads(path.read_text())
    if overrides:
        raw = _deep_merge(raw, overrides)
    validate(raw)
    return _freeze(raw)


def _deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for key, value in extra.items():
        out[key] = _deep_merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def promotion_blockers(cfg) -> list[str]:
    """Unresolved fields that must block any live-readiness or promotion claim."""
    blockers = []
    for key, value in cfg["promotion"].items():
        if value is None:
            blockers.append(f"promotion.{key} unresolved")
    if not cfg["promotion"]["prospective_evaluation_complete"]:
        blockers.append("promotion.prospective_evaluation_complete is false")
    for key in ("host_ram_budget_gib", "gpu_vram_budget_gib", "disk_quota_gib"):
        if cfg["resources"][key] is None:
            blockers.append(f"resources.{key} unmeasured")
    if cfg["phi"].get("backend", "fake") == "fake":
        blockers.append("fake Phi backend configured (phi.backend)")
    for key in ("model_revision", "runtime", "quantization"):
        value = str(cfg["phi"][key])
        if value.isupper() or value.startswith("RESOLVE") or value.startswith("SELECT") or value.startswith("BENCHMARK"):
            blockers.append(f"phi.{key} unresolved ({value})")
    blockers.append("Phi decision-maker value vs deterministic_rank_v1 not established on real point-in-time data")
    if cfg["paper_risk"]["venue_margin_rules"].startswith("REQUIRED"):
        blockers.append("venue margin rules not configured for a real venue")
    if cfg["market"]["venue"] == "SIMULATED":
        blockers.append("no real venue selected")
    return blockers

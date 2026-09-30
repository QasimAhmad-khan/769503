"""Accounting fixtures with hand-computed results, contract/hash rules and config safety."""
import json
from decimal import Decimal as Dec
from pathlib import Path

import pytest

from cqc import contracts
from cqc.accounting import AccountingError, Position, fee, funding_payment, maintenance_margin
from cqc.config import ConfigError, load_config, promotion_blockers
from cqc.quant import size_position
from cqc.util import D, floor_step, plan_hash

ROOT = Path(__file__).resolve().parent.parent
TIERS = load_config()["venue_sim"]["maintenance_margin_tiers"]


def test_linear_pnl_fees_and_partial_close_match_hand_fixture():
    # 10 contracts x 0.001 BTC: buy @60000, sell 4 @61000, sell 6 @59000; taker 5 bps
    p = Position("BTCUSDT", D("0.001"))
    p.apply_fill("buy", 10, 60000, fee(10, "0.001", 60000, "0.0005"))  # fee 0.30
    p.apply_fill("sell", 4, 61000, fee(4, "0.001", 61000, "0.0005"))   # +4.00 gross, fee 0.122
    p.apply_fill("sell", 6, 59000, fee(6, "0.001", 59000, "0.0005"))   # -6.00 gross, fee 0.177
    assert p.contracts == 0
    assert p.fees_paid == Dec("0.599")
    assert p.realized_pnl == Dec("4") - Dec("6") - Dec("0.599")


def test_short_pnl_and_signed_funding():
    p = Position("ETHUSDT", D("0.01"))
    p.apply_fill("sell", 100, 2500, 0)   # short 1 ETH
    assert p.unrealized(2400) == Dec("100")
    paid = p.settle_funding(2400, "0.0001")  # short receives positive funding: payment is negative
    assert paid == Dec("-0.24")
    assert p.realized_pnl == Dec("0.24")
    assert funding_payment(100, "0.01", 2400, "0.0001") == Dec("0.24")  # long would pay


def test_fee_is_on_notional_not_multiplied_by_leverage():
    assert fee(10, "0.001", 60000, "0.0005") == Dec("0.3")


def test_closing_fill_never_reverses_position():
    p = Position("BTCUSDT", D("0.001"))
    p.apply_fill("buy", 5, 60000, 0)
    with pytest.raises(AccountingError):
        p.apply_fill("sell", 6, 60000, 0)
    with pytest.raises(AccountingError):
        p.apply_fill("buy", 1, 60000, 0, reduce_only=True)


def test_tiered_maintenance_margin_and_isolated_liquidation_price():
    assert maintenance_margin(D("100000"), TIERS) == Dec("100000") * Dec("0.01") - Dec("250")
    p = Position("BTCUSDT", D("0.001"))
    p.apply_fill("buy", 1000, 60000, 0, leverage=2)  # 1 BTC, margin 30000
    liq = p.liquidation_price(TIERS)
    # long: L = (qE - margin - amount) / (q(1-mmr)) = (60000-30000-250)/0.99
    assert liq == (Dec("60000") - Dec("30000") - Dec("250")) / Dec("0.99")


def test_sizing_formula_rounds_down_and_reports_binding_bound():
    out = size_position(entry=60000, stop=59000, multiplier="0.001", qty_step="1", stress_slippage_per_base=60,
                        stressed_fees_per_contract="0.06", adverse_funding_per_contract="0.01", loss_budget=25,
                        exposure_room=10000, symbol_room=2500, margin_room=20000, liquidity_contracts=1000,
                        stress_room=200, stress_move="0.10")
    # loss/contract = 0.001*(1000+60) + 0.06 + 0.01 = 1.13 -> n_stop = 22.12 -> 22
    assert out["contracts"] == 22 and out["binding"] == "n_stop"
    assert out["contracts"] == floor_step(D(25) / D("1.13"), D(1))


def test_fixture_candidate_hash_rule_and_examples_validate():
    examples = json.loads((ROOT / "04_EXAMPLES.json").read_text())["records"]
    for rec in examples:
        assert contracts.schema_errors(rec) == [], rec["kind"]
    cand = next(r for r in examples if r["kind"] == "candidate_plan")
    assert plan_hash(cand) == cand["plan_sha256"]


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(unknown_field=1),
    lambda r: r.update(status="available", value=None),
    lambda r: r.update(value=None),  # available evidence without a value
])
def test_malformed_evidence_rejected(mutate):
    rec = next(r for r in json.loads((ROOT / "04_EXAMPLES.json").read_text())["records"]
               if r["kind"] == "evidence" and r["status"] == "available")
    rec = dict(rec)
    mutate(rec)
    with pytest.raises(contracts.ContractError):
        contracts.validate(rec)


def test_reduction_marked_as_new_exposure_rejected_and_probability_above_one_rejected():
    recs = json.loads((ROOT / "04_EXAMPLES.json").read_text())["records"]
    cand = dict(next(r for r in recs if r["kind"] == "candidate_plan"))
    cand.update(action="REDUCE", position_ref="pos1", risk_increasing=True)
    assert contracts.schema_errors(cand)
    dec = json.loads(json.dumps(next(r for r in recs if r["kind"] == "decision_receipt")))
    dec["choice_probabilities"]["ABSTAIN"] = 1.2
    assert contracts.schema_errors(dec)


def test_config_rejects_unsafe_overrides_and_is_immutable():
    for override in ({"live_trading_enabled": True}, {"remote_llm_fallback_enabled": True},
                     {"new_risk_on_model_failure": "ALLOW"}, {"phi": {"resident_model_processes": 2}},
                     {"jev": {"max_trade_candidates": 9}}, {"jev": {"model": "jev-latest"}},
                     {"paper_risk": {"allow_stop_loosening": True}}, {"promotion": {"auto_promote": True}}):
        with pytest.raises(ConfigError):
            load_config(overrides=override)
    cfg = load_config()
    with pytest.raises(TypeError):
        cfg["paper_risk"]["max_gross_notional_over_equity"] = 50  # model output cannot alter hard caps
    blockers = promotion_blockers(cfg)
    assert any("no real venue" in b for b in blockers) and any("calibration" in b for b in blockers)

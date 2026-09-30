"""Point-in-time integrity: no future/revised evidence, missing is never zero, reorgs invalidate."""
from datetime import timedelta

import numpy as np

from cqc.market import BarSeries, synthetic_market
from cqc.onchain import EsploraBlockConnector, FixtureChainConnector
from cqc.pit import EvidenceStore, make_evidence, usable
from cqc.quant import empirical_forecast, features
from cqc.util import utc

INST = {"venue": "SIMULATED", "symbol": "BTCUSDT-PERP", "product": "linear_perpetual", "base_asset": "BTC",
        "quote_asset": "USDT", "settle_asset": "USDT", "base_units_per_contract": "0.001",
        "quantity_step_contracts": "1", "price_tick": "0.1"}
T0 = utc(2026, 3, 1, 12)


def ev(value, event_time, available_at, revision=0, rid="r1"):
    return make_evidence("estimated_funding_rate", INST, value, "decimal_per_funding_interval", "available", None,
                         event_time, available_at, available_at, "src", rid, revision, available_at)


def test_future_and_later_revised_evidence_cannot_enter_past_decision():
    store = EvidenceStore()
    store.add(ev(0.0001, T0, T0 + timedelta(seconds=5)))
    store.add(ev(0.0009, T0, T0 + timedelta(hours=2), revision=1))  # revision published two hours later
    store.add(ev(0.0005, T0 + timedelta(minutes=10), T0 + timedelta(minutes=10)))  # future observation
    got = store.as_of("estimated_funding_rate", INST, T0 + timedelta(minutes=1), 600)
    assert got["value"] == 0.0001 and got["revision"] == 0
    later = store.as_of("estimated_funding_rate", INST, T0 + timedelta(hours=3), 3 * 3600)
    assert later["value"] == 0.0005


def test_missing_data_is_null_with_reason_never_zero():
    store = EvidenceStore()
    got = store.as_of("estimated_funding_rate", INST, T0, 60)
    assert got["status"] == "missing" and got["value"] is None and got["reason"]
    assert not usable(got)


def test_stale_evidence_is_not_usable():
    store = EvidenceStore()
    store.add(ev(0.0001, T0, T0))
    got = store.as_of("estimated_funding_rate", INST, T0 + timedelta(minutes=5), 60)
    assert got["status"] == "stale" and not usable(got)


def test_reorg_invalidates_dependent_evidence_but_preserves_history():
    store = EvidenceStore()
    chain = FixtureChainConnector(store)
    cutoff = utc(2026, 3, 1, 12)
    rec = chain.fetch("chain_large_transfer_count_1h", INST, cutoff, "c")[0]
    store.add(rec)
    height = rec["chain"]["block_height"]
    assert usable(store.as_of("chain_large_transfer_count_1h", INST, cutoff, 7200))
    chain.inject_reorg(height, cutoff + timedelta(minutes=1))
    after = store.as_of("chain_large_transfer_count_1h", INST, cutoff + timedelta(minutes=2), 7200)
    assert not usable(after)
    # what was known before the reorg is still reproducible for audit/replay
    assert usable(store.as_of("chain_large_transfer_count_1h", INST, cutoff, 7200))


def test_esplora_connector_with_recorded_responses_marks_forward_capture():
    responses = {"/blocks/tip/height": 900006, "/block-height/900000": "00" * 32,
                 f"/block/{'00' * 32}": {"timestamp": 1790000000, "tx_count": 3120}}
    conn = EsploraBlockConnector(http_get=responses.__getitem__)
    rec = conn.fetch("btc_finalized_block_tx_count", INST, utc(2026, 9, 30), "c")[0]
    assert rec["value"] == 3120 and rec["synthetic"] is False
    assert rec["chain"]["finality"] == "finalized" and "forward_capture_only" in rec["quality_flags"]


def test_completed_bars_never_include_unpublished_bar():
    series = synthetic_market(utc(2026, 1, 1), 0.1)["BTCUSDT"]
    bar = series.bars[10]
    assert series.completed(bar.end)[-1] is series.bars[9]  # bar 10 publishes one second after close
    assert series.completed(bar.available_at)[-1] is bar


def test_forecast_uses_only_matured_labels_and_causal_features():
    rng = np.random.default_rng(0)
    n, h = 400, 16
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    out = empirical_forecast(closes, closes, closes, np.ones(n), 1, h, 0.5, 0.0, 1.64)
    assert out["samples"] == n - h  # starts 0..n-1-h: every label window ends at or before the current bar
    params = {"vol_window_bars": 20, "trend_fast_bars": 5, "trend_slow_bars": 20, "trend_entry_z": 0.5,
              "funding_extreme_rate": "0.001"}
    shocked = closes.copy()
    shocked[-1] *= 3  # changing the latest bar must not change any earlier feature value
    f1, f2 = features(closes, np.zeros(n), params), features(shocked, np.zeros(n), params)
    assert np.array_equal(np.nan_to_num(f1["z"][:-1]), np.nan_to_num(f2["z"][:-1]))

"""Research-protocol mechanics: frozen manifest, immutable trial log, one-shot holdout, purged splits,
statistics sanity, leakage positive control, Monte Carlo layers, invariant monitor."""
import json
from datetime import timedelta

import numpy as np
import pytest

from cqc.research.leakage import causality_check, leaky_features_for_control
from cqc.research.manifest import HoldoutSeal, ProtocolViolation, TrialLog, freeze, load_verified
from cqc.research.metrics import bootstrap_ci, daily_stats, deflated_sharpe, effective_trades, pbo_cscv
from cqc.research.montecarlo import bootstrap_market, cost_mc, daily_bootstrap
from cqc.research.splits import assert_no_overlap, train_trades_before
from cqc import quant
from cqc.util import canonical_json, sha256_hex, utc


def test_manifest_freeze_detects_tampering_and_refuses_silent_changes(tmp_path):
    p = tmp_path / "m.json"
    m = freeze({"a": 1, "holdout": {"max_evaluations": 1}}, p)
    assert load_verified(p)["manifest_sha256"] == m["manifest_sha256"]
    with pytest.raises(ProtocolViolation):
        freeze({"a": 2, "holdout": {"max_evaluations": 1}}, p)  # a different manifest needs a new version
    doc = json.loads(p.read_text())
    doc["a"] = 99
    p.write_text(json.dumps(doc))
    with pytest.raises(ProtocolViolation):
        load_verified(p)


def test_trial_log_is_hash_chained_and_holdout_opens_once(tmp_path):
    log = TrialLog(tmp_path / "log.jsonl")
    m = freeze({"x": 1, "holdout": {"max_evaluations": 1}}, tmp_path / "m.json")
    cand = {"trial_id": "T00"}
    with pytest.raises(ProtocolViolation):
        HoldoutSeal(log, m).open_once(cand)  # no frozen candidate yet
    log.append({"kind": "trial", "trial_id": "T00"})
    log.append({"kind": "candidate_frozen", "candidate": cand, "candidate_sha256": sha256_hex(canonical_json(cand))})
    with pytest.raises(ProtocolViolation):
        HoldoutSeal(log, m).open_once({"trial_id": "T05"})  # not the frozen candidate
    HoldoutSeal(log, m).open_once(cand)
    with pytest.raises(ProtocolViolation):
        HoldoutSeal(log, m).open_once(cand)  # second look at the holdout is refused
    assert log.verify()
    lines = (tmp_path / "log.jsonl").read_text().splitlines()
    (tmp_path / "log.jsonl").write_text("\n".join(lines[:1] + lines[2:]) + "\n")  # delete a trial entry
    assert not log.verify()
    with pytest.raises(ProtocolViolation):
        log.append({"kind": "trial"})


def test_purge_excludes_labels_overlapping_the_test_window():
    t0 = utc(2026, 3, 10)
    trades = [{"opened": "2026-03-09T10:00:00Z", "closed": "2026-03-09T14:00:00Z"},
              {"opened": "2026-03-09T20:00:00Z", "closed": "2026-03-10T00:30:00Z"},  # overlaps test start
              {"opened": "2026-03-09T21:00:00Z", "closed": "2026-03-09T23:00:00Z"}]  # inside the 8 h embargo
    train = train_trades_before(trades, t0, 8 * 3600, utc(2026, 3, 1))
    assert [t["opened"] for t in train] == ["2026-03-09T10:00:00Z"]
    assert_no_overlap(train, t0, 8 * 3600)


def test_statistics_sanity():
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 10, 200)
    assert bootstrap_ci(noise)[0] < 0 < bootstrap_ci(noise)[1]
    st = daily_stats([10, -30, 5, 5, 20], 1000)
    assert st["max_drawdown"] == 30 and st["longest_under_water_days"] == 3
    # many noise trials: the best in-sample one should be deflated below 0.95
    trials = [rng.normal(0, 10, 60) for _ in range(30)]
    srs = [t.mean() / t.std(ddof=1) for t in trials]
    best = trials[int(np.argmax(srs))]
    assert deflated_sharpe(best, srs)["dsr"] < 0.95
    # PBO of pure-noise trials is high; of a truly dominant trial it is low
    noise_m = rng.normal(0, 1, (160, 10))
    assert pbo_cscv(noise_m)["pbo"] > 0.2
    good = noise_m.copy()
    good[:, 0] += 1.0
    assert pbo_cscv(good)["pbo"] < 0.1


def test_effective_trades_counts_only_non_overlapping():
    tr = [{"opened": "2026-01-01T00:00:00Z", "closed": "2026-01-01T04:00:00Z"},
          {"opened": "2026-01-01T01:00:00Z", "closed": "2026-01-01T05:00:00Z"},
          {"opened": "2026-01-01T06:00:00Z", "closed": "2026-01-01T07:00:00Z"}]
    assert effective_trades(tr, 14400) == 2


def test_leakage_checker_passes_registered_features_and_catches_positive_control():
    assert causality_check(quant.features)["causal"]
    assert not causality_check(leaky_features_for_control)["causal"]


def test_monte_carlo_layers_are_seeded_and_cost_mc_only_worsens():
    trades = [{"net_pnl_quote": "5", "entry_notional": 1000.0, "fees": 1.0, "funding_paid": 0.1, "exit_type": "stop"},
              {"net_pnl_quote": "-3", "entry_notional": 1000.0, "fees": 1.0, "funding_paid": -0.1, "exit_type": "market"}]
    a, b = cost_mc(trades, 500, seed=3), cost_mc(trades, 500, seed=3)
    assert a == b and a["total_q05_q50_q95"][2] <= a["base_total"] + 1e-9
    d = daily_bootstrap(np.random.default_rng(1).normal(0, 5, 30), 10000, reps=300)
    assert d["reps"] == 300 and 0 <= d["p_total_negative"] <= 1


def test_market_path_bootstrap_preserves_joint_days_and_continuity():
    from cqc.market import synthetic_market
    s = synthetic_market(utc(2026, 1, 1), 4, seed=3)
    p = bootstrap_market(s, utc(2026, 1, 1), utc(2026, 1, 4), 3, seed=5)
    btc, eth = p["BTCUSDT"].bars, p["ETHUSDT"].bars
    assert len(btc) == len(eth) == 3 * 1440
    jumps = [abs(btc[i].open / btc[i - 1].close - 1) for i in range(1, len(btc))]
    assert max(jumps) < 0.02 and all(b.high >= b.low > 0 for b in btc)
    assert all(b.start == e.start for b, e in zip(btc, eth))

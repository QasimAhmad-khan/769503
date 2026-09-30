"""research_v2 machinery: causal signals, screener timing/costs, controls, resampling, stress injection,
research-only Phi roles."""
import copy
from datetime import timedelta

import numpy as np
import pytest

from cqc.research import signals as S, vbt
from cqc.research.leakage import causality_check
from cqc.research.program_v2 import control_dirs, correlated_exec, resample_path
from cqc.research.stress import inject
from cqc.util import utc


def _ctx(n=900, seed=0):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.004, n)))
    o = np.r_[c[0], c[:-1]]
    return {"time": 1_780_000_000 + 900 * np.arange(1, n + 1, dtype=float), "open": o, "close": c,
            "high": np.maximum(o, c) * 1.001, "low": np.minimum(o, c) * 0.999, "volume": np.full(n, 1e4),
            "spread_bps": np.full(n, 2.0), "funding": rng.normal(0, 0.0003, n), "onchain": rng.poisson(40, n).astype(float)}


@pytest.mark.parametrize("name", sorted(S.REGISTRY))
def test_every_registered_hypothesis_is_causal(name):
    for p in S.grid(name):
        def fn(closes, funding, _params, p=p):
            n = len(closes)
            ctx = {"close": closes, "open": np.r_[closes[0], closes[:-1]], "high": closes * 1.002, "low": closes * 0.998,
                   "volume": np.ones(n), "spread_bps": 2 + np.abs(funding) * 1e4, "funding": funding,
                   "onchain": np.abs(funding) * 1e5}
            return {"dir": S.signals(name, ctx, p)}
        assert causality_check(fn, n=500, probes=10)["causal"], (name, p)


def test_screener_enters_after_the_signal_bar_and_charges_costs():
    ctx = {"X": _ctx()}
    d = np.zeros(900)
    d[400] = 1
    res = vbt.run(ctx, {"X": d}, 300, 900, gate=False)
    [t] = res["trades"]
    assert t["open_i"] == 401  # never fills on the bar whose close produced the signal
    lat = vbt.run(ctx, {"X": d}, 300, 900, gate=False, exec_params={"latency_bars": 2})
    assert lat["trades"][0]["open_i"] == 403
    free = vbt.run(ctx, {"X": d}, 300, 900, gate=False, exec_params={"fee_rate": 0, "slip_bps": 0, "spread_mult": 0,
                                                                        "stop_gap_bps": 0})
    assert free["trades"][0]["net"] > t["net"]


def test_screener_future_corruption_does_not_change_the_past():
    ctx = _ctx()
    d = S.signals("trend_breakout_v1", ctx, S.grid("trend_breakout_v1")[0])
    r1 = vbt.run({"X": ctx}, {"X": d}, 200, 900, gate=False)
    bad = {k: (np.r_[v[:600], v[600:] * 2] if k in ("open", "high", "low", "close") else v) for k, v in ctx.items()}
    d2 = S.signals("trend_breakout_v1", bad, S.grid("trend_breakout_v1")[0])
    r2 = vbt.run({"X": bad}, {"X": d2}, 200, 900, gate=False)
    assert np.allclose(r1["equity"][:398], r2["equity"][:398])


def test_negative_controls_preserve_exposure():
    d = {"X": np.r_[np.ones(300), np.zeros(600), -np.ones(60)]}
    for kind in ("block_randomized", "time_shifted"):
        c = control_dirs(d, kind, 3)["X"]
        assert (c != 0).sum() == (d["X"] != 0).sum() and not np.array_equal(c, d["X"])


def test_block_resampling_keeps_symbols_jointly_aligned():
    import cqc.research.program_v2 as P
    base = {"A": _ctx(20000, 1), "B": _ctx(20000, 2)}
    old = (P.PERIODS, P.MC)
    P.PERIODS = dict(P.PERIODS, train=[10, 100], dev=[100, 180])
    P.MC = dict(P.MC, path_days=30)
    try:
        for scheme in ("moving", "stationary"):
            p = resample_path(base, 5, scheme, 96)
            assert len(p["A"]["close"]) == len(p["B"]["close"]) == 30 * 96
            assert (p["A"]["high"] >= p["A"]["low"]).all() and (p["A"]["close"] > 0).all()
    finally:
        P.PERIODS, P.MC = old


def test_correlated_execution_draws_move_together():
    rng = np.random.default_rng(0)
    draws = [correlated_exec(rng, 0.6) for _ in range(2000)]
    st = np.array([d["_stress"] for d in draws])
    assert np.corrcoef(st, [d["slip_bps"] for d in draws])[0, 1] > 0.3
    assert np.corrcoef(st, [d["spread_mult"] for d in draws])[0, 1] > 0.3
    assert all(0.5 <= d["fill_prob"] <= 1 and 0 <= d["latency_bars"] <= 4 for d in draws)


def test_gap_stress_opens_at_the_gapped_price():
    from cqc.market import synthetic_market
    s = synthetic_market(utc(2026, 1, 1), 0.1, seed=1)
    t0 = s["BTCUSDT"].bars[50].start
    out = inject(s, {"kind": "gap", "move": -0.25, "spread_mult": 20, "volume_mult": 0.05, "minutes": 30}, t0.isoformat())
    before, after = s["BTCUSDT"].bars[50], out["BTCUSDT"].bars[50]
    assert abs(after.open / before.open - 0.75) < 1e-9 and out["BTCUSDT"].bars[49].close == s["BTCUSDT"].bars[49].close
    drop = inject(s, {"kind": "drop", "minutes": 20}, t0.isoformat())
    assert len(drop["BTCUSDT"].bars) == len(s["BTCUSDT"].bars) - 20


def test_research_roles_use_the_same_phi_and_only_registered_templates():
    from cqc.config import load_config
    from cqc.llm.phi import FakePhiBackend, PhiService
    svc = PhiService(load_config(), FakePhiBackend())
    menu = [{"template": k, "family": v["family"], "data": v["data"], "mechanism": v["mechanism"]} for k, v in S.REGISTRY.items()]
    prop = svc.run("hypothesis_proposer", {"menu": menu, "available_data": ["ohlc", "funding", "spread"]},
                   now=utc(2026, 1, 1), correlation_id="t", allowed_ids=list(S.REGISTRY))
    assert {p["template"] for p in prop["proposals"]} <= set(S.REGISTRY)
    assert "onchain_activity_trend_v1" not in {p["template"] for p in prop["proposals"]}  # data not available
    assert prop["producer"].startswith("phi:hypothesis_proposer:fake_phi_rules_v2")
    assert svc.queue.PRIORITY["hypothesis_proposer"] > svc.queue.PRIORITY["screener"]  # research never preempts trading

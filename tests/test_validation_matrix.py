"""Portability, data hygiene, execution perturbations, negative controls, invariants and the Phi bench."""
import builtins
import copy
import importlib
import json
import sys
from datetime import timedelta

import pytest

from cqc import faults
from cqc.market import synthetic_market
from cqc.research.invariants import InvariantMonitor
from cqc.util import D, utc


def test_cli_imports_and_runs_help_without_unix_resource_module(monkeypatch, capsys):
    real_import = builtins.__import__

    def no_resource(name, *a, **k):
        if name == "resource":
            raise ModuleNotFoundError("No module named 'resource'")  # what Windows raises
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_resource)
    monkeypatch.delitem(sys.modules, "resource", raising=False)
    import cqc.__main__ as cli
    cli = importlib.reload(cli)
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0 and "validate" in capsys.readouterr().out
    assert cli._rss_mib() is None or cli._rss_mib() > 0  # degrades to None instead of crashing


def test_bar_ingestion_rejects_duplicates_out_of_order_and_bad_ticks():
    src = synthetic_market(utc(2026, 1, 1), 0.05, seed=4)["BTCUSDT"].bars
    from cqc.market import BarSeries
    s = BarSeries("BTCUSDT")
    assert [s.ingest(b) for b in src[:3]] == ["accepted"] * 3
    assert s.ingest(src[2]) == "duplicate"
    changed = copy.copy(src[2])
    changed.close *= 1.001
    changed.high = max(changed.high, changed.close)
    assert s.ingest(changed) == "conflicting_redelivery"
    assert s.ingest(src[1]) == "out_of_order"
    spike = copy.copy(src[3])
    spike.close = spike.high = spike.close * 2
    assert s.ingest(spike) == "bad_tick"
    crossed = copy.copy(src[3])
    crossed.bid, crossed.ask = crossed.ask, crossed.bid * 0.99
    assert s.ingest(crossed) == "bad_tick"
    assert s.ingest(src[3]) == "accepted" and len(s.bars) == 4


def test_execution_scenarios_worsen_costs_and_are_seeded():
    base = faults.make_runtime()
    faults.run_to_entry(base, 30)
    stressed = faults.make_runtime()
    stressed.venue.apply_scenario({"name": "x", "fee_mult": 2.0, "extra_slippage_bps": 8, "seed": 1})
    faults.run_to_entry(stressed, 30)
    fb, fs = base.ledger.fills(), stressed.ledger.fills()
    assert fb and fs
    assert D(fs[0]["fee"]) > D(fb[0]["fee"]) and D(fs[0]["price"]) > D(fb[0]["price"])  # buy filled worse
    rej = faults.make_runtime()
    rej.venue.apply_scenario({"name": "r", "reject_prob": 1.0, "seed": 2})
    faults.run_to_entry(rej, 5)
    assert not rej.ledger.fills() and rej.ledger.events("order_terminal")


def test_negative_controls_change_the_signal():
    from cqc.runtime import PaperRuntime
    import numpy as np
    bars = synthetic_market(utc(2026, 1, 1), 1, seed=2)["BTCUSDT"].bars[::15]
    feats = {"trend_dir": np.ones(len(bars)), "funding_dir": np.zeros(len(bars))}
    rnd = PaperRuntime._negative_control(feats, "random_signal", "BTCUSDT", bars)
    assert set(np.unique(rnd["trend_dir"])) <= {-1.0, 0.0, 1.0} and len(np.unique(rnd["trend_dir"])) > 1
    rnd2 = PaperRuntime._negative_control(feats, "random_signal", "BTCUSDT", bars)
    assert (rnd["trend_dir"] == rnd2["trend_dir"]).all()  # deterministic, market-independent
    dly = PaperRuntime._negative_control({"trend_dir": np.arange(10.0), "funding_dir": np.zeros(10)}, "delay_signal:3",
                                         "BTCUSDT", bars[:10])
    assert list(dly["trend_dir"][:4]) == [0, 0, 0, 0] and dly["trend_dir"][5] == 2


def test_invariant_monitor_passes_normal_run_and_flags_a_violation():
    rt = faults.make_runtime()
    mon = InvariantMonitor(rt)
    t, _ = faults.first_entry_time()
    rt.run((t - timedelta(hours=1)).replace(second=0), (t + timedelta(hours=5)).replace(second=0), on_minute=mon)
    rep = mon.finalize()
    assert rep["passed"] and rep["minutes_checked"] > 300
    # an entry acknowledged while the recorded state was not NORMAL must be flagged
    ack = next(e for e in rt.ledger.events("order_ack") if rt.ledger.intent(e["payload"]["intent_id"])["purpose"] == "entry")
    from cqc.util import parse_ts
    mon.state_at[parse_ts(ack["ts"]).replace(second=0).isoformat()] = "NO_NEW_RISK"
    assert any(v["invariant"] == "ENTRY_SENT_OUTSIDE_NORMAL" for v in mon.finalize()["violations"])


def test_phi_bench_against_stub_server(tmp_path):
    from tests.test_model_adapters import _Stub, _plans
    from http.server import ThreadingHTTPServer
    import threading
    from cqc.config import load_config
    from cqc.research import phi_bench
    servers = []
    urls = {}
    for name in ("reference", "q4"):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        urls[name] = f"http://127.0.0.1:{srv.server_port}/v1/chat/completions"
    _Stub.seen, _Stub.decision, _Stub.fail, _Stub.delay = [], None, False, 0.0
    rep = phi_bench.run(load_config(), urls, [_plans(), _plans()[:1]], repeats=2)
    for s in servers:
        s.shutdown()
    assert rep["decision_agreement"]["agreement_fraction"] == 1.0
    for ep in rep["endpoints"].values():
        assert ep["schema_validity"]["decision_maker"] == "2/2" and ep["schema_validity"]["risk_analyst"] == "2/2"
        assert ep["summary"]["tokens_in_total"] > 0

"""Fault-injection scenarios (shared by `python -m cqc faults` and the test suite).

Each scenario replays the synthetic fixture up to a decision bar where the baseline policy
would authorize an entry, injects one fault, and records what actually happened.
"""
from __future__ import annotations

import time
from datetime import timedelta
from functools import lru_cache

from .config import load_config
from .ledger import Ledger, StaleFencingToken
from .llm.phi import FakePhiBackend
from .market import instruments_from_config, synthetic_market
from .runtime import PaperRuntime
from .util import D, parse_ts, utc
from . import policy

START = utc(2026, 1, 1)
DAYS = 30


@lru_cache(maxsize=4)
def fixture(days: int = DAYS, seed: int = 7):
    return synthetic_market(START, days, seed=seed)


def make_runtime(cfg=None, *, series=None, ledger=None, venue=None, phi_mode="ok", decision_mode=None, **kw):
    from .venue import SimulatedVenue
    cfg = cfg or load_config()
    series = series or fixture()
    if venue is None:
        venue = SimulatedVenue(instruments_from_config(cfg), cfg["venue_sim"], cfg["paper_risk"]["starting_equity_usdt"],
                               cfg["paper_risk"]["max_venue_leverage_setting"])
    phi = kw.pop("phi_backend", None) or FakePhiBackend()
    phi.mode = phi_mode
    if decision_mode:
        phi.role_modes["decision_maker"] = decision_mode
    return PaperRuntime(cfg, series, venue=venue, ledger=ledger, phi_backend=phi, **kw)


@lru_cache(maxsize=4)
def first_entry_time(search_start_day: int = 14, search_days: int = 12):
    """Decision time of the first authorized entry in the baseline replay (fresh account)."""
    rt = make_runtime()
    start = START + timedelta(days=search_start_day)
    found = {}

    def stop_when_found(runtime, t):
        if not found:
            for c in runtime.cycle_log:
                if c["status"] == "authorized":
                    found["t"] = parse_ts(c["ts"])
                    found["symbol"] = c["symbol"]
                    raise StopIteration
    try:
        rt.run(start, start + timedelta(days=search_days), on_minute=stop_when_found)
    except StopIteration:
        pass
    if not found:
        raise RuntimeError("baseline replay produced no entry in the search window")
    return found["t"], found["symbol"]


def run_to_entry(rt, extra_minutes: int = 0):
    """Replay from 45 minutes before the baseline entry decision through it (plus extra minutes)."""
    t, _sym = first_entry_time()
    decision_bar = t - timedelta(seconds=2)
    rt.run(decision_bar - timedelta(minutes=45), decision_bar + timedelta(minutes=extra_minutes))
    return decision_bar


def entries(rt):
    return rt.ledger.intents(purpose="entry")


def _result(name, injected, expected, observed, passed):
    return {"scenario": name, "injected": injected, "expected": expected, "observed": observed, "passed": bool(passed)}


# ------------------------------------------------------------------ scenarios
def baseline():
    rt = make_runtime()
    run_to_entry(rt, 5)
    n = len(entries(rt))
    return _result("baseline", "none", "one authorized entry", f"{n} entry intents", n == 1)


def decision_fault(mode):
    """Fault only in the Phi decision_maker role (screener/analyzer healthy)."""
    rt = make_runtime(decision_mode=mode)
    run_to_entry(rt, 5)
    n = len(entries(rt))
    records = [e["payload"] for e in rt.ledger.events("decision_record")]
    statuses = sorted({r["validation_status"] for r in records})
    return _result(f"phi_decision_{mode}", f"Phi decision_maker mode={mode}",
                   "no entry intent; decision record ABSTAIN; no probability fabricated",
                   f"entries={n} decision_status={statuses} locks={sorted(rt.risk.locks)}",
                   n == 0 and records and all(r["selected_id"] == "ABSTAIN" and r["calibrated_selection_probability"]
                                              is None for r in records))


def phi_fault(mode):
    rt = make_runtime(phi_mode=mode)
    run_to_entry(rt, 5)
    n = len(entries(rt))
    blocked = [c for c in rt.cycle_log if c["status"] == "blocked" or "SCREENER_REQUESTED_NON_WHITELISTED_SOURCE" in c["reasons"]]
    return _result(f"phi_{mode}", f"Phi backend mode={mode}", "no entry intent; cycle blocked/abstained",
                   f"entries={n} blocked_cycles={len(blocked)} locks={sorted(rt.risk.locks)}", n == 0 and blocked)


def phi_oom_keeps_protection():
    """Open a position normally, then OOM the Phi service: protection (stops, time exit) must continue."""
    rt = make_runtime()
    decision_bar = run_to_entry(rt, 5)
    rt.phi.backend.mode = "oom"
    t = decision_bar + timedelta(minutes=5)
    rt.run(t, t + timedelta(hours=5))
    prot = rt.ledger.events("protective_action")
    flat = all(p.contracts == 0 for p in rt.executor.book.values())
    return _result("phi_oom_after_entry", "Phi OOM while a position is open",
                   "protective stop placed and position exited by deterministic rules without Phi",
                   f"protective_actions={len(prot)} flat_after_5h={flat} new_entries={len(entries(rt)) - 1}",
                   len(prot) >= 1 and flat and len(entries(rt)) == 1)


def audit_failure():
    rt = make_runtime()
    t, _ = first_entry_time()
    decision_bar = t - timedelta(seconds=2)
    rt.run(decision_bar - timedelta(minutes=45), decision_bar - timedelta(minutes=1))
    rt.ledger.fail_writes = 3  # the next audit writes fail (decision persistence)
    rt.run(decision_bar - timedelta(minutes=1), decision_bar + timedelta(minutes=3))
    n = len(entries(rt))
    return _result("audit_persistence_failure", "3 failed audit writes at the decision bar",
                   "no entry; NO_NEW_RISK(audit_failure)", f"entries={n} locks={sorted(rt.risk.locks)}", n == 0)


def ack_loss_and_restart(db_path=":memory:"):
    """Entry ack is lost (order exists at venue). Worker 'crashes'; a new worker takes ownership,
    reconciles by client order ID, and must not duplicate the order. The old worker is fenced."""
    ledger = Ledger(db_path)
    rt = make_runtime(ledger=ledger)
    rt.venue.drop_next_ack = 1
    decision_bar = run_to_entry(rt, 0)
    lost = [i for i in entries(rt) if i["status"] == "unknown"]
    rt2 = make_runtime(ledger=ledger, venue=rt.venue, owner="worker-2")
    rt2.run(decision_bar, decision_bar + timedelta(minutes=10))
    venue_orders = [o for o in rt.venue.orders.values() if o.client_order_id.startswith("cqc")]
    fenced = False
    try:
        rt.executor.dispatch(decision_bar + timedelta(minutes=11), rt._admission)
        rt.ledger.update_intent(entries(rt)[0]["intent_id"], rt.executor.token, decision_bar, status="queued")
    except StaleFencingToken:
        fenced = True
    recovered = [e for e in ledger.events("recovery_complete")]
    st = entries(rt2)[0]["status"] if entries(rt2) else None
    return _result("ack_loss_restart", "ack dropped after venue accepted the entry; worker restart",
                   "one venue order only; intent reconciled; recovery completes; stale worker fenced",
                   f"unknown_before_restart={len(lost)} venue_entry_orders={len(venue_orders)} intent_status={st} "
                   f"recoveries={len(recovered)} stale_worker_fenced={fenced}",
                   len(lost) == 1 and len(venue_orders) == 1 and st in ("filled", "partially_filled", "acknowledged")
                   and fenced and len(recovered) >= 2)


def duplicate_fills():
    rt = make_runtime()
    rt.venue.duplicate_fill_delivery = True
    run_to_entry(rt, 30)
    venue_fills = len(rt.venue.fill_log)
    ledger_fills = len(rt.ledger.fills())
    return _result("duplicate_fill_delivery", "every fill batch re-delivers its first fill",
                   "fills deduplicated by venue fill ID; local position equals venue",
                   f"venue_fills={venue_fills} ledger_fills={ledger_fills} discrepancies={rt.executor.compare_positions()}",
                   venue_fills == ledger_fills and not rt.executor.compare_positions())


def missing_required_evidence():
    rt = make_runtime()
    rt.connectors.connectors.pop("sim_venue_market_data")  # required market-context source unavailable
    run_to_entry(rt, 5)
    missing = [c for c in rt.cycle_log if "REQUIRED_EVIDENCE_MISSING" in c["reasons"]]
    return _result("missing_required_evidence", "market-context connector removed",
                   "abstain with REQUIRED_EVIDENCE_MISSING; never zero-filled", f"entries={len(entries(rt))} "
                   f"missing_cycles={len(missing)}", len(entries(rt)) == 0 and missing)


def operator_halt():
    rt = make_runtime()
    t, _ = first_entry_time()
    rt.risk.operator_halt(t - timedelta(hours=1))
    run_to_entry(rt, 5)
    cleared_by_model = rt.risk.clear_lock("operator_halt", t)  # non-operator clear must fail
    return _result("operator_halt", "operator kill switch before the entry bar",
                   "no entries; lock not clearable without operator", f"entries={len(entries(rt))} "
                   f"state={rt.risk.state} non_operator_clear={cleared_by_model}",
                   len(entries(rt)) == 0 and rt.risk.state == "HALTED" and not cleared_by_model)


def unsupported_route():
    cfg = load_config()
    plan = {"risk_increasing": True, "candidate_id": "c1", "plan_sha256": "a" * 64}
    decision = {"kind": "decision_record", "selector": "phi_decision_maker", "backend_id": "B", "validation_status": "valid",
                "selected_id": "c1", "candidate_plan_hashes": {"c1": "a" * 64}, "expires_at": "2099-01-01T00:00:00Z"}
    now = utc(2026, 1, 1)
    chk = lambda route="autonomous_cycle", d=decision, sel="phi_decision_maker", be="B": policy.check_entry(  # noqa: E731
        cfg=cfg, route=route, strategy="trend_breakout_v1", plan=plan, decision=d, audit_ok=True, now=now,
        expected_selector=sel, expected_backend_id=be)[0]
    routes = {r: chk(r) for r in ("autonomous_cycle", "legacy_ai_filter", "manual_signal", "grid_strategy")}
    other_model = chk(d={**decision, "backend_id": "remote|other-model@x"})
    selector_fallback = chk(d={**decision, "selector": "deterministic_rank_v1"})
    legacy = chk(d={**decision, "kind": "decision_receipt", "provider": "open_jev"})
    return _result("alternate_entry_routes_and_fallbacks",
                   "legacy/manual/grid routes, another model backend, a selector fallback, a legacy Jev receipt",
                   "only autonomous_cycle with the configured Phi backend + selector passes",
                   f"routes={routes} other_model={other_model} selector_fallback={selector_fallback} legacy_jev={legacy}",
                   routes == {"autonomous_cycle": True, "legacy_ai_filter": False, "manual_signal": False,
                              "grid_strategy": False} and not (other_model or selector_fallback or legacy))


def authorize_without_dispatch(rt):
    """Replay to the baseline decision bar and run the decision cycles, stopping before dispatch."""
    t, sym = first_entry_time()
    decision_bar = t - timedelta(seconds=2)
    rt.run(decision_bar - timedelta(minutes=45), decision_bar - timedelta(minutes=1))
    with rt.account_lock:
        for s in rt.series:
            for bar in rt.series[s].bars:
                if decision_bar - timedelta(minutes=1) < bar.end <= decision_bar:
                    rt.venue.step(bar)
    rt.tick(decision_bar + timedelta(seconds=1))
    for s in rt.instruments:
        rt.decision_cycle(s, decision_bar + timedelta(seconds=2))
    return decision_bar, sym


def admission_account_changed():
    rt = make_runtime()
    decision_bar, sym = authorize_without_dispatch(rt)
    queued = rt.ledger.intents(statuses=["queued"], purpose="entry")
    rt.venue.positions[sym].realized_pnl -= D("40")  # e.g. an external fee/funding debit lands after authorization
    rt.dispatch(decision_bar + timedelta(seconds=3))
    rej = [e["payload"] for e in rt.ledger.events("admission_rejected")]
    sent = [o for o in rt.venue.orders.values() if o.client_order_id.startswith("cqc")]
    return _result("admission_account_changed", "wallet changed between authorization and dispatch",
                   "entry rejected at dispatch with ACCOUNT_VERSION_CHANGED / EQUITY_CHANGED; nothing sent",
                   f"queued={len(queued)} rejections={[r['reasons'] for r in rej]} venue_orders={len(sent)}",
                   len(queued) == 1 and len(sent) == 0 and rej and "ACCOUNT_VERSION_CHANGED" in rej[0]["reasons"])


def admission_price_collar():
    import copy
    rt = make_runtime()
    decision_bar, sym = authorize_without_dispatch(rt)
    bar = copy.copy(rt.venue.last_bar[sym])
    bar.ask, bar.bid = bar.ask * 1.02, bar.bid * 1.02  # market gaps 2% (envelope is 15 bps); mark unchanged
    rt.venue.last_bar[sym] = bar
    rt.dispatch(decision_bar + timedelta(seconds=3))
    rej = [e["payload"] for e in rt.ledger.events("admission_rejected")]
    sent = [o for o in rt.venue.orders.values() if o.client_order_id.startswith("cqc")]
    return _result("admission_price_collar", "executable quote moved 2% after authorization",
                   "entry rejected at dispatch with PRICE_OUTSIDE_COLLAR; nothing sent",
                   f"rejections={[r['reasons'] for r in rej]} venue_orders={len(sent)}",
                   len(sent) == 0 and rej and "PRICE_OUTSIDE_COLLAR" in rej[0]["reasons"])


def slow_phi_does_not_delay_protection():
    """A decision cycle blocks in a slow Phi call (2 s) in one thread while an emergency (mark near
    liquidation) hits; the protection thread must place a reduce-only exit without waiting."""
    import copy
    import threading
    from .protection import ProtectionLoop
    rt = make_runtime()
    decision_bar = run_to_entry(rt, 5)
    sym = next(s for s, p in rt.executor.book.items() if p.contracts != 0)
    rt.phi.backend.mode, rt.phi.backend.slow_seconds = "slow", 2.0
    now = decision_bar + timedelta(minutes=5, seconds=1)
    loop = ProtectionLoop(rt, interval_s=0.2, clock=lambda: now)
    loop.start()
    other = next(s for s in rt.instruments if s != sym)
    worker = threading.Thread(target=rt.decision_cycle, args=(other, decision_bar + timedelta(minutes=15, seconds=2)))
    worker.start()
    time.sleep(0.3)  # the decision thread is now inside the slow Phi call
    liq = rt.venue.account()["positions"][sym]["liquidation_price"]
    bar = copy.copy(rt.venue.last_bar[sym])
    bar.mark = float(liq) * 1.01
    rt.venue.last_bar[sym] = bar
    t_shock = time.perf_counter()
    loop.notify("mark_shock")
    deadline = time.perf_counter() + 1.5
    exit_intent = None
    while time.perf_counter() < deadline and exit_intent is None:
        exit_intent = next((i for i in rt.ledger.intents(purpose="protective", symbol=sym) if i["order_type"] == "market"),
                           None)
        time.sleep(0.01)
    reaction_ms = (time.perf_counter() - t_shock) * 1000
    phi_busy = worker.is_alive()
    loop.stop()
    worker.join(10)
    loop.join(2)
    return _result("slow_phi_does_not_delay_protection", "2 s Phi call in flight + mark near liquidation",
                   "reduce-only protective order placed while Phi is still busy",
                   f"exit_placed={exit_intent is not None} reaction_ms={reaction_ms:.1f} phi_still_busy={phi_busy} "
                   f"loop_errors={loop.errors}", exit_intent is not None and phi_busy and not loop.errors)


def legacy_jev_ledger():
    import json as _json
    from pathlib import Path
    from .llm.phi import RecordedPhiBackend
    ledger = Ledger()
    rec = next(r for r in _json.loads((Path(__file__).resolve().parent.parent / "04_EXAMPLES.json").read_text())["records"]
               if r["kind"] == "decision_receipt")
    ledger.append("decision_receipt", rec, utc(2026, 1, 1), "legacy")
    ledger.append("jev_raw_response", {"request_sha256": "0" * 64, "response_text": "{}"}, utc(2026, 1, 1), "legacy")
    replay = RecordedPhiBackend.from_ledger(ledger)
    rt = make_runtime(ledger=ledger)
    t, _ = first_entry_time()
    rt.start(t - timedelta(hours=1))
    detected = [e["payload"] for e in ledger.events("legacy_records_detected")]
    plan = {"risk_increasing": True, "candidate_id": rec["selected_id"], "plan_sha256": "a" * 64}
    ok = policy.check_entry(cfg=load_config(), route="autonomous_cycle", strategy="trend_breakout_v1", plan=plan,
                            decision=rec, audit_ok=True, now=utc(2026, 1, 1), expected_selector="phi_decision_maker",
                            expected_backend_id=rt.expected_backend_id)[0]
    return _result("legacy_jev_ledger", "ledger containing Open-Jev-era decision_receipt/jev_raw_response records",
                   "records identified as legacy, excluded from Phi replay, rejected by the entry gate",
                   f"detected={detected[0]['counts'] if detected else None} replay_recordings={len(replay.recordings)} "
                   f"gate_accepts_legacy={ok}", bool(detected) and not replay.recordings and not ok)


def four_roles_one_backend():
    rt = make_runtime()
    decision_bar = run_to_entry(rt, 5)
    rt._risk_analyst(decision_bar + timedelta(minutes=5, seconds=1), "demo_material_change")
    by_role = rt.phi.stats["by_role"]
    producers = {e["payload"]["producer"].split(":", 2)[2] for k in ("evidence_request", "fetch_plan", "analysis_packet",
                                                                   "adjustment_proposal") for e in rt.ledger.events(k)}
    decisions = {(e["payload"]["model_id"], e["payload"]["model_revision"], e["payload"]["backend_id"])
                 for e in rt.ledger.events("decision_record")}
    ident = rt.phi.identity
    same = producers == {f"{ident['model_id']}@{ident['revision']}"} and decisions == {
        (ident["model_id"], ident["revision"], rt.phi.backend_id)}
    return _result("four_roles_one_backend", "normal run + one risk review",
                   "screener, analyzer, decision_maker, risk_analyst all served by the same backend/model revision; "
                   "max concurrent inference 1",
                   f"calls_by_role={by_role} producers={sorted(producers)} decision_backends={len(decisions)} "
                   f"max_concurrency={rt.phi.queue.max_active_seen}",
                   all(by_role[r] > 0 for r in by_role) and same and rt.phi.queue.max_active_seen == 1)


def feed_loss_blocks_entries():
    """Stop delivering bars for both symbols before the entry bar: stale quotes must block new risk."""
    rt = make_runtime()
    t, _ = first_entry_time()
    decision_bar = t - timedelta(seconds=2)
    rt.run(decision_bar - timedelta(minutes=45), decision_bar - timedelta(minutes=10))
    frozen = {s: rt.series[s] for s in rt.series}
    from .market import BarSeries
    cut = decision_bar - timedelta(minutes=10)
    rt.series = {s: BarSeries(s, [b for b in frozen[s].bars if b.end <= cut],
                              [b.available_at for b in frozen[s].bars if b.end <= cut]) for s in frozen}
    rt.run(decision_bar - timedelta(minutes=10), decision_bar + timedelta(minutes=5))
    return _result("feed_loss", "market feed stops 10 minutes before the entry bar",
                   "stale_feed lock (NO_NEW_RISK); no entry", f"entries={len(entries(rt))} locks={sorted(rt.risk.locks)}",
                   len(entries(rt)) == 0 and "stale_feed" in rt.risk.locks)


def ledger_corruption_fails_closed():
    import os
    import tempfile
    from .ledger import LedgerCorrupted
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.write(fd, b"this is not a sqlite database" * 100)
    os.close(fd)
    try:
        Ledger(path)
        refused = False
    except LedgerCorrupted:
        refused = True
    finally:
        os.unlink(path)
    return _result("ledger_corruption", "ledger file overwritten with garbage", "worker refuses to start (LedgerCorrupted)",
                   f"refused={refused}", refused)


def venue_disconnect_around_entry():
    rt = make_runtime()
    decision_bar, sym = authorize_without_dispatch(rt)
    rt.venue.down = True
    rt.dispatch(decision_bar + timedelta(seconds=3))
    unknown = [i for i in rt.ledger.intents(purpose="entry") if i["status"] == "unknown"]
    rt.venue.down = False
    rt.protect(decision_bar + timedelta(minutes=1))
    after = rt.ledger.intents(purpose="entry")
    sent = [o for o in rt.venue.orders.values() if o.client_order_id.startswith("cqc")]
    return _result("venue_disconnect", "venue unreachable at dispatch, restored a minute later",
                   "entry marked unknown (reservation kept), reconciled without duplicate orders",
                   f"unknown_during_outage={len(unknown)} statuses_after={[i['status'] for i in after]} venue_orders={len(sent)}",
                   len(unknown) == 1 and len(sent) <= 1 and all(i["status"] != "unknown" for i in after))


def delisting_closes_and_blocks():
    rt = make_runtime()
    decision_bar = run_to_entry(rt, 5)
    sym = next(s for s, p in rt.executor.book.items() if p.contracts != 0)
    rt.delist(sym, decision_bar + timedelta(minutes=10))
    rt.run(decision_bar + timedelta(minutes=5), decision_bar + timedelta(hours=2))
    closes = [e["payload"] for e in rt.ledger.events("protective_action") if e["payload"]["reason"] == "instrument_delisted"]
    later = [c for c in rt.cycle_log if c["symbol"] == sym and "INSTRUMENT_DELISTED" in c["reasons"]]
    return _result("delisting", "instrument delisted while a position is open",
                   "position closed by protection; no new entries for the symbol",
                   f"flat={rt.executor.book[sym].contracts == 0} delist_closes={len(closes)} blocked_cycles={len(later)}",
                   rt.executor.book[sym].contracts == 0 and closes and later)


ALL = [baseline, lambda: decision_fault("timeout"), lambda: decision_fault("oom"), lambda: decision_fault("malformed"),
       lambda: decision_fault("invent_candidate"), lambda: decision_fault("schema_violation"),
       lambda: decision_fault("abstain"),
       lambda: phi_fault("malformed"), lambda: phi_fault("timeout"), lambda: phi_fault("oom"),
       lambda: phi_fault("schema_violation"), lambda: phi_fault("injected"), phi_oom_keeps_protection, audit_failure,
       ack_loss_and_restart, duplicate_fills, missing_required_evidence, operator_halt, unsupported_route,
       admission_account_changed, admission_price_collar, slow_phi_does_not_delay_protection, legacy_jev_ledger,
       four_roles_one_backend, feed_loss_blocks_entries, ledger_corruption_fails_closed, venue_disconnect_around_entry,
       delisting_closes_and_blocks]


def run_all() -> list[dict]:
    out = []
    for fn in ALL:
        try:
            out.append(fn())
        except Exception as exc:  # a crashing scenario is a failed scenario, reported honestly
            out.append(_result(getattr(fn, "__name__", "scenario"), "?", "?", f"EXCEPTION {type(exc).__name__}: {exc}", False))
    return out

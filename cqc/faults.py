"""Fault-injection scenarios (shared by `python -m cqc faults` and the test suite).

Each scenario replays the synthetic fixture up to a decision bar where the baseline policy
would authorize an entry, injects one fault, and records what actually happened.
"""
from __future__ import annotations

from datetime import timedelta
from functools import lru_cache

from .config import load_config
from .ledger import Ledger, StaleFencingToken
from .llm.jev import FakeJevTransport
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


def make_runtime(cfg=None, *, series=None, ledger=None, venue=None, phi_mode="ok", jev_mode="max_lcb", **kw):
    from .venue import SimulatedVenue
    cfg = cfg or load_config()
    series = series or fixture()
    if venue is None:
        venue = SimulatedVenue(instruments_from_config(cfg), cfg["venue_sim"], cfg["paper_risk"]["starting_equity_usdt"],
                               cfg["paper_risk"]["max_venue_leverage_setting"])
    phi = kw.pop("phi_backend", None) or FakePhiBackend()
    phi.mode = phi_mode
    jev = kw.pop("jev_transport", None) or FakeJevTransport(jev_mode)
    return PaperRuntime(cfg, series, venue=venue, ledger=ledger, phi_backend=phi, jev_transport=jev, **kw)


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


def jev_fault(mode):
    rt = make_runtime(jev_mode=mode)
    run_to_entry(rt, 5)
    n = len(entries(rt))
    receipts = [e["payload"] for e in rt.ledger.events("decision_receipt")]
    statuses = sorted({r["validation_status"] for r in receipts})
    return _result(f"jev_{mode}", f"Jev transport mode={mode}", "no entry intent; receipt ABSTAIN; NO_NEW_RISK lock",
                   f"entries={n} receipt_status={statuses} locks={sorted(rt.risk.locks)}",
                   n == 0 and all(r["selected_id"] == "ABSTAIN" for r in receipts))


def phi_fault(mode):
    rt = make_runtime(phi_mode=mode)
    run_to_entry(rt, 5)
    n = len(entries(rt))
    blocked = [c for c in rt.cycle_log if c["status"] == "blocked" or "SCREENER_TOOL_NOT_ALLOWLISTED" in c["reasons"]]
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
        rt.executor.dispatch(decision_bar + timedelta(minutes=11), rt.risk.submission_check)
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
    plan = {"risk_increasing": True, "candidate_id": "c1"}
    receipt = {"provider": "fake_jev_not_a_model", "validation_status": "valid", "selected_id": "c1",
               "expires_at": "2099-01-01T00:00:00Z"}
    now = utc(2026, 1, 1)
    results = {route: policy.check_entry(cfg=cfg, route=route, strategy="trend_breakout_v1", plan=plan, receipt=receipt,
                                         audit_ok=True, now=now, expected_provider="fake_jev_not_a_model")[0]
               for route in ("autonomous_cycle", "legacy_ai_filter", "manual_signal", "grid_strategy")}
    fallback = policy.check_entry(cfg=cfg, route="autonomous_cycle", strategy="trend_breakout_v1", plan=plan,
                                  receipt={**receipt, "provider": "openai_fallback"}, audit_ok=True, now=now,
                                  expected_provider="fake_jev_not_a_model")[0]
    return _result("alternate_entry_routes", "legacy/manual/grid routes and an LLM-fallback provider",
                   "only autonomous_cycle with the configured provider passes",
                   f"routes={results} llm_fallback_allowed={fallback}",
                   results == {"autonomous_cycle": True, "legacy_ai_filter": False, "manual_signal": False,
                               "grid_strategy": False} and not fallback)


ALL = [baseline, lambda: jev_fault("timeout"), lambda: jev_fault("unavailable"), lambda: jev_fault("malformed"),
       lambda: jev_fault("bad_label"), lambda: jev_fault("unnormalized"), lambda: jev_fault("nan"),
       lambda: phi_fault("malformed"), lambda: phi_fault("timeout"), lambda: phi_fault("oom"),
       lambda: phi_fault("schema_violation"), lambda: phi_fault("injected"), phi_oom_keeps_protection, audit_failure,
       ack_loss_and_restart, duplicate_fills, missing_required_evidence, operator_halt, unsupported_route]


def run_all() -> list[dict]:
    out = []
    for fn in ALL:
        try:
            out.append(fn())
        except Exception as exc:  # a crashing scenario is a failed scenario, reported honestly
            out.append(_result(getattr(fn, "__name__", "scenario"), "?", "?", f"EXCEPTION {type(exc).__name__}: {exc}", False))
    return out

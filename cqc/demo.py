"""Operator demonstration on ONE Phi backend serving four roles, with linked audit records:
eligible paper trade, abstention, partial fill, deterministic risk reduction, dispatch-time admission
rejection, protection during a slow Phi call, and restart recovery.
SYNTHETIC market data and a FAKE (rule-based, labeled) Phi backend."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from . import faults
from .config import load_config


def _events(rt, kind, corr):
    return [e["payload"] for e in rt.ledger.events(kind, corr)]


def _trade_chain(rt, cycle):
    corr = cycle["correlation_id"]
    intent = rt.ledger.intent(cycle["intent_id"])
    fills = [f for f in rt.ledger.fills() if f["intent_id"] == cycle["intent_id"]]
    dec = _events(rt, "decision_record", corr)[0]
    return {
        "correlation_id": corr, "snapshot_id": cycle.get("snapshot_id"),
        "evidence_requests": [{"request_id": r["request_id"], "round": r["round"], "hypothesis": r["hypothesis_id"],
                               "items": [(i["variable"], i["source_class"], i["required"]) for i in r["items"]],
                               "producer": r["producer"]} for r in _events(rt, "evidence_request", corr)],
        "fetch_plans": [{"producer": f["producer"], "fetch": f["fetch"]} for f in _events(rt, "fetch_plan", corr)],
        "evidence_results": [{"round": e["round"], "status": e["status"], "items": [
            {k: i[k] for k in ("variable", "status", "source_id", "source_timestamp", "observation_timestamp",
                               "available_at", "lag_seconds", "unit", "content_sha256")} for i in e["items"]]}
            for e in _events(rt, "evidence_result", corr)],
        "candidates": [{"candidate_id": p["candidate_id"], "action": p["action"], "qty": p["quantity_contracts"],
                        "stop": p["stop_price"], "utility_lcb_quote": p["metrics"]["utility_lcb_quote"],
                        "effective_samples": p["metrics"]["effective_sample_count"], "plan_sha256": p["plan_sha256"]}
                       for p in _events(rt, "candidate_plan", corr)],
        "analysis_packets": [{"producer": a["producer"], "status": a["status"], "passed": a["candidate_ids"]}
                             for a in _events(rt, "analysis_packet", corr)],
        "decision": {k: dec[k] for k in ("decision_id", "selector", "model_id", "model_revision", "backend_id",
                                         "candidate_ids", "selected_id", "reason_codes", "validation_status",
                                         "calibrated_selection_probability", "score_semantics")},
        "authorization_id": cycle["authorization_id"],
        "intent": {k: intent[k] for k in ("intent_id", "client_order_id", "status", "qty", "filled_qty", "price_limit",
                                          "stop_price", "auth_account_version", "auth_equity")},
        "fills": [{k: f[k] for k in ("fill_id", "qty", "price", "fee", "ts")} for f in fills]}


def run(out_dir: str = "reports") -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    t, _sym = faults.first_entry_time()
    decision_bar = t - timedelta(seconds=2)
    report = {"notice": "SYNTHETIC market data and a FAKE rule-based Phi backend (labeled 'fake_phi_rules_v2'). "
                        "Demonstrates mechanics and audit linkage only — not model quality, not trading edge.",
              "scenarios": {}}

    rt = faults.make_runtime()
    rt.run(decision_bar - timedelta(hours=1), decision_bar + timedelta(hours=7))
    rt._risk_analyst(decision_bar + timedelta(hours=7, seconds=1), "demo_material_change")
    cycle = next(c for c in rt.cycle_log if c["status"] == "authorized")
    chain = _trade_chain(rt, cycle)
    chain["protective_actions"] = [e["payload"] for e in rt.ledger.events("protective_action")]
    chain["outcomes"] = [e["payload"] for e in rt.ledger.events("outcome")]
    report["scenarios"]["eligible_paper_trade"] = chain
    report["scenarios"]["one_model_four_roles"] = {
        "backend_id": rt.phi.backend_id, "calls_by_role": rt.phi.stats["by_role"],
        "max_concurrent_inference": rt.phi.queue.max_active_seen, "phi_summary": rt.phi.resource_summary()}
    stop_exits = [f for f in rt.ledger.fills() if rt.ledger.intent(f["intent_id"])["order_type"] == "stop_market"]
    report["scenarios"]["risk_reduction"] = {
        "market_reductions": [a for a in chain["protective_actions"] if a["type"] == "market"],
        "stop_exits": [{k: f[k] for k in ("fill_id", "symbol", "side", "qty", "price")} for f in stop_exits],
        "risk_analyst_proposals": [e["payload"] for e in rt.ledger.events("adjustment_proposal")],
        "adjustments": [e["payload"] for e in rt.ledger.events("adjustment_applied") +
                        rt.ledger.events("adjustment_not_applied")]}

    abst = [c for c in rt.cycle_log if c["status"] == "abstain"]
    rt_abs = faults.make_runtime(decision_mode="abstain")
    faults.run_to_entry(rt_abs, 1)
    recs = [e["payload"] for e in rt_abs.ledger.events("decision_record")]
    report["scenarios"]["abstention"] = {
        "deterministic_abstentions_in_window": len(abst),
        "example_reasons": sorted({r for c in abst for r in c["reasons"]})[:12],
        "phi_abstain_record": {k: recs[0][k] for k in ("decision_id", "candidate_ids", "selected_id", "validation_status",
                                                       "reason_codes", "calibrated_selection_probability")} if recs else None,
        "entries_after_abstain": len(faults.entries(rt_abs))}

    cfg = load_config(overrides={"venue_sim": {"max_fill_fraction_of_bar_volume": "0.00005"}})
    rt_pf = faults.make_runtime(cfg)
    faults.run_to_entry(rt_pf, 8)
    [it] = faults.entries(rt_pf)
    res = rt_pf.ledger.db.execute("SELECT * FROM reservations WHERE intent_id=?", (it["intent_id"],)).fetchone()
    report["scenarios"]["partial_fill"] = {
        "intent_id": it["intent_id"], "ordered_qty": it["qty"], "filled_qty": it["filled_qty"], "status": it["status"],
        "reservation": dict(res), "fills": [{k: f[k] for k in ("fill_id", "qty", "price", "ts")}
                                            for f in rt_pf.ledger.fills() if f["intent_id"] == it["intent_id"]]}

    report["scenarios"]["admission_rejections"] = [faults.admission_account_changed(), faults.admission_price_collar()]
    report["scenarios"]["protection_during_slow_phi"] = faults.slow_phi_does_not_delay_protection()

    db = out / "demo_restart.sqlite"
    if db.exists():
        db.unlink()
    r = faults.ack_loss_and_restart(str(db))
    from .ledger import Ledger
    led = Ledger(str(db))
    report["scenarios"]["restart_recovery"] = {
        **r, "events": [{"kind": e["kind"], "ts": e["ts"], "payload": e["payload"]} for e in led.events()
                        if e["kind"] in ("worker_started", "order_unknown", "recovery_complete", "recovery_pending")]}
    report["summary"] = rt.summary()
    (out / "demo_report.json").write_text(json.dumps(report, indent=2, default=str))
    (out / "demo_report.md").write_text(render_md(report))
    return report


def render_md(rep: dict) -> str:
    s = rep["scenarios"]
    tr, one = s["eligible_paper_trade"], s["one_model_four_roles"]
    L = ["# Paper demo report", "", f"> {rep['notice']}", "", "## 0. One Phi backend, four roles", "",
         f"- backend: `{one['backend_id']}`", f"- calls by role: {one['calls_by_role']}",
         f"- max concurrent inference: {one['max_concurrent_inference']} (serialized admission queue)", "",
         "## 1. Eligible paper trade (linked audit chain)", "", f"- cycle `{tr['correlation_id']}`, snapshot `{tr['snapshot_id']}`"]
    for r in tr["evidence_requests"]:
        L.append(f"- analyzer EvidenceRequest `{r['request_id']}` round {r['round']} ({r['hypothesis']}): {r['items']}")
    for f in tr["fetch_plans"]:
        L.append(f"- screener fetch_plan: {[(x['variable'], x['action'], x['source_ids']) for x in f['fetch']]}")
    for e in tr["evidence_results"]:
        for i in e["items"]:
            L.append(f"  - evidence {i['variable']}: {i['status']} source={i['source_id']} src_ts={i['source_timestamp']} "
                     f"available={i['available_at']} lag={i['lag_seconds']}s unit={i['unit']} "
                     f"sha={str(i['content_sha256'])[:12]}")
    for c in tr["candidates"]:
        L.append(f"- candidate `{c['candidate_id']}` {c['action']} qty={c['qty']} stop={c['stop']} "
                 f"LCB={c['utility_lcb_quote']} USDT (n_eff={c['effective_samples']})")
    for a in tr["analysis_packets"]:
        L.append(f"- analyzer AnalysisPacket: {a['status']} passed={a['passed']}")
    d = tr["decision"]
    L += [f"- decision `{d['decision_id']}` by `{d['selector']}` ({d['model_id']}@{d['model_revision']}): offered "
          f"{d['candidate_ids']} + ABSTAIN → `{d['selected_id']}` reasons={d['reason_codes']}; "
          f"probability={d['calibrated_selection_probability']} ({d['score_semantics']})",
          f"- authorization `{tr['authorization_id']}` → intent `{tr['intent']['intent_id']}` status={tr['intent']['status']}"
          f" (bound to account version {tr['intent']['auth_account_version']}, equity {tr['intent']['auth_equity']})"]
    L += [f"- fill `{f['fill_id']}` {f['qty']} @ {f['price']} fee {f['fee']}" for f in tr["fills"]]
    L += [f"- protection: {a['type']} {a['side']} {a['qty']} {a['symbol']} ({a['reason']}) stop={a['stop_price']}"
          for a in tr["protective_actions"]]
    L += [f"- outcome: {o['symbol']} net PnL {o['net_pnl_quote']} USDT" for o in tr["outcomes"]]
    ab = s["abstention"]
    L += ["", "## 2. Abstention", "", f"- deterministic abstentions in window: {ab['deterministic_abstentions_in_window']} "
          f"(reasons: {', '.join(ab['example_reasons'])})",
          f"- Phi decision ABSTAIN record: `{json.dumps(ab['phi_abstain_record'])}`; entries: {ab['entries_after_abstain']}"]
    pf = s["partial_fill"]
    L += ["", "## 3. Partial fill", "", f"- ordered {pf['ordered_qty']}, filled {pf['filled_qty']}, status {pf['status']}, "
          f"reservation {pf['reservation']['status']} (remaining {pf['reservation']['qty_reserved']})"]
    rr = s["risk_reduction"]
    L += ["", "## 4. Risk reduction (deterministic; Phi advisory only)", ""]
    L += [f"- market {a['side']} {a['qty']} {a['symbol']} reason={a['reason']}" for a in rr["market_reductions"]]
    L += [f"- stop exit {f['side']} {f['qty']} {f['symbol']} @ {f['price']}" for f in rr["stop_exits"]]
    L += [f"- risk_analyst proposal: {p['status']}/{p['action_category']} → engine: "
          f"{[a.get('applied') or a.get('rejected') for a in rr['adjustments']]}" for p in rr["risk_analyst_proposals"]]
    L += ["", "## 5. Dispatch-time admission (fresh account/market re-check)", ""]
    L += [f"- {x['scenario']}: {x['observed']} → passed={x['passed']}" for x in s["admission_rejections"]]
    sp = s["protection_during_slow_phi"]
    L += ["", "## 6. Protection independent of Phi", "", f"- {sp['injected']}: {sp['observed']} → passed={sp['passed']}"]
    rs = s["restart_recovery"]
    L += ["", "## 7. Restart recovery", "", f"- {rs['injected']}: {rs['observed']} → passed={rs['passed']}"]
    L += [f"- `{e['kind']}` {e['ts']} {json.dumps(e['payload'])[:140]}" for e in rs["events"]]
    return "\n".join(L) + "\n"

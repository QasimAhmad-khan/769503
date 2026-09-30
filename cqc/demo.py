"""Phase-1 operator demonstration: eligible paper trade, abstention, partial fill, risk reduction and
restart recovery — each with linked audit records. SYNTHETIC data and FAKE model adapters."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from . import faults
from .config import load_config
from .util import D


def _trade_chain(rt, cycle):
    corr = cycle["correlation_id"]
    ev = {k: [e["payload"] for e in rt.ledger.events(k, corr)] for k in
          ("research_request", "evidence_result", "candidate_plan", "analysis_packet", "decision_receipt",
           "risk_authorization", "intent_created")}
    intent = rt.ledger.intent(cycle["intent_id"])
    fills = [f for f in rt.ledger.fills() if f["intent_id"] == cycle["intent_id"]]
    receipt = ev["decision_receipt"][0]
    return {"correlation_id": corr, "snapshot_id": cycle.get("snapshot_id"),
            "research_request_id": ev["research_request"][0]["request_id"] if ev["research_request"] else None,
            "evidence_ids": ev["evidence_result"][0]["evidence_ids"] if ev["evidence_result"] else [],
            "candidates": [{"candidate_id": p["candidate_id"], "action": p["action"], "qty": p["quantity_contracts"],
                            "stop": p["stop_price"], "utility_lcb_quote": p["metrics"]["utility_lcb_quote"],
                            "effective_samples": p["metrics"]["effective_sample_count"], "plan_sha256": p["plan_sha256"]}
                           for p in ev["candidate_plan"]],
            "decision": {"decision_id": receipt["decision_id"], "provider": receipt["provider"],
                         "selected_id": receipt["selected_id"], "choice_probabilities": receipt["choice_probabilities"],
                         "score_semantics": receipt["score_semantics"]},
            "authorization_id": cycle["authorization_id"], "intent": {k: intent[k] for k in (
                "intent_id", "client_order_id", "status", "qty", "filled_qty", "price_limit", "stop_price")},
            "fills": [{k: f[k] for k in ("fill_id", "qty", "price", "fee", "ts")} for f in fills]}


def run(out_dir: str = "reports") -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    t, _sym = faults.first_entry_time()
    decision_bar = t - timedelta(seconds=2)
    report = {"notice": "SYNTHETIC market data and FAKE Phi/Jev adapters (labeled). Demonstrates mechanics and audit "
                        "linkage only; it is not evidence of trading edge or model quality.", "scenarios": {}}

    # 1+4: eligible trade through to protective exit, on the baseline runtime
    rt = faults.make_runtime()
    rt.run(decision_bar - timedelta(hours=1), decision_bar + timedelta(hours=7))
    cycle = next(c for c in rt.cycle_log if c["status"] == "authorized")
    chain = _trade_chain(rt, cycle)
    chain["protective_actions"] = [e["payload"] for e in rt.ledger.events("protective_action")]
    chain["outcomes"] = [e["payload"] for e in rt.ledger.events("outcome")]
    report["scenarios"]["eligible_paper_trade"] = chain
    reductions = [a for a in chain["protective_actions"] if a["type"] == "market"]
    stop_exits = [f for f in rt.ledger.fills() if rt.ledger.intent(f["intent_id"])["order_type"] == "stop_market"]
    report["scenarios"]["risk_reduction"] = {
        "description": "Deterministic protective reductions/exits executed without any model call",
        "market_reductions": reductions, "stop_exits": [{k: f[k] for k in ("fill_id", "symbol", "side", "qty", "price")}
                                                        for f in stop_exits],
        "risk_events": [e["payload"] for e in rt.ledger.events("risk_event")]}

    # 2: abstention (deterministic rules and an explicit Jev ABSTAIN)
    abst = [c for c in rt.cycle_log if c["status"] == "abstain"]
    rt_abs = faults.make_runtime(jev_mode="abstain")
    faults.run_to_entry(rt_abs, 1)
    jev_abs = [e["payload"] for e in rt_abs.ledger.events("decision_receipt")]
    report["scenarios"]["abstention"] = {
        "deterministic_abstentions_in_window": len(abst),
        "example_reasons": sorted({r for c in abst for r in c["reasons"]}),
        "jev_abstain_receipt": {k: jev_abs[0][k] for k in ("decision_id", "candidate_ids", "selected_id",
                                                           "validation_status", "reason_codes")} if jev_abs else None,
        "entries_after_jev_abstain": len(faults.entries(rt_abs))}

    # 3: partial fill under thin simulated liquidity
    cfg = load_config(overrides={"venue_sim": {"max_fill_fraction_of_bar_volume": "0.00005"}})
    rt_pf = faults.make_runtime(cfg)
    faults.run_to_entry(rt_pf, 8)
    [it] = faults.entries(rt_pf)
    res = rt_pf.ledger.db.execute("SELECT * FROM reservations WHERE intent_id=?", (it["intent_id"],)).fetchone()
    report["scenarios"]["partial_fill"] = {
        "intent_id": it["intent_id"], "ordered_qty": it["qty"], "filled_qty": it["filled_qty"], "status": it["status"],
        "reservation": dict(res), "fills": [{k: f[k] for k in ("fill_id", "qty", "price", "ts")}
                                            for f in rt_pf.ledger.fills() if f["intent_id"] == it["intent_id"]],
        "terminal_events": [e["payload"] for e in rt_pf.ledger.events("order_terminal")]}

    # 5: restart recovery after a lost acknowledgement
    db = out / "demo_restart.sqlite"
    if db.exists():
        db.unlink()
    r = faults.ack_loss_and_restart(str(db))
    from .ledger import Ledger
    led = Ledger(str(db))
    report["scenarios"]["restart_recovery"] = {
        **r, "ledger_file": str(db),
        "events": [{"kind": e["kind"], "ts": e["ts"], "payload": e["payload"]} for e in led.events()
                   if e["kind"] in ("worker_started", "order_unknown", "recovery_complete", "recovery_pending",
                                    "risk_event", "order_ack", "protective_action")]}
    report["summary"] = rt.summary()
    (out / "demo_report.json").write_text(json.dumps(report, indent=2, default=str))
    (out / "demo_report.md").write_text(render_md(report))
    return report


def render_md(rep: dict) -> str:
    s = rep["scenarios"]
    tr = s["eligible_paper_trade"]
    lines = ["# Paper demo report", "", f"> {rep['notice']}", "", "## 1. Eligible paper trade (linked audit chain)", "",
             f"- cycle correlation: `{tr['correlation_id']}`  snapshot: `{tr['snapshot_id']}`",
             f"- research request: `{tr['research_request_id']}`; evidence: {', '.join(f'`{e}`' for e in tr['evidence_ids'])}"]
    for c in tr["candidates"]:
        lines.append(f"- candidate `{c['candidate_id']}` {c['action']} qty={c['qty']} stop={c['stop']} "
                     f"LCB={c['utility_lcb_quote']} USDT (n_eff={c['effective_samples']})")
    d = tr["decision"]
    lines += [f"- decision `{d['decision_id']}` provider={d['provider']} selected=`{d['selected_id']}` "
              f"scores={d['choice_probabilities']} ({d['score_semantics']})",
              f"- authorization `{tr['authorization_id']}` → intent `{tr['intent']['intent_id']}` "
              f"(client order `{tr['intent']['client_order_id']}`) status={tr['intent']['status']}"]
    lines += [f"- fill `{f['fill_id']}` {f['qty']} @ {f['price']} fee {f['fee']}" for f in tr["fills"]]
    lines += [f"- protection: {a['type']} {a['side']} {a['qty']} ({a['reason']}) stop={a['stop_price']}"
              for a in tr["protective_actions"]]
    lines += [f"- outcome: {o['symbol']} net PnL {o['net_pnl_quote']} USDT (closed {o['closed']})" for o in tr["outcomes"]]
    ab = s["abstention"]
    lines += ["", "## 2. Abstention", "", f"- deterministic abstentions in window: {ab['deterministic_abstentions_in_window']}",
              f"- reasons seen: {', '.join(ab['example_reasons'])}",
              f"- Jev ABSTAIN receipt: `{json.dumps(ab['jev_abstain_receipt'])}`; entries created: {ab['entries_after_jev_abstain']}"]
    pf = s["partial_fill"]
    lines += ["", "## 3. Partial fill", "", f"- intent `{pf['intent_id']}` ordered {pf['ordered_qty']}, filled "
              f"{pf['filled_qty']}, status {pf['status']}; reservation status {pf['reservation']['status']} "
              f"(remaining reserved {pf['reservation']['qty_reserved']})"]
    lines += [f"- fill `{f['fill_id']}` {f['qty']} @ {f['price']} at {f['ts']}" for f in pf["fills"]]
    rr = s["risk_reduction"]
    lines += ["", "## 4. Risk reduction (deterministic, no model)", ""]
    lines += [f"- market {a['side']} {a['qty']} {a['symbol']} reason={a['reason']}" for a in rr["market_reductions"]]
    lines += [f"- stop exit `{f['fill_id']}` {f['side']} {f['qty']} {f['symbol']} @ {f['price']}" for f in rr["stop_exits"]]
    rs = s["restart_recovery"]
    lines += ["", "## 5. Restart recovery", "", f"- injected: {rs['injected']}", f"- observed: {rs['observed']}",
              f"- passed: {rs['passed']}"]
    lines += [f"- `{e['kind']}` {e['ts']} {json.dumps(e['payload'])[:160]}" for e in rs["events"]]
    return "\n".join(lines) + "\n"

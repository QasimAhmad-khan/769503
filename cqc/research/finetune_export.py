"""Train-only dataset export for a future Phi decision-maker LoRA (no fine-tuning happens here: no GPU).

Each example = the exact decision payload the decision_maker saw (compact snapshot + offered candidate IDs)
plus a HINDSIGHT label computed by deterministic code from outcomes that fully matured inside the TRAIN
period (after costs): the offered candidate with the best realized net outcome, or ABSTAIN when none was
positive. Guards: examples whose label window ends after (train_end - embargo) are dropped, so no dev or
holdout information can enter training. The label is outcome-based, NOT the deterministic ranker's choice,
so a model fine-tuned on it is not trained to echo the ranker. Evaluate any tuned adapter against unchanged
Phi and the deterministic baseline on dev only.
"""
from __future__ import annotations

import json
from datetime import timedelta

from ..contracts import ABSTAIN
from ..util import parse_ts


def export(runtime, train_end, embargo_s: int, path) -> dict:
    from ..selection import PhiDecisionSelector
    plans = {e["payload"]["candidate_id"]: e["payload"] for e in runtime.ledger.events("candidate_plan")}
    outcomes = {e["payload"]["candidate_id"]: e["payload"] for e in runtime.ledger.events("outcome") if e["payload"]["candidate_id"]}
    sel = PhiDecisionSelector(runtime.cfg, runtime.phi) if runtime.phi else None
    kept, dropped = [], 0
    cutoff = train_end - timedelta(seconds=embargo_s)
    for e in runtime.ledger.events("decision_record"):
        d = e["payload"]
        if not d["candidate_ids"] or any(c not in plans for c in d["candidate_ids"]):
            continue
        offered = [plans[c] for c in d["candidate_ids"]]
        horizon_end = parse_ts(d["created_at"]) + timedelta(seconds=offered[0]["metrics"]["horizon_seconds"])
        if horizon_end > cutoff:
            dropped += 1
            continue
        realized = {c: float(outcomes[c]["net_pnl_quote"]) for c in d["candidate_ids"] if c in outcomes
                    and parse_ts(outcomes[c]["closed"]) <= cutoff}
        label = max(realized, key=realized.get) if realized and max(realized.values()) > 0 else ABSTAIN
        payload = sel.build_payload(snapshot_id=d["snapshot_id"], hypothesis="recorded", facts={}, plans=offered,
                                    equity=10000, cutoff=parse_ts(d["knowledge_cutoff"])) if sel else None
        kept.append({"snapshot_id": d["snapshot_id"], "input": payload, "label": label, "realized_net_quote": realized,
                     "ranker_choice": max(d["candidate_ids"], key=lambda c: float(plans[c]["metrics"]["utility_lcb_quote"])),
                     "label_source": "hindsight_train_only"})
    with open(path, "w") as fh:
        for k in kept:
            fh.write(json.dumps(k, default=str) + "\n")
    agree = sum(k["label"] == k["ranker_choice"] for k in kept)
    return {"examples": len(kept), "dropped_after_train_cutoff": dropped, "label_equals_ranker": agree,
            "abstain_labels": sum(k["label"] == ABSTAIN for k in kept), "path": str(path),
            "status": "dataset only; fine-tuning UNTESTED (no GPU)"}

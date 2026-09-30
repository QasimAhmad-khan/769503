"""Paper runtime: continuous protection loop + bounded decision loop over replayed market data.

Per 1-minute bar: venue matches → executor reconciles → risk watchdog → deterministic
protective actions (no model involvement). At each completed 15-minute decision bar: the
bounded decision cycle (screen → analyzer research request → screener evidence → numerical
candidates → risk precheck → final snapshot → Jev selection → fresh authorization → atomic
persistence). Paper/shadow and live would share this exact policy; only the venue transport
differs.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

from . import contracts, policy, quant
from .contracts import ABSTAIN
from .execution import Executor
from .graph import GraphMemory
from .ledger import AuditWriteError, Ledger, OPEN_STATUSES
from .llm.jev import FakeJevTransport, JevSelector, OpenJevTransport, candidate_facts
from .llm.phi import FakePhiBackend, OpenAICompatiblePhiBackend, PhiFailure, PhiService
from .market import BarSeries, aggregate, instruments_from_config
from .onchain import ConnectorRegistry, FixtureChainConnector, MarketContextConnector
from .pit import EvidenceStore, usable
from .risk import RiskEngine
from .util import D, ZERO, canonical_json, dstr, iso, stable_id

HYPOTHESES = {
    "trend_breakout_v1": [
        ("spread_bps", True, 120), ("estimated_funding_rate", True, 120), ("realized_vol_15m", True, 120),
        ("chain_large_transfer_count_1h", False, 7200)],
    "funding_crowding_v1": [
        ("estimated_funding_rate", True, 120), ("spread_bps", True, 120), ("realized_vol_15m", True, 120)],
}


def make_phi_backend(cfg):
    if cfg["phi"].get("backend", "fake") == "openai_compatible":
        return OpenAICompatiblePhiBackend(cfg["phi"]["endpoint"], cfg["phi"]["model"], cfg["phi"]["model_revision"])
    return FakePhiBackend()


def make_jev_transport(cfg):
    if cfg["jev"]["provider"] in ("open_jev", "typesafe") and cfg.get("_use_real_jev"):
        return OpenJevTransport(cfg["jev"]["api_base_url"], cfg["jev"]["timeout_seconds"])
    return FakeJevTransport()


class DeterministicSelector:
    """Ablation policy #2/#3: choose the highest utility LCB in code (no Jev)."""
    provider = "deterministic_max_lcb"


class PaperRuntime:
    def __init__(self, cfg, series_1m: dict, *, venue, db_path: str = ":memory:", ledger: Ledger | None = None,
                 phi_backend=None, jev_transport=None, selector: str = "jev", use_phi: bool = True,
                 use_onchain: bool = True, owner: str = "worker-1", synthetic: bool = True, chain=None,
                 store: EvidenceStore | None = None):
        self.cfg = cfg
        self.series = series_1m
        self.instruments = instruments_from_config(cfg)
        self.ledger = ledger or Ledger(db_path)
        self.venue = venue
        self.executor = Executor(self.ledger, venue, self.instruments, owner)
        self.risk = RiskEngine(cfg, self.ledger, self.instruments)
        self.store = store or EvidenceStore()
        self.connectors = ConnectorRegistry(self.store)
        self.connectors.register(MarketContextConnector(series_1m, synthetic))
        self.use_onchain = use_onchain
        if use_onchain:
            self.chain = chain or FixtureChainConnector(self.store)
            self.connectors.register(self.chain)
        self.phi = PhiService(cfg, phi_backend or make_phi_backend(cfg)) if use_phi else None
        self.selector = selector
        self.jev = JevSelector(cfg, jev_transport or make_jev_transport(cfg))
        self.graph = GraphMemory(self.ledger, cfg["graph"])
        self.bars15 = {}
        for sym, s in series_1m.items():
            b = BarSeries(sym)
            for bar in aggregate(s.bars, cfg["market"]["decision_bar_seconds"]):
                b.append(bar)
            self.bars15[sym] = b
        self.synthetic = synthetic
        self.cycle_log: list[dict] = []
        self.equity_curve: list[tuple] = []
        self.started = False

    # ------------------------------------------------------------------ lifecycle
    def start(self, now: datetime):
        """Fence ownership, then RECOVERY until account, orders, fills and protection reconcile."""
        self.executor.start(now)
        self.risk.set_lock("startup_recovery", "RECOVERY", "restart: reconcile before new entries", now, "auto")
        self.recover(now)
        self.started = True

    def recover(self, now: datetime):
        result = self.executor.reconcile(now)
        unresolved = [i for i in self.ledger.intents(statuses=["submitting", "unknown"])]
        if result["discrepancies"] or unresolved:
            self.ledger.append("recovery_pending", {"discrepancies": result["discrepancies"],
                                                    "unresolved": [i["intent_id"] for i in unresolved]}, now, "ops")
            return False
        for sym in self.instruments:
            if self.executor.book[sym].contracts != 0:
                self.executor.ensure_stop(sym, self._stop_price(sym), now)
        self.executor.track_entry_times(now)
        self.risk.clear_lock("startup_recovery", now)
        self.ledger.append("recovery_complete", {"state": self.risk.state}, now, "ops")
        return True

    # ------------------------------------------------------------------ main loop
    def run(self, start: datetime, end: datetime, on_minute=None):
        if not self.started:
            self.start(start)
        symbols = list(self.instruments)
        idx = {s: 0 for s in symbols}
        bars = {s: self.series[s].bars for s in symbols}
        for s in symbols:
            while idx[s] < len(bars[s]) and bars[s][idx[s]].end <= start:
                self.venue.last_bar[s] = bars[s][idx[s]]
                idx[s] += 1
        t = start
        while t < end:
            t += timedelta(minutes=1)
            for s in symbols:
                while idx[s] < len(bars[s]) and bars[s][idx[s]].end <= t:
                    self.venue.step(bars[s][idx[s]])
                    idx[s] += 1
            self.tick(t + timedelta(seconds=1))
            if int(t.timestamp()) % self.cfg["market"]["decision_bar_seconds"] == 0:
                for s in symbols:
                    self.decision_cycle(s, t + timedelta(seconds=2))
                self.executor.dispatch(t + timedelta(seconds=3), self.risk.submission_check)
            if on_minute:
                on_minute(self, t)
        return self

    def quote_ts(self):
        return {s: (self.venue.last_bar[s].end if s in self.venue.last_bar else None) for s in self.instruments}

    def tick(self, now: datetime):
        """Continuous protection loop step: reconcile, watchdog, deterministic protection."""
        before = self.risk.state
        rec = self.executor.reconcile(now)
        self.executor.track_entry_times(now)
        if rec["discrepancies"]:
            self.risk.set_lock("reconciliation", "RECOVERY", f"position mismatch {rec['discrepancies']}", now, "auto")
        elif "reconciliation" in self.risk.locks:
            self.risk._health("reconciliation", False, now, "RECOVERY", "")
        if "startup_recovery" in self.risk.locks:
            self.recover(now)
        acct = self.executor.account_view(now, self.quote_ts())
        actions = self.risk.watchdog(acct, now)
        for a in actions:
            if a["type"] == "ensure_stop":
                self.executor.ensure_stop(a["symbol"], self._stop_price(a["symbol"]), now)
            elif a["type"] in ("reduce", "close"):
                self.executor.protective_market(a["symbol"], a["qty"], now, a["reason"])
        if self.risk.state != "NORMAL":  # cancel unsafe working entries (reservation kept until confirmed)
            for it in self.ledger.intents(statuses=["acknowledged", "partially_filled"], purpose="entry"):
                self.executor.request_cancel(it["intent_id"], now)
        for it in self.ledger.intents(statuses=["acknowledged", "partially_filled"], purpose="entry"):
            if it["expires_at"] and it["expires_at"] <= iso(now):
                self.executor.request_cancel(it["intent_id"], now)  # unfilled remainder of an expired authorization
        self._outcomes(now)
        if self.risk.state != before and self.phi is not None:
            self._risk_analyst(now, f"state_{before}_to_{self.risk.state}", acct)
        self.equity_curve.append((now, acct.equity))

    def _stop_price(self, sym):
        stored = self.executor.stop_for(sym)
        if stored:
            return D(stored)
        pos = self.executor.book[sym]
        if pos.contracts == 0:
            return None
        # emergency policy for positions without a recorded plan stop: 3% from entry
        return pos.entry_price * (D("0.97") if pos.contracts > 0 else D("1.03"))

    def _outcomes(self, now):
        """Trade outcome = change in realized PnL (fees, funding included) from flat to flat."""
        for sym, pos in self.executor.book.items():
            key = f"open_trade:{sym}"
            trade = self.ledger.get(key)
            if pos.contracts == 0 and trade is None:
                if self.ledger.get(f"realized_flat:{sym}") != dstr(pos.realized_pnl):
                    self.ledger.put(f"realized_flat:{sym}", dstr(pos.realized_pnl))
            elif pos.contracts != 0 and trade is None:
                self.ledger.put(key, {"opened": iso(now), "realized_before": self.ledger.get(f"realized_flat:{sym}", "0"),
                                      "candidate_id": self.ledger.get(f"last_entry_candidate:{sym}")})
            elif pos.contracts == 0 and trade is not None:
                pnl = pos.realized_pnl - D(trade["realized_before"])
                self.ledger.append("outcome", {"symbol": sym, "opened": trade["opened"], "closed": iso(now),
                                               "net_pnl_quote": dstr(pnl), "candidate_id": trade["candidate_id"]},
                                   now, trade["candidate_id"] or sym)
                self.ledger.put(key, None)
                self.ledger.put(f"realized_flat:{sym}", dstr(pos.realized_pnl))
                self.executor.remember_stop(sym, None)

    def _risk_analyst(self, now, trigger, acct):
        payload = {"trigger": trigger, "state": self.risk.state, "locks": sorted(self.risk.locks),
                   "position_refs": [f"pos:{s}" for s, p in acct.positions.items() if D(p["contracts"]) != 0],
                   "positions_open": any(D(p["contracts"]) != 0 for p in acct.positions.values())}
        try:
            proposal = self.phi.run("risk_analyst", payload, now=now, correlation_id="risk", synthetic=self.synthetic)
            self.ledger.append("adjustment_proposal", proposal, now, "risk")  # advisory only; never executed directly
        except (PhiFailure, AuditWriteError) as exc:
            self.ledger.append("risk_analyst_unavailable", {"error": str(exc)[:200]}, now, "risk") if not isinstance(exc, AuditWriteError) else None

    # ------------------------------------------------------------------ decision cycle
    def decision_cycle(self, sym: str, now: datetime) -> dict:
        corr = stable_id("cycle", sym, iso(now))
        result = {"symbol": sym, "ts": iso(now), "status": "abstain", "reasons": [], "candidates": [], "selected": None,
                  "correlation_id": corr}
        try:
            self._cycle(sym, now, corr, result)
        except AuditWriteError as exc:
            self.risk.set_lock("audit_failure", "NO_NEW_RISK", str(exc)[:200], now, "auto")
            result.update(status="blocked", reasons=result["reasons"] + ["AUDIT_PERSISTENCE_FAILED"])
        except PhiFailure as exc:
            self.risk.set_lock("phi_health", "NO_NEW_RISK", str(exc)[:200], now, "auto")
            result.update(status="blocked", reasons=result["reasons"] + ["PHI_FAILURE"])
        self.cycle_log.append(result)
        try:
            self.ledger.append("cycle_result", result, now, corr)
            if "audit_failure" in self.risk.locks and "AUDIT_PERSISTENCE_FAILED" not in result["reasons"]:
                self.risk._health("audit_failure", False, now, "NO_NEW_RISK", "")
        except AuditWriteError as exc:
            self.risk.set_lock("audit_failure", "NO_NEW_RISK", str(exc)[:200], now, "auto")
        return result

    def _cycle(self, sym, now, corr, result):
        cfg, q = self.cfg, self.cfg["quant"]
        inst = self.instruments[sym]
        bars = self.bars15[sym].completed(now, limit=int(q["history_bars"]) + 64)
        if len(bars) < q["vol_window_bars"] + q["trend_slow_bars"] + 16:
            result["reasons"].append("INSUFFICIENT_HISTORY")
            return
        closes = np.array([b.close for b in bars])
        feats = quant.features(closes, np.array([b.funding_rate_est for b in bars]), dict(q))
        last = bars[-1]
        if last.spread_bps > float(q["max_spread_bps"]):
            result["reasons"].append("SPREAD_TOO_WIDE")
            return
        acct = self.executor.account_view(now, self.quote_ts())
        position = D(acct.positions[sym]["contracts"])
        open_entry = self.ledger.intents(statuses=sorted(OPEN_STATUSES), symbol=sym, purpose="entry")
        if open_entry:
            result["reasons"].append("ENTRY_ALREADY_PENDING")
            return
        signals = {"trend_breakout_v1": int(feats["trend_dir"][-1]), "funding_crowding_v1": int(feats["funding_dir"][-1])}
        snapshot_prelim = stable_id("snap0", sym, iso(now))

        if position != 0:
            hypothesis = "trend_breakout_v1"
            evidence = self._acquire(sym, inst, hypothesis, now, corr, result, phi_request=None)
            if evidence is None:
                return
            final_cutoff = now + timedelta(seconds=1)
            snapshot_id = stable_id("snap", sym, iso(final_cutoff), canonical_json(sorted(evidence)))
            plan = quant.build_close_candidate(cfg=cfg, inst=inst, bars15=bars, cutoff=final_cutoff,
                                               position_contracts=position, feats=feats, snapshot_id=snapshot_id,
                                               account_version=acct.version, evidence_ids=list(evidence),
                                               correlation_id=corr, synthetic=self.synthetic)
            plans = [plan] if plan else []
        else:
            hypothesis, phi_request = self._choose_hypothesis(sym, inst, signals, snapshot_prelim, now, corr, result)
            if hypothesis is None:
                return
            evidence = self._acquire(sym, inst, hypothesis, now, corr, result, phi_request)
            if evidence is None:
                return
            final_cutoff = now + timedelta(seconds=1)  # snapshot finalized after evidence acquisition
            snapshot_id = stable_id("snap", sym, iso(final_cutoff), canonical_json(sorted(evidence)))
            rooms = self.risk.sizing_rooms(acct, sym)
            ctx = quant.SizingContext(acct.equity, rooms["per_trade_budget"], rooms["aggregate_room"],
                                      rooms["exposure_room"], rooms["symbol_room"], rooms["margin_room"],
                                      rooms["stress_room"])
            funding = evidence.get("estimated_funding_rate", {}).get("value") or 0.0
            plans, analysis = quant.build_entry_candidates(
                cfg=cfg, inst=inst, bars15=bars, cutoff=final_cutoff, hypothesis=hypothesis,
                direction=signals[hypothesis], feats=feats, ctx=ctx, snapshot_id=snapshot_id,
                account_version=acct.version, evidence_ids=[e["evidence_id"] for e in evidence.values() if usable(e)],
                correlation_id=corr, funding_rate=float(funding), synthetic=self.synthetic)
            result["reasons"] += analysis["reason_codes"]
            result["effective_samples"] = analysis.get("forecast", {}).get("effective_samples")

        # deterministic risk precheck -> immutable plans with the precheck outcome bound into the hash
        checked = []
        for p in plans:
            ok, why = self.risk.precheck(p, acct)
            if ok:  # only prechecked plans become candidates (schema: risk_precheck_passed must be true)
                checked.append(quant.finalize_precheck(p, True))
            else:
                result["reasons"] += why
        eligible = checked[: cfg["jev"]["max_trade_candidates"]]
        if eligible and self.phi is not None and position == 0:
            eligible = self._analyzer_packet(sym, snapshot_id, hypothesis, eligible, evidence, now, corr, result)
        result["candidates"] = [p["candidate_id"] for p in eligible]
        for p in eligible:
            self.ledger.append("candidate_plan", p, now, corr)
            self._graph_candidate(p, evidence, now)
        if not eligible:
            result["reasons"].append("NO_ELIGIBLE_CANDIDATE")
            return

        receipt = self._select(snapshot_id, hypothesis, evidence, eligible, acct, now, corr)
        result["selected"] = receipt["selected_id"]
        result["decision_status"] = receipt["validation_status"]
        if receipt["validation_status"] in ("timeout", "unavailable", "invalid"):
            self.risk.set_lock("jev_health", "NO_NEW_RISK", f"decision {receipt['validation_status']}", now, "auto")
        elif "jev_health" in self.risk.locks:
            self.risk._health("jev_health", False, now, "NO_NEW_RISK", "")
        if receipt["selected_id"] == ABSTAIN:
            self.ledger.append("decision_receipt", receipt, now, corr)
            result["reasons"] += receipt["reason_codes"]
            return
        plan = next(p for p in eligible if p["candidate_id"] == receipt["selected_id"])
        auth_now = now + timedelta(seconds=1)
        acct2 = self.executor.account_view(auth_now, self.quote_ts())
        auth = self.risk.authorize(plan, receipt, acct2, auth_now)
        expected = self.jev.transport.provider if self.selector == "jev" else DeterministicSelector.provider
        ok, why = policy.check_entry(cfg=cfg, route="autonomous_cycle", strategy=hypothesis, plan=plan, receipt=receipt,
                                     audit_ok=self.ledger.fail_writes == 0, now=auth_now, expected_provider=expected)
        if not auth["approved"] or not ok:
            self.ledger.append("decision_receipt", receipt, now, corr)
            self.ledger.append("risk_authorization", auth, auth_now, corr)
            result.update(status="rejected", reasons=result["reasons"] + auth["reason_codes"] + ([] if ok else [why]))
            return
        per_contract = self.risk.plan_stop_risk_per_contract(plan) if plan["risk_increasing"] else ZERO
        intent_id = self.executor.persist_authorized_entry(plan=plan, receipt=receipt, auth=auth,
                                                           per_contract_risk=per_contract, now=auth_now,
                                                           correlation_id=corr)
        if plan["risk_increasing"]:
            self.executor.remember_stop(sym, plan["stop_price"])
            self.ledger.put(f"last_entry_candidate:{sym}", plan["candidate_id"])
        self.graph.add_node(receipt["decision_id"], "decision", f"events/{receipt['event_id']}", auth_now,
                            f"selected {receipt['selected_id']}")
        self.graph.add_edge(receipt["decision_id"], plan["candidate_id"], "selected", auth_now)
        self.graph.add_node(auth["authorization_id"], "policy", f"events/{auth['event_id']}", auth_now, "risk authorization")
        self.graph.add_edge(plan["candidate_id"], auth["authorization_id"], "authorized_by", auth_now)
        result.update(status="authorized", intent_id=intent_id, authorization_id=auth["authorization_id"],
                      snapshot_id=snapshot_id)

    # ------------------------------------------------------------------ cycle helpers
    def _choose_hypothesis(self, sym, inst, signals, snapshot_prelim, now, corr, result):
        registry = []
        for hyp, feats in HYPOTHESES.items():
            spec = []
            for feature, required, max_age in feats:
                if feature.startswith("chain_") and not self.use_onchain:
                    continue
                spec.append({"feature": feature, "unit": self.connectors.unit(feature) or "unknown", "required": required,
                             "max_age_seconds": max_age, "approved_source_ids": self.connectors.approved(feature) or ["none"]})
            registry.append({"hypothesis_id": hyp, "signal_direction": signals[hyp], "features": spec})
        if self.phi is None:
            hyp = next((h for h in registry if h["signal_direction"] != 0), None)
            if hyp is None:
                result["reasons"].append("NO_REGISTERED_SIGNAL")
                return None, None
            return hyp["hypothesis_id"], {"features": hyp["features"]}
        payload = {"stage": "request", "snapshot_id": snapshot_prelim, "request_id": stable_id("req", snapshot_prelim),
                   "instrument": inst.contract_dict(), "horizon_seconds": self.cfg["market"]["forecast_horizon_seconds"],
                   "cutoff": iso(now), "hypotheses": registry}
        out = self.phi.run("analyzer", payload, now=now, correlation_id=corr, key=f"analyzer:{snapshot_prelim}",
                           synthetic=self.synthetic)
        self.ledger.append(out["kind"], out, now, corr)
        if out["kind"] != "research_request":
            result["reasons"] += out["reason_codes"]
            return None, None
        # host-side semantic validation: the model cannot choose another instrument, unregistered features or sources
        allowed = {h["hypothesis_id"]: h for h in registry}
        hyp = allowed.get(out["hypothesis_id"])
        if hyp is None or out["instrument"] != inst.contract_dict():
            result["reasons"].append("RESEARCH_REQUEST_OUT_OF_SCOPE")
            return None, None
        registered = {f["feature"]: f for f in hyp["features"]}
        for f in out["features"]:
            reg = registered.get(f["feature"])
            if reg is None or not set(f["approved_source_ids"]) <= set(reg["approved_source_ids"]):
                result["reasons"].append("RESEARCH_REQUEST_UNREGISTERED_FEATURE_OR_SOURCE")
                return None, None
        missing_required = [f for f in hyp["features"] if f["required"] and f["feature"] not in {x["feature"] for x in out["features"]}]
        feats = out["features"] + missing_required  # required features are host policy, not model choice
        if hyp["signal_direction"] == 0:
            result["reasons"].append("HYPOTHESIS_SIGNAL_NEUTRAL")
            return None, None
        return out["hypothesis_id"], {"features": feats, "request": out}

    def _acquire(self, sym, inst, hypothesis, now, corr, result, phi_request):
        """Evidence acquisition: caches first, then approved connectors; at most one follow-up round."""
        spec = phi_request["features"] if phi_request else [
            {"feature": f, "required": r, "max_age_seconds": a, "approved_source_ids": self.connectors.approved(f)}
            for f, r, a in HYPOTHESES[hypothesis] if self.use_onchain or not f.startswith("chain_")]
        budget = min(self.cfg["research"]["max_connector_calls_per_request"], 4)
        calls_before = self.connectors.calls
        features_to_fetch, sources = [f["feature"] for f in spec], sorted({s for f in spec for s in f["approved_source_ids"]})
        if self.phi is not None and phi_request is not None:
            req = phi_request.get("request")
            payload = {"stage": "retrieve", "request": {"request_id": req["request_id"] if req else "req",
                                                        "features": [{**f, "cached": False} for f in spec]}}
            tr = self.phi.run("screener", payload, now=now, correlation_id=corr, key=f"screener:{corr}", synthetic=self.synthetic)
            self.ledger.append("tool_request", tr, now, corr)
            if tr["kind"] != "tool_request" or tr["tool"] != "retrieve_evidence_batch":
                result["reasons"].append("SCREENER_TOOL_NOT_ALLOWLISTED")
                return None
            wanted = set(tr["arguments"].get("features", []))
            approved = {s for f in spec for s in f["approved_source_ids"]}
            if not wanted <= {f["feature"] for f in spec} or not set(tr["arguments"].get("source_ids", [])) <= approved:
                result["reasons"].append("SCREENER_REQUEST_OUT_OF_SCOPE")
                return None
            features_to_fetch = [f for f in features_to_fetch if f in wanted]
        evidence = {}
        for round_no in range(1 + self.cfg["research"]["max_followup_rounds"]):
            for f in spec:
                if f["feature"] in evidence and usable(evidence[f["feature"]]):
                    continue
                cached = self.store.as_of(f["feature"], inst.contract_dict(), now, f["max_age_seconds"], corr)
                if not usable(cached) and f["feature"] in features_to_fetch and self.connectors.calls - calls_before < budget:
                    self.connectors.retrieve(f["feature"], inst.contract_dict(), now, f["approved_source_ids"], corr)
                    cached = self.store.as_of(f["feature"], inst.contract_dict(), now, f["max_age_seconds"], corr)
                evidence[f["feature"]] = cached
            if all(usable(evidence[f["feature"]]) for f in spec if f["required"]):
                break
            features_to_fetch = [f["feature"] for f in spec if f["required"] and not usable(evidence[f["feature"]])]
        missing = [f["feature"] for f in spec if f["required"] and not usable(evidence[f["feature"]])]
        if self.phi is not None and phi_request is not None:
            payload = {"stage": "conclude", "request": {"request_id": (phi_request.get("request") or {}).get("request_id", "req")},
                       "evidence": [{"feature": k, "evidence_id": v["evidence_id"], "usable": usable(v),
                                     "required": next(f["required"] for f in spec if f["feature"] == k)} for k, v in evidence.items()]}
            er = self.phi.run("screener", payload, now=now, correlation_id=corr, key=f"screener2:{corr}", synthetic=self.synthetic)
            self.ledger.append(er["kind"], er, now, corr)
            if er["kind"] == "evidence_result" and not set(er["evidence_ids"]) <= {v["evidence_id"] for v in evidence.values()}:
                result["reasons"].append("SCREENER_CITED_UNKNOWN_EVIDENCE")
                return None
        for e in evidence.values():
            if usable(e):
                self.graph.add_node(e["evidence_id"], "evidence", f"evidence/{e['evidence_id']}",
                                    datetime.fromisoformat(e["available_at"].replace("Z", "+00:00")),
                                    f"{e['feature']}={e['value']} {e['unit']} ({e['source_id']})")
        if missing:
            result["reasons"].append("REQUIRED_EVIDENCE_MISSING")
            result["missing"] = missing
            return None
        return evidence

    def _analyzer_packet(self, sym, snapshot_id, hypothesis, eligible, evidence, now, corr, result):
        payload = {"stage": "packet", "snapshot_id": snapshot_id, "hypothesis_id": hypothesis,
                   "tool_result_ids": [f"features_v1:{snapshot_id}", f"empirical_forecast_v1:{snapshot_id}"],
                   "effective_sample_count": eligible[0]["metrics"]["effective_sample_count"], "missing": [],
                   "evidence": [{"feature": k, "value": v["value"], "contradiction": False} for k, v in evidence.items()],
                   "candidates": [{"candidate_id": p["candidate_id"], "eligible": True, "action": p["action"],
                                   "utility_lcb_quote": p["metrics"]["utility_lcb_quote"]} for p in eligible]}
        packet = self.phi.run("analyzer", payload, now=now, correlation_id=corr, key=f"packet:{snapshot_id}",
                              synthetic=self.synthetic)
        self.ledger.append(packet["kind"], packet, now, corr)
        if packet["kind"] != "analysis_packet":
            result["reasons"].append("ANALYZER_WRONG_RESULT_KIND")
            return []
        ids = set(packet["candidate_ids"])
        if not ids <= {p["candidate_id"] for p in eligible}:
            result["reasons"].append("ANALYZER_INVENTED_CANDIDATE")
            return []
        return [p for p in eligible if p["candidate_id"] in ids]

    def _select(self, snapshot_id, hypothesis, evidence, eligible, acct, now, corr):
        ttl = int(self.cfg["quant"]["plan_ttl_seconds"])
        if self.selector == "deterministic":
            best = max(eligible, key=lambda p: (D(p["metrics"]["utility_lcb_quote"]), p["candidate_id"]))
            receipt = contracts.envelope("decision_receipt", stable_id("evt_dec", snapshot_id, "det"), corr,
                                         "deterministic_selector", iso(now), iso(now), iso(now + timedelta(seconds=ttl)),
                                         self.synthetic)
            probs = {ABSTAIN: 0.0, **{p["candidate_id"]: (1.0 if p is best else 0.0) for p in eligible}}
            receipt.update({"decision_id": stable_id("dec", snapshot_id, "det"), "snapshot_id": snapshot_id,
                            "candidate_ids": [p["candidate_id"] for p in eligible], "selected_id": best["candidate_id"],
                            "provider": DeterministicSelector.provider, "model_revision": "code:max_lcb_v1",
                            "prompt_version": "none", "request_sha256": stable_id("x", snapshot_id)[2:].ljust(64, "0")[:64],
                            "raw_response_sha256": "0" * 64, "choice_probabilities": probs, "provider_confidence": 1.0,
                            "score_semantics": "categorical_preference_not_win_probability",
                            "validation_status": "valid", "reason_codes": ["DETERMINISTIC_MAX_LCB"], "latency_ms": 0,
                            "executable_authorization": False})
            return contracts.validate(receipt)
        facts = {"synthetic": self.synthetic, "required_evidence_complete": True}
        for k, v in evidence.items():
            if usable(v):
                facts[k] = v["value"]
        sizes = {p["candidate_id"]: ("full" if i == 0 else "reduced") for i, p in enumerate(
            sorted(eligible, key=lambda p: -D(p["quantity_contracts"])))}
        cands = [candidate_facts(p, acct.equity, sizes[p["candidate_id"]]) for p in eligible]
        receipt, raw = self.jev.select(snapshot_id=snapshot_id, hypothesis=hypothesis, facts=facts, candidates=cands,
                                       now=now, correlation_id=corr, ttl_seconds=ttl, synthetic=self.synthetic)
        if raw is not None:  # bounded raw response retained for replay and audit
            self.ledger.append("jev_raw_response", {"request_sha256": receipt["request_sha256"],
                                                    "response_text": raw[:4096]}, now, corr)
        return receipt

    def _graph_candidate(self, plan, evidence, now):
        self.graph.add_node(plan["candidate_id"], "candidate", f"events/{plan['event_id']}", now,
                            f"{plan['action']} {plan['quantity_contracts']} {plan['instrument']['symbol']} "
                            f"lcb={plan['metrics']['utility_lcb_quote']}")
        for e in evidence.values():
            if usable(e) and e["evidence_id"] in plan["evidence_ids"]:
                self.graph.add_edge(plan["candidate_id"], e["evidence_id"], "derived_from", now, [e["evidence_id"]])

    # ------------------------------------------------------------------ reporting
    def summary(self) -> dict:
        acct = self.venue.account()
        statuses = {}
        for c in self.cycle_log:
            statuses[c["status"]] = statuses.get(c["status"], 0) + 1
        fills = self.ledger.fills()
        return {"equity": dstr(acct["equity"]), "wallet": dstr(acct["wallet"]), "risk_state": self.risk.state,
                "locks": dict(self.risk.locks), "cycles": len(self.cycle_log), "cycle_status_counts": statuses,
                "fills": len(fills), "fees_paid": dstr(sum((p.fees_paid for p in self.venue.positions.values()), ZERO)),
                "funding_paid": dstr(sum((p.funding_paid for p in self.venue.positions.values()), ZERO)),
                "outcomes": len(self.ledger.events("outcome")), "risk_latency": self.risk.latency_summary(),
                "phi": self.phi.resource_summary() if self.phi else None,
                "jev": {k: v for k, v in self.jev.stats.items() if k != "latency_ms"}, "graph": self.graph.counts()}

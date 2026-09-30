"""Paper runtime: independent protection loop + bounded decision loop over replayed market data.

Protection (`protect`) — reconcile, risk watchdog, deterministic protective actions — never calls
a model, holds only the account lock, and can run in its own thread (`cqc.protection.ProtectionLoop`)
on market/account events and a timer while a Phi call is in flight.

Decision cycle, per completed 15-minute bar and symbol (all model roles are ONE local Phi model):
  deterministic screen → analyzer EvidenceRequest (typed, bounded) → screener fetch_plan over
  whitelisted sources → host fetch + point-in-time stamping → ≤1 follow-up round → deterministic
  forecast/candidates → risk precheck → analyzer AnalysisPacket (subset only) → decision_maker picks one
  offered ID or ABSTAIN → fresh authorization → policy gate → atomic persistence. At dispatch the
  risk engine re-validates on a fresh account/market view before any entry is sent.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import numpy as np

from . import policy, quant
from .contracts import ABSTAIN
from .execution import Executor
from .graph import GraphMemory
from .ledger import AuditWriteError, Ledger, OPEN_STATUSES
from .llm.phi import FakePhiBackend, OpenAICompatiblePhiBackend, PhiFailure, PhiService
from .market import BarSeries, aggregate, instruments_from_config
from .onchain import ConnectorRegistry, FixtureChainConnector, MarketContextConnector
from .pit import EvidenceStore, usable
from .risk import RiskEngine
from .selection import DeterministicRankSelector, PhiDecisionSelector
from .util import D, ZERO, canonical_json, dstr, iso, parse_ts, round_price, stable_id

# Registered variables per hypothesis: (variable, required, max_age_seconds)
HYPOTHESES = {
    "trend_breakout_v1": [
        ("spread_bps", True, 120), ("estimated_funding_rate", True, 120), ("realized_vol_15m", True, 120),
        ("chain_large_transfer_count_1h", False, 7200)],
    "funding_crowding_v1": [
        ("estimated_funding_rate", True, 120), ("spread_bps", True, 120), ("realized_vol_15m", True, 120)],
}
PHI_FEATURES = ("evidence", "analysis", "decision", "risk")


class CycleBudgetExceeded(PhiFailure):
    status = "overloaded"


def make_phi_backend(cfg):
    if cfg["phi"].get("backend", "fake") == "openai_compatible":
        return OpenAICompatiblePhiBackend(cfg["phi"]["endpoint"], cfg["phi"]["model"], cfg["phi"]["model_revision"],
                                          record_logprobs=cfg["phi"].get("record_token_logprobs", False))
    return FakePhiBackend()


class PaperRuntime:
    def __init__(self, cfg, series_1m: dict, *, venue, db_path: str = ":memory:", ledger: Ledger | None = None,
                 phi_backend=None, phi_features=None, use_phi: bool = True, selector: str | None = None,
                 use_onchain: bool = True, owner: str = "worker-1", synthetic: bool = True, chain=None,
                 store: EvidenceStore | None = None, mode: str = "replay", record_phi: bool = True):
        self.cfg = cfg
        self.series = series_1m
        self.mode = mode
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
        self.phi_features = set(PHI_FEATURES if phi_features is None else phi_features) if use_phi else set()
        self.phi = None
        if self.phi_features:
            recorder = self._record_phi if record_phi else None
            self.phi = PhiService(cfg, phi_backend or make_phi_backend(cfg), recorder=recorder)
        wanted = selector or ("phi" if "decision" in self.phi_features else "deterministic_rank_v1")
        if wanted == "phi" and self.phi is None:
            raise ValueError("Phi decision selection requires the Phi service")
        self.selector = PhiDecisionSelector(cfg, self.phi) if wanted == "phi" else DeterministicRankSelector(cfg)
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
        self.protect_latency_ms: list[float] = []
        self.decision_latency_ms: list[float] = []
        self.account_lock = threading.RLock()
        self._plans: dict = {}
        self._auths: dict = {}
        self._review_pending: str | None = None
        self._phi_calls_this_cycle = 0
        self.started = False

    # ------------------------------------------------------------------ identity / recording
    @property
    def expected_selector(self):
        return self.selector.name

    @property
    def expected_backend_id(self):
        return self.phi.backend_id if isinstance(self.selector, PhiDecisionSelector) else "in-process"

    def _record_phi(self, role, key, text, ident):
        try:
            self.ledger.append("phi_raw_response", {"role": role, "request_key": key, "response_text": text,
                                                    "model_id": ident["model_id"], "revision": ident["revision"]},
                               self.venue.now or datetime.fromtimestamp(0).astimezone(), "phi")
        except AuditWriteError:
            pass  # replay capture is best effort; the decision record itself is mandatory

    def _phi(self, role, payload, now, corr, allowed_ids=None):
        self._phi_calls_this_cycle += 1
        if self._phi_calls_this_cycle > self.cfg["research"]["max_phi_calls_per_cycle"]:
            raise CycleBudgetExceeded("per-cycle Phi call budget exhausted")
        return self.phi.run(role, payload, now=now, correlation_id=corr, allowed_ids=allowed_ids, synthetic=self.synthetic)

    # ------------------------------------------------------------------ lifecycle
    def start(self, now: datetime):
        """Fence ownership, then RECOVERY until account, orders, fills and protection reconcile."""
        with self.account_lock:
            self.executor.start(now)
            legacy = self.ledger.legacy_summary()
            if legacy:
                self.ledger.append("legacy_records_detected", {"counts": legacy, "note": "Open-Jev-era records are "
                                   "audit-only; never replayed or interpreted as Phi decisions"}, now, "ops")
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

    # ------------------------------------------------------------------ main replay loop
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
            with self.account_lock:
                for s in symbols:
                    while idx[s] < len(bars[s]) and bars[s][idx[s]].end <= t:
                        self.venue.step(bars[s][idx[s]])
                        idx[s] += 1
            self.tick(t + timedelta(seconds=1))
            if int(t.timestamp()) % self.cfg["market"]["decision_bar_seconds"] == 0:
                for s in symbols:
                    self.decision_cycle(s, t + timedelta(seconds=2))
                self.dispatch(t + timedelta(seconds=3))
            if on_minute:
                on_minute(self, t)
        return self

    def quote_ts(self):
        return {s: (self.venue.last_bar[s].end if s in self.venue.last_bar else None) for s in self.instruments}

    def quote(self, sym):
        bar = self.venue.last_bar.get(sym)
        if bar is None:
            return None
        return {"bid": D(repr(bar.bid)), "ask": D(repr(bar.ask)), "ts": bar.end}

    # ------------------------------------------------------------------ protection (never calls a model)
    def protect(self, now: datetime) -> list[dict]:
        t0 = time.perf_counter()
        with self.account_lock:
            before = self.risk.state
            rec = self.executor.reconcile(now)
            self.executor.track_entry_times(now)
            if rec["discrepancies"]:
                self.risk.set_lock("reconciliation", "RECOVERY", f"position mismatch {rec['discrepancies']}", now, "auto")
            elif "reconciliation" in self.risk.locks:
                self.risk._health("reconciliation", False, now, "RECOVERY", "")
            if "startup_recovery" in self.risk.locks:
                self.recover(now)
            self._expire_advisory_lock(now)
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
                    self.executor.request_cancel(it["intent_id"], now)
            if self.risk.state != before:
                self._review_pending = f"state_{before}_to_{self.risk.state}"
        self.protect_latency_ms.append((time.perf_counter() - t0) * 1000)
        return actions

    def tick(self, now: datetime):
        self.protect(now)
        with self.account_lock:
            self._outcomes(now)
            acct_equity = self.venue.account()["equity"]
            if now.minute == 0:
                self.graph.prune(now, self._protected_graph_ids())
        if self._review_pending and "risk" in self.phi_features:
            trigger, self._review_pending = self._review_pending, None
            self._risk_analyst(now, trigger)
        self.equity_curve.append((now, acct_equity))

    def _protected_graph_ids(self):
        ids = set()
        for it in self.ledger.intents(statuses=sorted(OPEN_STATUSES)):
            ids.update(x for x in (it["candidate_id"], it["authorization_id"]) if x)
        for sym in self.instruments:
            c = self.ledger.get(f"last_entry_candidate:{sym}")
            if c and self.executor.book[sym].contracts != 0:
                ids.add(c)
        return ids

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

    # ------------------------------------------------------------------ risk analyst (advisory, validated in code)
    def _risk_analyst(self, now, trigger):
        with self.account_lock:
            acct = self.executor.account_view(now, self.quote_ts())
        payload = {"trigger": trigger, "state": self.risk.state, "locks": sorted(self.risk.locks),
                   "position_refs": [f"pos:{s}" for s, p in acct.positions.items() if D(p["contracts"]) != 0],
                   "positions_open": any(D(p["contracts"]) != 0 for p in acct.positions.values())}
        try:
            proposal = self.phi.run("risk_analyst", payload, now=now, correlation_id="risk", synthetic=self.synthetic)
        except PhiFailure as exc:
            self.ledger.append("risk_analyst_unavailable", {"error": str(exc)[:200]}, now, "risk")
            return None
        self.ledger.append("adjustment_proposal", proposal, now, "risk")
        return self.apply_proposal(proposal, now)

    def apply_proposal(self, proposal: dict, now: datetime) -> dict:
        """Deterministic validation/application. The model never supplies quantities or prices; only
        risk-reducing categories exist, bounded by verified positions."""
        applied, rejected = [], []
        with self.account_lock:
            acct = self.executor.account_view(now, self.quote_ts())
            cat = proposal["action_category"] if proposal["status"] == "propose" else "none"
            open_syms = {s for s, p in acct.positions.items() if D(p["contracts"]) != 0}
            refs = {r.replace("pos:", "") for r in proposal["position_refs"]}
            targets = sorted(refs & open_syms)
            if refs - open_syms:
                rejected.append(f"UNKNOWN_POSITION_REFS:{sorted(refs - open_syms)}")
            if cat == "move_to_no_new_risk":
                self.risk.set_lock("risk_analyst_advisory", "NO_NEW_RISK", "risk analyst advisory", now, "auto")
                self.ledger.put("advisory_until", iso(now + timedelta(minutes=15)))
                applied.append("NO_NEW_RISK_15M")
            elif cat == "cancel_pending":
                for it in self.ledger.intents(statuses=["queued", "acknowledged", "partially_filled"], purpose="entry"):
                    self.executor.request_cancel(it["intent_id"], now)
                    applied.append(f"CANCEL:{it['intent_id']}")
            elif cat in ("reduce", "close"):
                for sym in targets:
                    pos = abs(D(acct.positions[sym]["contracts"]))
                    qty = pos if cat == "close" else self.instruments[sym].floor_qty(pos / 2)
                    if qty > 0 and self.executor.protective_market(sym, qty, now, f"risk_analyst_{cat}"):
                        applied.append(f"{cat.upper()}:{sym}:{dstr(qty)}")
            elif cat == "tighten_stop":
                for sym in targets:
                    cur, mark = self._stop_price(sym), acct.marks[sym]
                    long_ = D(acct.positions[sym]["contracts"]) > 0
                    new = round_price(cur + (mark - cur) / 2, self.instruments[sym].price_tick)
                    if cur is None or (long_ and not (cur < new < mark)) or (not long_ and not (mark < new < cur)):
                        rejected.append(f"TIGHTEN_INVALID:{sym}")
                        continue
                    self.executor.remember_stop(sym, new)
                    self.executor.ensure_stop(sym, new, now)
                    applied.append(f"TIGHTEN:{sym}:{dstr(new)}")
        result = {"proposal_event_id": proposal["event_id"], "category": proposal["action_category"],
                  "applied": applied, "rejected": rejected}
        self.ledger.append("adjustment_applied" if applied else "adjustment_not_applied", result, now, "risk")
        return result

    def _expire_advisory_lock(self, now):
        until = self.ledger.get("advisory_until")
        if until and parse_ts(until) <= now and "risk_analyst_advisory" in self.risk.locks:
            self.risk.clear_lock("risk_analyst_advisory", now)
            self.ledger.put("advisory_until", None)

    # ------------------------------------------------------------------ dispatch with fresh admission
    def dispatch(self, now: datetime):
        with self.account_lock:
            return self.executor.dispatch(now, self._admission)

    def _admission(self, intent: dict, now: datetime):
        if intent["purpose"] == "protective":
            return True, ["PROTECTIVE"]
        plan = self._plans.get(intent["plan_sha256"])
        auth = self._auths.get(intent["authorization_id"])
        if plan is None or auth is None:  # e.g. after restart: recover from the durable ledger
            for e in self.ledger.events("candidate_plan"):
                if e["payload"]["plan_sha256"] == intent["plan_sha256"]:
                    plan = e["payload"]
            for e in self.ledger.events("risk_authorization"):
                if e["payload"]["authorization_id"] == intent["authorization_id"]:
                    auth = e["payload"]
        acct = self.executor.account_view(now, self.quote_ts(), exclude_intent=intent["intent_id"])
        return self.risk.admission_check(intent, plan, auth, acct, self.quote(intent["symbol"]), now)

    # ------------------------------------------------------------------ decision cycle
    def decision_cycle(self, sym: str, now: datetime) -> dict:
        corr = stable_id("cycle", sym, iso(now))
        result = {"symbol": sym, "ts": iso(now), "status": "abstain", "reasons": [], "candidates": [], "selected": None,
                  "correlation_id": corr}
        self._phi_calls_this_cycle = 0
        t0 = time.perf_counter()
        try:
            self._cycle(sym, now, corr, result)
        except AuditWriteError as exc:
            self.risk.set_lock("audit_failure", "NO_NEW_RISK", str(exc)[:200], now, "auto")
            result.update(status="blocked", reasons=result["reasons"] + ["AUDIT_PERSISTENCE_FAILED"])
        except PhiFailure as exc:
            self.risk.set_lock("phi_health", "NO_NEW_RISK", str(exc)[:200], now, "auto")
            result.update(status="blocked", reasons=result["reasons"] + [f"PHI_{exc.status.upper()}"])
        else:
            if "phi_health" in self.risk.locks and self._phi_calls_this_cycle:
                self.risk._health("phi_health", False, now, "NO_NEW_RISK", "")
        result["phi_calls"] = self._phi_calls_this_cycle
        self.decision_latency_ms.append((time.perf_counter() - t0) * 1000)
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
        if bars[-1].spread_bps > float(q["max_spread_bps"]):
            result["reasons"].append("SPREAD_TOO_WIDE")
            return
        with self.account_lock:
            acct = self.executor.account_view(now, self.quote_ts())
            open_entry = self.ledger.intents(statuses=sorted(OPEN_STATUSES), symbol=sym, purpose="entry")
        position = D(acct.positions[sym]["contracts"])
        if open_entry:
            result["reasons"].append("ENTRY_ALREADY_PENDING")
            return
        signals = {"trend_breakout_v1": int(feats["trend_dir"][-1]), "funding_crowding_v1": int(feats["funding_dir"][-1])}
        snapshot_prelim = stable_id("snap0", sym, iso(now))

        if position != 0:
            hypothesis = "trend_breakout_v1"
            evidence = self._acquire_deterministic(sym, inst, hypothesis, now, corr, result)
        else:
            hypothesis, evidence = self._evidence_dialogue(sym, inst, signals, snapshot_prelim, now, corr, result)
        if evidence is None:
            return
        final_cutoff = now + timedelta(seconds=1)  # snapshot finalized after evidence acquisition
        snapshot_id = stable_id("snap", sym, iso(final_cutoff), canonical_json(sorted(
            (k, v["evidence_id"]) for k, v in evidence.items())))
        ev_ids = [e["evidence_id"] for e in evidence.values() if usable(e)]
        if position != 0:
            plan = quant.build_close_candidate(cfg=cfg, inst=inst, bars15=bars, cutoff=final_cutoff,
                                               position_contracts=position, feats=feats, snapshot_id=snapshot_id,
                                               account_version=acct.version, evidence_ids=ev_ids,
                                               correlation_id=corr, synthetic=self.synthetic)
            plans = [plan] if plan else []
        else:
            rooms = self.risk.sizing_rooms(acct, sym)
            ctx = quant.SizingContext(acct.equity, rooms["per_trade_budget"], rooms["aggregate_room"],
                                      rooms["exposure_room"], rooms["symbol_room"], rooms["margin_room"],
                                      rooms["stress_room"])
            funding = evidence.get("estimated_funding_rate", {}).get("value") or 0.0
            plans, analysis = quant.build_entry_candidates(
                cfg=cfg, inst=inst, bars15=bars, cutoff=final_cutoff, hypothesis=hypothesis,
                direction=signals[hypothesis], feats=feats, ctx=ctx, snapshot_id=snapshot_id,
                account_version=acct.version, evidence_ids=ev_ids, correlation_id=corr, funding_rate=float(funding),
                synthetic=self.synthetic)
            result["reasons"] += analysis["reason_codes"]
            result["effective_samples"] = analysis.get("forecast", {}).get("effective_samples")

        with self.account_lock:  # deterministic precheck; precheck outcome is bound into the plan hash
            checked = []
            for p in plans:
                ok, why = self.risk.precheck(p, acct)
                if ok:
                    checked.append(quant.finalize_precheck(p, True))
                else:
                    result["reasons"] += why
        eligible = checked[: cfg["decision"]["max_trade_candidates"]]
        if eligible and "analysis" in self.phi_features and position == 0:
            eligible = self._analyzer_packet(snapshot_id, hypothesis, eligible, evidence, now, corr, result)
        result["candidates"] = [p["candidate_id"] for p in eligible]
        for p in eligible:
            self._plans[p["plan_sha256"]] = p
            self.ledger.append("candidate_plan", p, now, corr)
            self._graph_candidate(p, evidence, now)
        if not eligible:
            result["reasons"].append("NO_ELIGIBLE_CANDIDATE")
            return

        facts = {"synthetic": self.synthetic, "required_evidence_complete": True,
                 **{k: v["value"] for k, v in evidence.items() if usable(v)}}
        if isinstance(self.selector, PhiDecisionSelector):
            self._phi_calls_this_cycle += 1
            if self._phi_calls_this_cycle > cfg["research"]["max_phi_calls_per_cycle"]:
                raise CycleBudgetExceeded("per-cycle Phi call budget exhausted before decision")
        decision = self.selector.select(snapshot_id=snapshot_id, hypothesis=hypothesis, facts=facts, plans=eligible,
                                        equity=acct.equity, now=now, correlation_id=corr,
                                        ttl_seconds=int(q["plan_ttl_seconds"]), synthetic=self.synthetic,
                                        **({"cutoff": final_cutoff} if isinstance(self.selector, PhiDecisionSelector) else {}))
        result["selected"] = decision["selected_id"]
        result["decision_status"] = decision["validation_status"]
        if decision["validation_status"] in ("timeout", "unavailable", "invalid", "overloaded", "context_rejected"):
            self.risk.set_lock("phi_health", "NO_NEW_RISK", f"decision {decision['validation_status']}", now, "auto")
        if decision["selected_id"] == ABSTAIN:
            self.ledger.append("decision_record", decision, now, corr)
            result["reasons"] += decision["reason_codes"]
            return
        plan = next(p for p in eligible if p["candidate_id"] == decision["selected_id"])
        auth_now = now + timedelta(seconds=1)
        with self.account_lock:  # fresh authorization + gate + atomic persistence
            acct2 = self.executor.account_view(auth_now, self.quote_ts())
            auth = self.risk.authorize(plan, decision, acct2, auth_now)
            ok, why = policy.check_entry(cfg=cfg, route="autonomous_cycle", strategy=hypothesis, plan=plan,
                                         decision=decision, audit_ok=self.ledger.fail_writes == 0, now=auth_now,
                                         expected_selector=self.expected_selector,
                                         expected_backend_id=self.expected_backend_id)
            if not auth["approved"] or not ok:
                self.ledger.append("decision_record", decision, now, corr)
                self.ledger.append("risk_authorization", auth, auth_now, corr)
                result.update(status="rejected", reasons=result["reasons"] + auth["reason_codes"] + ([] if ok else [why]))
                return
            per_contract = self.risk.plan_stop_risk_per_contract(plan) if plan["risk_increasing"] else ZERO
            self._auths[auth["authorization_id"]] = auth
            intent_id = self.executor.persist_authorized_entry(plan=plan, decision=decision, auth=auth,
                                                               per_contract_risk=per_contract, now=auth_now,
                                                               correlation_id=corr, auth_equity=acct2.equity)
            if plan["risk_increasing"]:
                self.executor.remember_stop(sym, plan["stop_price"])
                self.ledger.put(f"last_entry_candidate:{sym}", plan["candidate_id"])
        ttl = self.cfg["graph"]["evidence_ttl_seconds"] * 7
        self.graph.add_node(decision["decision_id"], "decision", f"events/{decision['event_id']}", auth_now,
                            f"{decision['selector']} selected {decision['selected_id']}", decision["request_sha256"], ttl)
        self.graph.add_edge(decision["decision_id"], plan["candidate_id"], "selected", auth_now)
        self.graph.add_node(auth["authorization_id"], "policy", f"events/{auth['event_id']}", auth_now,
                            "risk authorization", None, ttl)
        self.graph.add_edge(plan["candidate_id"], auth["authorization_id"], "authorized_by", auth_now)
        result.update(status="authorized", intent_id=intent_id, authorization_id=auth["authorization_id"],
                      snapshot_id=snapshot_id, decision_id=decision["decision_id"])

    # ------------------------------------------------------------------ evidence dialogue
    def _variables(self, hypothesis):
        out = []
        replay = self.mode == "replay"
        for var, required, max_age in HYPOTHESES[hypothesis]:
            cls = self.connectors.source_class(var)
            if cls is None:  # no connector registered for this variable (e.g. on-chain ablation)
                if required:
                    out.append({"variable": var, "source_class": "venue_market_data", "required": True,
                                "max_age_seconds": max_age, "sources": [], "excluded": "no_connector"})
                continue
            sources = self.connectors.approved(var, cls, replay=replay)
            excluded = None if sources or not self.connectors.approved(var, cls) else "historical_availability_unproven"
            out.append({"variable": var, "source_class": cls, "required": required, "max_age_seconds": max_age,
                        "sources": sources, "excluded": excluded})
        return out

    def _evidence_dialogue(self, sym, inst, signals, snapshot_prelim, now, corr, result):
        registry = {h: self._variables(h) for h in HYPOTHESES}
        if "evidence" not in self.phi_features:
            hyp = next((h for h in HYPOTHESES if signals[h] != 0), None)
            if hyp is None:
                result["reasons"].append("NO_REGISTERED_SIGNAL")
                return None, None
            return hyp, self._acquire_deterministic(sym, inst, hyp, now, corr, result)
        interval_start = iso(now - timedelta(hours=4))
        payload = {"stage": "request", "snapshot_id": snapshot_prelim, "request_id": stable_id("req", snapshot_prelim),
                   "symbol": inst.symbol, "cutoff": iso(now), "interval_start": interval_start,
                   "hypotheses": [{"hypothesis_id": h, "signal_direction": signals[h],
                                   "variables": [{k: v[k] for k in ("variable", "source_class", "required",
                                                                    "max_age_seconds")} for v in registry[h]
                                                 if not v["excluded"]]} for h in HYPOTHESES]}
        out = self._phi("analyzer", payload, now, corr)
        self.ledger.append(out["kind"], out, now, corr)
        if out["kind"] != "evidence_request":
            result["reasons"] += out["reason_codes"]
            return None, None
        hyp = out["hypothesis_id"]
        if hyp not in HYPOTHESES or signals[hyp] == 0:
            result["reasons"].append("EVIDENCE_REQUEST_HYPOTHESIS_NOT_PERMITTED")
            return None, None
        reg = {v["variable"]: v for v in registry[hyp]}
        blocked = [v for v, r in reg.items() if r["required"] and r["excluded"]]
        if blocked:  # a required variable has no admissible source: abstain, never substitute
            result["reasons"] += ["REQUIRED_EVIDENCE_MISSING"] + [f"REQUIRED_VARIABLE_EXCLUDED:{v}" for v in blocked]
            result["missing"] = blocked
            return None, None
        reg = {v: r for v, r in reg.items() if not r["excluded"]}
        evidence = {}
        request = out
        for round_no in range(1 + self.cfg["research"]["max_followup_rounds"]):
            problems = self._validate_request(request, reg, inst, now)
            if problems:
                result["reasons"] += problems
                return None, None
            wl = {i["variable"]: reg[i["variable"]]["sources"] for i in request["items"]}
            plan = self._phi("screener", {"stage": "fetch", "request": {k: request[k] for k in ("request_id", "items")},
                                          "whitelist": wl}, now, corr)
            self.ledger.append("fetch_plan", plan, now, corr)
            bad = [f for f in plan["fetch"] if f["variable"] not in wl or not set(f["source_ids"]) <= set(wl[f["variable"]])]
            if bad:
                result["reasons"].append("SCREENER_REQUESTED_NON_WHITELISTED_SOURCE")
                return None, None
            self._fetch(inst, request, plan, reg, evidence, now, corr)
            ev_result = self._evidence_result(request, reg, evidence, now, corr, round_no)
            self.ledger.append("evidence_result", ev_result, now, corr)
            missing = [v for v, r in reg.items() if r["required"] and not usable(evidence.get(v, {"status": "missing",
                                                                                                 "value": None}))]
            if not missing:
                break
            if round_no >= self.cfg["research"]["max_followup_rounds"]:
                result["reasons"].append("REQUIRED_EVIDENCE_MISSING")
                result["missing"] = missing
                return None, None
            follow = self._phi("analyzer", {"stage": "followup", "snapshot_id": snapshot_prelim,
                                            "request_id": stable_id("req1", snapshot_prelim), "hypothesis_id": hyp,
                                            "symbol": inst.symbol, "cutoff": iso(now), "interval_start": interval_start,
                                            "missing_required": missing,
                                            "variables": [{k: v[k] for k in ("variable", "source_class", "required",
                                                                             "max_age_seconds")} for v in reg.values()]},
                               now, corr)
            self.ledger.append(follow["kind"], follow, now, corr)
            if follow["kind"] != "evidence_request" or follow["round"] != 1:
                result["reasons"] += ["REQUIRED_EVIDENCE_MISSING"] + list(follow.get("reason_codes", []))
                result["missing"] = missing
                return None, None
            request = follow
        return hyp, evidence

    def _validate_request(self, req, reg, inst, now) -> list[str]:
        errs = []
        for item in req["items"]:
            r = reg.get(item["variable"])
            if r is None:
                errs.append("UNREGISTERED_VARIABLE")
            elif r["excluded"]:
                errs.append(f"VARIABLE_EXCLUDED_{r['excluded'].upper()}")
            elif item["source_class"] != r["source_class"]:
                errs.append("SOURCE_CLASS_MISMATCH")
            if item["symbol"] != inst.symbol:
                errs.append("SYMBOL_OUT_OF_SCOPE")
            if parse_ts(item["interval_end"]) > now:
                errs.append("INTERVAL_AFTER_CUTOFF")
        return sorted(set(errs))

    def _fetch(self, inst, request, plan, reg, evidence, now, corr):
        budget = self.cfg["research"]["max_connector_calls_per_request"]
        calls0 = self.connectors.calls
        actions = {f["variable"]: f for f in plan["fetch"]}
        for item in request["items"]:
            var = item["variable"]
            max_age = min(item["max_age_seconds"], reg[var]["max_age_seconds"])
            cached = self.store.as_of(var, inst.contract_dict(), now, max_age, corr)
            act = actions.get(var, {"action": "decline", "source_ids": []})
            if not usable(cached) and act["action"] == "fetch" and self.connectors.calls - calls0 < budget:
                self.connectors.retrieve(var, inst.contract_dict(), now, act["source_ids"], corr)
                cached = self.store.as_of(var, inst.contract_dict(), now, max_age, corr)
            evidence[var] = cached
            if usable(cached):
                self._graph_evidence(cached)

    def _evidence_result(self, request, reg, evidence, now, corr, round_no):
        items = []
        for item in request["items"]:
            e = evidence.get(item["variable"])
            if e is None:
                continue
            avail = e["available_at"] if e["status"] != "missing" else None
            items.append({"variable": item["variable"], "evidence_id": e["evidence_id"],
                          "status": e["status"] if e["status"] in ("available", "missing", "stale", "invalidated") else "missing",
                          "source_id": e["source_id"], "source_timestamp": e["event_time"] if avail else None,
                          "observation_timestamp": e["observed_at"] if avail else None, "available_at": avail,
                          "lag_seconds": (parse_ts(e["available_at"]) - parse_ts(e["event_time"])).total_seconds()
                          if avail else None, "unit": e["unit"], "quality_flags": list(e["quality_flags"]),
                          "content_sha256": e["content_sha256"], "reason": e["reason"]})
        missing = [v for v, r in reg.items() if r["required"] and not usable(evidence.get(v, {"status": "missing",
                                                                                             "value": None}))]
        from .contracts import envelope
        rec = envelope("evidence_result", stable_id("evt_er", corr, round_no, iso(now)), corr, "host:screener_fetch",
                       iso(now), iso(now), iso(now + timedelta(minutes=5)), self.synthetic)
        rec.update({"request_id": request["request_id"], "round": round_no,
                    "status": "complete" if not missing else "incomplete", "items": items[:8],
                    "missing_required": missing[:8], "contradictions": [],
                    "reason_codes": ["REQUIRED_EVIDENCE_COMPLETE"] if not missing else ["REQUIRED_EVIDENCE_MISSING"]})
        return rec

    def _acquire_deterministic(self, sym, inst, hypothesis, now, corr, result):
        """No-Phi evidence path (baseline and position management): same registry, same PIT rules."""
        evidence = {}
        replay = self.mode == "replay"
        for var, required, max_age in HYPOTHESES[hypothesis]:
            cls = self.connectors.source_class(var)
            sources = self.connectors.approved(var, cls, replay=replay) if cls else []
            e = self.store.as_of(var, inst.contract_dict(), now, max_age, corr)
            if not usable(e) and sources:
                self.connectors.retrieve(var, inst.contract_dict(), now, sources, corr)
                e = self.store.as_of(var, inst.contract_dict(), now, max_age, corr)
            if cls is None and not required:
                continue
            evidence[var] = e
            if usable(e):
                self._graph_evidence(e)
        missing = [v for v, r, _ in HYPOTHESES[hypothesis] if r and not usable(evidence.get(v, {"status": "missing",
                                                                                              "value": None}))]
        if missing:
            result["reasons"].append("REQUIRED_EVIDENCE_MISSING")
            result["missing"] = missing
            return None
        return evidence

    def _analyzer_packet(self, snapshot_id, hypothesis, eligible, evidence, now, corr, result):
        payload = {"stage": "packet", "snapshot_id": snapshot_id, "hypothesis_id": hypothesis,
                   "tool_result_ids": [f"features_v1:{snapshot_id}", f"empirical_forecast_v1:{snapshot_id}"],
                   "effective_sample_count": eligible[0]["metrics"]["effective_sample_count"],
                   "evidence": [{"variable": k, "value": v["value"], "contradiction": False} for k, v in evidence.items()],
                   "candidates": [{"candidate_id": p["candidate_id"], "action": p["action"],
                                   "utility_lcb_quote": p["metrics"]["utility_lcb_quote"]} for p in eligible]}
        packet = self._phi("analyzer", payload, now, corr)
        self.ledger.append(packet["kind"], packet, now, corr)
        if packet["kind"] != "analysis_packet":
            result["reasons"].append("ANALYZER_WRONG_RESULT_KIND")
            return []
        ids = set(packet["candidate_ids"])
        if not ids <= {p["candidate_id"] for p in eligible}:
            result["reasons"].append("ANALYZER_INVENTED_CANDIDATE")
            return []
        return [p for p in eligible if p["candidate_id"] in ids]

    def _graph_evidence(self, e):
        self.graph.add_node(e["evidence_id"], "evidence", f"evidence/{e['evidence_id']}", parse_ts(e["available_at"]),
                            f"{e['feature']}={e['value']} {e['unit']} ({e['source_id']})", e["content_sha256"],
                            self.cfg["graph"]["evidence_ttl_seconds"])

    def _graph_candidate(self, plan, evidence, now):
        self.graph.add_node(plan["candidate_id"], "candidate", f"events/{plan['event_id']}", now,
                            f"{plan['action']} {plan['quantity_contracts']} {plan['instrument']['symbol']} "
                            f"lcb={plan['metrics']['utility_lcb_quote']}", plan["plan_sha256"],
                            self.cfg["graph"]["evidence_ttl_seconds"] * 7)
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
        pl = sorted(self.protect_latency_ms) or [0.0]
        dl = sorted(self.decision_latency_ms) or [0.0]
        pick = lambda xs, p: round(xs[min(len(xs) - 1, int(p * len(xs)))], 3)  # noqa: E731
        return {"equity": dstr(acct["equity"]), "wallet": dstr(acct["wallet"]), "risk_state": self.risk.state,
                "locks": dict(self.risk.locks), "cycles": len(self.cycle_log), "cycle_status_counts": statuses,
                "fills": len(fills), "fees_paid": dstr(sum((p.fees_paid for p in self.venue.positions.values()), ZERO)),
                "funding_paid": dstr(sum((p.funding_paid for p in self.venue.positions.values()), ZERO)),
                "outcomes": len(self.ledger.events("outcome")), "risk_watchdog_latency": self.risk.latency_summary(),
                "protect_cycle_latency_ms": {"p50": pick(pl, .5), "p99": pick(pl, .99), "max": pick(pl, 1.0)},
                "decision_cycle_latency_ms": {"p50": pick(dl, .5), "p99": pick(dl, .99), "max": pick(dl, 1.0)},
                "selector": self.expected_selector, "phi_features": sorted(self.phi_features),
                "phi": self.phi.resource_summary() if self.phi else None,
                "selector_stats": getattr(self.selector, "stats", None), "graph": self.graph.counts(),
                "admission_rejections": len(self.ledger.events("admission_rejected"))}

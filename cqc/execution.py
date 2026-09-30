"""Execution and reconciliation.

Invariants:
- an intent is persisted (status `submitting`) before any network send;
- a timeout means `unknown`: query by client order ID and reconcile before any retry;
- fills are deduplicated by venue fill ID;
- reservations are released only from confirmed venue state (a cancel request releases nothing);
- a protective replacement stop is accepted before the old stop is canceled;
- closing orders are reduce-only and bounded by the verified position (never reverse).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .accounting import Position
from .ledger import OPEN_STATUSES, Ledger
from .risk import AccountView
from .util import D, ZERO, canonical_json, dstr, iso, parse_ts, round_price, sha256_hex, stable_id
from .venue import VenueReject, VenueTimeout, VenueUnavailable
from decimal import ROUND_CEILING, ROUND_FLOOR

MAX_SUBMIT_ATTEMPTS = 2


class Executor:
    def __init__(self, ledger: Ledger, venue, instruments: dict, owner: str = "worker-1"):
        self.ledger, self.venue, self.instruments, self.owner = ledger, venue, instruments, owner
        self.token: int | None = None
        self.book = {k: Position(k, inst.multiplier) for k, inst in instruments.items()}
        self.fill_cursor = ledger.get(f"fill_cursor", 0)
        self.funding_cursor = ledger.get("funding_cursor", 0)
        self.discrepancies: list[dict] = []

    # ------------------------------------------------------------------ lifecycle
    def start(self, now: datetime) -> int:
        self.token = self.ledger.acquire_lease(self.owner, now)
        self._rebuild_book()
        self.ledger.append("worker_started", {"owner": self.owner, "token": self.token}, now, "ops")
        return self.token

    def _rebuild_book(self):
        """Local position mirror derived only from durable fills and funding (never agent memory)."""
        self.book = {k: Position(k, inst.multiplier) for k, inst in self.instruments.items()}
        for f in self.ledger.fills():
            self.book[f["symbol"]].apply_fill(f["side"], D(f["qty"]), D(f["price"]), D(f["fee"]),
                                              reduce_only=bool(f["reduce_only"]))
        for e in self.ledger.events("funding_settlement"):
            p = e["payload"]
            self.book[p["symbol"]].funding_paid += D(p["payment"])
            self.book[p["symbol"]].realized_pnl -= D(p["payment"])

    # ------------------------------------------------------------------ entry persistence (atomic)
    def persist_authorized_entry(self, *, plan: dict, receipt: dict, auth: dict, per_contract_risk, now: datetime,
                                 correlation_id: str) -> str:
        """Decision, authorization, reservation and outbox intent in one transaction."""
        inst = self.instruments[plan["instrument"]["symbol"].replace("-PERP", "")]
        side = "buy" if plan["position_side"] == "long" else "sell"
        if plan["action"] in ("REDUCE", "CLOSE"):
            side = "sell" if plan["position_side"] == "long" else "buy"
        intent_id = stable_id("int", auth["authorization_id"])
        coid = "cqc" + sha256_hex(intent_id)[:29]  # venue-compliant <=32 char client order ID
        limit = plan["price_max"] if side == "buy" else plan["price_min"]
        intent = {"intent_id": intent_id, "client_order_id": coid, "authorization_id": auth["authorization_id"],
                  "candidate_id": plan["candidate_id"], "plan_sha256": plan["plan_sha256"],
                  "purpose": "entry" if plan["risk_increasing"] else "discretionary_reduce", "symbol": inst.key,
                  "side": side, "qty": plan["quantity_contracts"], "price_limit": limit,
                  "stop_price": plan["stop_price"], "order_type": "limit", "reduce_only": int(plan["reduce_only"]),
                  "status": "queued", "expires_at": auth["expires_at"], "created_ts": iso(now), "updated_ts": iso(now)}
        reservation = None
        if plan["risk_increasing"]:
            reservation = {"reservation_id": stable_id("res", intent_id), "symbol": inst.key,
                           "stop_risk_per_contract": str(per_contract_risk),
                           "notional_per_contract": str(inst.multiplier * D(plan["price_max"])),
                           "qty_reserved": plan["quantity_contracts"]}
        with self.ledger.tx():
            self.ledger.append("decision_receipt", receipt, now, correlation_id)
            self.ledger.append("risk_authorization", auth, now, correlation_id)
            self.ledger.create_intent(intent, reservation, self.token)
            self.ledger.append("intent_created", {"intent_id": intent_id, "authorization_id": auth["authorization_id"],
                                                  "plan_sha256": plan["plan_sha256"]}, now, correlation_id)
        return intent_id

    def create_protective_intent(self, symbol: str, side: str, qty, order_type: str, now: datetime, reason: str,
                                 stop_price=None, correlation_id="protection") -> str:
        intent_id = stable_id("prot", symbol, side, str(qty), order_type, str(stop_price), iso(now), reason)
        coid = "cqp" + sha256_hex(intent_id)[:29]
        self.ledger.create_intent({"intent_id": intent_id, "client_order_id": coid, "authorization_id": None,
                                   "candidate_id": None, "plan_sha256": None, "purpose": "protective", "symbol": symbol,
                                   "side": side, "qty": dstr(qty), "price_limit": None,
                                   "stop_price": dstr(stop_price) if stop_price is not None else None,
                                   "order_type": order_type, "reduce_only": 1, "status": "queued", "expires_at": None,
                                   "created_ts": iso(now), "updated_ts": iso(now)}, None, self.token)
        self.ledger.append("protective_action", {"intent_id": intent_id, "symbol": symbol, "side": side,
                                                 "qty": dstr(qty), "type": order_type, "reason": reason,
                                                 "stop_price": dstr(stop_price) if stop_price is not None else None},
                           now, correlation_id)
        return intent_id

    # ------------------------------------------------------------------ dispatch
    def dispatch(self, now: datetime, submission_check) -> list[str]:
        sent = []
        for it in self.ledger.intents(statuses=["queued"]):
            ok, reason = submission_check(it, now)
            if not ok:
                self._terminal(it, "rejected" if reason != "AUTHORIZATION_EXPIRED" else "expired", now, reason)
                continue
            self.ledger.update_intent(it["intent_id"], self.token, now, status="submitting",
                                      submit_attempts=it["submit_attempts"] + 1)
            try:
                ack = self.venue.submit(it["client_order_id"], it["symbol"], it["side"], D(it["qty"]), it["order_type"],
                                        it["price_limit"], it["stop_price"], bool(it["reduce_only"]), now)
                self.ledger.update_intent(it["intent_id"], self.token, now, status="acknowledged",
                                          venue_order_id=ack["venue_order_id"])
                self.ledger.append("order_ack", {"intent_id": it["intent_id"], "venue_order_id": ack["venue_order_id"]},
                                   now, it["intent_id"])
                sent.append(it["intent_id"])
            except (VenueTimeout, VenueUnavailable) as exc:
                self.ledger.update_intent(it["intent_id"], self.token, now, status="unknown")
                self.ledger.append("order_unknown", {"intent_id": it["intent_id"], "error": type(exc).__name__}, now,
                                   it["intent_id"])
            except VenueReject as exc:
                self._terminal(it, "rejected", now, str(exc))
        return sent

    def _terminal(self, it: dict, status: str, now: datetime, reason: str):
        self.ledger.update_intent(it["intent_id"], self.token, now, status=status)
        # safe release: the venue confirmed there is no remaining working quantity
        self.ledger.set_reservation_qty(it["intent_id"], 0, self.token, release=True)
        self.ledger.append("order_terminal", {"intent_id": it["intent_id"], "status": status, "reason": reason[:200]},
                           now, it["intent_id"])

    def request_cancel(self, intent_id: str, now: datetime):
        it = self.ledger.intent(intent_id)
        if it is None or it["status"] not in OPEN_STATUSES:
            return
        self.ledger.update_intent(intent_id, self.token, now, status="cancel_requested")
        try:
            self.venue.cancel(it["client_order_id"])
        except VenueUnavailable:
            pass  # reservation stays until reconciliation confirms the final state

    # ------------------------------------------------------------------ reconciliation
    def reconcile(self, now: datetime) -> dict:
        new_fills = self._ingest_fills(now)
        self._ingest_funding(now)
        for it in self.ledger.intents(statuses=sorted(OPEN_STATUSES - {"queued"})):
            try:
                v = self.venue.query(it["client_order_id"])
            except VenueUnavailable:
                continue
            if v is None:
                if it["status"] in ("submitting", "unknown"):
                    # confirmed absent at the venue: safe to resubmit with the same client order ID
                    expired = it["expires_at"] and parse_ts(it["expires_at"]) <= now
                    if it["submit_attempts"] >= MAX_SUBMIT_ATTEMPTS or expired:
                        self._terminal(it, "expired" if expired else "rejected", now, "absent_at_venue")
                    else:
                        self.ledger.update_intent(it["intent_id"], self.token, now, status="queued")
                continue
            filled = D(v["filled"])
            if v["status"] == "filled":
                self.ledger.update_intent(it["intent_id"], self.token, now, status="filled", filled_qty=dstr(filled),
                                          venue_order_id=v["venue_order_id"])
                self.ledger.set_reservation_qty(it["intent_id"], 0, self.token, release=True)
            elif v["status"] in ("canceled", "rejected"):
                self.ledger.update_intent(it["intent_id"], self.token, now, filled_qty=dstr(filled),
                                          venue_order_id=v["venue_order_id"])
                self._terminal(self.ledger.intent(it["intent_id"]), v["status"], now, "venue_confirmed")
            else:
                status = "partially_filled" if filled > 0 else ("cancel_requested" if it["status"] == "cancel_requested" else "acknowledged")
                self.ledger.update_intent(it["intent_id"], self.token, now, status=status, filled_qty=dstr(filled),
                                          venue_order_id=v["venue_order_id"])
                if it["purpose"] == "entry":
                    self.ledger.set_reservation_qty(it["intent_id"], D(it["qty"]) - filled, self.token)
        self.discrepancies = self.compare_positions()
        return {"new_fills": new_fills, "discrepancies": self.discrepancies}

    def _ingest_fills(self, now: datetime) -> int:
        batch, cursor = self.venue.fills_since(self.fill_cursor)
        count = 0
        for f in batch:
            it = self.ledger.intent_by_coid(f["client_order_id"])
            row = {**f, "intent_id": it["intent_id"] if it else None}
            if not self.ledger.record_fill(row):
                continue  # duplicate delivery
            count += 1
            self.book[f["symbol"]].apply_fill(f["side"], D(f["qty"]), D(f["price"]), D(f["fee"]),
                                              reduce_only=bool(f["reduce_only"]))
            self.ledger.append("fill", {"fill_id": f["fill_id"], "intent_id": row["intent_id"], "symbol": f["symbol"],
                                        "side": f["side"], "qty": dstr(f["qty"]), "price": dstr(f["price"]),
                                        "fee": dstr(f["fee"])}, f["ts"], row["intent_id"] or "venue")
            if f["client_order_id"].startswith("liq-"):
                self.ledger.append("liquidation", {"symbol": f["symbol"], "qty": dstr(f["qty"])}, f["ts"], "risk")
        self.fill_cursor = cursor
        self.ledger.put("fill_cursor", cursor)
        return count

    def _ingest_funding(self, now: datetime):
        log = self.venue.funding_log
        for entry in log[self.funding_cursor:]:
            self.book[entry["symbol"]].funding_paid += entry["payment"]
            self.book[entry["symbol"]].realized_pnl -= entry["payment"]
            self.ledger.append("funding_settlement", {"symbol": entry["symbol"], "ts": iso(entry["ts"]),
                                                      "payment": dstr(entry["payment"]), "rate": dstr(entry["rate"]),
                                                      "contracts": dstr(entry["contracts"])}, entry["ts"], "funding")
        self.funding_cursor = len(log)
        self.ledger.put("funding_cursor", self.funding_cursor)

    def compare_positions(self) -> list[dict]:
        out = []
        for sym, pos in self.venue.positions.items():
            if pos.contracts != self.book[sym].contracts:
                out.append({"symbol": sym, "venue": dstr(pos.contracts), "local": dstr(self.book[sym].contracts)})
        return out

    # ------------------------------------------------------------------ protection
    def working_stops(self, symbol: str) -> list[dict]:
        return [it for it in self.ledger.intents(statuses=["acknowledged", "partially_filled", "queued", "submitting",
                                                            "unknown"], symbol=symbol, purpose="protective")
                if it["order_type"] == "stop_market"]

    def ensure_stop(self, symbol: str, stop_price, now: datetime) -> str | None:
        """Keep exactly one verified reduce-only stop covering the full position."""
        pos = self.book[symbol].contracts
        if pos == 0 or stop_price is None:
            return None
        qty = abs(pos)
        side = "sell" if pos > 0 else "buy"
        tick = self.instruments[symbol].price_tick
        stop_price = round_price(D(stop_price), tick, ROUND_FLOOR if pos > 0 else ROUND_CEILING)
        stops = self.working_stops(symbol)
        good = [s for s in stops if D(s["qty"]) - D(s["filled_qty"]) == qty and s["side"] == side]
        if good:
            return good[0]["intent_id"]
        new_id = self.create_protective_intent(symbol, side, qty, "stop_market", now, "protect_position", stop_price)
        self.dispatch(now, lambda it, t: (True, "OK"))
        if self.ledger.intent(new_id)["status"] == "acknowledged":
            for old in stops:  # cancel stale stops only after the replacement is accepted
                self.request_cancel(old["intent_id"], now)
            return new_id
        return None

    def protective_market(self, symbol: str, qty, now: datetime, reason: str) -> str | None:
        pos = self.book[symbol].contracts
        pending = self.pending_reductions().get(symbol, ZERO)
        qty = min(D(qty), abs(pos) - pending)
        if pos == 0 or qty <= 0:
            return None
        side = "sell" if pos > 0 else "buy"
        intent_id = self.create_protective_intent(symbol, side, qty, "market", now, reason)
        self.dispatch(now, lambda it, t: (True, "OK"))
        return intent_id

    def pending_reductions(self) -> dict:
        out = {}
        for it in self.ledger.intents(statuses=sorted(OPEN_STATUSES)):
            if it["reduce_only"] and it["order_type"] != "stop_market":
                out[it["symbol"]] = out.get(it["symbol"], ZERO) + D(it["qty"]) - D(it["filled_qty"])
        return out

    # ------------------------------------------------------------------ account view for risk
    def account_view(self, now: datetime, quote_ts: dict) -> AccountView:
        acct = self.venue.account()
        marks = {s: self.venue.mark(s) or ZERO for s in self.instruments}
        stops = {}
        for sym in self.instruments:
            verified = [s for s in self.working_stops(sym) if s["status"] in ("acknowledged", "partially_filled")]
            stops[sym] = D(verified[0]["stop_price"]) if verified else None
        reservations = self.ledger.active_reservations()
        open_intents = [(i["intent_id"], i["status"], i["filled_qty"]) for i in self.ledger.intents(statuses=sorted(OPEN_STATUSES))]
        positions = {s: {"contracts": self.book[s].contracts, "entry_price": self.book[s].entry_price,
                         "liquidation_price": acct["positions"][s]["liquidation_price"]} for s in self.instruments}
        version = "acct_" + sha256_hex(canonical_json({
            "pos": {s: dstr(p["contracts"]) for s, p in positions.items()}, "open": open_intents,
            "res": sorted((r["reservation_id"], r["qty_reserved"]) for r in reservations),
            "fills": self.fill_cursor}))[:20]
        entry_ts = self.ledger.get("entry_ts", {}) or {}
        return AccountView(ts=acct["ts"] or now, equity=acct["equity"], wallet=acct["wallet"], positions=positions,
                           marks=marks, quote_ts=quote_ts, stops=stops, reservations=reservations,
                           pending_reductions=self.pending_reductions(), version=version,
                           entry_ts={k: parse_ts(v) for k, v in entry_ts.items()})

    def track_entry_times(self, now: datetime):
        entry_ts = self.ledger.get("entry_ts", {}) or {}
        changed = False
        for sym, pos in self.book.items():
            if pos.contracts != 0 and sym not in entry_ts:
                entry_ts[sym], changed = iso(now), True
            elif pos.contracts == 0 and sym in entry_ts:
                del entry_ts[sym]
                changed = True
        if changed:
            self.ledger.put("entry_ts", entry_ts)

    def stop_for(self, symbol: str):
        return self.ledger.get(f"stop:{symbol}")

    def remember_stop(self, symbol: str, stop_price):
        self.ledger.put(f"stop:{symbol}", dstr(stop_price) if stop_price is not None else None)

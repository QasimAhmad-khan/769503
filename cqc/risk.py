"""Deterministic risk engine. No model call is ever on this path.

State = most severe active lock (HALTED > RECOVERY > REDUCE_ONLY > NO_NEW_RISK > NORMAL).
Each lock records its reason and clear policy: `auto` (health; clears after fresh checks and a
stable interval), `daily_reset` (clears at the next 00:00 UTC reference) or `operator`
(explicit reset). A healthy model never clears a drawdown, integrity or operator lock.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import contracts
from .accounting import notional
from .ledger import AuditWriteError
from .market import daily_reference_time
from .util import D, ZERO, dstr, iso, parse_ts, plan_hash, stable_id

SEVERITY = {"NORMAL": 0, "NO_NEW_RISK": 1, "REDUCE_ONLY": 2, "RECOVERY": 3, "HALTED": 4}


@dataclass
class AccountView:
    """Reconciled account facts the risk engine consumes (venue truth + durable reservations)."""
    ts: datetime
    equity: object
    wallet: object
    positions: dict          # symbol -> {"contracts", "entry_price", "liquidation_price"}
    marks: dict              # symbol -> Decimal mark
    quote_ts: dict           # symbol -> datetime of last quote
    stops: dict              # symbol -> Decimal protective stop price (verified working) or None
    reservations: list       # active reservations
    pending_reductions: dict  # symbol -> Decimal contracts in open reduce-only discretionary orders
    version: str
    entry_ts: dict = field(default_factory=dict)  # symbol -> datetime position opened


class RiskEngine:
    def __init__(self, cfg, ledger, instruments):
        self.cfg, self.ledger, self.instruments = cfg, ledger, instruments
        self.r = cfg["paper_risk"]
        self.locks: dict = ledger.get("risk_locks", {}) or {}
        self.healthy_since: dict = {}
        self.stable_seconds = 60
        self.latencies_ms: list[float] = []
        self.unlogged_transitions: list[dict] = []
        if ledger.get("hwm") is None:
            ledger.put("hwm", self.r["starting_equity_usdt"])

    # ------------------------------------------------------------------ state machine
    @property
    def state(self) -> str:
        if not self.locks:
            return "NORMAL"
        return max((lock["state"] for lock in self.locks.values()), key=SEVERITY.get)

    def _transition(self, before: str, now: datetime, trigger: str, detail: str):
        after = self.state
        if after != before:
            rec = contracts.envelope("risk_event", stable_id("evt_risk", trigger, iso(now), before, after), "risk",
                                     "risk_engine", iso(now), iso(now), iso(now + timedelta(days=1)))
            rec.update({"from_state": before, "to_state": after, "trigger": trigger[:64], "detail": detail[:256]})
            try:
                self.ledger.append("risk_event", rec, now, "risk")
            except AuditWriteError:
                # the restrictive state still applies in memory; the missing audit record is itself a lock
                self.unlogged_transitions.append(rec)
                if "audit_failure" not in self.locks:
                    self.locks["audit_failure"] = {"state": "NO_NEW_RISK", "reason": "risk_event not persisted",
                                                   "clear": "auto", "since": iso(now)}
        self.ledger.put("risk_locks", self.locks)

    def set_lock(self, name: str, state: str, reason: str, now: datetime, clear: str = "auto"):
        existing = self.locks.get(name)
        if existing and existing["state"] == state:
            return
        before = self.state
        self.locks[name] = {"state": state, "reason": reason, "clear": clear, "since": iso(now)}
        self.healthy_since.pop(name, None)
        self._transition(before, now, name, reason)

    def clear_lock(self, name: str, now: datetime, operator: bool = False) -> bool:
        lock = self.locks.get(name)
        if lock is None:
            return True
        if lock["clear"] == "operator" and not operator:
            return False
        if lock["clear"] == "daily_reset" and not operator and daily_reference_time(now) <= parse_ts(lock["since"]):
            return False
        before = self.state
        del self.locks[name]
        self._transition(before, now, f"clear:{name}", "operator reset" if operator else "conditions cleared")
        return True

    def _health(self, name: str, bad: bool, now: datetime, state: str, reason: str):
        """Auto-clearing health lock with a stable-interval requirement before clearing."""
        if bad:
            self.set_lock(name, state, reason, now, "auto")
            return
        if name in self.locks and self.locks[name]["clear"] == "auto":
            first_ok = self.healthy_since.setdefault(name, now)
            if (now - first_ok).total_seconds() >= self.stable_seconds:
                self.clear_lock(name, now)

    def operator_halt(self, now: datetime, reason: str = "operator kill switch"):
        self.set_lock("operator_halt", "HALTED", reason, now, "operator")

    # ------------------------------------------------------------------ watchdog
    def watchdog(self, acct: AccountView, now: datetime) -> list[dict]:
        """Evaluate account risk; return deterministic protective actions. Measures reaction latency."""
        t0 = time.perf_counter()
        actions = []
        equity = D(acct.equity)
        max_quote_age = self.r["max_core_quote_age_seconds"]
        stale = [s for s, ts in acct.quote_ts.items() if ts is None or (now - ts).total_seconds() > max_quote_age]
        self._health("stale_feed", bool(stale), now, "NO_NEW_RISK", f"stale quotes: {stale}")
        self._health("stale_account", (now - acct.ts).total_seconds() > self.r["max_account_snapshot_age_seconds"],
                     now, "NO_NEW_RISK", "account snapshot too old")

        # daily loss against the 00:00 UTC reference (equity incl. realized/unrealized, fees, funding)
        day = iso(daily_reference_time(now))
        ref = self.ledger.get("daily_ref")
        if ref is None or ref["day"] != day:
            ref = {"day": day, "equity": dstr(equity)}
            self.ledger.put("daily_ref", ref)
            if "daily_loss" in self.locks:
                self.clear_lock("daily_loss", now)
        if equity <= D(ref["equity"]) * (1 - D(repr(self.r["daily_loss_fraction_stop_new_risk"]))):
            self.set_lock("daily_loss", "NO_NEW_RISK", f"equity {dstr(equity)} below daily limit", now, "daily_reset")

        hwm = max(D(self.ledger.get("hwm")), equity)
        self.ledger.put("hwm", dstr(hwm))
        if equity <= hwm * (1 - D(repr(self.r["peak_drawdown_fraction_reduce_only"]))):
            self.set_lock("drawdown", "REDUCE_ONLY", f"drawdown from HWM {dstr(hwm)}", now, "operator")

        max_symbol = equity * D(repr(self.r["max_symbol_notional_over_equity"]))
        max_hold = self.cfg["market"]["max_holding_seconds"]
        for sym, pos in acct.positions.items():
            n = D(pos["contracts"])
            if n == 0:
                continue
            inst = self.instruments[sym]
            mark = acct.marks[sym]
            avail = abs(n) - acct.pending_reductions.get(sym, ZERO)
            if self.state in ("REDUCE_ONLY",) and "drawdown" in self.locks:
                actions.append({"type": "close", "symbol": sym, "qty": avail, "reason": "drawdown_reduce_only"})
                continue
            opened = acct.entry_ts.get(sym)
            if opened is not None and (now - opened).total_seconds() >= max_hold:
                actions.append({"type": "close", "symbol": sym, "qty": avail, "reason": "max_holding_time"})
                continue
            sym_notional = notional(abs(n), inst.multiplier, mark)
            if sym_notional > max_symbol * D("1.10"):  # hysteresis band before a forced reduction
                excess = (sym_notional - max_symbol) / (inst.multiplier * mark)
                qty = min(inst.floor_qty(excess) + inst.qty_step, avail)
                if qty > 0:
                    actions.append({"type": "reduce", "symbol": sym, "qty": qty, "reason": "symbol_notional_breach"})
            liq = pos.get("liquidation_price")
            if liq is not None and mark > 0 and abs(mark - D(liq)) / mark < D("0.03"):
                actions.append({"type": "reduce", "symbol": sym, "qty": inst.floor_qty(avail / 2) or avail,
                                "reason": "liquidation_buffer"})
            if acct.stops.get(sym) is None:
                actions.append({"type": "ensure_stop", "symbol": sym, "qty": abs(n), "reason": "unprotected_position"})
        self.latencies_ms.append((time.perf_counter() - t0) * 1000)
        return actions

    # ------------------------------------------------------------------ budgets
    def exposure(self, acct: AccountView) -> dict:
        equity = D(acct.equity)
        gross, per_symbol, open_stop_risk = ZERO, {}, ZERO
        for sym, pos in acct.positions.items():
            n = abs(D(pos["contracts"]))
            if n == 0:
                continue
            inst, mark = self.instruments[sym], acct.marks[sym]
            value = notional(n, inst.multiplier, mark)
            gross += value
            per_symbol[sym] = per_symbol.get(sym, ZERO) + value
            stop = acct.stops.get(sym)
            # unprotected positions count their full stress notional against the stop-risk budget
            open_stop_risk += n * inst.multiplier * abs(mark - stop) if stop is not None else value * D("0.10")
        reserved_risk = ZERO
        for res in acct.reservations:
            qty = D(res["qty_reserved"])
            reserved_risk += qty * D(res["stop_risk_per_contract"])
            value = qty * D(res["notional_per_contract"])
            gross += value
            per_symbol[res["symbol"]] = per_symbol.get(res["symbol"], ZERO) + value
        return {"equity": equity, "gross": gross, "per_symbol": per_symbol, "stop_risk": open_stop_risk + reserved_risk}

    def sizing_rooms(self, acct: AccountView, symbol: str) -> dict:
        exp = self.exposure(acct)
        eq = exp["equity"]
        f = lambda k: D(repr(self.r[k]))  # noqa: E731
        margin_used = sum((abs(D(p["contracts"])) * self.instruments[s].multiplier * acct.marks[s]
                           for s, p in acct.positions.items() if D(p["contracts"])), ZERO) / D(self.r["max_venue_leverage_setting"])
        return {"per_trade_budget": eq * f("max_equity_fraction_at_stop_per_new_trade"),
                "aggregate_room": eq * f("max_aggregate_reserved_stop_risk_fraction") - exp["stop_risk"],
                "exposure_room": eq * f("max_gross_notional_over_equity") - exp["gross"],
                "symbol_room": eq * f("max_symbol_notional_over_equity") - exp["per_symbol"].get(symbol, ZERO),
                "margin_room": (eq - margin_used) * D(self.r["max_venue_leverage_setting"]),
                "stress_room": eq * D("0.02") - exp["gross"] * D("0.10")}

    def plan_stop_risk_per_contract(self, plan: dict) -> object:
        inst = self.instruments[_key(plan)]
        entry = D(plan["price_max"]) if plan["position_side"] == "long" else D(plan["price_min"])
        stress = entry * D(self.cfg["venue_sim"]["stress_slippage_bps"]) / D(10000)
        fees = inst.multiplier * entry * D(self.cfg["venue_sim"]["taker_fee_rate"]) * 2
        return inst.multiplier * (abs(entry - D(plan["stop_price"])) + stress) + fees

    def precheck(self, plan: dict, acct: AccountView) -> tuple[bool, list[str]]:
        reasons = []
        if plan["risk_increasing"]:
            if self.state != "NORMAL":
                reasons.append(f"STATE_{self.state}")
            if plan["stop_price"] is None:
                reasons.append("NO_STOP")
            else:
                inst = self.instruments[_key(plan)]
                qty = D(plan["quantity_contracts"])
                rooms = self.sizing_rooms(acct, _key(plan))
                risk = qty * self.plan_stop_risk_per_contract(plan)
                value = qty * inst.multiplier * D(plan["price_max"])
                if risk > rooms["per_trade_budget"] * D("1.0001"):
                    reasons.append("PER_TRADE_STOP_RISK")
                if risk > rooms["aggregate_room"]:
                    reasons.append("AGGREGATE_STOP_RISK")
                if value > rooms["exposure_room"]:
                    reasons.append("GROSS_EXPOSURE")
                if value > rooms["symbol_room"]:
                    reasons.append("SYMBOL_CONCENTRATION")
                if value > rooms["margin_room"]:
                    reasons.append("MARGIN")
        else:
            if self.state == "HALTED":
                reasons.append("STATE_HALTED")
            pos = D(acct.positions.get(_key(plan), {}).get("contracts", 0))
            avail = abs(pos) - acct.pending_reductions.get(_key(plan), ZERO)
            if pos == 0 or D(plan["quantity_contracts"]) > avail:
                reasons.append("REDUCTION_EXCEEDS_VERIFIED_POSITION")
            if (pos > 0 and plan["position_side"] != "long") or (pos < 0 and plan["position_side"] != "short"):
                reasons.append("POSITION_SIDE_MISMATCH")
        return not reasons, reasons

    def authorize(self, plan: dict, receipt: dict, acct: AccountView, now: datetime) -> dict:
        """Fresh authorization bound to plan hash, decision, account version and policy version."""
        reasons = []
        if plan_hash(plan) != plan["plan_sha256"]:
            reasons.append("PLAN_HASH_MISMATCH")
        if receipt["validation_status"] != "valid" or receipt["selected_id"] != plan["candidate_id"]:
            reasons.append("DECISION_NOT_SELECTING_PLAN")
        if receipt["snapshot_id"] != plan["snapshot_id"]:
            reasons.append("DECISION_SNAPSHOT_MISMATCH")
        if parse_ts(receipt["expires_at"]) <= now or parse_ts(plan["expires_at"]) <= now:
            reasons.append("EXPIRED")
        if plan["account_state_version"] != acct.version:
            reasons.append("ACCOUNT_VERSION_CHANGED")
        if plan["policy_version"] != self.cfg["autonomous_policy"]:
            reasons.append("POLICY_VERSION_MISMATCH")
        if not plan["risk_precheck_passed"]:
            reasons.append("PRECHECK_NOT_PASSED")
        ok, pre = self.precheck(plan, acct)
        reasons += pre
        inst = self.instruments[_key(plan)]
        per_contract_risk = self.plan_stop_risk_per_contract(plan) if plan["risk_increasing"] else ZERO
        auth_id = stable_id("auth", plan["plan_sha256"], receipt["decision_id"], acct.version)
        rec = contracts.envelope("risk_authorization", stable_id("evt", auth_id), plan["correlation_id"], "risk_engine",
                                 iso(now), iso(now), min(plan["expires_at"], receipt["expires_at"]), plan["synthetic"])
        qty = D(plan["quantity_contracts"])
        rec.update({"authorization_id": auth_id, "candidate_id": plan["candidate_id"], "plan_sha256": plan["plan_sha256"],
                    "decision_id": receipt["decision_id"], "account_state_version": acct.version,
                    "policy_version": plan["policy_version"], "approved": not reasons,
                    "max_quantity_contracts": plan["quantity_contracts"], "price_min": plan["price_min"],
                    "price_max": plan["price_max"], "reserved_stop_risk_quote": dstr(qty * per_contract_risk),
                    "reserved_notional_quote": dstr(qty * inst.multiplier * D(plan["price_max"])) if plan["risk_increasing"] else "0",
                    "reason_codes": (reasons or ["APPROVED"])[:8]})
        if parse_ts(rec["expires_at"]) <= now:
            rec["expires_at"] = iso(now + timedelta(seconds=1))
        return contracts.validate(rec)

    def submission_check(self, intent: dict, now: datetime) -> tuple[bool, str]:
        """Revalidated immediately before network send (state may have changed since authorization)."""
        if intent["purpose"] == "entry":
            if self.state != "NORMAL":
                return False, f"STATE_{self.state}"
            if intent["expires_at"] and parse_ts(intent["expires_at"]) <= now:
                return False, "AUTHORIZATION_EXPIRED"
        elif intent["purpose"] == "discretionary_reduce" and self.state == "HALTED":
            return False, "STATE_HALTED"
        return True, "OK"

    def latency_summary(self) -> dict:
        if not self.latencies_ms:
            return {}
        xs = sorted(self.latencies_ms)
        pick = lambda p: xs[min(len(xs) - 1, int(p * len(xs)))]  # noqa: E731
        return {"count": len(xs), "p50_ms": round(pick(0.50), 4), "p95_ms": round(pick(0.95), 4),
                "p99_ms": round(pick(0.99), 4), "max_ms": round(xs[-1], 4),
                "target_p99_ms": self.r["risk_reaction_p99_target_ms"]}


def _key(plan: dict) -> str:
    return plan["instrument"]["symbol"].replace("-PERP", "")

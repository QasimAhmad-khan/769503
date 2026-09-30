"""Hard safety invariants checked on every run (any violation = FAIL, regardless of PnL).

Per minute: gross and per-symbol exposure within caps (+ watchdog hysteresis and one tick of mark drift),
every open position has a verified protective stop within a grace period, local positions reconcile
with the venue within a grace period. Post-run: no entry order acknowledged outside NORMAL, every
approved authorization's reserved stop risk within the per-trade budget, no fill ever reversed a
position, no liquidation.
"""
from __future__ import annotations

from ..util import D, ZERO, parse_ts


class InvariantMonitor:
    def __init__(self, runtime, grace_minutes: int = 3):
        self.rt = runtime
        self.r = runtime.cfg["paper_risk"]
        self.grace = grace_minutes
        self.violations: list[dict] = []
        self.state_at: dict[str, str] = {}
        self._unprotected: dict[str, int] = {}
        self._mismatch = 0
        self._over: dict[str, int] = {}
        self.minutes = 0

    def _v(self, t, kind, detail):
        self.violations.append({"ts": t.isoformat(), "invariant": kind, "detail": str(detail)[:200]})

    def __call__(self, rt, t):
        self.minutes += 1
        self.state_at[t.replace(second=0).isoformat()] = rt.risk.state
        acct = rt.executor.account_view(t, rt.quote_ts())
        eq = D(acct.equity)
        gross = ZERO
        for sym, pos in acct.positions.items():
            n = abs(D(pos["contracts"]))
            if n == 0:
                self._unprotected.pop(sym, None)
                self._over.pop(sym, None)
                continue
            val = n * rt.instruments[sym].multiplier * acct.marks[sym]
            gross += val
            cap = eq * D(repr(self.r["max_symbol_notional_over_equity"])) * D("1.10") * D("1.02")
            self._over[sym] = self._over.get(sym, 0) + 1 if val > cap else 0
            if self._over[sym] > self.grace:
                self._v(t, "SYMBOL_NOTIONAL_CAP", f"{sym} {val} > {cap}")
            self._unprotected[sym] = self._unprotected.get(sym, 0) + 1 if acct.stops.get(sym) is None else 0
            if self._unprotected[sym] > self.grace:
                self._v(t, "UNPROTECTED_POSITION", sym)
        if gross > eq * D(repr(self.r["max_gross_notional_over_equity"])) * D("1.12"):
            self._v(t, "GROSS_NOTIONAL_CAP", f"{gross} > cap")
        self._mismatch = self._mismatch + 1 if rt.executor.compare_positions() else 0
        if self._mismatch > self.grace:
            self._v(t, "UNRESOLVED_RECONCILIATION", rt.executor.compare_positions())

    def finalize(self) -> dict:
        rt = self.rt
        intents = {i["intent_id"]: i for i in rt.ledger.intents()}
        for e in rt.ledger.events("order_ack"):
            it = intents.get(e["payload"]["intent_id"])
            if it and it["purpose"] == "entry":
                minute = parse_ts(e["ts"]).replace(second=0).isoformat()
                st = self.state_at.get(minute)
                if st not in (None, "NORMAL"):
                    self._v(parse_ts(e["ts"]), "ENTRY_SENT_OUTSIDE_NORMAL", st)
        for e in rt.ledger.events("risk_authorization"):
            a = e["payload"]
            it = next((i for i in intents.values() if i["authorization_id"] == a["authorization_id"]), None)
            if a["approved"] and it and it["auth_equity"]:
                budget = D(it["auth_equity"]) * D(repr(self.r["max_equity_fraction_at_stop_per_new_trade"]))
                if D(a["reserved_stop_risk_quote"]) > budget * D("1.0001"):
                    self._v(parse_ts(e["ts"]), "PER_TRADE_STOP_RISK", f"{a['reserved_stop_risk_quote']} > {budget}")
        for sym, pos in rt.venue.positions.items():
            running = ZERO
            for side, qty, _p, _f in pos.history:
                new = running + (qty if side == "buy" else -qty)
                if running != 0 and new != 0 and (new > 0) != (running > 0):
                    self._v(rt.venue.now, "POSITION_REVERSAL", sym)
                running = new
        liq = rt.ledger.events("liquidation")
        for e in liq:
            self._v(parse_ts(e["ts"]), "LIQUIDATION", e["payload"])
        return {"minutes_checked": self.minutes, "violations": self.violations, "passed": not self.violations,
                "liquidations": len(liq)}

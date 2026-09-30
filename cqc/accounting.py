"""Linear USDT perpetual accounting in Decimal.

    q_base = n * m ; gross_PnL = s * q_base * (P_exit - P_entry)
    net_PnL = gross_PnL - fees - signed_funding_payment   (funding received is a negative payment)

Fees are charged on notional once; leverage never multiplies fees.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .util import D, ZERO


class AccountingError(ValueError):
    pass


def side_sign(side: str) -> int:
    return {"long": 1, "buy": 1, "short": -1, "sell": -1}[side]


def notional(contracts, multiplier, price):
    return D(contracts) * D(multiplier) * D(price)


def fee(contracts, multiplier, price, rate):
    return notional(contracts, multiplier, price) * D(rate)


def funding_payment(signed_contracts, multiplier, mark, rate):
    """Positive result = the position pays. Longs pay positive funding rates."""
    return D(signed_contracts) * D(multiplier) * D(mark) * D(rate)


def mmr_for(notional_quote, tiers):
    for tier in tiers:
        if D(notional_quote) <= D(tier["max_notional_quote"]):
            return D(tier["mmr"]), D(tier["maintenance_amount_quote"])
    raise AccountingError("notional exceeds configured maintenance-margin tiers")


def maintenance_margin(notional_quote, tiers):
    rate, amount = mmr_for(notional_quote, tiers)
    return D(notional_quote) * rate - amount


@dataclass
class Position:
    """One-way position. `contracts` is signed: + long, - short."""
    symbol: str
    multiplier: object
    contracts: object = ZERO
    entry_price: object = ZERO
    realized_pnl: object = ZERO
    fees_paid: object = ZERO
    funding_paid: object = ZERO
    isolated_margin: object = ZERO
    history: list = field(default_factory=list)

    @property
    def side(self):
        return None if self.contracts == 0 else ("long" if self.contracts > 0 else "short")

    def unrealized(self, mark):
        return self.contracts * self.multiplier * (D(mark) - self.entry_price)

    def apply_fill(self, side: str, contracts, price, fee_quote, reduce_only: bool = False, leverage=D(1)):
        """Apply a fill. A reduce-only fill may never flip the position (enforced here too)."""
        qty, price, fee_quote = D(contracts), D(price), D(fee_quote)
        if qty <= 0:
            raise AccountingError("fill quantity must be positive")
        signed = qty * side_sign(side)
        self.fees_paid += fee_quote
        self.realized_pnl -= fee_quote
        if self.contracts == 0 or (self.contracts > 0) == (signed > 0):
            if reduce_only:
                raise AccountingError("reduce-only fill would increase or open a position")
            new = self.contracts + signed
            self.entry_price = (self.entry_price * abs(self.contracts) + price * qty) / abs(new)
            self.contracts = new
            self.isolated_margin += notional(qty, self.multiplier, price) / D(leverage)
        else:
            if qty > abs(self.contracts):
                raise AccountingError("fill would reverse the position; closing orders never reverse")
            closed_fraction = qty / abs(self.contracts)
            pnl = (1 if self.contracts > 0 else -1) * qty * self.multiplier * (price - self.entry_price)
            self.realized_pnl += pnl
            self.isolated_margin -= self.isolated_margin * closed_fraction
            self.contracts += signed
            if self.contracts == 0:
                self.entry_price = ZERO
                self.isolated_margin = ZERO
        self.history.append((side, qty, price, fee_quote))

    def settle_funding(self, mark, rate):
        pay = funding_payment(self.contracts, self.multiplier, mark, rate)
        self.funding_paid += pay
        self.realized_pnl -= pay
        return pay

    def liquidation_price(self, tiers):
        """Isolated-margin liquidation mark: margin + uPnL == maintenance margin (tiered, linear)."""
        if self.contracts == 0:
            return None
        q = abs(self.contracts) * self.multiplier
        s = 1 if self.contracts > 0 else -1
        rate, amount = mmr_for(q * self.entry_price, tiers)
        # margin + s*q*(L - E) = q*L*rate - amount  =>  L = (margin + amount - s*q*E) / (q*rate - s*q)
        denom = q * rate - s * q
        if denom == 0:
            return None
        price = (self.isolated_margin + amount - s * q * self.entry_price) / denom
        return max(price, ZERO)

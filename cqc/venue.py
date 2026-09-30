"""Simulated linear-perpetual venue (the paper account's source of truth).

Orders placed after a decision execute only against *subsequent* 1-minute bars. Fills are
liquidity-capped (partial fills), stops gap through with stress slippage, funding settles on
the venue schedule and isolated positions liquidate at tiered maintenance margin. Fault
flags reproduce ack loss, rejects, outages and duplicate fill delivery.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .accounting import Position, fee as fee_of
from .util import D, ZERO, iso, round_price, stable_id
from .market import funding_times


class VenueTimeout(RuntimeError):
    """The request may or may not have reached the venue."""


class VenueReject(RuntimeError):
    pass


class VenueUnavailable(RuntimeError):
    pass


@dataclass
class VenueOrder:
    client_order_id: str
    venue_order_id: str
    symbol: str
    side: str
    qty: object
    order_type: str  # limit | market | stop_market
    limit_price: object
    stop_price: object
    reduce_only: bool
    created: datetime
    filled: object = ZERO
    status: str = "open"  # open | filled | canceled | rejected

    def view(self):
        return {"client_order_id": self.client_order_id, "venue_order_id": self.venue_order_id, "symbol": self.symbol,
                "side": self.side, "qty": self.qty, "filled": self.filled, "status": self.status,
                "order_type": self.order_type, "reduce_only": self.reduce_only, "stop_price": self.stop_price}


class SimulatedVenue:
    def __init__(self, instruments: dict, venue_cfg, starting_balance, leverage=2):
        self.instruments = instruments
        self.cfg = venue_cfg
        self.taker = D(venue_cfg["taker_fee_rate"])
        self.max_fill_frac = float(venue_cfg["max_fill_fraction_of_bar_volume"])
        self.stress = D(venue_cfg["stress_slippage_bps"]) / D(10000)
        self.tiers = venue_cfg["maintenance_margin_tiers"]
        self.funding_hours = int(venue_cfg["funding_interval_hours"])
        self.leverage = D(leverage)
        self.starting_balance = D(starting_balance)
        self.positions = {k: Position(k, inst.multiplier) for k, inst in instruments.items()}
        self.orders: dict[str, VenueOrder] = {}
        self.fill_log: list[dict] = []
        self.funding_log: list[dict] = []
        self.last_bar: dict = {}
        self.now: datetime | None = None
        self._seq = 0
        # fault injection
        self.drop_next_ack = 0
        self.reject_next = 0
        self.down = False
        self.duplicate_fill_delivery = False

    # ------------------------------------------------------------------ account truth
    @property
    def wallet(self):
        return self.starting_balance + sum((p.realized_pnl for p in self.positions.values()), ZERO)

    def mark(self, symbol):
        bar = self.last_bar.get(symbol)
        return D(repr(bar.mark)) if bar else None

    def account(self) -> dict:
        upnl = sum((p.unrealized(self.mark(s)) for s, p in self.positions.items() if p.contracts and self.mark(s)), ZERO)
        return {"wallet": self.wallet, "equity": self.wallet + upnl, "ts": self.now,
                "positions": {s: {"contracts": p.contracts, "entry_price": p.entry_price,
                                  "liquidation_price": p.liquidation_price(self.tiers)}
                              for s, p in self.positions.items()}}

    # ------------------------------------------------------------------ order API
    def _check_up(self):
        if self.down:
            raise VenueUnavailable("venue unavailable")

    def submit(self, client_order_id, symbol, side, qty, order_type, limit_price=None, stop_price=None,
               reduce_only=False, now=None) -> dict:
        self._check_up()
        if client_order_id in self.orders:  # duplicate client order ID: idempotent, returns the original
            return self.orders[client_order_id].view()
        inst = self.instruments[symbol]
        qty = D(qty)
        if self.reject_next:
            self.reject_next -= 1
            raise VenueReject("injected reject")
        if qty <= 0 or qty % inst.qty_step != 0 or qty < inst.min_contracts or qty > inst.max_contracts:
            raise VenueReject("invalid quantity precision or size")
        for price in (limit_price, stop_price):
            if price is not None and D(price) % inst.price_tick != 0:
                raise VenueReject("invalid price precision")
        self._seq += 1
        order = VenueOrder(client_order_id, f"v{self._seq:08d}", symbol, side, qty, order_type,
                           D(limit_price) if limit_price is not None else None,
                           D(stop_price) if stop_price is not None else None, bool(reduce_only), now or self.now)
        self.orders[client_order_id] = order
        if self.drop_next_ack:
            self.drop_next_ack -= 1
            raise VenueTimeout("ack lost after the order was accepted")
        return order.view()

    def cancel(self, client_order_id) -> dict | None:
        self._check_up()
        order = self.orders.get(client_order_id)
        if order is None:
            return None
        if order.status == "open":
            order.status = "canceled"
        return order.view()

    def query(self, client_order_id) -> dict | None:
        self._check_up()
        order = self.orders.get(client_order_id)
        return order.view() if order else None

    def open_orders(self, symbol=None) -> list:
        return [o.view() for o in self.orders.values() if o.status == "open" and (symbol is None or o.symbol == symbol)]

    def fills_since(self, cursor: int):
        batch = self.fill_log[cursor:]
        if self.duplicate_fill_delivery and batch:
            batch = batch + batch[:1]
        return batch, len(self.fill_log)

    # ------------------------------------------------------------------ matching
    def _fill(self, order: VenueOrder, qty, price, ts):
        pos = self.positions[order.symbol]
        if order.reduce_only:
            # Reduce-only can only shrink the position on the opposite side; never reverse.
            closing = (pos.contracts > 0 and order.side == "sell") or (pos.contracts < 0 and order.side == "buy")
            qty = min(qty, abs(pos.contracts)) if closing else ZERO
            if qty <= 0:
                order.status = "canceled"
                return
        inst = self.instruments[order.symbol]
        price = round_price(price, inst.price_tick)
        fee_quote = fee_of(qty, inst.multiplier, price, self.taker)
        pos.apply_fill(order.side, qty, price, fee_quote, reduce_only=order.reduce_only, leverage=self.leverage)
        order.filled += qty
        if order.filled >= order.qty:
            order.status = "filled"
        fill = {"fill_id": stable_id("fill", order.venue_order_id, len(self.fill_log)), "client_order_id":
                order.client_order_id, "venue_order_id": order.venue_order_id, "symbol": order.symbol,
                "side": order.side, "qty": qty, "price": price, "fee": fee_quote, "ts": ts,
                "reduce_only": order.reduce_only}
        self.fill_log.append(fill)
        if pos.contracts == 0:  # flat: remaining reduce-only orders for this symbol are now void
            for other in self.orders.values():
                if other.symbol == order.symbol and other.reduce_only and other.status == "open":
                    other.status = "canceled"

    def step(self, bar):
        """Advance one completed 1-minute bar for `bar.symbol`: match, settle funding, liquidate."""
        previous = self.last_bar.get(bar.symbol)
        self.now = bar.end
        inst = self.instruments[bar.symbol]
        half = D(repr((bar.ask - bar.bid) / 2))
        o, hi, lo = D(repr(bar.open)), D(repr(bar.high)), D(repr(bar.low))
        liquidity = D(repr(bar.volume_base * self.max_fill_frac)) / inst.multiplier
        for order in [x for x in self.orders.values() if x.symbol == bar.symbol and x.status == "open"]:
            if order.created is not None and order.created >= bar.start + (bar.end - bar.start):
                continue  # placed after this bar closed
            if order.created is not None and order.created > bar.start:
                continue  # executes from the next bar that starts after placement
            remaining = order.qty - order.filled
            if order.order_type == "stop_market":
                triggered = (order.side == "sell" and lo <= order.stop_price) or (order.side == "buy" and hi >= order.stop_price)
                if triggered:
                    ref = min(o, order.stop_price) if order.side == "sell" else max(o, order.stop_price)
                    px = ref * (1 - self.stress) if order.side == "sell" else ref * (1 + self.stress)
                    self._fill(order, remaining, px, bar.end)
                continue
            if order.order_type == "market":
                px = (o - half) * (1 - self.stress) if order.side == "sell" else (o + half) * (1 + self.stress)
                self._fill(order, remaining, px, bar.end)
                continue
            # marketable limit: fill at the open quote if inside the limit, else at the limit if touched
            exec_px = o + half if order.side == "buy" else o - half
            touched = (lo + half <= order.limit_price) if order.side == "buy" else (hi - half >= order.limit_price)
            if (order.side == "buy" and exec_px <= order.limit_price) or (order.side == "sell" and exec_px >= order.limit_price):
                px = exec_px
            elif touched:
                px = order.limit_price
            else:
                continue
            qty = min(remaining, inst.floor_qty(liquidity))
            if qty > 0:
                liquidity -= qty
                self._fill(order, qty, px, bar.end)
        self.last_bar[bar.symbol] = bar
        if previous is not None:
            for t in funding_times(previous.end, bar.end, self.funding_hours):
                pos = self.positions[bar.symbol]
                if pos.contracts:
                    pay = pos.settle_funding(D(repr(bar.mark)), D(repr(previous.funding_rate_est)))
                    self.funding_log.append({"symbol": bar.symbol, "ts": t, "payment": pay,
                                             "rate": D(repr(previous.funding_rate_est)), "contracts": pos.contracts})
        self._liquidate(bar)

    def _liquidate(self, bar):
        pos = self.positions[bar.symbol]
        liq = pos.liquidation_price(self.tiers)
        if liq is None:
            return
        mark = D(repr(bar.mark))
        if (pos.contracts > 0 and mark <= liq) or (pos.contracts < 0 and mark >= liq):
            side = "sell" if pos.contracts > 0 else "buy"
            order = VenueOrder(f"liq-{bar.symbol}-{iso(bar.end)}", f"liq{self._seq}", bar.symbol, side,
                               abs(pos.contracts), "market", None, None, True, bar.end)
            self.orders[order.client_order_id] = order
            self._fill(order, abs(pos.contracts), liq, bar.end)

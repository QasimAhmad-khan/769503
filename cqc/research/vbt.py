"""Fast mark-to-market portfolio simulator on 15-minute bars for screening and Monte Carlo.

It mirrors the production decision rules on a coarser clock:
- signal known at bar end, order enters at the open of bar i+1+latency (never the same bar),
- empirical-forecast gate: the rolling matured outcomes of the SAME signal direction must give a lower
  confidence bound on net return above costs (as in quant.build_entry_candidates), with n_eff >= min,
- stop-risk sizing (per-trade loss budget), per-symbol and gross caps, liquidity cap, partial fills,
  fill probability and rejects, stop gap-through, time exit, taker fees on both legs, half-spread and
  slippage on both legs, 8-hourly funding on the position, liquidation check at 2x isolated margin.
Equity is marked to market every bar; daily returns are close-to-close MTM portfolio returns.
It is an approximation of the event-driven runtime (which is used for finalists and stress tests).
"""
from __future__ import annotations

import math
from collections import deque

import numpy as np

BASE_EXEC = {"fee_rate": 0.0005, "spread_mult": 1.0, "slip_bps": 2.0, "funding_mult": 1.0, "fill_prob": 1.0,
             "reject_prob": 0.0, "latency_bars": 0, "stop_gap_bps": 10.0, "liquidity_frac": 0.02}
RISK = {"per_trade_risk": 0.0025, "symbol_cap": 0.25, "gross_cap": 1.0, "leverage": 2.0, "daily_loss_limit": 0.01,
        "drawdown_limit": 0.03}


class Ctx:
    """Column arrays for one symbol on 15-minute bars."""
    FIELDS = ("time", "open", "high", "low", "close", "volume", "spread_bps", "funding", "onchain")


def build_ctx(bars15: list, onchain: np.ndarray | None = None) -> dict:
    n = len(bars15)
    ctx = {"time": np.array([b.end.timestamp() for b in bars15]), "open": np.array([b.open for b in bars15]),
           "high": np.array([b.high for b in bars15]), "low": np.array([b.low for b in bars15]),
           "close": np.array([b.close for b in bars15]), "volume": np.array([b.volume_base for b in bars15]),
           "spread_bps": np.array([b.spread_bps for b in bars15]), "funding": np.array([b.funding_rate_est for b in bars15])}
    ctx["onchain"] = onchain if onchain is not None else np.full(n, np.nan)
    return ctx


def run(ctxs: dict, dirs: dict, start: int, end: int, *, hold_bars: int = 16, stop_mult: float = 2.0,
        lcb_z: float = 1.64, min_eff: int = 20, lookback: int = 2000, exec_params: dict | None = None,
        equity0: float = 10000.0, seed: int = 0, gate: bool = True, warm_from: int | None = None) -> dict:
    """`warm_from`: bar index from which matured outcomes may seed the entry gate before `start` (no trading
    before `start`). None reproduces the research_v2 frozen behaviour (gate memory starts empty at `start`)."""
    ex = dict(BASE_EXEC, **(exec_params or {}))
    rng = np.random.default_rng(seed)
    syms = sorted(ctxs)
    lat = int(ex["latency_bars"])
    fee = ex["fee_rate"]
    slip = ex["slip_bps"] / 1e4
    gap = ex["stop_gap_bps"] / 1e4
    equity, cash_pnl = equity0, 0.0
    pos = {s: None for s in syms}
    trades, eq_curve, t_curve = [], [], []
    vols = {s: _roll_vol(ctxs[s]["close"], 96) for s in syms}
    # matured outcome memory per symbol/direction for the empirical gate: deque of (start_idx, net_ret)
    mem = {s: {1: deque(), -1: deque()} for s in syms}
    pending = {s: deque() for s in syms}  # (signal_idx, dir) awaiting maturity
    breaches = {"daily_loss": 0, "drawdown": 0}
    peak, day_start_eq, cur_day = equity0, equity0, None
    first = max(start, 1) if warm_from is None else max(warm_from, 1)
    for i in range(first, end):
        warm = i < start
        for s in syms:
            c = ctxs[s]
            half = c["close"][i] * c["spread_bps"][i] / 2e4 * ex["spread_mult"]
            # --- mature past signals (outcome measured with the same exit rules, costs deducted)
            while pending[s] and pending[s][0][0] + hold_bars <= i - 1:
                j, d = pending[s].popleft()
                mem[s][d].append((j, _outcome(c, j, d, hold_bars, stop_mult, vols[s][j], fee, slip, gap)))
                while mem[s][d] and mem[s][d][0][0] < i - lookback:
                    mem[s][d].popleft()
            sig_i = i - 1 - lat
            dsig = int(dirs[s][sig_i]) if sig_i >= 0 else 0
            if dsig != 0 and sig_i >= 0 and (not pending[s] or pending[s][-1][0] != sig_i):
                pending[s].append((sig_i, dsig))
            if warm:
                continue
            p = pos[s]
            # --- manage open position
            if p is not None:
                d = p["dir"]
                o, h, lo, cl = c["open"][i], c["high"][i], c["low"][i], c["close"][i]
                exit_px = None
                if (d > 0 and lo <= p["stop"]) or (d < 0 and h >= p["stop"]):
                    ref = min(o, p["stop"]) if d > 0 else max(o, p["stop"])
                    exit_px = ref * (1 - gap - slip) - half if d > 0 else ref * (1 + gap + slip) + half
                    p["exit"] = "stop"
                elif i - p["i"] >= hold_bars:
                    exit_px = cl - half - cl * slip if d > 0 else cl + half + cl * slip
                    p["exit"] = "time"
                liq = p["entry"] * (1 - 1 / RISK["leverage"] * 0.99) if d > 0 else p["entry"] * (1 + 1 / RISK["leverage"] * 0.99)
                if exit_px is None and ((d > 0 and lo <= liq) or (d < 0 and h >= liq)):
                    exit_px, p["exit"] = liq, "liquidation"
                if _funding_bar(c["time"][i]):
                    pay = d * p["qty"] * cl * c["funding"][i] * ex["funding_mult"]
                    cash_pnl -= pay
                    p["funding"] += pay
                if exit_px is not None:
                    pnl = d * p["qty"] * (exit_px - p["entry"]) - p["qty"] * exit_px * fee
                    cash_pnl += pnl
                    p.update(close_i=i, exit_px=exit_px)
                    trades.append({"symbol": s, "dir": d, "open_i": p["i"], "close_i": i, "exit": p["exit"],
                                   "notional": p["qty"] * p["entry"], "net": pnl - p["entry_fee"] - p["funding"]})
                    pos[s] = None
            # --- entry
            if pos[s] is None and dsig != 0 and sig_i >= 1 and math.isfinite(vols[s][sig_i]) and vols[s][sig_i] > 0:
                stop_frac = stop_mult * vols[s][sig_i] * math.sqrt(hold_bars)
                cost_frac = 2 * fee + 2 * (half / c["close"][i]) + 2 * slip
                if gate and not _gate(mem[s][dsig], hold_bars, lcb_z, min_eff):
                    continue
                if rng.random() > ex["fill_prob"] or rng.random() < ex["reject_prob"]:
                    continue
                entry = c["open"][i] + half + c["open"][i] * slip if dsig > 0 else c["open"][i] - half - c["open"][i] * slip
                eq_now = equity0 + cash_pnl + _unreal(pos, ctxs, i - 1)
                budget = eq_now * RISK["per_trade_risk"]
                notional = budget / (stop_frac + gap + cost_frac)
                gross = sum(q["qty"] * ctxs[t]["close"][i - 1] for t, q in pos.items() if q)
                notional = min(notional, eq_now * RISK["symbol_cap"], max(eq_now * RISK["gross_cap"] - gross, 0.0))
                liq_cap = ex["liquidity_frac"] * c["volume"][i] * c["open"][i]
                if notional > liq_cap:  # partial fill limited by available liquidity
                    notional = liq_cap
                if notional < 5:
                    continue
                qty = notional / entry
                efee = qty * entry * fee
                cash_pnl -= efee
                pos[s] = {"dir": dsig, "qty": qty, "entry": entry, "i": i, "entry_fee": efee, "funding": 0.0,
                          "stop": entry * (1 - stop_frac) if dsig > 0 else entry * (1 + stop_frac)}
        if warm:
            continue
        equity = equity0 + cash_pnl + _unreal(pos, ctxs, i)
        eq_curve.append(equity)
        t_curve.append(ctxs[syms[0]]["time"][i])
        day = int(ctxs[syms[0]]["time"][i] // 86400)
        if day != cur_day:
            cur_day, day_start_eq = day, eq_curve[-2] if len(eq_curve) > 1 else equity0
        peak = max(peak, equity)
        if equity < day_start_eq * (1 - RISK["daily_loss_limit"]):
            breaches["daily_loss"] += 1
        if equity < peak * (1 - RISK["drawdown_limit"]):
            breaches["drawdown"] += 1
    return {"equity": np.array(eq_curve), "time": np.array(t_curve), "trades": trades, "breaches": breaches,
            "daily_returns": daily_returns(np.array(t_curve), np.array(eq_curve), equity0)}


def daily_returns(t, eq, equity0):
    if not len(eq):
        return np.array([])
    days = (t // 86400).astype(np.int64)
    last = {}
    for d, e in zip(days, eq):
        last[int(d)] = e
    vals = [equity0] + [last[d] for d in sorted(last)]
    return np.diff(vals) / np.array(vals[:-1])


def _roll_vol(close, w):
    r = np.diff(np.log(close), prepend=np.log(close[0]))
    from .. import quant
    return quant._rolling_std(r, w)


def _funding_bar(ts_end):
    return int(ts_end) % 28800 == 0


def _unreal(pos, ctxs, i):
    return sum(p["dir"] * p["qty"] * (ctxs[s]["close"][i] - p["entry"]) for s, p in pos.items() if p)


def _outcome(c, j, d, hold, stop_mult, vol, fee, slip, gap):
    """Net return of a signal at j entered at j+1 open, same stop/time exit, costs deducted."""
    if not math.isfinite(vol) or vol <= 0 or j + 1 + hold >= len(c["close"]):
        return 0.0
    entry = c["open"][j + 1]
    stop_frac = stop_mult * vol * math.sqrt(hold)
    path_lo = c["low"][j + 1:j + 1 + hold].min()
    path_hi = c["high"][j + 1:j + 1 + hold].max()
    adverse = (path_lo / entry - 1) if d > 0 else (1 - path_hi / entry)
    gross = -(stop_frac + gap) if adverse <= -stop_frac else d * (c["close"][j + hold] / entry - 1)
    spread = c["spread_bps"][j + 1] / 1e4
    return gross - 2 * fee - spread - 2 * slip


def _gate(mem, hold, lcb_z, min_eff):
    if len(mem) < 2:
        return False
    n_eff, last = 0, -10 ** 9
    for j, _ in mem:
        if j - last >= hold:
            n_eff, last = n_eff + 1, j
    if n_eff < min_eff:
        return False
    x = np.array([o for _, o in mem])
    return x.mean() - lcb_z * x.std(ddof=1) / math.sqrt(n_eff) > 0


def summarize(res: dict, equity0: float = 10000.0) -> dict:
    r = res["daily_returns"]
    eq = res["equity"]
    tr = res["trades"]
    sharpe = float(r.mean() / r.std(ddof=1) * math.sqrt(365)) if len(r) > 2 and r.std(ddof=1) > 0 else None
    peak = np.maximum.accumulate(np.concatenate([[equity0], eq])) if len(eq) else np.array([equity0])
    dd = (peak - np.concatenate([[equity0], eq])) / peak if len(eq) else np.array([0.0])
    under, longest = 0, 0
    for v in dd:
        under = under + 1 if v > 1e-12 else 0
        longest = max(longest, under)
    k = max(1, math.ceil(0.05 * len(r))) if len(r) else 0
    n_eff, last_end = 0, -1
    for t in sorted(tr, key=lambda t: t["open_i"]):
        if t["open_i"] >= last_end:
            n_eff, last_end = n_eff + 1, t["close_i"]
    return {"net_return_pct": round(float((eq[-1] / equity0 - 1) * 100), 4) if len(eq) else 0.0,
            "sharpe_ann_mtm": round(sharpe, 4) if sharpe is not None else None, "days": int(len(r)),
            "trades": len(tr), "effective_trades": n_eff,
            "expectancy_quote": round(float(np.mean([t["net"] for t in tr])), 4) if tr else None,
            "max_drawdown_pct": round(float(dd.max() * 100), 4),
            "longest_under_water_bars": int(longest),
            "cvar95_daily_pct": round(float(np.sort(r)[:k].mean() * 100), 4) if k else None,
            "liquidations": sum(t["exit"] == "liquidation" for t in tr),
            "limit_breach_bars": res["breaches"]}

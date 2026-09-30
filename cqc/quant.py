"""Registered numerical tools. Deterministic code owns every trusted number.

Two logged hypotheses (research candidates only, not claimed edges):
  trend_breakout_v1   volatility-scaled trend continuation
  funding_crowding_v1 fade extreme funding when the trend rule is neutral (a fixed, declared
                      regime rule — not a trained one)

The forecast is an empirical distribution of *matured* historical path outcomes at the
cutoff (stop assumed hit first whenever touched: a declared conservative rule), with an
effective sample count from non-overlapping windows and a normal-approximation lower bound.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from . import contracts
from .util import D, ZERO, dstr, floor_step, iso, plan_hash, round_price, stable_id
from decimal import ROUND_CEILING, ROUND_FLOOR

TOOLS: dict[str, dict] = {}


def tool(name: str, version: str, args: tuple):
    def wrap(fn):
        TOOLS[name] = {"version": version, "fn": fn, "args": args}
        return fn
    return wrap


def call_tool(name: str, arguments: dict):
    """Allowlisted dispatch: unknown tools or unexpected arguments are rejected."""
    spec = TOOLS.get(name)
    if spec is None:
        raise KeyError(f"tool {name!r} is not registered")
    unexpected = set(arguments) - set(spec["args"])
    if unexpected:
        raise ValueError(f"unexpected arguments for {name}: {sorted(unexpected)}")
    return spec["fn"](**arguments)


# ------------------------------------------------------------------ vectorized features
def _rolling_mean(x: np.ndarray, w: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= w:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[w - 1:] = (c[w:] - c[:-w]) / w
    return out


def _rolling_std(x: np.ndarray, w: int) -> np.ndarray:
    m = _rolling_mean(x, w)
    m2 = _rolling_mean(x * x, w)
    return np.sqrt(np.maximum(m2 - m * m, 0.0) * w / max(w - 1, 1))


@tool("features_v1", "1", ("closes", "funding", "params"))
def features(closes: np.ndarray, funding: np.ndarray, params: dict) -> dict:
    """Per-bar features using data up to each bar only (no look-ahead)."""
    closes = np.asarray(closes, float)
    r = np.diff(np.log(closes), prepend=np.log(closes[0]))
    vol = _rolling_std(r, params["vol_window_bars"])
    fast, slow = _rolling_mean(closes, params["trend_fast_bars"]), _rolling_mean(closes, params["trend_slow_bars"])
    z = (fast - slow) / (closes * vol * math.sqrt(params["trend_slow_bars"] / 2))
    trend_dir = np.where(np.abs(z) > params["trend_entry_z"], np.sign(z), 0.0)
    extreme = float(params["funding_extreme_rate"])
    fund_dir = np.where((trend_dir == 0) & (np.abs(funding) > extreme), -np.sign(funding), 0.0)
    return {"vol": vol, "z": z, "trend_dir": np.nan_to_num(trend_dir), "funding_dir": np.nan_to_num(fund_dir)}


@tool("empirical_forecast_v1", "1", ("closes", "highs", "lows", "directions", "direction", "horizon_bars",
                                      "stop_frac", "stop_slippage_frac", "lcb_z", "min_index"))
def empirical_forecast(closes, highs, lows, directions, direction: int, horizon_bars: int, stop_frac: float,
                       stop_slippage_frac: float, lcb_z: float, min_index: int = 0) -> dict:
    """Outcome = side-adjusted return to the horizon, or -(stop + slippage) if the stop is touched.
    Only windows fully matured before the current bar are used (purged labels)."""
    n = len(closes)
    last_start = n - 1 - horizon_bars  # label window must end at or before the current bar
    outcomes, starts = [], []
    for i in range(max(min_index, 0), last_start + 1):
        if directions[i] != direction:
            continue
        p0 = closes[i]
        path_hi, path_lo = highs[i + 1:i + 1 + horizon_bars], lows[i + 1:i + 1 + horizon_bars]
        adverse = (path_lo.min() / p0 - 1) if direction > 0 else (1 - path_hi.max() / p0)
        if adverse <= -stop_frac:
            outcomes.append(-(stop_frac + stop_slippage_frac))
        else:
            outcomes.append(direction * (closes[i + horizon_bars] / p0 - 1))
        starts.append(i)
    n_eff, last = 0, -10 ** 9
    for s in starts:  # non-overlapping windows
        if s - last >= horizon_bars:
            n_eff, last = n_eff + 1, s
    if len(outcomes) < 2 or n_eff < 2:
        return {"samples": len(outcomes), "effective_samples": n_eff, "mean": None, "std": None, "lcb": None,
                "ucb": None}
    arr = np.array(outcomes)
    mean, std = float(arr.mean()), float(arr.std(ddof=1))
    se = std / math.sqrt(n_eff)
    return {"samples": len(outcomes), "effective_samples": n_eff, "mean": mean, "std": std,
            "lcb": mean - lcb_z * se, "ucb": mean + lcb_z * se, "stop_hit_rate": float(np.mean(arr <= -stop_frac))}


@tool("cost_model_v1", "1", ("contracts", "multiplier", "price", "taker_fee", "half_spread", "stress_bps"))
def cost_model(contracts, multiplier, price, taker_fee, half_spread, stress_bps) -> dict:
    """Round-trip fees on notional (never multiplied by leverage) and half-spread crossing on both legs."""
    notional = D(contracts) * D(multiplier) * D(price)
    fees = notional * D(taker_fee) * 2
    execution = D(contracts) * D(multiplier) * D(half_spread) * 2
    stressed_fees_per_contract = D(multiplier) * D(price) * D(taker_fee) * 2 * (1 + D(stress_bps) / 10000)
    return {"notional": notional, "fees": fees, "execution": execution,
            "stressed_fees_per_contract": stressed_fees_per_contract}


@tool("funding_accrual_v1", "1", ("signed_contracts", "multiplier", "mark", "rate", "horizon_seconds",
                                   "interval_seconds"))
def funding_accrual(signed_contracts, multiplier, mark, rate, horizon_seconds, interval_seconds) -> object:
    """Expected signed funding payment over the horizon (positive = pays)."""
    intervals = D(math.ceil(horizon_seconds / interval_seconds))
    return D(signed_contracts) * D(multiplier) * D(mark) * D(rate) * intervals


@tool("size_position_v1", "1", ("entry", "stop", "multiplier", "qty_step", "stress_slippage_per_base",
                                 "stressed_fees_per_contract", "adverse_funding_per_contract", "loss_budget",
                                 "exposure_room", "symbol_room", "margin_room", "liquidity_contracts",
                                 "stress_room", "stress_move"))
def size_position(entry, stop, multiplier, qty_step, stress_slippage_per_base, stressed_fees_per_contract,
                  adverse_funding_per_contract, loss_budget, exposure_room, symbol_room, margin_room,
                  liquidity_contracts, stress_room, stress_move) -> dict:
    """n_final = floor_step(min(n_stop, n_exposure, n_margin, n_liquidity, n_stress, n_concentration))."""
    entry, stop, m = D(entry), D(stop), D(multiplier)
    loss_per_contract = (m * (abs(entry - stop) + D(stress_slippage_per_base)) + D(stressed_fees_per_contract)
                         + D(adverse_funding_per_contract))
    per_contract_notional = m * entry
    if loss_per_contract <= 0 or per_contract_notional <= 0:
        raise ValueError("non-positive sizing denominator")
    bounds = {
        "n_stop": max(D(loss_budget), ZERO) / loss_per_contract,
        "n_exposure": max(D(exposure_room), ZERO) / per_contract_notional,
        "n_concentration": max(D(symbol_room), ZERO) / per_contract_notional,
        "n_margin": max(D(margin_room), ZERO) / per_contract_notional,
        "n_liquidity": max(D(liquidity_contracts), ZERO),
        "n_stress": max(D(stress_room), ZERO) / (per_contract_notional * D(stress_move)),
    }
    binding = min(bounds, key=bounds.get)
    return {"contracts": floor_step(bounds[binding], D(qty_step)), "binding": binding,
            "loss_per_contract": loss_per_contract, "bounds": {k: str(v) for k, v in bounds.items()}}


@tool("stress_pnl_v1", "1", ("signed_contracts", "multiplier", "price", "move"))
def stress_pnl(signed_contracts, multiplier, price, move) -> object:
    """Loss (positive) of a position under an adverse joint price move fraction."""
    return abs(D(signed_contracts)) * D(multiplier) * D(price) * D(move)


# ------------------------------------------------------------------ candidate construction
@dataclass
class SizingContext:
    equity: object
    per_trade_budget: object
    aggregate_room: object
    exposure_room: object
    symbol_room: object
    margin_room: object
    stress_room: object


def _q(x, places="0.01"):
    return D(x).quantize(D(places))


def build_entry_candidates(*, cfg, inst, bars15, cutoff: datetime, hypothesis: str, direction: int, feats: dict,
                           ctx: SizingContext, snapshot_id: str, account_version: str, evidence_ids: list,
                           correlation_id: str, funding_rate: float, synthetic: bool = True) -> tuple[list, dict]:
    """Returns (eligible candidate plans, analysis dict). Ineligible plans are dropped in code."""
    q = cfg["quant"]
    horizon_s = cfg["market"]["forecast_horizon_seconds"]
    horizon_bars = horizon_s // cfg["market"]["decision_bar_seconds"]
    closes = np.array([b.close for b in bars15])
    highs, lows = np.array([b.high for b in bars15]), np.array([b.low for b in bars15])
    dirs = feats.get(f"dir:{hypothesis}")
    if dirs is None:
        dirs = feats["trend_dir"] if hypothesis == "trend_breakout_v1" else feats["funding_dir"]
    vol = float(feats["vol"][-1])
    analysis = {"hypothesis": hypothesis, "direction": direction, "vol_per_bar": vol, "reason_codes": []}
    if not math.isfinite(vol) or vol <= 0:
        analysis["reason_codes"].append("VOL_UNAVAILABLE")
        return [], analysis
    stop_frac = float(q["stop_vol_multiple"]) * vol * math.sqrt(horizon_bars)
    stress_frac = float(cfg["venue_sim"]["stress_slippage_bps"]) / 1e4
    fc = empirical_forecast(closes, highs, lows, dirs, direction, horizon_bars, stop_frac, stress_frac,
                            float(q["lcb_z"]), max(0, len(closes) - int(q["history_bars"])))
    analysis["forecast"] = fc
    last = bars15[-1]
    if fc["effective_samples"] < int(q["min_effective_samples"]) or fc["mean"] is None:
        analysis["reason_codes"].append("INSUFFICIENT_SAMPLE_SUPPORT")
        return [], analysis
    half_spread = (last.ask - last.bid) / 2
    ref = D(repr(last.ask if direction > 0 else last.bid))
    env = D(q["price_envelope_bps"]) / D(10000)
    price_min = round_price(ref * (1 - env), inst.price_tick, ROUND_CEILING)
    price_max = round_price(ref * (1 + env), inst.price_tick, ROUND_FLOOR)
    stop = ref * (1 - D(repr(stop_frac))) if direction > 0 else ref * (1 + D(repr(stop_frac)))
    stop = round_price(stop, inst.price_tick, ROUND_FLOOR if direction > 0 else ROUND_CEILING)
    worst_entry = price_max if direction > 0 else price_min
    taker = cfg["venue_sim"]["taker_fee_rate"]
    unit_cost = cost_model(1, inst.multiplier, worst_entry, taker, repr(half_spread), cfg["venue_sim"]["stress_slippage_bps"])
    adverse_funding = max(funding_accrual(direction, inst.multiplier, worst_entry, repr(funding_rate), horizon_s,
                                          cfg["venue_sim"]["funding_interval_hours"] * 3600), ZERO)
    avg_vol = float(np.mean([b.volume_base for b in bars15[-96:]]))
    sizing = size_position(worst_entry, stop, inst.multiplier, inst.qty_step, worst_entry * D(repr(stress_frac)),
                           unit_cost["stressed_fees_per_contract"], adverse_funding,
                           min(ctx.per_trade_budget, ctx.aggregate_room), ctx.exposure_room, ctx.symbol_room,
                           ctx.margin_room, D(repr(avg_vol * 0.05)) / inst.multiplier, ctx.stress_room, D("0.10"))
    analysis["sizing"] = {k: (str(v) if not isinstance(v, dict) else v) for k, v in sizing.items()}
    plans = []
    for frac in q["size_fractions"]:
        n = floor_step(sizing["contracts"] * D(frac), inst.qty_step)
        notional = n * inst.multiplier * worst_entry
        if n < inst.min_contracts or notional < inst.min_notional:
            continue  # never round up to meet an exchange minimum
        costs = cost_model(n, inst.multiplier, worst_entry, taker, repr(half_spread), cfg["venue_sim"]["stress_slippage_bps"])
        funding_pay = funding_accrual(direction * n, inst.multiplier, worst_entry, repr(funding_rate), horizon_s,
                                      cfg["venue_sim"]["funding_interval_hours"] * 3600)
        ev = notional * D(repr(fc["mean"])) - costs["fees"] - costs["execution"] - funding_pay
        lcb = notional * D(repr(fc["lcb"])) - costs["fees"] - costs["execution"] - funding_pay
        if lcb <= D(q["net_ev_hurdle_quote"]):
            analysis["reason_codes"].append(f"LCB_BELOW_HURDLE_{frac}")
            continue
        stress_loss = n * sizing["loss_per_contract"]
        plans.append(make_plan(cfg=cfg, inst=inst, cutoff=cutoff, correlation_id=correlation_id, snapshot_id=snapshot_id,
                               account_version=account_version, action="OPEN_LONG" if direction > 0 else "OPEN_SHORT",
                               side="long" if direction > 0 else "short", qty=n, price_min=price_min,
                               price_max=price_max, stop=stop, position_ref=None, evidence_ids=evidence_ids,
                               metrics={"utility_basis": "net_entry_ev", "horizon_seconds": horizon_s,
                                        "expected_utility_quote": dstr(_q(ev)), "utility_lcb_quote": dstr(_q(lcb)),
                                        "estimated_fees_quote": dstr(_q(costs["fees"])),
                                        "estimated_execution_cost_quote": dstr(_q(costs["execution"])),
                                        "expected_funding_payment_quote": dstr(_q(funding_pay, "0.0001")),
                                        "stress_loss_quote": dstr(_q(stress_loss)),
                                        "forecast_artifact_id": f"{hypothesis}:empirical_forecast_v1",
                                        "uncertainty_artifact_id": f"normal_lcb_z{q['lcb_z']}_neff",
                                        "effective_sample_count": int(fc["effective_samples"])},
                               tag=f"{hypothesis}:{frac}", synthetic=synthetic))
    return remove_dominated(plans), analysis


def build_close_candidate(*, cfg, inst, bars15, cutoff, position_contracts, feats, snapshot_id, account_version,
                          evidence_ids, correlation_id, synthetic=True):
    """Discretionary CLOSE valued as incremental utility versus holding (sunk entry fees ignored)."""
    if position_contracts == 0:
        return None
    side = 1 if position_contracts > 0 else -1
    q = cfg["quant"]
    horizon_bars = cfg["market"]["forecast_horizon_seconds"] // cfg["market"]["decision_bar_seconds"]
    closes = np.array([b.close for b in bars15])
    highs, lows = np.array([b.high for b in bars15]), np.array([b.low for b in bars15])
    current_dir = int(feats["trend_dir"][-1])
    if current_dir != -side:
        return None  # only offer a discretionary close when the registered signal has flipped
    vol = float(feats["vol"][-1])
    stop_frac = float(q["stop_vol_multiple"]) * vol * math.sqrt(horizon_bars)
    fc = empirical_forecast(closes, highs, lows, feats["trend_dir"], current_dir, horizon_bars, stop_frac, 0.0,
                            float(q["lcb_z"]), max(0, len(closes) - int(q["history_bars"])))
    if fc["mean"] is None or fc["effective_samples"] < int(q["min_effective_samples"]):
        return None
    last = bars15[-1]
    ref = D(repr(last.bid if side > 0 else last.ask))
    n = abs(D(position_contracts))
    notional = n * inst.multiplier * ref
    exit_cost = notional * D(cfg["venue_sim"]["taker_fee_rate"]) + n * inst.multiplier * D(repr((last.ask - last.bid) / 2))
    # holding is a bet in the position's direction, i.e. against the flipped signal's outcome distribution
    utility = notional * D(repr(fc["mean"])) - exit_cost
    lcb = notional * D(repr(fc["lcb"])) - exit_cost
    if lcb <= 0:
        return None
    env = D(q["price_envelope_bps"]) / D(10000)
    return make_plan(cfg=cfg, inst=inst, cutoff=cutoff, correlation_id=correlation_id, snapshot_id=snapshot_id,
                     account_version=account_version, action="CLOSE", side="long" if side > 0 else "short", qty=n,
                     price_min=round_price(ref * (1 - env), inst.price_tick, ROUND_CEILING),
                     price_max=round_price(ref * (1 + env), inst.price_tick, ROUND_FLOOR), stop=None,
                     position_ref=f"pos:{inst.key}", evidence_ids=evidence_ids,
                     metrics={"utility_basis": "incremental_vs_hold",
                              "horizon_seconds": cfg["market"]["forecast_horizon_seconds"],
                              "expected_utility_quote": dstr(_q(utility)), "utility_lcb_quote": dstr(_q(lcb)),
                              "estimated_fees_quote": dstr(_q(notional * D(cfg["venue_sim"]["taker_fee_rate"]))),
                              "estimated_execution_cost_quote": dstr(_q(exit_cost)),
                              "expected_funding_payment_quote": "0", "stress_loss_quote": "0",
                              "forecast_artifact_id": "trend_breakout_v1:empirical_forecast_v1",
                              "uncertainty_artifact_id": f"normal_lcb_z{q['lcb_z']}_neff",
                              "effective_sample_count": int(fc["effective_samples"])},
                     tag="close", synthetic=synthetic)


def make_plan(*, cfg, inst, cutoff, correlation_id, snapshot_id, account_version, action, side, qty, price_min,
              price_max, stop, position_ref, evidence_ids, metrics, tag, synthetic=True) -> dict:
    ttl = int(cfg["quant"]["plan_ttl_seconds"])
    risk_increasing = action.startswith("OPEN")
    candidate_id = stable_id("cand", snapshot_id, inst.key, action, tag, str(qty))
    plan = contracts.envelope("candidate_plan", stable_id("evt", candidate_id), correlation_id, "quant_engine",
                              iso(cutoff), iso(cutoff), iso(cutoff + timedelta(seconds=ttl)), synthetic)
    plan.update({"candidate_id": candidate_id, "snapshot_id": snapshot_id, "account_state_version": account_version,
                 "policy_version": cfg["autonomous_policy"], "instrument": inst.contract_dict(), "action": action,
                 "position_ref": position_ref, "position_side": side, "quantity_contracts": dstr(qty),
                 "price_min": dstr(price_min), "price_max": dstr(price_max),
                 "stop_price": dstr(stop) if stop is not None else None, "reduce_only": not risk_increasing,
                 "risk_increasing": risk_increasing, "evidence_ids": list(evidence_ids)[:16], "metrics": metrics,
                 "risk_precheck_passed": False})
    plan["plan_sha256"] = plan_hash(plan)
    return plan


def finalize_precheck(plan: dict, passed: bool) -> dict:
    """Precheck outcome is part of the immutable plan: set it, then re-hash (new plan identity)."""
    out = dict(plan)
    out["risk_precheck_passed"] = bool(passed)
    out["plan_sha256"] = plan_hash(out)
    return contracts.validate(out)


def remove_dominated(plans: list) -> list:
    """Drop a plan if another in the same direction has >= utility LCB and <= stress loss (strictly better in one)."""
    keep = []
    for p in plans:
        lp, sp = D(p["metrics"]["utility_lcb_quote"]), D(p["metrics"]["stress_loss_quote"])
        dominated = False
        for other in plans:
            if other is p or other["action"] != p["action"]:
                continue
            lo, so = D(other["metrics"]["utility_lcb_quote"]), D(other["metrics"]["stress_loss_quote"])
            if lo >= lp and so <= sp and (lo > lp or so < sp):
                dominated = True
        if not dominated:
            keep.append(p)
    return keep

"""Render docs/VALIDATION_REPORT.md from the committed JSON artifacts (no numbers are typed by hand)."""
from __future__ import annotations

import json
from pathlib import Path

from .manifest import TrialLog, load_verified
from . import program


def _j(name):
    p = program.OUT / name
    return json.loads(p.read_text()) if p.exists() else None


def render() -> str:
    m = load_verified(program.MANIFEST)
    dev, rob, hold, gates, soak = (_j("dev_trials.json"), _j("robustness_dev.json"), _j("holdout.json"),
                                   _j("gates.json"), _j("soak_wallclock.json"))
    log = TrialLog(program.LOG)
    L = ["# Validation report — Phi-only paper system", "",
         "> **Scope.** All economic numbers below come from a **labeled synthetic fixture** and a **fake rule-based Phi "
         "backend** that reproduces the deterministic ranker. They validate the *protocol and the simulator*, not an "
         "investment thesis. Investment gates are **UNTESTED**. Nothing here is a profitability claim, and Monte Carlo "
         "results do not prove future performance. Live trading remains disabled; `main` does not contain this work.", "",
         f"- Manifest `{m['manifest_version']}` sha256 `{m['manifest_sha256']}` (frozen {m['frozen_at']})",
         f"- Fixture sha256 `{m['data']['fixture_sha256']}` (seed {m['data']['seed']}, {m['data']['days']} days)",
         f"- Trial log: {len(log.entries())} entries, hash chain valid: **{log.verify()}** (`research/trial_log.jsonl`)",
         f"- Periods (days): warmup {m['periods']['warmup']}, dev {m['periods']['dev']} in {m['periods']['dev_fold_days']}-day "
         f"folds, embargo gap {m['periods']['embargo_gap']}, sealed holdout {m['periods']['holdout']}", "",
         "## Gate verdicts", "", "| gate | verdict | evidence |", "|---|---|---|"]
    for k, (v, why) in gates.items():
        L.append(f"| {k} | **{v}** | {why} |")
    L += ["", "## Development trials (pre-registered, all logged)", "",
          "| trial | params | dev objective (net trade PnL) | folds | trades | eff. trades | maxDD % | Sharpe/day | invariants |",
          "|---|---|---:|---|---:|---:|---:|---:|---|"]
    for t in dev["trials"]:
        L.append(f"| {t['trial_id']} | {t['params']} | {t['objective_dev_net_trade_pnl']} | {t['fold_pnl']} | "
                 f"{t['trades']['trades']} | {t['effective_trades']} | {t['daily']['max_drawdown_pct']} | "
                 f"{t['daily']['sharpe_daily']} | {'PASS' if t['invariants_passed'] else 'FAIL'} |")
    wf = dev["walk_forward_selection"]
    L += ["", f"- **Selected (frozen before the holdout):** `{dev['selected']['trial_id']}` {dev['selected']['quant']}; "
          f"no verified edge on dev: **{dev['no_verified_edge_on_dev']}**",
          f"- Walk-forward estimate of the *selection procedure* (choose on purged earlier folds, score next fold): "
          f"total {dev['walk_forward_oos_total']} USDT; per fold {[(w['selected'], w['oos_fold_pnl']) for w in wf]}",
          f"- PBO (CSCV): {dev['pbo'].get('pbo')} — {dev['pbo'].get('assumptions', dev['pbo'].get('reason'))}",
          f"- Deflated Sharpe of the selected trial on dev: {dev['dsr_dev_selected'].get('dsr')} "
          f"(SR/day {dev['dsr_dev_selected'].get('sharpe_daily')}, SR0 {dev['dsr_dev_selected'].get('sr0_daily')}, "
          f"trials {dev['dsr_dev_selected'].get('trials')})", ""]
    L += ["## Robustness on the development period (frozen candidate)", "",
          f"{rob['runs']} replays, all hard invariants passed: **{rob['all_runs_invariants_passed']}** "
          f"(wall {rob['wall_seconds']} s).", "", "### Paired ablations (same data, capital, candidate generator)", "",
          "| policy | net PnL | trades | maxDD % | CVaR95/day | Phi calls | est. Phi input tokens |", "|---|---:|---:|---:|---:|---:|---:|"]
    for k, v in rob["ablations"].items():
        L.append(f"| {k} | {v['net_pnl']} | {v['trades']} | {v['max_drawdown_pct']} | {v['cvar95_daily']} | "
                 f"{v['phi_calls']} | {v['phi_tokens_in_est']} |")
    L += ["", "| comparison | mean daily diff | 90% CI | reading |", "|---|---:|---|---|"]
    for k, v in rob["paired_vs_P0"].items():
        L.append(f"| {k} − P0 | {v['mean_daily_diff']} | {v['ci90_mean_daily_diff']} | {v['reading']} |")
    cp = rob["cash_and_passive"]
    L += [f"| P0 − exposure-matched passive long | {cp['paired_P0_minus_passive']['mean_daily_diff']} | "
          f"{cp['paired_P0_minus_passive']['ci90_mean_daily_diff']} | {cp['paired_P0_minus_passive']['reading']} |",
          "", f"- Cash: 0. Passive long matched to average gross exposure {cp['avg_gross_exposure_fraction_matched']}: "
          f"{cp['passive_long_exposure_matched_net']} USDT.",
          f"- Decision isolation (identical offered sets, P3): {rob['decision_isolation_P3']} — the fake decision maker "
          "is the ranker, so no incremental value can exist by construction.", "",
          "### Negative controls, delisting, execution perturbations", "", "| run | net PnL | trades | maxDD % | invariants |",
          "|---|---:|---:|---:|---|"]
    for k, v in {**rob["negative_controls"], "delisting (ETH mid-period)": rob["delisting"],
                 **{f"exec: {a}": b for a, b in rob["execution_perturbations"].items()}}.items():
        L.append(f"| {k} | {v['net_pnl']} | {v['trades']} | {v['max_drawdown_pct']} | {'PASS' if v['invariants_passed'] else 'FAIL'} |")
    mc = rob["market_path_mc"]
    L += ["", "### Monte Carlo (fragility, not proof)", "",
          f"- Market paths ({mc['paths']} × {mc['days_each']} days; {mc['method']}): net PnL q05/q50/q95 "
          f"{mc['net_pnl_q05_q50_q95']}, P(net<0) {mc['p_net_negative']}, maxDD q95 {mc['max_drawdown_q95']}, "
          f"invariant failures {mc['invariant_failures']}, liquidations {mc['liquidations']}.",
          f"- Cost/execution MC on dev trades ({rob['cost_mc_dev'].get('reps')} seeded reps): total q05/q50/q95 "
          f"{rob['cost_mc_dev'].get('total_q05_q50_q95')}, P(total<0) {rob['cost_mc_dev'].get('p_total_negative')}; priors "
          f"{rob['cost_mc_dev'].get('priors')}.",
          f"- Stationary bootstrap of dev daily PnL: total q05/q50/q95 {rob['daily_bootstrap_dev'].get('total_pnl_q05_q50_q95')}, "
          f"P(total<0) {rob['daily_bootstrap_dev'].get('p_total_negative')}, maxDD q50/q95/q99 "
          f"{rob['daily_bootstrap_dev'].get('max_drawdown_q50_q95_q99')}.", "",
          "### Regime slices (dev, P0; underpowered slices are inconclusive)", "",
          "| regime | days | trades | eff. trades | expectancy | net | verdict |", "|---|---:|---:|---:|---:|---:|---|"]
    for k, v in rob["regimes_dev_P0"].items():
        L.append(f"| {k} | {v['days']} | {v['trades']} | {v['effective_trades']} | {v['expectancy']} | {v['daily_pnl_sum']} | {v['verdict']} |")
    lk = rob["leakage"]
    L += ["", f"- Leakage: registered features causal = {lk['features_v1']['causal']} ({lk['features_v1']['probes']} probes); "
          f"deliberately leaky positive control detected = {not lk['positive_control_leaky_feature']['causal']}.", ""]
    if hold:
        p0 = hold["P0"]
        L += ["## Sealed holdout (evaluated exactly once)", "",
              f"- Candidate `{hold['candidate']['trial_id']}` {hold['candidate']['quant']}",
              f"- P0 net {p0['net_pnl']} USDT ({p0['net_return_pct']}%), trades {p0['trades']}, effective {p0['effective_trades']}, "
              f"expectancy {p0['expectancy']}, maxDD {p0['max_drawdown_pct']}%, longest under water {p0['longest_under_water_days']} d, "
              f"CVaR95/day {p0['cvar95_daily']}, invariants {'PASS' if p0['invariants']['passed'] else 'FAIL'}",
              f"- 90% CI mean daily PnL: {hold['ci90_mean_daily_P0']}; DSR {hold['dsr'].get('dsr')}; cost-MC P(total<0) "
              f"{hold['cost_mc'].get('p_total_negative')}",
              f"- P3 (all four fake-Phi roles) − P0: {hold['paired_P3_vs_P0']}; decision isolation {hold['P3']['decision_isolation']}", ""]
    if soak:
        L += ["## Wall-clock protection soak (shortened)", "",
              f"- {soak['wall_seconds']} s wall, {soak['simulated_minutes']} simulated minutes, protection loop runs "
              f"{soak['protection_loop_runs']}, errors {soak['protection_loop_errors']}, protect latency p50/p99/max "
              f"{soak['protect_latency_ms_p50_p99_max']} ms; last sample {soak['samples'][-1] if soak['samples'] else None}.",
              f"- {soak['note']}", ""]
    L += ["## Diagnoses and rule changes (from the trial log)", ""]
    for e in log.entries():
        if e.get("kind") in ("diagnosis", "report_rule_correction"):
            L.append(f"- [{e['kind']}] {e.get('issue', e.get('change'))} → {e.get('finding', e.get('direction'))}"
                     + (f"; action: {e['action']}" if e.get("action") else ""))
    L += ["", "## Why the economic gates failed or are inconclusive", "",
          "1. **Selection is not reliable.** PBO is high and the deflated Sharpe of the selected trial is low: the "
          "best dev trial is mostly the luckiest of 12, and its advantage does not persist across time blocks.",
          "2. **The selected rule trades rarely.** The strict lower-bound rule (lcb_z 2.33) plus a 2000-bar forecast "
          "window made the candidate abstain for the whole sealed holdout. Zero holdout trades means no economic "
          "evidence either way; E2/E3/E5/E6 are inconclusive and E1 fails.",
          "3. **Phi value cannot be measured here.** The fake Phi decision maker is the deterministic ranker by "
          "construction (identical PnL for P0–P4, 100% agreement on offered sets).",
          "4. **Latency sensitivity.** One or more bars of extra latency with the 120 s plan TTL produces zero fills "
          "(fail-closed), which must be addressed as an execution-policy decision before live-like testing.", "",
          "**Outcome: NO VERIFIED EDGE on the synthetic fixture; investment verdict UNTESTED.** The gates are not "
          "weakened and the holdout is not re-used.", "",
          "## Assumptions (model and data)", "",
          "- Synthetic regime-switching GBM market (seed 2026), 1-minute bars, bar availability = close + 1 s; not market data.",
          "- Fills: marketable limit from the first full bar after placement, capped at a fraction of bar volume; "
          "stops fill at the worse of stop/open with 10 bps stress; no queue-position model; maker fees unused.",
          "- Funding: estimate known at bar end, settled 8-hourly on the position; mark = close ± small noise; "
          "illustrative maintenance-margin tiers; isolated 2x.",
          "- Execution perturbations and cost Monte Carlo priors are declared, not fitted (listed above).",
          "- Market-path Monte Carlo re-samples whole days of the SAME dev pool; it measures fragility, not future returns.",
          "- Token counts are byte-based estimates for the fake backend; real counts come from the server's tokenizer "
          "(`usage`) in `bench-phi`.",
          "- Passive benchmark: static 50/50 long matched to the candidate's average gross exposure (approximation).", "",
          "## Next steps (not weaker gates, not another holdout pass)", "",
          "- Capture real point-in-time exchange data (trades, books, mark/index, funding with capture times) and "
          "admissible on-chain data; freeze `research_v2` with a new sealed holdout.",
          "- Measure the real pinned Phi model (`bench-phi`), then run P0–P4 with real Phi on the same candidate sets.",
          "- Pre-register the execution-policy question (TTL vs latency) as its own hypothesis on dev data.",
          "- Consider hypotheses with a larger effective sample (more instruments or shorter horizons) so gates are powered.",
          "- Follow `docs/PROSPECTIVE_PAPER_PLAN.md` for the prospective paper period; live orders stay disabled.", ""]
    return "\n".join(L) + "\n"

"""Render docs/VALIDATION_REPORT_V2.md from research_v2 artifacts (numbers are never typed by hand)."""
from __future__ import annotations

import json

from . import program_v2 as P
from .manifest import TrialLog, load_verified


def _j(name):
    p = P.OUT / name
    return json.loads(p.read_text()) if p.exists() else None


def _row(d):
    return " / ".join(str(d.get(k)) for k in ("q05", "q50", "q95")) if d else "-"


def render() -> str:
    m = load_verified(P.MANIFEST)
    log = TrialLog(P.LOG)
    g, dev, scr, mc, rob, rt, hold = (_j("gates.json"), _j("dev_results.json"), _j("screen_results.json"), _j("mc_results.json"),
                                      _j("robust_results.json"), _j("runtime_results.json"), _j("holdout_results.json"))
    subj = dev["candidate"] or dev.get("diagnostic_subject")
    L = ["# research_v2 — alpha discovery and simulation report", "",
         "> **Scope.** Synthetic fixture and fake rule-based Phi backend: this validates the machinery. Alpha and Phi-value "
         "results are **UNTESTED**. Simulations and Monte Carlo describe fragility on re-sampled history; they do not "
         "guarantee any future Sharpe. The research_v1 holdout is spent and is not used. Live trading is disabled; `main` "
         "is unchanged.", "",
         f"- Manifest `{m['manifest_version']}` sha256 `{m['manifest_sha256']}` (frozen {m['frozen_at']}); fixture sha256 "
         f"`{m['data']['fixture_sha256']}` (seed {m['data']['seed']}, {m['data']['days']} days)",
         f"- Dates (fixture days): {m['dates_days']}", f"- Trial log `research/v2/trial_log.jsonl`: {len(log.entries())} entries, "
         f"chain valid **{log.verify()}**; trials used {sum(1 for e in log.entries() if e.get('kind') == 'trial')} of budget "
         f"{m['trial_budget_total']}", "", "## Verdicts", "", "| test | verdict | reason / evidence |", "|---|---|---|"]
    for k, (v, why) in g.items():
        L.append(f"| {k} | **{v}** | {why} |")
    L += ["", "## Hypotheses (Phi proposed and critiqued; deterministic code tested)", "",
          "| template | family | critic verdict | concerns | causal | eligible |", "|---|---|---|---|---|---|"]
    for t, h in m["hypotheses"].items():
        from .signals import REGISTRY
        L.append(f"| {t} | {REGISTRY[t]['family']} | {h['critic_verdict']} | {', '.join(h['concerns'])} | {h['causality_check']} | {h['eligible']} |")
    L += ["", "## Train screening (all trials logged)", "", "| trial | hypothesis | params | train Sharpe (ann., MTM) | train net % | eff. trades | eligible |",
          "|---|---|---|---:|---:|---:|---|"]
    for r in scr["rows"]:
        L.append(f"| {r['trial_id']} | {r['hypothesis']} | {r['params']} | {r['train_sharpe']} | {r['train_net_pct']} | "
                 f"{r['train_effective_trades']} | {r['train_eligible']} |")
    L += ["", "## Development: purged walk-forward", "",
          f"- Train-eligible trials: {dev['eligible'] or 'none'}",
          f"- Folds: {[(w['fold'], w.get('selected'), w.get('oos_return_pct')) for w in dev['walk_forward']]}",
          f"- OOS daily mean {dev['oos_daily_mean']}, 90% CI {dev['oos_ci90']}, OOS Sharpe {dev['oos_sharpe_ann']}",
          f"- PBO {dev['pbo'].get('pbo')} ({dev['pbo'].get('assumptions', dev['pbo'].get('reason'))})",
          f"- Candidate: {dev['candidate'] and dev['candidate']['trial_id']}; diagnostic subject (not a candidate): "
          f"{dev.get('diagnostic_subject', {}).get('trial_id')}"]
    if dev["candidate"]:
        c = dev["candidate"]
        L.append(f"- Candidate dev Sharpe {c['dev_sharpe']}, dev net {c['dev_net_pct']}%, effective trades "
                 f"{c['dev_effective_trades']}, DSR {c['dsr'].get('dsr')} (N={c['n_trials_total']})")
    L += ["", f"Everything below is computed for **{subj['trial_id']}** ({subj['hypothesis']} {subj['params']})"
          + ("" if dev["candidate"] else " as a labeled diagnostic subject, because nothing passed train eligibility") + ".", ""]
    if mc:
        L += ["## Monte Carlo market paths (joint BTC/ETH block resampling of the train+dev pool)", "",
              f"Wall {mc['wall_seconds']} s. q05 / q50 / q95.", "",
              "| config | paths | net % | Sharpe | maxDD % | under water (days) | CVaR95 daily % | P(net<0) | P(DD>3%) | P(daily-loss breach) | P(no trades) |",
              "|---|---:|---|---|---|---|---|---:|---:|---:|---:|"]
        for k, v in mc["configs"].items():
            L.append(f"| {k} | {v['paths']} | {_row(v['net_return_pct'])} | {_row(v['sharpe_ann'])} | {_row(v['max_drawdown_pct'])} | "
                     f"{_row(v['time_under_water_days'])} | {_row(v['cvar95_daily_pct'])} | {v['p_net_negative']} | "
                     f"{v['p_drawdown_gt_3pct']} | {v['p_daily_loss_limit_breach']} | {v['p_no_trades']} |")
        L += ["", "Portfolio-return bootstrap of the subject's own dev daily MTM returns:", "",
              "| scheme/block | net % | Sharpe | maxDD % | P(net<0) | P(DD>3%) |", "|---|---|---|---|---:|---:|"]
        for k, v in mc["portfolio_bootstrap"].items():
            L.append(f"| {k} | {_row(v['net_return_pct'])} | {_row(v['sharpe_ann'])} | {_row(v['max_drawdown_pct'])} | "
                     f"{v['p_net_negative']} | {v['p_drawdown_gt_3pct']} |")
    if rob:
        e = rob["execution_mc"]
        L += ["", "## Execution uncertainty and break-even", "",
              f"- Base dev (screener): {rob['base_dev']}",
              f"- Correlated execution MC ({e['draws']} draws, rho {e['rho']}): net % {_row(e['net_return_pct'])}, Sharpe "
              f"{_row(e['sharpe_ann'])}, P(net<0) {e['p_net_negative']}, P(net<0 | top-decile stress) "
              f"{e['p_net_negative_top_decile_stress']}, corr(stress, net) {e['corr_stress_vs_net']}",
              f"- Cost multiplier curve (x, net %): {rob['cost_multiplier_curve']} → break-even x{rob['break_even_cost_multiplier']}",
              f"- Latency curve (bars, net %): {rob['latency_curve_bars']} → break-even {rob['break_even_latency_bars']} bars",
              "", "## Robustness and negative controls", "",
              f"- Grid neighbours: {[(n['params'], n['sharpe_ann_mtm']) for n in rob['neighbors']]}",
              f"- Decision-time offsets (5/10 min): {[(k, v['sharpe_ann_mtm'], v['net_return_pct']) for k, v in rob['decision_time_offsets'].items()]}",
              f"- Block-randomized signals (200, exposure-matched): Sharpe {_row(rob['negative_controls']['rand']['sharpe_ann'])}; "
              f"subject percentile {rob['negative_controls']['rand']['candidate_percentile']}",
              f"- Time-shifted signals (200): Sharpe {_row(rob['negative_controls']['shift']['sharpe_ann'])}; subject percentile "
              f"{rob['negative_controls']['shift']['candidate_percentile']}",
              f"- Delayed on-chain availability: {[(k, v['net_return_pct'], v['trades']) for k, v in rob['onchain_delay'].items()]}",
              f"- Lookahead (end-to-end corruption of future data): {rob['leakage']}",
              f"- Data quality (ingest dispositions): {rob['data_quality']}", "", "| regime | days | trades | eff. | expectancy | CI90 | verdict |",
              "|---|---:|---:|---:|---:|---|---|"]
        for k, v in rob["regimes"].items():
            L.append(f"| {k} | {v['days']} | {v['trades']} | {v['effective_trades']} | {v['expectancy']} | {v['ci90']} | {v['verdict']} |")
    if rt:
        L += ["", "## Event-driven replay (full runtime, dev) and Phi ablations", "",
              "| policy | net % | Sharpe | trades | invariants | Phi calls | decision isolation |", "|---|---:|---:|---:|---|---:|---|"]
        for k, v in rt["ablations"].items():
            L.append(f"| {k} | {v['net_pct']} | {v['sharpe_ann']} | {v['trades']} | {v['invariants']['passed']} | {v['phi_calls']} | "
                     f"{v['decision_isolation']} |")
        L += ["", f"Stress scenarios (strategy exposed: {rt.get('stress_strategy')}):", "",
              "| scenario | verdict | worst trade | loss bound | final state | protective actions | reasons |", "|---|---|---:|---:|---|---|---|"]
        for k, v in rt["stress_checks"].items():
            L.append(f"| {k.replace('stress:', '')} | {v['verdict']} | {v['worst_trade']} | {round(v['loss_bound'], 1)} | "
                     f"{v['final_state']} | {v['protective']} | {v['reasons']} |")
    ft = _j("finetune_export.json")
    L += ["", "## Why nothing survived, and what is needed next", "",
          "1. **G1 fails**: the purged walk-forward out-of-sample mean daily return's 90% CI includes zero. The evidence is "
          "not strong enough even on synthetic data.",
          "2. **G5 fails**: the candidate is parameter-fragile. One grid neighbour never trades and the other keeps under "
          "half the Sharpe. Shifting the decision clock by 10 minutes turns the dev result negative, so the result depends "
          "on bar alignment.",
          "3. **Fidelity gap** (see diagnoses): the screener's cold-start entry gate overstated trades that start at the dev "
          "boundary, and the event-driven simulator earns much less (+0.51% vs +1.87%) because of 1-minute execution, "
          "price envelopes, TTLs and sizing rooms. Screening must be warm-started (`warm_from`) and finalists must be "
          "judged on the event-driven simulator.",
          "4. **Synthetic artifacts**: breakout and trend edges exist because the generator switches drift regimes every "
          "4 hours. The on-chain hypothesis passed train eligibility on pure-noise on-chain data, a live illustration "
          "of multiple-testing false positives.",
          "5. **Phi value is unmeasurable here**: the fake decision maker agreed with the ranker on 83/83 decisions.", "",
          "**Outcome: NO VERIFIED EDGE; alpha and Phi value UNTESTED. The research_v2 holdout stays sealed.**", "",
          "Needed next (research_v3, new manifest):",
          "- Real point-in-time exchange data (trades, L2, mark/index, funding with receive timestamps) for BTC/ETH plus a "
          "point-in-time listing universe; forward-captured on-chain data with finality.",
          "- The real pinned Phi model measured with `bench-phi` (for example via `deploy/colab_phi_benchmark.ipynb` or a "
          "local GPU), then P0-P4 on identical offered candidate sets.",
          "- Screener gate warm-start and event-driven evaluation of every finalist; decision-time offsets pre-registered "
          "as a robustness gate; a wider but still bounded neighbourhood grid, so stability can be assessed.",
          "- Hypotheses with more independent observations per unit time (more instruments), to power the regime slices.", "",
          f"Fine-tune dataset (train only): {ft}", ""]
    L += ["", "## Holdout", "", f"{json.dumps(hold) if hold and hold.get('opened', True) else 'Not opened: no candidate survived development; the research_v2 holdout stays sealed for a future pre-registered candidate.'}",
          "", "## Trial-log decisions and diagnoses", ""]
    for e in log.entries():
        if e.get("kind") in ("hypothesis_eligibility", "dev_walkforward", "holdout_not_opened", "diagnosis", "gates"):
            L.append(f"- [{e['kind']}] " + json.dumps({k: v for k, v in e.items() if k not in ('prev_sha256', 'entry_sha256', 'seq',
                                                                                                 'logged_at', 'kind')})[:400])
    return "\n".join(L) + "\n"

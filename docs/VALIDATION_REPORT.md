# Validation report — Phi-only paper system

> **Scope.** All economic numbers below come from a **labeled synthetic fixture** and a **fake rule-based Phi backend** that reproduces the deterministic ranker. They validate the *protocol and the simulator*, not an investment thesis. Investment gates are **UNTESTED**. Nothing here is a profitability claim, and Monte Carlo results do not prove future performance. Live trading remains disabled; `main` does not contain this work.

- Manifest `research_v1` sha256 `1d3653404bd9588eb311f0be0af9aba1af5c90329633a0879d8a2026d8548dca` (frozen 2026-09-30T20:43:46.432991+00:00)
- Fixture sha256 `bceaaecb95815319d710a80c998bd5a6ab5c9e1d7fee9302ef210fad108ed71c` (seed 2026, 65 days)
- Trial log: 20 entries, hash chain valid: **True** (`research/trial_log.jsonl`)
- Periods (days): warmup [0, 7], dev [7, 49] in 7-day folds, embargo gap [49, 50], sealed holdout [50, 64]

## Gate verdicts

| gate | verdict | evidence |
|---|---|---|
| H1_safety_invariants | **PASS** | 63 runs |
| L1_leakage_checks | **PASS** | registered features causal; leaky positive control detected |
| E2_sample_size | **INCONCLUSIVE (SYNTHETIC ONLY)** | effective trades 0 vs required 30 |
| E1_holdout_net_positive | **FAIL (SYNTHETIC ONLY)** | net 0.0 USDT, CI [0.0, 0.0] |
| E3_deflated_sharpe | **INCONCLUSIVE (SYNTHETIC ONLY)** | DSR None |
| E4_pbo | **FAIL (SYNTHETIC ONLY)** | PBO 0.9143 |
| E5_tail_risk | **INCONCLUSIVE (SYNTHETIC ONLY)** | maxDD 0.0%, CVaR95 0.0 (vacuous: zero holdout trades) |
| E6_cost_robustness | **INCONCLUSIVE (SYNTHETIC ONLY)** | P(total<0) None |
| E7_phi_incremental_value | **UNTESTED** | fake Phi reproduces the deterministic ranker by construction; real Phi not run |
| R1_real_phi_measured | **UNTESTED** | no GPU / weights in this environment; use `python -m cqc bench-phi` |
| D1_real_point_in_time_data | **UNTESTED** | no real exchange/on-chain captures; all economics are synthetic |
| ECONOMIC_OUTCOME_SYNTHETIC | **NO VERIFIED EDGE** | selected candidate did not earn a positive, significant holdout return and the dev search shows high overfitting risk |
| INVESTMENT_VERDICT | **UNTESTED** | no real-data, real-model evidence exists; synthetic results are engineering-only |

## Development trials (pre-registered, all logged)

| trial | params | dev objective (net trade PnL) | folds | trades | eff. trades | maxDD % | Sharpe/day | invariants |
|---|---|---:|---|---:|---:|---:|---:|---|
| T00 | {'lcb_z': 1.64, 'stop_vol_multiple': '2.0', 'trend_entry_z': 0.5} | 35.1776 | [-30.8392, 4.2441, 70.3916, -8.6189, 0, 0] | 51 | 44 | 0.7486 | 0.0498 | PASS |
| T01 | {'lcb_z': 2.33, 'stop_vol_multiple': '1.5', 'trend_entry_z': 0.5} | 14.8137 | [-30.3697, -9.7032, 54.8866, 0, 0, 0] | 18 | 15 | 0.4696 | 0.0434 | PASS |
| T02 | {'lcb_z': 2.33, 'stop_vol_multiple': '2.5', 'trend_entry_z': 0.5} | -28.5556 | [0, -3.4989, -25.0567, 0, 0, 0] | 8 | 6 | 0.2856 | -0.1706 | PASS |
| T03 | {'lcb_z': 1.64, 'stop_vol_multiple': '1.5', 'trend_entry_z': 0.75} | -77.5486 | [-0.1093, 13.6123, 23.0359, -114.0875, 0, 0] | 50 | 38 | 2.4058 | -0.0786 | PASS |
| T04 | {'lcb_z': 2.33, 'stop_vol_multiple': '1.5', 'trend_entry_z': 0.75} | 38.6063 | [0, -13.772, 52.3783, 0, 0, 0] | 11 | 8 | 0.1377 | 0.1271 | PASS |
| T05 | {'lcb_z': 2.33, 'stop_vol_multiple': '2.0', 'trend_entry_z': 0.75} | -2.1366 | [0, 0, -2.1366, 0, 0, 0] | 8 | 6 | 0.1853 | -0.013 | PASS |
| T06 | {'lcb_z': 1.64, 'stop_vol_multiple': '2.5', 'trend_entry_z': 0.75} | 12.5812 | [-10.0972, 5.4122, 30.4157, -13.1495, 0, 0] | 34 | 30 | 0.633 | 0.0307 | PASS |
| T07 | {'lcb_z': 1.64, 'stop_vol_multiple': '1.5', 'trend_entry_z': 1.0} | -18.8277 | [0, 0, -19.2433, 13.8878, -13.4722, 0] | 30 | 24 | 0.7901 | -0.0338 | PASS |
| T08 | {'lcb_z': 2.33, 'stop_vol_multiple': '1.5', 'trend_entry_z': 1.0} | -69.6143 | [0, 0, -26.2083, -43.406, 0, 0] | 4 | 4 | 0.6961 | -0.2722 | PASS |
| T09 | {'lcb_z': 1.64, 'stop_vol_multiple': '2.0', 'trend_entry_z': 1.0} | -62.916 | [0, 0, -31.9276, -20.51, -10.4784, 0] | 26 | 21 | 1.1339 | -0.1325 | PASS |
| T10 | {'lcb_z': 2.33, 'stop_vol_multiple': '2.0', 'trend_entry_z': 1.0} | -10.459 | [0, 0, -10.459, 0, 0, 0] | 1 | 1 | 0.1046 | -0.1525 | PASS |
| T11 | {'lcb_z': 2.33, 'stop_vol_multiple': '2.5', 'trend_entry_z': 1.0} | -17.6867 | [0, 0, -17.6867, 0, 0, 0] | 1 | 1 | 0.1769 | -0.1525 | PASS |

- **Selected (frozen before the holdout):** `T04` {'lcb_z': 2.33, 'stop_vol_multiple': '1.5', 'trend_entry_z': 0.75}; no verified edge on dev: **False**
- Walk-forward estimate of the *selection procedure* (choose on purged earlier folds, score next fold): total 10.9181 USDT; per fold [('T02', -3.4989), ('T03', 23.0359), ('T00', -8.6189), ('T04', 0), ('T04', 0)]
- PBO (CSCV): 0.9143 — CSCV with contiguous time blocks; performance = mean daily PnL per block set
- Deflated Sharpe of the selected trial on dev: 0.2699 (SR/day 0.1271, SR0 0.1936, trials 12)

## Robustness on the development period (frozen candidate)

49 replays, all hard invariants passed: **True** (wall 695.7 s).

### Paired ablations (same data, capital, candidate generator)

| policy | net PnL | trades | maxDD % | CVaR95/day | Phi calls | est. Phi input tokens |
|---|---:|---:|---:|---:|---:|---:|
| P0_deterministic | 38.6064 | 11 | 0.1377 | -4.5907 | 0 | 0 |
| P1_phi_screening | 38.6064 | 11 | 0.1377 | -4.5907 | 12448 | 13351524 |
| P2_screening_analysis | 38.6064 | 11 | 0.1377 | -4.5907 | 12460 | 13363556 |
| P3_all_four_roles | 38.6064 | 11 | 0.1377 | -4.5907 | 12472 | 13374781 |
| P4_all_four_no_onchain | 38.6064 | 11 | 0.1377 | -4.5907 | 12472 | 12665564 |

| comparison | mean daily diff | 90% CI | reading |
|---|---:|---|---|
| P1_phi_screening − P0 | 0.0 | [0.0, 0.0] | inconclusive |
| P2_screening_analysis − P0 | 0.0 | [0.0, 0.0] | inconclusive |
| P3_all_four_roles − P0 | 0.0 | [0.0, 0.0] | inconclusive |
| P4_all_four_no_onchain − P0 | 0.0 | [0.0, 0.0] | inconclusive |
| P0 − exposure-matched passive long | 0.939 | [-0.6118, 2.8896] | inconclusive |

- Cash: 0. Passive long matched to average gross exposure 0.00356: -1.7695 USDT.
- Decision isolation (identical offered sets, P3): {'decisions': 12, 'agree_with_deterministic_rank': 12, 'abstained': 0} — the fake decision maker is the ranker, so no incremental value can exist by construction.

### Negative controls, delisting, execution perturbations

| run | net PnL | trades | maxDD % | invariants |
|---|---:|---:|---:|---|
| neg_random_signal | 0.0 | 0 | 0.0 | PASS |
| neg_delayed_signal_16 | -28.3839 | 17 | 0.531 | PASS |
| delisting (ETH mid-period) | 38.6064 | 11 | 0.1377 | PASS |
| exec: fees_x2 | 29.3898 | 11 | 0.1448 | PASS |
| exec: slippage_+8bps | 23.8591 | 11 | 0.149 | PASS |
| exec: latency_2_bars | 0.0 | 0 | 0.0 | PASS |
| exec: rejects_20pct | 41.1625 | 11 | 0.1363 | PASS |
| exec: thin_liquidity_x0.02 | 40.2596 | 11 | 0.0898 | PASS |
| exec: stop_gap_+40bps | 31.7533 | 11 | 0.1377 | PASS |
| exec: funding_x5 | 34.8604 | 11 | 0.1321 | PASS |
| exec: outages_6x30min | 38.6064 | 11 | 0.1377 | PASS |
| exec: combined_stress | 0.0 | 0 | 0.0 | PASS |

### Monte Carlo (fragility, not proof)

- Market paths (32 × 14 days; joint BTC/ETH whole-day block bootstrap of the dev pool; half the paths with 2x vol shocks on 15% of days, a quarter with decorrelation): net PnL q05/q50/q95 [-43.7895, 0.0, 76.9411], P(net<0) 0.1562, maxDD q95 52.9445, invariant failures 0, liquidations 0.
- Cost/execution MC on dev trades (5000 seeded reps): total q05/q50/q95 [14.7472, 21.2557, 26.8942], P(total<0) 0.0; priors {'fee_multiplier': ['uniform', 1.0, 2.0], 'extra_slippage_bps_per_leg': ['lognormal_median', 2.0, 0.75], 'latency_drift_bps_per_leg': ['uniform', 0.0, 5.0], 'stop_gap_extra_bps': ['exponential_mean', 15.0], 'funding_paid_multiplier': ['uniform', 0.5, 3.0], 'funding_received_multiplier': ['uniform', 0.0, 1.0]}.
- Stationary bootstrap of dev daily PnL: total q05/q50/q95 [-27.5439, 38.6064, 134.0577], P(total<0) 0.215, maxDD q50/q95/q99 [13.772, 27.5439, 41.3159].

### Regime slices (dev, P0; underpowered slices are inconclusive)

| regime | days | trades | eff. trades | expectancy | net | verdict |
|---|---:|---:|---:|---:|---:|---|
| bear | 23 | 6 | 3 | 0.861 | 9.3053 | INCONCLUSIVE (underpowered) |
| bull | 20 | 5 | 5 | 6.688 | 29.301 | INCONCLUSIVE (underpowered) |
| chop | 15 | 6 | 3 | 0.861 | 9.3053 | INCONCLUSIVE (underpowered) |
| correlated_shock | 6 | 0 | 0 | None | 0.0 | INCONCLUSIVE (underpowered) |
| funding_extreme | 43 | 11 | 8 | 3.5097 | 38.6064 | INCONCLUSIVE (underpowered) |
| high_vol | 21 | 7 | 4 | -1.2294 | -4.4666 | INCONCLUSIVE (underpowered) |
| low_vol | 22 | 4 | 4 | 11.8031 | 43.073 | INCONCLUSIVE (underpowered) |
| trend | 28 | 5 | 5 | 6.688 | 29.301 | INCONCLUSIVE (underpowered) |
| weekend_illiquid | 13 | 0 | 0 | None | 0.0 | INCONCLUSIVE (underpowered) |

- Leakage: registered features causal = True (25 probes); deliberately leaky positive control detected = True.

## Sealed holdout (evaluated exactly once)

- Candidate `T04` {'lcb_z': 2.33, 'stop_vol_multiple': '1.5', 'trend_entry_z': 0.75}
- P0 net 0.0 USDT (0.0%), trades 0, effective 0, expectancy None, maxDD 0.0%, longest under water 0 d, CVaR95/day 0.0, invariants PASS
- 90% CI mean daily PnL: [0.0, 0.0]; DSR None; cost-MC P(total<0) None
- P3 (all four fake-Phi roles) − P0: {'days': 15, 'mean_daily_diff': 0.0, 'total_diff': 0.0, 'ci90_mean_daily_diff': [0.0, 0.0], 'reading': 'inconclusive'}; decision isolation None

## Wall-clock protection soak (shortened)

- 300.0 s wall, 5755 simulated minutes, protection loop runs 5755, errors [], protect latency p50/p99/max [0.443, 0.731, 3.597] ms; last sample {'elapsed_s': 270.1, 'sim_time': '2026-01-18T14:17:00+00:00', 'rss_mib': 131.4, 'graph': {'nodes': 438, 'edges': 0}, 'events': 3583}.
- shortened wall-clock soak with FAKE Phi; the 24 h soak against the real Phi server is UNTESTED

## Diagnoses and rule changes (from the trial log)

- [diagnosis] all trials show 0 PnL in dev folds 5-6 (days 35-49) → not a simulator/risk bug: every cycle abstained (LCB_BELOW_HURDLE x979, NO_REGISTERED_SIGNAL x350, INSUFFICIENT_SAMPLE_SUPPORT x15 over days 33-40 for T00); no risk locks active; action: none; abstention is the intended behavior when the empirical forecast shows no edge
- [diagnosis] latency >= 1 extra bar yields zero fills → plan/authorization TTL (120 s) expires before a latency-delayed marketable limit reaches the book; the system fails closed (orders expire, reservations released); action: reported as an execution-policy finding; not retuned inside this manifest
- [report_rule_correction] E5 tail-risk gate reports INCONCLUSIVE instead of PASS when the holdout had zero trades (vacuous pass) → stricter only; no parameter, data or gate threshold changed; holdout not re-run

## Why the economic gates failed or are inconclusive

1. **Selection is not reliable.** PBO is high and the deflated Sharpe of the selected trial is low: the best dev trial is mostly the luckiest of 12, and its advantage does not persist across time blocks.
2. **The selected rule trades rarely.** The strict lower-bound rule (lcb_z 2.33) plus a 2000-bar forecast window made the candidate abstain for the whole sealed holdout. Zero holdout trades means no economic evidence either way; E2/E3/E5/E6 are inconclusive and E1 fails.
3. **Phi value cannot be measured here.** The fake Phi decision maker is the deterministic ranker by construction (identical PnL for P0–P4, 100% agreement on offered sets).
4. **Latency sensitivity.** One or more bars of extra latency with the 120 s plan TTL produces zero fills (fail-closed), which must be addressed as an execution-policy decision before live-like testing.

**Outcome: NO VERIFIED EDGE on the synthetic fixture; investment verdict UNTESTED.** The gates are not weakened and the holdout is not re-used.

## Assumptions (model and data)

- Synthetic regime-switching GBM market (seed 2026), 1-minute bars, bar availability = close + 1 s; not market data.
- Fills: marketable limit from the first full bar after placement, capped at a fraction of bar volume; stops fill at the worse of stop/open with 10 bps stress; no queue-position model; maker fees unused.
- Funding: estimate known at bar end, settled 8-hourly on the position; mark = close ± small noise; illustrative maintenance-margin tiers; isolated 2x.
- Execution perturbations and cost Monte Carlo priors are declared, not fitted (listed above).
- Market-path Monte Carlo re-samples whole days of the SAME dev pool; it measures fragility, not future returns.
- Token counts are byte-based estimates for the fake backend; real counts come from the server's tokenizer (`usage`) in `bench-phi`.
- Passive benchmark: static 50/50 long matched to the candidate's average gross exposure (approximation).

## Next steps (not weaker gates, not another holdout pass)

- Capture real point-in-time exchange data (trades, books, mark/index, funding with capture times) and admissible on-chain data; freeze `research_v2` with a new sealed holdout.
- Measure the real pinned Phi model (`bench-phi`), then run P0–P4 with real Phi on the same candidate sets.
- Pre-register the execution-policy question (TTL vs latency) as its own hypothesis on dev data.
- Consider hypotheses with a larger effective sample (more instruments or shorter horizons) so gates are powered.
- Follow `docs/PROSPECTIVE_PAPER_PLAN.md` for the prospective paper period; live orders stay disabled.


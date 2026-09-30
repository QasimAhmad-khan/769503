# research_v2 — alpha discovery and simulation report

> **Scope.** Synthetic fixture and fake rule-based Phi backend: this validates the machinery. Alpha and Phi-value results are **UNTESTED**. Simulations and Monte Carlo describe fragility on re-sampled history; they do not guarantee any future Sharpe. The research_v1 holdout is spent and is not used. Live trading is disabled; `main` is unchanged.

- Manifest `research_v2` sha256 `6a69c50640fd0273a615044df25108a2e0f5a369b9170feb06f80b20dd7f7eb3` (frozen 2026-09-30T21:40:50.834085+00:00); fixture sha256 `ed1bc4664bd7ec72ccb8ba404bde46ededf2a0a5dd9820160593b3491a93ae50` (seed 4242, 180 days)
- Dates (fixture days): {'dev': [100, 145], 'dev_fold_days': 9, 'embargo_gap': [145, 146], 'holdout': [146, 179], 'train': [10, 100], 'warmup': [0, 10]}
- Trial log `research/v2/trial_log.jsonl`: 44 entries, chain valid **True**; trials used 27 of budget 48

## Verdicts

| test | verdict | reason / evidence |
|---|---|---|
| G1_walkforward_oos | **FAIL (SYNTHETIC ONLY)** | OOS daily mean 0.000415, CI [-0.0001, 0.0009] |
| G2_effective_sample | **PASS (SYNTHETIC ONLY)** | 66 effective dev trades |
| G3_deflated_sharpe | **PASS (SYNTHETIC ONLY)** | DSR 0.9583 with N=39 trials |
| G4_pbo | **PASS (SYNTHETIC ONLY)** | PBO 0.0198 |
| G5_parameter_stability | **FAIL (SYNTHETIC ONLY)** | candidate 6.6744; neighbours [None, 2.8169] |
| G6_negative_controls | **PASS (SYNTHETIC ONLY)** | candidate 6.6744 vs p95 randomized 3.3299, shifted 3.6272 |
| G7_cost_robustness | **PASS (SYNTHETIC ONLY)** | break-even cost x4.0, correlated exec MC P(net<0) 0.0144 |
| G8_latency | **PASS (SYNTHETIC ONLY)** | break-even latency 7.367 bars |
| G10_regimes | **PASS (SYNTHETIC ONLY)** | 1 powered of 9 regimes |
| L1_lookahead_e2e | **PASS** | 1919 bars compared after corrupting future data |
| G9_market_path_mc | **PASS (SYNTHETIC ONLY)** | primary median net 0.4402%, P(DD>3%) 0.0002, median sign by config {'primary': True, 'moving_16': True, 'moving_96': True, 'moving_288': True, 'stationary_16': True, 'stationary_288': True} |
| G11_event_driven_consistency | **PASS (SYNTHETIC ONLY)** | event-driven dev net 0.51%, trades 79, invariants True |
| S1_stress_safety | **PASS** | 10/10 scenarios; failures: {} |
| DEV_SURVIVOR | **NONE** | all development gates must pass |
| E_phi_value | **UNTESTED** | fake Phi = deterministic ranker (identical ablations); real Phi not available |
| R_real_phi_benchmark | **UNTESTED** | no GPU/weights in this container; `python -m cqc bench-phi` on a GPU host |
| R_fine_tuning | **UNTESTED** | train-only dataset export provided; no GPU to fine-tune |
| R_24h_soak_real_services | **UNTESTED** | requires the real Phi server |
| D_real_point_in_time_data | **UNTESTED** | no real exchange/on-chain captures reachable |
| ALPHA_VERDICT | **UNTESTED** | synthetic data only; NO VERIFIED EDGE |

## Hypotheses (Phi proposed and critiqued; deterministic code tested)

| template | family | critic verdict | concerns | causal | eligible |
|---|---|---|---|---|---|
| funding_crowding_v1 | funding | test | multiple_testing, synthetic_artifact | True | True |
| liquidity_shock_reversal_v1 | liquidity | test | multiple_testing, synthetic_artifact | True | True |
| onchain_activity_trend_v1 | onchain | test | multiple_testing, data_availability, synthetic_artifact | True | True |
| overextension_reversion_v1 | market_structure | test | multiple_testing, synthetic_artifact | True | True |
| trend_breakout_v1 | market_structure | test | multiple_testing, synthetic_artifact | True | True |
| volatility_breakout_v1 | market_structure | test | multiple_testing, synthetic_artifact | True | True |

## Train screening (all trials logged)

| trial | hypothesis | params | train Sharpe (ann., MTM) | train net % | eff. trades | eligible |
|---|---|---|---:|---:|---:|---|
| V2T00 | funding_crowding_v1 | {'thr': 0.0002} | None | 0.0 | 0 | False |
| V2T01 | funding_crowding_v1 | {'thr': 0.0003} | None | 0.0 | 0 | False |
| V2T02 | funding_crowding_v1 | {'thr': 0.0004} | None | 0.0 | 0 | False |
| V2T03 | liquidity_shock_reversal_v1 | {'k': 1.5, 'm': 2.0} | None | 0.0 | 0 | False |
| V2T04 | liquidity_shock_reversal_v1 | {'k': 1.5, 'm': 3.0} | None | 0.0 | 0 | False |
| V2T05 | liquidity_shock_reversal_v1 | {'k': 2.0, 'm': 2.0} | None | 0.0 | 0 | False |
| V2T06 | liquidity_shock_reversal_v1 | {'k': 2.0, 'm': 3.0} | None | 0.0 | 0 | False |
| V2T07 | onchain_activity_trend_v1 | {'w': 24, 'z': 1.5} | 0.7098 | 0.8119 | 93 | True |
| V2T08 | onchain_activity_trend_v1 | {'w': 24, 'z': 2.5} | None | 0.0 | 0 | False |
| V2T09 | onchain_activity_trend_v1 | {'w': 96, 'z': 1.5} | 1.0537 | 1.138 | 67 | True |
| V2T10 | onchain_activity_trend_v1 | {'w': 96, 'z': 2.5} | None | 0.0 | 0 | False |
| V2T11 | overextension_reversion_v1 | {'n': 32, 'z': 1.0} | -0.7817 | -0.1418 | 6 | False |
| V2T12 | overextension_reversion_v1 | {'n': 32, 'z': 1.5} | None | 0.0 | 0 | False |
| V2T13 | overextension_reversion_v1 | {'n': 96, 'z': 1.0} | None | 0.0 | 0 | False |
| V2T14 | overextension_reversion_v1 | {'n': 96, 'z': 1.5} | None | 0.0 | 0 | False |
| V2T15 | trend_breakout_v1 | {'fast': 8, 'slow': 48, 'z': 0.5} | 1.3535 | 1.2194 | 43 | True |
| V2T16 | trend_breakout_v1 | {'fast': 8, 'slow': 48, 'z': 1.0} | -1.8998 | -0.3081 | 10 | False |
| V2T17 | trend_breakout_v1 | {'fast': 8, 'slow': 96, 'z': 0.5} | 0.4884 | 0.4207 | 37 | True |
| V2T18 | trend_breakout_v1 | {'fast': 8, 'slow': 96, 'z': 1.0} | 0.8954 | 0.592 | 41 | True |
| V2T19 | trend_breakout_v1 | {'fast': 16, 'slow': 48, 'z': 0.5} | 1.7154 | 0.907 | 30 | True |
| V2T20 | trend_breakout_v1 | {'fast': 16, 'slow': 48, 'z': 1.0} | None | 0.0 | 0 | False |
| V2T21 | trend_breakout_v1 | {'fast': 16, 'slow': 96, 'z': 0.5} | 0.7393 | 0.6672 | 35 | True |
| V2T22 | trend_breakout_v1 | {'fast': 16, 'slow': 96, 'z': 1.0} | 0.8553 | 0.3175 | 33 | True |
| V2T23 | volatility_breakout_v1 | {'c': 0.6, 'm': 96, 'n': 16} | None | 0.0 | 0 | False |
| V2T24 | volatility_breakout_v1 | {'c': 0.6, 'm': 96, 'n': 32} | None | 0.0 | 0 | False |
| V2T25 | volatility_breakout_v1 | {'c': 0.8, 'm': 96, 'n': 16} | 4.6347 | 5.9619 | 90 | True |
| V2T26 | volatility_breakout_v1 | {'c': 0.8, 'm': 96, 'n': 32} | 2.5297 | 2.1666 | 47 | True |

## Development: purged walk-forward

- Train-eligible trials: ['V2T07', 'V2T09', 'V2T15', 'V2T17', 'V2T18', 'V2T19', 'V2T21', 'V2T22', 'V2T25', 'V2T26']
- Folds: [([100, 109], 'V2T25', -0.2207), ([109, 118], 'V2T25', -0.4188), ([118, 127], 'V2T25', 0.472), ([127, 136], 'V2T25', 1.6013), ([136, 145], 'V2T25', 0.4314)]
- OOS daily mean 0.000415, 90% CI [-0.0001, 0.0009], OOS Sharpe 3.4634819615911234
- PBO 0.0198 (CSCV with contiguous time blocks; performance = mean daily PnL per block set)
- Candidate: V2T25; diagnostic subject (not a candidate): None
- Candidate dev Sharpe 3.4634819615911234, dev net 1.8707%, effective trades 66, DSR 0.9583 (N=39)

Everything below is computed for **V2T25** (volatility_breakout_v1 {'c': 0.8, 'm': 96, 'n': 16}).

## Monte Carlo market paths (joint BTC/ETH block resampling of the train+dev pool)

Wall 733.9 s. q05 / q50 / q95.

| config | paths | net % | Sharpe | maxDD % | under water (days) | CVaR95 daily % | P(net<0) | P(DD>3%) | P(daily-loss breach) | P(no trades) |
|---|---:|---|---|---|---|---|---:|---:|---:|---:|
| primary | 5000 | -0.8496 / 0.4402 / 3.8265 | -3.8726 / 1.5198 / 6.4527 | 0.2664 / 0.8364 / 1.6741 | 3.3323 / 14.3854 / 30.5312 | -0.4803 / -0.2706 / -0.0628 | 0.3224 | 0.0002 | 0.002 | 0.023 |
| moving_16 | 1000 | -0.7403 / 0.1397 / 2.684 | -4.2569 / 0.9004 / 5.7302 | 0.0 / 0.6894 / 1.4745 | 0.0 / 15.3021 / 30.699 | -0.4159 / -0.2212 / 0.0 | 0.374 | 0.0 | 0.0 | 0.063 |
| moving_96 | 1000 | -0.8279 / 0.4521 / 3.6519 | -3.8505 / 1.667 / 6.6533 | 0.2138 / 0.8303 / 1.6353 | 2.4995 / 14.3958 / 30.4594 | -0.4877 / -0.2696 / -0.0333 | 0.297 | 0.0 | 0.007 | 0.033 |
| moving_288 | 1000 | -0.8876 / 0.3238 / 3.5881 | -3.9651 / 1.2147 / 6.252 | 0.2377 / 0.8587 / 1.6638 | 3.5089 / 15.2083 / 30.9224 | -0.4924 / -0.2757 / -0.0486 | 0.351 | 0.001 | 0.005 | 0.024 |
| stationary_16 | 1000 | -0.8251 / 0.122 / 2.6828 | -4.3483 / 0.8948 / 5.79 | 0.0 / 0.7005 / 1.4989 | 0.0 / 14.901 / 30.9797 | -0.4178 / -0.2257 / 0.0 | 0.377 | 0.001 | 0.002 | 0.069 |
| stationary_288 | 1000 | -0.8301 / 0.4477 / 3.8058 | -3.7647 / 1.5341 / 6.3659 | 0.2951 / 0.8851 / 1.6694 | 3.5307 / 14.9219 / 30.7714 | -0.5226 / -0.2823 / -0.0703 | 0.331 | 0.001 | 0.007 | 0.018 |

Portfolio-return bootstrap of the subject's own dev daily MTM returns:

| scheme/block | net % | Sharpe | maxDD % | P(net<0) | P(DD>3%) |
|---|---|---|---|---:|---:|
| moving_1d | -0.6134 / 1.99 / 4.6444 | -1.1901 / 3.7207 / 8.344 | 0.4443 / 0.8739 / 1.9144 | 0.0978 | 0.0016 |
| stationary_1d | -0.6063 / 1.8831 / 4.548 | -1.1742 / 3.5381 / 8.2625 | 0.4324 / 0.881 / 1.9051 | 0.1112 | 0.002 |
| moving_3d | -0.3324 / 1.6985 / 3.7864 | -0.6055 / 3.2411 / 7.3297 | 0.4645 / 0.8226 / 1.6494 | 0.0868 | 0.0006 |
| stationary_3d | -0.4139 / 1.8669 / 4.1468 | -0.7828 / 3.5137 / 7.8198 | 0.4615 / 0.8132 / 1.6756 | 0.0866 | 0.0014 |
| moving_7d | -0.65 / 1.6666 / 3.9832 | -1.2418 / 3.1998 / 7.8154 | 0.4645 / 0.8519 / 1.7877 | 0.121 | 0.0012 |
| stationary_7d | -0.4927 / 1.8505 / 4.1 | -0.8817 / 3.4638 / 7.8096 | 0.4486 / 0.8656 / 1.6316 | 0.0924 | 0.0004 |

## Execution uncertainty and break-even

- Base dev (screener): {'net_return_pct': 2.9759, 'sharpe_ann_mtm': 6.6744, 'days': 46, 'trades': 47, 'effective_trades': 43, 'expectancy_quote': 6.3317, 'max_drawdown_pct': 0.6544, 'longest_under_water_bars': 708, 'cvar95_daily_pct': -0.2099, 'liquidations': 0, 'limit_breach_bars': {'daily_loss': 0, 'drawdown': 0}}
- Correlated execution MC (5000 draws, rho 0.6): net % 0.4448 / 2.2951 / 3.2209, Sharpe 1.3942 / 5.0284 / 6.6226, P(net<0) 0.0144, P(net<0 | top-decile stress) 0.124, corr(stress, net) -0.7584
- Cost multiplier curve (x, net %): [[0.5, 3.4007], [1, 2.9759], [1.5, 1.4029], [2, 0.6018], [3, 0.082], [4, 0.0], [6, 0.0], [8, 0.0]] → break-even x4.0
- Latency curve (bars, net %): [[0, 2.9759], [1, 2.4602], [2, 2.0864], [3, 1.7879], [4, 1.1194], [5, 0.8058], [6, 0.6762], [7, 0.074], [8, -0.1279]] → break-even 7.367 bars

## Robustness and negative controls

- Grid neighbours: [({'c': 0.6, 'm': 96, 'n': 16}, None), ({'c': 0.8, 'm': 96, 'n': 32}, 2.8169)]
- Decision-time offsets (5/10 min): [('5', 2.6912, 1.0997), ('10', -0.6777, -0.208)]
- Block-randomized signals (200, exposure-matched): Sharpe -4.2264 / -1.4595 / 3.3299; subject percentile 1.0
- Time-shifted signals (200): Sharpe -4.5525 / -0.7584 / 3.6272; subject percentile 1.0
- Delayed on-chain availability: [('3600', 2.9759, 47), ('21600', 2.9759, 47)]
- Lookahead (end-to-end corruption of future data): {'equity_before_corruption_unchanged': True, 'bars_compared': 1919, 'survivorship': 'universe fixed to BTC/ETH survivors: a real study must use a point-in-time listing universe'}
- Data quality (ingest dispositions): {'ingest_dispositions': {'accepted': 518400}, 'note': 'synthetic data is clean by construction; real feeds will not be'}

| regime | days | trades | eff. | expectancy | CI90 | verdict |
|---|---:|---:|---:|---:|---|---|
| bear | 23 | 31 | 28 | 9.9146 | None | INCONCLUSIVE (underpowered) |
| bull | 22 | 16 | 15 | -0.6102 | None | INCONCLUSIVE (underpowered) |
| chop | 21 | 23 | 19 | 2.3644 | None | INCONCLUSIVE (underpowered) |
| correlated_shock | 7 | 7 | 7 | 19.3821 | None | INCONCLUSIVE (underpowered) |
| funding_extreme | 45 | 47 | 43 | 6.3317 | [2.9288, 9.8658] | PASS |
| high_vol | 22 | 19 | 18 | 5.7543 | None | INCONCLUSIVE (underpowered) |
| low_vol | 23 | 28 | 25 | 6.7235 | None | INCONCLUSIVE (underpowered) |
| trend | 24 | 24 | 24 | 10.1337 | None | INCONCLUSIVE (underpowered) |
| weekend_illiquid | 12 | 11 | 8 | -1.6815 | None | INCONCLUSIVE (underpowered) |

## Event-driven replay (full runtime, dev) and Phi ablations

| policy | net % | Sharpe | trades | invariants | Phi calls | decision isolation |
|---|---:|---:|---:|---|---:|---|
| P0_deterministic | 0.51 | 1.0349069799840052 | 79 | True | 0 | {'decisions': 0, 'agree_with_rank': 0} |
| P1_phi_screening | 0.51 | 1.0349069799840052 | 79 | True | 7870 | {'decisions': 0, 'agree_with_rank': 0} |
| P2_screening_analysis | 0.51 | 1.0349069799840052 | 79 | True | 7953 | {'decisions': 0, 'agree_with_rank': 0} |
| P3_all_four_roles | 0.51 | 1.0349069799840052 | 79 | True | 8036 | {'decisions': 83, 'agree_with_rank': 83} |
| P4_all_four_no_onchain | 0.51 | 1.0349069799840052 | 79 | True | 8036 | {'decisions': 83, 'agree_with_rank': 83} |

Stress scenarios (strategy exposed: {'hypothesis': 'volatility_breakout_v1', 'params': {'c': 0.8, 'm': 96, 'n': 16}}):

| scenario | verdict | worst trade | loss bound | final state | protective actions | reasons |
|---|---|---:|---:|---|---|---|
| crash_-15pct_1h | PASS | -9.2571486 | 300.0 | NORMAL | [['stop_market', 'protect_position'], ['market', 'max_holding_time'], ['stop_market', 'protect_position'], ['market', 'max_holding_time'], ['stop_market', 'protect_position']] | [] |
| short_squeeze_+20pct_30m | PASS | -26.8706448 | 400.0 | NORMAL | [['stop_market', 'protect_position']] | [] |
| vol_regime_x3_24h | PASS | -2.795003093713159 | 50.0 | NORMAL | [['stop_market', 'protect_position'], ['market', 'max_holding_time']] | [] |
| correlated_drop_-8pct_15m | PASS | -10.0189161 | 200.0 | NORMAL | [['stop_market', 'protect_position'], ['market', 'max_holding_time'], ['stop_market', 'protect_position'], ['market', 'max_holding_time'], ['stop_market', 'protect_position']] | [] |
| liquidation_cascade_gap_-25pct | PASS | 190.6590229161873 | 500.0 | NORMAL | [['stop_market', 'protect_position'], ['market', 'max_holding_time']] | [] |
| squeeze_gap_+25pct | PASS | -199.9209267 | 500.0 | NORMAL | [['stop_market', 'protect_position']] | [] |
| repeated_losses_whipsaw_2d | PASS | -28.2969576 | 50.0 | NORMAL | [['stop_market', 'protect_position']] | [] |
| exchange_outage_45m | PASS | -15.1037845 | 50.0 | NORMAL | [['stop_market', 'protect_position'], ['market', 'max_holding_time'], ['stop_market', 'protect_position'], ['market', 'max_holding_time']] | [] |
| stale_feed_20m | PASS | -4.193102045083602 | 50.0 | NORMAL | [['stop_market', 'protect_position'], ['market', 'max_holding_time']] | [] |
| delayed_onchain_+5h | PASS | -15.1037845 | 50.0 | NORMAL | [['stop_market', 'protect_position'], ['market', 'max_holding_time'], ['stop_market', 'protect_position'], ['market', 'max_holding_time']] | [] |

## Why nothing survived, and what is needed next

1. **G1 fails**: the purged walk-forward out-of-sample mean daily return's 90% CI includes zero. The evidence is not strong enough even on synthetic data.
2. **G5 fails**: the candidate is parameter-fragile. One grid neighbour never trades and the other keeps under half the Sharpe. Shifting the decision clock by 10 minutes turns the dev result negative, so the result depends on bar alignment.
3. **Fidelity gap** (see diagnoses): the screener's cold-start entry gate overstated trades that start at the dev boundary, and the event-driven simulator earns much less (+0.51% vs +1.87%) because of 1-minute execution, price envelopes, TTLs and sizing rooms. Screening must be warm-started (`warm_from`) and finalists must be judged on the event-driven simulator.
4. **Synthetic artifacts**: breakout and trend edges exist because the generator switches drift regimes every 4 hours. The on-chain hypothesis passed train eligibility on pure-noise on-chain data, a live illustration of multiple-testing false positives.
5. **Phi value is unmeasurable here**: the fake decision maker agreed with the ranker on 83/83 decisions.

**Outcome: NO VERIFIED EDGE; alpha and Phi value UNTESTED. The research_v2 holdout stays sealed.**

Needed next (research_v3, new manifest):
- Real point-in-time exchange data (trades, L2, mark/index, funding with receive timestamps) for BTC/ETH plus a point-in-time listing universe; forward-captured on-chain data with finality.
- The real pinned Phi model measured with `bench-phi` (for example via `deploy/colab_phi_benchmark.ipynb` or a local GPU), then P0-P4 on identical offered candidate sets.
- Screener gate warm-start and event-driven evaluation of every finalist; decision-time offsets pre-registered as a robustness gate; a wider but still bounded neighbourhood grid, so stability can be assessed.
- Hypotheses with more independent observations per unit time (more instruments), to power the regime slices.

Fine-tune dataset (train only): {'examples': 80, 'dropped_after_train_cutoff': 0, 'label_equals_ranker': 48, 'abstain_labels': 32, 'path': '/home/user/769503/reports/validation_v2/finetune_train_only.jsonl', 'status': 'dataset only; fine-tuning UNTESTED (no GPU)'}


## Holdout

Not opened: no candidate survived development; the research_v2 holdout stays sealed for a future pre-registered candidate.

## Trial-log decisions and diagnoses

- [hypothesis_eligibility] {"eligible": ["funding_crowding_v1", "liquidity_shock_reversal_v1", "onchain_activity_trend_v1", "overextension_reversion_v1", "trend_breakout_v1", "volatility_breakout_v1"], "rule": "proposed AND critic did not demand missing data/untestable AND causality check passes"}
- [dev_walkforward] {"candidate": "V2T25", "diagnostic_subject": null, "oos_ci90": [-0.0001, 0.0009], "pbo": 0.0198}
- [gates] {"survivor": false, "verdicts": {"ALPHA_VERDICT": "UNTESTED", "DEV_SURVIVOR": "NONE", "D_real_point_in_time_data": "UNTESTED", "E_phi_value": "UNTESTED", "G10_regimes": "PASS (SYNTHETIC ONLY)", "G11_event_driven_consistency": "PASS (SYNTHETIC ONLY)", "G1_walkforward_oos": "FAIL (SYNTHETIC ONLY)", "G2_effective_sample": "PASS (SYNTHETIC ONLY)", "G3_deflated_sharpe": "PASS (SYNTHETIC ONLY)", "G4_pbo
- [holdout_not_opened] {"reason": "no candidate survived development gates; the research_v2 holdout stays sealed for a future, pre-registered candidate"}
- [diagnosis] {"action": "not re-screened inside research_v2: a clean re-run of all 27 round-0 trials would exceed the 48-trial budget (27 + 27), and re-running only survivors after seeing dev results would be selection leakage; the fix (warm_from) is carried into research_v3", "classification": "simulator defect in the screener (discards available pre-start history for the entry gate); the remaining gap reflec
- [diagnosis] {"action": "disclosed; conclusions unchanged because the candidate already fails G1 and G5; fixed going forward via vbt.run(warm_from=...)", "finding": "phase_robust and phase_mc evaluate windows that begin with empty gate memory (base dev 2.98% vs 1.87% with the warm gate the walk-forward used); comparisons within those phases are internally consistent but optimistic relative to the walk-forward 

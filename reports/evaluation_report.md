# Ablation evaluation report

> Harness demonstration on SYNTHETIC data with a FAKE rule-based Phi backend (token counts are estimates, not tokenizer output). No alpha, model-quality or live-readiness claim is made or supported. Promotion is blocked.

## Policies

| policy | net PnL | max DD | trades | fees | funding | avg gross/equity | abstain | Phi calls | Phi tokens in/out | wall s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| P0_deterministic_baseline | -82.4234 | 87.027 | 7 | 3.999488 | -0.30191 | 0.00244 | 0.9983 | 0 | 0/0 | 42.8 |
| P1_phi_screening | -82.4234 | 87.027 | 7 | 3.999488 | -0.30191 | 0.00244 | 0.9983 | 7123 | 7603706/1743888 | 62.8 |
| P2_phi_screening_analysis | -82.4234 | 87.027 | 7 | 3.999488 | -0.30191 | 0.00244 | 0.9983 | 7130 | 7610694/1744776 | 63.4 |
| P3_phi_all_four_roles | -82.4234 | 87.027 | 7 | 3.999488 | -0.30191 | 0.00244 | 0.9983 | 7137 | 7617134/1745028 | 62.5 |
| P4_all_four_without_onchain | -82.4234 | 87.027 | 7 | 3.999488 | -0.30191 | 0.00244 | 0.9983 | 7137 | 7167594/1405758 | 60.8 |
| A_cash | 0.0 | - | - | - | - | - | - | - | -/- | - |
| B_exposure_matched_static_long | 6.2414 | - | - | - | - | 0.00244 | - | - | -/- | - |

## Paired comparisons (daily PnL differences)

| policy | vs | days | mean daily diff | 90% block-bootstrap CI | reading |
|---|---|---:|---:|---|---|
| P0_deterministic_baseline | A_cash | 23 | -3.5836 | [-8.0351, -0.4691] | negative |
| P0_deterministic_baseline | B_exposure_matched_static_long | 23 | -3.855 | [-8.1698, -0.8888] | negative |
| P1_phi_screening | P0_deterministic_baseline | 23 | 0.0 | [0.0, 0.0] | inconclusive |
| P2_phi_screening_analysis | P1_phi_screening | 23 | 0.0 | [0.0, 0.0] | inconclusive |
| P3_phi_all_four_roles | P2_phi_screening_analysis | 23 | 0.0 | [0.0, 0.0] | inconclusive |
| P3_phi_all_four_roles | P4_all_four_without_onchain | 23 | 0.0 | [0.0, 0.0] | inconclusive |

## Decision isolation (same offered candidate sets)

- P3_phi_all_four_roles: {'decisions': 7, 'agree_with_deterministic_rank': 7, 'phi_abstained': 0}
- P4_all_four_without_onchain: {'decisions': 7, 'agree_with_deterministic_rank': 7, 'phi_abstained': 0}

## Chronological folds (weekly net PnL)

- fold 0 2026-01-09→2026-01-15: P0_deterministic_baseline=-10.788, P1_phi_screening=-10.788, P2_phi_screening_analysis=-10.788, P3_phi_all_four_roles=-10.788, P4_all_four_without_onchain=-10.788, A_cash=0.0, B_exposure_matched_static_long=2.993
- fold 1 2026-01-16→2026-01-22: P0_deterministic_baseline=0.0, P1_phi_screening=0.0, P2_phi_screening_analysis=0.0, P3_phi_all_four_roles=0.0, P4_all_four_without_onchain=0.0, A_cash=0.0, B_exposure_matched_static_long=0.237
- fold 2 2026-01-23→2026-01-29: P0_deterministic_baseline=-71.635, P1_phi_screening=-71.635, P2_phi_screening_analysis=-71.635, P3_phi_all_four_roles=-71.635, P4_all_four_without_onchain=-71.635, A_cash=0.0, B_exposure_matched_static_long=1.871
- fold 3 2026-01-30→2026-01-31: P0_deterministic_baseline=0.0, P1_phi_screening=0.0, P2_phi_screening_analysis=0.0, P3_phi_all_four_roles=0.0, P4_all_four_without_onchain=0.0, A_cash=0.0, B_exposure_matched_static_long=1.141

## Promotion blockers

- promotion.research_manifest_id unresolved
- promotion.minimum_effective_sample_count unresolved
- promotion.net_ev_hurdle_quote unresolved
- promotion.paired_improvement_hurdle unresolved
- promotion.required_confidence_method unresolved
- promotion.prospective_evaluation_complete is false
- resources.host_ram_budget_gib unmeasured
- resources.gpu_vram_budget_gib unmeasured
- resources.disk_quota_gib unmeasured
- fake Phi backend configured (phi.backend)
- phi.model_revision unresolved (RESOLVE_AND_PIN)
- phi.runtime unresolved (SELECT_AFTER_GPU_DISCOVERY)
- phi.quantization unresolved (BENCHMARK_SUPPORTED_4BIT_AGAINST_REFERENCE)
- Phi decision-maker value vs deterministic_rank_v1 not established on real point-in-time data
- venue margin rules not configured for a real venue
- no real venue selected

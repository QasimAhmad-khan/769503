# Ablation evaluation report

> Harness demonstration on SYNTHETIC data with FAKE model adapters. No alpha, model-quality or live-readiness claim is made or supported. Promotion is blocked.

## Policies

| policy | net PnL | max DD | trades | fees | funding | avg gross/equity | abstain | Phi calls | Jev calls | wall s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| C_deterministic | -82.4234 | 87.027 | 7 | 3.999488 | -0.30191 | 0.00244 | 0.9983 | 0 | 0 | 43.1 |
| D_fake_phi_screen_deterministic_choice | -82.4234 | 87.027 | 7 | 3.999488 | -0.30191 | 0.00244 | 0.9983 | 10122 | 0 | 58.4 |
| E_fake_phi_plus_fake_jev | -82.4234 | 87.027 | 7 | 3.999488 | -0.30191 | 0.00244 | 0.9983 | 10122 | 7 | 58.7 |
| F_E_without_onchain | -82.4234 | 87.027 | 7 | 3.999488 | -0.30191 | 0.00244 | 0.9983 | 10122 | 7 | 60.5 |
| A_cash | 0.0 | - | - | - | - | - | - | - | - | - |
| B_exposure_matched_static_long | 6.2414 | - | - | - | - | 0.00244 | - | - | - | - |

## Paired comparisons (daily PnL differences)

| policy | vs | days | mean daily diff | 90% block-bootstrap CI | reading |
|---|---|---:|---:|---|---|
| C_deterministic | A_cash | 23 | -3.5836 | [-8.0351, -0.4691] | negative |
| C_deterministic | B_exposure_matched_static_long | 23 | -3.855 | [-8.1698, -0.8888] | negative |
| D_fake_phi_screen_deterministic_choice | C_deterministic | 23 | 0.0 | [0.0, 0.0] | inconclusive |
| E_fake_phi_plus_fake_jev | D_fake_phi_screen_deterministic_choice | 23 | 0.0 | [0.0, 0.0] | inconclusive |
| E_fake_phi_plus_fake_jev | F_E_without_onchain | 23 | 0.0 | [0.0, 0.0] | inconclusive |

## Chronological folds (weekly net PnL)

- fold 0 2026-01-09→2026-01-15: C_deterministic=-10.788, D_fake_phi_screen_deterministic_choice=-10.788, E_fake_phi_plus_fake_jev=-10.788, F_E_without_onchain=-10.788, A_cash=0.0, B_exposure_matched_static_long=2.993
- fold 1 2026-01-16→2026-01-22: C_deterministic=0.0, D_fake_phi_screen_deterministic_choice=0.0, E_fake_phi_plus_fake_jev=0.0, F_E_without_onchain=0.0, A_cash=0.0, B_exposure_matched_static_long=0.237
- fold 2 2026-01-23→2026-01-29: C_deterministic=-71.635, D_fake_phi_screen_deterministic_choice=-71.635, E_fake_phi_plus_fake_jev=-71.635, F_E_without_onchain=-71.635, A_cash=0.0, B_exposure_matched_static_long=1.871
- fold 3 2026-01-30→2026-01-31: C_deterministic=0.0, D_fake_phi_screen_deterministic_choice=0.0, E_fake_phi_plus_fake_jev=0.0, F_E_without_onchain=0.0, A_cash=0.0, B_exposure_matched_static_long=1.141

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
- fake model adapters configured (jev.transport / phi.backend)
- jev.calibration_artifact_id missing (live requires locked calibration)
- phi.model_revision unresolved (RESOLVE_AND_PIN)
- phi.runtime unresolved (SELECT_AFTER_GPU_DISCOVERY)
- phi.quantization unresolved (BENCHMARK_SUPPORTED_4BIT_AGAINST_REFERENCE)
- venue margin rules not configured for a real venue
- no real venue selected

# Fault-injection results

24/24 scenarios passed (30.0 s). SYNTHETIC data, FAKE rule-based Phi backend.

| scenario | injected | expected | observed | pass |
|---|---|---|---|---|
| baseline | none | one authorized entry | 1 entry intents | yes |
| phi_decision_timeout | Phi decision_maker mode=timeout | no entry intent; decision record ABSTAIN; no probability fabricated | entries=0 decision_status=['timeout'] locks=['phi_health'] | yes |
| phi_decision_oom | Phi decision_maker mode=oom | no entry intent; decision record ABSTAIN; no probability fabricated | entries=0 decision_status=['unavailable'] locks=['phi_health'] | yes |
| phi_decision_malformed | Phi decision_maker mode=malformed | no entry intent; decision record ABSTAIN; no probability fabricated | entries=0 decision_status=['invalid'] locks=['phi_health'] | yes |
| phi_decision_invent_candidate | Phi decision_maker mode=invent_candidate | no entry intent; decision record ABSTAIN; no probability fabricated | entries=0 decision_status=['invalid'] locks=['phi_health'] | yes |
| phi_decision_schema_violation | Phi decision_maker mode=schema_violation | no entry intent; decision record ABSTAIN; no probability fabricated | entries=0 decision_status=['invalid'] locks=['phi_health'] | yes |
| phi_decision_abstain | Phi decision_maker mode=abstain | no entry intent; decision record ABSTAIN; no probability fabricated | entries=0 decision_status=['valid'] locks=[] | yes |
| phi_malformed | Phi backend mode=malformed | no entry intent; cycle blocked/abstained | entries=0 blocked_cycles=6 locks=['phi_health'] | yes |
| phi_timeout | Phi backend mode=timeout | no entry intent; cycle blocked/abstained | entries=0 blocked_cycles=6 locks=['phi_health'] | yes |
| phi_oom | Phi backend mode=oom | no entry intent; cycle blocked/abstained | entries=0 blocked_cycles=6 locks=['phi_health'] | yes |
| phi_schema_violation | Phi backend mode=schema_violation | no entry intent; cycle blocked/abstained | entries=0 blocked_cycles=6 locks=['phi_health'] | yes |
| phi_injected | Phi backend mode=injected | no entry intent; cycle blocked/abstained | entries=0 blocked_cycles=6 locks=[] | yes |
| phi_oom_after_entry | Phi OOM while a position is open | protective stop placed and position exited by deterministic rules without Phi | protective_actions=1 flat_after_5h=True new_entries=0 | yes |
| audit_persistence_failure | 3 failed audit writes at the decision bar | no entry; NO_NEW_RISK(audit_failure) | entries=0 locks=['audit_failure'] | yes |
| ack_loss_restart | ack dropped after venue accepted the entry; worker restart | one venue order only; intent reconciled; recovery completes; stale worker fenced | unknown_before_restart=1 venue_entry_orders=1 intent_status=filled recoveries=2 stale_worker_fenced=True | yes |
| duplicate_fill_delivery | every fill batch re-delivers its first fill | fills deduplicated by venue fill ID; local position equals venue | venue_fills=2 ledger_fills=2 discrepancies=[] | yes |
| missing_required_evidence | market-context connector removed | abstain with REQUIRED_EVIDENCE_MISSING; never zero-filled | entries=0 missing_cycles=6 | yes |
| operator_halt | operator kill switch before the entry bar | no entries; lock not clearable without operator | entries=0 state=HALTED non_operator_clear=False | yes |
| alternate_entry_routes_and_fallbacks | legacy/manual/grid routes, another model backend, a selector fallback, a legacy Jev receipt | only autonomous_cycle with the configured Phi backend + selector passes | routes={'autonomous_cycle': True, 'legacy_ai_filter': False, 'manual_signal': False, 'grid_strategy': False} other_model=False selector_fallback=False legacy_jev=False | yes |
| admission_account_changed | wallet changed between authorization and dispatch | entry rejected at dispatch with ACCOUNT_VERSION_CHANGED / EQUITY_CHANGED; nothing sent | queued=1 rejections=[['ACCOUNT_VERSION_CHANGED', 'EQUITY_CHANGED_OR_UNKNOWN']] venue_orders=0 | yes |
| admission_price_collar | executable quote moved 2% after authorization | entry rejected at dispatch with PRICE_OUTSIDE_COLLAR; nothing sent | rejections=[['PRICE_OUTSIDE_COLLAR']] venue_orders=0 | yes |
| slow_phi_does_not_delay_protection | 2 s Phi call in flight + mark near liquidation | reduce-only protective order placed while Phi is still busy | exit_placed=True reaction_ms=20.7 phi_still_busy=True loop_errors=[] | yes |
| legacy_jev_ledger | ledger containing Open-Jev-era decision_receipt/jev_raw_response records | records identified as legacy, excluded from Phi replay, rejected by the entry gate | detected={'legacy_jev_raw_response_v1': 1, 'legacy_jev_selection_v1': 1} replay_recordings=0 gate_accepts_legacy=False | yes |
| four_roles_one_backend | normal run + one risk review | screener, analyzer, decision_maker, risk_analyst all served by the same backend/model revision; max concurrent inference 1 | calls_by_role={'screener': 6, 'analyzer': 7, 'decision_maker': 1, 'risk_analyst': 1} producers=['fake_phi_rules_v2@fake'] decision_backends=1 max_concurrency=1 | yes |

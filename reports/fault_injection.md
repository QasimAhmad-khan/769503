# Fault-injection results

19/19 scenarios passed (22.5 s). SYNTHETIC data, FAKE model adapters.

| scenario | injected | expected | observed | pass |
|---|---|---|---|---|
| baseline | none | one authorized entry | 1 entry intents | yes |
| jev_timeout | Jev transport mode=timeout | no entry intent; receipt ABSTAIN; NO_NEW_RISK lock | entries=0 receipt_status=['timeout'] locks=['jev_health'] | yes |
| jev_unavailable | Jev transport mode=unavailable | no entry intent; receipt ABSTAIN; NO_NEW_RISK lock | entries=0 receipt_status=['unavailable'] locks=['jev_health'] | yes |
| jev_malformed | Jev transport mode=malformed | no entry intent; receipt ABSTAIN; NO_NEW_RISK lock | entries=0 receipt_status=['invalid'] locks=['jev_health'] | yes |
| jev_bad_label | Jev transport mode=bad_label | no entry intent; receipt ABSTAIN; NO_NEW_RISK lock | entries=0 receipt_status=['invalid'] locks=['jev_health'] | yes |
| jev_unnormalized | Jev transport mode=unnormalized | no entry intent; receipt ABSTAIN; NO_NEW_RISK lock | entries=0 receipt_status=['invalid'] locks=['jev_health'] | yes |
| jev_nan | Jev transport mode=nan | no entry intent; receipt ABSTAIN; NO_NEW_RISK lock | entries=0 receipt_status=['invalid'] locks=['jev_health'] | yes |
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
| alternate_entry_routes | legacy/manual/grid routes and an LLM-fallback provider | only autonomous_cycle with the configured provider passes | routes={'autonomous_cycle': True, 'legacy_ai_filter': False, 'manual_signal': False, 'grid_strategy': False} llm_fallback_allowed=False | yes |

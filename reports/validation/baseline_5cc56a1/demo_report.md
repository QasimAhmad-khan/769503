# Paper demo report

> SYNTHETIC market data and a FAKE rule-based Phi backend (labeled 'fake_phi_rules_v2'). Demonstrates mechanics and audit linkage only — not model quality, not trading edge.

## 0. One Phi backend, four roles

- backend: `in-process://fake-phi|fake_phi_rules_v2@fake`
- calls by role: {'screener': 21, 'analyzer': 37, 'decision_maker': 3, 'risk_analyst': 1}
- max concurrent inference: 1 (serialized admission queue)

## 1. Eligible paper trade (linked audit chain)

- cycle `cycle_8d920a882f726ec1`, snapshot `snap_d5bf65830412ed8a`
- analyzer EvidenceRequest `req_a77d0415fcd70e08` round 0 (trend_breakout_v1): [('spread_bps', 'venue_market_data', True), ('estimated_funding_rate', 'venue_market_data', True), ('realized_vol_15m', 'venue_market_data', True), ('chain_large_transfer_count_1h', 'onchain_finalized', False)]
- screener fetch_plan: [('spread_bps', 'fetch', ['sim_venue_market_data']), ('estimated_funding_rate', 'fetch', ['sim_venue_market_data']), ('realized_vol_15m', 'fetch', ['sim_venue_market_data']), ('chain_large_transfer_count_1h', 'fetch', ['synthetic_chain_fixture'])]
  - evidence spread_bps: available source=sim_venue_market_data src_ts=2026-01-23T12:45:00Z available=2026-01-23T12:45:01Z lag=1.0s unit=bps sha=f75e3c2639b2
  - evidence estimated_funding_rate: available source=sim_venue_market_data src_ts=2026-01-23T12:45:00Z available=2026-01-23T12:45:01Z lag=1.0s unit=decimal_per_funding_interval sha=5ad07a02a117
  - evidence realized_vol_15m: available source=sim_venue_market_data src_ts=2026-01-23T12:45:00Z available=2026-01-23T12:45:01Z lag=1.0s unit=decimal_per_bar sha=cb78d2fa8d5b
  - evidence chain_large_transfer_count_1h: available source=synthetic_chain_fixture src_ts=2026-01-23T11:00:00Z available=2026-01-23T12:00:00Z lag=3600.0s unit=count_per_hour sha=ef6fc7f7e1aa
- candidate `cand_86330df79b5c5d11` OPEN_LONG qty=10 stop=72206.5 LCB=0.03 USDT (n_eff=55)
- candidate `cand_befc93be1eee1cb6` OPEN_LONG qty=5 stop=72206.5 LCB=0.01 USDT (n_eff=55)
- analyzer AnalysisPacket: candidates passed=['cand_86330df79b5c5d11', 'cand_befc93be1eee1cb6']
- decision `dec_ca9dcee5848f83b1` by `phi_decision_maker` (fake_phi_rules_v2@fake): offered ['cand_86330df79b5c5d11', 'cand_befc93be1eee1cb6'] + ABSTAIN → `cand_86330df79b5c5d11` reasons=['EVIDENCE_SUPPORTS']; probability=None (none_uncalibrated_generative_choice)
- authorization `auth_b53b658bedae9c48` → intent `int_b9346ebc0d2852be` status=filled (bound to account version acct_fbff4a5fc9e3ce27053e, equity 10000)
- fill `fill_76bb1dfb54943824` 10 @ 74403.5 fee 0.37201750
- protection: stop_market sell 10 BTCUSDT (protect_position) stop=72206.5
- protection: stop_market sell 20 ETHUSDT (protect_position) stop=2908.18
- protection: stop_market sell 20 ETHUSDT (protect_position) stop=2800.83
- protection: market sell 20 ETHUSDT (max_holding_time) stop=None
- outcome: BTCUSDT net PnL -23.4256885 USDT
- outcome: ETHUSDT net PnL -23.600558 USDT
- outcome: ETHUSDT net PnL 1.80939282194076437913091007825922 USDT

## 2. Abstention

- deterministic abstentions in window: 61 (reasons: LCB_BELOW_HURDLE_0.5, LCB_BELOW_HURDLE_1.0, NO_ELIGIBLE_CANDIDATE, NO_REGISTERED_SIGNAL)
- Phi decision ABSTAIN record: `{"decision_id": "dec_79eb898e83eccfbe", "candidate_ids": ["cand_53bc63a423e25879", "cand_1b17c5c390b1cc26"], "selected_id": "ABSTAIN", "validation_status": "valid", "reason_codes": ["INSUFFICIENT_EVIDENCE"], "calibrated_selection_probability": null}`; entries: 0

## 3. Partial fill

- ordered 10, filled 2, status canceled, reservation released (remaining 0)

## 4. Risk reduction (deterministic; Phi advisory only)

- market sell 20 ETHUSDT reason=max_holding_time
- stop exit sell 10 BTCUSDT @ 72134.2
- stop exit sell 20 ETHUSDT @ 2905.27
- risk_analyst proposal: no_change/none → engine: [[]]

## 5. Dispatch-time admission (fresh account/market re-check)

- admission_account_changed: queued=1 rejections=[['ACCOUNT_VERSION_CHANGED', 'EQUITY_CHANGED_OR_UNKNOWN']] venue_orders=0 → passed=True
- admission_price_collar: rejections=[['PRICE_OUTSIDE_COLLAR']] venue_orders=0 → passed=True

## 6. Protection independent of Phi

- 2 s Phi call in flight + mark near liquidation: exit_placed=True reaction_ms=20.7 phi_still_busy=True loop_errors=[] → passed=True

## 7. Restart recovery

- ack dropped after venue accepted the entry; worker restart: unknown_before_restart=1 venue_entry_orders=1 intent_status=filled recoveries=2 stale_worker_fenced=True → passed=True
- `worker_started` 2026-01-23T12:00:00Z {"owner": "worker-1", "token": 1}
- `recovery_complete` 2026-01-23T12:00:00Z {"state": "NORMAL"}
- `order_unknown` 2026-01-23T12:45:03Z {"error": "VenueTimeout", "intent_id": "int_067d32845285f1cb"}
- `worker_started` 2026-01-23T12:45:00Z {"owner": "worker-2", "token": 2}
- `recovery_complete` 2026-01-23T12:45:00Z {"state": "NORMAL"}

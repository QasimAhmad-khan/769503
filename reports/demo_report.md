# Paper demo report

> SYNTHETIC market data and FAKE Phi/Jev adapters (labeled). Demonstrates mechanics and audit linkage only; it is not evidence of trading edge or model quality.

## 1. Eligible paper trade (linked audit chain)

- cycle correlation: `cycle_8d920a882f726ec1`  snapshot: `snap_918cdb64006f63ee`
- research request: `req_a77d0415fcd70e08`; evidence: `ev_f4e4d3e6996ab6ab`, `ev_6c466a243c3b18f6`, `ev_57aff170a0f8bb13`, `ev_932e05dbeed0eb8d`
- candidate `cand_2b34ae20b10bf57c` OPEN_LONG qty=10 stop=72206.5 LCB=0.03 USDT (n_eff=55)
- candidate `cand_2f3f5ca8164eb1e6` OPEN_LONG qty=5 stop=72206.5 LCB=0.01 USDT (n_eff=55)
- decision `dec_b5565a54bd1e3a9e` provider=fake_jev_not_a_model selected=`cand_2b34ae20b10bf57c` scores={'ABSTAIN': 0.15, 'cand_2b34ae20b10bf57c': 0.7, 'cand_2f3f5ca8164eb1e6': 0.15} (categorical_preference_not_win_probability)
- authorization `auth_58490f946264823a` → intent `int_8c4894641c1c46a8` (client order `cqc8f68387f3c766f382b52205d74c1d`) status=filled
- fill `fill_76bb1dfb54943824` 10 @ 74403.5 fee 0.37201750
- protection: stop_market sell 10 (protect_position) stop=72206.5
- protection: stop_market sell 20 (protect_position) stop=2908.18
- protection: stop_market sell 20 (protect_position) stop=2800.83
- protection: market sell 20 (max_holding_time) stop=None
- outcome: BTCUSDT net PnL -23.4256885 USDT (closed 2026-01-23T14:31:01Z)
- outcome: ETHUSDT net PnL -23.600558 USDT (closed 2026-01-23T14:53:01Z)
- outcome: ETHUSDT net PnL 1.80939282194076437913091007825922 USDT (closed 2026-01-23T19:04:01Z)

## 2. Abstention

- deterministic abstentions in window: 61
- reasons seen: LCB_BELOW_HURDLE_0.5, LCB_BELOW_HURDLE_1.0, NO_ELIGIBLE_CANDIDATE, NO_REGISTERED_SIGNAL
- Jev ABSTAIN receipt: `{"decision_id": "dec_0a9498ed0bee0beb", "candidate_ids": ["cand_2b34ae20b10bf57c", "cand_2f3f5ca8164eb1e6"], "selected_id": "ABSTAIN", "validation_status": "valid", "reason_codes": ["MODEL_ABSTAINED"]}`; entries created: 0

## 3. Partial fill

- intent `int_97697afc92e5a3cc` ordered 10, filled 2, status canceled; reservation status released (remaining reserved 0)
- fill `fill_76bb1dfb54943824` 1 @ 74403.5 at 2026-01-23T12:47:00Z
- fill `fill_d6e1f9405faab3de` 1 @ 74374.0 at 2026-01-23T12:48:00Z

## 4. Risk reduction (deterministic, no model)

- market sell 20 ETHUSDT reason=max_holding_time
- stop exit `fill_3adab716effcd595` sell 10 BTCUSDT @ 72134.2
- stop exit `fill_56d48530a22e64fe` sell 20 ETHUSDT @ 2905.27

## 5. Restart recovery

- injected: ack dropped after venue accepted the entry; worker restart
- observed: unknown_before_restart=1 venue_entry_orders=1 intent_status=filled recoveries=2 stale_worker_fenced=True
- passed: True
- `worker_started` 2026-01-23T12:00:00Z {"owner": "worker-1", "token": 1}
- `risk_event` 2026-01-23T12:00:00Z {"correlation_id": "risk", "created_at": "2026-01-23T12:00:00Z", "detail": "restart: reconcile before new entries", "environment": "paper", "event_id": "evt_ris
- `risk_event` 2026-01-23T12:00:00Z {"correlation_id": "risk", "created_at": "2026-01-23T12:00:00Z", "detail": "conditions cleared", "environment": "paper", "event_id": "evt_risk_b59307738905f33b"
- `recovery_complete` 2026-01-23T12:00:00Z {"state": "NORMAL"}
- `order_unknown` 2026-01-23T12:45:03Z {"error": "VenueTimeout", "intent_id": "int_97697afc92e5a3cc"}
- `worker_started` 2026-01-23T12:45:00Z {"owner": "worker-2", "token": 2}
- `risk_event` 2026-01-23T12:45:00Z {"correlation_id": "risk", "created_at": "2026-01-23T12:45:00Z", "detail": "restart: reconcile before new entries", "environment": "paper", "event_id": "evt_ris
- `risk_event` 2026-01-23T12:45:00Z {"correlation_id": "risk", "created_at": "2026-01-23T12:45:00Z", "detail": "conditions cleared", "environment": "paper", "event_id": "evt_risk_ee274e6bd0bbc8df"
- `recovery_complete` 2026-01-23T12:45:00Z {"state": "NORMAL"}
- `protective_action` 2026-01-23T12:47:01Z {"intent_id": "prot_3276dab916fc34bd", "qty": "10", "reason": "protect_position", "side": "sell", "stop_price": "72206.5", "symbol": "BTCUSDT", "type": "stop_ma
- `order_ack` 2026-01-23T12:47:01Z {"intent_id": "prot_3276dab916fc34bd", "venue_order_id": "v00000002"}

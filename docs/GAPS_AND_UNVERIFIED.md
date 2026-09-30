# Measured, unverified and still missing

> **Update (validation program, research_v1):** `docs/VALIDATION_REPORT.md` holds the gate table. Safety
> invariants and leakage checks PASS across 63 runs. On the synthetic fixture the economic outcome is
> **NO VERIFIED EDGE**: the holdout failed, PBO is 0.91, and the sealed holdout produced zero trades. The
> investment verdict is UNTESTED (no real data, no real Phi). A 5-minute wall-clock protection soak ran
> with zero loop errors; the 24 h soak against the real Phi server is still UNTESTED.

State at the ADR-002 correction (2026-10-01). Hardware: a Linux container with 4 vCPUs and 15 GiB RAM,
**no GPU**, Python 3.11. All trading runs use **labeled synthetic data** and the **labeled
rule-based fake Phi backend** (`fake_phi_rules_v2`).

## Measured versus unverified claims

| Claim | Status | Evidence |
|---|---|---|
| All four roles use one Phi backend and model revision, with max concurrent inference 1 | **Measured (fake backend, stub HTTP server)** | `four_roles_one_backend`; `test_all_four_roles_use_one_endpoint_and_model_revision`; soak `max_concurrent_inference=1` |
| Risk work is admitted before waiting research | **Measured (threads + stub server)** | `test_inference_is_serialized_and_risk_work_is_admitted_first` |
| The decision is only an offered ID or ABSTAIN; no probability is recorded | **Measured** | `phi_decision_invent_candidate`, `test_phi_decision_chooses_only_offered_ids_or_abstain_over_a_replay` |
| Fail-closed on Phi timeout, OOM, malformed output, schema violation, overload or context budget | **Measured** | 24/24 fault scenarios (`reports/fault_injection.md`) |
| Protection does not wait for Phi | **Measured, synthetic:** emergency exit placed about 21 ms after a mark shock while a 2 s Phi call was in flight | `slow_phi_does_not_delay_protection` |
| Dispatch-time re-validation catches account and price changes | **Measured** | `admission_account_changed`, `admission_price_collar` |
| Stress limit enforced in the precheck | **Measured** | `test_stress_room_is_enforced_by_precheck_independently_of_sizing` |
| Risk loop latency (1-minute replay ticks, file-backed SQLite) | **Measured, CPU only:** watchdog p99 1.6 ms; full protect cycle p99 5.6 ms (target 100 ms) | `reports/soak_report.json` |
| Decision-cycle latency | **Measured with the fake backend only:** p99 44 ms. Real Phi latency is unknown | soak |
| Bounded graph memory | **Measured:** 395 nodes after 14 simulated days (was 6,417 before retention) | soak |
| Ledger growth | **Measured:** about 1.7 MB per simulated day, mostly Phi replay records and evidence records. Event retention is not implemented | soak |
| Token use | **Estimated** (bytes/3 heuristic, not the Phi tokenizer): about 0.35M input tokens per day for two symbols | evaluation, soak |
| Phi inference latency, VRAM, host RSS of the real server, quantization quality | **Unverified.** No GPU and no weights | `deploy/README.md` |
| Phi (any role) adds value over deterministic ranking | **Unverified.** The fake backend reproduces the ranker by construction: 7/7 agreement, identical PnL across P0–P4 | `reports/evaluation_report.md` |
| Trading edge | **Not supported.** The deterministic policy lost 82 USDT over 23 synthetic days, worse than cash (90% CI negative) | `reports/evaluation_report.md` |
| On-chain value | **Unverified.** Synthetic fixture only. Esplora is forward-capture only and is excluded from replays | `onchain.py` |

## Remaining blockers

**GPU / model**
- Serve Phi-4-mini-instruct on the target GPU with a pinned revision and vLLM tag
  (`deploy/README.md`).
- Run `check-phi`. Measure peak VRAM, process-tree RSS, inference p50/p95/p99, queue delay and
  schema-failure rate per role.
- Choose the quantization only after comparing it with a higher-precision reference on schema
  validity, evidence-request relevance, decision agreement and downstream paper PnL.
- Fill in `resources.*` with 20% headroom.

**Data**
- Add real point-in-time market, exchange and funding connectors that record arrival times.
- Replace the synthetic fixture for all research.
- Capture on-chain data forward (Esplora or an equivalent). Admit it to backtests only once
  historical availability is proven.
- Run leakage tests on real data.

**Evaluation**
- Walk-forward P0–P4 on real data with fixed candidate generation.
- Pre-register the promotion manifest: hurdle, minimum effective samples, confidence method,
  paired-improvement hurdle.
- Run a prospective paper evaluation.
- Calibrate any score only through a locked, separate study.

**QuantDinger integration**
- Port per the ADR-001 map, with `PhiDecisionSelector` beside the existing AI filter.
- Keep the legacy fail-open behavior only outside `autonomous_evidence_v1`, with regression tests.

**Real-time risk**
- Run `ProtectionLoop` against live venue streams (order, fill and account WebSockets).
- Verify venue-side stop semantics.
- Configure real venue margin tiers and liquidation rules.
- Do a 24 h wall-clock soak against the real Phi server.
- Implement event/ledger retention with disk quotas.

## Known simplifications

- Replay ticks every 1 simulated minute. The threaded `ProtectionLoop` supports a 1 s timer plus
  events, but it has only been exercised in tests, not against a live feed.
- Fills are marketable limit orders from the next full minute bar with liquidity caps. Stops gap
  through with stress slippage. There is no queue-position model.
- The stress limit is a single joint-move bound. Named stress scenarios are advisory
  (`request_stress_test`) and are not yet computed.
- The risk analyst's `reduce` action halves the position, and `tighten_stop` moves the stop halfway
  to the mark. Both are fixed deterministic policies. The model never supplies numbers.
- The `funding_crowding_v1` regime rule is declared, not trained.

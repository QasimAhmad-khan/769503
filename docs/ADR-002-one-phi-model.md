# ADR-002: One local Phi model for all four roles; Jev only as a design inspiration

Date: 2026-10-01. Status: accepted. Supersedes the model architecture of ADR-001 and commit `1135387`.

## Context

The operator's review of commit `1135387` found that the build had added an independent Open-Jev 9B
decision model, server and deployment. The intent was different: *Jev inspires bounded, auditable
choice*, and the system should use one model. The review also asked for five further changes:

1. dispatch-time re-validation of entries against fresh state;
2. an explicit stress limit in the precheck;
3. no probability semantics on generative output;
4. a typed, bounded analyzer–screener evidence dialogue;
5. protection that is independent of inference.

## Decision

- **Roles.** One Phi service (`cqc/llm/phi.py`) and one backend (endpoint, model, revision) serve
  `screener`, `analyzer`, `decision_maker` and `risk_analyst`. A thread-safe single-slot
  `InferenceQueue` serializes inference. Its priority order is risk, then decision, then research. It
  applies admission deadlines and sheds research under overload, and it exposes metrics (queue delay,
  inference p50/p95/p99, tokens, cache hit rate, maximum concurrency).
- **Decision.** `selection.PhiDecisionSelector` sends a compact versioned snapshot, the deterministic
  candidate facts and at most four prechecked IDs plus `ABSTAIN`. The JSON schema narrows
  `selected_id` to exactly those labels on every call, and the host re-checks membership, plan hashes,
  freshness, token budget and deadline. The result is a `decision_record` with
  `calibrated_selection_probability = null` and `score_semantics =
  none_uncalibrated_generative_choice`. Token log-probabilities, if enabled, are
  `uncalibrated_diagnostics` only. `DeterministicRankSelector` is the baseline.
- **Removed.** `cqc/llm/jev.py`, the Open-Jev deployment, `check-jev`, the Jev config fields,
  readiness items, tests and ablation names. Config loading now rejects a `jev` section.
- **Legacy records.** Old `decision_receipt` and `jev_raw_response` events remain parseable. They are
  labeled through `contracts.LEGACY_EVENT_KINDS`, reported by `Ledger.legacy_summary()` and
  `legacy_records_detected`, excluded from `RecordedPhiBackend`, and rejected by the entry gate. A
  ledger migration adds new intent and graph columns without rewriting old rows.
- **Evidence dialogue.** The analyzer emits a typed `evidence_request` (variable, symbol, interval,
  source class, maximum age, requiredness, relevance). The screener returns only a `fetch_plan` over
  whitelisted sources. The host fetches, stamps and hashes the evidence (`evidence_result`: source,
  observation and availability timestamps, lag, unit, quality flags, content hash). There is at most
  one follow-up round and a per-cycle Phi call budget. Variables whose historical availability is not
  proven are excluded in replay mode.
- **Risk.** `RiskEngine.admission_check` runs right before every entry is sent. It uses a fresh
  account view (excluding the intent's own reservation) and checks state, expiry, authorization and
  plan hashes, account version, equity tolerance, quote freshness, price collar, venue precision and
  minimums, and the full precheck including the new `STRESS_LIMIT`. Any failure means reject and
  record. Protective orders bypass it.
- **Protection.** `protection.ProtectionLoop` runs `runtime.protect` on events and a timer, holding
  only the account lock. Phi calls never hold that lock.
- **Risk analyst.** It can propose `tighten_stop`, `reduce`, `close`, `cancel_pending`,
  `move_to_no_new_risk` or `request_stress_test`, never quantities or prices.
  `runtime.apply_proposal` validates each proposal and applies only bounded, risk-reducing actions.
  Stops are replaced before the old one is canceled, and a stop is never loosened.

## Consequences

- The fake backend's decision_maker reproduces the deterministic ranker by construction. The value of
  Phi's decision role is therefore unmeasured until the real model is evaluated on real
  point-in-time data.
- The QuantDinger porting map in ADR-001 still applies. Replace its Jev rows with
  `selection.PhiDecisionSelector` next to the existing AI filter.

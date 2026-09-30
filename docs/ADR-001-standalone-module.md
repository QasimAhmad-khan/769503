# ADR-001: Standalone module with QuantDinger-shaped boundaries; Open-Jev as the decision maker

Date: 2026-09-30. Status: accepted for the paper build.

## Context

- The repository (`QasimAhmad-khan/769503`, base commit `5a0310c`) contained only the build
  package. It had no QuantDinger checkout. The package's audit reference is QuantDinger
  `9e3095df84c0c1631a2c7921444cc9b2150cfb24` (v5.4.1). The operator's budget was one session.
- The operator replaced hosted TypeSafe Jev with **Open-Jev** (<https://github.com/Zefan-Cai/Open-Jev>,
  9B package). Its `jev/serving.py` and `jev/api.py` were read at clone time: it serves
  `POST /v1/systemone` and accepts `model` in {None, `open-jev`, `jev-latest`, served name}. Choice
  answers carry `choice`, `probabilities` (normalized within 1e-6) and `confidence`, and the choice
  is the argmax. It binds to loopback and never substitutes answers on failure (HTTP 500).
- Container limits: no GPU. `huggingface.co`, exchange APIs and chain APIs were blocked.
  `github.com` was reachable for reads; pushing was refused (403).

## Decision

1. Build a cohesive Python package `cqc/` whose seams match QuantDinger's, rather than an
   unverifiable patch against a checkout we could not integrate and test end to end in budget.
2. Use SQLite as a stand-in for PostgreSQL. It keeps the same transactional boundaries: atomic
   receipt, authorization, reservation and intent writes; unique client order IDs; fill dedupe
   keys; a fencing lease.
3. Make Open-Jev the default provider. It uses the loopback HTTP transport with the `open-jev`
   alias, and the 9B package and base revisions are pinned in `config/paper.json`. The TypeSafe
   provider remains selectable through the same transport and contract.
4. Label the fake Phi/Jev adapters in every record (`fake_phi_rules_v1`, `fake_jev_not_a_model`).
   While they are configured, `promotion_blockers()` stays non-empty.

## Porting map (to `backend_api_python/`)

| This build | QuantDinger target (per `06_REPO_MAP_AND_SOURCES.md`) | Porting work |
|---|---|---|
| `policy.check_entry` | `app/services/ai_decision_filter.py`, `strategy_v2/live_execution.py` | Mode-specific gate. Legacy fail-open behavior stays unchanged outside the mode, with regression tests |
| `llm/jev.py` | provider layer next to `ai_decision_filter.py` | Candidate-selection adapter. Avoid a second disconnected Jev gate for the same decision |
| `llm/phi.py` | `app/services/llm.py` | Dedicated local Phi provider with remote fallback disabled for the three roles |
| `ledger.py` intents/reservations | `strategy_runtime/order_intents.py`, `pending_orders/order_budget.py` | Add plan hash, decision and authorization references, and atomic account-wide reservations |
| `execution.py` | `pending_order_worker.py`, `execution_streams/`, `live_trading/funding_reconciliation.py` | Fresh authorization at submission; reconcile `unknown` before retry |
| `risk.py` | `live_trading/account_risk.py` (audited as unwired) | Wire into production submission and integration-test it |
| `venue.py` | `strategy_v2/runtime.py` `MultiAssetSimulationBroker` | Add funding settlement, tiered maintenance margin and mark-price liquidation |
| `graph.py` | PostgreSQL node/edge tables | Same schema. Tenant and account scope columns are still needed |
| `ledger.acquire_lease` | `strategy_command_repository.py`, `commands/trading_worker.py` | Reuse the existing leases and fencing |

## Consequences

- The paper path is complete and testable today. What remains is QuantDinger integration and
  real-model verification, listed in `GAPS_AND_UNVERIFIED.md`.
- Nothing in this repository proves that QuantDinger's current behavior matches the audited
  reference. The audit-reference comparison must be redone against a real checkout.

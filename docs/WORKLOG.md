# Work log (2026-09-30, single session)

Base commit: `5a0310c` (the build package only). Audit reference: QuantDinger `9e3095d` (v5.4.1), not
present in this repository (see ADR-001).

## Backlog (in dependency order) and status

1. Contracts, config loader and validation. **Done**
2. Point-in-time evidence store and connectors (market context, synthetic chain with reorgs,
   Esplora forward capture). **Done**, live endpoints unverified
3. Numerical tools, empirical forecast, sizing, immutable plans. **Done**
4. Risk engine: locks, watchdog, precheck, authorization, reservations. **Done**
5. Simulated venue, executor, reconciliation, protection, fencing and restart. **Done**
6. Phi service (one backend, three roles), Open-Jev selector, recorded replay. **Done**, real models
   unverified
7. Paper runtime (protection loop and decision loop), graph memory, autonomous gate. **Done**
8. Fault injection, demo, ablations, soak and readiness CLI. **Done**
9. QuantDinger port, real-model benchmarks, 24 h wall-clock soak. **Open** (external gates)

## Defects found by the fault harness and tests, then fixed

- Plans that failed the precheck were validated as candidates. The schema requires
  `risk_precheck_passed=true`, so they are now dropped, with reasons logged.
- An audit-write failure inside a risk-state transition escaped as an exception. The lock now
  stays in memory, and the unlogged transition itself sets `audit_failure`.
- Phi event IDs collided when two symbols received identical payloads at the same bar. The
  correlation ID is now part of the event ID.
- A NaN in a Jev response crashed canonical JSON. It is now an `invalid` receipt and the cycle
  abstains.
- The synthetic chain fixture's values depended on query order, which made replays
  non-deterministic. Values are now seeded per block height.

## Commands actually run (results in `reports/`)

```
python -m pytest -q          # full suite
python -m cqc faults         # 19/19 scenarios passed (reports/fault_injection.md)
python -m cqc demo           # reports/demo_report.md
python -m cqc evaluate       # reports/evaluation_report.md
python -m cqc soak --days 14 # reports/soak_report.json
python -m cqc readiness      # exit 1: 16 blockers (expected in paper mode)
```

Hardware: 4 vCPU and 15 GiB RAM (Linux container), no GPU, Python 3.11.

---

# Work log: architecture correction (2026-10-01, ADR-002)

Base: `1135387` (PR #1 merged). This correction follows the operator review "one Phi model, four
roles".

## Changes

1. **Contracts.** Added `evidence_request`, `fetch_plan`, `decision_choice` and `decision_record`
   (no probabilities), and rebuilt `evidence_result` as a host-built record carrying timestamps, lag
   and hashes. Added the new risk-analyst action categories. Legacy Jev kinds are labeled.
2. **Phi service.** Four roles run on one backend through a thread-safe single-slot priority
   queue with admission deadlines and shedding. The decision schema is narrowed to the offered IDs
   per call. Added a result cache, metrics (queue delay, p50/p95/p99, tokens, cache hit rate,
   maximum concurrency) and a recorded-replay backend.
3. **Selection.** Added `PhiDecisionSelector` and the `DeterministicRankSelector` baseline.
   `cqc/llm/jev.py` was deleted.
4. **Risk.** Added `admission_check` on fresh state before every entry send, and `STRESS_LIMIT` in
   the precheck. Authorization is bound to the decision's plan hashes.
5. **Runtime.** Added the bounded evidence dialogue with a per-cycle Phi budget, `protect()` and the
   threaded `ProtectionLoop`, validated risk-analyst application, graph expiry and ring-buffer
   pruning, replay-mode exclusion of on-chain features without proven availability, and
   legacy-ledger detection.
6. **Config, deploy, CLI.** Removed the `jev` section (now rejected on load) and added a `decision`
   section. Deployment is a single Phi server. `check-jev` was removed and `check-phi` probes all
   four roles.
7. **Tests, fault scenarios, demo, evaluation.** Updated for the new architecture (P0–P4 ablations
   plus decision isolation).

## Defects found while doing this

- `ProtectionLoop._stop` shadowed a `threading.Thread` internal and crashed `join()`. Renamed.
- A required variable with no admissible source was requested, then rejected with an unclear
  reason. It now abstains immediately with `REQUIRED_EVIDENCE_MISSING`.
- A replayed run compared equal only if it impersonated the live backend. The comparison now uses
  `trading_digest()`, and replays stay labeled.

## Commands run

```
python -m pytest -q          # 64 passed
python -m cqc faults         # 24/24 passed
python -m cqc demo           # reports/demo_report.md
python -m cqc soak --days 14 # reports/soak_report.json
python -m cqc evaluate       # reports/evaluation_report.md
python -m cqc readiness      # exit 1, 16 blockers (expected)
```

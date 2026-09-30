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

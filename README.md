# claude-quant-crypto: paper build of the QuantDinger + Phi + Jev specification

This is a **paper-only** research and trading system for BTC and ETH linear USDT perpetuals. It
follows the build package in this repository (`00_START_HERE.md` through `07_PACKAGE_VERIFICATION.md`).

- **One Phi service, three logical roles:** screener, analyzer and risk analyst. They share one
  backend instance with a bounded queue, token budgets and strict role-result schemas.
- **Jev selects among candidates.** The decision maker is **Open-Jev**
  (<https://github.com/Zefan-Cai/Open-Jev>, 9B package). It speaks the TypeSafe-style
  `POST /v1/systemone` contract. It can only pick `ABSTAIN` or one of at most four immutable,
  prechecked candidate IDs.
- **Deterministic code owns every trusted number.** That covers features, forecasts, costs,
  sizing, hard risk, authorization, execution, reconciliation and accounting.
- **Fail-closed autonomous policy (`autonomous_evidence_v1`).** A model error, missing or stale
  required evidence, an expired decision, a failed audit write, an unsupported route or a provider
  fallback means **no new risk**. Protective exits never wait for a model.

> **Status: live trading is disabled and cannot be enabled by configuration.** The system runs on
> **labeled synthetic market data** with **labeled fake Phi/Jev adapters**, because the build
> container had no GPU and was blocked from Hugging Face and the market and chain APIs. The real
> Open-Jev and Phi HTTP adapters are implemented and tested against local stubs, not against the
> real models. Nothing here is evidence of a trading edge. See `docs/GAPS_AND_UNVERIFIED.md`.

## Quick start

```bash
python -m pip install -e '.[test]'     # numpy, jsonschema, pytest
python -m pytest -q                    # 53 tests: accounting, PIT integrity, faults, replay, adapters
python -m cqc demo                     # Phase-1 demo -> reports/demo_report.md
python -m cqc faults                   # fault injection -> reports/fault_injection.md
python -m cqc evaluate                 # paired ablations -> reports/evaluation_report.md
python -m cqc soak --days 14           # simulated-time soak (RSS, DB growth, risk latency)
python -m cqc readiness                # lists every unresolved live/promotion blocker (exit 1)
```

To use the real models (GPU host), see `deploy/README.md`, then run `python -m cqc check-jev` and
`python -m cqc check-phi`.

## How a decision flows

```
1-minute bar → venue match → reconcile fills/funding → risk watchdog → deterministic protection
15-minute bar → screen (allowlist, spread, history)
             → Phi analyzer: ResearchRequest (host validates hypothesis/instrument/features/sources)
             → Phi screener: retrieve_evidence_batch tool request (allowlisted) → connectors → PIT store
             → ≤1 follow-up round; required evidence missing/stale → ABSTAIN
             → quant tools: causal features, empirical path-outcome forecast (stop-first rule),
               costs, funding, stop-risk sizing → immutable CandidatePlans (hash-bound)
             → risk precheck (state, per-trade/aggregate stop risk incl. reservations, gross, symbol, margin)
             → Phi analyzer: AnalysisPacket may only pass a subset of eligible IDs
             → Jev: ABSTAIN or one candidate ID (validated labels, probabilities, argmax, model)
             → fresh RiskAuthorization (plan hash, account version, policy version, expiry)
             → autonomous_evidence_v1 gate → atomic persist (receipt + auth + reservation + intent)
             → dispatch: persist `submitting` before send; timeout → `unknown` → reconcile by client ID
```

## Module map

| Module | Responsibility |
|---|---|
| `cqc/config.py` | Load, validate and deep-freeze the paper profile. Unsafe settings are rejected; `promotion_blockers()` lists what is unresolved |
| `cqc/contracts.py` | Starter JSON Schema (`03_CONTRACTS`) plus the extension payloads (ToolRequest/Result, EvidenceResult, AnalysisPacket, AdjustmentProposal, RiskAuthorization, RiskEvent) and semantic validators |
| `cqc/pit.py` | Point-in-time evidence store: as-of queries, revisions, invalidation. Missing data is null with a reason, never zero |
| `cqc/onchain.py` | Connector registry. Market-context connector, synthetic finalized-chain fixture with reorgs, and a real Esplora forward-capture connector (stub-tested) |
| `cqc/market.py` | Instruments, labeled synthetic 1-minute fixture, point-in-time bar access |
| `cqc/quant.py` | Registered numerical tools, empirical forecast, sizing formula, immutable plans, dominance removal |
| `cqc/risk.py` | Risk state locks (NORMAL/NO_NEW_RISK/REDUCE_ONLY/RECOVERY/HALTED), watchdog, precheck, authorization, latency |
| `cqc/ledger.py` | Durable SQLite ledger standing in for PostgreSQL: events, intents, reservations, fills, fencing lease, graph tables |
| `cqc/venue.py` | Simulated linear-perp venue: partial fills, gap-through stops, funding, tiered liquidation, fault flags |
| `cqc/execution.py` | Executor: atomic entry persistence, dispatch, reconciliation, fill dedupe, protection, restart |
| `cqc/graph.py` | Bounded evidence graph with hop, node, edge and byte caps and missing-required reporting |
| `cqc/llm/phi.py`, `cqc/llm/jev.py` | Model adapters (fake, local HTTP, recorded replay) and output validation |
| `cqc/policy.py` | The `autonomous_evidence_v1` entry gate |
| `cqc/runtime.py` | Paper runtime: protection loop and bounded decision loop |
| `cqc/faults.py`, `cqc/demo.py`, `cqc/evaluate.py` | Fault scenarios, operator demo, paired ablations |

## Operator runbook (paper)

- **Start/restart.** A worker takes the fencing lease, enters `RECOVERY`, reconciles orders, fills,
  positions and protective stops, and only then returns to `NORMAL`. A stale worker's writes raise
  `StaleFencingToken`.
- **Kill switch.** `RiskEngine.operator_halt(now)` sets `HALTED`. Only
  `clear_lock("operator_halt", now, operator=True)` clears it. Model health never clears operator,
  drawdown or daily-loss locks.
- **Lock clearing.** `stale_feed`, `phi_health`, `jev_health`, `audit_failure` and `reconciliation`
  clear automatically after a stable healthy interval. `daily_loss` clears at the next 00:00 UTC
  reference. `drawdown` needs an operator.
- **What to watch.** `risk_event`, `protective_action`, `order_unknown`, `recovery_pending`,
  `liquidation` and `risk_analyst_unavailable` events, plus `summary()["risk_latency"]` against the
  100 ms p99 target.
- **Replay.** Recorded Jev responses (`jev_raw_response` events) replay deterministically through
  `RecordedJevTransport`. `Ledger.event_digest()` proves identical event and accounting output.
  Rerunning a model is a separate experiment.
- **Backups.** The ledger is a single SQLite file (`--db path`). Copy it while no worker holds the
  lease. In QuantDinger this maps to the existing PostgreSQL backups.
- **Rollback.** Every artifact that can change behavior is versioned in the records: policy
  (`autonomous_evidence_v1`), prompts (`roles_v1`, `selection_v1`), tool versions (`*_v1`), and
  model IDs and revisions in each receipt. To roll back, pin the previous config and code, then
  replay the recorded ledger.

## Relation to QuantDinger

The package asks for integration inside QuantDinger. This repository held only the specification,
so the system is built as a **standalone module with the same boundaries**, ready to port into
`backend_api_python/app/services/`. `docs/ADR-001-standalone-module.md` maps each module to its
QuantDinger integration point and lists the porting work that remains.

Licenses: QuantDinger is Apache-2.0. Open-Jev and Phi-4-mini-instruct are MIT. No third-party
code is vendored here.

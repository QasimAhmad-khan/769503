# Prospective paper-trading plan and prerequisites for real exchange integration

Live trading stays disabled. This plan only produces evidence. It never promotes parameters
automatically.

## 0. Preconditions (all must be true before day 1)

1. **Real Phi measured.** A pinned `microsoft/Phi-4-mini-instruct` revision, with the chosen
   quantization, is served by one vLLM process on the target GPU.
   - `python -m cqc bench-phi --endpoint reference=<bf16 url> --endpoint candidate=<4-bit url> --ledger <dev ledger>`
     reports four-role schema validity of at least 99%, measured VRAM/RSS/latency within declared
     budgets (20% headroom), and decision agreement between the quantized and reference models on the
     frozen candidate sets.
   - Record the results in `resources.*` and `phi.*`.
2. **Real point-in-time data capture.** Connectors record trades, top-of-book or L2 snapshots,
   mark/index and funding estimates, each with a **capture timestamp**. Admissible on-chain data
   comes from forward capture with finality and lag.
   - Ingestion uses `BarSeries.ingest`, which drops duplicates and rejects out-of-order bars and bad
     ticks.
   - At least 90 days of history must be captured before the historical program is re-run.
3. **New manifest (`research_v2`) frozen before evaluation.** It carries the real data hashes and the
   same pre-registered gates. The v1 synthetic manifest is not edited.
4. **Historical program re-run once on real data.** Run `python -m cqc validate --phase all` with the
   real fixture and the real Phi backend. If `NO VERIFIED EDGE` results, stop and collect more data or
   form a new hypothesis. Do not weaken the gates.

## 1. Prospective paper period

- **Duration.** Fixed in advance to reach the minimum effective sample: at least 30 non-overlapping
  trades **and** at least 60 calendar days, whichever comes later. It is never extended or cut short
  because of results.
- **Policies.** P0 (deterministic ranker) and P3 (all four Phi roles) run **in parallel** on the same
  live data, with separate paper accounts and identical capital and limits. The decision maker is
  also scored on identical offered candidate sets (decision isolation).
- **Frozen artifacts.** Code commit, config hash, Phi revision, prompt versions and manifest hash.
  Any change starts a new period.
- **Monitoring** (daily, operator-reviewed):
  - net PnL after fees and funding, drawdown, CVaR
  - admission rejections, locks, protection latency, reconciliation discrepancies
  - Phi schema failures, queue delay p99, timeouts and OOMs
  - token cost per decision
- **Kill criteria** (switch to `HALTED`, then investigate):
  - any hard-invariant violation
  - more than 1% daily loss twice in a week
  - drawdown beyond 3%
  - an unresolved reconciliation discrepancy
  - Phi schema-failure rate above 1%
  - p99 protection latency above 100 ms
- **Evaluation at the end, once.** Apply the same gates E1–E7 to the paper period (P3 − P0 paired
  CI, DSR against all logged trials, cost robustness).

## 2. Prerequisites for real exchange integration (still NOT enabling live orders)

- **Venue selection.** Select the venue and account, and verify:
  - product specs (contract multiplier, tick and quantity steps, minimum notional)
  - position mode and isolated margin
  - maintenance-margin tiers and liquidation rules, loaded into `venue_sim` and the readiness checks
- **Testnet.** Verify order-type semantics on testnet: marketable limit, reduce-only, stop-market
  (trigger source and gap behavior), idempotent client order IDs, and query-by-client-ID. Testnet
  fills do not prove live execution quality.
- **Streams.** Connect account, order and fill WebSocket streams to `ProtectionLoop.notify`.
  Measure event-to-protective-action latency.
- **Porting.** Port the code into QuantDinger per ADR-001, with regression tests for legacy
  fail-open behavior outside `autonomous_evidence_v1`.
- **Credentials.** Trading scope only, never withdrawal, held in the existing secret store and
  never in prompts.
- **Wall-clock soak.** A 24 h soak against the real Phi server and live market data in paper mode.
- **Promotion.** Only by an operator decision, backed by the locked evaluation artifacts. There is
  no automatic promotion, no self-modifying prompts or parameters, and no profitability guarantee.

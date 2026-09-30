# QuantDinger + Phi + Jev: Claude build package

Prepared 30 September 2026 for a GPU machine and crypto perpetual futures.

This package turns the rough idea into an implementable engineering specification. It does not contain a deployed trading bot or evidence of profitable trading. All numerical defaults are paper-research settings, not approved live account limits.

## What you are building

Three logical agents share one locally loaded Phi model:

1. **Screener:** retrieve only evidence the analyzer needs.
2. **Analyzer:** coordinate tested numerical tools and construct trade candidates.
3. **Risk analyst:** investigate changing risk and recommend bounded adjustments.

**TypeSafe Jev**, integrated into QuantDinger, chooses among eligible candidates. An independent deterministic risk engine can veto every candidate and manage protective exits without either model. Execution is a normal service, not a fourth Phi agent.

The intended product is [QuantDinger](https://github.com/OpenByteInc/QuantDinger). This is different from similarly named OpenJev model repositories and public gateways. The architecture uses one local Phi model **plus hosted Jev**; it is not a fully local, single-model system.

## Give Claude these files

- `01_BUILD_SPEC.md`: architecture, quant research, risk, data, memory, implementation phases, acceptance tests.
- `02_AGENT_PROMPTS.md`: constrained role prompts and Jev selection policy.
- `03_CONTRACTS.schema.json`: starter machine-readable contracts for evidence, research requests, candidates, decisions and graph memory.
- `04_EXAMPLES.json`: synthetic contract examples; never trading signals.
- `05_PAPER_CONFIG.json`: proposed paper profile; Claude must implement its config loader and validation.
- `06_REPO_MAP_AND_SOURCES.md`: verified repository integration points and source notes.

The contract file is a starter specification for the highest-risk boundaries. Claude must implement the additional payloads, persistence, state transitions and semantic checks described in the build spec. Schema validity alone does not establish trade safety.

## Paste this into Claude Code

```text
Act as a senior quantitative systems engineer implementing the attached QuantDinger + Phi + Jev specification. Build the working system in the QuantDinger repository; do not stop at a plan or a demo chatbot.

Read 01_BUILD_SPEC.md, 02_AGENT_PROMPTS.md, 03_CONTRACTS.schema.json, 04_EXAMPLES.json, 05_PAPER_CONFIG.json and 06_REPO_MAP_AND_SOURCES.md before editing. Treat the attached package as the product requirements and distinguish verified current behavior from proposed changes. Inspect the actual checkout, local contributor instructions and tests. Record the base commit; compare it to the audited reference 9e3095df84c0c1631a2c7921444cc9b2150cfb24, version 5.4.1. Recheck relevant upstream API/model contracts rather than assuming they are unchanged.

Implement three logical roles—screener, numerical-analysis coordinator and risk analyst—using exactly one resident Phi inference service. Use TypeSafe Jev as the bounded candidate decision maker. Keep all arithmetic, forecasts, sizing, hard risk limits, order authorization and execution in tested code. Reuse QuantDinger's database, worker ownership, exchange adapters, audit logging and existing provider interfaces.

The priorities are: execution/accounting correctness; point-in-time data integrity; bounded memory and graceful failure; demonstrated incremental predictive value; then speed and presentation. Do not claim alpha, optimality or state-of-the-art results without the specified evaluations. A correct result may be no deployable edge.

Default to linear USDT perpetuals, BTC/ETH, one venue adapter, one-way isolated-margin mode where supported, 15-minute decision bars and faster independent risk monitoring. Use a simulated venue until a real venue is selected. Do not enable live trading, enter credentials into prompts, or use real money. Unknown GPU, venue and paid-data details must not block the replay/paper implementation; discover hardware when available, mark real integration checks unverified, and use documented fixtures when credentials are absent.

Create a work log and a small dependency-ordered backlog. Then execute the phases in the specification. Start with an end-to-end deterministic paper path using fake Phi/Jev adapters, durable events, risk authorization, execution reconciliation and replay. Next connect the single Phi service and hosted Jev behind validated adapters. Add on-chain evidence with documented availability/provenance and an incremental-value test; where valid historical arrival data is unavailable, collect forward instead of inventing a backtest.

In autonomous mode, model errors, missing required data, expired decisions or failed validation must prevent new risk. Protective exits and reconciliation must remain available without model inference. Patch QuantDinger's legacy fail-open entry behavior through an explicit mode-specific policy and test all entry paths and fallbacks.

For each phase deliver working code, focused tests, commands actually run, observed results and unresolved limitations. Run relevant existing checks. Add tests for real failure modes, not implementation-shaped trivialities. Do not fabricate benchmark output, simulate passing live integration tests, silently substitute data or confuse Jev class scores with win probabilities. Keep artifacts, model revisions, prompts and data snapshots reproducible. Finish with a runnable paper demonstration, replay report, fault-injection results, actual hardware/resource measurements when available, evaluation report and operator runbook. Leave live trading disabled.
```

## Inputs Claude should eventually collect

Exact GPU/VRAM and host RAM; operating system; selected exchange/account and permitted products; account sizing and live loss limits; target holding horizon; data-provider budget; Jev availability; and whether sending compact decision context to hosted Jev is acceptable. These affect deployment configuration, not the ability to build the paper system.

No QuantDinger trading code or model was installed or executed as part of preparing this package. Repository source and primary documentation were inspected read-only; JSON examples were checked as described in the accompanying verification report.

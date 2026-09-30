# Repository map and primary sources

Prepared 30 September 2026 for a GPU-hosted crypto perpetual-futures system: three logical Phi roles, hosted TypeSafe Jev candidate selection, and independent deterministic risk/execution.

**Audited reference:** QuantDinger commit `9e3095df84c0c1631a2c7921444cc9b2150cfb24`, version `5.4.1`. Source was inspected read-only; application tests, model inference and exchange integrations were not run. Claude must compare the actual checkout with this reference before editing. Proposed changes below are requirements, not existing capabilities.

## Verified behavior and required changes

**Jev gate.** [`app/services/ai_decision_filter.py`](https://github.com/OpenByteInc/QuantDinger/blob/9e3095df84c0c1631a2c7921444cc9b2150cfb24/backend_api_python/app/services/ai_decision_filter.py) asks five atomic Choice questions: data quality, signal alignment, market regime, account risk and execution quality. Deterministic convergence approves unless risk/execution blocks or confident signal conflict coincides with an adverse regime. This is an entry filter, not candidate selection or allocation.

Legacy entries can proceed after provider failure, missing configuration or unavailable billing. Malformed/low-confidence Jev answers can fall back to an LLM. Exits and certain strategy families bypass the filter. Required change: `autonomous_evidence_v1` must abstain on invalid/unavailable/expired decisions and unsupported entry paths, while independent protective actions remain available. Add bounded candidate-ID selection, avoid duplicate disconnected Jev calls, and require durable authorization. Preserve deliberate legacy behavior outside this mode with regression tests.

**AI backtests.** [`app/services/ai_decision.py`](https://github.com/OpenByteInc/QuantDinger/blob/9e3095df84c0c1631a2c7921444cc9b2150cfb24/backend_api_python/app/services/ai_decision.py) is a separate strategy AI interface. `BacktestAIDecisionClient` explicitly bypasses AI, and `allows` defaults to allowing skipped decisions. The order gateway's AI gate runs only in live mode. Required change: paper/shadow execution must exercise production decision policies; replay recorded structured decisions and point-in-time evidence. Existing backtests do not establish Phi/Jev value.

**Perpetual simulation.** [`strategy_v2/service.py`](https://github.com/OpenByteInc/QuantDinger/blob/9e3095df84c0c1631a2c7921444cc9b2150cfb24/backend_api_python/app/services/strategy_v2/service.py#L388) explicitly declares funding unmodeled. [`strategy_v2/runtime.py`](https://github.com/OpenByteInc/QuantDinger/blob/9e3095df84c0c1631a2c7921444cc9b2150cfb24/backend_api_python/app/services/strategy_v2/runtime.py#L1079) liquidates at nonpositive equity and absorbs residual deficits. Required change: historical funding settlements, venue maintenance-margin tiers, mark-price liquidation and stressed fills/costs. Existing liquidation is not a venue-accurate perpetual model.

**Account risk.** [`live_trading/account_risk.py`](https://github.com/OpenByteInc/QuantDinger/blob/9e3095df84c0c1631a2c7921444cc9b2150cfb24/backend_api_python/app/services/live_trading/account_risk.py) provides exposure and estimated margin/fee/funding checks. Backend search found its public helpers called only by tests, not production submission code. Required change: explicitly wire and integration-test aggregate risk, pending reservations and final submission checks. A helper's existence does not establish enforcement.

## Integration map

Paths below are relative to `backend_api_python/` in the [pinned source tree](https://github.com/OpenByteInc/QuantDinger/tree/9e3095df84c0c1631a2c7921444cc9b2150cfb24/backend_api_python). Inspect each before reuse.

| Existing boundary | Verified path | Intended extension |
|---|---|---|
| Bounded evidence | `app/services/ai_decision_context.py` | Versioned evidence/graph projections, availability and freshness |
| Model providers | `app/services/llm.py` | Dedicated local Phi adapter; disable remote fallback for the three roles |
| Order gateway | `app/services/strategy_v2/live_execution.py` | Bind candidate, decision, account version and authorization; preserve idempotency/latches |
| Durable intents | `app/services/strategy_runtime/order_intents.py` | Reuse IDs; add immutable decision/plan references |
| Entry budget | `app/services/pending_orders/order_budget.py`; caller `app/services/trading_executor.py` | Preserve per-strategy guard; add atomic account-wide reservations |
| Dispatch/recovery | `app/services/pending_order_worker.py`; `app/services/pending_orders/` | Fresh authorization at submission; reconcile ambiguous outcomes before retry |
| Venue interface | `app/services/live_trading/factory.py`, `contracts.py`, `capabilities.py` | One selected linear-perpetual adapter with verified units/modes |
| Protection | `app/services/live_trading/native_protection.py`; `app/services/strategy_v2/protection.py` | Independent deterministic exits and verified venue-side stops |
| Fills/account truth | `app/services/execution_streams/`; `app/services/live_trading/account_snapshot.py`, `funding_reconciliation.py` | Reuse reconciliation/accounting; derive graph state from durable facts |
| Ownership | `app/services/strategy_command_repository.py`; `app/commands/trading_worker.py` | Preserve leases/fencing; serialize shared account risk |
| Simulation | `app/services/strategy_v2/runtime.py`, `service.py` | Extend `MultiAssetSimulationBroker` and `StrategyV2BacktestRunner` |
| Quant research | `app/services/factors/`; `app/services/strategy_evolution/` | Registered numerical tools, purged evaluation, bounded caches |

The README's `services/backtest_engine/` path does not exist at this commit. `backtest_execution.py` normalizes fees/slippage; it is not the simulation engine.

Reuse PostgreSQL and existing worker boundaries. Trading workers own long-lived strategies and broker sessions; Celery handles finite jobs. Cache Redis and durable job Redis have distinct roles. Keep one resident Phi service. See [process roles](https://github.com/OpenByteInc/QuantDinger/blob/9e3095df84c0c1631a2c7921444cc9b2150cfb24/docs/architecture/PROCESS_ROLES_AND_TASKS.md) and [concurrency rules](https://github.com/OpenByteInc/QuantDinger/blob/9e3095df84c0c1631a2c7921444cc9b2150cfb24/docs/architecture/CONCURRENCY_MODEL.md).

Regression starting points under `tests/`: `test_ai_decision_filter.py`, `test_strategy_v2_order_gateway.py`, `test_strategy_v2_bar_phase_ordering.py`, `test_strategy_v2_multitimeframe.py`, `test_strategy_v2_protection.py`, `test_exchange_order_param_contracts.py`, `test_funding_reconciliation.py`, `test_execution_stream_late_fill_recovery.py`, and `integration/test_execution_projection_atomic.py`. Add autonomous-mode failures and the acceptance scenarios in the build specification.

## External contracts and evidence

- **TypeSafe API:** [`POST /v1/systemone`](https://docs.typesafe.ai/api) accepts model, state and typed questions. Choice returns selected option, probabilities and confidence. [Model documentation](https://docs.typesafe.ai/models) lists `jev-1.13.0` at research time; pin/recheck availability. Jev is a separate hosted service, not the local Phi model.
- **Jev limitations:** [Publisher documentation](https://docs.typesafe.ai/model-jaggedness/jev-1.13) identifies arithmetic/date-comparison weaknesses, irrelevant-context sensitivity and adversarial-content risks. Compute quantities, freshness, expected returns and risk in code. [Confidence documentation](https://docs.typesafe.ai/confidence) does not establish trade win probabilities; evaluate thresholds against the actual task.
- **Phi baseline:** [Microsoft Phi-4-mini-instruct](https://huggingface.co/microsoft/Phi-4-mini-instruct) is a 3.8B MIT-licensed model with tool-calling support. Its advertised context maximum is not a memory budget or financial benchmark. Pin weights/tokenizer/runtime; measure quantization quality, full RAM/VRAM and bounded-context behavior. [vLLM structured outputs](https://docs.vllm.ai/en/latest/features/structured_outputs/) is one serving option, subject to compatibility testing.
- **Execution acknowledgment:** [Bybit order creation](https://bybit-exchange.github.io/docs/v5/order/create-order) documents asynchronous acceptance and status confirmation via WebSocket. This supports distinguishing acknowledgment from fill; it does not select Bybit as the deployment venue.
- **Selection bias:** Bailey et al., [The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf), studies overfitting induced by strategy selection and proposes PBO/CSCV. Trial tracking and the build package's validation design are engineering requirements; the paper does not validate this system's alpha.

QuantDinger code uses [Apache-2.0](https://github.com/OpenByteInc/QuantDinger/blob/9e3095df84c0c1631a2c7921444cc9b2150cfb24/LICENSE); preserve applicable notices. [Branding rules](https://github.com/OpenByteInc/QuantDinger/blob/9e3095df84c0c1631a2c7921444cc9b2150cfb24/TRADEMARKS.md) are separate. Frontend source resides in separate repositories; this checkout uses published UI images. Recheck current licenses and external API contracts before implementation.

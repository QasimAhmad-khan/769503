# Gaps, unverified checks and external gates

Everything below is still open. None of it is claimed as done.

## Unverified (needs hardware, network or credentials)

| Item | Why it is unverified | How to close it |
|---|---|---|
| Open-Jev 9B inference, latency and memory | No GPU; `huggingface.co` blocked | `deploy/README.md` §1, then `python -m cqc check-jev`; record VRAM and latency |
| Open-Jev revision pins | Taken from Open-Jev's `docker/README.md`, not re-fetched | Check the published package manifest during the docker build |
| Phi-4-mini-instruct serving (vLLM structured outputs) | No GPU or weights | `deploy/README.md` §2, `check-phi`; pin the revision; compare 4-bit with the reference on schema validity and downstream decisions |
| Esplora on-chain connector against the live API | Egress blocked | Run forward capture. No on-chain backtest is claimed |
| Real market data (Binance/Bybit/OKX) | Egress blocked | Add a public-data connector behind `Connector`. The venue remains `SIMULATED` |
| Measured host and GPU budgets, 24 h wall-clock soak | No GPU; one session | Run `soak` against the real services; fill in `resources.*` with 20% headroom |
| QuantDinger integration and regression tests | No QuantDinger checkout in this repo | Follow the ADR-001 porting map and run the regression starting points in `06_REPO_MAP` |

## Known simplifications (documented, tested where noted)

- **Watchdog cadence.** The replay evaluates risk on every 1-minute bar, not once per second. A
  live runtime must tick at 1 Hz. Measured risk-decision latency is reported separately
  (p99 about 0.1 ms on the build container CPU).
- **Fills.** Entries are marketable limit orders that execute from the first full minute bar after
  placement. Liquidity is capped per bar (partial fills). Stops fill at the worse of stop and open,
  with stress slippage (gap-through). There is no queue-position model, and latency beyond the
  1-minute step is not modeled.
- **Stop/target ambiguity in the forecast.** The declared conservative rule is that the stop hits
  first whenever the path touches it.
- **Liquidation.** Isolated linear, tiered MMR, liquidation at the mark crossing. Tiers are
  illustrative until a real venue's rules are configured (still a readiness blocker).
- **Funding.** Settled every 8 hours on the venue schedule, using the estimate known at the
  previous bar. Real funding history is not available offline.
- **Portfolio stress.** Sizing and the precheck use a single 10% joint-move stress bound. The full
  joint BTC/ETH, basis, spread and funding-shock scenario set is only named in the
  AdjustmentProposal schema, not computed.
- **Graph retention.** Nodes and edges accumulate (reported by `soak`). The pruning job, disk quota
  and retention windows are not implemented.
- **Fake analyzer.** The rule-based fake passes every eligible candidate unless an evidence
  contradiction is flagged, so ablation D is identical to C by construction. Real Phi value can
  only be measured with the real model.
- **Hypothesis 2 (`funding_crowding_v1`).** Its regime rule is declared, not trained.
- **Evidence content hash.** `04_EXAMPLES.json` evidence hashes follow a rule the package does not
  document. This build defines its own rule (`contracts.evidence_content`) and enforces it at
  ingestion.

## Evaluation honesty

- The ablation report runs on a synthetic fixture with fake models. It demonstrates the harness:
  separate portfolio replays, paired daily differences, a moving-block bootstrap and folds. It is
  not evidence about Phi, Jev or on-chain value, and it is not evidence of an edge.
- During debugging, days 21–24 of the synthetic fixture were replayed. No parameter was tuned; the
  config was fixed in advance.
- Promotion and readiness stay blocked (`python -m cqc readiness`) until the manifest, calibration,
  measured budgets, a real venue and prospective evaluation exist.

## Requested plugins

The requested plugins ("spotify bulk reader", "spotify shunt", "pony tail") were not available in
this session. Neither the account's enabled-plugin list nor the plugin catalog contained them, so
they were not used.

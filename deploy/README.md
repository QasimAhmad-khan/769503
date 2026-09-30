# The local Phi server (GPU host)

**Nothing in this folder was run while this package was built.** The build container had no GPU and
could not reach `huggingface.co`. Treat every step below as unverified until you run it yourself.

## One model, four roles

All four roles (screener, analyzer, decision_maker, risk_analyst) call the same server, model and
revision. The trading runtime additionally serializes calls through `cqc.llm.phi.InferenceQueue`: one
active call at a time, risk work first, admission deadlines, and overload shedding. The server's
`--max-num-seqs 1` enforces the same limit on the GPU side. There is no second decision model and no
remote fallback.

```bash
PHI_REVISION=<pinned commit> VLLM_TAG=<tested tag> docker compose -f deploy/docker-compose.yml up -d phi
cp config/paper.json config/paper.phi-real.json   # then set phi.backend = "openai_compatible"
python -m cqc check-phi --config config/paper.phi-real.json
```

`check-phi` sends one schema-constrained request per role through the same backend. It reports schema
validity, whether the decision stayed inside the offered set, latency, queue delay and token use.

## Before trusting it

1. **Pin the revisions.** Pin the model revision (`phi.model_revision`) and the vLLM image, and record
   the artifact hashes and licence.
2. **Choose a quantization by measurement.** Compare a supported 4-bit build against a higher-precision
   reference on:
   - schema validity for each role
   - evidence-request relevance
   - decision agreement with the deterministic ranker on frozen candidate sets
   - downstream paper PnL on identical candidate sets

   Do not claim a speed or memory gain without that benchmark.
3. **Measure resources.** Record peak VRAM (`nvidia-smi --query-gpu=memory.used --format=csv -l 1`),
   host RSS of the whole process tree, and inference p50/p95/p99 and queue delay (`cqc` resource
   summary). Put the results in `resources.*` with at least 20% headroom.
4. **Keep log-probabilities diagnostic.** Setting `phi.record_token_logprobs=true` stores them only as
   `uncalibrated_diagnostics`. They must not gate trades until a locked calibration study exists.

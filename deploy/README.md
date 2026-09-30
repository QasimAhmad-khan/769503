# Model services (GPU host)

Nothing in this folder was run while this package was built: the build container had no GPU, and
`huggingface.co` plus the market and chain APIs were blocked by its network policy. Treat every
step below as **unverified** until you run it on your own machine.

## 1. Open-Jev 9B (decision maker)

This build uses Open-Jev instead of hosted TypeSafe Jev. It is an independent MIT-licensed
implementation (<https://github.com/Zefan-Cai/Open-Jev>). It serves the same `POST /v1/systemone`
contract on loopback and returns `choice`, `probabilities` and `confidence` for a `choice` question.

```bash
docker compose -f deploy/docker-compose.yml up -d --build open-jev
# or, from an Open-Jev checkout with a downloaded 9B package:
python -m jev.server --checkpoint /path/to/Open-Jev-9B/package/checkpoint --max-length 4096
```

The Open-Jev README says the 9B model needs roughly one 16 GB GPU in bf16. Its revisions are pinned
in `config/paper.json` (`jev.model_revision`, `jev.base_model_revision`). The requests use the
server alias `open-jev`, so `python -m cqc check-jev` also checks `/v1/models`.

Then switch the runtime to the real transport. Copy `config/paper.json`, set `jev.transport` to
`"http"`, pass it with `--config`, and run:

```bash
python -m cqc check-jev            # /v1/models + one bounded synthetic choice request
```

Open-Jev's choice scores are calibrated **classification** preferences. They are not trade win
probabilities. Any acceptance threshold needs its own locked calibration artifact
(`jev.calibration_artifact_id`) before it can matter.

## 2. Phi-4-mini-instruct (screener / analyzer / risk-analyst roles)

```bash
PHI_REVISION=<pinned commit> docker compose -f deploy/docker-compose.yml up -d phi
python -m cqc check-phi --config config/paper.phi-real.json   # set phi.backend=openai_compatible
```

The three roles are prompt/schema profiles of **one** process (`--max-num-seqs 1`). Measure GPU
memory and the host process tree, then record them in `resources.*`, keeping 20% headroom. Also
compare a 4-bit artifact with the reference on schema validity and on downstream decisions. That
comparison stays open until you run it.

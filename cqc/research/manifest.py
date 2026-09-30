"""Frozen research manifest, append-only hash-chained trial log, and a one-shot sealed holdout.

- `freeze()` writes the manifest with its SHA-256; later code verifies the hash before any run.
- `TrialLog` is JSONL where every entry carries the previous entry's hash; `verify()` detects edits,
  deletions or reordering. Every attempted trial is logged, including failures.
- `HoldoutSeal` refuses to evaluate the final holdout unless the manifest hash matches, a candidate
  has been frozen, and no holdout run was recorded before. The seal is recorded before execution, so
  a crashed run still consumes the single evaluation.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..util import UTC, canonical_json, sha256_hex


class ProtocolViolation(RuntimeError):
    pass


def manifest_hash(manifest: dict) -> str:
    return sha256_hex(canonical_json({k: v for k, v in manifest.items() if k != "manifest_sha256"}))


def freeze(manifest: dict, path: Path) -> dict:
    path = Path(path)
    if path.exists():
        existing = json.loads(path.read_text())
        if manifest_hash(existing) != existing.get("manifest_sha256"):
            raise ProtocolViolation("existing manifest was edited after freezing")
        if manifest_hash(existing) != manifest_hash(manifest):
            raise ProtocolViolation("a different manifest is already frozen; changes require a new versioned manifest")
        return existing
    frozen = dict(manifest, frozen_at=datetime.now(UTC).isoformat())
    frozen["manifest_sha256"] = manifest_hash(frozen)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(frozen, indent=2, sort_keys=True))
    return frozen


def load_verified(path: Path) -> dict:
    m = json.loads(Path(path).read_text())
    if manifest_hash(m) != m.get("manifest_sha256"):
        raise ProtocolViolation("manifest hash mismatch: manifest was modified after freezing")
    return m


class TrialLog:
    GENESIS = "0" * 64

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def verify(self) -> bool:
        prev = self.GENESIS
        for e in self.entries():
            body = {k: v for k, v in e.items() if k != "entry_sha256"}
            if e["prev_sha256"] != prev or sha256_hex(canonical_json(body)) != e["entry_sha256"]:
                return False
            prev = e["entry_sha256"]
        return True

    def append(self, record: dict) -> dict:
        if not self.verify():
            raise ProtocolViolation("trial log hash chain broken; refusing to append")
        entries = self.entries()
        body = {"seq": len(entries), "logged_at": datetime.now(UTC).isoformat(),
                "prev_sha256": entries[-1]["entry_sha256"] if entries else self.GENESIS, **record}
        body["entry_sha256"] = sha256_hex(canonical_json(body))
        with self.path.open("a") as fh:
            fh.write(canonical_json(body) + "\n")
        return body

    def count(self, kind: str) -> int:
        return sum(1 for e in self.entries() if e.get("kind") == kind)


class HoldoutSeal:
    def __init__(self, log: TrialLog, manifest: dict):
        self.log, self.manifest = log, manifest

    def open_once(self, candidate: dict) -> dict:
        if not self.log.verify():
            raise ProtocolViolation("trial log tampered")
        if manifest_hash(self.manifest) != self.manifest["manifest_sha256"]:
            raise ProtocolViolation("manifest changed after freezing")
        frozen = [e for e in self.log.entries() if e.get("kind") == "candidate_frozen"]
        if not frozen:
            raise ProtocolViolation("no frozen candidate; select and freeze on train/dev first")
        if frozen[-1]["candidate_sha256"] != sha256_hex(canonical_json(candidate)):
            raise ProtocolViolation("candidate differs from the frozen candidate")
        if self.log.count("holdout_opened") >= self.manifest["holdout"]["max_evaluations"]:
            raise ProtocolViolation("final holdout already evaluated; it cannot be reused for tuning or re-testing")
        return self.log.append({"kind": "holdout_opened", "manifest_sha256": self.manifest["manifest_sha256"],
                                "candidate_sha256": frozen[-1]["candidate_sha256"]})

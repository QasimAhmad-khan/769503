"""Reproducibility record: exact commit, dirty state, config/data/manifest hashes, dependency versions."""
from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from ..util import canonical_json, sha256_hex

ROOT = Path(__file__).resolve().parents[2]


def _git(*args) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def file_sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def series_sha256(series: dict) -> str:
    """Content hash of a market fixture (every bar, every field, both symbols)."""
    h = hashlib.sha256()
    for sym in sorted(series):
        for b in series[sym].bars:
            h.update(f"{sym}|{b.start.isoformat()}|{b.open!r}|{b.high!r}|{b.low!r}|{b.close!r}|{b.volume_base!r}|"
                     f"{b.bid!r}|{b.ask!r}|{b.mark!r}|{b.funding_rate_est!r}\n".encode())
    return h.hexdigest()


def capture(extra: dict | None = None) -> dict:
    deps = {}
    for name in ("numpy", "jsonschema", "pytest"):
        try:
            deps[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            deps[name] = None
    rec = {"commit": _git("rev-parse", "HEAD"), "dirty": bool(_git("status", "--porcelain")),
           "branch": _git("rev-parse", "--abbrev-ref", "HEAD"), "python": sys.version.split()[0],
           "platform": platform.platform(), "deps": deps,
           "config_sha256": file_sha256(ROOT / "config" / "paper.json"),
           "schema_sha256": file_sha256(ROOT / "cqc" / "schemas" / "contracts.schema.json")}
    if extra:
        rec.update(extra)
    rec["record_sha256"] = sha256_hex(canonical_json(rec))
    return rec

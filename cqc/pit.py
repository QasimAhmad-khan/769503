"""Point-in-time evidence store.

Every record carries event_time / available_at / observed_at / revision. An as-of query at a
cutoff returns the latest revision *known by that cutoff*; future or later-revised facts
never leak into a past decision, and missing data is an explicit null with a reason (never 0).
Invalidations (reorgs, corrections) are appended with their own time, so audit history of
what was known earlier is preserved.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from . import contracts
from .contracts import evidence_content, evidence_hash_ok
from .util import content_hash, iso, parse_ts, stable_id


class EvidenceStore:
    def __init__(self):
        self._records: dict[tuple, list[dict]] = defaultdict(list)  # (feature, symbol) -> records
        self._invalidations: dict[str, list[tuple[datetime, str]]] = defaultdict(list)  # source_record_id -> [(at, reason)]
        self.by_id: dict[str, dict] = {}

    def add(self, record: dict) -> dict:
        contracts.validate(record)
        if not evidence_hash_ok(record):
            raise contracts.ContractError("evidence content_sha256 mismatch")
        if record["evidence_id"] in self.by_id:
            return self.by_id[record["evidence_id"]]  # exact duplicate evidence is deduplicated
        self._records[(record["feature"], record["instrument"]["symbol"])].append(record)
        self.by_id[record["evidence_id"]] = record
        return record

    def invalidate(self, source_record_id: str, at: datetime, reason: str):
        self._invalidations[source_record_id].append((at, reason))

    def invalidated_as_of(self, source_record_id: str, cutoff: datetime):
        for at, reason in self._invalidations.get(source_record_id, []):
            if at <= cutoff:
                return reason
        return None

    def as_of(self, feature: str, instrument: dict, cutoff: datetime, max_age_seconds: int,
              correlation_id: str = "adhoc", producer: str = "pit_store") -> dict:
        symbol = instrument["symbol"]
        known = [r for r in self._records.get((feature, symbol), [])
                 if parse_ts(r["available_at"]) <= cutoff and parse_ts(r["event_time"]) <= cutoff]
        valid = [r for r in known if r["status"] in ("available", "provisional")
                 and not self.invalidated_as_of(r["source_record_id"], cutoff)]
        if not valid:
            reason = "no_record_available_by_cutoff"
            if known:
                reason = "all_known_records_invalidated" if any(
                    self.invalidated_as_of(r["source_record_id"], cutoff) for r in known) else "only_missing_records"
            return missing_evidence(feature, instrument, cutoff, reason, correlation_id, producer)
        latest = max(valid, key=lambda r: (r["event_time"], r["revision"], r["available_at"]))
        age = (cutoff - parse_ts(latest["event_time"])).total_seconds()
        if age > max_age_seconds:
            return make_evidence(feature, instrument, None, latest["unit"], "stale", f"age_{int(age)}s_exceeds_{max_age_seconds}s",
                                 parse_ts(latest["event_time"]), parse_ts(latest["available_at"]), cutoff,
                                 latest["source_id"], latest["source_record_id"], latest["revision"], cutoff,
                                 correlation_id, producer, chain=latest["chain"], synthetic=latest["synthetic"])
        return latest


def make_evidence(feature, instrument, value, unit, status, reason, event_time: datetime, available_at: datetime,
                  observed_at: datetime, source_id, source_record_id, revision, cutoff: datetime,
                  correlation_id="adhoc", producer="connector", chain=None, synthetic=True, ttl_seconds=900,
                  quality_flags=None) -> dict:
    body = {"feature": feature, "instrument": instrument, "value": value, "unit": unit, "status": status,
            "reason": reason, "event_time": iso(event_time), "source_id": source_id,
            "source_record_id": source_record_id, "revision": revision, "chain": chain}
    evidence_id = stable_id("ev", content_hash(body), iso(available_at))
    record = contracts.envelope("evidence", stable_id("evt", evidence_id, iso(cutoff)), correlation_id, producer,
                                iso(max(observed_at, cutoff)), iso(cutoff),
                                iso(max(observed_at, cutoff) + timedelta(seconds=ttl_seconds)), synthetic)
    record.update(body)
    record.update({"evidence_id": evidence_id, "available_at": iso(available_at), "observed_at": iso(observed_at),
                   "content_sha256": content_hash(evidence_content({**body})), "quality_flags": quality_flags or []})
    return record


def missing_evidence(feature, instrument, cutoff: datetime, reason: str, correlation_id="adhoc", producer="pit_store"):
    return make_evidence(feature, instrument, None, "none", "missing", reason, cutoff, cutoff, cutoff,
                         "pit_store", f"missing:{feature}:{instrument['symbol']}:{iso(cutoff)}", 0, cutoff,
                         correlation_id, producer, synthetic=True)


def usable(record: dict) -> bool:
    return record["status"] == "available" and record["value"] is not None

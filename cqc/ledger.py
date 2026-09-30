"""Durable append-only event ledger, order intents, reservations, fills and ownership fencing.

SQLite stands in for QuantDinger's PostgreSQL in this standalone paper build; the table
shapes and transaction boundaries are what must be ported. Writes that change intents or
reservations require the current fencing token, so a stale worker cannot act after a
restart has taken ownership.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from . import contracts
from .util import D, canonical_json, iso, sha256_hex, stable_id

TERMINAL = {"filled", "canceled", "rejected", "expired"}
OPEN_STATUSES = {"queued", "submitting", "acknowledged", "partially_filled", "cancel_requested", "unknown"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
  kind TEXT NOT NULL, correlation_id TEXT, ts TEXT NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intents (intent_id TEXT PRIMARY KEY, client_order_id TEXT UNIQUE NOT NULL,
  authorization_id TEXT, candidate_id TEXT, plan_sha256 TEXT, purpose TEXT NOT NULL, symbol TEXT NOT NULL,
  side TEXT NOT NULL, qty TEXT NOT NULL, price_limit TEXT, stop_price TEXT, order_type TEXT NOT NULL,
  reduce_only INTEGER NOT NULL, status TEXT NOT NULL, venue_order_id TEXT, filled_qty TEXT NOT NULL DEFAULT '0',
  expires_at TEXT, created_ts TEXT NOT NULL, updated_ts TEXT NOT NULL, fencing_token INTEGER NOT NULL,
  submit_attempts INTEGER NOT NULL DEFAULT 0, auth_account_version TEXT, auth_equity TEXT);
CREATE TABLE IF NOT EXISTS reservations (reservation_id TEXT PRIMARY KEY, intent_id TEXT UNIQUE NOT NULL,
  symbol TEXT NOT NULL, stop_risk_per_contract TEXT NOT NULL, notional_per_contract TEXT NOT NULL,
  qty_reserved TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fills (fill_id TEXT PRIMARY KEY, intent_id TEXT, client_order_id TEXT NOT NULL,
  symbol TEXT NOT NULL, side TEXT NOT NULL, qty TEXT NOT NULL, price TEXT NOT NULL, fee TEXT NOT NULL,
  ts TEXT NOT NULL, reduce_only INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS lease (name TEXT PRIMARY KEY, owner TEXT NOT NULL, token INTEGER NOT NULL, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS graph_nodes (id TEXT PRIMARY KEY, type TEXT NOT NULL, record_ref TEXT NOT NULL,
  available_at TEXT NOT NULL, summary TEXT NOT NULL, invalidated_at TEXT);
CREATE TABLE IF NOT EXISTS graph_edges (id TEXT PRIMARY KEY, src TEXT NOT NULL, dst TEXT NOT NULL, type TEXT NOT NULL,
  available_at TEXT NOT NULL, evidence_ids TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_edges_src ON graph_edges(src);
CREATE INDEX IF NOT EXISTS ix_edges_dst ON graph_edges(dst);
CREATE INDEX IF NOT EXISTS ix_intents_status ON intents(status);
"""


class AuditWriteError(RuntimeError):
    pass


class StaleFencingToken(RuntimeError):
    pass


class LedgerCorrupted(RuntimeError):
    """The durable ledger failed its integrity check: the worker must not start (fail closed)."""


class Ledger:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            status = self.db.execute("PRAGMA quick_check").fetchone()[0]
            if status != "ok":
                raise LedgerCorrupted(f"integrity check failed: {status}")
            self.db.executescript(SCHEMA)
        except sqlite3.DatabaseError as exc:
            raise LedgerCorrupted(str(exc)) from exc
        self._lock = __import__("threading").RLock()
        self._migrate()
        self.fail_writes = 0  # fault injection: number of upcoming audit writes that fail
        self._depth = 0

    def _migrate(self):
        """Additive, idempotent migrations so older ledgers stay readable (never rewritten)."""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(intents)")}
        for col in ("auth_account_version", "auth_equity"):
            if col not in cols:
                self.db.execute(f"ALTER TABLE intents ADD COLUMN {col} TEXT")
        gcols = {r["name"] for r in self.db.execute("PRAGMA table_info(graph_nodes)")}
        for col in ("content_sha256", "expires_at"):
            if col not in gcols:
                self.db.execute(f"ALTER TABLE graph_nodes ADD COLUMN {col} TEXT")

    def legacy_summary(self) -> dict:
        """Counts of records written by the superseded Open-Jev path; they are audit-only."""
        out = {}
        for kind, label in contracts.LEGACY_EVENT_KINDS.items():
            n = self.db.execute("SELECT COUNT(*) FROM events WHERE kind=?", (kind,)).fetchone()[0]
            if n:
                out[label] = n
        return out

    # ------------------------------------------------------------------ transactions
    @contextmanager
    def tx(self):
        with self._lock:
            with self._tx() as db:
                yield db

    @contextmanager
    def _tx(self):
        if self._depth:
            self._depth += 1
            try:
                yield self.db
            finally:
                self._depth -= 1
            return
        self._depth = 1
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        finally:
            self._depth = 0

    # ------------------------------------------------------------------ events
    def append(self, kind: str, payload: dict, ts: datetime, correlation_id: str | None = None,
               event_id: str | None = None) -> str:
        with self._lock:
            return self._append(kind, payload, ts, correlation_id, event_id)

    def _append(self, kind, payload, ts, correlation_id, event_id):
        if self.fail_writes > 0:
            self.fail_writes -= 1
            raise AuditWriteError("injected audit persistence failure")
        if isinstance(payload, dict) and payload.get("kind") in contracts.DEFS:
            contracts.validate(payload)
            event_id = event_id or payload["event_id"]
        body = canonical_json(payload)
        event_id = event_id or stable_id("evt", kind, body, iso(ts))
        with self.tx() as db:
            db.execute("INSERT INTO events(event_id, kind, correlation_id, ts, payload, sha256) VALUES (?,?,?,?,?,?)",
                       (event_id, kind, correlation_id, iso(ts), body, sha256_hex(body)))
        return event_id

    def events(self, kind: str | None = None, correlation_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM events WHERE 1=1", []
        if kind:
            sql, args = sql + " AND kind=?", args + [kind]
        if correlation_id:
            sql, args = sql + " AND correlation_id=?", args + [correlation_id]
        rows = self.db.execute(sql + " ORDER BY seq", args).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    TRADING_KINDS = ("candidate_plan", "risk_authorization", "intent_created", "order_ack", "fill", "funding_settlement",
                     "protective_action", "outcome", "admission_rejected", "order_terminal")

    def trading_digest(self) -> str:
        """Digest of trading/accounting outputs only (plans, authorizations, orders, fills, funding, outcomes).
        Model provenance (e.g. live vs replay backend) is deliberately excluded so a replay stays labeled."""
        kinds = set(self.TRADING_KINDS)
        rows = self.db.execute("SELECT kind, payload FROM events ORDER BY seq").fetchall()
        return sha256_hex("\n".join(f"{r['kind']}|{r['payload']}" for r in rows if r["kind"] in kinds))

    def event_digest(self, exclude_kinds=("worker_started",)) -> str:
        """Hash over the ordered event stream (used to prove deterministic replay). Wall-clock
        measurements (latency_ms) are excluded: they are resource metrics, not trading events."""
        rows = self.db.execute("SELECT kind, payload FROM events ORDER BY seq").fetchall()
        parts = []
        for r in rows:
            if r["kind"] in exclude_kinds:
                continue
            body = json.loads(r["payload"])
            if isinstance(body, dict):
                body.pop("latency_ms", None)
            parts.append(f"{r['kind']}|{canonical_json(body)}")
        return sha256_hex("\n".join(parts))

    # ------------------------------------------------------------------ ownership fencing
    def acquire_lease(self, owner: str, ts: datetime, name: str = "trading_account") -> int:
        with self.tx() as db:
            row = db.execute("SELECT token FROM lease WHERE name=?", (name,)).fetchone()
            token = (row["token"] if row else 0) + 1
            db.execute("INSERT OR REPLACE INTO lease(name, owner, token, ts) VALUES (?,?,?,?)",
                       (name, owner, token, iso(ts)))
        return token

    def check_fence(self, token: int, name: str = "trading_account"):
        row = self.db.execute("SELECT token FROM lease WHERE name=?", (name,)).fetchone()
        if row is None or row["token"] != token:
            raise StaleFencingToken(f"fencing token {token} is not current")

    # ------------------------------------------------------------------ intents & reservations
    def create_intent(self, intent: dict, reservation: dict | None, token: int):
        self.check_fence(token)
        cols = ("intent_id", "client_order_id", "authorization_id", "candidate_id", "plan_sha256", "purpose",
                "symbol", "side", "qty", "price_limit", "stop_price", "order_type", "reduce_only", "status",
                "expires_at", "created_ts", "updated_ts", "auth_account_version", "auth_equity")
        with self.tx() as db:
            db.execute(f"INSERT INTO intents({','.join(cols)}, fencing_token) VALUES ({','.join('?' * len(cols))},?)",
                       [intent.get(c) for c in cols] + [token])
            if reservation:
                db.execute("INSERT INTO reservations VALUES (?,?,?,?,?,?,?)",
                           (reservation["reservation_id"], intent["intent_id"], reservation["symbol"],
                            reservation["stop_risk_per_contract"], reservation["notional_per_contract"],
                            reservation["qty_reserved"], "active"))

    def update_intent(self, intent_id: str, token: int, ts: datetime, **fields):
        self.check_fence(token)
        fields["updated_ts"] = iso(ts)
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as db:
            db.execute(f"UPDATE intents SET {sets} WHERE intent_id=?", list(fields.values()) + [intent_id])

    def intent(self, intent_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM intents WHERE intent_id=?", (intent_id,)).fetchone()
        return dict(row) if row else None

    def intent_by_coid(self, coid: str) -> dict | None:
        row = self.db.execute("SELECT * FROM intents WHERE client_order_id=?", (coid,)).fetchone()
        return dict(row) if row else None

    def intents(self, statuses=None, symbol=None, purpose=None) -> list[dict]:
        sql, args = "SELECT * FROM intents WHERE 1=1", []
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        if symbol:
            sql, args = sql + " AND symbol=?", args + [symbol]
        if purpose:
            sql, args = sql + " AND purpose=?", args + [purpose]
        return [dict(r) for r in self.db.execute(sql + " ORDER BY created_ts, intent_id", args)]

    def active_reservations(self, symbol: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM reservations WHERE status='active'", []
        if symbol:
            sql, args = sql + " AND symbol=?", [symbol]
        return [dict(r) for r in self.db.execute(sql, args)]

    def set_reservation_qty(self, intent_id: str, qty, token: int, release: bool = False):
        self.check_fence(token)
        with self.tx() as db:
            db.execute("UPDATE reservations SET qty_reserved=?, status=? WHERE intent_id=?",
                       (str(D(qty)), "released" if release else "active", intent_id))

    # ------------------------------------------------------------------ fills
    def record_fill(self, fill: dict) -> bool:
        """Insert a fill keyed by venue fill ID. Returns False for duplicate deliveries."""
        with self.tx() as db:
            cur = db.execute("INSERT OR IGNORE INTO fills VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (fill["fill_id"], fill.get("intent_id"), fill["client_order_id"], fill["symbol"],
                              fill["side"], str(fill["qty"]), str(fill["price"]), str(fill["fee"]), iso(fill["ts"]),
                              int(fill["reduce_only"])))
        return cur.rowcount == 1

    def fills(self, symbol: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM fills", []
        if symbol:
            sql, args = sql + " WHERE symbol=?", [symbol]
        return [dict(r) for r in self.db.execute(sql + " ORDER BY ts, fill_id", args)]

    # ------------------------------------------------------------------ kv
    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def put(self, key: str, value):
        with self.tx() as db:
            db.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?,?)", (key, json.dumps(value)))

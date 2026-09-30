"""Bounded evidence graph (indexed node/edge tables in the operational database).

The graph stores references and short summaries, never bulk arrays. Slices are filtered by
cutoff and invalidation *before* ranking and are capped by hops, nodes, edges and bytes —
whichever limit is hit first. Dropping optional context sets `truncated`; required IDs that
cannot be included are reported in `missing_required_ids`, never silently dropped.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from . import contracts
from .util import canonical_json, iso, parse_ts, stable_id

NODE_TYPES = set(contracts.DEFS["graph_slice"]["properties"]["nodes"]["items"]["properties"]["type"]["enum"])
EDGE_TYPES = set(contracts.DEFS["graph_slice"]["properties"]["edges"]["items"]["properties"]["type"]["enum"])


class GraphMemory:
    def __init__(self, ledger, limits):
        self.db = ledger.db
        self.ledger = ledger
        self.limits = limits

    def add_node(self, node_id: str, node_type: str, record_ref: str, available_at: datetime, summary: str):
        if node_type not in NODE_TYPES:
            raise ValueError(f"unregistered node type {node_type}")
        with self.ledger.tx() as db:
            db.execute("INSERT OR IGNORE INTO graph_nodes(id, type, record_ref, available_at, summary) VALUES (?,?,?,?,?)",
                       (node_id, node_type, record_ref[:256], iso(available_at), summary[:240] or "-"))

    def add_edge(self, src: str, dst: str, edge_type: str, available_at: datetime, evidence_ids=()):
        if edge_type not in EDGE_TYPES:
            raise ValueError(f"unregistered edge type {edge_type}")
        edge_id = stable_id("edge", src, dst, edge_type)
        with self.ledger.tx() as db:
            db.execute("INSERT OR IGNORE INTO graph_edges VALUES (?,?,?,?,?,?)",
                       (edge_id, src, dst, edge_type, iso(available_at), json.dumps(list(evidence_ids)[:32])))
        return edge_id

    def invalidate(self, node_id: str, at: datetime):
        with self.ledger.tx() as db:
            db.execute("UPDATE graph_nodes SET invalidated_at=? WHERE id=? AND invalidated_at IS NULL", (iso(at), node_id))

    def _node(self, node_id, cutoff):
        row = self.db.execute("SELECT * FROM graph_nodes WHERE id=?", (node_id,)).fetchone()
        if row is None or parse_ts(row["available_at"]) > cutoff:
            return None
        if row["invalidated_at"] and parse_ts(row["invalidated_at"]) <= cutoff:
            return None
        return row

    def slice(self, root_ids: list, cutoff: datetime, snapshot_id: str, correlation_id: str,
              required_ids: list = (), synthetic: bool = True) -> dict:
        lim = self.limits
        nodes, edges, seen, truncated = {}, {}, set(), False
        frontier = []
        for rid in list(required_ids) + [r for r in root_ids if r not in required_ids]:
            row = self._node(rid, cutoff)
            if row is not None and rid not in nodes:
                nodes[rid] = row
                frontier.append(rid)
        for _hop in range(lim["max_hops"]):
            nxt = []
            for nid in frontier:
                if nid in seen:
                    continue
                seen.add(nid)
                rows = self.db.execute("SELECT * FROM graph_edges WHERE (src=? OR dst=?) ORDER BY available_at DESC, id",
                                       (nid, nid)).fetchall()
                for e in rows:
                    if parse_ts(e["available_at"]) > cutoff or e["id"] in edges:
                        continue
                    other = e["dst"] if e["src"] == nid else e["src"]
                    row = self._node(other, cutoff)
                    if row is None:
                        continue
                    if other not in nodes:
                        if len(nodes) >= lim["max_nodes_per_context"]:
                            truncated = True
                            continue
                        nodes[other] = row
                        nxt.append(other)
                    if len(edges) >= lim["max_edges_per_context"]:
                        truncated = True
                        continue
                    edges[e["id"]] = e
            frontier = nxt
        rec = contracts.envelope("graph_slice", stable_id("evt_gs", snapshot_id, iso(cutoff), canonical_json(sorted(root_ids))),
                                 correlation_id, "graph_memory", iso(cutoff), iso(cutoff),
                                 iso(cutoff + timedelta(minutes=15)), synthetic)
        rec.update({"snapshot_id": snapshot_id, "root_ids": list(root_ids)[:8],
                    "nodes": [{"id": n["id"], "type": n["type"], "record_ref": n["record_ref"],
                               "available_at": n["available_at"], "summary": n["summary"]} for n in nodes.values()],
                    "edges": [{"id": e["id"], "from": e["src"], "to": e["dst"], "type": e["type"],
                               "available_at": e["available_at"], "evidence_ids": json.loads(e["evidence_ids"])}
                              for e in edges.values()],
                    "truncated": truncated, "missing_required_ids": [r for r in required_ids if r not in nodes][:32]})
        # byte budget: drop optional (non-required, non-root) nodes from the end until it fits
        protected = set(required_ids) | set(root_ids)
        while len(canonical_json(rec).encode()) > lim["max_serialized_bytes"]:
            optional = [n for n in rec["nodes"] if n["id"] not in protected]
            if not optional:
                rec["missing_required_ids"] = list(dict.fromkeys(rec["missing_required_ids"] + [
                    n["id"] for n in rec["nodes"] if n["id"] in required_ids]))[:32]
                break
            drop = optional[-1]["id"]
            rec["nodes"] = [n for n in rec["nodes"] if n["id"] != drop]
            rec["edges"] = [e for e in rec["edges"] if drop not in (e["from"], e["to"])]
            rec["truncated"] = True
        return contracts.validate(rec)

    def counts(self) -> dict:
        return {"nodes": self.db.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0],
                "edges": self.db.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]}

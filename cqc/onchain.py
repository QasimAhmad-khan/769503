"""On-chain and derivatives-context connectors behind a plugin interface.

Connectors normalize records into evidence with provenance; models never manufacture fields.
- MarketContextConnector: funding estimate and spread from venue market data (derivatives
  market data, *not* on-chain analysis).
- FixtureChainConnector: labeled synthetic finalized-chain activity with finality lag and
  reorg injection, used to exercise the plumbing offline.
- EsploraBlockConnector: a real public connector (Esplora REST API used by mempool.space /
  blockstream.info) for finalized BTC block activity. It captures *forward* only: without
  point-in-time history no on-chain backtest is claimed.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from urllib.request import Request, urlopen

import numpy as np

from .pit import EvidenceStore, make_evidence
from .util import UTC, iso, stable_id


class Connector:
    source_id = "abstract"
    features: dict = {}  # feature -> unit

    def fetch(self, feature: str, instrument: dict, cutoff: datetime, correlation_id: str) -> list[dict]:
        raise NotImplementedError


class MarketContextConnector(Connector):
    source_id = "sim_venue_market_data"
    features = {"estimated_funding_rate": "decimal_per_funding_interval", "spread_bps": "bps",
                "realized_vol_15m": "decimal_per_bar"}

    def __init__(self, series_1m: dict, synthetic: bool = True):
        self.series = series_1m
        self.synthetic = synthetic

    def fetch(self, feature, instrument, cutoff, correlation_id):
        key = instrument["symbol"].replace("-PERP", "")
        bars = self.series[key].completed(cutoff, limit=16 * 96 if feature == "realized_vol_15m" else 1)
        if not bars:
            return []
        bar = bars[-1]
        if feature == "estimated_funding_rate":
            value = round(bar.funding_rate_est, 8)
        elif feature == "spread_bps":
            value = round(bar.spread_bps, 4)
        else:
            closes = np.array([b.close for b in bars[::15]])
            value = round(float(np.std(np.diff(np.log(closes)), ddof=1)), 8) if len(closes) > 3 else None
            if value is None:
                return []
        return [make_evidence(feature, instrument, value, self.features[feature], "available", None, bar.end,
                              bar.available_at, cutoff, self.source_id, f"{key}:{feature}:{iso(bar.end)}", 0, cutoff,
                              correlation_id, "screener_connector", synthetic=self.synthetic)]


class FixtureChainConnector(Connector):
    """SYNTHETIC finalized-chain activity index. Blocks every 10 minutes, finalized after 6
    confirmations (60 min). Reorgs can be injected to test invalidation."""
    source_id = "synthetic_chain_fixture"
    features = {"chain_large_transfer_count_1h": "count_per_hour"}
    confirmations = 6
    block_seconds = 600

    def __init__(self, store: EvidenceStore, seed: int = 11, genesis: datetime | None = None):
        self.store = store
        self.seed = seed
        self.genesis = genesis or datetime(2026, 1, 1, tzinfo=UTC)
        self.reorged: set[int] = set()
        self._cache: dict[int, int] = {}

    def _height_at(self, t: datetime) -> int:
        return int((t - self.genesis).total_seconds() // self.block_seconds)

    def _value(self, height: int) -> int:
        if height not in self._cache:
            self._cache[height] = int(np.random.default_rng((self.seed, height)).poisson(40))
        return self._cache[height]

    def inject_reorg(self, height: int, at: datetime):
        self.reorged.add(height)
        self.store.invalidate(f"btc:{height}", at, "reorg_orphaned_block")

    def fetch(self, feature, instrument, cutoff, correlation_id):
        tip = self._height_at(cutoff)
        final = tip - self.confirmations
        if final < 0:
            return []
        block_time = self.genesis + timedelta(seconds=final * self.block_seconds)
        available = block_time + timedelta(seconds=self.confirmations * self.block_seconds)
        chain = {"chain_id": "bitcoin-synthetic", "block_height": final, "block_hash": stable_id("blk", final),
                 "finality": "orphaned" if final in self.reorged else "finalized"}
        status = "invalidated" if final in self.reorged else "available"
        value = None if status == "invalidated" else self._value(final)
        return [make_evidence(feature, instrument, value, self.features[feature], status,
                              "reorg_orphaned_block" if value is None else None, block_time, available, cutoff,
                              self.source_id, f"btc:{final}", 0, cutoff, correlation_id, "screener_connector",
                              chain=chain, synthetic=True)]


class EsploraBlockConnector(Connector):
    """Forward-capture connector for finalized BTC blocks via a public Esplora REST endpoint.

    Unverified in the build container (egress to mempool.space / blockstream.info is blocked);
    tests exercise it with recorded responses through the injectable `http_get`.
    """
    source_id = "esplora_btc_blocks"
    features = {"btc_finalized_block_tx_count": "tx_count"}
    confirmations = 6

    def __init__(self, base_url: str = "https://mempool.space/api", http_get=None, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.http_get = http_get or self._http_get
        self.capture_log: list[dict] = []  # forward availability record: observed_at is our real capture time

    def _http_get(self, path: str):
        request = Request(self.base_url + path, headers={"User-Agent": "cqc-paper/0.1"})
        with urlopen(request, timeout=self.timeout) as response:
            body = response.read(65536)
        return json.loads(body)

    def fetch(self, feature, instrument, cutoff, correlation_id):
        observed = datetime.now(UTC)
        if observed < cutoff:
            observed = cutoff
        tip = int(self.http_get("/blocks/tip/height"))
        height = tip - self.confirmations
        block_hash = str(self.http_get(f"/block-height/{height}"))
        block = self.http_get(f"/block/{block_hash}")
        block_time = datetime.fromtimestamp(int(block["timestamp"]), UTC)
        record = make_evidence(feature, instrument, int(block["tx_count"]), self.features[feature], "available", None,
                               block_time, observed, observed, self.source_id, f"btc:{height}:{block_hash}", 0,
                               max(cutoff, observed), correlation_id, "screener_connector",
                               chain={"chain_id": "bitcoin-mainnet", "block_height": height, "block_hash": block_hash,
                                      "finality": "finalized"}, synthetic=False,
                               quality_flags=["forward_capture_only", f"confirmations_{self.confirmations}"])
        self.capture_log.append({"height": height, "observed_at": iso(observed)})
        return [record]


class ConnectorRegistry:
    """Source registry: approved sources per feature. Connector calls are budgeted per request."""

    def __init__(self, store: EvidenceStore):
        self.store = store
        self.connectors: dict[str, Connector] = {}
        self.calls = 0

    def register(self, connector: Connector):
        self.connectors[connector.source_id] = connector

    def approved(self, feature: str) -> list[str]:
        return [sid for sid, c in self.connectors.items() if feature in c.features]

    def unit(self, feature: str) -> str | None:
        for c in self.connectors.values():
            if feature in c.features:
                return c.features[feature]
        return None

    def retrieve(self, feature, instrument, cutoff, source_ids, correlation_id):
        for sid in source_ids:
            connector = self.connectors.get(sid)
            if connector is None or feature not in connector.features:
                continue
            self.calls += 1
            try:
                for record in connector.fetch(feature, instrument, cutoff, correlation_id):
                    self.store.add(record)
            except Exception:  # connector failure = missing evidence, never a substituted value
                continue

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


class MetricsDB:
    """Persistent time-series storage for snapshot counter metrics.

    Writes one row per counter per snapshot. The history endpoint
    returns bucketed averages for charting; rate computation from
    consecutive snapshots is done by the frontend.
    """

    def __init__(self, db_path: str = "/data/metrics.db") -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self.healthy: bool = False

    async def initialize(self) -> None:
        """Open connection, enable WAL, create table."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        try:
            cursor = await self._db.execute("PRAGMA journal_mode=WAL")
            await cursor.fetchall()
            await self._db.execute("""
                CREATE TABLE IF NOT EXISTS metric_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    value REAL NOT NULL,
                    labels TEXT DEFAULT '{}'
                )
            """)
            await self._db.execute("""
                CREATE INDEX IF NOT EXISTS idx_ms_ts_name
                ON metric_snapshots(ts, name)
            """)
            await self._db.commit()
            self.healthy = True
            logger.info("Metrics DB initialized at %s (WAL mode)", self.db_path)
        except Exception:
            await self._db.close()
            self._db = None
            raise

    async def close(self) -> None:
        self.healthy = False
        if self._db:
            await self._db.close()
            self._db = None

    async def insert_snapshot(self, ts: int, metrics: dict[str, Any]) -> None:
        """Write one row per counter metric at the given unix ts."""
        if not self.healthy or self._db is None:
            return

        rows: list[tuple[int, str, float, str]] = []

        def add(name: str, value: float, labels: dict[str, str] | None = None) -> None:
            rows.append((ts, name, value, json.dumps(labels or {})))

        add("llm_requests_total", metrics.get("llm_requests_total", 0))
        add("semantic_cache_hits_total", metrics.get("semantic_cache_hits_total", 0))
        add("semantic_cache_misses_total", metrics.get("semantic_cache_misses_total", 0))
        add("total_input_tokens", metrics.get("total_input_tokens", 0))
        add("total_output_tokens", metrics.get("total_output_tokens", 0))
        add("total_cache_read_tokens", metrics.get("total_cache_read_tokens", 0))
        add("total_cache_creation_tokens", metrics.get("total_cache_creation_tokens", 0))
        add("estimated_tokens_saved", metrics.get("estimated_tokens_saved", 0))
        add("estimated_cost_saved_usd", metrics.get("estimated_cost_saved_usd", 0))
        add("estimated_total_cost_usd", metrics.get("estimated_total_cost_usd", 0))

        hits = metrics.get("semantic_cache_hits_total", 0)
        misses = metrics.get("semantic_cache_misses_total", 0)
        semantic_total = hits + misses
        if semantic_total > 0:
            add("semantic_cache_hit_rate", hits / semantic_total)
        else:
            add("semantic_cache_hit_rate", 0.0)

        by_model = metrics.get("by_model", {})
        for model_name, model_data in by_model.items():
            add("model_requests", model_data.get("requests", 0), {"model": model_name})
            add("model_input_tokens", model_data.get("input_tokens", 0), {"model": model_name})
            add(
                "model_cache_read_tokens",
                model_data.get("cache_read_tokens", 0),
                {"model": model_name},
            )

        await self._db.executemany(
            "INSERT INTO metric_snapshots (ts, name, value, labels) VALUES (?, ?, ?, ?)",
            rows,
        )
        await self._db.commit()

    async def query_history(
        self,
        name: str,
        start_ts: int,
        end_ts: int,
        bucket_seconds: int = 300,
        labels_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get bucketed time-series for a single metric.

        Returns list of {ts, avg, samples} per populated bucket.
        Empty buckets are omitted — the chart frontend should treat
        absent points as gaps rather than zeros.
        """
        if not self.healthy or self._db is None:
            return []

        w = bucket_seconds
        labels_sql = "AND labels = ?" if labels_filter else ""
        params = [w, w, name]
        if labels_filter:
            params.append(labels_filter)
        params.extend([start_ts, end_ts, w])

        cursor = await self._db.execute(
            f"""
            SELECT
                (CAST(ts / ? AS INTEGER) * ?) AS bucket_ts,
                AVG(value) AS avg_value,
                COUNT(*) AS sample_count
            FROM metric_snapshots
            WHERE name = ? {labels_sql}
              AND ts >= ? AND ts <= ?
            GROUP BY CAST(ts / ? AS INTEGER)
            ORDER BY bucket_ts
            """,
            params,
        )
        rows = await cursor.fetchall()

        return [
            {
                "ts": row["bucket_ts"],
                "avg": round(row["avg_value"], 4) if row["avg_value"] is not None else None,
                "samples": row["sample_count"],
            }
            for row in rows
        ]

    async def query_counters_with_labels(
        self,
        start_ts: int,
        end_ts: int,
    ) -> list[dict[str, Any]]:
        """Get all counter names and distinct labels in a time range."""
        if not self.healthy or self._db is None:
            return []

        cursor = await self._db.execute(
            """
            SELECT DISTINCT name, labels
            FROM metric_snapshots
            WHERE ts >= ? AND ts <= ?
            ORDER BY name, labels
            """,
            (start_ts, end_ts),
        )
        rows = await cursor.fetchall()
        return [{"name": row["name"], "labels": row["labels"]} for row in rows]

    async def snapshot_age(self) -> int | None:
        """Seconds since the most recent snapshot, or None if empty."""
        if not self.healthy or self._db is None:
            return None

        cursor = await self._db.execute("SELECT MAX(ts) AS max_ts FROM metric_snapshots")
        row = await cursor.fetchone()
        if row and row["max_ts"] is not None:
            return int(time.time()) - int(row["max_ts"])
        return None

    async def prune(self, retention_days: int = 7) -> int:
        """Delete rows older than retention_days. Returns count removed."""
        if not self.healthy or self._db is None:
            return 0

        cutoff = int(time.time()) - retention_days * 86400
        cursor = await self._db.execute(
            "DELETE FROM metric_snapshots WHERE ts < ?",
            (cutoff,),
        )
        await self._db.commit()
        removed = cursor.rowcount if cursor.rowcount >= 0 else 0
        if removed > 0:
            logger.debug("Pruned %d metric snapshot(s) older than %d days", removed, retention_days)
        return removed

    async def checkpoint(self) -> None:
        """Checkpoint WAL to keep its size bounded."""
        if not self.healthy or self._db is None:
            return
        try:
            cursor = await self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await cursor.fetchall()
        except Exception:
            logger.debug("WAL checkpoint skipped", exc_info=True)

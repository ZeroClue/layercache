"""Metrics Aggregator — Analytics rollups and queries.

Provides pre-computed hourly/daily rollups for the analytics dashboard.
Rollups are computed in the background snapshot loop to avoid real-time aggregation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


@dataclass
class HourlyRollup:
    """Hourly metrics rollup."""

    hour: str  # ISO format: YYYY-MM-DDTHH:00:00
    total_requests: int
    cache_hits: int
    cache_misses: int
    avg_latency_ms: float
    total_input_tokens: int
    total_output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int


@dataclass
class DailyRollup:
    """Daily metrics rollup."""

    date: str  # ISO format: YYYY-MM-DD
    total_requests: int
    cache_hits: int
    cache_misses: int
    avg_latency_ms: float
    total_input_tokens: int
    total_output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    unique_sessions: int


class MetricsAggregator:
    """Aggregate metrics for analytics dashboard."""

    def __init__(self, db_path: str) -> None:
        """Initialize aggregator.

        Args:
            db_path: Path to metrics SQLite database.
        """
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Connect to metrics database."""
        try:
            self._db = await aiosqlite.connect(self.db_path)
            self._db.row_factory = aiosqlite.Row
            await self._create_rollup_tables()
        except Exception as e:
            logger.error("Failed to connect to metrics database: %s", e)
            raise

    async def close(self) -> None:
        """Close database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    async def _create_rollup_tables(self) -> None:
        """Create rollup tables if they don't exist."""
        assert self._db is not None
        cursor = await self._db.execute("""
            CREATE TABLE IF NOT EXISTS metrics_hourly (
                hour TEXT PRIMARY KEY,
                total_requests INTEGER,
                cache_hits INTEGER,
                cache_misses INTEGER,
                avg_latency_ms REAL,
                total_input_tokens INTEGER,
                total_output_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_creation_tokens INTEGER
            )
        """)
        await cursor.fetchall()

        cursor = await self._db.execute("""
            CREATE TABLE IF NOT EXISTS metrics_daily (
                date TEXT PRIMARY KEY,
                total_requests INTEGER,
                cache_hits INTEGER,
                cache_misses INTEGER,
                avg_latency_ms REAL,
                total_input_tokens INTEGER,
                total_output_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_creation_tokens INTEGER,
                unique_sessions INTEGER
            )
        """)
        await cursor.fetchall()

        cursor = await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_hourly_hour
            ON metrics_hourly(hour)
        """)
        await cursor.fetchall()

        cursor = await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_daily_date
            ON metrics_daily(date)
        """)
        await cursor.fetchall()

        await self._db.commit()

    async def compute_hourly_rollup(self, hour: str) -> HourlyRollup | None:
        """Compute hourly rollup for a specific hour.

        Args:
            hour: Hour in ISO format (YYYY-MM-DDTHH:00:00).

        Returns:
            HourlyRollup or None if no data.
        """
        if self._db is None:
            return None

        # Normalize hour format for SQLite comparison (replace T with space)
        hour_sql = hour.replace("T", " ")
        next_hour_sql = self._next_hour(hour).replace("T", " ")

        cursor = await self._db.execute(
            """
            SELECT
                COUNT(*) as total_requests,
                SUM(CASE WHEN semantic_cache_hit = 1 THEN 1 ELSE 0 END) as cache_hits,
                SUM(CASE WHEN semantic_cache_hit = 0 THEN 1 ELSE 0 END) as cache_misses,
                AVG(duration_ms) as avg_latency_ms,
                COALESCE(SUM(input_tokens), 0) as total_input_tokens,
                COALESCE(SUM(output_tokens), 0) as total_output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) as cache_read_tokens,
                COALESCE(SUM(cache_creation_tokens), 0) as cache_creation_tokens
            FROM metrics_requests
            WHERE created_at >= ? AND created_at < ?
        """,
            (hour_sql, next_hour_sql),
        )

        row = await cursor.fetchone()
        if row is None or row["total_requests"] == 0:
            return None

        return HourlyRollup(
            hour=hour,
            total_requests=row["total_requests"] or 0,
            cache_hits=row["cache_hits"] or 0,
            cache_misses=row["cache_misses"] or 0,
            avg_latency_ms=row["avg_latency_ms"] or 0.0,
            total_input_tokens=row["total_input_tokens"] or 0,
            total_output_tokens=row["total_output_tokens"] or 0,
            cache_read_tokens=row["cache_read_tokens"] or 0,
            cache_creation_tokens=row["cache_creation_tokens"] or 0,
        )

    async def compute_daily_rollup(self, date: str) -> DailyRollup | None:
        """Compute daily rollup for a specific date.

        Args:
            date: Date in ISO format (YYYY-MM-DD).

        Returns:
            DailyRollup or None if no data.
        """
        if self._db is None:
            return None

        cursor = await self._db.execute(
            """
            SELECT
                COUNT(*) as total_requests,
                SUM(CASE WHEN semantic_cache_hit = 1 THEN 1 ELSE 0 END) as cache_hits,
                SUM(CASE WHEN semantic_cache_hit = 0 THEN 1 ELSE 0 END) as cache_misses,
                AVG(duration_ms) as avg_latency_ms,
                COALESCE(SUM(input_tokens), 0) as total_input_tokens,
                COALESCE(SUM(output_tokens), 0) as total_output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) as cache_read_tokens,
                COALESCE(SUM(cache_creation_tokens), 0) as cache_creation_tokens,
                COUNT(DISTINCT session_id) as unique_sessions
            FROM metrics_requests
            WHERE DATE(created_at) = ?
        """,
            (date,),
        )

        row = await cursor.fetchone()
        if row is None or row["total_requests"] == 0:
            return None

        return DailyRollup(
            date=date,
            total_requests=row["total_requests"] or 0,
            cache_hits=row["cache_hits"] or 0,
            cache_misses=row["cache_misses"] or 0,
            avg_latency_ms=row["avg_latency_ms"] or 0.0,
            total_input_tokens=row["total_input_tokens"] or 0,
            total_output_tokens=row["total_output_tokens"] or 0,
            cache_read_tokens=row["cache_read_tokens"] or 0,
            cache_creation_tokens=row["cache_creation_tokens"] or 0,
            unique_sessions=row["unique_sessions"] or 0,
        )

    async def save_hourly_rollup(self, rollup: HourlyRollup) -> None:
        """Save hourly rollup to database.

        Args:
            rollup: HourlyRollup to save.
        """
        if self._db is None:
            return

        cursor = await self._db.execute(
            """
            INSERT OR REPLACE INTO metrics_hourly
            (hour, total_requests, cache_hits, cache_misses, avg_latency_ms,
             total_input_tokens, total_output_tokens, cache_read_tokens, cache_creation_tokens)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                rollup.hour,
                rollup.total_requests,
                rollup.cache_hits,
                rollup.cache_misses,
                rollup.avg_latency_ms,
                rollup.total_input_tokens,
                rollup.total_output_tokens,
                rollup.cache_read_tokens,
                rollup.cache_creation_tokens,
            ),
        )
        await cursor.fetchall()
        await self._db.commit()

    async def save_daily_rollup(self, rollup: DailyRollup) -> None:
        """Save daily rollup to database.

        Args:
            rollup: DailyRollup to save.
        """
        if self._db is None:
            return

        cursor = await self._db.execute(
            """
            INSERT OR REPLACE INTO metrics_daily
            (date, total_requests, cache_hits, cache_misses, avg_latency_ms,
             total_input_tokens, total_output_tokens, cache_read_tokens,
             cache_creation_tokens, unique_sessions)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                rollup.date,
                rollup.total_requests,
                rollup.cache_hits,
                rollup.cache_misses,
                rollup.avg_latency_ms,
                rollup.total_input_tokens,
                rollup.total_output_tokens,
                rollup.cache_read_tokens,
                rollup.cache_creation_tokens,
                rollup.unique_sessions,
            ),
        )
        await cursor.fetchall()
        await self._db.commit()

    async def get_recent_hourly(self, limit: int = 24) -> list[HourlyRollup]:
        """Get recent hourly rollups.

        Args:
            limit: Number of hours to return (default 24).

        Returns:
            List of HourlyRollup, most recent first.
        """
        if self._db is None:
            return []

        cursor = await self._db.execute(
            """
            SELECT * FROM metrics_hourly
            ORDER BY hour DESC
            LIMIT ?
        """,
            (limit,),
        )

        rows = await cursor.fetchall()
        return [
            HourlyRollup(
                hour=row["hour"],
                total_requests=row["total_requests"],
                cache_hits=row["cache_hits"],
                cache_misses=row["cache_misses"],
                avg_latency_ms=row["avg_latency_ms"],
                total_input_tokens=row["total_input_tokens"],
                total_output_tokens=row["total_output_tokens"],
                cache_read_tokens=row["cache_read_tokens"],
                cache_creation_tokens=row["cache_creation_tokens"],
            )
            for row in rows
        ]

    async def get_recent_daily(self, limit: int = 30) -> list[DailyRollup]:
        """Get recent daily rollups.

        Args:
            limit: Number of days to return (default 30).

        Returns:
            List of DailyRollup, most recent first.
        """
        if self._db is None:
            return []

        cursor = await self._db.execute(
            """
            SELECT * FROM metrics_daily
            ORDER BY date DESC
            LIMIT ?
        """,
            (limit,),
        )

        rows = await cursor.fetchall()
        return [
            DailyRollup(
                date=row["date"],
                total_requests=row["total_requests"],
                cache_hits=row["cache_hits"],
                cache_misses=row["cache_misses"],
                avg_latency_ms=row["avg_latency_ms"],
                total_input_tokens=row["total_input_tokens"],
                total_output_tokens=row["total_output_tokens"],
                cache_read_tokens=row["cache_read_tokens"],
                cache_creation_tokens=row["cache_creation_tokens"],
                unique_sessions=row["unique_sessions"],
            )
            for row in rows
        ]

    async def get_cache_hit_rate(self, hours: int = 24) -> float:
        """Get cache hit rate for recent period.

        Args:
            hours: Number of hours to look back (default 24).

        Returns:
            Cache hit rate as percentage (0-100).
        """
        if self._db is None:
            return 0.0

        cursor = await self._db.execute(
            """
            SELECT
                SUM(cache_hits) as total_hits,
                SUM(total_requests) as total_requests
            FROM metrics_hourly
            WHERE hour >= datetime('now', ?)
        """,
            (f"-{hours} hours",),
        )

        row = await cursor.fetchone()
        if row is None or row["total_requests"] is None or row["total_requests"] == 0:
            return 0.0

        hits = row["total_hits"] or 0
        requests = row["total_requests"] or 0
        return (hits / requests) * 100.0

    async def get_token_savings(self, hours: int = 24) -> dict[str, Any]:
        """Get token savings from caching.

        Args:
            hours: Number of hours to look back (default 24).

        Returns:
            Dict with input_tokens_saved, output_tokens, total_tokens.
        """
        if self._db is None:
            return {"input_tokens_saved": 0, "output_tokens": 0, "total_tokens": 0}

        cursor = await self._db.execute(
            """
            SELECT
                COALESCE(SUM(cache_read_tokens), 0) as cache_read_tokens,
                COALESCE(SUM(total_input_tokens), 0) as total_input_tokens,
                COALESCE(SUM(total_output_tokens), 0) as total_output_tokens
            FROM metrics_hourly
            WHERE hour >= datetime('now', ?)
        """,
            (f"-{hours} hours",),
        )

        row = await cursor.fetchone()
        if row is None:
            return {"input_tokens_saved": 0, "output_tokens": 0, "total_tokens": 0}

        cache_read = row["cache_read_tokens"] or 0
        total_input = row["total_input_tokens"] or 0
        total_output = row["total_output_tokens"] or 0

        return {
            "input_tokens_saved": cache_read,
            "output_tokens": total_output,
            "total_tokens": total_input + total_output,
            "savings_percentage": (cache_read / total_input * 100.0) if total_input > 0 else 0.0,
        }

    @staticmethod
    def _next_hour(hour: str) -> str:
        """Get next hour from ISO hour string."""
        from datetime import timedelta
        dt = datetime.fromisoformat(hour.replace("Z", "+00:00"))
        next_dt = dt.replace(tzinfo=UTC) + timedelta(hours=1)
        return next_dt.strftime("%Y-%m-%dT%H:00:00")

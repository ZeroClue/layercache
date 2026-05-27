"""Probation tracking for new cache entries.

Tracks probation count for new cache entries and handles promotion to stable cache.
Entries must pass N=10 successful validations or wait 1 hour for auto-promotion.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


class ProbationTracker:
    """Tracks probation status for new cache entries.

    New cache entries start in probation with count=0. Each successful validation
    increments the count. After N=10 successful validations or 1 hour timeout,
    entries are promoted to stable cache.

    Attributes:
        db_path: Path to the SQLite database.
        healthy: Whether the tracker is initialized and healthy.
        max_probation_entries: Maximum entries to keep in probation (LRU eviction).
        promotion_threshold: Number of successful validations needed for promotion.
        auto_promotion_seconds: Seconds before auto-promotion regardless of count.
    """

    def __init__(
        self,
        db_path: str = "/data/semantic_cache.db",
        max_probation_entries: int = 1000,
        promotion_threshold: int = 10,
        auto_promotion_seconds: int = 3600,
    ) -> None:
        self.db_path = db_path
        self.healthy = False
        self.max_probation_entries = max_probation_entries
        self.promotion_threshold = promotion_threshold
        self.auto_promotion_seconds = auto_promotion_seconds
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Initialize the probation tracker database.

        Creates the probation_tracker table if it doesn't exist.
        """
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row

        try:
            cursor = await self._db.execute("PRAGMA journal_mode=WAL")
            await cursor.fetchall()

            await self._db.execute("""
                CREATE TABLE IF NOT EXISTS probation_tracker (
                    entry_id TEXT PRIMARY KEY,
                    probation_count INTEGER DEFAULT 0,
                    created_at REAL NOT NULL,
                    last_validated_at REAL,
                    validation_failed INTEGER DEFAULT 0
                )
            """)

            await self._db.execute("""
                CREATE INDEX IF NOT EXISTS idx_probation_count
                ON probation_tracker(probation_count)
            """)

            await self._db.execute("""
                CREATE INDEX IF NOT EXISTS idx_probation_created
                ON probation_tracker(created_at)
            """)

            await self._db.commit()
            self.healthy = True
            logger.info("Probation tracker initialized at %s", self.db_path)
        except Exception:
            await self._db.close()
            self._db = None
            raise

    async def close(self) -> None:
        """Close the database connection."""
        self.healthy = False
        if self._db:
            await self._db.close()
            self._db = None

    async def increment_probation_count(self, entry_id: str) -> None:
        """Increment the probation count for an entry.

        Args:
            entry_id: The cache entry ID.
        """
        if not self.healthy or self._db is None:
            return

        now = time.time()
        await self._db.execute(
            """
            INSERT INTO probation_tracker (entry_id, probation_count, created_at, last_validated_at)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(entry_id) DO UPDATE SET
                probation_count = probation_count + 1,
                last_validated_at = ?
            WHERE validation_failed = 0
            """,
            (entry_id, now, now, now),
        )
        await self._db.commit()

        await self.evict_oldest()

    async def get_probation_count(self, entry_id: str) -> int:
        """Get the current probation count for an entry.

        Args:
            entry_id: The cache entry ID.

        Returns:
            The probation count (0 if entry doesn't exist).
        """
        if not self.healthy or self._db is None:
            return 0

        cursor = await self._db.execute(
            "SELECT probation_count FROM probation_tracker WHERE entry_id = ?",
            (entry_id,),
        )
        row = await cursor.fetchone()
        return row["probation_count"] if row else 0

    async def check_promotion(self, entry_id: str) -> bool:
        """Check if an entry should be promoted from probation.

        Promotion occurs when:
        1. Probation count reaches N=10, OR
        2. Auto-promotion timeout (1 hour) is reached

        Args:
            entry_id: The cache entry ID.

        Returns:
            True if the entry should be promoted, False otherwise.
        """
        if not self.healthy or self._db is None:
            return False

        cursor = await self._db.execute(
            """
            SELECT probation_count, created_at, validation_failed
            FROM probation_tracker
            WHERE entry_id = ?
            """,
            (entry_id,),
        )
        row = await cursor.fetchone()

        if not row:
            return False

        if row["validation_failed"]:
            return False

        if row["probation_count"] >= self.promotion_threshold:
            return True

        now = time.time()
        age_seconds = now - row["created_at"]
        if age_seconds >= self.auto_promotion_seconds:
            return True

        return False

    async def record_validation_failure(self, entry_id: str) -> None:
        """Record a validation failure for an entry.

        Failed entries are not promoted and don't increment probation count.

        Args:
            entry_id: The cache entry ID.
        """
        if not self.healthy or self._db is None:
            return

        now = time.time()
        await self._db.execute(
            """
            INSERT INTO probation_tracker (entry_id, probation_count, created_at, validation_failed)
            VALUES (?, 0, ?, 1)
            ON CONFLICT(entry_id) DO UPDATE SET
                validation_failed = 1,
                last_validated_at = ?
            """,
            (entry_id, now, now),
        )
        await self._db.commit()

    async def remove_from_probation(self, entry_id: str) -> None:
        """Remove an entry from probation tracking (after promotion).

        Args:
            entry_id: The cache entry ID.
        """
        if not self.healthy or self._db is None:
            return

        await self._db.execute(
            "DELETE FROM probation_tracker WHERE entry_id = ?",
            (entry_id,),
        )
        await self._db.commit()

    async def evict_oldest(self) -> int:
        """Evict oldest probation entries to stay within max bound.

        Returns:
            Number of entries evicted.
        """
        if not self.healthy or self._db is None:
            return 0

        cursor = await self._db.execute(
            "SELECT COUNT(*) as count FROM probation_tracker",
        )
        row = await cursor.fetchone()
        current_count = row["count"] if row else 0

        if current_count <= self.max_probation_entries:
            return 0

        to_evict = current_count - self.max_probation_entries
        await self._db.execute(
            """
            DELETE FROM probation_tracker
            WHERE entry_id IN (
                SELECT entry_id FROM probation_tracker
                ORDER BY created_at ASC
                LIMIT ?
            )
            """,
            (to_evict,),
        )
        await self._db.commit()

        logger.debug("Evicted %d probation entries", to_evict)
        return to_evict

    async def stats(self) -> dict[str, Any]:
        """Get probation tracker statistics.

        Returns:
            Dictionary with probation statistics.
        """
        if not self.healthy or self._db is None:
            return {"probation_entries_count": 0}

        cursor = await self._db.execute(
            "SELECT COUNT(*) as count FROM probation_tracker WHERE validation_failed = 0",
        )
        row = await cursor.fetchone()
        active_count = row["count"] if row else 0

        cursor = await self._db.execute(
            "SELECT COUNT(*) as count FROM probation_tracker WHERE validation_failed = 1",
        )
        row = await cursor.fetchone()
        failed_count = row["count"] if row else 0

        return {
            "probation_entries_count": active_count,
            "probation_failed_count": failed_count,
            "max_entries": self.max_probation_entries,
        }

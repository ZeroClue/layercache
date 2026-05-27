"""Semantic Cache - Bypasses the LLM for semantically similar queries.

The Semantic Cache uses a dual-key strategy:
1. **Exact Match Key:** SHA-256 hash of L0+L1+L2 content (stable prefix)
2. **Semantic Search Key:** Embedding of L4 (user query)

A cache hit requires BOTH:
- The prefix hash matches exactly (same system instructions, context, session)
- The user query embedding has cosine similarity > threshold (similar question)

This ensures that if system instructions change but the query is the same,
the cache correctly misses (the answer would be different).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from pathlib import Path
from typing import Any

from ..models import CacheEntry, StratifiedPrompt

logger = logging.getLogger(__name__)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if len(a) != len(b) or len(a) == 0:
        return 0.0
    dot_product = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot_product / (norm_a * norm_b)


class SemanticCache:
    """SQLite-based semantic cache for LLM responses.

    Stores responses keyed by prefix hash + query embedding similarity.
    Supports configurable TTL and similarity thresholds.
    """

    def __init__(
        self,
        db_path: str = "/data/semantic_cache.db",
        default_ttl: int = 300,
        similarity_threshold: float = 0.95,
        embedder: Any = None,
    ) -> None:
        self.db_path = db_path
        self.default_ttl = default_ttl
        self.similarity_threshold = similarity_threshold
        self._embedder = embedder
        self._db = None

    async def initialize(self) -> None:
        """Initialize the cache database and create tables."""
        import aiosqlite

        # Ensure parent directory exists
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        cursor = await self._db.execute("PRAGMA journal_mode=WAL")
        await cursor.fetchall()

        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS semantic_cache (
                id TEXT PRIMARY KEY,
                prefix_hash TEXT NOT NULL,
                query_text TEXT NOT NULL,
                query_embedding BLOB NOT NULL,
                response_payload TEXT NOT NULL,
                model TEXT NOT NULL,
                ttl_expires_at REAL NOT NULL,
                created_at REAL NOT NULL
            )
        """)

        # Index for prefix hash lookups
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_prefix_hash
            ON semantic_cache(prefix_hash)
        """)

        # Index for TTL expiration
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ttl_expires
            ON semantic_cache(ttl_expires_at)
        """)

        await self._db.commit()
        logger.info("Semantic cache initialized at %s", self.db_path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    def _hash_prefix(self, prompt: StratifiedPrompt) -> str:
        """Compute exact-match hash of the stable prefix (L0+L1+L2)."""
        return prompt.prefix_hash()

    async def lookup(self, prompt: StratifiedPrompt, model: str = "") -> CacheEntry | None:
        """Look up a cached response for the given prompt.

        A cache hit requires:
        1. Prefix hash match (exact)
        2. Query embedding similarity > threshold
        3. TTL not expired

        Args:
            prompt: The stratified prompt to look up.
            model: The model name (used for additional matching).

        Returns:
            A CacheEntry if found, None otherwise.
        """
        if self._db is None:
            return None

        prefix_hash = self._hash_prefix(prompt)
        query_text = prompt.get_user_query()

        if not query_text:
            return None

        # Compute query embedding
        query_embedding = await self._get_embedding(query_text)
        if query_embedding is None:
            return None

        # Find all cache entries with matching prefix hash
        now = time.time()
        cursor = await self._db.execute(
            """
            SELECT id, prefix_hash, query_text, query_embedding, response_payload,
                   model, ttl_expires_at, created_at
            FROM semantic_cache
            WHERE prefix_hash = ? AND ttl_expires_at > ?
            ORDER BY created_at DESC
            """,
            (prefix_hash, now),
        )
        rows = await cursor.fetchall()

        best_match: CacheEntry | None = None
        best_similarity = 0.0

        for row in rows:
            stored_embedding = json.loads(row["query_embedding"])

            sim = cosine_similarity(query_embedding, stored_embedding)

            if sim > best_similarity and sim >= self.similarity_threshold:
                best_similarity = sim
                best_match = CacheEntry(
                    id=row["id"],
                    prefix_hash=row["prefix_hash"],
                    query_text=row["query_text"],
                    query_embedding=stored_embedding,
                    response_payload=json.loads(row["response_payload"]),
                    model=row["model"],
                    ttl_expires_at=row["ttl_expires_at"],
                    created_at=row["created_at"],
                )

        if best_match:
            logger.debug(
                "Semantic cache HIT (similarity=%.4f, prefix=%s...)",
                best_similarity,
                prefix_hash[:12],
            )
        else:
            logger.debug("Semantic cache MISS (prefix=%s...)", prefix_hash[:12])

        return best_match

    async def store(
        self,
        prompt: StratifiedPrompt,
        response: dict[str, Any],
        model: str,
        ttl: int | None = None,
    ) -> str:
        """Store a response in the semantic cache.

        Args:
            prompt: The stratified prompt.
            response: The LLM response to cache.
            model: The model that generated the response.
            ttl: TTL in seconds. Uses default if None.

        Returns:
            The cache entry ID.
        """
        if self._db is None:
            return ""

        prefix_hash = self._hash_prefix(prompt)
        query_text = prompt.get_user_query()

        if not query_text:
            return ""

        query_embedding = await self._get_embedding(query_text)
        if query_embedding is None:
            return ""

        entry_id = hashlib.sha256(f"{prefix_hash}:{query_text}:{time.time()}".encode()).hexdigest()

        now = time.time()
        effective_ttl = ttl if ttl is not None else self.default_ttl

        await self._db.execute(
            """
            INSERT INTO semantic_cache
            (id, prefix_hash, query_text, query_embedding, response_payload,
             model, ttl_expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry_id,
                prefix_hash,
                query_text,
                json.dumps(query_embedding),
                json.dumps(response),
                model,
                now + effective_ttl,
                now,
            ),
        )
        await self._db.commit()

        logger.debug(
            "Stored semantic cache entry (id=%s, prefix=%s..., ttl=%ds)",
            entry_id[:12],
            prefix_hash[:12],
            effective_ttl,
        )

        return entry_id

    async def invalidate(self, prefix_hash: str | None = None) -> int:
        """Invalidate cache entries.

        Args:
            prefix_hash: If provided, only invalidate entries with this prefix hash.
                        If None, invalidate all entries.

        Returns:
            Number of entries invalidated.
        """
        if self._db is None:
            return 0

        if prefix_hash:
            cursor = await self._db.execute(
                "DELETE FROM semantic_cache WHERE prefix_hash = ?",
                (prefix_hash,),
            )
        else:
            cursor = await self._db.execute("DELETE FROM semantic_cache")

        await self._db.commit()
        return cursor.rowcount

    async def cleanup_expired(self) -> int:
        """Remove expired entries from the cache.

        Returns:
            Number of entries removed.
        """
        if self._db is None:
            return 0

        now = time.time()
        cursor = await self._db.execute(
            "DELETE FROM semantic_cache WHERE ttl_expires_at <= ?",
            (now,),
        )
        await self._db.commit()
        removed = cursor.rowcount
        if removed > 0:
            logger.debug("Cleaned up %d expired cache entries", removed)
        return removed

    async def is_in_probation(self, entry_id: str) -> bool:
        """Check if a cache entry is in probation.

        Args:
            entry_id: The cache entry ID to check.

        Returns:
            True if the entry is in probation, False otherwise.
        """
        # This method is a placeholder - actual probation tracking
        # is handled by ProbationTracker which has direct DB access
        # This is here for API completeness
        return False

    async def stats(self) -> dict[str, Any]:
        """Get cache statistics."""
        if self._db is None:
            return {"total_entries": 0, "valid_entries": 0}

        now = time.time()

        cursor = await self._db.execute("SELECT COUNT(*) as total FROM semantic_cache")
        row = await cursor.fetchone()
        total = row["total"] if row else 0

        cursor = await self._db.execute(
            "SELECT COUNT(*) as valid FROM semantic_cache WHERE ttl_expires_at > ?",
            (now,),
        )
        row = await cursor.fetchone()
        valid = row["valid"] if row else 0

        return {
            "total_entries": total,
            "valid_entries": valid,
        }

    async def _get_embedding(self, text: str) -> list[float] | None:
        """Get embedding for a text string."""
        if self._embedder is None:
            return None

        try:
            if hasattr(self._embedder, "embed"):
                result = await self._embedder.embed(text)
                return result
        except Exception as e:
            logger.error("Failed to generate embedding: %s", e)

        return None

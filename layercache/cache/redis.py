"""Redis Backend for Semantic Cache.

Redis-based semantic cache for multi-agent concurrent access.
Implements the same interface as SemanticCache for easy swapping.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from typing import Any

import redis.asyncio as redis

from ..models import CacheEntry, StratifiedPrompt

logger = logging.getLogger(__name__)


def _sanitize_session_id(session_id: str) -> str:
    """Remove non-alphanumeric chars except dash from session ID."""
    return re.sub(r"[^a-zA-Z0-9-]", "", session_id) or "default"


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


class RedisSemanticCache:
    """Redis-based semantic cache for LLM responses.

    Stores response keyed by prefix hash + query embedding similarity.
    Supports configurable TTL and similarity thresholds.
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        default_ttl: int = 3600,
        similarity_threshold: float = 0.95,
        embedder: Any = None,
        pool_size: int = 10,
        socket_timeout: float = 5.0,
    ) -> None:
        self.redis_url = redis_url
        self.default_ttl = default_ttl
        self.similarity_threshold = similarity_threshold
        self._embedder = embedder
        self._pool_size = pool_size
        self._socket_timeout = socket_timeout
        self._redis: redis.Redis | None = None
        self._pool: redis.ConnectionPool | None = None

    async def initialize(self) -> None:
        """Initialize the Redis connection pool."""
        try:
            self._pool = redis.ConnectionPool.from_url(
                self.redis_url,
                max_connections=self._pool_size,
                socket_timeout=self._socket_timeout,
                decode_responses=False,
            )
            self._redis = redis.Redis(connection_pool=self._pool)

            # Health check with explicit timeout
            import asyncio

            await asyncio.wait_for(self._redis.ping(), timeout=2.0)
            logger.info(
                "Redis cache initialized at %s (pool_size=%d)",
                self.redis_url,
                self._pool_size,
            )
        except Exception as e:
            logger.error("Failed to initialize Redis cache: %s", e)
            raise

    async def close(self) -> None:
        """Close the Redis connection pool."""
        if self._pool:
            await self._pool.disconnect()
            self._pool = None
            self._redis = None

    def _hash_prefix(self, prompt: StratifiedPrompt) -> str:
        """Compute exact-match hash of the stable prefix (L0+L1+L2)."""
        return prompt.prefix_hash()

    def _make_cache_key(self, prefix_hash: str, entry_id: str) -> str:
        """Create Redis key for cache entry."""
        return f"layercache:cache:{prefix_hash}:{entry_id}"

    def _make_index_key(self, prefix_hash: str) -> str:
        """Create Redis key for prefix hash index (sorted set)."""
        return f"layercache:index:{prefix_hash}"

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
        if self._redis is None:
            return None

        prefix_hash = self._hash_prefix(prompt)
        query_text = prompt.get_user_query()

        if not query_text:
            return None

        # Compute query embedding
        query_embedding = await self._get_embedding(query_text)
        if query_embedding is None:
            return None

        # Get all cache entries for this prefix hash (sorted by created_at DESC)
        index_key = self._make_index_key(prefix_hash)
        now = time.time()

        # Get entries from sorted set (score = created_at, descending)
        entry_ids = await self._redis.zrevrange(index_key, 0, -1, withscores=True)

        if not entry_ids:
            logger.debug("Semantic cache MISS (prefix=%s...) - no entries", prefix_hash[:12])
            return None

        best_match: CacheEntry | None = None
        best_similarity = 0.0

        for entry_id_bytes, created_at in entry_ids:
            entry_id = (
                entry_id_bytes.decode() if isinstance(entry_id_bytes, bytes) else entry_id_bytes
            )

            # Check TTL
            ttl_key = f"layercache:ttl:{entry_id}"
            ttl_expires = await self._redis.get(ttl_key)
            if ttl_expires is None:
                continue
            ttl_expires_at = float(ttl_expires)
            if ttl_expires_at <= now:
                continue

            # Get cache entry
            cache_key = self._make_cache_key(prefix_hash, entry_id)
            entry_data = await self._redis.get(cache_key)
            if entry_data is None:
                continue

            entry = json.loads(entry_data)
            stored_embedding = json.loads(entry["query_embedding"])
            sim = cosine_similarity(query_embedding, stored_embedding)

            if sim > best_similarity and sim >= self.similarity_threshold:
                best_similarity = sim
                best_match = CacheEntry(
                    id=entry_id,
                    prefix_hash=prefix_hash,
                    query_text=entry["query_text"],
                    query_embedding=stored_embedding,
                    response_payload=json.loads(entry["response_payload"]),
                    model=entry["model"],
                    ttl_expires_at=ttl_expires_at,
                    created_at=created_at,
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
        if self._redis is None:
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

        # Create cache entry
        cache_entry = {
            "query_text": query_text,
            "query_embedding": json.dumps(query_embedding),
            "response_payload": json.dumps(response),
            "model": model,
        }

        # Store in Redis
        cache_key = self._make_cache_key(prefix_hash, entry_id)
        ttl_key = f"layercache:ttl:{entry_id}"
        index_key = self._make_index_key(prefix_hash)

        async with self._redis.pipeline(transaction=True) as pipe:
            # Store cache entry
            await pipe.set(cache_key, json.dumps(cache_entry))
            # Store TTL
            await pipe.set(ttl_key, str(now + effective_ttl))
            # Add to sorted set index (score = created_at)
            await pipe.zadd(index_key, {entry_id: now})
            # Set TTL on all keys
            await pipe.expire(cache_key, effective_ttl)
            await pipe.expire(ttl_key, effective_ttl)
            await pipe.expire(index_key, effective_ttl)
            await pipe.execute()

        logger.debug(
            "Stored Redis cache entry (id=%s, prefix=%s..., ttl=%ds)",
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
        if self._redis is None:
            return 0

        removed = 0

        if prefix_hash:
            # Delete all entries for this prefix hash
            index_key = self._make_index_key(prefix_hash)
            entry_ids = await self._redis.zrange(index_key, 0, -1)

            async with self._redis.pipeline(transaction=True) as pipe:
                for entry_id_bytes in entry_ids:
                    entry_id = (
                        entry_id_bytes.decode()
                        if isinstance(entry_id_bytes, bytes)
                        else entry_id_bytes
                    )
                    cache_key = self._make_cache_key(prefix_hash, entry_id)
                    ttl_key = f"layercache:ttl:{entry_id}"
                    await pipe.delete(cache_key, ttl_key)
                    removed += 1
                await pipe.delete(index_key)
                await pipe.execute()
        else:
            # Delete all cache entries
            cursor = 0
            while True:
                cursor, keys = await self._redis.scan(cursor, match="layercache:cache:*", count=100)
                if keys:
                    await self._redis.delete(*keys)
                    removed += len(keys)
                if cursor == 0:
                    break

        logger.debug("Invalidated %d cache entries", removed)
        return removed

    async def cleanup_expired(self) -> int:
        """Remove expired entries from the cache.

        Redis handles expiration automatically via TTL, but this method
        can clean up orphaned index entries.

        Returns:
            Number of entries removed.
        """
        # Redis auto-expires keys with TTL, so this is mostly a no-op
        # Could add logic to clean up orphaned index entries if needed
        return 0

    async def stats(self) -> dict[str, Any]:
        """Get cache statistics."""
        if self._redis is None:
            return {"total_entries": 0, "valid_entries": 0}

        # Count all index keys
        cursor = 0
        total_keys = 0
        while True:
            cursor, keys = await self._redis.scan(cursor, match="layercache:index:*", count=100)
            total_keys += len(keys)
            if cursor == 0:
                break

        # Get Redis info
        info = await self._redis.info("memory")
        memory_used = info.get("used_memory_human", "unknown")

        return {
            "total_entries": total_keys,
            "valid_entries": total_keys,  # Redis auto-expires, so all indexed entries are valid
            "memory_used": memory_used,
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

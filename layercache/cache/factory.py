"""Cache backend factory - creates appropriate cache backend based on config."""

from __future__ import annotations

import logging
from typing import Any

from ..config import SemanticCacheConfig
from .redis import RedisSemanticCache
from .semantic import SemanticCache

logger = logging.getLogger(__name__)


async def get_cache_backend(
    config: SemanticCacheConfig,
    embedder: Any | None = None,
) -> SemanticCache | RedisSemanticCache:
    """Create a cache backend instance based on configuration.

    Args:
        config: Semantic cache configuration.
        embedder: Optional embedder instance for query embeddings.

    Returns:
        SemanticCache (SQLite) or RedisSemanticCache instance.

    Raises:
        RuntimeError: If Redis backend is configured but connection fails.
    """
    if config.backend == "redis":
        cache = RedisSemanticCache(
            redis_url=config.redis_url,
            default_ttl=config.default_ttl,
            similarity_threshold=config.similarity_threshold,
            embedder=embedder,
            pool_size=config.redis_pool_size,
            socket_timeout=config.redis_timeout,
        )
        try:
            await cache.initialize()
            logger.info("Using Redis cache backend at %s", config.redis_url)
            return cache
        except Exception as e:
            logger.warning(
                "Redis cache initialization failed (%s), falling back to SQLite at %s",
                e,
                config.db_path,
                exc_info=True,
            )
            # Fall back to SQLite
            cache = SemanticCache(
                db_path=config.db_path,
                default_ttl=config.default_ttl,
                similarity_threshold=config.similarity_threshold,
                embedder=embedder,
            )
            await cache.initialize()
            return cache
    else:
        # SQLite backend
        cache = SemanticCache(
            db_path=config.db_path,
            default_ttl=config.default_ttl,
            similarity_threshold=config.similarity_threshold,
            embedder=embedder,
        )
        await cache.initialize()
        logger.info("Using SQLite cache backend at %s", config.db_path)
        return cache

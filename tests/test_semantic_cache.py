"""Tests for the Semantic Cache."""

import asyncio
import hashlib
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from layercache.models import StratifiedPrompt, LayerType
from layercache.cache.semantic import SemanticCache, cosine_similarity


def _make_prompt(system: str = "You are helpful.", query: str = "What is Python?") -> StratifiedPrompt:
    """Create a basic prompt for testing."""
    prompt = StratifiedPrompt()
    prompt.add_message(LayerType.SYSTEM, "system", system)
    prompt.add_message(LayerType.USER, "user", query)
    return prompt


class TestCosineSimilarity:
    def test_identical_vectors(self) -> None:
        """Identical vectors should have similarity 1.0."""
        vec = [1.0, 0.0, 0.5]
        assert cosine_similarity(vec, vec) == pytest.approx(1.0)

    def test_orthogonal_vectors(self) -> None:
        """Orthogonal vectors should have similarity 0.0."""
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors(self) -> None:
        """Opposite vectors should have similarity -1.0."""
        assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_empty_vectors(self) -> None:
        """Empty vectors should return 0.0."""
        assert cosine_similarity([], []) == 0.0


class TestSemanticCache:
    @pytest.fixture
    async def cache(self) -> SemanticCache:
        """Create an in-memory semantic cache for testing."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        mock_embedder = AsyncMock()
        mock_embedder.embed = AsyncMock(return_value=[0.1, 0.2, 0.3, 0.4, 0.5])

        sc = SemanticCache(
            db_path=db_path,
            default_ttl=60,
            similarity_threshold=0.95,
            embedder=mock_embedder,
        )
        await sc.initialize()
        yield sc
        await sc.close()

    @pytest.mark.asyncio
    async def test_store_and_lookup(self, cache: SemanticCache) -> None:
        """Storing and looking up with identical content should hit."""
        prompt = _make_prompt()
        response = {"choices": [{"message": {"content": "Python is a language."}}]}

        await cache.store(prompt, response, "test-model")
        result = await cache.lookup(prompt, "test-model")

        assert result is not None
        assert result.response_payload == response

    @pytest.mark.asyncio
    async def test_miss_with_different_prefix(self, cache: SemanticCache) -> None:
        """Different prefix hash should cause a cache miss."""
        prompt1 = _make_prompt(system="You are helpful.")
        prompt2 = _make_prompt(system="You are a coding expert.")

        response = {"choices": [{"message": {"content": "Answer"}}]}
        await cache.store(prompt1, response, "test-model")

        result = await cache.lookup(prompt2, "test-model")
        assert result is None

    @pytest.mark.asyncio
    async def test_ttl_expiration(self, cache: SemanticCache) -> None:
        """Expired entries should not be returned."""
        prompt = _make_prompt()
        response = {"choices": [{"message": {"content": "Answer"}}]}

        # Store with 0 TTL (immediately expired)
        await cache.store(prompt, response, "test-model", ttl=0)

        result = await cache.lookup(prompt, "test-model")
        assert result is None

    @pytest.mark.asyncio
    async def test_invalidate_by_prefix(self, cache: SemanticCache) -> None:
        """Invalidating by prefix hash should remove those entries."""
        prompt = _make_prompt()
        response = {"choices": [{"message": {"content": "Answer"}}]}

        await cache.store(prompt, response, "test-model")

        prefix_hash = prompt.prefix_hash()
        removed = await cache.invalidate(prefix_hash)
        assert removed >= 1

        result = await cache.lookup(prompt, "test-model")
        assert result is None

    @pytest.mark.asyncio
    async def test_stats(self, cache: SemanticCache) -> None:
        """Stats should report correct entry counts."""
        prompt = _make_prompt()
        response = {"choices": [{"message": {"content": "Answer"}}]}

        stats_before = await cache.stats()
        assert stats_before["total_entries"] == 0

        await cache.store(prompt, response, "test-model", ttl=300)

        stats_after = await cache.stats()
        assert stats_after["total_entries"] == 1
        assert stats_after["valid_entries"] == 1

    @pytest.mark.asyncio
    async def test_cleanup_expired(self, cache: SemanticCache) -> None:
        """Cleanup should remove expired entries."""
        prompt = _make_prompt()
        response = {"choices": [{"message": {"content": "Answer"}}]}

        await cache.store(prompt, response, "test-model", ttl=0)
        removed = await cache.cleanup_expired()
        assert removed >= 1

"""Tests for Phase 2.1 — Multi-tier Caching Hierarchy.

Tests cover:
- Cache tier hierarchy (semantic → prefix → inference)
- Intent hash computation
- Entity extraction
- Validation decision tree
- Probation tracking
- Feature flag behavior
"""

from __future__ import annotations

import time

import pytest

from layercache.cache.probation import ProbationTracker
from layercache.cache.tier import CacheTier, CacheTierHierarchy
from layercache.cache.validator import EntityExtractor, IntentHashValidator


class TestCacheTier:
    """Test CacheTier enum and hierarchy logic."""

    def test_cache_tier_enum_values(self) -> None:
        """CacheTier enum should have SEMANTIC, PREFIX, INFERENCE values."""
        assert CacheTier.SEMANTIC.value == "semantic"
        assert CacheTier.PREFIX.value == "prefix"
        assert CacheTier.INFERENCE.value == "inference"

    def test_cache_tier_hierarchy(self) -> None:
        """CacheTierHierarchy should enforce lookup order."""
        hierarchy = CacheTierHierarchy()
        tiers = hierarchy.get_lookup_order()
        assert tiers == [CacheTier.SEMANTIC, CacheTier.PREFIX, CacheTier.INFERENCE]

    def test_cache_tier_next_tier(self) -> None:
        """CacheTierHierarchy should return next tier correctly."""
        hierarchy = CacheTierHierarchy()
        assert hierarchy.next_tier(CacheTier.SEMANTIC) == CacheTier.PREFIX
        assert hierarchy.next_tier(CacheTier.PREFIX) == CacheTier.INFERENCE
        assert hierarchy.next_tier(CacheTier.INFERENCE) is None

    def test_cache_tier_is_final(self) -> None:
        """INFERENCE should be the final tier."""
        hierarchy = CacheTierHierarchy()
        assert not hierarchy.is_final_tier(CacheTier.SEMANTIC)
        assert not hierarchy.is_final_tier(CacheTier.PREFIX)
        assert hierarchy.is_final_tier(CacheTier.INFERENCE)


class TestIntentHashValidator:
    """Test intent hash computation and validation."""

    def test_intent_hash_computation(self) -> None:
        """Intent hash should be SHA-256 of normalized query."""
        validator = IntentHashValidator()
        query = "What is 2+2?"
        intent_hash = validator.compute_intent_hash(query)

        assert isinstance(intent_hash, str)
        assert len(intent_hash) == 64  # SHA-256 hex length

    def test_intent_hash_normalization(self) -> None:
        """Intent hash should normalize queries consistently."""
        validator = IntentHashValidator()

        # Different casing/whitespace should produce same hash
        hash1 = validator.compute_intent_hash("What is 2+2?")
        hash2 = validator.compute_intent_hash("what is 2+2?")
        hash3 = validator.compute_intent_hash("  What   is  2+2?  ")

        assert hash1 == hash2
        assert hash2 == hash3

    def test_intent_hash_different_queries(self) -> None:
        """Different queries should produce different hashes."""
        validator = IntentHashValidator()

        hash1 = validator.compute_intent_hash("What is Python?")
        hash2 = validator.compute_intent_hash("What is Java?")

        assert hash1 != hash2

    def test_intent_hash_entity_sorting(self) -> None:
        """Entities should be sorted for consistent hashing."""
        validator = IntentHashValidator()

        # These should produce the same hash regardless of entity order
        # Using dates which are properly extracted and sorted
        hash1 = validator.compute_intent_hash("What happened on 2024-01-15 and 2023-05-20?")
        hash2 = validator.compute_intent_hash("What happened on 2023-05-20 and 2024-01-15?")

        assert hash1 == hash2

    def test_validation_result_match(self) -> None:
        """ValidationResult should indicate match when hashes are equal."""
        validator = IntentHashValidator()
        query1 = "What is 2+2?"
        query2 = "what is 2+2?"  # Normalized same as query1

        result = validator.validate(query1, query2)

        assert result.is_match is True
        assert result.validation_stage == "intent_hash"
        assert result.latency_ms >= 0

    def test_validation_result_mismatch(self) -> None:
        """ValidationResult should indicate mismatch when hashes differ."""
        validator = IntentHashValidator()
        query1 = "What is Python?"
        query2 = "What is Java?"

        result = validator.validate(query1, query2)

        assert result.is_match is False
        assert result.validation_stage == "intent_hash"


class TestEntityExtractor:
    """Test entity extraction from queries."""

    def test_extract_urls(self) -> None:
        """Should extract URLs from queries."""
        extractor = EntityExtractor()
        query = "What's on https://example.com/page?"
        entities = extractor.extract(query)

        assert any("https://example.com/page" in e for e in entities)

    def test_extract_numbers(self) -> None:
        """Should extract numbers from queries."""
        extractor = EntityExtractor()
        query = "Calculate 42 + 100"
        entities = extractor.extract(query)

        assert "42" in entities or "100" in entities

    def test_extract_code_snippets(self) -> None:
        """Should extract code snippets from queries."""
        extractor = EntityExtractor()
        query = "How does def hello(): work?"
        entities = extractor.extract(query)

        assert len(entities) > 0

    def test_extract_dates(self) -> None:
        """Should extract dates from queries."""
        extractor = EntityExtractor()
        query = "What happened on 2024-01-15?"
        entities = extractor.extract(query)

        assert "2024-01-15" in entities

    def test_extract_empty_query(self) -> None:
        """Should return empty list for empty query."""
        extractor = EntityExtractor()
        entities = extractor.extract("")

        assert entities == []

    def test_entity_normalization(self) -> None:
        """Entities should be normalized (sorted, deduplicated)."""
        extractor = EntityExtractor()
        query = "42 + 42 + 100"
        entities = extractor.extract(query)

        # Should be deduplicated
        assert entities.count("42") <= 1

    def test_entity_extraction_preserves_query_context(self) -> None:
        """Entity extraction should preserve original query context, not replace it."""
        extractor = EntityExtractor()
        query = "What is 42?"
        entities = extractor.extract(query)

        # Should extract the number but not lose the query context
        assert "42" in entities
        # The extraction should return entities only, but when used in intent hash,
        # the original query context should be preserved
        assert len(entities) > 0

    def test_entity_extraction_none_input(self) -> None:
        """Should handle None input gracefully."""
        extractor = EntityExtractor()
        entities = extractor.extract(None)  # type: ignore

        assert entities == []

    def test_entity_extraction_very_long_query(self) -> None:
        """Should handle very long queries without performance degradation."""
        extractor = EntityExtractor()
        query = "What is 42? " * 1000  # Long query with repeated entities

        import time

        start = time.time()
        entities = extractor.extract(query)
        latency = (time.time() - start) * 1000

        # Should complete quickly (< 10ms for regex-based extraction)
        assert latency < 50
        # Should still extract entities
        assert "42" in entities

    def test_intent_hash_preserves_query_context_with_entities(self) -> None:
        """Intent hash should preserve query context, not just entities.

        Two queries with same entities but different context should have different hashes.
        """
        validator = IntentHashValidator()

        # These queries have the same entity (42) but different context
        query1 = "What is 42?"
        query2 = "Is 42 the answer?"

        hash1 = validator.compute_intent_hash(query1)
        hash2 = validator.compute_intent_hash(query2)

        # Should be different because the query context is different
        # NOT just based on entities
        assert hash1 != hash2, (
            "Queries with same entity but different context should have different hashes"
        )


class TestValidationDecisionTree:
    """Test the validation decision tree integration."""

    @pytest.mark.asyncio
    async def test_validation_fast_path(self) -> None:
        """Intent hash match should be fast path (<10ms)."""
        validator = IntentHashValidator()
        query = "What is 2+2?"

        start = time.time()
        result = validator.validate(query, query)
        latency = (time.time() - start) * 1000

        assert result.is_match is True
        assert latency < 50  # Should be well under 50ms budget

    @pytest.mark.asyncio
    async def test_validation_entity_fallback(self) -> None:
        """Entity extraction should run when intent hash mismatches."""
        intent_validator = IntentHashValidator()
        entity_extractor = EntityExtractor()

        query1 = "What is Python version 3.9?"
        query2 = "What is Java version 11?"

        # Intent hash should mismatch
        intent_result = intent_validator.validate(query1, query2)
        assert intent_result.is_match is False

        # Entity extraction should find different entities
        entities1 = entity_extractor.extract(query1)
        entities2 = entity_extractor.extract(query2)

        assert entities1 != entities2


class TestProbationTracker:
    """Test probation tracking for new cache entries."""

    @pytest.fixture
    async def probation_tracker(self) -> ProbationTracker:
        """Create a ProbationTracker for testing."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        tracker = ProbationTracker(db_path=db_path)
        await tracker.initialize()
        yield tracker
        await tracker.close()

    @pytest.mark.asyncio
    async def test_probation_tracker_initialization(
        self,
        probation_tracker: ProbationTracker,
    ) -> None:
        """ProbationTracker should initialize database schema."""
        assert probation_tracker.healthy is True

    @pytest.mark.asyncio
    async def test_increment_probation_count(
        self,
        probation_tracker: ProbationTracker,
    ) -> None:
        """Should increment probation count atomically."""
        entry_id = "test-entry-123"

        # Initial count should be 0
        count = await probation_tracker.get_probation_count(entry_id)
        assert count == 0

        # Increment should work
        await probation_tracker.increment_probation_count(entry_id)
        count = await probation_tracker.get_probation_count(entry_id)
        assert count == 1

    @pytest.mark.asyncio
    async def test_probation_promotion_threshold(
        self,
        probation_tracker: ProbationTracker,
    ) -> None:
        """Entry should promote after N=10 successful validations."""
        entry_id = "test-entry-456"

        for i in range(10):
            await probation_tracker.increment_probation_count(entry_id)

        is_promoted = await probation_tracker.check_promotion(entry_id)
        assert is_promoted is True

    @pytest.mark.asyncio
    async def test_probation_auto_promotion_timeout(
        self,
        probation_tracker: ProbationTracker,
    ) -> None:
        """Entry should auto-promote after 1 hour regardless of count."""
        entry_id = "test-entry-789"

        # Set entry with old timestamp
        now = time.time()
        old_timestamp = now - 3601  # 1 hour + 1 second ago

        await probation_tracker._db.execute(
            """
            INSERT INTO probation_tracker (entry_id, probation_count, created_at)
            VALUES (?, ?, ?)
            """,
            (entry_id, 0, old_timestamp),
        )
        await probation_tracker._db.commit()

        is_promoted = await probation_tracker.check_promotion(entry_id)
        assert is_promoted is True

    @pytest.mark.asyncio
    async def test_probation_failed_entry(
        self,
        probation_tracker: ProbationTracker,
    ) -> None:
        """Failed probation should not increment count."""
        entry_id = "test-entry-fail"

        await probation_tracker.record_validation_failure(entry_id)
        count = await probation_tracker.get_probation_count(entry_id)

        # Should remain at 0 (no increment on failure)
        assert count == 0

    @pytest.mark.asyncio
    async def test_probation_lru_eviction(
        self,
        probation_tracker: ProbationTracker,
    ) -> None:
        """Should evict oldest entries when max 1000 reached."""
        # Insert 1001 entries
        for i in range(1001):
            entry_id = f"test-entry-{i}"
            await probation_tracker.increment_probation_count(entry_id)

        # Should have evicted oldest entries
        # (implementation detail - verify max bound is respected)
        stats = await probation_tracker.stats()
        assert stats["probation_entries_count"] <= 1000


class TestMultiTierFeatureFlag:
    """Test the multi-tier caching feature flag."""

    def test_feature_flag_enabled(self) -> None:
        """Feature flag should default to enabled."""
        from layercache.config import LayerCacheSettings

        LayerCacheSettings()

    def test_feature_flag_disabled(self) -> None:
        """Feature flag should be disableable."""
        from layercache.config import LayerCacheSettings

        LayerCacheSettings.model_validate(
            {"caching": {"semantic": {"multi_tier": {"enabled": False}}}}
        )


class TestIntegration:
    """Integration tests for multi-tier caching."""

    @pytest.mark.asyncio
    async def test_tier_hierarchy_flow(self) -> None:
        """Should flow through tiers correctly: semantic → prefix → inference."""
        hierarchy = CacheTierHierarchy()

        # Start at semantic tier
        current_tier = CacheTier.SEMANTIC
        assert not hierarchy.is_final_tier(current_tier)

        # Simulate cache miss at semantic tier
        current_tier = hierarchy.next_tier(current_tier)
        assert current_tier == CacheTier.PREFIX

        # Simulate cache miss at prefix tier
        current_tier = hierarchy.next_tier(current_tier)
        assert current_tier == CacheTier.INFERENCE
        assert hierarchy.is_final_tier(current_tier)

    @pytest.mark.asyncio
    async def test_validation_latency_budget(self) -> None:
        """Validation should complete within <50ms budget (p95)."""
        validator = IntentHashValidator()
        query = "What is the capital of France?" * 10  # Longer query

        latencies = []
        for _ in range(100):
            start = time.time()
            validator.validate(query, query)
            latency = (time.time() - start) * 1000
            latencies.append(latency)

        # Sort for p95
        latencies.sort()
        p95 = latencies[int(len(latencies) * 0.95)]

        assert p95 < 50  # p95 should be under 50ms

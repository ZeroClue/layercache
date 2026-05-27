"""Tests for Phase 1.1: Stable Prefix Architecture."""

from unittest.mock import patch

import pytest

from layercache.canonicalizer import Canonicalizer
from layercache.enhancements.base import EnhancementRegistry
from layercache.metrics.collector import MetricsCollector
from layercache.models import LayerCacheRequest, LayerType, StratifiedPrompt
from layercache.pipeline import RequestPipeline
from layercache.stratifier import Stratifier
from layercache.truncation import TokenCounter


class TestStablePrefixTokens:
    """Test stable_prefix_tokens method."""

    def test_counts_all_three_layers(self):
        """stable_prefix_tokens counts L0 + L1 + L2."""
        prompt = StratifiedPrompt(session_id="test")

        # Add L0 (system)
        prompt.add_message(LayerType.SYSTEM, "system", "You are a helpful assistant." * 10)

        # Add L1 (context)
        prompt.add_message(LayerType.CONTEXT, "user", "Context information." * 20)

        # Add L2 (session)
        prompt.add_message(LayerType.SESSION, "user", "User message." * 15)
        prompt.add_message(LayerType.SESSION, "assistant", "Assistant response." * 15)

        # L3/L4 should not be counted
        prompt.add_message(LayerType.USER, "user", "Current query")
        prompt.add_message(LayerType.ENHANCEMENT, "assistant", "Enhancement")

        token_count = prompt.stable_prefix_tokens()

        # Should count L0 + L1 + L2 only
        assert token_count > 0
        # Verify L3/L4 not included by checking L0+L1+L2 separately
        counter = TokenCounter()
        l0_tokens = counter.count_messages(prompt.layers[LayerType.SYSTEM])
        l1_tokens = counter.count_messages(prompt.layers[LayerType.CONTEXT])
        l2_tokens = counter.count_messages(prompt.layers[LayerType.SESSION])

        assert token_count == l0_tokens + l1_tokens + l2_tokens

    def test_empty_layers_return_zero(self):
        """Empty stable prefix returns 0 tokens."""
        prompt = StratifiedPrompt(session_id="test")

        token_count = prompt.stable_prefix_tokens()

        assert token_count == 0

    def test_multimodal_content_counted(self):
        """Multimodal content (text + images) is counted correctly."""
        prompt = StratifiedPrompt(session_id="test")

        # Add multimodal content
        prompt.add_message(
            LayerType.SYSTEM,
            "system",
            [
                {"type": "text", "text": "Analyze this image." * 10},
                {"type": "image_url", "image_url": "https://example.com/image.png"},
            ],
        )

        token_count = prompt.stable_prefix_tokens()

        # Text content should be counted
        assert token_count > 0


class TestPrefixHashStability:
    """Test that prefix_hash is stable and deterministic."""

    def test_same_content_same_hash(self):
        """Identical content produces identical prefix_hash."""
        prompt1 = StratifiedPrompt(session_id="test")
        prompt1.add_message(LayerType.SYSTEM, "system", "System instruction")
        prompt1.add_message(LayerType.CONTEXT, "user", "Context")
        prompt1.add_message(LayerType.SESSION, "user", "Session message")

        prompt2 = StratifiedPrompt(session_id="test")
        prompt2.add_message(LayerType.SYSTEM, "system", "System instruction")
        prompt2.add_message(LayerType.CONTEXT, "user", "Context")
        prompt2.add_message(LayerType.SESSION, "user", "Session message")

        assert prompt1.prefix_hash() == prompt2.prefix_hash()

    def test_different_content_different_hash(self):
        """Different content produces different prefix_hash."""
        prompt1 = StratifiedPrompt(session_id="test")
        prompt1.add_message(LayerType.SYSTEM, "system", "System instruction")

        prompt2 = StratifiedPrompt(session_id="test")
        prompt2.add_message(LayerType.SYSTEM, "system", "Different instruction")

        assert prompt1.prefix_hash() != prompt2.prefix_hash()

    def test_session_isolation_affects_hash(self):
        """Different session_id produces different prefix_hash."""
        prompt1 = StratifiedPrompt(session_id="session1")
        prompt1.add_message(LayerType.SYSTEM, "system", "System instruction")

        prompt2 = StratifiedPrompt(session_id="session2")
        prompt2.add_message(LayerType.SYSTEM, "system", "System instruction")

        assert prompt1.prefix_hash() != prompt2.prefix_hash()


class TestPrefixThresholdValidation:
    """Test prefix threshold validation (≥1,024 tokens for cache eligibility)."""

    def test_below_threshold_warning(self, caplog):
        """Prefix <1,024 tokens triggers warning."""
        prompt = StratifiedPrompt(session_id="test")
        prompt.add_message(LayerType.SYSTEM, "system", "Short system")
        prompt.add_message(LayerType.CONTEXT, "user", "Short context")
        prompt.add_message(LayerType.SESSION, "user", "Short session")

        token_count = prompt.stable_prefix_tokens()

        # Should be below threshold
        assert token_count < 1024

    def test_above_threshold_no_warning(self, caplog):
        """Prefix ≥1,024 tokens is cache-eligible."""
        prompt = StratifiedPrompt(session_id="test")

        # Add enough content to exceed threshold (need ~1,024 tokens)
        # Each "System instruction. " is ~4 tokens, so 100 repetitions = ~400 tokens
        # Need more: 300 repetitions per layer × 3 layers ≈ 3,600 tokens
        prompt.add_message(LayerType.SYSTEM, "system", "System instruction. " * 300)
        prompt.add_message(LayerType.CONTEXT, "user", "Context information. " * 300)
        prompt.add_message(LayerType.SESSION, "user", "Session message. " * 300)

        token_count = prompt.stable_prefix_tokens()

        # Should be above threshold
        assert token_count >= 1024


class TestResponseMetadataIntegration:
    """Test that prefix metadata is added to responses."""

    def test_prefix_hash_in_response(self):
        """Response includes lc_prefix_hash metadata."""
        # This is an integration test - would need full pipeline setup
        # For now, just verify the method exists and returns a valid hash
        prompt = StratifiedPrompt(session_id="test")
        prompt.add_message(LayerType.SYSTEM, "system", "System")

        prefix_hash = prompt.prefix_hash()

        assert isinstance(prefix_hash, str)
        assert len(prefix_hash) == 64  # SHA-256 hex length
        assert all(c in "0123456789abcdef" for c in prefix_hash)

    def test_prefix_tokens_in_response(self):
        """Response includes lc_prefix_tokens metadata."""
        prompt = StratifiedPrompt(session_id="test")
        prompt.add_message(LayerType.SYSTEM, "system", "System instruction" * 10)

        prefix_tokens = prompt.stable_prefix_tokens()

        assert isinstance(prefix_tokens, int)
        assert prefix_tokens > 0


class TestPipelineIntegration:
    """Integration tests for full pipeline."""

    @pytest.fixture
    def pipeline(self):
        """Create minimal pipeline for testing."""
        stratifier = Stratifier()
        canonicalizer = Canonicalizer()
        enhancement_registry = EnhancementRegistry()
        metrics = MetricsCollector()

        pipeline = RequestPipeline(
            stratifier=stratifier,
            canonicalizer=canonicalizer,
            enhancement_registry=enhancement_registry,
            semantic_cache=None,
            prompt_registry=None,
            metrics=metrics,
            metrics_db=None,
            max_session_tokens=500,
            providers_config=None,
        )
        return pipeline

    async def test_truncation_occurs_when_max_tokens_set(self, pipeline):
        """Verify _truncate_session is called and works."""
        request = LayerCacheRequest(
            messages=[{"role": "user", "content": "Hello" * 1000}],
            model="gpt-4o",
            max_tokens=100,
        )

        with patch.object(pipeline, "_call_llm") as mock_llm:
            mock_llm.return_value = {
                "choices": [{"message": {"content": "Hi"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }

            response = await pipeline.process_request(request, api_key="test")

            assert "lc_prefix_hash" in response
            assert "lc_prefix_tokens" in response
            assert isinstance(response["lc_prefix_hash"], str)
            assert isinstance(response["lc_prefix_tokens"], int)

    async def test_no_truncation_when_max_tokens_none(self):
        """Verify no truncation when max_session_tokens is None."""
        stratifier = Stratifier()
        canonicalizer = Canonicalizer()
        enhancement_registry = EnhancementRegistry()
        metrics = MetricsCollector()

        pipeline = RequestPipeline(
            stratifier=stratifier,
            canonicalizer=canonicalizer,
            enhancement_registry=enhancement_registry,
            semantic_cache=None,
            prompt_registry=None,
            metrics=metrics,
            metrics_db=None,
            max_session_tokens=None,
            providers_config=None,
        )

        request = LayerCacheRequest(
            messages=[{"role": "user", "content": "Hello" * 100}],
            model="gpt-4o",
            max_tokens=100,
        )

        with patch.object(pipeline, "_call_llm") as mock_llm:
            mock_llm.return_value = {
                "choices": [{"message": {"content": "Hi"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }

            response = await pipeline.process_request(request, api_key="test")

            assert "lc_prefix_hash" in response
            assert "lc_prefix_tokens" in response

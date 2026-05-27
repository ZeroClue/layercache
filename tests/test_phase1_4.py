"""Tests for Phase 1.4: OpenAI Cache Metrics Extraction.

Verifies that OpenAI response cache metrics are correctly extracted
and integrated with the metrics collector for tracking cache hit rates
and token savings.
"""

from layercache.adapters.openai import OpenAIAdapter
from layercache.metrics.collector import MetricsCollector


class TestOpenAICacheMetricsExtraction:
    """Test cache metrics extraction from OpenAI responses."""

    def test_cached_tokens_extracted_from_response(self) -> None:
        """Should extract cached_tokens from OpenAI response usage object.

        OpenAI returns cache metrics in prompt_tokens_details.cached_tokens
        when prefix caching is active.
        """
        adapter = OpenAIAdapter()
        response = {
            "id": "chatcmpl-123",
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 500,
                "total_tokens": 1500,
                "prompt_tokens_details": {
                    "cached_tokens": 600,
                },
            },
        }

        metrics = adapter.extract_cache_metrics(response)

        assert metrics["cache_read_input_tokens"] == 600
        assert metrics["input_tokens"] == 1000
        assert metrics["output_tokens"] == 500

    def test_missing_cached_tokens_defaults_to_zero(self) -> None:
        """Should default to 0 when cached_tokens is not present.

        Not all OpenAI responses include cached_tokens (e.g., cache miss
        or first request). The adapter should handle this gracefully.
        """
        adapter = OpenAIAdapter()
        response = {
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 500,
                "total_tokens": 1500,
            },
        }

        metrics = adapter.extract_cache_metrics(response)

        assert metrics["cache_read_input_tokens"] == 0
        assert metrics["input_tokens"] == 1000
        assert metrics["output_tokens"] == 500

    def test_cache_hit_rate_calculated_correctly(self) -> None:
        """Cache hit rate should be calculated as cached_tokens / total_prompt_tokens.

        For a response with 1000 prompt tokens and 600 cached tokens,
        the hit rate should be 0.6 (60%).
        """
        adapter = OpenAIAdapter()
        response = {
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 500,
                "prompt_tokens_details": {
                    "cached_tokens": 600,
                },
            },
        }

        metrics = adapter.extract_cache_metrics(response)

        # Calculate hit rate
        hit_rate = metrics["cache_read_input_tokens"] / metrics["input_tokens"]

        assert hit_rate == 0.6
        assert metrics["cache_read_input_tokens"] == 600
        assert metrics["input_tokens"] == 1000

    def test_metrics_recorded_with_cache_data(self) -> None:
        """MetricsCollector should record cache_read_input_tokens from OpenAI responses.

        The collector should track cached tokens separately to enable
        cost savings calculations (50% discount on cached tokens).
        """
        collector = MetricsCollector()
        adapter = OpenAIAdapter()

        response = {
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 500,
                "prompt_tokens_details": {
                    "cached_tokens": 600,
                },
            },
        }

        metrics = adapter.extract_cache_metrics(response)

        # Record the metrics
        collector.record_request(
            model="gpt-4o",
            input_tokens=metrics["input_tokens"],
            output_tokens=metrics["output_tokens"],
            cache_read_tokens=metrics["cache_read_input_tokens"],
            cache_creation_tokens=metrics.get("cache_creation_input_tokens", 0),
            duration_seconds=1.5,
        )

        # Verify metrics were recorded
        aggregated = collector.get_metrics()

        assert aggregated["total_cache_read_tokens"] == 600
        assert aggregated["total_input_tokens"] == 1000
        assert aggregated["total_output_tokens"] == 500

        # Verify cache hit rate is calculated
        assert aggregated["provider_token_cache_hit_rate"] == 0.6

        # Verify per-model metrics
        assert "gpt-4o" in aggregated["by_model"]
        model_metrics = aggregated["by_model"]["gpt-4o"]
        assert model_metrics["cache_read_tokens"] == 600
        assert model_metrics["input_tokens"] == 1000

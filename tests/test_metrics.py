"""Tests for the Metrics Collector."""

import pytest

from layercache.metrics.collector import MetricsCollector, RequestTimer


class TestMetricsCollector:
    def test_record_request(self) -> None:
        """Recording a request should update all counters."""
        metrics = MetricsCollector()
        metrics.record_request(
            model="claude-3-5-sonnet-20241022",
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=600,
            duration_seconds=0.5,
        )

        m = metrics.get_metrics()
        assert m["llm_requests_total"] == 1
        assert m["total_input_tokens"] == 1000
        assert m["total_output_tokens"] == 500
        assert m["total_cache_read_tokens"] == 600
        assert m["estimated_tokens_saved"] == 600

    def test_semantic_cache_tracking(self) -> None:
        """Semantic cache hits/misses should be tracked."""
        metrics = MetricsCollector()
        metrics.record_semantic_cache_hit()
        metrics.record_semantic_cache_hit()
        metrics.record_semantic_cache_miss()

        m = metrics.get_metrics()
        assert m["semantic_cache_hits_total"] == 2
        assert m["semantic_cache_misses_total"] == 1
        assert m["semantic_cache_hit_rate"] == pytest.approx(2 / 3, rel=1e-4)

    def test_provider_cache_hit_rate(self) -> None:
        """Provider token cache hit rate should be calculated correctly."""
        metrics = MetricsCollector()
        metrics.record_request(
            model="claude-3-5-sonnet",
            input_tokens=1000,
            cache_read_tokens=650,
        )

        m = metrics.get_metrics()
        assert m["provider_token_cache_hit_rate"] == pytest.approx(0.65)

    def test_cost_estimation(self) -> None:
        """Cost savings should be estimated based on model pricing."""
        metrics = MetricsCollector()
        metrics.record_request(
            model="claude-3-5-sonnet-20241022",
            input_tokens=1_000_000,
            output_tokens=500_000,
            cache_read_tokens=800_000,
        )

        m = metrics.get_metrics()
        # Claude 3.5 Sonnet: input=$3/M, cache_read=$0.30/M
        # Savings = 800K * ($3 - $0.30) / 1M = $2.16
        assert m["estimated_cost_saved_usd"] > 0

    def test_per_model_metrics(self) -> None:
        """Metrics should be tracked per model."""
        metrics = MetricsCollector()
        metrics.record_request(model="claude-3-5-sonnet", input_tokens=1000, cache_read_tokens=600)
        metrics.record_request(model="gpt-4o", input_tokens=2000, cache_read_tokens=400)
        metrics.record_request(model="claude-3-5-sonnet", input_tokens=500, cache_read_tokens=300)

        m = metrics.get_metrics()
        assert m["llm_requests_total"] == 3
        assert "claude-3-5-sonnet" in m["by_model"]
        assert "gpt-4o" in m["by_model"]
        assert m["by_model"]["claude-3-5-sonnet"]["requests"] == 2
        assert m["by_model"]["gpt-4o"]["requests"] == 1

    def test_prometheus_output(self) -> None:
        """Prometheus metrics output should be valid format."""
        metrics = MetricsCollector()
        metrics.record_request(model="test-model", input_tokens=1000)
        metrics.record_semantic_cache_hit()

        output = metrics.get_prometheus_metrics()
        assert "lc_llm_requests_total 1" in output
        assert "lc_semantic_cache_hits_total 1" in output
        assert "# HELP lc_llm_requests_total" in output
        assert "# TYPE lc_llm_requests_total counter" in output


class TestRequestTimer:
    def test_timer_measures_duration(self) -> None:
        """Timer should measure elapsed time."""
        import time

        timer = RequestTimer()
        with timer:
            time.sleep(0.05)

        assert timer.duration >= 0.05
        assert timer.duration < 1.0  # Sanity check

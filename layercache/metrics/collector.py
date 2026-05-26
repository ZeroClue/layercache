"""Metrics collection and cache ROI calculation.

Provides Prometheus-compatible metrics and a cache ROI calculator
that estimates cost savings from token caching.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# Model pricing per 1M tokens (input) as of late 2024
# Used for ROI estimation
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-3-5-sonnet-20241022": {"input": 3.0, "output": 15.0, "cache_read": 0.30},
    "claude-3-5-haiku-20241022": {"input": 0.80, "output": 4.0, "cache_read": 0.08},
    "claude-3-opus-20240229": {"input": 15.0, "output": 75.0, "cache_read": 1.50},
    "gpt-4o": {"input": 2.50, "output": 10.0, "cache_read": 1.25},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60, "cache_read": 0.075},
    "gpt-4-turbo": {"input": 10.0, "output": 30.0, "cache_read": 5.0},
    "gemini-1.5-pro": {"input": 1.25, "output": 5.0, "cache_read": 0.3125},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30, "cache_read": 0.01875},
}


class MetricsCollector:
    """Collects and aggregates cache performance metrics.

    Tracks:
    - Request counts (total, by model, by provider)
    - Token usage (input, output, cached)
    - Latency (request duration)
    - Semantic cache hits/misses
    """

    def __init__(self) -> None:
        # Counters
        self._llm_requests_total: int = 0
        self._semantic_cache_hits_total: int = 0
        self._semantic_cache_misses_total: int = 0

        # Token accumulators
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        self._total_cache_read_tokens: int = 0
        self._total_cache_creation_tokens: int = 0
        self._total_tokens_saved: int = 0

        # Per-model metrics
        self._model_requests: dict[str, int] = {}
        self._model_input_tokens: dict[str, int] = {}
        self._model_output_tokens: dict[str, int] = {}
        self._model_cache_read_tokens: dict[str, int] = {}

        # Latency tracking
        self._request_latencies: list[float] = []
        self._max_latency_samples = 10000

        # Cost tracking
        self._total_cost_saved_usd: float = 0.0
        self._total_cost_usd: float = 0.0

        # Concurrency guard — protects all mutable state above
        self._lock = threading.Lock()

    def record_request(
        self,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        duration_seconds: float = 0,
    ) -> None:
        """Record metrics for a single LLM request."""
        with self._lock:
            self._llm_requests_total += 1
            self._total_input_tokens += input_tokens
            self._total_output_tokens += output_tokens
            self._total_cache_read_tokens += cache_read_tokens
            self._total_cache_creation_tokens += cache_creation_tokens

            self._model_requests[model] = self._model_requests.get(model, 0) + 1
            self._model_input_tokens[model] = (
                self._model_input_tokens.get(model, 0) + input_tokens
            )
            self._model_output_tokens[model] = (
                self._model_output_tokens.get(model, 0) + output_tokens
            )
            self._model_cache_read_tokens[model] = (
                self._model_cache_read_tokens.get(model, 0) + cache_read_tokens
            )

            tokens_saved = cache_read_tokens
            self._total_tokens_saved += tokens_saved

            pricing = self._get_pricing(model)
            cost_saved = (
                (cache_read_tokens / 1_000_000)
                * (pricing["input"] - pricing["cache_read"])
            )
            cost_cached = (cache_read_tokens / 1_000_000) * pricing["cache_read"] + (
                (input_tokens - cache_read_tokens) / 1_000_000
            ) * pricing["input"]
            self._total_cost_saved_usd += cost_saved
            self._total_cost_usd += cost_cached + (output_tokens / 1_000_000) * pricing["output"]

            if duration_seconds > 0:
                self._request_latencies.append(duration_seconds)
                if len(self._request_latencies) > self._max_latency_samples:
                    self._request_latencies = self._request_latencies[-self._max_latency_samples :]

    def record_semantic_cache_hit(self) -> None:
        """Record a semantic cache hit (no LLM call needed)."""
        with self._lock:
            self._semantic_cache_hits_total += 1

    def record_semantic_cache_miss(self) -> None:
        """Record a semantic cache miss."""
        with self._lock:
            self._semantic_cache_misses_total += 1

    def get_metrics(self) -> dict[str, Any]:
        """Get aggregated metrics for the dashboard API."""
        with self._lock:
            total_requests = self._llm_requests_total
            hits = self._semantic_cache_hits_total
            misses = self._semantic_cache_misses_total
            total_input = self._total_input_tokens
            total_output = self._total_output_tokens
            total_cache_read = self._total_cache_read_tokens
            total_cache_creation = self._total_cache_creation_tokens
            total_saved = self._total_tokens_saved
            cost_saved = self._total_cost_saved_usd
            cost_total = self._total_cost_usd
            latencies = list(self._request_latencies)
            model_requests = dict(self._model_requests)
            model_inputs = dict(self._model_input_tokens)
            model_cached = dict(self._model_cache_read_tokens)

        semantic_total = hits + misses

        provider_hit_rate = (
            total_cache_read / total_input if total_input > 0 else 0.0
        )

        semantic_hit_rate = (
            hits / semantic_total if semantic_total > 0 else 0.0
        )

        avg_latency = (
            sum(latencies) / len(latencies) if latencies else 0.0
        )

        p95_latency = (
            self._percentile(latencies, 95) if latencies else 0.0
        )

        by_model: dict[str, dict[str, float]] = {}
        for model in model_requests:
            model_input = model_inputs.get(model, 0)
            model_cached_val = model_cached.get(model, 0)
            by_model[model] = {
                "requests": model_requests[model],
                "provider_token_cache_hit_rate": (
                    model_cached_val / model_input if model_input > 0 else 0.0
                ),
                "input_tokens": model_input,
                "cache_read_tokens": model_cached_val,
            }

        return {
            "llm_requests_total": total_requests,
            "semantic_cache_hits_total": hits,
            "semantic_cache_misses_total": misses,
            "provider_token_cache_hit_rate": round(provider_hit_rate, 4),
            "semantic_cache_hit_rate": round(semantic_hit_rate, 4),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cache_read_tokens": total_cache_read,
            "total_cache_creation_tokens": total_cache_creation,
            "estimated_tokens_saved": total_saved,
            "estimated_cost_saved_usd": round(cost_saved, 4),
            "estimated_total_cost_usd": round(cost_total, 4),
            "avg_request_duration_seconds": round(avg_latency, 4),
            "p95_request_duration_seconds": round(p95_latency, 4),
            "by_model": by_model,
        }

    def get_prometheus_metrics(self) -> str:
        """Generate Prometheus-compatible metrics output."""
        m = self.get_metrics()
        lines = [
            "# HELP lc_llm_requests_total Total number of LLM requests proxied",
            "# TYPE lc_llm_requests_total counter",
            f"lc_llm_requests_total {m['llm_requests_total']}",
            "",
            "# HELP lc_semantic_cache_hits_total Total semantic cache hits",
            "# TYPE lc_semantic_cache_hits_total counter",
            f"lc_semantic_cache_hits_total {m['semantic_cache_hits_total']}",
            "",
            "# HELP lc_semantic_cache_misses_total Total semantic cache misses",
            "# TYPE lc_semantic_cache_misses_total counter",
            f"lc_semantic_cache_misses_total {m['semantic_cache_misses_total']}",
            "",
            "# HELP lc_tokens_saved_total Total tokens saved from caching",
            "# TYPE lc_tokens_saved_total counter",
            f"lc_tokens_saved_total {m['estimated_tokens_saved']}",
            "",
            "# HELP lc_cache_read_tokens_total Total tokens read from provider cache",
            "# TYPE lc_cache_read_tokens_total counter",
            f"lc_cache_read_tokens_total {m['total_cache_read_tokens']}",
            "",
            "# HELP lc_input_tokens_total Total input tokens",
            "# TYPE lc_input_tokens_total counter",
            f"lc_input_tokens_total {m['total_input_tokens']}",
            "",
            "# HELP lc_output_tokens_total Total output tokens",
            "# TYPE lc_output_tokens_total counter",
            f"lc_output_tokens_total {m['total_output_tokens']}",
            "",
            "# HELP lc_cost_saved_usd Total cost saved from caching",
            "# TYPE lc_cost_saved_usd counter",
            f"lc_cost_saved_usd {m['estimated_cost_saved_usd']}",
            "",
            "# HELP lc_request_duration_seconds Request duration in seconds",
            "# TYPE lc_request_duration_seconds summary",
            f"lc_request_duration_seconds_avg {m['avg_request_duration_seconds']}",
            f"lc_request_duration_seconds_p95 {m['p95_request_duration_seconds']}",
        ]

        return "\n".join(lines) + "\n"

    @staticmethod
    def _get_pricing(model: str) -> dict[str, float]:
        """Get pricing for a model, with fuzzy matching."""
        model_lower = (
            model.lower().replace("anthropic/", "").replace("openai/", "").replace("gemini/", "")
        )

        if model_lower in MODEL_PRICING:
            return MODEL_PRICING[model_lower]

        # Fuzzy match — check longest keys first so specific substrings win
        for key in sorted(MODEL_PRICING, key=len, reverse=True):
            if key in model_lower or model_lower in key:
                return MODEL_PRICING[key]

        return {"input": 3.0, "output": 15.0, "cache_read": 0.30}

    @staticmethod
    def _percentile(data: list[float], percentile: float) -> float:
        """Calculate percentile from a list of values."""
        if not data:
            return 0.0
        sorted_data = sorted(data)
        index = int(len(sorted_data) * percentile / 100)
        index = min(index, len(sorted_data) - 1)
        return sorted_data[index]


class RequestTimer:
    """Context manager for timing request duration."""

    def __init__(self) -> None:
        self.start_time: float = 0.0
        self.duration: float = 0.0

    def __enter__(self) -> RequestTimer:
        self.start_time = time.time()
        return self

    def __exit__(self, *args: Any) -> None:
        self.duration = time.time() - self.start_time

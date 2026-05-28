"""Request Pipeline - Orchestrates the full request processing flow.

The pipeline processes each incoming request through these stages:
1. Semantic Cache Lookup (bypass LLM if similar query cached)
2. Stratification (L0-L4 classification)
3. Canonicalization (normalization for cache-friendly output)
4. Enhancement Injection (L3 modifications)
5. Cache Marker Injection (provider-specific)
6. Provider Routing (via LiteLLM)
7. Response Handling (metrics, cache storage)
8. Background cache creation (provider-specific, e.g. Gemini CachedContent)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from typing import Any

from .adapters import detect_provider, get_adapter
from .cache.probation import ProbationTracker
from .cache.semantic import SemanticCache
from .cache.tier import CacheTierHierarchy
from .cache.validator import IntentHashValidator
from .canonicalizer import Canonicalizer
from .config import ProvidersConfig
from .enhancements.base import EnhancementRegistry
from .metrics.collector import MetricsCollector, RequestTimer
from .metrics.storage import MetricsDB
from .models import LayerCacheRequest, StratifiedPrompt
from .registry.prompt_registry import PromptRegistry
from .stratifier import Stratifier
from .truncation import TokenCounter, TruncationStrategy, Truncator

logger = logging.getLogger(__name__)

# Model names must match known provider prefixes to block SSRF via LiteLLM
_ALLOWED_MODEL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.:-]*(/[a-zA-Z][a-zA-Z0-9_.:-]+)?$")

_ANTHROPIC_STOP_REASON_MAP: dict[str, str] = {
    "end_turn": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
}


def _anthropic_stop_reason(reason: str) -> str:
    """Map Anthropic stop_reason to OpenAI finish_reason."""
    return _ANTHROPIC_STOP_REASON_MAP.get(reason, reason)


def _anthropic_auth_headers(api_key: str) -> dict[str, str]:
    """Build auth headers for Anthropic API.

    Uses LiteLLM's own OAuth handling for Pro/Max tokens (sk-ant-oat-*).
    For regular API keys (sk-ant-api* or others), uses x-api-key.
    """
    from litellm.llms.anthropic.common_utils import optionally_handle_anthropic_oauth

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    if api_key.startswith("sk-ant-api"):
        headers["x-api-key"] = api_key
    else:
        headers, api_key = optionally_handle_anthropic_oauth(headers, api_key)
        if "authorization" not in headers and "x-api-key" not in headers:
            headers["x-api-key"] = api_key
    return headers


def validate_model_name(model: str) -> None:
    """Reject model names that look like URLs, IPs, or paths (SSRF guard).

    Raises ValueError on invalid input.
    """
    if not model:
        raise ValueError("model is required")
    if "://" in model or "@" in model or ".." in model:
        raise ValueError(f"Rejected suspicious model name: {model}")
    if not _ALLOWED_MODEL_RE.match(model):
        raise ValueError(f"Model name does not match allowed pattern: {model}")


def _log_task_error(task: asyncio.Task[Any]) -> None:
    """Log any exception from a fire-and-forget background task."""
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("Background task failed: %s", e, exc_info=True)


class RequestPipeline:
    """Main request processing pipeline for LayerCache.

    Orchestrates all processing stages from receiving a request
    to returning a response (or cached result).
    """

    def __init__(
        self,
        stratifier: Stratifier,
        canonicalizer: Canonicalizer,
        enhancement_registry: EnhancementRegistry,
        semantic_cache: SemanticCache | None,
        prompt_registry: PromptRegistry | None,
        metrics: MetricsCollector,
        metrics_db: MetricsDB | None = None,
        timeout: int = 120,
        max_retries: int = 3,
        max_session_tokens: int | None = None,
        prefix_hash_max_tokens: int = 250,
        providers_config: ProvidersConfig | None = None,
        truncation_strategy: str = "recent",
        litellm_model: str = "gpt-4o",
    ) -> None:
        self.stratifier = stratifier
        self.canonicalizer = canonicalizer
        self.enhancements = enhancement_registry
        self.semantic_cache = semantic_cache
        self.prompt_registry = prompt_registry
        self.metrics = metrics
        self.metrics_db = metrics_db
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_session_tokens = max_session_tokens
        self._prefix_hash_max_tokens = prefix_hash_max_tokens
        self._providers_config = providers_config
        self._upstream_models: dict[str, set[str]] = {}

        # Truncation
        strategy = TruncationStrategy(truncation_strategy.lower())
        self._truncator = Truncator(
            strategy=strategy, token_counter=TokenCounter(), model_name=litellm_model
        )

        # P2: throttled prefix-hash warning set
        self._prefix_warning_throttle: dict[str, float] = {}
        self._prefix_warning_throttle_max = 10000

        # Multi-tier cache components (Phase 2.1b)
        self._tier_hierarchy = CacheTierHierarchy()
        self._validator = IntentHashValidator()
        self._probation_tracker: ProbationTracker | None = None
        self._multi_tier_enabled = True

        # Initialize probation tracker if semantic cache is enabled
        if semantic_cache and semantic_cache.db_path:
            self._probation_tracker = ProbationTracker(db_path=semantic_cache.db_path)

    async def initialize(self) -> None:
        """Initialize async components (probation tracker, upstream model discovery)."""
        if self._probation_tracker:
            await self._probation_tracker.initialize()
        await self._discover_upstream_models()

    async def _discover_upstream_models(self) -> None:
        """Fetch available model IDs from each provider's upstream API.

        Cached in self._upstream_models for auto-resolution in _resolve_model.
        Non-fatal: if discovery fails, auto-resolution falls back gracefully.
        """
        if not self._providers_config:
            return
        import httpx

        for key, cfg in self._providers_config.root.items():
            if not cfg.base_url:
                continue
            api_key = os.environ.get(cfg.api_key_env) if cfg.api_key_env else None
            models_url = cfg.base_url.rstrip("/") + "/models"
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    headers = {}
                    if api_key:
                        headers["Authorization"] = f"Bearer {api_key}"
                    resp = await client.get(models_url, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                    ids = set()
                    for m in data.get("data", []):
                        mid = m.get("id", "")
                        if mid:
                            ids.add(mid)
                    if ids:
                        self._upstream_models[key] = ids
                        logger.info(
                            "Discovered %d upstream models for provider %s from %s",
                            len(ids),
                            key,
                            models_url,
                        )
            except Exception as e:
                logger.debug(
                    "Failed to discover upstream models for %s (%s): %s",
                    key,
                    models_url,
                    e,
                )

    def _truncate_session(
        self,
        prompt: StratifiedPrompt,
        model: str,
    ) -> None:
        """Truncate L2 session messages to fit within token budget.

        Args:
            prompt: Prompt to truncate (modified in place).
            model: Model name for token counting.
        """
        if self._max_session_tokens is None:
            return

        self._truncator.truncate(prompt, self._max_session_tokens)

    # ------------------------------------------------------------------
    # P2: Prefix threshold warning
    # ------------------------------------------------------------------

    def _check_prefix_threshold(
        self,
        prompt: StratifiedPrompt,
        model: str,
    ) -> None:
        """Emit an INFO log if the stable prefix (L0+L1+L2) is below the
        provider caching threshold (~1024 tokens). Rate-limited to once per
        prefix hash per hour.

        This is a read-only diagnostic — no behavior change.
        """
        prefix_hash = prompt.prefix_hash()
        now = time.time()
        last_warn = self._prefix_warning_throttle.get(prefix_hash, 0.0)
        if now - last_warn < 3600:
            return  # already warned within the hour

        # Use the new stable_prefix_tokens method for accurate counting
        prefix_tokens = prompt.stable_prefix_tokens()

        if prefix_tokens >= 1024:
            return  # prefix is cache-eligible

        self._prefix_warning_throttle[prefix_hash] = now
        if len(self._prefix_warning_throttle) > self._prefix_warning_throttle_max:
            cutoff = now - 7200
            for k in list(self._prefix_warning_throttle):
                if self._prefix_warning_throttle[k] < cutoff:
                    del self._prefix_warning_throttle[k]

        logger.info(
            "Stable prefix below cache threshold: %d tokens (need ≥1,024 for provider caching). "
            "Add static content to L0-L2 (system, tools, templates).",
            prefix_tokens,
        )

    # ------------------------------------------------------------------

    async def process_request(
        self,
        request: LayerCacheRequest,
        api_key: str,
    ) -> dict[str, Any]:
        """Process a non-streaming request through the full pipeline.

        Args:
            request: The parsed LayerCache request.
            api_key: The provider API key.

        Returns:
            The LLM response (or cached response) as a dict.
        """
        timer = RequestTimer()
        timer.__enter__()

        try:
            # Stage 1: Semantic Cache Lookup (with multi-tier support)
            cache_tier_used: str | None = None
            if (
                self.semantic_cache
                and not request.lc_skip_semantic_cache
                and not request.lc_bypass_cache
            ):
                # Create a temporary prompt for cache lookup
                temp_prompt = self.stratifier.stratify(
                    request.messages,
                    template_name=request.lc_template,
                    layer_hints=request.lc_layer_hints,
                    session_id=request.lc_session_id,
                )

                # Canonicalize the temp prompt so its hash matches what will be stored
                lookup_prompt, canonical_tools = self.canonicalizer.canonicalize(
                    temp_prompt, request.tools
                )

                cache_entry = await self.semantic_cache.lookup(
                    lookup_prompt,
                    request.model,
                    tools=canonical_tools,
                    max_l0_tokens=self._prefix_hash_max_tokens,
                )
                if cache_entry:
                    # Multi-tier validation (Phase 2.1b)
                    if self._multi_tier_enabled and self._validator:
                        query_text = lookup_prompt.get_user_query()
                        if query_text:
                            validation_result = self._validator.validate(
                                cache_entry.query_text,
                                query_text,
                            )

                            # Log validation latency
                            if validation_result.latency_ms > 50:
                                logger.warning(
                                    "Cache validation exceeded latency budget: %.2fms",
                                    validation_result.latency_ms,
                                )

                            # If validation fails, treat as cache miss
                            if not validation_result.is_match:
                                logger.info(
                                    "Cache validation failed, falling back to inference",
                                )
                                cache_entry = None
                                cache_tier_used = None
                            else:
                                cache_tier_used = "semantic"

                    if cache_entry:
                        usage = cache_entry.response_payload.get("usage", {}) or {}
                        hit_input = usage.get("prompt_tokens", 0) or 0
                        hit_output = usage.get("completion_tokens", 0) or 0
                        self.metrics.record_semantic_cache_hit(
                            model=request.model,
                            input_tokens=hit_input,
                            output_tokens=hit_output,
                        )
                        self.metrics.record_cache_lookup(
                            prefix_hash=lookup_prompt.prefix_hash(),
                            model=request.model,
                            hit=True,
                        )
                        logger.info(
                            "Semantic cache HIT for model=%s (prefix=%s...) saved %d+%d tokens",
                            request.model,
                            cache_entry.prefix_hash[:12],
                            hit_input,
                            hit_output,
                        )

                        # Track probation on cache hit (Phase 2.1b)
                        if self._multi_tier_enabled and self._probation_tracker and cache_entry.id:
                            await self._probation_tracker.increment_probation_count(cache_entry.id)

                        # Record request metrics for analytics rollup
                        if self.metrics_db:
                            from datetime import UTC, datetime

                            task = asyncio.create_task(
                                self.metrics_db.insert_request(
                                    created_at=datetime.now(UTC).isoformat(),
                                    model=request.model,
                                    session_id=request.lc_session_id,
                                    semantic_cache_hit=True,
                                    cache_tier="semantic",
                                    duration_ms=0,
                                    input_tokens=0,
                                    output_tokens=0,
                                    cache_read_tokens=hit_input + hit_output,
                                    cache_creation_tokens=0,
                                    template_name=request.lc_template,
                                    enhancements=request.lc_enhancements,
                                )
                            )
                            task.add_done_callback(_log_task_error)

                        return cache_entry.response_payload

                self.metrics.record_semantic_cache_miss()
                self.metrics.record_cache_lookup(
                    prefix_hash=lookup_prompt.prefix_hash(),
                    model=request.model,
                    hit=False,
                )

            # Stage 2: Stratification
            prompt = self.stratifier.stratify(
                request.messages,
                template_name=request.lc_template,
                layer_hints=request.lc_layer_hints,
                session_id=request.lc_session_id,
            )

            # Stage 3: Canonicalization
            prompt, canonical_tools = self.canonicalizer.canonicalize(prompt, request.tools)

            # Stage 3b: Session truncation (P3)
            self._truncate_session(prompt, request.model)

            # Stage 3c: Prefix threshold warning (P2)
            self._check_prefix_threshold(prompt, request.model)
            prefix_tokens = prompt.stable_prefix_tokens()

            # Stage 4: Enhancement Injection (L3 only)
            if request.lc_enhancements:
                await self._apply_enhancements(prompt, request)

            # Stage 5: Build LiteLLM payload
            payload = self._build_payload(request, prompt, canonical_tools)

            # Stage 6: Provider Cache Marker Injection
            provider = detect_provider(request.model, self._providers_config)
            adapter = get_adapter(provider, self._providers_config)
            payload = adapter.inject_markers(prompt, payload)

            # Stage 7: Route to LLM Provider
            response = await self._call_llm(payload, api_key, request.model, provider)

            # Stage 8: Extract metrics and store in semantic cache
            cache_metrics = adapter.extract_cache_metrics(response)
            timer.duration = time.time() - timer.start_time
            self.metrics.record_request(
                model=request.model,
                input_tokens=cache_metrics.get("input_tokens") or 0,
                output_tokens=cache_metrics.get("output_tokens") or 0,
                cache_read_tokens=cache_metrics.get("cache_read_input_tokens") or 0,
                cache_creation_tokens=cache_metrics.get("cache_creation_input_tokens") or 0,
                duration_seconds=timer.duration,
            )

            if self.metrics_db:
                from datetime import UTC, datetime

                task = asyncio.create_task(
                    self.metrics_db.insert_request(
                        created_at=datetime.now(UTC).isoformat(),
                        model=request.model,
                        session_id=request.lc_session_id,
                        semantic_cache_hit=cache_tier_used is not None,
                        cache_tier=cache_tier_used,
                        duration_ms=timer.duration * 1000,
                        input_tokens=cache_metrics.get("input_tokens", 0),
                        output_tokens=cache_metrics.get("output_tokens", 0),
                        cache_read_tokens=cache_metrics.get("cache_read_input_tokens", 0),
                        cache_creation_tokens=cache_metrics.get("cache_creation_input_tokens", 0),
                        template_name=request.lc_template,
                        enhancements=request.lc_enhancements,
                    )
                )
                task.add_done_callback(_log_task_error)

            # Store in semantic cache for future lookups
            if (
                self.semantic_cache
                and not request.lc_skip_semantic_cache
                and not request.lc_bypass_cache
                and request.lc_cache_ttl > 0
            ):
                try:
                    entry_id = await self.semantic_cache.store(
                        prompt,
                        response,
                        request.model,
                        ttl=request.lc_cache_ttl,
                        tools=canonical_tools,
                        max_l0_tokens=self._prefix_hash_max_tokens,
                    )

                    # Track new entry in probation (Phase 2.1b)
                    if self._multi_tier_enabled and self._probation_tracker and entry_id:
                        await self._probation_tracker.increment_probation_count(entry_id)
                        logger.debug(
                            "New cache entry in probation (id=%s, prefix=%s...)",
                            entry_id[:12],
                            prompt.prefix_hash()[:12],
                        )
                except Exception as e:
                    logger.warning("Failed to store in semantic cache: %s", e)

            # Stage 9: Trigger provider-specific background cache creation
            if hasattr(adapter, "create_cached_content"):
                task = asyncio.create_task(
                    adapter.create_cached_content(prompt, api_key, request.model)
                )
                task.add_done_callback(_log_task_error)

            logger.info(
                "Request completed: model=%s, input=%d, output=%d, cached=%d, duration=%.3fs",
                request.model,
                cache_metrics.get("input_tokens", 0),
                cache_metrics.get("output_tokens", 0),
                cache_metrics.get("cache_read_input_tokens", 0),
                timer.duration,
            )

            # Add stable prefix metadata to response (Phase 1.1)
            response["lc_prefix_hash"] = prompt.prefix_hash()
            response["lc_prefix_tokens"] = prefix_tokens

            return response

        except Exception as e:
            logger.error("Pipeline error: %s", e, exc_info=True)
            raise
        finally:
            timer.__exit__(None, None, None)

    async def process_streaming_request(
        self,
        request: LayerCacheRequest,
        api_key: str,
    ) -> AsyncIterator[dict[str, Any] | str]:
        """Process a streaming request through the pipeline.

        For semantic cache hits, the cached response is streamed back
        with artificial delays to mimic standard streaming behavior.

        Yields:
            Chunks from the LLM response stream (or simulated chunks from cache).
        """
        timer = RequestTimer()
        timer.__enter__()

        try:
            # Stage 1: Semantic Cache Lookup (for streaming)
            if (
                self.semantic_cache
                and not request.lc_skip_semantic_cache
                and not request.lc_bypass_cache
            ):
                temp_prompt = self.stratifier.stratify(
                    request.messages,
                    template_name=request.lc_template,
                    layer_hints=request.lc_layer_hints,
                    session_id=request.lc_session_id,
                )

                lookup_prompt, canonical_tools = self.canonicalizer.canonicalize(
                    temp_prompt, request.tools
                )

                cache_entry = await self.semantic_cache.lookup(
                    lookup_prompt,
                    request.model,
                    tools=canonical_tools,
                    max_l0_tokens=self._prefix_hash_max_tokens,
                )
                if cache_entry:
                    # Multi-tier validation (Phase 2.1b)
                    if self._multi_tier_enabled and self._validator:
                        query_text = lookup_prompt.get_user_query()
                        if query_text:
                            validation_result = self._validator.validate(
                                cache_entry.query_text,
                                query_text,
                            )

                            if not validation_result.is_match:
                                logger.info(
                                    "Cache validation failed for streaming request",
                                )
                                cache_entry = None

                    if cache_entry:
                        usage = cache_entry.response_payload.get("usage", {}) or {}
                        hit_input = usage.get("prompt_tokens", 0) or 0
                        hit_output = usage.get("completion_tokens", 0) or 0
                        self.metrics.record_semantic_cache_hit(
                            model=request.model,
                            input_tokens=hit_input,
                            output_tokens=hit_output,
                        )
                        self.metrics.record_cache_lookup(
                            prefix_hash=lookup_prompt.prefix_hash(),
                            model=request.model,
                            hit=True,
                        )
                        logger.info(
                            "Semantic cache HIT for streaming request (saved %d+%d tokens)",
                            hit_input,
                            hit_output,
                        )

                        # Track probation on cache hit (Phase 2.1b)
                        if self._multi_tier_enabled and self._probation_tracker and cache_entry.id:
                            await self._probation_tracker.increment_probation_count(cache_entry.id)

                        # Record request metrics for analytics rollup
                        if self.metrics_db:
                            from datetime import UTC, datetime

                            task = asyncio.create_task(
                                self.metrics_db.insert_request(
                                    created_at=datetime.now(UTC).isoformat(),
                                    model=request.model,
                                    session_id=request.lc_session_id,
                                    semantic_cache_hit=True,
                                    cache_tier="semantic",
                                    duration_ms=0,
                                    input_tokens=0,
                                    output_tokens=0,
                                    cache_read_tokens=hit_input + hit_output,
                                    cache_creation_tokens=0,
                                    template_name=request.lc_template,
                                    enhancements=request.lc_enhancements,
                                )
                            )
                            task.add_done_callback(_log_task_error)

                        async for chunk in self._stream_cached_response(
                            cache_entry.response_payload
                        ):
                            yield chunk
                        return

                self.metrics.record_semantic_cache_miss()
                self.metrics.record_cache_lookup(
                    prefix_hash=lookup_prompt.prefix_hash(),
                    model=request.model,
                    hit=False,
                )

            # Stages 2-6: Same as non-streaming
            prompt = self.stratifier.stratify(
                request.messages,
                template_name=request.lc_template,
                layer_hints=request.lc_layer_hints,
                session_id=request.lc_session_id,
            )
            prompt, canonical_tools = self.canonicalizer.canonicalize(prompt, request.tools)

            self._truncate_session(prompt, request.model)
            self._check_prefix_threshold(prompt, request.model)

            if request.lc_enhancements:
                await self._apply_enhancements(prompt, request)

            payload = self._build_payload(request, prompt, canonical_tools, stream=True)

            provider = detect_provider(request.model, self._providers_config)
            adapter = get_adapter(provider, self._providers_config)
            payload = adapter.inject_markers(prompt, payload)

            # Stage 7: Stream from LLM
            final_chunk: dict[str, Any] | None = None
            content_parts: list[str] = []
            async for chunk in self._stream_llm(payload, api_key, request.model, provider):
                if isinstance(chunk, dict):
                    final_chunk = chunk
                    # Accumulate content for cache storage
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta", {})
                        if isinstance(delta, dict):
                            c = delta.get("content")
                            if c:
                                content_parts.append(c)
                yield chunk

            # Stage 8: Record metrics from the final streaming chunk
            if final_chunk and isinstance(final_chunk, dict):
                choices = final_chunk.get("choices", [{}])
                usage = final_chunk.get("usage", {})

                if usage:
                    input_tokens = usage.get("prompt_tokens", 0)
                    output_tokens = usage.get("completion_tokens", 0)
                else:
                    last_choice = choices[-1] if choices else {}
                    if isinstance(last_choice, dict):
                        choice_usage = last_choice.get("usage", {})
                        input_tokens = choice_usage.get("prompt_tokens", 0)
                        output_tokens = choice_usage.get("completion_tokens", 0)
                    else:
                        input_tokens = output_tokens = 0

                cache_metrics = adapter.extract_cache_metrics(final_chunk)
                timer.duration = time.time() - timer.start_time
                self.metrics.record_request(
                    model=request.model,
                    input_tokens=input_tokens or cache_metrics.get("input_tokens") or 0,
                    output_tokens=output_tokens or cache_metrics.get("output_tokens") or 0,
                    cache_read_tokens=cache_metrics.get("cache_read_input_tokens") or 0,
                    cache_creation_tokens=cache_metrics.get("cache_creation_input_tokens") or 0,
                    duration_seconds=timer.duration,
                )

            # Store in semantic cache for future lookups
            if (
                self.semantic_cache
                and not request.lc_skip_semantic_cache
                and not request.lc_bypass_cache
                and request.lc_cache_ttl > 0
                and final_chunk
            ):
                try:
                    full_content = "".join(content_parts)
                    response = {
                        "id": final_chunk.get("id", ""),
                        "object": "chat.completion",
                        "created": final_chunk.get("created", 0),
                        "model": final_chunk.get("model", request.model),
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": full_content,
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": final_chunk.get("usage", {}),
                    }
                    entry_id = await self.semantic_cache.store(
                        prompt,
                        response,
                        request.model,
                        ttl=request.lc_cache_ttl,
                        tools=canonical_tools,
                        max_l0_tokens=self._prefix_hash_max_tokens,
                    )
                    if self._multi_tier_enabled and self._probation_tracker and entry_id:
                        await self._probation_tracker.increment_probation_count(entry_id)
                        logger.debug(
                            "New cache entry in probation (id=%s, prefix=%s...)",
                            entry_id[:12],
                            prompt.prefix_hash()[:12],
                        )
                except Exception as e:
                    logger.warning("Failed to store streaming response in semantic cache: %s", e)

            # Stage 9: Trigger provider-specific background cache creation
            if hasattr(adapter, "create_cached_content"):
                task = asyncio.create_task(
                    adapter.create_cached_content(prompt, api_key, request.model)
                )
                task.add_done_callback(_log_task_error)

        except asyncio.CancelledError:
            logger.warning("Streaming request cancelled by client")
            raise
        except Exception as e:
            logger.error("Streaming pipeline error: %s", e, exc_info=True)
            raise
        finally:
            timer.__exit__(None, None, None)

    async def _apply_enhancements(
        self,
        prompt: StratifiedPrompt,
        request: LayerCacheRequest,
    ) -> None:
        """Apply requested enhancements to the prompt."""
        if "dynamic_few_shot" in request.lc_enhancements:
            few_shot = self.enhancements.get("dynamic_few_shot")
            if few_shot is not None and hasattr(few_shot, "apply_async"):
                names = [n for n in request.lc_enhancements if n != "dynamic_few_shot"]
                self.enhancements.apply_enhancements(prompt, names, model=request.model)
                await few_shot.apply_async(prompt, model=request.model)
                return

        self.enhancements.apply_enhancements(prompt, request.lc_enhancements, model=request.model)

    def _build_payload(
        self,
        request: LayerCacheRequest,
        prompt: StratifiedPrompt,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
    ) -> dict[str, Any]:
        """Build the LiteLLM-compatible payload from the processed prompt."""
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": prompt.reassemble(),
            "stream": stream,
        }

        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if tools:
            payload["tools"] = tools
        if request.tool_choice is not None:
            payload["tool_choice"] = request.tool_choice
        if request.response_format is not None:
            payload["response_format"] = request.response_format
        if request.user is not None:
            payload["user"] = request.user
        if request.stop is not None:
            payload["stop"] = request.stop

        return payload

    async def _call_llm(
        self,
        payload: dict[str, Any],
        api_key: str,
        model: str,
        provider: str = "",
    ) -> dict[str, Any]:
        """Route the request to the LLM provider via LiteLLM."""
        try:
            import litellm

            # For Anthropic providers, call the API directly to avoid LiteLLM's
            # OpenAI->Anthropic message translation which corrupts tool sequences.
            if provider == "anthropic":
                return await self._call_anthropic_direct(payload, api_key, model)

            # Look up base_url and adapter from provider config
            kwargs: dict[str, Any] = {
                "api_key": api_key,
                "timeout": self._timeout,
                "num_retries": self._max_retries,
            }
            litellm_model = model
            if self._providers_config and provider in self._providers_config.root:
                provider_cfg = self._providers_config.root[provider]
                if provider_cfg.base_url:
                    kwargs["api_base"] = provider_cfg.base_url
                # Use adapter name as LiteLLM provider prefix
                adapter = self._providers_config.adapter_for(provider)
                # Build litellm model name: adapter/model_name
                if "/" in model:
                    model_name = model.split("/", 1)[1]
                else:
                    model_name = model
                model_name = self._resolve_model(model_name, provider)
                litellm_model = f"{adapter}/{model_name}"

            response = await litellm.acompletion(
                model=litellm_model,
                **{k: v for k, v in payload.items() if k != "model"},
                **kwargs,
            )
            return response.model_dump()
        except Exception as e:
            logger.error("LiteLLM call failed: %s", e)
            raise

    async def _call_anthropic_direct(
        self,
        payload: dict[str, Any],
        api_key: str,
        model: str,
    ) -> dict[str, Any]:
        """Call the Anthropic Messages API directly, bypassing LiteLLM.

        Converts the pipeline's OpenAI-format payload to Anthropic format,
        sends it via HTTP, and converts the response back to OpenAI format
        for cache storage.
        """
        import json

        import httpx

        # --- Convert OpenAI messages to Anthropic format ---
        anthropic_messages: list[dict[str, Any]] = []
        system_content: str | None = None

        for msg in payload.get("messages", []):
            role = msg["role"]
            content = msg.get("content", "")
            tool_calls = msg.get("tool_calls")
            tool_call_id = msg.get("tool_call_id")

            if role == "system":
                system_content = (
                    (system_content or "") + ("\n" if system_content else "") + str(content)
                )
                continue

            if role == "assistant" and tool_calls:
                blocks: list[dict[str, Any]] = []
                if content:
                    blocks.append({"type": "text", "text": str(content)})
                for tc in tool_calls:
                    func = tc.get("function", {})
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": func.get("name", ""),
                            "input": json.loads(func.get("arguments", "{}"))
                            if isinstance(func.get("arguments"), str)
                            else func.get("arguments", {}),
                        }
                    )
                anthropic_messages.append({"role": "assistant", "content": blocks})
                continue

            if role == "tool":
                anthropic_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_call_id or "",
                                "content": str(content) if content else "",
                            }
                        ],
                    }
                )
                continue

            # Regular user message
            if role == "user":
                if isinstance(content, str):
                    anthropic_messages.append(
                        {"role": "user", "content": [{"type": "text", "text": content}]}
                    )
                elif isinstance(content, list):
                    anthropic_messages.append({"role": "user", "content": content})
                continue

            # Fallback: pass through
            anthropic_messages.append({"role": role, "content": content or ""})

        # --- Build Anthropic request body ---
        anthropic_body: dict[str, Any] = {
            "model": model,
            "messages": anthropic_messages,
            "max_tokens": payload.get("max_tokens", 4096),
        }
        if system_content:
            anthropic_body["system"] = system_content
        if payload.get("temperature") is not None:
            anthropic_body["temperature"] = payload["temperature"]
        if payload.get("top_p") is not None:
            anthropic_body["top_p"] = payload["top_p"]
        if payload.get("stop"):
            anthropic_body["stop_sequences"] = payload["stop"]

        # Convert tools from OpenAI to Anthropic format
        tools = payload.get("tools")
        if tools:
            anthropic_tools = []
            for t in tools:
                func = t.get("function", {})
                anthropic_tools.append(
                    {
                        "name": func.get("name", ""),
                        "description": func.get("description", ""),
                        "input_schema": func.get("parameters", {}),
                    }
                )
            anthropic_body["tools"] = anthropic_tools

        tool_choice = payload.get("tool_choice")
        if tool_choice:
            if isinstance(tool_choice, str):
                reverse_map = {"auto": "auto", "required": "any", "none": "none"}
                anthropic_body["tool_choice"] = {"type": reverse_map.get(tool_choice, tool_choice)}
            elif isinstance(tool_choice, dict):
                tc_type = tool_choice.get("type")
                if tc_type == "function":
                    anthropic_body["tool_choice"] = {
                        "type": "tool",
                        "name": tool_choice.get("function", {}).get("name", ""),
                    }
                else:
                    anthropic_body["tool_choice"] = tool_choice

        # --- Call Anthropic API ---
        provider_cfg = (
            self._providers_config.root.get("anthropic") if self._providers_config else None
        )
        base_url = (
            provider_cfg.base_url
            if provider_cfg and provider_cfg.base_url
            else "https://api.anthropic.com/v1"
        )
        api_url = f"{base_url.rstrip('/')}/messages"

        headers = _anthropic_auth_headers(api_key)

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(api_url, json=anthropic_body, headers=headers)
            if response.status_code != 200:
                logger.error(
                    "Anthropic API error (HTTP %d): %s",
                    response.status_code,
                    response.text[:500],
                )
                response.raise_for_status()

            anthropic_resp = response.json()

        # --- Convert Anthropic response to OpenAI format ---
        openai_choices: list[dict[str, Any]] = []
        content_blocks = anthropic_resp.get("content", [])
        text_parts: list[str] = []
        openai_tool_calls: list[dict[str, Any]] = []

        for block in content_blocks:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                openai_tool_calls.append(
                    {
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    }
                )

        message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
        if openai_tool_calls:
            message["tool_calls"] = openai_tool_calls

        usage = anthropic_resp.get("usage", {})
        openai_choices.append(
            {
                "index": 0,
                "message": message,
                "finish_reason": _anthropic_stop_reason(anthropic_resp.get("stop_reason", "")),
            }
        )

        return {
            "id": anthropic_resp.get("id", ""),
            "object": "chat.completion",
            "created": int(__import__("time").time()),
            "model": anthropic_resp.get("model", model),
            "choices": openai_choices,
            "usage": {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
            },
        }

    def _resolve_model(self, model: str, provider: str) -> str:
        """Resolve model name through provider aliases or upstream model list."""
        if not self._providers_config:
            return model
        cfg = self._providers_config.root.get(provider)
        if not cfg:
            return model
        # First check explicit aliases
        if cfg.model_aliases:
            resolved = cfg.model_aliases.get(model)
            if resolved:
                logger.debug("Model alias: %s -> %s (provider=%s)", model, resolved, provider)
                return resolved
        # Strip provider-specific suffixes (e.g. :cloud for Ollama Cloud)
        resolved = model
        if model.endswith(":cloud") and provider == "ollama-cloud":
            resolved = model[: -len(":cloud")]
            logger.debug(
                "Stripped :cloud suffix: %s -> %s (provider=%s)",
                model,
                resolved,
                provider,
            )
        # Auto-resolve: if upstream has a model whose name starts with <model>-,
        # use that (catches deepseek-v4-flash -> deepseek-v4-flash-free etc.)
        if cfg.base_url and self._upstream_models.get(provider):
            upstream_ids = self._upstream_models[provider]
            if resolved not in upstream_ids:
                prefix = f"{resolved}-"
                matches = [m for m in upstream_ids if m.startswith(prefix)]
                if len(matches) == 1:
                    logger.info(
                        "Auto-resolved model %s -> %s (provider=%s)",
                        resolved,
                        matches[0],
                        provider,
                    )
                    return matches[0]
        return resolved

    async def _stream_llm(
        self,
        payload: dict[str, Any],
        api_key: str,
        model: str,
        provider: str = "",
    ) -> AsyncIterator[dict[str, Any] | str]:
        """Stream responses from the LLM provider."""
        try:
            import litellm

            # For Anthropic providers, stream directly to avoid LiteLLM translation issues
            if provider == "anthropic":
                async for chunk in self._stream_anthropic_direct(payload, api_key, model):
                    yield chunk
                return

            # LiteLLM path for non-Anthropic providers
            kwargs: dict[str, Any] = {
                "api_key": api_key,
                "timeout": self._timeout,
                "num_retries": self._max_retries,
            }
            litellm_model = model
            if self._providers_config and provider in self._providers_config.root:
                provider_cfg = self._providers_config.root[provider]
                if provider_cfg.base_url:
                    kwargs["api_base"] = provider_cfg.base_url
                adapter = self._providers_config.adapter_for(provider)
                if "/" in model:
                    model_name = model.split("/", 1)[1]
                else:
                    model_name = model
                model_name = self._resolve_model(model_name, provider)
                litellm_model = f"{adapter}/{model_name}"

            response = await litellm.acompletion(
                model=litellm_model,
                **{k: v for k, v in payload.items() if k != "model"},
                **kwargs,
            )
            async for chunk in response:
                yield chunk.model_dump()
        except Exception as e:
            print(f"DEBUG LLM ERROR: {type(e).__name__}: {e}", flush=True)
            logger.error("LiteLLM streaming failed: %s", e)
            raise

    async def _stream_anthropic_direct(
        self,
        payload: dict[str, Any],
        api_key: str,
        model: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream from the Anthropic Messages API directly, yielding OpenAI-format chunks."""
        import json

        import httpx

        # Build Anthropic body (same as _call_anthropic_direct)
        anthropic_messages: list[dict[str, Any]] = []
        system_content: str | None = None

        for msg in payload.get("messages", []):
            role = msg["role"]
            content = msg.get("content", "")
            tool_calls = msg.get("tool_calls")
            tool_call_id = msg.get("tool_call_id")

            if role == "system":
                system_content = (
                    (system_content or "") + ("\n" if system_content else "") + str(content)
                )
                continue
            if role == "assistant" and tool_calls:
                blocks = []
                if content:
                    blocks.append({"type": "text", "text": str(content)})
                for tc in tool_calls:
                    func = tc.get("function", {})
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": func.get("name", ""),
                            "input": json.loads(func.get("arguments", "{}"))
                            if isinstance(func.get("arguments"), str)
                            else func.get("arguments", {}),
                        }
                    )
                anthropic_messages.append({"role": "assistant", "content": blocks})
                continue
            if role == "tool":
                anthropic_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_call_id or "",
                                "content": str(content) if content else "",
                            }
                        ],
                    }
                )
                continue
            if role == "user":
                if isinstance(content, str):
                    anthropic_messages.append(
                        {"role": "user", "content": [{"type": "text", "text": content}]}
                    )
                elif isinstance(content, list):
                    anthropic_messages.append({"role": "user", "content": content})
                continue
            anthropic_messages.append({"role": role, "content": content or ""})

        anthropic_body: dict[str, Any] = {
            "model": model,
            "messages": anthropic_messages,
            "max_tokens": payload.get("max_tokens", 4096),
            "stream": True,
        }
        if system_content:
            anthropic_body["system"] = system_content
        if payload.get("temperature") is not None:
            anthropic_body["temperature"] = payload["temperature"]
        if payload.get("top_p") is not None:
            anthropic_body["top_p"] = payload["top_p"]

        tools = payload.get("tools")
        if tools:
            anthropic_tools = []
            for t in tools:
                func = t.get("function", {})
                anthropic_tools.append(
                    {
                        "name": func.get("name", ""),
                        "description": func.get("description", ""),
                        "input_schema": func.get("parameters", {}),
                    }
                )
            anthropic_body["tools"] = anthropic_tools

        provider_cfg = (
            self._providers_config.root.get("anthropic") if self._providers_config else None
        )
        base_url = (
            provider_cfg.base_url
            if provider_cfg and provider_cfg.base_url
            else "https://api.anthropic.com/v1"
        )
        api_url = f"{base_url.rstrip('/')}/messages"

        headers = _anthropic_auth_headers(api_key)

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream("POST", api_url, json=anthropic_body, headers=headers) as resp:
                if resp.status_code != 200:
                    error_text = await resp.aread()
                    logger.error(
                        "Anthropic API error (HTTP %d): %s",
                        resp.status_code,
                        error_text[:500].decode(),
                    )
                    resp.raise_for_status()

                message_id = ""
                model_name = model
                text_accumulator = ""
                tool_calls_accumulator: list[dict] = []

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = json.loads(line[6:])
                    event_type = data.get("type", "")

                    if event_type == "message_start":
                        msg_data = data.get("message", {})
                        message_id = msg_data.get("id", "")
                        model_name = msg_data.get("model", model)

                    elif event_type == "content_block_start":
                        block = data.get("content_block", {})
                        if block.get("type") == "tool_use":
                            tool_calls_accumulator.append(
                                {
                                    "id": block.get("id", ""),
                                    "type": "function",
                                    "function": {"name": block.get("name", ""), "arguments": ""},
                                }
                            )

                    elif event_type == "content_block_delta":
                        delta = data.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text_accumulator += delta.get("text", "")
                        elif delta.get("type") == "input_json_delta":
                            if tool_calls_accumulator:
                                tool_calls_accumulator[-1]["function"]["arguments"] += delta.get(
                                    "partial_json", ""
                                )

                    elif event_type == "message_delta":
                        delta = data.get("delta", {})
                        usage = data.get("usage", {})
                        stop_reason = _anthropic_stop_reason(delta.get("stop_reason", ""))

                        # Yield final chunk with usage
                        delta_content: dict[str, Any] = {"content": "", "role": "assistant"}
                        if tool_calls_accumulator:
                            delta_content["tool_calls"] = tool_calls_accumulator
                        yield {
                            "id": message_id or f"msg_{id(self)}",
                            "object": "chat.completion.chunk",
                            "created": int(__import__("time").time()),
                            "model": model_name,
                            "choices": [
                                {"index": 0, "delta": delta_content, "finish_reason": stop_reason}
                            ],
                            "usage": {
                                "prompt_tokens": usage.get("input_tokens", 0),
                                "completion_tokens": usage.get("output_tokens", 0),
                            },
                        }

                    # Yield streaming content chunks
                    if text_accumulator and event_type == "content_block_delta":
                        delta_type = data.get("delta", {}).get("type", "")
                        if delta_type == "text_delta":
                            yield {
                                "id": message_id or f"msg_{id(self)}",
                                "object": "chat.completion.chunk",
                                "created": int(__import__("time").time()),
                                "model": model_name,
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {
                                            "content": data["delta"]["text"],
                                            "role": "assistant",
                                        },
                                        "finish_reason": None,
                                    }
                                ],
                            }

    @staticmethod
    async def _stream_cached_response(response: dict[str, Any]) -> AsyncIterator[str]:
        """Stream a cached response with artificial delays."""
        choices = response.get("choices", [])
        if not choices:
            return

        message = choices[0].get("message", {})
        content = message.get("content", "")

        if content:
            chunk_size = 20
            for i in range(0, len(content), chunk_size):
                chunk_text = content[i : i + chunk_size]
                yield chunk_text
                await asyncio.sleep(0.01)

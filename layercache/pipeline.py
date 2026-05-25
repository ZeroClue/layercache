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
import re
from collections.abc import AsyncIterator
from typing import Any

from .adapters import detect_provider, get_adapter
from .cache.semantic import SemanticCache
from .canonicalizer import Canonicalizer
from .enhancements.base import EnhancementRegistry
from .metrics.collector import MetricsCollector, RequestTimer
from .models import LayerCacheRequest, StratifiedPrompt
from .registry.prompt_registry import PromptRegistry
from .stratifier import Stratifier

logger = logging.getLogger(__name__)

# Model names must match known provider prefixes to block SSRF via LiteLLM
_ALLOWED_MODEL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*(/[a-zA-Z][a-zA-Z0-9_.-]+)?$")


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
        timeout: int = 120,
        max_retries: int = 3,
    ) -> None:
        self.stratifier = stratifier
        self.canonicalizer = canonicalizer
        self.enhancements = enhancement_registry
        self.semantic_cache = semantic_cache
        self.prompt_registry = prompt_registry
        self.metrics = metrics
        self._timeout = timeout
        self._max_retries = max_retries

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
            # Stage 1: Semantic Cache Lookup
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
                )

                # Canonicalize the temp prompt so its hash matches what will be stored
                lookup_prompt, _canonical_tools = self.canonicalizer.canonicalize(
                    temp_prompt, request.tools
                )

                cache_entry = await self.semantic_cache.lookup(lookup_prompt, request.model)
                if cache_entry:
                    self.metrics.record_semantic_cache_hit()
                    logger.info(
                        "Semantic cache HIT for model=%s (prefix=%s...)",
                        request.model,
                        cache_entry.prefix_hash[:12],
                    )
                    return cache_entry.response_payload

                self.metrics.record_semantic_cache_miss()

            # Stage 2: Stratification
            prompt = self.stratifier.stratify(
                request.messages,
                template_name=request.lc_template,
                layer_hints=request.lc_layer_hints,
            )

            # Stage 3: Canonicalization
            prompt, canonical_tools = self.canonicalizer.canonicalize(prompt, request.tools)

            # Stage 4: Enhancement Injection (L3 only)
            if request.lc_enhancements:
                await self._apply_enhancements(prompt, request)

            # Stage 5: Build LiteLLM payload
            payload = self._build_payload(request, prompt, canonical_tools)

            # Stage 6: Provider Cache Marker Injection
            provider = detect_provider(request.model)
            adapter = get_adapter(provider)
            payload = adapter.inject_markers(prompt, payload)

            # Stage 7: Route to LLM Provider
            response = await self._call_llm(payload, api_key)

            # Stage 8: Extract metrics and store in semantic cache
            cache_metrics = adapter.extract_cache_metrics(response)
            self.metrics.record_request(
                model=request.model,
                input_tokens=cache_metrics.get("input_tokens", 0),
                output_tokens=cache_metrics.get("output_tokens", 0),
                cache_read_tokens=cache_metrics.get("cache_read_input_tokens", 0),
                cache_creation_tokens=cache_metrics.get("cache_creation_input_tokens", 0),
                duration_seconds=timer.duration,
            )

            # Store in semantic cache for future lookups
            if (
                self.semantic_cache
                and not request.lc_skip_semantic_cache
                and not request.lc_bypass_cache
                and request.lc_cache_ttl > 0
            ):
                try:
                    await self.semantic_cache.store(
                        prompt,
                        response,
                        request.model,
                        ttl=request.lc_cache_ttl,
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
                )

                lookup_prompt, _canonical_tools = self.canonicalizer.canonicalize(
                    temp_prompt, request.tools
                )

                cache_entry = await self.semantic_cache.lookup(lookup_prompt, request.model)
                if cache_entry:
                    self.metrics.record_semantic_cache_hit()
                    logger.info("Semantic cache HIT for streaming request")
                    async for chunk in self._stream_cached_response(cache_entry.response_payload):
                        yield chunk
                    return

                self.metrics.record_semantic_cache_miss()

            # Stages 2-6: Same as non-streaming
            prompt = self.stratifier.stratify(
                request.messages,
                template_name=request.lc_template,
                layer_hints=request.lc_layer_hints,
            )
            prompt, canonical_tools = self.canonicalizer.canonicalize(prompt, request.tools)

            if request.lc_enhancements:
                await self._apply_enhancements(prompt, request)

            payload = self._build_payload(request, prompt, canonical_tools, stream=True)

            provider = detect_provider(request.model)
            adapter = get_adapter(provider)
            payload = adapter.inject_markers(prompt, payload)

            # Stage 7: Stream from LLM
            final_chunk: dict[str, Any] | None = None
            async for chunk in self._stream_llm(payload, api_key, provider):
                # Keep the last chunk for metrics (it carries usage data)
                if isinstance(chunk, dict):
                    final_chunk = chunk
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
                self.metrics.record_request(
                    model=request.model,
                    input_tokens=input_tokens or cache_metrics.get("input_tokens", 0),
                    output_tokens=output_tokens or cache_metrics.get("output_tokens", 0),
                    cache_read_tokens=cache_metrics.get("cache_read_input_tokens", 0),
                    cache_creation_tokens=cache_metrics.get("cache_creation_input_tokens", 0),
                    duration_seconds=timer.duration,
                )

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

    async def _call_llm(self, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
        """Route the request to the LLM provider via LiteLLM."""
        try:
            import litellm

            response = await litellm.acompletion(
                **payload,
                api_key=api_key,
                timeout=self._timeout,
                num_retries=self._max_retries,
            )
            return response.model_dump()
        except Exception as e:
            logger.error("LiteLLM call failed: %s", e)
            raise

    async def _stream_llm(
        self,
        payload: dict[str, Any],
        api_key: str,
        provider: str = "",
    ) -> AsyncIterator[dict[str, Any] | str]:
        """Stream responses from the LLM provider via LiteLLM."""
        try:
            import litellm

            response = await litellm.acompletion(
                **payload,
                api_key=api_key,
                timeout=self._timeout,
                num_retries=self._max_retries,
            )
            async for chunk in response:
                yield chunk.model_dump()
        except Exception as e:
            logger.error("LiteLLM streaming failed: %s", e)
            raise

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

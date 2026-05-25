"""Request Pipeline - Orchestrates the full request processing flow.

The pipeline processes each incoming request through these stages:
1. Semantic Cache Lookup (bypass LLM if similar query cached)
2. Stratification (L0-L4 classification)
3. Canonicalization (normalization for cache-friendly output)
4. Enhancement Injection (L3 modifications)
5. Cache Marker Injection (provider-specific)
6. Provider Routing (via LiteLLM)
7. Response Handling (metrics, cache storage)
"""

from __future__ import annotations

import logging
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
    ) -> None:
        self.stratifier = stratifier
        self.canonicalizer = canonicalizer
        self.enhancements = enhancement_registry
        self.semantic_cache = semantic_cache
        self.prompt_registry = prompt_registry
        self.metrics = metrics

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

                cache_entry = await self.semantic_cache.lookup(temp_prompt, request.model)
                if cache_entry:
                    self.metrics.record_semantic_cache_hit()
                    logger.info(
                        "Semantic cache HIT for model=%s (prefix=%s...)",
                        request.model,
                        cache_entry.prefix_hash[:12],
                    )
                    timer.__exit__(None, None, None)
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
                cache_entry = await self.semantic_cache.lookup(temp_prompt, request.model)
                if cache_entry:
                    self.metrics.record_semantic_cache_hit()
                    logger.info("Semantic cache HIT for streaming request")
                    timer.__exit__(None, None, None)
                    # Stream cached response with artificial chunks
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

            async for chunk in self._stream_llm(payload, api_key):
                yield chunk

            # Note: For streaming, detailed token metrics come in the final chunk
            # The streaming handler in main.py will parse and record these

            timer.__exit__(None, None, None)

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
        # Check for dynamic_few_shot which needs async embedding
        if "dynamic_few_shot" in request.lc_enhancements:
            few_shot = self.enhancements.get("dynamic_few_shot")
            if few_shot and hasattr(few_shot, "apply_async"):
                # Remove from list and apply async
                names = [n for n in request.lc_enhancements if n != "dynamic_few_shot"]
                self.enhancements.apply_enhancements(prompt, names, model=request.model)
                await few_shot.apply_async(prompt, model=request.model)
                return

        # Standard synchronous enhancement application
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

        return payload

    async def _call_llm(self, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
        """Route the request to the LLM provider via LiteLLM."""
        try:
            import litellm
            response = await litellm.acompletion(
                **payload,
                api_key=api_key,
            )
            return response.model_dump()
        except Exception as e:
            logger.error("LiteLLM call failed: %s", e)
            raise

    async def _stream_llm(
        self, payload: dict[str, Any], api_key: str
    ) -> AsyncIterator[dict[str, Any] | str]:
        """Stream responses from the LLM provider via LiteLLM."""
        try:
            import litellm
            response = await litellm.acompletion(
                **payload,
                api_key=api_key,
            )
            async for chunk in response:
                yield chunk.model_dump()
        except Exception as e:
            logger.error("LiteLLM streaming failed: %s", e)
            raise

    @staticmethod
    async def _stream_cached_response(response: dict[str, Any]) -> AsyncIterator[str]:
        """Stream a cached response with artificial delays."""
        import asyncio

        choices = response.get("choices", [])
        if not choices:
            return

        message = choices[0].get("message", {})
        content = message.get("content", "")

        if content:
            # Split into small chunks and stream with small delays
            chunk_size = 20
            for i in range(0, len(content), chunk_size):
                chunk_text = content[i : i + chunk_size]
                # Format as SSE-like data
                yield chunk_text
                await asyncio.sleep(0.01)

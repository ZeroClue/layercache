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
import time
from collections.abc import AsyncIterator
from typing import Any

from .adapters import detect_provider, get_adapter
from .cache.semantic import SemanticCache
from .canonicalizer import Canonicalizer
from .enhancements.base import EnhancementRegistry
from .metrics.collector import MetricsCollector, RequestTimer
from .models import LayerCacheRequest, LayerType, StratifiedMessage, StratifiedPrompt
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
        max_session_tokens: int | None = None,
    ) -> None:
        self.stratifier = stratifier
        self.canonicalizer = canonicalizer
        self.enhancements = enhancement_registry
        self.semantic_cache = semantic_cache
        self.prompt_registry = prompt_registry
        self.metrics = metrics
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_session_tokens = max_session_tokens

        # P2: throttled prefix-hash warning set
        self._prefix_warning_throttle: dict[str, float] = {}
        self._prefix_warning_throttle_max = 10000

    # ------------------------------------------------------------------
    # P3: Session truncation
    # ------------------------------------------------------------------

    @staticmethod
    def _split_turns(
        messages: list[StratifiedMessage],
    ) -> list[list[StratifiedMessage]]:
        """Split session messages into turn groups. A turn starts with a user message."""
        turns: list[list[StratifiedMessage]] = []
        current: list[StratifiedMessage] = []
        for m in messages:
            if m.role == "user" and current:
                turns.append(current)
                current = []
            current.append(m)
        if current:
            turns.append(current)
        return turns

    def _truncate_session(
        self,
        prompt: StratifiedPrompt,
        model: str,
    ) -> None:
        """Truncate L2 to fit within max_session_tokens.

        Splits session messages into turn groups (user→assistant/tool sequences)
        and drops the oldest groups until the remaining fit within budget.
        At least one turn is always preserved, even if it exceeds the budget.
        Logs at INFO when truncation occurs.
        """
        max_tokens = self._max_session_tokens
        if max_tokens is None or max_tokens <= 0:
            return

        session_msgs = prompt.layers.get(LayerType.SESSION, [])
        if not session_msgs:
            return

        import litellm

        def _count(content: str) -> int:
            try:
                result = litellm.token_counter(model=model, text=content)
                return int(result) if result is not None else len(content) // 2
            except Exception:
                return len(content) // 2

        turns = self._split_turns(session_msgs)
        if len(turns) <= 1:
            return  # single turn, nothing to truncate

        # Work backwards: keep trailing turns until budget is breached
        kept_turns: list[list[StratifiedMessage]] = []
        total_tokens = 0
        for turn in reversed(turns):
            turn_tokens = sum(_count(str(m.content)) for m in turn)
            if total_tokens + turn_tokens > max_tokens and kept_turns:
                break
            kept_turns.insert(0, turn)
            total_tokens += turn_tokens

        # Flatten
        kept: list[StratifiedMessage] = []
        for t in kept_turns:
            kept.extend(t)

        if len(kept) != len(session_msgs):
            prompt.layers[LayerType.SESSION] = kept
            logger.info(
                "Truncated L2 from %d to %d messages (%d turns, ~%d tokens) for model=%s. "
                "Provider prefix caching enabled; semantic cache will miss for this conversation.",
                len(session_msgs),
                len(kept),
                len(kept_turns),
                total_tokens,
                model,
            )

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

        import litellm

        parts: list[str] = []
        for layer_type in (LayerType.SYSTEM, LayerType.CONTEXT, LayerType.SESSION):
            for msg in sorted(
                prompt.layers[layer_type], key=lambda m: m.content_hash()
            ):
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                parts.append(content)
        prefix_text = "\n".join(parts)
        if not prefix_text.strip():
            return

        try:
            raw = litellm.token_counter(model=model, text=prefix_text)
            estimated = int(raw) if raw is not None else len(prefix_text) // 2
        except Exception:
            estimated = len(prefix_text) // 2  # CJK-safe fallback (~2 chars/token)

        if estimated < 1024:
            self._prefix_warning_throttle[prefix_hash] = now
            if len(self._prefix_warning_throttle) > self._prefix_warning_throttle_max:
                cutoff = now - 7200
                for k in list(self._prefix_warning_throttle):
                    if self._prefix_warning_throttle[k] < cutoff:
                        del self._prefix_warning_throttle[k]
            logger.info(
                "Stable prefix (L0+L1+L2) ~%d tokens (model=%s) — below ~1024 token "
                "caching threshold. Add more content to L0/L1 or expect low cache hit rates.",
                estimated,
                model,
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

            # Stage 3b: Session truncation (P3)
            self._truncate_session(prompt, request.model)

            # Stage 3c: Prefix threshold warning (P2)
            self._check_prefix_threshold(prompt, request.model)

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

            self._truncate_session(prompt, request.model)
            self._check_prefix_threshold(prompt, request.model)

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

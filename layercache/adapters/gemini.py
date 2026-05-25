"""Google Gemini provider adapter - handles CachedContent API for prefix caching."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from ..models import LayerType, StratifiedPrompt
from .base import BaseAdapter

logger = logging.getLogger(__name__)

# Default TTL for Gemini CachedContent resources (1 hour)
_DEFAULT_CACHE_TTL = 3600


class GeminiAdapter(BaseAdapter):
    """Adapter for Google Gemini's explicit content caching.

    Gemini requires creating `CachedContent` resources explicitly via the API.
    LayerCache creates these resources for the L0+L1 prefix and references
    them in subsequent requests.

    Strategy:
    - Compute a prefix hash from L0 + L1 content
    - On first request with a new hash, make a standard call AND async-create the cache
    - On subsequent requests, use the cached content resource
    - Store prefix_hash -> CachedContent name mapping in memory/DB
    """

    provider_name = "gemini"

    def __init__(self) -> None:
        self._cache_map: dict[str, str] = {}  # prefix_hash -> cached_content_name
        self._pending_creates: set[str] = set()  # hashes being created

    def inject_markers(
        self,
        prompt: StratifiedPrompt,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Inject Gemini cache references or trigger cache creation.

        For Gemini, we:
        1. Compute the prefix hash (L0 + L1)
        2. Check if we have a cached content resource
        3. If yes, use it and only send L2+ content
        4. If no, send full content (cache creation is triggered by the pipeline)
        """
        prefix_hash = self._compute_prefix_hash(prompt)

        cached_content_name = self._cache_map.get(prefix_hash)

        if cached_content_name:
            # Use cached content: only send L2+ content
            remaining_messages = self._get_remaining_messages(prompt)
            payload["contents"] = self._convert_to_gemini_format(remaining_messages)
            payload["cached_content"] = cached_content_name
        else:
            # No cache yet: send full content
            all_messages = prompt.reassemble()
            payload["contents"] = self._convert_to_gemini_format(all_messages)

        return payload

    async def create_cached_content(
        self,
        prompt: StratifiedPrompt,
        api_key: str,
        model: str,
        ttl_seconds: int = _DEFAULT_CACHE_TTL,
    ) -> str | None:
        """Create a Gemini CachedContent resource for the L0+L1 prefix.

        Called by the pipeline after the first successful LLM response.
        Subsequent requests will use the cached content and only send L2+.
        """
        import httpx

        prefix_hash = self._compute_prefix_hash(prompt)

        # Already cached or creation already in flight
        if prefix_hash in self._cache_map:
            return self._cache_map[prefix_hash]
        if prefix_hash in self._pending_creates:
            return None

        # Build system instruction from L0 (system role messages)
        system_parts: list[dict[str, str]] = []
        for msg in sorted(prompt.layers[LayerType.SYSTEM], key=lambda m: m.content_hash()):
            system_parts.append({"text": str(msg.content)})

        # Build cached contents from L1 (context messages)
        contents: list[dict[str, Any]] = []
        for msg in sorted(prompt.layers[LayerType.CONTEXT], key=lambda m: m.content_hash()):
            contents.append(
                {
                    "role": "user",
                    "parts": [{"text": str(msg.content)}],
                }
            )

        # Nothing meaningful to cache
        if not system_parts and not contents:
            return None

        # Normalise model name: strip provider prefix, add models/ prefix
        gemini_model = model
        if "/" in gemini_model:
            gemini_model = gemini_model.split("/", 1)[1]
        if not gemini_model.startswith("models/"):
            gemini_model = f"models/{gemini_model}"

        self._pending_creates.add(prefix_hash)
        try:
            body: dict[str, Any] = {
                "model": gemini_model,
                "displayName": f"layercache-{prefix_hash[:12]}",
                "ttl": f"{ttl_seconds}s",
            }
            if system_parts:
                body["systemInstruction"] = {"parts": system_parts}
            if contents:
                body["contents"] = contents

            url = "https://generativelanguage.googleapis.com/v1beta/cachedContents"

            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    url,
                    json=body,
                    headers={"X-Goog-Api-Key": api_key},
                )
                response.raise_for_status()
                result = response.json()

            cached_content_name: str | None = result.get("name")
            if cached_content_name:
                self.mark_cache_created(prefix_hash, cached_content_name)
                return cached_content_name

            logger.warning("Gemini CachedContent API returned no name: %s", result)
        except Exception as e:
            logger.warning("Gemini CachedContent creation failed: %s", e)
        finally:
            self._pending_creates.discard(prefix_hash)

        return None

    def extract_cache_metrics(self, response: dict[str, Any]) -> dict[str, Any]:
        """Extract cache usage from Gemini's response.

        Gemini returns cache usage in the `usageMetadata` field:
        - cachedContentTokenCount: tokens read from cache
        """
        usage_metadata = response.get("usageMetadata", {})
        return {
            "cache_read_input_tokens": usage_metadata.get("cachedContentTokenCount", 0),
            "cache_creation_input_tokens": usage_metadata.get("tokensToCache", 0),
            "input_tokens": usage_metadata.get("promptTokenCount", 0),
            "output_tokens": usage_metadata.get("candidatesTokenCount", 0),
        }

    def mark_cache_created(self, prefix_hash: str, cached_content_name: str) -> None:
        """Register a newly created CachedContent resource."""
        self._cache_map[prefix_hash] = cached_content_name
        logger.info(
            "Gemini: Registered CachedContent '%s' for prefix hash %s",
            cached_content_name,
            prefix_hash[:12],
        )

    def _compute_prefix_hash(self, prompt: StratifiedPrompt) -> str:
        """Compute a hash of the L0 + L1 content for cache keying."""
        import json

        parts: list[str] = []
        for layer_type in (LayerType.SYSTEM, LayerType.CONTEXT):
            for msg in sorted(prompt.layers[layer_type], key=lambda m: m.content_hash()):
                content_str = (
                    json.dumps(msg.content, sort_keys=True, separators=(",", ":"))
                    if isinstance(msg.content, (dict, list))
                    else str(msg.content)
                )
                parts.append(f"{msg.role}:{content_str}")

        combined = "|".join(parts)
        return hashlib.sha256(combined.encode()).hexdigest()

    def _get_remaining_messages(self, prompt: StratifiedPrompt) -> list[dict]:
        """Get messages from L2, L3, and L4 (everything after the cached prefix)."""
        messages = []
        for layer_type in (LayerType.SESSION, LayerType.ENHANCEMENT, LayerType.USER):
            for msg in sorted(prompt.layers[layer_type], key=lambda m: m.content_hash()):
                messages.append({"role": msg.role, "content": msg.content})
        return messages

    @staticmethod
    def _convert_to_gemini_format(messages: list[dict]) -> list[dict]:
        """Convert OpenAI-format messages to Gemini's `contents` format.

        Gemini uses 'user' and 'model' roles instead of 'user' and 'assistant'.
        """
        role_map = {
            "user": "user",
            "assistant": "model",
            "system": "user",  # Gemini handles system via cached content
            "tool": "user",
        }

        gemini_contents = []
        for msg in messages:
            role = role_map.get(msg.get("role", "user"), "user")
            gemini_contents.append(
                {
                    "role": role,
                    "parts": [{"text": str(msg.get("content", ""))}],
                }
            )

        return gemini_contents

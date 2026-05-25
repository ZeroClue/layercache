"""Google Gemini provider adapter - handles CachedContent API for prefix caching."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from ..models import LayerType, StratifiedPrompt
from .base import BaseAdapter

logger = logging.getLogger(__name__)


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
        self._pending_creates: set[str] = set()  # hashes currently being created

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
        4. If no, send full content and trigger async cache creation
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

            # Mark for async cache creation
            if prefix_hash not in self._pending_creates:
                self._pending_creates.add(prefix_hash)
                logger.info(
                    "Gemini: Will create CachedContent for prefix hash %s in background",
                    prefix_hash[:12],
                )

        return payload

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

    def get_pending_creates(self) -> set[str]:
        """Get prefix hashes that need CachedContent creation."""
        return self._pending_creates

    def mark_cache_created(self, prefix_hash: str, cached_content_name: str) -> None:
        """Register a newly created CachedContent resource."""
        self._cache_map[prefix_hash] = cached_content_name
        self._pending_creates.discard(prefix_hash)
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
            gemini_contents.append({
                "role": role,
                "parts": [{"text": str(msg.get("content", ""))}],
            })

        return gemini_contents

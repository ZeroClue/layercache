"""Anthropic provider adapter - handles cache_control injection for Anthropic's prompt caching."""

from __future__ import annotations

import logging
from typing import Any

from ..models import LayerType, StratifiedPrompt
from .base import BaseAdapter

logger = logging.getLogger(__name__)

ANTHROPIC_MIN_CACHE_TOKENS = 1024
ANTHROPIC_MODEL_PREFIXES = ("claude", "anthropic/")


class AnthropicAdapter(BaseAdapter):
    """Adapter for Anthropic's prompt caching via cache_control markers.

    Anthropic uses ``{"cache_control": {"type": "ephemeral"}}`` on content blocks
    to mark cache breakpoints. We inject these at the L2/L3 boundary (end of stable
    prefix) to cache the entire L0+L1+L2 prefix as a single unit.

    Anthropic caches from the beginning of the prompt up to each cache_control marker.
    Markers extend the cache TTL on each hit (5 min base).

    Cache markers are only injected when:
    - L0+L1+L2 >= 1,024 tokens (Anthropic minimum)
    - Model is Anthropic Claude (auto-detected from model name)
    """

    provider_name = "anthropic"

    def inject_markers(
        self,
        prompt: StratifiedPrompt,
        payload: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Inject Anthropic cache_control markers at L2/L3 boundary.

        Only injects cache_control at the end of L2 (stable prefix), not on L0 or L1.
        This maximizes cache efficiency by caching the entire stable prefix as one unit.

        Args:
            prompt: The stratified prompt with L0-L4 layers.
            payload: The original request payload to modify.
            config: Optional config dict (not used, kept for interface compatibility).

        Returns:
            Modified payload with cache_control marker at L2/L3 boundary, or unmodified
            payload if caching conditions are not met.
        """
        model = payload.get("model", "")

        if not self._is_anthropic_model(model):
            logger.debug("Model '%s' is not Anthropic, skipping cache markers", model)
            return self._build_messages_without_cache(prompt, payload)

        prefix_tokens = prompt.stable_prefix_tokens()
        if prefix_tokens < ANTHROPIC_MIN_CACHE_TOKENS:
            logger.debug(
                "Prefix %d tokens < %d minimum, skipping cache markers",
                prefix_tokens,
                ANTHROPIC_MIN_CACHE_TOKENS,
            )
            return self._build_messages_without_cache(prompt, payload)

        logger.info(
            "Injecting cache_control at L2/L3 boundary (%d tokens >= %d minimum)",
            prefix_tokens,
            ANTHROPIC_MIN_CACHE_TOKENS,
        )

        return self._build_messages_with_cache_marker(prompt, payload)

    def extract_cache_metrics(self, response: dict[str, Any]) -> dict[str, Any]:
        """Extract cache usage from Anthropic's response.

        Anthropic returns cache metrics in the `usage` field:
        - cache_read_input_tokens: tokens served from cache (90% discount)
        - cache_creation_input_tokens: tokens written to cache (25% premium)

        Args:
            response: The raw Anthropic response.

        Returns:
            Dict with cache metrics (input_tokens, output_tokens, cache_read_input_tokens,
            cache_creation_input_tokens). Missing fields default to 0.
        """
        usage = response.get("usage", {})
        return {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
        }

    def _is_anthropic_model(self, model: str) -> bool:
        """Check if model is an Anthropic Claude model.

        Args:
            model: Model name string (e.g., 'claude-3-5-sonnet-20241022').

        Returns:
            True if model is Anthropic, False otherwise.
        """
        if not model:
            return False
        model_lower = model.lower()
        return any(model_lower.startswith(prefix) for prefix in ANTHROPIC_MODEL_PREFIXES)

    def _build_messages_without_cache(
        self,
        prompt: StratifiedPrompt,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Build messages without cache_control markers.

        Used when caching is disabled or conditions not met.

        Args:
            prompt: The stratified prompt.
            payload: The request payload.

        Returns:
            Payload with messages array (no cache_control markers).
        """
        messages = self._reassemble_with_metadata(prompt)
        formatted_messages: list[dict[str, Any]] = []

        for msg in messages:
            msg_dict: dict[str, Any] = {
                "role": msg["role"],
                "content": self._format_content(msg["content"]),
            }
            if "_layer" in msg:
                msg_dict["_layer"] = msg["_layer"]
            if "_original_index" in msg:
                msg_dict["_original_index"] = msg["_original_index"]
            formatted_messages.append(msg_dict)

        payload["messages"] = formatted_messages
        return payload

    def _build_messages_with_cache_marker(
        self,
        prompt: StratifiedPrompt,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Build messages with cache_control at L2/L3 boundary.

        Injects cache_control only on the last message of L2 (SESSION layer),
        which marks the end of the stable prefix.

        Args:
            prompt: The stratified prompt.
            payload: The request payload.

        Returns:
            Payload with messages array and cache_control at L2 boundary.
        """
        messages = self._reassemble_with_metadata(prompt)
        formatted_messages: list[dict[str, Any]] = []

        last_l2_index: int | None = None
        for i, msg in enumerate(messages):
            layer = msg.get("_layer", LayerType.USER)
            if layer == LayerType.SESSION:
                last_l2_index = i

        for i, msg in enumerate(messages):
            layer = msg.get("_layer", LayerType.USER)
            content = self._format_content(msg["content"])

            msg_dict: dict[str, Any] = {
                "role": msg["role"],
                "content": content,
            }

            if "_layer" in msg:
                msg_dict["_layer"] = msg["_layer"]
            if "_original_index" in msg:
                msg_dict["_original_index"] = msg["_original_index"]

            if i == last_l2_index:
                if isinstance(content, list):
                    content[-1]["cache_control"] = {"type": "ephemeral"}
                else:
                    msg_dict["content"] = [
                        {
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ]

            formatted_messages.append(msg_dict)

        payload["messages"] = formatted_messages

        if "system" in payload and payload["system"]:
            if isinstance(payload["system"], str):
                payload["system"] = [{"type": "text", "text": payload["system"]}]
            elif isinstance(payload["system"], list) and payload["system"]:
                pass

        return payload

    @staticmethod
    def _format_content(content: str | list[dict[str, Any]]) -> str | list[dict[str, Any]]:
        """Format content for Anthropic's API.

        Anthropic accepts either string content or list of content blocks.
        Content blocks can be 'text' or 'image' types.

        Args:
            content: String or list of content blocks.

        Returns:
            Formatted content preserving multimodal structure.
        """
        if isinstance(content, str):
            return content
        elif isinstance(content, list):
            formatted = []
            for block in content:
                if isinstance(block, dict) and "type" in block:
                    if block["type"] == "image":
                        formatted.append(block)
                    elif block["type"] == "text":
                        formatted.append(block)
                    else:
                        formatted.append({"type": "text", "text": str(block)})
                elif isinstance(block, dict) and "text" in block:
                    formatted.append({"type": "text", "text": block["text"]})
                else:
                    formatted.append({"type": "text", "text": str(block)})
            return formatted
        return str(content)

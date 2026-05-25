"""Anthropic provider adapter - handles cache_control injection for Anthropic's prompt caching."""

from __future__ import annotations

from typing import Any

from ..models import LayerType, StratifiedPrompt
from .base import BaseAdapter


class AnthropicAdapter(BaseAdapter):
    """Adapter for Anthropic's prompt caching via cache_control markers.

    Anthropic uses ``{"cache_control": {"type": "ephemeral"}}`` on content blocks
    to mark cache breakpoints. We inject these at the boundaries of stable layers
    (L0, L1, L2) to maximize cache hit rates.

    Anthropic caches from the beginning of the prompt up to each cache_control marker.
    Markers extend the cache TTL on each hit (5 min base).
    """

    provider_name = "anthropic"

    def inject_markers(
        self,
        prompt: StratifiedPrompt,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Inject Anthropic cache_control markers at L0, L1, and L2 boundaries.

        For Anthropic's API, cache_control is placed on the last content block
        of each stable layer. This ensures:
        - L0 content is cached
        - L0+L1 combined prefix is cached
        - L0+L1+L2 full prefix is cached
        """
        messages = self._reassemble_with_metadata(prompt)
        marked_messages: list[dict[str, Any]] = []

        stable_layers = {LayerType.SYSTEM, LayerType.CONTEXT, LayerType.SESSION}
        last_index_per_layer: dict[LayerType, int] = {}

        # Find the last message index for each stable layer
        for i, msg in enumerate(messages):
            layer = msg.get("_layer", LayerType.USER)
            if layer in stable_layers:
                last_index_per_layer[layer] = i

        boundary_indices = set(last_index_per_layer.values())

        for i, msg in enumerate(messages):
            layer = msg.get("_layer", LayerType.USER)

            # Build message dict for Anthropic format
            msg_dict: dict[str, Any] = {
                "role": msg["role"],
                "content": self._format_content(msg["content"]),
            }

            # Inject cache_control at the end of each stable layer block
            if i in boundary_indices:
                if isinstance(msg_dict["content"], list):
                    # Multimodal content: add cache_control to the last block
                    msg_dict["content"][-1]["cache_control"] = {"type": "ephemeral"}
                else:
                    # String content: wrap in content block list
                    msg_dict["content"] = [
                        {
                            "type": "text",
                            "text": msg["content"],
                            "cache_control": {"type": "ephemeral"},
                        }
                    ]

            marked_messages.append(msg_dict)

        payload["messages"] = marked_messages

        # Also inject cache markers on the system prompt if present
        if "system" in payload and payload["system"]:
            if isinstance(payload["system"], str):
                payload["system"] = [
                    {
                        "type": "text",
                        "text": payload["system"],
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            elif isinstance(payload["system"], list) and payload["system"]:
                # Add cache_control to the last block
                payload["system"][-1]["cache_control"] = {"type": "ephemeral"}

        return payload

    def extract_cache_metrics(self, response: dict[str, Any]) -> dict[str, Any]:
        """Extract cache usage from Anthropic's response.

        Anthropic returns cache metrics in the `usage` field:
        - cache_read_input_tokens: tokens served from cache
        - cache_creation_input_tokens: tokens written to cache
        """
        usage = response.get("usage", {})
        return {
            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        }

    @staticmethod
    def _format_content(content: str | list[dict]) -> str | list[dict]:
        """Format content for Anthropic's API.

        Anthropic requires content to be either a string or a list of content blocks.
        """
        if isinstance(content, str):
            return content
        elif isinstance(content, list):
            # Ensure each block has a 'type' field
            formatted = []
            for block in content:
                if isinstance(block, dict) and "type" in block:
                    formatted.append(block)
                elif isinstance(block, dict) and "text" in block:
                    formatted.append({"type": "text", "text": block["text"]})
                else:
                    formatted.append({"type": "text", "text": str(block)})
            return formatted
        return str(content)

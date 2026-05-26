"""Base adapter interface for provider-specific cache marker injection.

Each provider (Anthropic, OpenAI, Gemini) has a different mechanism for
leveraging prompt caching. Adapters translate the abstract L0-L4 layer
boundaries into provider-specific API parameters.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..models import LayerType, StratifiedPrompt


class BaseAdapter(ABC):
    """Abstract base class for provider-specific cache marker injection."""

    provider_name: str = "base"

    @abstractmethod
    def inject_markers(
        self,
        prompt: StratifiedPrompt,
        payload: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Inject provider-specific cache control markers into the request payload.

        Args:
            prompt: The stratified prompt with L0-L4 layers.
            payload: The original request payload to modify.
            config: Optional provider-specific config dict (e.g. use_auto_cache_control).

        Returns:
            Modified payload with provider-specific cache markers.
        """
        ...

    @abstractmethod
    def extract_cache_metrics(self, response: dict[str, Any]) -> dict[str, Any]:
        """Extract cache usage metrics from the provider's response.

        Args:
            response: The raw provider response.

        Returns:
            Dict with keys like 'cache_read_input_tokens', 'cache_creation_input_tokens'.
        """
        ...

    def _reassemble_with_metadata(
        self,
        prompt: StratifiedPrompt,
    ) -> list[dict[str, Any]]:
        """Reassemble prompt messages and attach layer metadata for downstream use."""
        messages: list[dict[str, Any]] = []
        for layer_type in sorted(LayerType, key=lambda lt: lt.sort_order):
            layer_msgs = sorted(prompt.layers[layer_type], key=lambda m: m.content_hash())
            for msg in layer_msgs:
                message_dict: dict[str, Any] = {
                    "role": msg.role,
                    "content": msg.content,
                    "_layer": layer_type,
                    "_original_index": msg.original_index,
                }
                if msg.metadata:
                    message_dict.update(msg.metadata)
                messages.append(message_dict)
        return messages

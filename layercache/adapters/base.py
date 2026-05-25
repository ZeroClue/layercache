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
    ) -> dict[str, Any]:
        """Inject provider-specific cache control markers into the request payload.

        Args:
            prompt: The stratified prompt with L0-L4 layers.
            payload: The original request payload to modify.

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

    def _get_stable_layer_boundary_indices(self, prompt: StratifiedPrompt) -> list[int]:
        """Calculate the indices of the last message in each stable layer (L0, L1, L2).

        Returns a sorted list of indices where cache breakpoints should be placed.
        """
        reassembled = prompt.reassemble()
        boundaries: list[int] = []
        current_layer = None

        stable_layers = {LayerType.SYSTEM, LayerType.CONTEXT, LayerType.SESSION}

        for i, msg in enumerate(reassembled):
            msg_layer = self._infer_layer_from_position(prompt, i, len(reassembled))
            if msg_layer in stable_layers and msg_layer != current_layer:
                # This is the start of a new stable layer; the boundary is at i
                boundaries.append(i)
                current_layer = msg_layer

        # Return the last index of each stable block (boundary + count - 1)
        # Simplified: return the last stable message index
        last_stable = -1
        for i, msg in enumerate(reassembled):
            msg_layer = self._infer_layer_from_position(prompt, i, len(reassembled))
            if msg_layer in stable_layers:
                last_stable = i

        return boundaries + ([last_stable] if last_stable >= 0 else [])

    @staticmethod
    def _infer_layer_from_position(
        prompt: StratifiedPrompt,
        index: int,
        total: int,
    ) -> LayerType:
        """Infer which layer a message at a given position in the reassembled output belongs to."""
        # Walk through the layers in order and count messages
        cumulative = 0
        for layer_type in sorted(LayerType, key=lambda lt: lt.sort_order):
            layer_msgs = sorted(prompt.layers[layer_type], key=lambda m: m.content_hash())
            layer_count = len(layer_msgs)
            if cumulative + layer_count > index:
                return layer_type
            cumulative += layer_count
        return LayerType.USER

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

"""Prompt Stratifier - Classifies messages into the L0-L4 layered architecture.

The Stratifier converts standard OpenAI-format message arrays into a StratifiedPrompt
with messages organized by layer. It supports three classification methods:

1. Template-based: L0/L1 are loaded from the Prompt Registry, client messages become L2+
2. Explicit hints: Client provides lc_layer_hints mapping message indices to layer names
3. Heuristic: Automatic classification based on role, position, and content patterns
"""

from __future__ import annotations

import re
from typing import Any

from .models import LayerType, StratifiedPrompt

# Mapping from string names (as used in lc_layer_hints) to LayerType enum values
_LAYER_NAME_MAP: dict[str, LayerType] = {
    "L0": LayerType.SYSTEM,
    "L0_SYSTEM": LayerType.SYSTEM,
    "system": LayerType.SYSTEM,
    "L1": LayerType.CONTEXT,
    "L1_CONTEXT": LayerType.CONTEXT,
    "context": LayerType.CONTEXT,
    "L2": LayerType.SESSION,
    "L2_SESSION": LayerType.SESSION,
    "session": LayerType.SESSION,
    "L3": LayerType.ENHANCEMENT,
    "L3_ENHANCEMENT": LayerType.ENHANCEMENT,
    "enhancement": LayerType.ENHANCEMENT,
    "L4": LayerType.USER,
    "L4_USER": LayerType.USER,
    "user": LayerType.USER,
}


class Stratifier:
    """Classifies incoming messages into the Layered Prompt Architecture (L0-L4).

    Classification priority:
    1. Template mode (lc_template) — L0/L1 from registry, rest auto-classified
    2. Explicit hints mode (lc_layer_hints) — client provides per-index mapping
    3. Heuristic mode — automatic classification based on role and content patterns
    """

    def __init__(self) -> None:
        self._registry = None  # Will be set by the app to the PromptRegistry instance

    def set_registry(self, registry: Any) -> None:
        """Set the prompt registry for template-based stratification."""
        self._registry = registry

    def stratify(
        self,
        messages: list[dict[str, Any]],
        template_name: str | None = None,
        layer_hints: dict[int, str] | None = None,
    ) -> StratifiedPrompt:
        """Classify messages into L0-L4 layers.

        Args:
            messages: Standard OpenAI-format message array.
            template_name: If set, load L0/L1 from the Prompt Registry.
            layer_hints: Explicit mapping of message index -> layer name string.

        Returns:
            A StratifiedPrompt with messages organized by layer.
        """
        prompt = StratifiedPrompt()

        if template_name and self._registry:
            self._stratify_with_template(prompt, messages, template_name)
        elif layer_hints:
            self._stratify_with_hints(prompt, messages, layer_hints)
        else:
            self._stratify_heuristic(prompt, messages)

        return prompt

    def _stratify_with_template(
        self,
        prompt: StratifiedPrompt,
        messages: list[dict[str, Any]],
        template_name: str,
    ) -> None:
        """Stratify using a named template from the Prompt Registry.

        Template provides L0 and L1. Client messages become L2+.
        """
        try:
            template = self._registry.get_template(template_name)
            # L0 messages from template
            for msg_data in template.get("L0", []):
                prompt.add_message(
                    LayerType.SYSTEM,
                    msg_data.get("role", "system"),
                    msg_data.get("content", ""),
                )
            # L1 messages from template
            for msg_data in template.get("L1", []):
                prompt.add_message(
                    LayerType.CONTEXT,
                    msg_data.get("role", "system"),
                    msg_data.get("content", ""),
                )
        except Exception:
            # If template not found, fall back to heuristic
            self._stratify_heuristic(prompt, messages)
            return

        # Remaining client messages -> classify as L2 or L4
        self._classify_remaining_messages(prompt, messages)

    def _stratify_with_hints(
        self,
        prompt: StratifiedPrompt,
        messages: list[dict[str, Any]],
        layer_hints: dict[int, str],
    ) -> None:
        """Stratify using explicit layer hints from the client."""
        for i, msg in enumerate(messages):
            layer_name = layer_hints.get(i)
            if layer_name and layer_name in _LAYER_NAME_MAP:
                layer = _LAYER_NAME_MAP[layer_name]
            else:
                # Fall back to heuristic for un-hinted messages
                layer = self._classify_single_message(msg, i, len(messages))
            prompt.add_message(
                layer,
                msg.get("role", "user"),
                msg.get("content", ""),
                original_index=i,
            )

    def _stratify_heuristic(
        self,
        prompt: StratifiedPrompt,
        messages: list[dict[str, Any]],
    ) -> None:
        """Stratify using automatic heuristic classification.

        Rules:
        - role=system + no prior messages -> L0 (System)
        - role=system + contains tool definitions or long context -> L1 (Context)
        - role=assistant or role=tool -> L2 (Session)
        - role=user (final message) -> L4 (User Input)
        - role=user (not final) -> L2 (Session)
        """
        for i, msg in enumerate(messages):
            layer = self._classify_single_message(msg, i, len(messages))
            prompt.add_message(
                layer,
                msg.get("role", "user"),
                msg.get("content", ""),
                original_index=i,
            )

    def _classify_single_message(
        self,
        msg: dict[str, Any],
        index: int,
        total: int,
    ) -> LayerType:
        """Classify a single message using heuristic rules."""
        role = msg.get("role", "user")
        content = msg.get("content", "")
        content_str = str(content) if content else ""

        # L0: First system message (core persona, safety rules)
        if role == "system" and index == 0:
            return LayerType.SYSTEM

        # L1: Subsequent system messages with tool definitions or long context
        if role == "system":
            if self._is_contextual(content_str):
                return LayerType.CONTEXT
            # Additional system messages after the first are usually context
            return LayerType.CONTEXT

        # L2: Assistant and tool messages (conversation history)
        if role in ("assistant", "tool"):
            return LayerType.SESSION

        # L4: Final user message (the actual query)
        if role == "user" and index == total - 1:
            return LayerType.USER

        # L2: Non-final user messages (part of conversation history)
        if role == "user":
            return LayerType.SESSION

        # Default: treat unknown roles as session
        return LayerType.SESSION

    def _classify_remaining_messages(
        self,
        prompt: StratifiedPrompt,
        messages: list[dict[str, Any]],
    ) -> None:
        """Classify client messages when template mode is used (L0/L1 already set)."""
        total = len(messages)
        for i, msg in enumerate(messages):
            role = msg.get("role", "user")
            content = msg.get("content", "")
            content_str = str(content) if content else ""

            # Skip system messages (template already provided L0/L1)
            if role == "system":
                # Additional system content that's not in template -> L1
                if self._is_contextual(content_str):
                    prompt.add_message(LayerType.CONTEXT, role, content, original_index=i)
                continue

            if role in ("assistant", "tool"):
                prompt.add_message(LayerType.SESSION, role, content, original_index=i)
            elif role == "user" and i == total - 1:
                prompt.add_message(LayerType.USER, role, content, original_index=i)
            elif role == "user":
                prompt.add_message(LayerType.SESSION, role, content, original_index=i)
            else:
                prompt.add_message(LayerType.SESSION, role, content, original_index=i)

    @staticmethod
    def _is_contextual(content: str) -> bool:
        """Check if a system message contains contextual information (tools, RAG, etc.)."""
        indicators = [
            r"tools?\s*:",
            r"function",
            r"definition",
            r"schema",
            r"knowledge\s*base",
            r"reference\s*document",
            r"few.?shot",
            r"<tool",
            r"<function",
            r"```json",
        ]
        for pattern in indicators:
            if re.search(pattern, content, re.IGNORECASE):
                return True
        # Long system messages (> 500 chars) are typically context, not core persona
        if len(content) > 500:
            return True
        return False

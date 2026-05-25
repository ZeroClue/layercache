"""Base enhancement interface and registry for prompt enhancements.

Enhancements are composable prompt engineering techniques that are injected
at L3 (between session history and user query). They NEVER alter L0-L2,
ensuring stable prefixes remain cacheable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..models import LayerType, StratifiedMessage, StratifiedPrompt


class BaseEnhancement(ABC):
    """Abstract base class for all prompt enhancements.

    Every enhancement must:
    1. Have a unique `name` for referencing via `lc_enhancements`
    2. Implement `apply()` that modifies only L3 (ENHANCEMENT layer)
    3. Never modify L0, L1, L2, or L4 content
    """

    name: str = "base"

    @abstractmethod
    def apply(self, prompt: StratifiedPrompt, **kwargs: Any) -> StratifiedPrompt:
        """Apply this enhancement to the prompt.

        Must ONLY append/modify messages in the L3 (ENHANCEMENT) layer.
        Must NOT alter L0 (System), L1 (Context), L2 (Session), or L4 (User).

        Args:
            prompt: The stratified prompt to enhance.
            **kwargs: Additional context (e.g., model name, user preferences).

        Returns:
            The modified stratified prompt (same instance, modified in-place).
        """
        ...

    def _add_enhancement_message(
        self,
        prompt: StratifiedPrompt,
        role: str,
        content: str,
        insert_at_start: bool = False,
    ) -> None:
        """Add a message to the L3 (ENHANCEMENT) layer.

        Args:
            prompt: The stratified prompt.
            role: The message role (typically 'user' or 'assistant').
            content: The message content.
            insert_at_start: If True, insert at the beginning of L3 (before other enhancements).
        """
        msg = StratifiedMessage(
            layer=LayerType.ENHANCEMENT,
            role=role,
            content=content,
            original_index=-1,  # Enhancement messages don't have original indices
        )
        if insert_at_start:
            prompt.layers[LayerType.ENHANCEMENT].insert(0, msg)
        else:
            prompt.layers[LayerType.ENHANCEMENT].append(msg)

    def _add_enhancement_pair(
        self,
        prompt: StratifiedPrompt,
        user_content: str,
        assistant_content: str,
        insert_at_start: bool = False,
    ) -> None:
        """Add a user/assistant message pair to L3.

        Args:
            prompt: The stratified prompt.
            user_content: The user's part of the example.
            assistant_content: The assistant's response.
            insert_at_start: If True, insert at the beginning of L3.
        """
        if insert_at_start:
            # Insert in reverse order at position 0
            msg_assistant = StratifiedMessage(
                layer=LayerType.ENHANCEMENT,
                role="assistant",
                content=assistant_content,
                original_index=-1,
            )
            msg_user = StratifiedMessage(
                layer=LayerType.ENHANCEMENT,
                role="user",
                content=user_content,
                original_index=-1,
            )
            prompt.layers[LayerType.ENHANCEMENT].insert(0, msg_assistant)
            prompt.layers[LayerType.ENHANCEMENT].insert(0, msg_user)
        else:
            self._add_enhancement_message(prompt, "user", user_content)
            self._add_enhancement_message(prompt, "assistant", assistant_content)


class EnhancementRegistry:
    """Registry for managing enhancement plugins.

    Enhancements can be registered programmatically or loaded from configuration.
    """

    def __init__(self) -> None:
        self._enhancements: dict[str, BaseEnhancement] = {}

    def register(self, enhancement: BaseEnhancement) -> None:
        """Register an enhancement plugin."""
        self._enhancements[enhancement.name] = enhancement

    def get(self, name: str) -> BaseEnhancement | None:
        """Get an enhancement by name."""
        return self._enhancements.get(name)

    def apply_enhancements(
        self,
        prompt: StratifiedPrompt,
        enhancement_names: list[str],
        **kwargs: Any,
    ) -> StratifiedPrompt:
        """Apply a list of named enhancements to the prompt.

        Args:
            prompt: The stratified prompt.
            enhancement_names: List of enhancement names to apply.
            **kwargs: Additional context passed to each enhancement.

        Returns:
            The enhanced prompt.
        """
        for name in enhancement_names:
            enhancement = self._enhancements.get(name)
            if enhancement:
                enhancement.apply(prompt, **kwargs)
            else:
                import logging
                logging.getLogger(__name__).warning(
                    "Unknown enhancement '%s', skipping", name
                )
        return prompt

    def list_enhancements(self) -> list[str]:
        """List all registered enhancement names."""
        return list(self._enhancements.keys())

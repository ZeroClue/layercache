"""Chain of Thought enhancement.

Instructs the LLM to think step-by-step before answering,
improving reasoning quality for complex problems.
"""

from __future__ import annotations

from typing import Any

from ..models import StratifiedPrompt
from .base import BaseEnhancement


class ChainOfThoughtEnhancement(BaseEnhancement):
    """Injects a Chain of Thought instruction at L3.

    This enhancement adds a user message requesting step-by-step reasoning
    before the final answer. It does NOT alter the stable prefix (L0-L2).
    """

    name = "chain_of_thought"

    DEFAULT_INSTRUCTION = (
        "Before providing your final answer, please think through this step by step. "
        "Break down the problem, consider relevant factors, and show your reasoning process."
    )

    def __init__(self, instruction: str | None = None) -> None:
        """Initialize the CoT enhancement.

        Args:
            instruction: Custom CoT instruction. If None, uses default.
        """
        self._instruction = instruction or self.DEFAULT_INSTRUCTION

    def apply(self, prompt: StratifiedPrompt, **kwargs: Any) -> StratifiedPrompt:
        """Apply Chain of Thought instruction at the beginning of L3."""
        self._add_enhancement_message(
            prompt,
            role="user",
            content=self._instruction,
            insert_at_start=True,
        )
        self._add_enhancement_message(
            prompt,
            role="assistant",
            content="Understood, I will reason through this step by step.",
            insert_at_start=True,
        )
        return prompt

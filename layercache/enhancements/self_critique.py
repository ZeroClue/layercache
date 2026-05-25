"""Self-Critique enhancement.

Instructs the LLM to review and improve its own response before
returning it, enhancing output quality.
"""

from __future__ import annotations

from typing import Any

from ..models import StratifiedPrompt
from .base import BaseEnhancement


class SelfCritiqueEnhancement(BaseEnhancement):
    """Injects a self-critique instruction at L3.

    The LLM is instructed to:
    1. Generate an initial response
    2. Critically review it for errors, gaps, or improvements
    3. Provide a refined final response

    This is injected as a user/assistant pair at L3.
    """

    name = "self_critique"

    CRITIQUE_INSTRUCTION = (
        "After drafting your response, please perform a self-critique:\n"
        "1. Review your answer for factual accuracy and logical consistency.\n"
        "2. Check for any gaps or missing considerations.\n"
        "3. Identify areas that could be clearer or more helpful.\n"
        "4. Provide your improved, final response.\n\n"
        "Format your response as:\n"
        "- **Initial Analysis:** [your initial thoughts]\n"
        "- **Critique:** [what you found that could be improved]\n"
        "- **Final Response:** [your refined answer]"
    )

    def apply(self, prompt: StratifiedPrompt, **kwargs: Any) -> StratifiedPrompt:
        """Apply self-critique instruction at the beginning of L3."""
        self._add_enhancement_message(
            prompt,
            role="user",
            content=self.CRITIQUE_INSTRUCTION,
            insert_at_start=True,
        )
        self._add_enhancement_message(
            prompt,
            role="assistant",
            content="Understood, I will analyze, critique, and refine my response.",
            insert_at_start=True,
        )
        return prompt

"""Structured Output enhancement.

Enforces JSON schema compliance in the LLM response by injecting
format instructions at L3.
"""

from __future__ import annotations

import json
from typing import Any

from ..models import StratifiedPrompt
from .base import BaseEnhancement


class StructuredOutputEnhancement(BaseEnhancement):
    """Injects JSON output format instructions at L3.

    If a JSON schema is provided, it includes the schema in the instruction.
    Otherwise, it uses a generic JSON output instruction.
    """

    name = "structured_json"

    DEFAULT_INSTRUCTION = (
        "You must respond with valid JSON only. "
        "Do not include any text outside of the JSON structure. "
        "Ensure your response is properly formatted "
        "and can be parsed by a standard JSON parser."
    )

    SCHEMA_INSTRUCTION = (
        "You must respond with valid JSON that conforms to the following schema. "
        "Do not include any text outside of the JSON structure.\n\n"
        "Response Schema:\n```json\n{schema}\n```"
    )

    def __init__(self, schema: dict[str, Any] | None = None) -> None:
        """Initialize the structured output enhancement.

        Args:
            schema: Optional JSON schema to enforce. If None, uses generic instruction.
        """
        self._schema = schema

    def apply(self, prompt: StratifiedPrompt, **kwargs: Any) -> StratifiedPrompt:
        """Apply structured output instruction at the beginning of L3."""
        if self._schema:
            schema_str = json.dumps(self._schema, indent=2, ensure_ascii=False)
            instruction = self.SCHEMA_INSTRUCTION.format(schema=schema_str)
        else:
            instruction = self.DEFAULT_INSTRUCTION

        # Allow runtime schema override via kwargs
        runtime_schema = kwargs.get("schema")
        if runtime_schema:
            schema_str = json.dumps(runtime_schema, indent=2, ensure_ascii=False)
            instruction = self.SCHEMA_INSTRUCTION.format(schema=schema_str)

        self._add_enhancement_message(
            prompt,
            role="user",
            content=instruction,
            insert_at_start=True,
        )
        self._add_enhancement_message(
            prompt,
            role="assistant",
            content="Understood, I will respond with valid JSON only.",
            insert_at_start=True,
        )
        return prompt

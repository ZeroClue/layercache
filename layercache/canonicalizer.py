"""Prompt Canonicalizer - Normalizes prompts for maximum prefix cache hits.

The Canonicalizer performs non-semantic transformations to ensure that
functionally identical prompts produce byte-for-byte identical output.
This is critical for provider-level token caching (Anthropic, OpenAI, Gemini).

Canonicalization rules:
- Tool sorting: Sort tools array alphabetically by function.name
- JSON normalization: Minify JSON strings (no extra whitespace)
- Whitespace normalization: strip() all content, collapse multiple newlines
- Deterministic ordering: Sort messages within the same layer by content hash
"""

from __future__ import annotations

import json
import re
from typing import Any

from .models import LayerType, StratifiedPrompt
from .serializers.tool_serializer import ToolSerializer


class Canonicalizer:
    """Normalizes prompt content for deterministic, cache-friendly output.

    All transformations are strictly non-semantic — they never alter the
    meaning of the prompt, only its formatting.
    """

    # Regex for collapsing multiple consecutive newlines (3+ -> 2)
    _MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
    # Regex for collapsing multiple consecutive spaces (2+ -> 1)
    _MULTI_SPACE_RE = re.compile(r" {2,}")

    def canonicalize(
        self,
        prompt: StratifiedPrompt,
        tools: list[dict[str, Any]] | None = None,
    ) -> tuple[StratifiedPrompt, list[dict[str, Any]] | None]:
        """Apply all canonicalization rules to a stratified prompt and its tools.

        Args:
            prompt: The stratified prompt to canonicalize.
            tools: Optional list of OpenAI-format tool definitions.

        Returns:
            Tuple of (canonicalized prompt, canonicalized tools).
        """
        # Canonicalize message content within each layer
        for layer_type in LayerType:
            messages = prompt.layers[layer_type]
            for msg in messages:
                msg.content = self._canonicalize_content(msg.content)

        # Canonicalize tool definitions
        canonical_tools = None
        if tools:
            canonical_tools = self._canonicalize_tools(tools)

        return prompt, canonical_tools

    def _canonicalize_content(self, content: str | list[dict]) -> str | list[dict]:
        """Canonicalize the content of a single message.

        Handles both string content and multimodal content arrays.
        """
        if isinstance(content, str):
            return self._canonicalize_string(content)
        elif isinstance(content, list):
            return self._canonicalize_content_array(content)
        return content

    def _canonicalize_string(self, text: str) -> str:
        """Apply whitespace and formatting normalization to a string."""
        # Strip leading/trailing whitespace
        text = text.strip()
        # Collapse 3+ consecutive newlines into 2
        text = self._MULTI_NEWLINE_RE.sub("\n\n", text)
        # Collapse 2+ consecutive spaces into 1 (but preserve newlines)
        lines = text.split("\n")
        lines = [self._MULTI_SPACE_RE.sub(" ", line) for line in lines]
        text = "\n".join(lines)
        # Strip trailing whitespace from each line
        lines = [line.rstrip() for line in text.split("\n")]
        return "\n".join(lines)

    def _canonicalize_content_array(self, content: list[dict]) -> list[dict]:
        """Canonicalize a multimodal content array (text + image blocks)."""
        result = []
        for block in content:
            block = dict(block)  # shallow copy
            if block.get("type") == "text" and "text" in block:
                block["text"] = self._canonicalize_string(block["text"])
            result.append(block)
        return result

    def _canonicalize_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Canonicalize tool definitions for deterministic output.

        Uses ToolSerializer for deterministic serialization:
        - Sort tools alphabetically by function.name
        - Sort all dict keys recursively
        - Normalize float representations
        - Minify all JSON strings within function parameters
        - Normalize whitespace in descriptions

        Args:
            tools: List of tool definitions.

        Returns:
            Canonicalized list of tools.
        """
        if not tools:
            return tools

        # Use ToolSerializer for deterministic serialization
        serialized = ToolSerializer.serialize_tools_deterministic(tools)

        # Parse back to dict for downstream use
        return json.loads(serialized)

    @staticmethod
    def _minify_json(obj: Any) -> Any:
        """Recursively minify JSON-compatible objects by removing extra whitespace.

        For strings that are JSON objects/arrays, parse and re-serialize with
        minimal separators.
        """
        if isinstance(obj, str):
            # Try to parse as JSON; if successful, re-serialize minimally
            try:
                parsed = json.loads(obj)
                return json.dumps(parsed, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
            except (json.JSONDecodeError, ValueError):
                return obj
        elif isinstance(obj, dict):
            return {k: Canonicalizer._minify_json(v) for k, v in sorted(obj.items())}
        elif isinstance(obj, list):
            return [Canonicalizer._minify_json(item) for item in obj]
        return obj

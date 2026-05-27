"""Tool Serializer - Deterministic serialization for tool schemas.

This module provides deterministic serialization of tool definitions to ensure
byte-identical JSON output for semantically-identical tool definitions.

Key features:
- Sort tools by name
- Sort all dict keys recursively
- Normalize float representations
- Handle regex patterns consistently
- Compute stable tool hashes for cache key inclusion
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from typing import Any


class ToolSerializer:
    """Deterministic serializer for tool definitions.

    Ensures byte-identical JSON output for semantically-identical tool
    definitions, enabling maximum prefix cache hits.
    """

    @staticmethod
    def serialize_tools_deterministic(tools: list[dict[str, Any]]) -> str:
        """Serialize tools to byte-identical JSON string.

        Args:
            tools: List of tool definitions in OpenAI format.

        Returns:
            Deterministically serialized JSON string.
        """
        if not tools:
            return ""

        # Sort tools by name
        sorted_tools = sorted(tools, key=lambda t: t.get("function", {}).get("name", ""))

        # Normalize each tool
        normalized_tools = [ToolSerializer._normalize_tool(tool) for tool in sorted_tools]

        # Serialize with sorted keys and minimal whitespace
        return json.dumps(
            normalized_tools,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    @staticmethod
    def _normalize_tool(tool: dict[str, Any]) -> dict[str, Any]:
        """Normalize a single tool definition.

        Args:
            tool: Tool definition dict.

        Returns:
            Normalized tool definition with sorted keys and normalized values.
        """
        normalized = dict(tool)

        if "function" in normalized:
            func = dict(normalized["function"])

            # Normalize function description
            if "description" in func and isinstance(func["description"], str):
                func["description"] = func["description"].strip()

            # Normalize parameters
            if "parameters" in func and isinstance(func["parameters"], dict):
                func["parameters"] = ToolSerializer._normalize_dict(func["parameters"])

            normalized["function"] = func

        return normalized

    @staticmethod
    def _normalize_dict(value: Any) -> Any:
        """Recursively normalize a dictionary.

        - Sort keys alphabetically
        - Normalize floats
        - Handle nested structures

        Args:
            value: Dictionary or value to normalize.

        Returns:
            Normalized value with sorted keys and normalized floats.
        """
        if isinstance(value, dict):
            return {k: ToolSerializer._normalize_dict(v) for k, v in sorted(value.items())}
        elif isinstance(value, list):
            return [ToolSerializer._normalize_dict(item) for item in value]
        elif isinstance(value, float):
            return ToolSerializer._normalize_float(value)
        elif isinstance(value, str):
            # Try to normalize floats in strings (e.g., "0.50" -> "0.5")
            return ToolSerializer._normalize_float_string(value)
        return value

    @staticmethod
    def _normalize_float(value: float) -> float:
        """Normalize float representation.

        Converts to Decimal for precise representation, then back to float.
        This ensures 0.50 == 0.5 in serialization.

        Args:
            value: Float value to normalize.

        Returns:
            Normalized float value.
        """
        # Use Decimal to normalize representation
        # This handles cases like 0.50 vs 0.5
        try:
            decimal_value = Decimal(str(value))
            # Normalize to remove trailing zeros
            normalized = decimal_value.normalize()
            return float(normalized)
        except Exception:
            return value

    @staticmethod
    def _normalize_float_string(value: str) -> str:
        """Normalize float representations in strings.

        Converts strings that look like floats to normalized form.
        E.g., "0.50" -> "0.5", "1.000" -> "1.0"

        Args:
            value: String value to check.

        Returns:
            Normalized string or original if not a float.
        """
        # Match float patterns
        float_pattern = r"^-?\d+\.\d+$"
        if re.match(float_pattern, value):
            try:
                normalized = ToolSerializer._normalize_float(float(value))
                return str(normalized)
            except Exception:
                return value
        return value

    @staticmethod
    def compute_tool_hash(tools: list[dict[str, Any]] | None) -> str:
        """Compute a deterministic hash of tool definitions.

        Args:
            tools: List of tool definitions, or None.

        Returns:
            SHA-256 hash of deterministically serialized tools.
        """
        if not tools:
            # Return hash of empty string for no tools
            return hashlib.sha256(b"").hexdigest()

        serialized = ToolSerializer.serialize_tools_deterministic(tools)
        return hashlib.sha256(serialized.encode()).hexdigest()

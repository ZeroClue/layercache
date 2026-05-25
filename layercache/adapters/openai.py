"""OpenAI provider adapter - ensures prefix structure for automatic caching."""

from __future__ import annotations

from typing import Any

from ..models import StratifiedPrompt
from .base import BaseAdapter


class OpenAIAdapter(BaseAdapter):
    """Adapter for OpenAI's automatic prompt caching.

    OpenAI automatically caches the prefix of prompt requests. LayerCache's
    job is purely Canonicalization — ensuring L0-L2 is byte-for-byte identical
    across requests. No explicit cache markers are needed.

    OpenAI caches:
    - Prefixes of at least 1024 tokens
    - Cache TTL is managed automatically
    - Cache metrics are returned in the `usage` field
    """

    provider_name = "openai"

    def inject_markers(
        self,
        prompt: StratifiedPrompt,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Prepare the payload for OpenAI's automatic prefix caching.

        The key responsibility is ensuring the reassembled message array
        places all stable content (L0-L2) at the beginning in a deterministic
        order. OpenAI handles caching automatically.
        """
        messages = prompt.reassemble()
        payload["messages"] = messages

        # OpenAI dev messages can use developer role for system-level content
        # Some newer models use "developer" instead of "system"
        # We keep "system" as-is since our canonicalizer produces standard format

        return payload

    def extract_cache_metrics(self, response: dict[str, Any]) -> dict[str, Any]:
        """Extract cache usage from OpenAI's response.

        OpenAI returns cache metrics in the `usage` field:
        - cached_tokens: tokens served from cache (if available)
        """
        usage = response.get("usage", {})
        return {
            "cache_read_input_tokens": usage.get("cached_tokens", 0),
            "cache_creation_input_tokens": 0,
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        }

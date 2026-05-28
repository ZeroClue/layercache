"""Core data models for LayerCache prompt stratification and request handling."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class LayerType(StrEnum):
    """Prompt layer types in the Layered Prompt Architecture.

    Layers are ordered from most stable (L0) to most dynamic (L4).
    Cache breakpoints are set at the boundaries of stable layers (L0-L2).
    """

    SYSTEM = "L0_SYSTEM"
    CONTEXT = "L1_CONTEXT"
    SESSION = "L2_SESSION"
    ENHANCEMENT = "L3_ENHANCEMENT"
    USER = "L4_USER"

    @property
    def is_cacheable(self) -> bool:
        """Whether this layer's content should be cached by the provider."""
        return self in (LayerType.SYSTEM, LayerType.CONTEXT, LayerType.SESSION)

    @property
    def sort_order(self) -> int:
        """Numeric sort order for deterministic reassembly."""
        order = {
            LayerType.SYSTEM: 0,
            LayerType.CONTEXT: 1,
            LayerType.SESSION: 2,
            LayerType.ENHANCEMENT: 3,
            LayerType.USER: 4,
        }
        return order[self]


class StratifiedMessage(BaseModel):
    """A single message assigned to a specific prompt layer."""

    layer: LayerType
    role: str
    content: str | list[dict]
    original_index: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)

    def content_hash(self) -> str:
        """Generate a deterministic hash of this message's content for sorting."""
        import hashlib
        import json

        content_str = (
            json.dumps(self.content, sort_keys=True, separators=(",", ":"))
            if isinstance(self.content, (dict, list))
            else str(self.content)
        )
        return hashlib.sha256(f"{self.role}:{content_str}".encode()).hexdigest()[:16]


class StratifiedPrompt(BaseModel):
    """A prompt stratified into the L0-L4 layered architecture.

    This is the core internal representation used by all LayerCache components.
    Messages are organized by layer, allowing independent manipulation of
    stable (cached) and dynamic (uncached) sections.
    """

    layers: dict[LayerType, list[StratifiedMessage]] = Field(
        default_factory=lambda: {lt: [] for lt in LayerType}
    )
    session_id: str | None = Field(
        default=None,
        description="Session ID for metadata/logging (not included in prefix_hash)",
    )

    def add_message(
        self,
        layer: LayerType,
        role: str,
        content: str | list[dict],
        original_index: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Add a message to a specific layer."""
        msg = StratifiedMessage(
            layer=layer,
            role=role,
            content=content,
            original_index=original_index,
            metadata=metadata or {},
        )
        self.layers[layer].append(msg)

    def reassemble(self) -> list[dict[str, Any]]:
        """Flatten layers back into standard OpenAI message format (L0 -> L4).

        L0 and L1 are sorted by content hash for deterministic ordering
        (stable cache layers). L2-L4 are sorted by original_index to
        preserve message order (critical for tool_call/tool sequences).
        """
        messages: list[dict[str, Any]] = []
        for layer_type in sorted(LayerType, key=lambda lt: lt.sort_order):
            if layer_type in (LayerType.SYSTEM, LayerType.CONTEXT):
                layer_msgs = sorted(
                    self.layers[layer_type],
                    key=lambda m: m.content_hash(),
                )
            else:
                layer_msgs = sorted(
                    self.layers[layer_type],
                    key=lambda m: m.original_index,
                )
            for msg in layer_msgs:
                message_dict: dict[str, Any] = {"role": msg.role, "content": msg.content}
                if msg.metadata:
                    message_dict.update(msg.metadata)
                messages.append(message_dict)
        return messages

    def get_layer_content(self, layer: LayerType) -> list[StratifiedMessage]:
        """Get all messages in a specific layer."""
        return self.layers.get(layer, [])

    @staticmethod
    def _normalize_content(content: str) -> str:
        """Normalize content before hashing for cross-session matching.

        Applies whitespace normalization and redacts common
        session-specific metadata (timestamps, IDs, etc.).
        """
        import re

        content = re.sub(r"\s+", " ", content).strip()
        content = re.sub(
            r"(?i)(timestamp|date|time|session[_-]?id|request[_-]?id):\s*\S+",
            r"\1:__REDACTED__",
            content,
        )
        return content

    def prefix_hash(self, max_l0_tokens: int | None = None) -> str:
        """Generate a SHA-256 hash of the stable prefix (L0 + L1).

        Used as the exact-match key for the semantic cache.
        L2 (session history), session_id, and tools are excluded from the hash
        to enable cross-conversation cache hits. Provider KV caching
        handles intra-session token-level prefix reuse.

        If max_l0_tokens is set, L0 content is truncated to the first N tokens
        before hashing. This excludes per-project context (CLAUDE.md, AGENTS.md)
        that is appended after the stable boilerplate, enabling cross-project
        cache hits without a template registry.

        Args:
            max_l0_tokens: Maximum tokens to include from L0 content.
                Variable content appended after this limit is excluded from
                the hash.

        Returns:
            SHA-256 hash of the stable prefix.
        """
        import hashlib
        import json

        stable_layers = [LayerType.SYSTEM, LayerType.CONTEXT]
        prefix_content: list[str] = []

        for lt in stable_layers:
            for msg in sorted(self.layers[lt], key=lambda m: m.content_hash()):
                content_str = (
                    json.dumps(msg.content, sort_keys=True, separators=(",", ":"))
                    if isinstance(msg.content, (dict, list))
                    else str(msg.content)
                )
                content_str = self._normalize_content(content_str)

                # Truncate L0 to first N tokens to exclude per-project context
                if max_l0_tokens is not None and lt == LayerType.SYSTEM:
                    import tiktoken

                    encoding = tiktoken.get_encoding("cl100k_base")
                    tokens = encoding.encode(content_str)
                    if len(tokens) > max_l0_tokens:
                        truncated = encoding.decode(tokens[:max_l0_tokens])
                        content_str = truncated

                prefix_content.append(f"{msg.role}:{content_str}")

        combined = "|".join(prefix_content)
        return hashlib.sha256(combined.encode()).hexdigest()

    def stable_prefix_tokens(self) -> int:
        """Count tokens in the stable prefix (L0 + L1 + L2).

        Used to validate cache eligibility (Anthropic requires ≥1,024 tokens).
        """
        from .truncation import TokenCounter

        counter = TokenCounter()
        stable_layers = [LayerType.SYSTEM, LayerType.CONTEXT, LayerType.SESSION]
        total = 0

        for lt in stable_layers:
            for msg in self.layers[lt]:
                total += counter.count_messages([msg])

        return total

    def get_user_query(self) -> str:
        """Extract the user's query from L4."""
        user_msgs = self.layers[LayerType.USER]
        if not user_msgs:
            return ""
        last_user = user_msgs[-1]
        return str(last_user.content)

    def clone(self) -> StratifiedPrompt:
        """Create a deep copy of this stratified prompt."""
        import copy

        return copy.deepcopy(self)


class LayerCacheRequest(BaseModel):
    """Extended request payload with LayerCache-specific directives.

    Clients can use standard OpenAI SDKs and pass LayerCache directives
    via the `extra_body` parameter.
    """

    # Standard OpenAI fields
    model: str = Field(max_length=256)
    messages: list[dict[str, Any]]
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stream: bool = False
    tools: list[dict[str, Any]] | None = Field(default=None, max_length=256)
    tool_choice: str | dict[str, Any] | None = None
    response_format: dict[str, Any] | None = None

    # Additional OpenAI-compatible fields
    user: str | None = None
    stop: str | list[str] | None = None

    # LayerCache Extensions
    lc_template: str | None = Field(default=None, max_length=128)
    lc_enhancements: list[str] = Field(default_factory=list, max_length=32)
    lc_cache_ttl: int = 300
    lc_layer_hints: dict[int, str] | None = Field(default=None, max_length=512)
    lc_skip_semantic_cache: bool = Field(
        default=False,
        description="Skip semantic cache lookup but still store result (cache warming)",
    )
    lc_bypass_cache: bool = Field(
        default=False,
        description="Skip semantic cache lookup AND don't store result (full bypass)",
    )
    lc_session_id: str | None = Field(
        default=None,
        max_length=128,
        description="Session ID for cache isolation (from X-Session-ID header or auto-generated)",
    )


class CacheEntry(BaseModel):
    """A single semantic cache entry."""

    id: str | None = None
    prefix_hash: str
    query_text: str
    query_embedding: list[float] | None = None
    response_payload: dict[str, Any]
    model: str
    tool_hash: str = ""
    ttl_expires_at: float
    created_at: float = 0.0

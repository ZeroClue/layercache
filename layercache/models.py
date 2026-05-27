"""Core data models for LayerCache prompt stratification and request handling."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .serializers.tool_serializer import ToolSerializer


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
        description="Session ID for cache isolation (included in prefix_hash if set)",
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

        Messages within the same layer are sorted by content hash for
        deterministic ordering, ensuring maximum prefix cache hits.
        """
        messages: list[dict[str, Any]] = []
        for layer_type in sorted(LayerType, key=lambda lt: lt.sort_order):
            layer_msgs = sorted(self.layers[layer_type], key=lambda m: m.content_hash())
            for msg in layer_msgs:
                message_dict: dict[str, Any] = {"role": msg.role, "content": msg.content}
                if msg.metadata:
                    message_dict.update(msg.metadata)
                messages.append(message_dict)
        return messages

    def get_layer_content(self, layer: LayerType) -> list[StratifiedMessage]:
        """Get all messages in a specific layer."""
        return self.layers.get(layer, [])

    def prefix_hash(self, tools: list[dict] | None = None) -> str:
        """Generate a SHA-256 hash of the stable prefix (L0 + L1 + L2).

        Used as the exact-match key for the semantic cache.
        If session_id is set, it's included in the hash for session isolation.
        If tools are provided, their deterministic hash is included for tool-aware caching.

        Args:
            tools: Optional list of tool definitions. If provided, included in hash.

        Returns:
            SHA-256 hash of the stable prefix including tools if provided.
        """
        import hashlib
        import json

        stable_layers = [LayerType.SYSTEM, LayerType.CONTEXT, LayerType.SESSION]
        prefix_content: list[str] = []

        # Include session_id in hash if set (for session isolation)
        if self.session_id:
            prefix_content.append(f"_session:{self.session_id}")

        for lt in stable_layers:
            for msg in sorted(self.layers[lt], key=lambda m: m.content_hash()):
                content_str = (
                    json.dumps(msg.content, sort_keys=True, separators=(",", ":"))
                    if isinstance(msg.content, (dict, list))
                    else str(msg.content)
                )
                prefix_content.append(f"{msg.role}:{content_str}")

        # Include tool_hash if tools are provided
        if tools:
            tool_hash = ToolSerializer.compute_tool_hash(tools)
            prefix_content.append(f"_tools:{tool_hash}")

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
    messages: list[dict[str, Any]] = Field(max_length=512)
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
    ttl_expires_at: float
    created_at: float = 0.0

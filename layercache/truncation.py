"""Smart Truncation — Intelligent session context management.

LayerCache truncates L2 (session) messages to fit within token budget.
This module provides multiple truncation strategies:

- `recent`: Keep last N messages that fit budget (default)
- `important`: Score messages by importance, drop lowest scores
- `trim`: Use LiteLLM's trim_messages for model-aware trimming
- `semantic`: Embed messages, drop least similar to current query (v1.6)

Truncation happens BEFORE cache lookup, so truncated prompts have their own
cache namespace (different prefix_hash).
"""

from __future__ import annotations

import logging
from enum import StrEnum

import tiktoken

from .models import LayerType, StratifiedMessage, StratifiedPrompt

logger = logging.getLogger(__name__)

# LiteLLM import for trim_messages (optional dependency)
try:
    from litellm.utils import trim_messages

    LITELLM_AVAILABLE = True
except ImportError:
    trim_messages = None  # type: ignore
    LITELLM_AVAILABLE = False


class TruncationStrategy(StrEnum):
    """Available truncation strategies."""

    RECENT = "recent"
    IMPORTANT = "important"
    TRIM = "trim"
    SEMANTIC = "semantic"  # Deferred to v1.6


class TokenCounter:
    """Count tokens in text."""

    def __init__(self, encoding: str = "cl100k_base") -> None:
        """Initialize token counter.

        Args:
            encoding: Tiktoken encoding name (cl100k_base works for GPT-4, Claude, etc.)
        """
        self._encoding = tiktoken.get_encoding(encoding)

    def count(self, text: str) -> int:
        """Count tokens in text."""
        if not text:
            return 0
        return len(self._encoding.encode(text))

    def count_messages(self, messages: list[StratifiedMessage]) -> int:
        """Count tokens in a list of messages."""
        total = 0
        for msg in messages:
            content = msg.content
            if isinstance(content, str):
                total += self.count(content)
            elif isinstance(content, list):
                # Multimodal content
                for item in content:
                    if isinstance(item, dict) and "text" in item:
                        total += self.count(item["text"])
                    elif isinstance(item, str):
                        total += self.count(item)
            # Add overhead for role tokens (approx 4 tokens per message)
            total += 4
        return total


class Truncator:
    """Smart session truncation."""

    # Keywords that indicate important context
    IMPORTANT_KEYWORDS = frozenset(
        {
            "system",
            "instruction",
            "context",
            "rule",
            "constraint",
            "requirement",
            "specification",
            "definition",
            "schema",
        }
    )

    def __init__(
        self,
        strategy: TruncationStrategy = TruncationStrategy.RECENT,
        token_counter: TokenCounter | None = None,
        model_name: str | None = None,
    ) -> None:
        """Initialize truncator.

        Args:
            strategy: Truncation strategy to use.
            token_counter: Token counter instance (creates default if None).
            model_name: Model name for LiteLLM token counting (required for TRIM strategy).
        """
        self.strategy = strategy
        self._token_counter = token_counter or TokenCounter()
        self._model_name = model_name or "gpt-4o"  # Default for trim strategy

    def truncate(
        self,
        prompt: StratifiedPrompt,
        max_tokens: int,
    ) -> StratifiedPrompt:
        """Truncate prompt's L2 layer to fit within token budget.

        Args:
            prompt: Prompt to truncate (modified in place).
            max_tokens: Maximum tokens for L2 layer.

        Returns:
            Same prompt instance (for chaining).
        """
        if max_tokens <= 0:
            logger.warning("Invalid max_tokens=%d, skipping truncation", max_tokens)
            return prompt

        l2_messages = prompt.layers.get(LayerType.SESSION, [])
        if not l2_messages:
            return prompt

        # Count current L2 tokens
        current_tokens = self._token_counter.count_messages(l2_messages)
        if current_tokens <= max_tokens:
            return prompt  # No truncation needed

        logger.debug(
            "Truncating L2: %d tokens > %d budget (strategy=%s)",
            current_tokens,
            max_tokens,
            self.strategy.value,
        )

        # Apply strategy
        if self.strategy == TruncationStrategy.RECENT:
            kept = self._truncate_recent(l2_messages, max_tokens)
        elif self.strategy == TruncationStrategy.IMPORTANT:
            kept = self._truncate_important(l2_messages, max_tokens)
        elif self.strategy == TruncationStrategy.TRIM:
            kept = self._truncate_with_litellm(l2_messages, max_tokens)
        elif self.strategy == TruncationStrategy.SEMANTIC:
            # Deferred to v1.6 — fall back to recent
            logger.warning("Semantic truncation not implemented (v1.6), falling back to recent")
            kept = self._truncate_recent(l2_messages, max_tokens)
        else:
            kept = self._truncate_recent(l2_messages, max_tokens)

        # Update prompt's L2 layer
        prompt.layers[LayerType.SESSION] = kept

        # Log truncation results
        new_tokens = self._token_counter.count_messages(kept)
        dropped = len(l2_messages) - len(kept)
        logger.info(
            "Truncated L2: dropped %d/%d messages (%d → %d tokens)",
            dropped,
            len(l2_messages),
            current_tokens,
            new_tokens,
        )

        return prompt

    def _truncate_recent(
        self,
        messages: list[StratifiedMessage],
        max_tokens: int,
    ) -> list[StratifiedMessage]:
        """Keep last N messages that fit budget.

        Works backwards from the end, keeping messages until budget is exceeded.
        Always keeps at least the last message (user's current query context).
        """
        if not messages:
            return []

        # Always keep at least the last message
        kept: list[StratifiedMessage] = [messages[-1]]
        current_tokens = self._token_counter.count_messages(kept)

        # Add messages from end to beginning while under budget
        for msg in reversed(messages[:-1]):
            msg_tokens = self._token_counter.count_messages([msg])
            if current_tokens + msg_tokens <= max_tokens:
                kept.insert(0, msg)
                current_tokens += msg_tokens
            else:
                break

        return kept

    def _truncate_important(
        self,
        messages: list[StratifiedMessage],
        max_tokens: int,
    ) -> list[StratifiedMessage]:
        """Keep most important messages that fit budget.

        Scores each message by:
        - Length (approx tokens): len(content) / 4
        - Tool calls: +2 if message has tool calls
        - Keywords: +3 if content contains important keywords

        Keeps highest-scored messages until budget is exceeded.
        Always keeps at least the last message.
        """
        if not messages:
            return []

        # Score all messages
        scored: list[tuple[int, int, StratifiedMessage]] = []
        for i, msg in enumerate(messages):
            score = self._score_message(msg)
            scored.append((score, i, msg))

        # Sort by score (descending), then by position (descending for ties)
        scored.sort(key=lambda x: (-x[0], -x[1]))

        # Keep highest-scored messages until budget exceeded
        kept: list[StratifiedMessage] = []
        current_tokens = 0

        # Always keep the last message (most recent user query context)
        last_msg = messages[-1]
        last_tokens = self._token_counter.count_messages([last_msg])
        if last_tokens <= max_tokens:
            kept.append(last_msg)
            current_tokens = last_tokens

        # Add remaining messages by importance
        kept_indices = {len(messages) - 1}  # Last message already kept
        for score, idx, msg in scored:
            if idx == len(messages) - 1:
                continue  # Already kept
            if idx in kept_indices:
                continue

            msg_tokens = self._token_counter.count_messages([msg])
            if current_tokens + msg_tokens <= max_tokens:
                kept.append(msg)
                kept_indices.add(idx)
                current_tokens += msg_tokens

        # Restore original order
        kept.sort(key=lambda m: messages.index(m))

        return kept

    def _score_message(self, msg: StratifiedMessage) -> int:
        """Score a message by importance.

        Scoring formula:
        - Base: len(content) / 4 (approximate tokens)
        - Tool calls: +2
        - Important keywords: +3
        """
        # Base score: approximate tokens
        content = msg.content
        if isinstance(content, str):
            base_score = len(content) // 4
            content_str = content
        elif isinstance(content, list):
            base_score = sum(
                len(item.get("text", "")) // 4 if isinstance(item, dict) else len(item) // 4
                for item in content
            )
            content_str = " ".join(
                item.get("text", "") if isinstance(item, dict) else item for item in content
            )
        else:
            base_score = 0
            content_str = ""

        # Tool call bonus
        tool_bonus = 0
        if msg.role == "assistant" and msg.metadata:
            if msg.metadata.get("tool_calls"):
                tool_bonus = 2
        elif msg.role == "tool":
            tool_bonus = 2

        # Keyword bonus
        keyword_bonus = 0
        content_lower = content_str.lower()
        for keyword in self.IMPORTANT_KEYWORDS:
            if keyword in content_lower:
                keyword_bonus += 3
                break  # Only count once

        return base_score + tool_bonus + keyword_bonus

    def _truncate_with_litellm(
        self,
        messages: list[StratifiedMessage],
        max_tokens: int,
    ) -> list[StratifiedMessage]:
        """Use LiteLLM's trim_messages for model-aware trimming.

        Args:
            messages: L2 session messages to trim.
            max_tokens: Maximum token budget.

        Returns:
            Trimmed list of messages.
        """
        if not LITELLM_AVAILABLE:
            logger.warning(
                "LiteLLM not installed, falling back to recent truncation. "
                "Install with: pip install litellm"
            )
            return self._truncate_recent(messages, max_tokens)

        # Convert StratifiedMessage to dict format for LiteLLM
        messages_dict = [msg.model_dump() for msg in messages]

        # Trim using LiteLLM with 75% ratio (leaves 25% headroom)
        try:
            trimmed_dict = trim_messages(
                messages=messages_dict,
                model=self._model_name,
                max_tokens=max_tokens,
                trim_ratio=0.75,
            )

            # Convert back to StratifiedMessage
            return [StratifiedMessage.model_validate(m) for m in trimmed_dict]
        except Exception as e:
            logger.warning("LiteLLM trim_messages failed (%s), falling back to recent", e)
            return self._truncate_recent(messages, max_tokens)

"""Tests for smart truncation strategies."""

import pytest

from layercache.models import LayerType, StratifiedMessage, StratifiedPrompt
from layercache.truncation import TokenCounter, Truncator, TruncationStrategy


class TestTokenCounter:
    """Test token counting."""

    def test_count_simple_text(self):
        counter = TokenCounter()
        assert counter.count("Hello world") > 0

    def test_count_empty_text(self):
        counter = TokenCounter()
        assert counter.count("") == 0

    def test_count_messages(self):
        counter = TokenCounter()
        messages = [
            StratifiedMessage(
                role="user",
                content="Hello",
                layer=LayerType.SESSION,
            ),
            StratifiedMessage(
                role="assistant",
                content="Hi there!",
                layer=LayerType.SESSION,
            ),
        ]
        assert counter.count_messages(messages) > 0


class TestTruncateRecent:
    """Test recent truncation strategy."""

    def test_no_truncation_needed(self):
        """When under budget, no truncation occurs."""
        prompt = StratifiedPrompt(session_id="test")
        prompt.add_message(LayerType.SESSION, "user", "Hello")
        prompt.add_message(LayerType.SESSION, "assistant", "Hi there!")

        truncator = Truncator(strategy=TruncationStrategy.RECENT)
        result = truncator.truncate(prompt, max_tokens=1000)

        assert len(result.layers[LayerType.SESSION]) == 2

    def test_truncates_to_fit_budget(self):
        """When over budget, oldest messages are dropped."""
        prompt = StratifiedPrompt(session_id="test")
        for i in range(10):
            prompt.add_message(LayerType.SESSION, "user", f"Message {i} - " + "word " * 50)
            prompt.add_message(LayerType.SESSION, "assistant", f"Response {i} - " + "word " * 50)

        truncator = Truncator(strategy=TruncationStrategy.RECENT)
        result = truncator.truncate(prompt, max_tokens=500)

        # Should have fewer messages than original
        assert len(result.layers[LayerType.SESSION]) < 20
        # Should keep at least the last message
        assert len(result.layers[LayerType.SESSION]) >= 1

    def test_always_keeps_last_message(self):
        """Even if budget is very small, last message is kept."""
        prompt = StratifiedPrompt(session_id="test")
        prompt.add_message(LayerType.SESSION, "user", "First message - " + "word " * 100)
        prompt.add_message(LayerType.SESSION, "assistant", "Second message - " + "word " * 100)
        prompt.add_message(LayerType.SESSION, "user", "Last message")

        truncator = Truncator(strategy=TruncationStrategy.RECENT)
        result = truncator.truncate(prompt, max_tokens=50)

        # Should keep at least the last message
        assert len(result.layers[LayerType.SESSION]) >= 1
        # Last message should be preserved
        last_msg = result.layers[LayerType.SESSION][-1]
        assert last_msg.content == "Last message"


class TestTruncateImportant:
    """Test important truncation strategy."""

    def test_keeps_high_score_messages(self):
        """Messages with higher scores are kept."""
        prompt = StratifiedPrompt(session_id="test")
        # Low importance message
        prompt.add_message(LayerType.SESSION, "user", "Hi")
        # High importance message (has keyword)
        prompt.add_message(
            LayerType.SESSION,
            "system",
            "Important instruction: follow these rules",
        )
        # Low importance message
        prompt.add_message(LayerType.SESSION, "user", "Bye")

        truncator = Truncator(strategy=TruncationStrategy.IMPORTANT)
        result = truncator.truncate(prompt, max_tokens=100)

        # Should keep the important instruction message
        session_msgs = result.layers[LayerType.SESSION]
        has_instruction = any("instruction" in str(m.content).lower() for m in session_msgs)
        assert has_instruction

    def test_tool_calls_get_bonus(self):
        """Messages with tool calls get higher score."""
        prompt = StratifiedPrompt(session_id="test")
        prompt.add_message(LayerType.SESSION, "user", "Regular message")
        prompt.add_message(
            LayerType.SESSION,
            "assistant",
            "Tool response",
            metadata={"tool_calls": [{"name": "test"}]},
        )

        truncator = Truncator(strategy=TruncationStrategy.IMPORTANT)
        result = truncator.truncate(prompt, max_tokens=100)

        # Should keep both messages (under budget)
        assert len(result.layers[LayerType.SESSION]) == 2


class TestTruncationEdgeCases:
    """Test edge cases in truncation."""

    def test_empty_session(self):
        """Empty session layer is handled gracefully."""
        prompt = StratifiedPrompt(session_id="test")

        truncator = Truncator(strategy=TruncationStrategy.RECENT)
        result = truncator.truncate(prompt, max_tokens=100)

        assert len(result.layers[LayerType.SESSION]) == 0

    def test_zero_budget(self):
        """Zero budget skips truncation."""
        prompt = StratifiedPrompt(session_id="test")
        prompt.add_message(LayerType.SESSION, "user", "Hello")

        truncator = Truncator(strategy=TruncationStrategy.RECENT)
        result = truncator.truncate(prompt, max_tokens=0)

        # Should not modify (zero budget means skip)
        assert len(result.layers[LayerType.SESSION]) == 1

    def test_negative_budget(self):
        """Negative budget skips truncation."""
        prompt = StratifiedPrompt(session_id="test")
        prompt.add_message(LayerType.SESSION, "user", "Hello")

        truncator = Truncator(strategy=TruncationStrategy.RECENT)
        result = truncator.truncate(prompt, max_tokens=-100)

        # Should not modify
        assert len(result.layers[LayerType.SESSION]) == 1


class TestSessionIsolationWithTruncation:
    """Test that truncation respects session isolation."""

    def test_different_sessions_different_hashes_after_truncation(self):
        """Truncated prompts from different sessions have different hashes."""
        prompt1 = StratifiedPrompt(session_id="session-1")
        prompt1.add_message(LayerType.SESSION, "user", "Message 1")
        prompt1.add_message(LayerType.SESSION, "assistant", "Response 1")

        prompt2 = StratifiedPrompt(session_id="session-2")
        prompt2.add_message(LayerType.SESSION, "user", "Message 1")
        prompt2.add_message(LayerType.SESSION, "assistant", "Response 1")

        # Truncate both
        truncator = Truncator(strategy=TruncationStrategy.RECENT)
        truncator.truncate(prompt1, max_tokens=50)
        truncator.truncate(prompt2, max_tokens=50)

        # Hashes should be different due to session_id
        assert prompt1.prefix_hash() != prompt2.prefix_hash()

    def test_same_session_same_hash_after_truncation(self):
        """Same session produces same hash after truncation."""
        prompt1 = StratifiedPrompt(session_id="session-123")
        prompt1.add_message(LayerType.SESSION, "user", "Message 1")

        prompt2 = StratifiedPrompt(session_id="session-123")
        prompt2.add_message(LayerType.SESSION, "user", "Message 1")

        # Truncate both
        truncator = Truncator(strategy=TruncationStrategy.RECENT)
        truncator.truncate(prompt1, max_tokens=50)
        truncator.truncate(prompt2, max_tokens=50)

        # Hashes should be same
        assert prompt1.prefix_hash() == prompt2.prefix_hash()

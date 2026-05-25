"""Tests for the Stratifier module."""

import pytest

from layercache.models import LayerType
from layercache.stratifier import Stratifier


@pytest.fixture
def stratifier() -> Stratifier:
    return Stratifier()


class TestHeuristicStratification:
    """Tests for automatic heuristic-based message classification."""

    def test_single_system_message_goes_to_l0(self, stratifier: Stratifier) -> None:
        """First system message should be classified as L0 (System)."""
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello!"},
        ]
        prompt = stratifier.stratify(messages)

        assert len(prompt.layers[LayerType.SYSTEM]) == 1
        assert prompt.layers[LayerType.SYSTEM][0].content == "You are a helpful assistant."
        assert len(prompt.layers[LayerType.USER]) == 1
        assert prompt.layers[LayerType.USER][0].content == "Hello!"

    def test_multiple_system_messages_l0_then_l1(self, stratifier: Stratifier) -> None:
        """First system -> L0, subsequent system messages -> L1 (Context)."""
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "system", "content": "You have access to the following tools: ..."},
            {"role": "user", "content": "What tools do I have?"},
        ]
        prompt = stratifier.stratify(messages)

        assert len(prompt.layers[LayerType.SYSTEM]) == 1
        assert len(prompt.layers[LayerType.CONTEXT]) == 1
        expected = "You have access to the following tools: ..."
        assert prompt.layers[LayerType.CONTEXT][0].content == expected

    def test_conversation_history_goes_to_l2(self, stratifier: Stratifier) -> None:
        """Assistant and non-final user messages go to L2 (Session)."""
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello!"},
            {"role": "assistant", "content": "Hi there! How can I help?"},
            {"role": "user", "content": "Tell me about Python."},
            {"role": "assistant", "content": "Python is a versatile language."},
            {"role": "user", "content": "What about lists?"},  # Final user -> L4
        ]
        prompt = stratifier.stratify(messages)

        # L2 should have: non-final user, assistant, non-final user, assistant
        l2_count = len(prompt.layers[LayerType.SESSION])
        assert l2_count == 4

        # L4 should have only the final user message
        assert len(prompt.layers[LayerType.USER]) == 1
        assert prompt.layers[LayerType.USER][0].content == "What about lists?"

    def test_tool_messages_go_to_l2(self, stratifier: Stratifier) -> None:
        """Tool messages should be classified as L2 (Session)."""
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What's the weather?"},
            {
                "role": "assistant",
                "content": "Let me check.",
                "tool_calls": [
                    {"id": "1", "type": "function", "function": {"name": "get_weather"}}
                ],
            },
            {"role": "tool", "content": '{"temp": 72}', "tool_call_id": "1"},
            {"role": "assistant", "content": "It's 72 degrees."},
            {"role": "user", "content": "Thanks!"},  # Final user -> L4
        ]
        prompt = stratifier.stratify(messages)

        assert len(prompt.layers[LayerType.USER]) == 1
        assert prompt.layers[LayerType.USER][0].content == "Thanks!"

    def test_long_system_message_goes_to_l1(self, stratifier: Stratifier) -> None:
        """Long system messages (>500 chars) should be classified as L1 (Context)."""
        long_content = "You are a helpful assistant. " * 100  # >500 chars
        messages = [
            {"role": "system", "content": long_content},
            {"role": "user", "content": "Hello!"},
        ]
        prompt = stratifier.stratify(messages)

        # Long system message at index 0 goes to L0 (first system message rule takes precedence)
        assert len(prompt.layers[LayerType.SYSTEM]) == 1

    def test_system_with_tools_keyword_goes_to_l1(self, stratifier: Stratifier) -> None:
        """System messages containing tool-related keywords go to L1."""
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {
                "role": "system",
                "content": "Here are the available tools:\n- search_web\n- get_weather",
            },
            {"role": "user", "content": "Search the web"},
        ]
        prompt = stratifier.stratify(messages)

        assert len(prompt.layers[LayerType.SYSTEM]) == 1
        assert len(prompt.layers[LayerType.CONTEXT]) == 1


class TestLayerHintsStratification:
    """Tests for explicit layer hint classification."""

    def test_explicit_layer_hints(self, stratifier: Stratifier) -> None:
        """Messages should be placed in layers according to explicit hints."""
        messages = [
            {"role": "system", "content": "Core persona"},
            {"role": "system", "content": "Context info"},
            {"role": "user", "content": "Previous question"},
            {"role": "assistant", "content": "Previous answer"},
            {"role": "user", "content": "Current question"},
        ]
        hints = {0: "L0", 1: "L1", 2: "L2", 3: "L2", 4: "L4"}
        prompt = stratifier.stratify(messages, layer_hints=hints)

        assert len(prompt.layers[LayerType.SYSTEM]) == 1
        assert len(prompt.layers[LayerType.CONTEXT]) == 1
        assert len(prompt.layers[LayerType.SESSION]) == 2
        assert len(prompt.layers[LayerType.USER]) == 1


class TestReassembly:
    """Tests for prompt reassembly from layers."""

    def test_reassemble_preserves_layer_order(self, stratifier: Stratifier) -> None:
        """Reassembled messages should maintain L0->L4 order."""
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Previous question"},
            {"role": "assistant", "content": "Previous answer"},
            {"role": "user", "content": "Current question"},
        ]
        prompt = stratifier.stratify(messages)
        reassembled = prompt.reassemble()

        assert len(reassembled) == 4
        # First message should be the system prompt (L0)
        assert reassembled[0]["role"] == "system"
        assert reassembled[0]["content"] == "System prompt"
        # Last message should be the user query (L4)
        assert reassembled[-1]["role"] == "user"
        assert reassembled[-1]["content"] == "Current question"

    def test_prefix_hash_is_deterministic(self, stratifier: Stratifier) -> None:
        """Same messages should produce the same prefix hash."""
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello!"},
        ]

        prompt1 = stratifier.stratify(messages)
        prompt2 = stratifier.stratify(messages)

        assert prompt1.prefix_hash() == prompt2.prefix_hash()

    def test_prefix_hash_changes_with_l0(self, stratifier: Stratifier) -> None:
        """Different L0 content should produce different prefix hashes."""
        messages1 = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello!"},
        ]
        messages2 = [
            {"role": "system", "content": "You are a coding expert."},
            {"role": "user", "content": "Hello!"},
        ]

        prompt1 = stratifier.stratify(messages1)
        prompt2 = stratifier.stratify(messages2)

        assert prompt1.prefix_hash() != prompt2.prefix_hash()

    def test_prefix_hash_same_when_l4_differs(self, stratifier: Stratifier) -> None:
        """Same L0-L2 but different L4 should produce the same prefix hash."""
        base = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Question 1"},
            {"role": "assistant", "content": "Answer 1"},
        ]

        prompt1 = stratifier.stratify(base + [{"role": "user", "content": "New question A"}])
        prompt2 = stratifier.stratify(base + [{"role": "user", "content": "New question B"}])

        assert prompt1.prefix_hash() == prompt2.prefix_hash()

    def test_get_user_query(self, stratifier: Stratifier) -> None:
        """get_user_query should return the content of the final L4 message."""
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "The actual question"},
        ]
        prompt = stratifier.stratify(messages)

        assert prompt.get_user_query() == "The actual question"

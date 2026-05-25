"""Tests for the Enhancement Engine."""

import pytest
from layercache.models import StratifiedPrompt, LayerType
from layercache.enhancements.base import EnhancementRegistry
from layercache.enhancements.chain_of_thought import ChainOfThoughtEnhancement
from layercache.enhancements.structured_output import StructuredOutputEnhancement
from layercache.enhancements.self_critique import SelfCritiqueEnhancement


def _make_prompt() -> StratifiedPrompt:
    """Create a basic prompt with L0 and L4."""
    prompt = StratifiedPrompt()
    prompt.add_message(LayerType.SYSTEM, "system", "You are a helpful assistant.")
    prompt.add_message(LayerType.USER, "user", "What is Python?")
    return prompt


class TestChainOfThoughtEnhancement:
    def test_adds_cot_instructions(self) -> None:
        """CoT enhancement should add step-by-step instructions at L3."""
        prompt = _make_prompt()
        enhancer = ChainOfThoughtEnhancement()
        result = enhancer.apply(prompt)

        l3_messages = result.layers[LayerType.ENHANCEMENT]
        assert len(l3_messages) >= 2  # user instruction + assistant acknowledgment

        # Check the content includes step-by-step instruction
        all_content = " ".join(str(m.content) for m in l3_messages)
        assert "step by step" in all_content.lower()

    def test_does_not_modify_l0(self) -> None:
        """CoT enhancement should never modify L0 (System) layer."""
        prompt = _make_prompt()
        original_l0 = [m.content for m in prompt.layers[LayerType.SYSTEM]]
        enhancer = ChainOfThoughtEnhancement()
        enhancer.apply(prompt)

        current_l0 = [m.content for m in prompt.layers[LayerType.SYSTEM]]
        assert original_l0 == current_l0

    def test_does_not_modify_l4(self) -> None:
        """CoT enhancement should never modify L4 (User) layer."""
        prompt = _make_prompt()
        original_l4 = [m.content for m in prompt.layers[LayerType.USER]]
        enhancer = ChainOfThoughtEnhancement()
        enhancer.apply(prompt)

        current_l4 = [m.content for m in prompt.layers[LayerType.USER]]
        assert original_l4 == current_l4


class TestStructuredOutputEnhancement:
    def test_adds_json_instruction(self) -> None:
        """Structured output enhancement should add JSON format instructions."""
        prompt = _make_prompt()
        enhancer = StructuredOutputEnhancement()
        result = enhancer.apply(prompt)

        l3_messages = result.layers[LayerType.ENHANCEMENT]
        assert len(l3_messages) >= 2

        all_content = " ".join(str(m.content) for m in l3_messages)
        assert "json" in all_content.lower()

    def test_includes_schema_when_provided(self) -> None:
        """When a schema is provided, it should be included in the instruction."""
        prompt = _make_prompt()
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        enhancer = StructuredOutputEnhancement(schema=schema)
        result = enhancer.apply(prompt)

        l3_messages = result.layers[LayerType.ENHANCEMENT]
        all_content = " ".join(str(m.content) for m in l3_messages)
        assert "name" in all_content


class TestSelfCritiqueEnhancement:
    def test_adds_critique_instructions(self) -> None:
        """Self-critique enhancement should add critique instructions."""
        prompt = _make_prompt()
        enhancer = SelfCritiqueEnhancement()
        result = enhancer.apply(prompt)

        l3_messages = result.layers[LayerType.ENHANCEMENT]
        assert len(l3_messages) >= 2

        all_content = " ".join(str(m.content) for m in l3_messages)
        assert "critique" in all_content.lower()


class TestEnhancementRegistry:
    def test_register_and_apply(self) -> None:
        """Registry should register and apply enhancements correctly."""
        registry = EnhancementRegistry()
        registry.register(ChainOfThoughtEnhancement())
        registry.register(StructuredOutputEnhancement())

        prompt = _make_prompt()
        result = registry.apply_enhancements(prompt, ["chain_of_thought", "structured_json"])

        l3_messages = result.layers[LayerType.ENHANCEMENT]
        # CoT adds 2 messages, structured adds 2 messages = 4 total
        assert len(l3_messages) >= 4

    def test_unknown_enhancement_skipped(self) -> None:
        """Unknown enhancement names should be skipped with a warning."""
        registry = EnhancementRegistry()
        registry.register(ChainOfThoughtEnhancement())

        prompt = _make_prompt()
        # Should not raise, just skip "unknown_enhancement"
        result = registry.apply_enhancements(prompt, ["unknown_enhancement", "chain_of_thought"])

        l3_messages = result.layers[LayerType.ENHANCEMENT]
        assert len(l3_messages) >= 2  # Only CoT applied

    def test_list_enhancements(self) -> None:
        """Registry should list all registered enhancement names."""
        registry = EnhancementRegistry()
        registry.register(ChainOfThoughtEnhancement())
        registry.register(StructuredOutputEnhancement())

        names = registry.list_enhancements()
        assert "chain_of_thought" in names
        assert "structured_json" in names


class TestEnhancementCacheSafety:
    """Tests that enhancements never break the cache prefix."""

    def test_prefix_hash_unchanged_after_enhancement(self) -> None:
        """Applying enhancements should NOT change the prefix hash."""
        prompt = _make_prompt()
        original_hash = prompt.prefix_hash()

        enhancer = ChainOfThoughtEnhancement()
        enhancer.apply(prompt)

        # Prefix hash should be identical since L0-L2 are unchanged
        assert prompt.prefix_hash() == original_hash

    def test_multiple_enhancements_preserve_prefix(self) -> None:
        """Multiple enhancements should not affect the prefix hash."""
        prompt = _make_prompt()
        original_hash = prompt.prefix_hash()

        registry = EnhancementRegistry()
        registry.register(ChainOfThoughtEnhancement())
        registry.register(StructuredOutputEnhancement())
        registry.register(SelfCritiqueEnhancement())
        registry.apply_enhancements(
            prompt, ["chain_of_thought", "structured_json", "self_critique"]
        )

        assert prompt.prefix_hash() == original_hash

"""Tests for the Canonicalizer module."""

import json
import pytest
from layercache.canonicalizer import Canonicalizer
from layercache.models import StratifiedPrompt, LayerType


@pytest.fixture
def canonicalizer() -> Canonicalizer:
    return Canonicalizer()


def _make_prompt(messages: list[tuple[LayerType, str, str]]) -> StratifiedPrompt:
    """Helper to create a StratifiedPrompt from (layer, role, content) tuples."""
    prompt = StratifiedPrompt()
    for layer, role, content in messages:
        prompt.add_message(layer, role, content)
    return prompt


class TestWhitespaceNormalization:
    """Tests for whitespace normalization in content."""

    def test_strip_leading_trailing_whitespace(self, canonicalizer: Canonicalizer) -> None:
        """Content should have leading/trailing whitespace stripped."""
        prompt = _make_prompt([
            (LayerType.SYSTEM, "system", "  Hello world  "),
        ])
        prompt, _ = canonicalizer.canonicalize(prompt)

        assert prompt.layers[LayerType.SYSTEM][0].content == "Hello world"

    def test_collapse_triple_newlines(self, canonicalizer: Canonicalizer) -> None:
        """Three or more consecutive newlines should be collapsed to two."""
        prompt = _make_prompt([
            (LayerType.SYSTEM, "system", "Line 1\n\n\n\nLine 2"),
        ])
        prompt, _ = canonicalizer.canonicalize(prompt)

        assert prompt.layers[LayerType.SYSTEM][0].content == "Line 1\n\nLine 2"

    def test_collapse_multiple_spaces(self, canonicalizer: Canonicalizer) -> None:
        """Multiple consecutive spaces should be collapsed to one."""
        prompt = _make_prompt([
            (LayerType.SYSTEM, "system", "Hello   world   test"),
        ])
        prompt, _ = canonicalizer.canonicalize(prompt)

        assert prompt.layers[LayerType.SYSTEM][0].content == "Hello world test"

    def test_strip_trailing_whitespace_per_line(self, canonicalizer: Canonicalizer) -> None:
        """Trailing whitespace should be removed from each line."""
        prompt = _make_prompt([
            (LayerType.SYSTEM, "system", "Line 1   \nLine 2  "),
        ])
        prompt, _ = canonicalizer.canonicalize(prompt)

        content = prompt.layers[LayerType.SYSTEM][0].content
        assert content == "Line 1\nLine 2"


class TestToolCanonicalization:
    """Tests for tool definition canonicalization."""

    def test_tools_sorted_alphabetically(self, canonicalizer: Canonicalizer) -> None:
        """Tools should be sorted by function.name."""
        tools = [
            {"type": "function", "function": {"name": "zebra_search", "description": "Search zebras"}},
            {"type": "function", "function": {"name": "apple_fetch", "description": "Fetch apples"}},
            {"type": "function", "function": {"name": "mango_list", "description": "List mangoes"}},
        ]
        prompt = _make_prompt([])
        _, canonical_tools = canonicalizer.canonicalize(prompt, tools)

        names = [t["function"]["name"] for t in canonical_tools]
        assert names == ["apple_fetch", "mango_list", "zebra_search"]

    def test_json_schema_minified(self, canonicalizer: Canonicalizer) -> None:
        """Tool parameter JSON schemas should be minified."""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "test_tool",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "age": {"type": "integer"},
                        },
                    },
                },
            }
        ]
        prompt = _make_prompt([])
        _, canonical_tools = canonicalizer.canonicalize(prompt, tools)

        params = canonical_tools[0]["function"]["parameters"]
        # Properties should be sorted alphabetically
        assert list(params["properties"].keys()) == ["age", "name"]


class TestDeterminism:
    """Tests ensuring canonicalization is deterministic."""

    def test_same_input_same_output(self, canonicalizer: Canonicalizer) -> None:
        """Same prompt should always produce identical canonical output."""
        prompt1 = _make_prompt([
            (LayerType.SYSTEM, "system", "  Hello   world  "),
            (LayerType.USER, "user", "  Question?  "),
        ])
        prompt2 = _make_prompt([
            (LayerType.SYSTEM, "system", "  Hello   world  "),
            (LayerType.USER, "user", "  Question?  "),
        ])

        p1, _ = canonicalizer.canonicalize(prompt1)
        p2, _ = canonicalizer.canonicalize(prompt2)

        assert p1.reassemble() == p2.reassemble()

    def test_reassemble_is_deterministic(self, canonicalizer: Canonicalizer) -> None:
        """Reassembling the same prompt multiple times should produce identical output."""
        prompt = _make_prompt([
            (LayerType.SYSTEM, "system", "System message"),
            (LayerType.CONTEXT, "system", "Context information"),
            (LayerType.SESSION, "user", "Previous question"),
            (LayerType.SESSION, "assistant", "Previous answer"),
            (LayerType.USER, "user", "Current question"),
        ])

        prompt, _ = canonicalizer.canonicalize(prompt)
        result1 = prompt.reassemble()
        result2 = prompt.reassemble()

        assert result1 == result2


class TestMultimodalContent:
    """Tests for canonicalizing multimodal content arrays."""

    def test_text_blocks_canonicalized(self, canonicalizer: Canonicalizer) -> None:
        """Text blocks in multimodal content should be canonicalized."""
        prompt = _make_prompt([
            (LayerType.USER, "user", [
                {"type": "text", "text": "  Hello   world  "},
                {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}},
            ]),
        ])
        prompt, _ = canonicalizer.canonicalize(prompt)

        content = prompt.layers[LayerType.USER][0].content
        assert isinstance(content, list)
        assert content[0]["text"] == "Hello world"
        # Image block should be preserved
        assert content[1]["type"] == "image_url"

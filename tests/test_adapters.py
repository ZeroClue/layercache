"""Tests for the Provider Adapters."""

import pytest
from layercache.models import StratifiedPrompt, LayerType
from layercache.adapters.anthropic import AnthropicAdapter
from layercache.adapters.openai import OpenAIAdapter
from layercache.adapters.gemini import GeminiAdapter
from layercache.adapters import detect_provider, get_adapter


def _make_prompt() -> StratifiedPrompt:
    """Create a basic prompt with L0, L1, L2, L4."""
    prompt = StratifiedPrompt()
    prompt.add_message(LayerType.SYSTEM, "system", "You are a helpful assistant.")
    prompt.add_message(LayerType.CONTEXT, "system", "You have access to web search.")
    prompt.add_message(LayerType.SESSION, "user", "Previous question")
    prompt.add_message(LayerType.SESSION, "assistant", "Previous answer")
    prompt.add_message(LayerType.USER, "user", "What is Python?")
    return prompt


class TestAnthropicAdapter:
    def test_injects_cache_control_markers(self) -> None:
        """Anthropic adapter should inject cache_control at layer boundaries."""
        prompt = _make_prompt()
        adapter = AnthropicAdapter()
        payload = {"model": "claude-3-5-sonnet-20241022"}
        result = adapter.inject_markers(prompt, payload)

        messages = result["messages"]
        # Check that cache_control is injected on messages at layer boundaries
        cache_controlled = [
            m for m in messages
            if isinstance(m.get("content"), list)
            and any(block.get("cache_control") for block in m["content"])
        ]

        # Should have cache_control markers for L0, L1, L2 boundaries
        assert len(cache_controlled) >= 2

    def test_extracts_cache_metrics(self) -> None:
        """Should correctly extract Anthropic cache metrics."""
        adapter = AnthropicAdapter()
        response = {
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 500,
                "cache_read_input_tokens": 600,
                "cache_creation_input_tokens": 400,
            }
        }
        metrics = adapter.extract_cache_metrics(response)

        assert metrics["cache_read_input_tokens"] == 600
        assert metrics["cache_creation_input_tokens"] == 400
        assert metrics["input_tokens"] == 1000
        assert metrics["output_tokens"] == 500


class TestOpenAIAdapter:
    def test_preserves_message_order(self) -> None:
        """OpenAI adapter should place messages in correct order for automatic caching."""
        prompt = _make_prompt()
        adapter = OpenAIAdapter()
        payload = {"model": "gpt-4o"}
        result = adapter.inject_markers(prompt, payload)

        messages = result["messages"]
        assert len(messages) == 5
        # First message should be system (L0)
        assert messages[0]["role"] == "system"
        # Last message should be user (L4)
        assert messages[-1]["role"] == "user"

    def test_extracts_cache_metrics(self) -> None:
        """Should correctly extract OpenAI cache metrics."""
        adapter = OpenAIAdapter()
        response = {
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 500,
                "cached_tokens": 600,
            }
        }
        metrics = adapter.extract_cache_metrics(response)

        assert metrics["cache_read_input_tokens"] == 600
        assert metrics["input_tokens"] == 1000
        assert metrics["output_tokens"] == 500


class TestGeminiAdapter:
    def test_first_request_no_cache(self) -> None:
        """First request should not have cached_content reference."""
        prompt = _make_prompt()
        adapter = GeminiAdapter()
        payload = {"model": "gemini-1.5-pro"}
        result = adapter.inject_markers(prompt, payload)

        assert "cached_content" not in result
        assert "contents" in result

    def test_subsequent_request_uses_cache(self) -> None:
        """After marking cache as created, subsequent requests should use it."""
        prompt = _make_prompt()
        adapter = GeminiAdapter()

        # First call (no cache)
        adapter.inject_markers(prompt, {"model": "gemini-1.5-pro"})

        # Register cache
        prefix_hash = adapter._compute_prefix_hash(prompt)
        adapter.mark_cache_created(prefix_hash, "cached-content-123")

        # Second call (should use cache)
        result = adapter.inject_markers(prompt, {"model": "gemini-1.5-pro"})
        assert result["cached_content"] == "cached-content-123"

    def test_extracts_cache_metrics(self) -> None:
        """Should correctly extract Gemini cache metrics."""
        adapter = GeminiAdapter()
        response = {
            "usageMetadata": {
                "promptTokenCount": 1000,
                "candidatesTokenCount": 500,
                "cachedContentTokenCount": 600,
                "tokensToCache": 400,
            }
        }
        metrics = adapter.extract_cache_metrics(response)

        assert metrics["cache_read_input_tokens"] == 600
        assert metrics["input_tokens"] == 1000
        assert metrics["output_tokens"] == 500


class TestProviderDetection:
    def test_detect_anthropic(self) -> None:
        assert detect_provider("anthropic/claude-3-5-sonnet-20241022") == "anthropic"
        assert detect_provider("claude-3-5-sonnet-20241022") == "anthropic"

    def test_detect_openai(self) -> None:
        assert detect_provider("openai/gpt-4o") == "openai"
        assert detect_provider("gpt-4o-mini") == "openai"

    def test_detect_gemini(self) -> None:
        assert detect_provider("gemini/gemini-1.5-pro") == "gemini"
        assert detect_provider("gemini-1.5-flash") == "gemini"

    def test_default_to_openai(self) -> None:
        assert detect_provider("unknown-model") == "openai"

    def test_get_adapter(self) -> None:
        adapter = get_adapter("anthropic")
        assert isinstance(adapter, AnthropicAdapter)

        adapter = get_adapter("openai")
        assert isinstance(adapter, OpenAIAdapter)

        adapter = get_adapter("gemini")
        assert isinstance(adapter, GeminiAdapter)

        # Unknown provider defaults to OpenAI
        adapter = get_adapter("unknown")
        assert isinstance(adapter, OpenAIAdapter)

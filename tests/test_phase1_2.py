"""Tests for Phase 1.2 — Anthropic Provider Cache Integration.

Tests verify:
- Cache markers injected at L2/L3 boundary only
- No markers when prefix < 1,024 tokens
- No markers for non-Anthropic models
- Cache metrics extraction from responses
- Multimodal content handling
"""

from layercache.adapters.anthropic import AnthropicAdapter
from layercache.models import LayerType, StratifiedPrompt


class TestCacheMarkersInjection:
    """Test cache_control marker injection at L2/L3 boundary."""

    def test_cache_markers_injected_at_l2_l3_boundary(self) -> None:
        """Should inject cache_control only at L2/L3 boundary (end of stable prefix)."""
        adapter = AnthropicAdapter()
        prompt = StratifiedPrompt()

        # Create a prompt with enough tokens to exceed 1,024 threshold
        # Each word is ~1 token, so we need ~1024+ words
        # L0: System instructions (~400 tokens)
        system_content = "You are a helpful assistant with extensive knowledge. " * 50
        prompt.add_message(LayerType.SYSTEM, "system", system_content)

        # L1: Context (~400 tokens)
        context_content = "You have access to a comprehensive knowledge base with facts. " * 40
        prompt.add_message(LayerType.CONTEXT, "system", context_content)

        # L2: Session history (~400 tokens) - total prefix ~1,200+ tokens
        session_content = (
            "User: What is the previous question? "
            "Assistant: The previous question was about Python. "
        ) * 20
        prompt.add_message(LayerType.SESSION, "user", session_content)

        # L3: Enhancement (should NOT get cache marker)
        prompt.add_message(
            LayerType.ENHANCEMENT,
            "system",
            "Think step by step.",
        )

        # L4: User query (should NOT get cache marker)
        prompt.add_message(LayerType.USER, "user", "What is Python?")

        # Verify we have enough tokens
        prefix_tokens = prompt.stable_prefix_tokens()
        assert prefix_tokens >= 1024, f"Need 1024+ tokens, got {prefix_tokens}"

        payload = {"model": "claude-3-5-sonnet-20241022", "messages": []}
        result = adapter.inject_markers(prompt, payload)

        messages = result["messages"]

        # Find messages with cache_control
        cache_controlled_messages = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if block.get("cache_control"):
                        cache_controlled_messages.append(msg)
                        break

        # Should have cache_control ONLY at L2 boundary (last stable layer)
        # Not on L0, L1, L3, or L4
        assert len(cache_controlled_messages) == 1, (
            f"Expected 1 cache-controlled message (at L2 boundary), "
            f"got {len(cache_controlled_messages)}"
        )

        # Verify the cache_control is on the last L2 message
        cache_msg = cache_controlled_messages[0]
        assert cache_msg["_layer"] == LayerType.SESSION, (
            f"Cache marker should be on L2_SESSION, got {cache_msg.get('_layer')}"
        )

    def test_no_markers_when_prefix_below_1024_tokens(self) -> None:
        """Should NOT inject cache_control when L0+L1+L2 < 1,024 tokens."""
        adapter = AnthropicAdapter()
        prompt = StratifiedPrompt()

        # Create a short prompt (< 1,024 tokens)
        prompt.add_message(LayerType.SYSTEM, "system", "You are helpful.")
        prompt.add_message(LayerType.CONTEXT, "system", "You have tools.")
        prompt.add_message(LayerType.SESSION, "user", "Previous question")
        prompt.add_message(LayerType.SESSION, "assistant", "Previous answer")
        prompt.add_message(LayerType.USER, "user", "What is Python?")

        payload = {"model": "claude-3-5-sonnet-20241022", "messages": []}
        result = adapter.inject_markers(prompt, payload)

        messages = result["messages"]

        # Verify no cache_control markers anywhere
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    assert "cache_control" not in block, (
                        "Cache marker should NOT be injected when prefix < 1,024 tokens"
                    )

    def test_no_markers_for_non_anthropic_models(self) -> None:
        """Should NOT inject cache_control for non-Anthropic models."""
        adapter = AnthropicAdapter()
        prompt = StratifiedPrompt()

        # Create a long prompt (would normally qualify for caching)
        system_content = "You are a helpful assistant. " * 50
        context_content = "You have access to tools. " * 50
        session_content = "User: Hi\nAssistant: Hello\n" * 50

        prompt.add_message(LayerType.SYSTEM, "system", system_content)
        prompt.add_message(LayerType.CONTEXT, "system", context_content)
        prompt.add_message(LayerType.SESSION, "user", session_content)
        prompt.add_message(LayerType.USER, "user", "What is Python?")

        # Test with OpenAI model
        payload_openai = {"model": "gpt-4o", "messages": []}
        result_openai = adapter.inject_markers(prompt, payload_openai)

        for msg in result_openai["messages"]:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    assert "cache_control" not in block, (
                        "Cache marker should NOT be injected for OpenAI models"
                    )

        # Test with Gemini model
        payload_gemini = {"model": "gemini-1.5-pro", "messages": []}
        result_gemini = adapter.inject_markers(prompt, payload_gemini)

        for msg in result_gemini["messages"]:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    assert "cache_control" not in block, (
                        "Cache marker should NOT be injected for Gemini models"
                    )


class TestCacheMetricsExtraction:
    """Test cache metrics extraction from Anthropic responses."""

    def test_cache_metrics_extracted_from_response(self) -> None:
        """Should correctly extract all cache metrics from Anthropic response."""
        adapter = AnthropicAdapter()

        response = {
            "id": "msg_123",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello!"}],
            "model": "claude-3-5-sonnet-20241022",
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 1500,
                "output_tokens": 250,
                "cache_read_input_tokens": 1000,
                "cache_creation_input_tokens": 500,
            },
        }

        metrics = adapter.extract_cache_metrics(response)

        assert metrics == {
            "input_tokens": 1500,
            "output_tokens": 250,
            "cache_read_input_tokens": 1000,
            "cache_creation_input_tokens": 500,
        }

    def test_cache_metrics_handles_missing_fields(self) -> None:
        """Should handle responses with missing cache metrics fields."""
        adapter = AnthropicAdapter()

        # Response without cache metrics (first request, no caching)
        response_no_cache = {
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 200,
            }
        }

        metrics = adapter.extract_cache_metrics(response_no_cache)

        assert metrics["input_tokens"] == 1000
        assert metrics["output_tokens"] == 200
        assert metrics["cache_read_input_tokens"] == 0
        assert metrics["cache_creation_input_tokens"] == 0

    def test_cache_metrics_handles_empty_usage(self) -> None:
        """Should handle responses with empty or missing usage dict."""
        adapter = AnthropicAdapter()

        # Empty usage
        response_empty = {"usage": {}}
        metrics = adapter.extract_cache_metrics(response_empty)
        assert metrics["input_tokens"] == 0
        assert metrics["output_tokens"] == 0

        # Missing usage
        response_no_usage = {}
        metrics = adapter.extract_cache_metrics(response_no_usage)
        assert metrics["input_tokens"] == 0
        assert metrics["output_tokens"] == 0


class TestMultimodalContent:
    """Test handling of multimodal content (text + images)."""

    def test_multimodal_content_handled_correctly(self) -> None:
        """Should correctly handle multimodal content with cache_control injection."""
        adapter = AnthropicAdapter()
        prompt = StratifiedPrompt()

        # Create a long prompt to exceed threshold (need 1024+ tokens)
        system_content = "You are a helpful assistant with image analysis capabilities. " * 60
        prompt.add_message(LayerType.SYSTEM, "system", system_content)

        context_content = "You can analyze images and describe their contents in detail. " * 50
        prompt.add_message(LayerType.CONTEXT, "system", context_content)

        # L2 with multimodal content - add enough text to exceed threshold
        session_text = "Please analyze this image carefully and describe what you see in it. " * 20
        session_content = [
            {"type": "text", "text": session_text},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "base64data"},
            },
        ]
        prompt.add_message(LayerType.SESSION, "user", session_content)

        prompt.add_message(LayerType.USER, "user", "Describe it.")

        # Verify we have enough tokens
        prefix_tokens = prompt.stable_prefix_tokens()
        assert prefix_tokens >= 1024, f"Need 1024+ tokens, got {prefix_tokens}"

        payload = {"model": "claude-3-5-sonnet-20241022", "messages": []}
        result = adapter.inject_markers(prompt, payload)

        messages = result["messages"]

        # Find the message with cache_control
        cache_controlled_msg = None
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if block.get("cache_control"):
                        cache_controlled_msg = msg
                        break

        assert cache_controlled_msg is not None, "Should have cache_control marker"
        assert cache_controlled_msg["_layer"] == LayerType.SESSION

        # Verify multimodal structure is preserved
        content = cache_controlled_msg["content"]
        assert isinstance(content, list)
        assert len(content) >= 2  # text + image blocks

        # cache_control should be on the last content block
        last_block = content[-1]
        assert last_block.get("cache_control") == {"type": "ephemeral"}

    def test_multimodal_content_preserves_image_blocks(self) -> None:
        """Should preserve image blocks when injecting cache_control."""
        adapter = AnthropicAdapter()
        prompt = StratifiedPrompt()

        # Long system content to exceed threshold
        system_content = "You analyze images. " * 60
        prompt.add_message(LayerType.SYSTEM, "system", system_content)

        # Multimodal L2 content
        session_content = [
            {"type": "text", "text": "Look at this:"},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": "imagedata"},
            },
            {"type": "text", "text": "What do you see?"},
        ]
        prompt.add_message(LayerType.SESSION, "user", session_content)
        prompt.add_message(LayerType.USER, "user", "Describe.")

        payload = {"model": "claude-3-5-sonnet-20241022", "messages": []}
        result = adapter.inject_markers(prompt, payload)

        messages = result["messages"]

        # Find session message
        session_msg = None
        for msg in messages:
            if msg.get("_layer") == LayerType.SESSION:
                session_msg = msg
                break

        assert session_msg is not None

        content = session_msg["content"]
        assert isinstance(content, list)

        # Should have all original blocks plus cache_control on last
        text_blocks = [b for b in content if b.get("type") == "text"]
        image_blocks = [b for b in content if b.get("type") == "image"]

        assert len(text_blocks) >= 2
        assert len(image_blocks) == 1

        # Image block should be preserved (not have cache_control)
        image_block = image_blocks[0]
        assert "cache_control" not in image_block or image_block.get("cache_control") != {
            "type": "ephemeral"
        }


class TestCacheBreakpointDetection:
    """Test L2/L3 boundary detection logic."""

    def test_get_stable_prefix_content(self) -> None:
        """Should correctly identify L0+L1+L2 as stable prefix."""
        adapter = AnthropicAdapter()
        prompt = StratifiedPrompt()

        prompt.add_message(LayerType.SYSTEM, "system", "System instruction")
        prompt.add_message(LayerType.CONTEXT, "system", "Context info")
        prompt.add_message(LayerType.SESSION, "user", "Session message")
        prompt.add_message(LayerType.ENHANCEMENT, "system", "Enhancement")
        prompt.add_message(LayerType.USER, "user", "User query")

        # Use internal method to get stable prefix
        messages = adapter._reassemble_with_metadata(prompt)
        stable_layers = {LayerType.SYSTEM, LayerType.CONTEXT, LayerType.SESSION}

        stable_messages = [m for m in messages if m.get("_layer") in stable_layers]
        dynamic_messages = [m for m in messages if m.get("_layer") not in stable_layers]

        assert len(stable_messages) == 3
        assert len(dynamic_messages) == 2

    def test_prefix_token_count(self) -> None:
        """Should correctly count tokens in stable prefix."""
        prompt = StratifiedPrompt()

        # Add known content
        prompt.add_message(LayerType.SYSTEM, "system", "Hello world " * 100)
        prompt.add_message(LayerType.CONTEXT, "system", "Context " * 50)
        prompt.add_message(LayerType.SESSION, "user", "Session " * 50)

        prefix_tokens = prompt.stable_prefix_tokens()

        # Should be > 0 and reasonable (exact count depends on tiktoken)
        assert prefix_tokens > 0
        assert prefix_tokens < 10000  # Sanity check

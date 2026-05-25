"""Tests for the Anthropic /v1/messages compatibility layer."""

from layercache.adapters.anthropic_messages import (
    AnthropicStreamTranslator,
    anthropic_request_to_fields,
    openai_response_to_anthropic,
)

# ===========================================================================
# Request translation tests
# ===========================================================================


class TestAnthropicRequestToFields:
    def test_basic_text_request(self) -> None:
        body = {
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "Hello"}],
        }
        fields = anthropic_request_to_fields(body)
        assert fields["model"] == "claude-3-5-sonnet-20241022"
        assert fields["max_tokens"] == 1024
        assert len(fields["messages"]) == 1
        assert fields["messages"][0]["role"] == "user"
        assert fields["messages"][0]["content"] == "Hello"
        assert fields["stream"] is False

    def test_system_prompt(self) -> None:
        body = {
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 100,
            "system": "You are a helpful assistant.",
            "messages": [{"role": "user", "content": "Hi"}],
        }
        fields = anthropic_request_to_fields(body)
        assert len(fields["messages"]) == 2
        assert fields["messages"][0]["role"] == "system"
        assert fields["messages"][0]["content"] == "You are a helpful assistant."
        assert fields["messages"][1]["role"] == "user"

    def test_system_as_content_array(self) -> None:
        body = {
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 100,
            "system": [{"type": "text", "text": "Be concise."}],
            "messages": [{"role": "user", "content": "Hi"}],
        }
        fields = anthropic_request_to_fields(body)
        assert fields["messages"][0]["role"] == "system"
        assert fields["messages"][0]["content"] == [{"type": "text", "text": "Be concise."}]

    def test_stop_sequences(self) -> None:
        body = {
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 100,
            "stop_sequences": ["\n\n", "END"],
            "messages": [{"role": "user", "content": "Write"}],
        }
        fields = anthropic_request_to_fields(body)
        assert fields["stop"] == ["\n\n", "END"]

    def test_metadata_user_id(self) -> None:
        body = {
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 100,
            "metadata": {"user_id": "user_abc123"},
            "messages": [{"role": "user", "content": "Hello"}],
        }
        fields = anthropic_request_to_fields(body)
        assert fields["user"] == "user_abc123"

    def test_tool_result_message(self) -> None:
        body = {
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 100,
            "messages": [
                {"role": "user", "content": "What is the weather?"},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_123",
                            "content": "Sunny, 72°F",
                        }
                    ],
                },
            ],
        }
        fields = anthropic_request_to_fields(body)
        assert len(fields["messages"]) == 2
        assert fields["messages"][1]["role"] == "tool"
        assert fields["messages"][1]["tool_call_id"] == "tu_123"
        assert fields["messages"][1]["content"] == "Sunny, 72°F"

    def test_tools_translation(self) -> None:
        body = {
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "What's the weather?"}],
            "tools": [
                {
                    "name": "get_weather",
                    "description": "Get weather for a city",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "location": {"type": "string"},
                        },
                        "required": ["location"],
                    },
                }
            ],
        }
        fields = anthropic_request_to_fields(body)
        assert len(fields["tools"]) == 1
        assert fields["tools"][0]["type"] == "function"
        assert fields["tools"][0]["function"]["name"] == "get_weather"
        assert "parameters" in fields["tools"][0]["function"]

    def test_tool_choice_auto(self) -> None:
        body = {
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "Hi"}],
            "tool_choice": {"type": "auto"},
        }
        fields = anthropic_request_to_fields(body)
        assert fields["tool_choice"] == "auto"

    def test_tool_choice_any(self) -> None:
        body = {
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "Hi"}],
            "tool_choice": {"type": "any"},
        }
        fields = anthropic_request_to_fields(body)
        assert fields["tool_choice"] == "required"

    def test_tool_choice_specific_tool(self) -> None:
        body = {
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "Hi"}],
            "tool_choice": {"type": "tool", "name": "get_weather"},
        }
        fields = anthropic_request_to_fields(body)
        assert fields["tool_choice"]["type"] == "function"
        assert fields["tool_choice"]["function"]["name"] == "get_weather"

    def test_optional_fields_omitted(self) -> None:
        body = {
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "Hi"}],
        }
        fields = anthropic_request_to_fields(body)
        assert "temperature" not in fields
        assert "top_p" not in fields
        assert "stop" not in fields
        assert "user" not in fields

    def test_image_content_block(self) -> None:
        body = {
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 100,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What's in this image?"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": "abc123",
                            },
                        },
                    ],
                }
            ],
        }
        fields = anthropic_request_to_fields(body)
        msg = fields["messages"][0]
        assert isinstance(msg["content"], list)
        assert msg["content"][0]["type"] == "text"
        assert msg["content"][1]["type"] == "image_url"


# ===========================================================================
# Response translation tests
# ===========================================================================


class TestOpenaiResponseToAnthropic:
    def test_text_response(self) -> None:
        response = {
            "id": "chatcmpl-123",
            "model": "claude-3-5-sonnet-20241022",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hello!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        result = openai_response_to_anthropic(response)
        assert result["type"] == "message"
        assert result["role"] == "assistant"
        assert len(result["content"]) == 1
        assert result["content"][0]["type"] == "text"
        assert result["content"][0]["text"] == "Hello!"
        assert result["stop_reason"] == "end_turn"
        assert result["usage"]["input_tokens"] == 10
        assert result["usage"]["output_tokens"] == 5

    def test_tool_call_response(self) -> None:
        response = {
            "id": "chatcmpl-456",
            "model": "claude-3-5-sonnet-20241022",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"location": "San Francisco"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 8},
        }
        result = openai_response_to_anthropic(response)
        assert len(result["content"]) == 1
        assert result["content"][0]["type"] == "tool_use"
        assert result["content"][0]["id"] == "call_abc"
        assert result["content"][0]["name"] == "get_weather"
        assert result["content"][0]["input"] == {"location": "San Francisco"}
        assert result["stop_reason"] == "tool_use"

    def test_malformed_tool_arguments(self) -> None:
        response = {
            "id": "chatcmpl-456",
            "model": "claude-3-5-sonnet-20241022",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"location": "San Francisco"',  # truncated
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 8},
        }
        result = openai_response_to_anthropic(response)
        assert result["content"][0]["type"] == "tool_use"
        assert result["content"][0]["input"] == {}

    def test_length_stop_reason(self) -> None:
        response = {
            "id": "chatcmpl-789",
            "model": "claude-3-5-sonnet-20241022",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Partial..."},
                    "finish_reason": "length",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 100},
        }
        result = openai_response_to_anthropic(response)
        assert result["stop_reason"] == "max_tokens"

    def test_content_filter_stop_reason(self) -> None:
        response = {
            "id": "chatcmpl-789",
            "model": "claude-3-5-sonnet-20241022",
            "choices": [
                {
                    "message": {"role": "assistant", "content": ""},
                    "finish_reason": "content_filter",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 0},
        }
        result = openai_response_to_anthropic(response)
        assert result["stop_reason"] == "content_filter"


# ===========================================================================
# Streaming translator tests
# ===========================================================================


class TestAnthropicStreamTranslator:
    def test_basic_text_streaming(self) -> None:
        """Text streaming should emit correct Anthropic SSE events."""
        translator = AnthropicStreamTranslator(model="claude-3-5-sonnet-20241022")

        # Chunk 1: role-only (no content yet)
        events = translator.translate({"choices": [{"delta": {"role": "assistant"}, "index": 0}]})
        assert len(events) == 1
        assert events[0].startswith("event: message_start")
        assert "claude-3-5-sonnet-20241022" in events[0]
        assert translator._has_emitted_start is True

        # Chunk 2: first content
        events = translator.translate({"choices": [{"delta": {"content": "Hello"}, "index": 0}]})
        assert len(events) == 2
        assert events[0].startswith("event: content_block_start")
        assert '"type": "text"' in events[0]
        assert events[1].startswith("event: content_block_delta")
        assert "Hello" in events[1]

        # Chunk 3: more content
        events = translator.translate({"choices": [{"delta": {"content": " world"}, "index": 0}]})
        assert len(events) == 1
        assert events[0].startswith("event: content_block_delta")
        assert " world" in events[0]

        # Chunk 4: finish
        events = translator.translate(
            {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}
        )
        assert len(events) == 3
        assert events[0].startswith("event: content_block_stop")
        assert events[1].startswith("event: message_delta")
        assert '"stop_reason": "end_turn"' in events[1]
        assert events[2].startswith("event: message_stop")

    def test_message_start_on_first_chunk_with_content(self) -> None:
        """message_start should fire even when the first chunk carries content."""
        translator = AnthropicStreamTranslator(model="claude-3-5-sonnet-20241022")
        events = translator.translate({"choices": [{"delta": {"content": "Direct"}, "index": 0}]})
        assert len(events) == 3
        assert events[0].startswith("event: message_start")
        assert events[1].startswith("event: content_block_start")
        assert events[2].startswith("event: content_block_delta")

    def test_tool_call_streaming(self) -> None:
        """Tool call chunks should produce correct Anthropic events."""
        translator = AnthropicStreamTranslator(model="claude-3-5-sonnet-20241022")

        # Chunk 1: role-only
        events = translator.translate({"choices": [{"delta": {"role": "assistant"}, "index": 0}]})
        assert len(events) == 1
        assert events[0].startswith("event: message_start")

        # Chunk 2: tool call name
        events = translator.translate(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_abc",
                                    "function": {"name": "get_weather", "arguments": ""},
                                }
                            ]
                        },
                        "index": 0,
                    }
                ]
            }
        )
        assert len(events) == 3
        assert events[0].startswith("event: content_block_start")
        assert "content_block_start" in events[0]
        assert '"type": "text"' in events[0]
        assert events[1].startswith("event: content_block_stop")
        assert events[2].startswith("event: content_block_start")
        assert "tool_use" in events[2]
        assert "get_weather" in events[2]

        # Chunk 3: tool call arguments delta
        events = translator.translate(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '{"location":'},
                                }
                            ]
                        },
                        "index": 0,
                    }
                ]
            }
        )
        assert len(events) == 1
        assert events[0].startswith("event: content_block_delta")
        assert "input_json_delta" in events[0]
        assert "location" in events[0]

        # Chunk 4: finish
        events = translator.translate(
            {"choices": [{"delta": {}, "finish_reason": "tool_calls", "index": 0}]}
        )
        assert len(events) == 3
        assert events[0].startswith("event: content_block_stop")
        assert events[1].startswith("event: message_delta")
        assert '"stop_reason": "tool_use"' in events[1]
        assert events[2].startswith("event: message_stop")

    def test_no_duplicate_termination_events(self) -> None:
        """When translator emits message_stop, the caller must not emit another."""
        translator = AnthropicStreamTranslator(model="claude-3-5-sonnet-20241022")

        translator.translate({"choices": [{"delta": {"role": "assistant"}, "index": 0}]})
        translator.translate({"choices": [{"delta": {"content": "Hi"}, "index": 0}]})
        last = translator.translate(
            {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}
        )

        assert translator._has_emitted_stop is True
        # Last batch has stop events
        assert any(e.startswith("event: message_stop") for e in last)

        # Calling translate again should NOT emit duplicate stop
        extra = translator.translate({"choices": [{"delta": {}, "index": 0}]})
        assert not any(e.startswith("event: message_stop") for e in extra)

    def test_empty_stream_emit_nothing(self) -> None:
        """Empty chunks with no delta should produce no events."""
        translator = AnthropicStreamTranslator(model="claude-3-5-sonnet-20241022")
        events = translator.translate({})
        assert events == []

        events = translator.translate({"object": "chat.completion.chunk", "choices": []})
        assert events == []

    def test_cache_hit_fake_chunk_path(self) -> None:
        """Fake chunks from cache-hit path should produce valid events."""
        translator = AnthropicStreamTranslator(model="claude-3-5-sonnet-20241022")

        # Simulate what _handle_anthropic_stream does for cache-hit raw strings
        fake = {"choices": [{"delta": {"content": "Cached reply"}, "finish_reason": None}]}
        events = translator.translate(fake)
        assert len(events) == 3
        assert events[0].startswith("event: message_start")
        assert events[1].startswith("event: content_block_start")
        assert events[2].startswith("event: content_block_delta")

        events = translator.translate(fake)
        assert len(events) == 1
        assert events[0].startswith("event: content_block_delta")

    def test_stop_reason_maps_from_openai(self) -> None:
        """All OpenAI finish_reason values map correctly."""
        translator = AnthropicStreamTranslator(model="claude-3-5-sonnet-20241022")
        translator.translate({"choices": [{"delta": {"content": "x"}, "index": 0}]})

        events = translator.translate(
            {"choices": [{"delta": {}, "finish_reason": "length", "index": 0}]}
        )
        assert '"stop_reason": "max_tokens"' in events[1]

    def test_text_and_tool_blocks_both_close_on_finish(self) -> None:
        """Both text and tool blocks should be properly closed at finish."""
        translator = AnthropicStreamTranslator(model="claude-3-5-sonnet-20241022")
        translator.translate({"choices": [{"delta": {"role": "assistant"}, "index": 0}]})

        translator.translate(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "foo", "arguments": ""},
                                }
                            ]
                        },
                        "index": 0,
                    }
                ]
            }
        )

        translator.translate(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": "{}"},
                                }
                            ]
                        },
                        "index": 0,
                    }
                ]
            }
        )

        events = translator.translate(
            {"choices": [{"delta": {}, "finish_reason": "tool_calls", "index": 0}]}
        )
        # Should have content_block_stop (for tool block) + message_delta + message_stop
        assert len(events) == 3
        assert events[0].startswith("event: content_block_stop")

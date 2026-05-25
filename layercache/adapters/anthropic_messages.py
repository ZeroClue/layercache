"""Anthropic Messages API compatibility layer.

Translates between Anthropic's ``/v1/messages`` wire format and LayerCache's
internal OpenAI-compatible pipeline format.  This enables Claude Code and
other Anthropic-native tools to use LayerCache's caching pipeline.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Request translation  (Anthropic /v1/messages  ->  LayerCacheRequest fields)
# ---------------------------------------------------------------------------

_FINISH_REASON_TO_STOP: dict[str, str] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "content_filter",
}


_ANTHROPIC_TO_OPENAI_TOOL_CHOICE: dict[str, str] = {
    "auto": "auto",
    "any": "required",
    "none": "none",
}


def anthropic_request_to_fields(body: dict[str, Any]) -> dict[str, Any]:
    """Translate an Anthropic ``/v1/messages`` request into pipeline-compatible fields.

    Returns a dict that can be unpacked as ``LayerCacheRequest(**fields)``.
    """
    model = body["model"]

    # Build OpenAI-style messages list
    messages: list[dict[str, Any]] = []

    # system prompt -> prepend as a system-role message
    system = body.get("system")
    if system:
        messages.append({"role": "system", "content": system})

    # Translate message content blocks
    for msg in body.get("messages", []):
        role = msg["role"]
        content = msg.get("content", "")

        if isinstance(content, list):
            messages.append(_translate_content_blocks(role, content))
        else:
            messages.append({"role": role, "content": content})

    fields: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": body.get("stream", False),
    }

    if "max_tokens" in body:
        fields["max_tokens"] = body["max_tokens"]
    if "temperature" in body:
        fields["temperature"] = body["temperature"]
    if "top_p" in body:
        fields["top_p"] = body["top_p"]
    if "stop_sequences" in body:
        fields["stop"] = body["stop_sequences"]
    if "metadata" in body and isinstance(body["metadata"], dict):
        fields["user"] = body["metadata"].get("user_id", "")

    # Tools: Anthropic uses input_schema; OpenAI uses parameters
    tools = body.get("tools")
    if tools:
        openai_tools: list[dict[str, Any]] = []
        for t in tools:
            openai_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {}),
                    },
                }
            )
        fields["tools"] = openai_tools

    # Tool choice
    tc = body.get("tool_choice")
    if tc is not None:
        if isinstance(tc, str):
            mapped = _ANTHROPIC_TO_OPENAI_TOOL_CHOICE.get(tc)
            if mapped:
                fields["tool_choice"] = mapped
        elif isinstance(tc, dict):
            tc_type = tc.get("type")
            if tc_type == "tool":
                fields["tool_choice"] = {
                    "type": "function",
                    "function": {"name": tc["name"]},
                }
            elif tc_type in _ANTHROPIC_TO_OPENAI_TOOL_CHOICE:
                fields["tool_choice"] = _ANTHROPIC_TO_OPENAI_TOOL_CHOICE[tc_type]
            else:
                fields["tool_choice"] = tc

    return fields


def _translate_content_blocks(role: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Translate Anthropic content-block array to OpenAI message format."""
    # Tool-result messages map directly to OpenAI ``role: "tool"``
    if role == "user" and any(b.get("type") == "tool_result" for b in blocks):
        tool_result = next(b for b in blocks if b["type"] == "tool_result")
        return {
            "role": "tool",
            "tool_call_id": tool_result.get("tool_use_id", ""),
            "content": _tool_result_content(tool_result),
        }

    # Single text block -> flatten to string
    if len(blocks) == 1 and blocks[0].get("type") == "text":
        return {"role": role, "content": blocks[0]["text"]}

    # Multimodal (text + image) -> array content
    openai_content: list[dict[str, Any]] = []
    for b in blocks:
        if b["type"] == "text":
            openai_content.append({"type": "text", "text": b["text"]})
        elif b["type"] == "image":
            src = b["source"]
            openai_content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{src['media_type']};base64,{src['data']}",
                    },
                }
            )
    if not openai_content:
        return {"role": role, "content": ""}
    return {"role": role, "content": openai_content}


def _tool_result_content(block: dict[str, Any]) -> str | list[dict[str, Any]]:
    """Extract the text content of a ``tool_result`` block."""
    inner = block.get("content", "")
    if isinstance(inner, list):
        texts = [b.get("text", "") for b in inner if b.get("type") == "text"]
        return "\n".join(texts)
    return str(inner)


# ---------------------------------------------------------------------------
# Response translation  (OpenAI pipeline response  ->  Anthropic /v1/messages)
# ---------------------------------------------------------------------------


def openai_response_to_anthropic(response: dict[str, Any]) -> dict[str, Any]:
    """Convert a pipeline response dict (OpenAI format) to Anthropic format."""
    choice = response.get("choices", [{}])[0]
    message = choice.get("message", {})
    usage = response.get("usage", {})

    content_blocks: list[dict[str, Any]] = []
    text_content = message.get("content", "") or ""
    tool_calls = message.get("tool_calls")

    if text_content:
        content_blocks.append({"type": "text", "text": text_content})

    if tool_calls:
        for tc in tool_calls:
            func = tc.get("function", {})
            arguments = func.get("arguments", "{}")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments) if arguments.strip() else {}
                except json.JSONDecodeError:
                    logger.warning("Failed to parse tool call arguments JSON")
                    arguments = {}
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": func.get("name", ""),
                    "input": arguments,
                }
            )

    return {
        "id": response.get("id", ""),
        "type": "message",
        "role": "assistant",
        "content": content_blocks,
        "model": response.get("model", ""),
        "stop_reason": _FINISH_REASON_TO_STOP.get(choice.get("finish_reason", ""))
        or choice.get("finish_reason"),
        "stop_sequence": choice.get("stop_sequence"),
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# ---------------------------------------------------------------------------
# Streaming  (OpenAI per-chunk  ->  Anthropic SSE events)
# ---------------------------------------------------------------------------


class AnthropicStreamTranslator:
    """Converts OpenAI-format streaming chunks to Anthropic SSE event strings.

    Usage::

        translator = AnthropicStreamTranslator(model, chunk_id)
        async for chunk in pipeline.process_streaming_request(...):
            for event in translator.translate(chunk):
                yield event
    """

    def __init__(self, model: str, message_id: str = "") -> None:
        self._model = model
        self._message_id = message_id or f"msg_{id(self)}"
        self._started = False
        self._block_index = 0
        self._text_block_open = False
        self._tool_block_open = False
        self._has_emitted_start = False
        self._has_emitted_stop = False

    def translate(self, chunk: dict[str, Any]) -> list[str]:
        """Convert one OpenAI streaming chunk to Anthropic SSE event strings."""
        if chunk.get("object") != "chat.completion.chunk" and "choices" not in chunk:
            return []

        choices = chunk.get("choices", [])
        if not choices:
            return []

        delta = choices[0].get("delta", {})
        finish_reason = choices[0].get("finish_reason")
        usage = chunk.get("usage", {})

        events: list[str] = []

        # First chunk always emits message_start (even if role-only)
        if not self._has_emitted_start:
            self._has_emitted_start = True
            events.append(
                self._sse(
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            "id": self._message_id,
                            "type": "message",
                            "role": "assistant",
                            "content": [],
                            "model": self._model,
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 0, "output_tokens": 0},
                        },
                    },
                )
            )

            # If this first chunk already carries content, open first block too
            content = delta.get("content", "")
            tool_calls = delta.get("tool_calls")
            if content or tool_calls or finish_reason:
                if content or (not tool_calls and not finish_reason):
                    events.append(self._open_text_block())
                elif tool_calls:
                    events.append(self._open_text_block())
                    events.append(self._close_text_block())
                    for tc in tool_calls:
                        events.append(self._open_tool_block(tc))

        # Text delta
        content = delta.get("content", "")
        if content:
            if not self._text_block_open:
                events.append(self._open_text_block())
            events.append(
                self._sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self._block_index - 1
                        if self._text_block_open
                        else self._block_index,
                        "delta": {"type": "text_delta", "text": content},
                    },
                )
            )

        # Tool-call deltas
        tool_calls = delta.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                func = tc.get("function", {})
                name = func.get("name", "")
                arguments = func.get("arguments", "")

                if name:
                    if self._text_block_open:
                        events.append(self._close_text_block())
                    if not self._tool_block_open:
                        events.append(self._open_text_block())
                        events.append(self._close_text_block())
                        events.append(self._open_tool_block(tc))
                    else:
                        events.append(self._open_tool_block(tc))
                elif arguments:
                    idx = self._block_index - 1 if self._tool_block_open else self._block_index
                    events.append(
                        self._sse(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": idx,
                                "delta": {"type": "input_json_delta", "partial_json": arguments},
                            },
                        )
                    )

        # Final chunk
        if finish_reason:
            if self._text_block_open:
                events.append(self._close_text_block())
            if self._tool_block_open:
                events.append(self._close_tool_block())
            self._has_emitted_stop = True
            events.append(
                self._sse(
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {
                            "stop_reason": _FINISH_REASON_TO_STOP.get(finish_reason, finish_reason),
                            "stop_sequence": None,
                        },
                        "usage": {
                            "input_tokens": usage.get("prompt_tokens", 0),
                            "output_tokens": usage.get("completion_tokens", 0),
                        },
                    },
                )
            )
            events.append(self._sse("message_stop", {"type": "message_stop"}))

        return events

    # -- helpers ---------------------------------------------------------

    def _open_text_block(self) -> str:
        self._text_block_open = True
        self._tool_block_open = False
        idx = self._block_index
        self._block_index += 1
        return self._sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": idx,
                "content_block": {"type": "text", "text": ""},
            },
        )

    def _close_text_block(self) -> str:
        self._text_block_open = False
        return self._sse(
            "content_block_stop",
            {
                "type": "content_block_stop",
                "index": self._block_index - 1,
            },
        )

    def _close_tool_block(self) -> str:
        self._tool_block_open = False
        return self._sse(
            "content_block_stop",
            {
                "type": "content_block_stop",
                "index": self._block_index - 1,
            },
        )

    def _open_tool_block(self, tc: dict[str, Any]) -> str:
        self._tool_block_open = True
        self._text_block_open = False
        idx = self._block_index
        self._block_index += 1
        func = tc.get("function", {})
        return self._sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": idx,
                "content_block": {
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": func.get("name", ""),
                    "input": {},
                },
            },
        )

    @staticmethod
    def _sse(event: str, data: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

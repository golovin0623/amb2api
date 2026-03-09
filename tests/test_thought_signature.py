"""Tests for thought_signature preservation through the request/response round-trip.

Gemini thinking models (e.g., gemini-3-pro-preview) include a thoughtSignature
field in response parts that have thought=True. This signature must be preserved
so that subsequent multi-turn requests (especially function calling) can send it
back, otherwise Vertex AI rejects the request with 'missing thought_signature'.
"""
import json

from src.transform.openai_transfer import (
    _extract_content_and_reasoning,
    _build_message_with_reasoning,
    gemini_response_to_openai,
    gemini_stream_chunk_to_openai,
    assembly_response_to_openai,
)
from src.services.assembly_client import _sanitize_messages
from src.models.models import OpenAIChatMessage


# ---------------------------------------------------------------------------
# _extract_content_and_reasoning
# ---------------------------------------------------------------------------


def test_extract_content_and_reasoning_returns_thought_signature():
    parts = [
        {"text": "Let me think...", "thought": True, "thoughtSignature": "sig123"},
        {"text": "Final answer."},
    ]
    content, reasoning, sig = _extract_content_and_reasoning(parts)
    assert content == "Final answer."
    assert reasoning == "Let me think..."
    assert sig == "sig123"


def test_extract_content_and_reasoning_returns_none_when_no_signature():
    parts = [
        {"text": "thinking", "thought": True},
        {"text": "content"},
    ]
    content, reasoning, sig = _extract_content_and_reasoning(parts)
    assert content == "content"
    assert reasoning == "thinking"
    assert sig is None


def test_extract_content_and_reasoning_uses_last_signature():
    parts = [
        {"text": "chunk1", "thought": True, "thoughtSignature": "sigA"},
        {"text": "chunk2", "thought": True, "thoughtSignature": "sigB"},
        {"text": "answer"},
    ]
    content, reasoning, sig = _extract_content_and_reasoning(parts)
    assert reasoning == "chunk1chunk2"
    assert sig == "sigB"


# ---------------------------------------------------------------------------
# _build_message_with_reasoning
# ---------------------------------------------------------------------------


def test_build_message_includes_thought_signature():
    msg = _build_message_with_reasoning("assistant", "hi", "thinking", "sig123")
    assert msg["thought_signature"] == "sig123"
    assert msg["reasoning_content"] == "thinking"


def test_build_message_omits_thought_signature_when_none():
    msg = _build_message_with_reasoning("assistant", "hi", "thinking", None)
    assert "thought_signature" not in msg


# ---------------------------------------------------------------------------
# gemini_response_to_openai
# ---------------------------------------------------------------------------


def test_gemini_response_to_openai_preserves_thought_signature():
    gemini = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "text": "Let me think about calling the tool...",
                            "thought": True,
                            "thoughtSignature": "base64sig==",
                        },
                        {"text": "I will call get_weather."},
                    ],
                },
                "finishReason": "STOP",
            }
        ]
    }
    openai_resp = gemini_response_to_openai(gemini, "gemini-3-pro-preview")
    msg = openai_resp["choices"][0]["message"]
    assert msg["reasoning_content"] == "Let me think about calling the tool..."
    assert msg["thought_signature"] == "base64sig=="
    assert msg["content"] == "I will call get_weather."


def test_gemini_response_to_openai_no_signature_when_absent():
    gemini = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"text": "Hello!"}],
                },
                "finishReason": "STOP",
            }
        ]
    }
    openai_resp = gemini_response_to_openai(gemini, "gemini-2.0-flash")
    msg = openai_resp["choices"][0]["message"]
    assert "thought_signature" not in msg


# ---------------------------------------------------------------------------
# gemini_stream_chunk_to_openai
# ---------------------------------------------------------------------------


def test_gemini_stream_chunk_includes_thought_signature():
    chunk = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "text": "reasoning...",
                            "thought": True,
                            "thoughtSignature": "stream_sig",
                        }
                    ],
                }
            }
        ]
    }
    openai_chunk = gemini_stream_chunk_to_openai(chunk, "gemini-3-pro-preview", "resp-1")
    delta = openai_chunk["choices"][0]["delta"]
    assert delta["reasoning_content"] == "reasoning..."
    assert delta["thought_signature"] == "stream_sig"


# ---------------------------------------------------------------------------
# assembly_response_to_openai
# ---------------------------------------------------------------------------


def test_assembly_response_preserves_reasoning_and_thought_signature():
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "calling tool",
                    "reasoning_content": "internal reasoning",
                    "thought_signature": "asm_sig",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": "{\"city\":\"SF\"}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    openai_resp = assembly_response_to_openai(response, "gemini-3-pro-preview")
    msg = openai_resp["choices"][0]["message"]
    assert msg["reasoning_content"] == "internal reasoning"
    assert msg["thought_signature"] == "asm_sig"
    assert len(msg["tool_calls"]) == 1


def test_assembly_response_preserves_camelcase_thought_signature():
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "response",
                    "thoughtSignature": "camel_sig",
                },
                "finish_reason": "stop",
            }
        ]
    }
    openai_resp = assembly_response_to_openai(response, "gemini-3-pro-preview")
    msg = openai_resp["choices"][0]["message"]
    assert msg["thought_signature"] == "camel_sig"


# ---------------------------------------------------------------------------
# _sanitize_messages — round-trip preservation
# ---------------------------------------------------------------------------


def test_sanitize_messages_preserves_thought_signature_on_assistant_with_tool_calls():
    """thought_signature must survive the sanitize round-trip for function calling."""
    messages = [
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "thinking about the tool",
            "thought_signature": "sig_abc",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{\"city\":\"SF\"}"},
                }
            ],
        }
    ]
    sanitized = _sanitize_messages(messages)
    # Should produce an assistant message (with reasoning/sig) + function_call message
    assert len(sanitized) == 2
    assistant_msg = sanitized[0]
    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["reasoning_content"] == "thinking about the tool"
    assert assistant_msg["thought_signature"] == "sig_abc"
    fc_msg = sanitized[1]
    assert fc_msg["type"] == "function_call"
    # thought_signature must propagate to function_call message and its content blocks
    assert fc_msg["thought_signature"] == "sig_abc"
    assert fc_msg["content"][0]["thoughtSignature"] == "sig_abc"
    assert fc_msg["content"][0]["tool_use"]["thoughtSignature"] == "sig_abc"


def test_sanitize_messages_preserves_thought_signature_on_regular_assistant():
    """thought_signature on a plain assistant message (no tool_calls)."""
    messages = [
        {
            "role": "assistant",
            "content": "Hello!",
            "reasoning_content": "thinking first",
            "thought_signature": "plain_sig",
        }
    ]
    sanitized = _sanitize_messages(messages)
    assert len(sanitized) == 1
    msg = sanitized[0]
    assert msg["role"] == "assistant"
    assert msg["reasoning_content"] == "thinking first"
    assert msg["thought_signature"] == "plain_sig"


def test_sanitize_messages_emits_assistant_msg_when_only_reasoning_and_tool_calls():
    """Even if content is empty, assistant msg should appear when reasoning_content exists."""
    messages = [
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "I need to call the function",
            "thought_signature": "empty_content_sig",
            "tool_calls": [
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "search", "arguments": "{}"},
                }
            ],
        }
    ]
    sanitized = _sanitize_messages(messages)
    assert len(sanitized) == 2
    # First message is the assistant message with reasoning
    assert sanitized[0]["role"] == "assistant"
    assert sanitized[0]["reasoning_content"] == "I need to call the function"
    assert sanitized[0]["thought_signature"] == "empty_content_sig"
    # Second is the function_call with thought_signature propagated
    assert sanitized[1]["type"] == "function_call"
    assert sanitized[1]["thought_signature"] == "empty_content_sig"
    assert sanitized[1]["content"][0]["thoughtSignature"] == "empty_content_sig"


def test_sanitize_messages_no_extra_fields_when_no_reasoning():
    """Regular messages should not have reasoning_content or thought_signature."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    sanitized = _sanitize_messages(messages)
    assert len(sanitized) == 2
    assert "reasoning_content" not in sanitized[0]
    assert "thought_signature" not in sanitized[0]
    assert "reasoning_content" not in sanitized[1]
    assert "thought_signature" not in sanitized[1]


def test_sanitize_messages_function_call_no_thought_signature_when_absent():
    """function_call content blocks should NOT have thoughtSignature when absent."""
    messages = [
        {
            "role": "assistant",
            "content": "calling tool",
            "tool_calls": [
                {
                    "id": "call_x",
                    "type": "function",
                    "function": {"name": "do_thing", "arguments": "{}"},
                }
            ],
        }
    ]
    sanitized = _sanitize_messages(messages)
    fc_msg = [m for m in sanitized if m.get("type") == "function_call"][0]
    assert "thought_signature" not in fc_msg
    assert "thoughtSignature" not in fc_msg["content"][0]
    assert "thoughtSignature" not in fc_msg["content"][0]["tool_use"]


def test_sanitize_messages_multiple_tool_calls_all_get_thought_signature():
    """All function_call messages from one assistant should get thought_signature."""
    messages = [
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "planning two calls",
            "thought_signature": "multi_sig",
            "tool_calls": [
                {
                    "id": "call_a",
                    "type": "function",
                    "function": {"name": "search", "arguments": "{}"},
                },
                {
                    "id": "call_b",
                    "type": "function",
                    "function": {"name": "browse", "arguments": "{}"},
                },
            ],
        }
    ]
    sanitized = _sanitize_messages(messages)
    fc_msgs = [m for m in sanitized if m.get("type") == "function_call"]
    assert len(fc_msgs) == 2
    for fc in fc_msgs:
        assert fc["thought_signature"] == "multi_sig"
        assert fc["content"][0]["thoughtSignature"] == "multi_sig"
        assert fc["content"][0]["tool_use"]["thoughtSignature"] == "multi_sig"


# ---------------------------------------------------------------------------
# OpenAIChatMessage model accepts thought_signature
# ---------------------------------------------------------------------------


def test_openai_chat_message_accepts_thought_signature():
    msg = OpenAIChatMessage(
        role="assistant",
        content="hello",
        reasoning_content="thinking",
        thought_signature="model_sig",
    )
    assert msg.thought_signature == "model_sig"
    assert msg.reasoning_content == "thinking"


def test_openai_chat_message_thought_signature_defaults_to_none():
    msg = OpenAIChatMessage(role="user", content="hi")
    assert msg.thought_signature is None

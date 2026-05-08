"""
Tests for AssemblyAI LLM Gateway prompt-caching support.

Covers:
- Anthropic-style ``cache_control`` blocks pass through Anthropic→OpenAI conversion
- ``_sanitize_messages`` keeps ``cache_control`` on the upstream payload
- Upstream responses surface ``cache_creation`` token counts on OpenAI usage
- Anthropic responses surface ``cache_read_input_tokens`` and
  ``cache_creation_input_tokens`` so SDKs render cache hits correctly
"""
from src.services.assembly_client import _sanitize_messages
from src.transform.anthropic_transfer import (
    anthropic_request_to_openai_payload,
    openai_response_to_anthropic_message,
    convert_openai_sse_payload_to_anthropic_events,
)
from src.transform.claude_to_openai import convert_claude_request_to_openai
from src.transform.openai_to_claude import openai_response_to_anthropic
from src.transform.openai_transfer import assembly_response_to_openai


def _example_cache_control():
    return {"type": "ephemeral", "ttl": "5m"}


def test_anthropic_request_lifts_system_cache_control_to_message():
    cc = _example_cache_control()
    payload = anthropic_request_to_openai_payload(
        {
            "model": "claude-4.5-sonnet-20250929",
            "system": [
                {"type": "text", "text": "You are a careful assistant."},
                {"type": "text", "text": "Always use tools when needed.", "cache_control": cc},
            ],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            "max_tokens": 64,
            "prompt_cache_retention": "24h",
            "prompt_cache_key": "support-bot-v1",
        }
    )

    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][0]["cache_control"] == cc
    # Top-level cache fields propagate through.
    assert payload["prompt_cache_retention"] == "24h"
    assert payload["prompt_cache_key"] == "support-bot-v1"


def test_anthropic_request_preserves_message_block_cache_control():
    cc = _example_cache_control()
    payload = convert_claude_request_to_openai(
        {
            "model": "claude-4.5-sonnet-20250929",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Long context to cache..."},
                        {"type": "text", "text": "actual question", "cache_control": cc},
                    ],
                }
            ],
        }
    )

    user_msg = payload["messages"][0]
    assert user_msg["role"] == "user"
    assert user_msg["cache_control"] == cc


def test_sanitize_messages_keeps_cache_control_on_user_message():
    cc = _example_cache_control()
    sanitized = _sanitize_messages(
        [{"role": "user", "content": "hello", "cache_control": cc}]
    )
    assert sanitized == [
        {"role": "user", "content": "hello", "cache_control": cc}
    ]


def test_sanitize_messages_keeps_cache_control_on_tool_result_message():
    cc = _example_cache_control()
    sanitized = _sanitize_messages(
        [
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "{\"ok\": true}",
                "cache_control": cc,
            }
        ]
    )
    assert len(sanitized) == 1
    assert sanitized[0]["type"] == "function_call_output"
    assert sanitized[0]["cache_control"] == cc


def test_assembly_response_to_openai_surfaces_cache_creation():
    upstream = {
        "id": "resp_1",
        "model": "claude-4.5-sonnet-20250929",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 20,
            "total_tokens": 1020,
            "prompt_tokens_details": {
                "cached_tokens": 750,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 200,
                    "ephemeral_1h_input_tokens": 50,
                },
            },
        },
    }

    openai_resp = assembly_response_to_openai(upstream, "claude-4.5-sonnet-20250929")
    usage = openai_resp["usage"]
    assert usage["cached_tokens"] == 750
    details = usage["prompt_tokens_details"]
    assert details["cached_tokens"] == 750
    assert details["cache_creation"] == {
        "ephemeral_5m_input_tokens": 200,
        "ephemeral_1h_input_tokens": 50,
    }


def test_anthropic_message_usage_includes_cache_fields():
    openai_response = {
        "id": "resp_2",
        "model": "claude-4.5-sonnet-20250929",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 500,
            "completion_tokens": 8,
            "cached_tokens": 320,
            "prompt_tokens_details": {
                "cached_tokens": 320,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 100,
                    "ephemeral_1h_input_tokens": 0,
                },
            },
        },
    }

    msg = openai_response_to_anthropic_message(openai_response)
    assert msg["usage"]["input_tokens"] == 500
    assert msg["usage"]["output_tokens"] == 8
    assert msg["usage"]["cache_read_input_tokens"] == 320
    assert msg["usage"]["cache_creation_input_tokens"] == 100
    assert msg["usage"]["cache_creation"] == {
        "ephemeral_5m_input_tokens": 100,
        "ephemeral_1h_input_tokens": 0,
    }

    # Same behavior in the live converter used by the router.
    msg_live = openai_response_to_anthropic(openai_response)
    assert msg_live["usage"]["cache_read_input_tokens"] == 320
    assert msg_live["usage"]["cache_creation_input_tokens"] == 100


def test_anthropic_streaming_message_delta_carries_cache_usage():
    payload = (
        '{"id":"chatcmpl_1","model":"claude-4.5-sonnet-20250929",'
        '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
        '"usage":{"completion_tokens":12,'
        '"prompt_tokens_details":{"cached_tokens":250,'
        '"cache_creation":{"ephemeral_5m_input_tokens":100,"ephemeral_1h_input_tokens":0}}}}'
    )
    state = {}
    events = convert_openai_sse_payload_to_anthropic_events(payload, state=state)

    delta_events = [e for e in events if e.get("type") == "message_delta"]
    assert delta_events, "expected a message_delta event"
    usage = delta_events[0]["usage"]
    assert usage["output_tokens"] == 12
    assert usage["cache_read_input_tokens"] == 250
    assert usage["cache_creation_input_tokens"] == 100
    assert "input_tokens" not in usage

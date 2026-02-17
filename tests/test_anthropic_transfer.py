import json

from src.transform.anthropic_transfer import (
    anthropic_request_to_openai_payload,
    openai_response_to_anthropic_message,
)
from src.models.models import ChatCompletionRequest


def test_anthropic_request_to_openai_payload_maps_core_fields():
    payload = anthropic_request_to_openai_payload(
        {
            "model": "claude-4.5-sonnet-20250929",
            "system": [{"type": "text", "text": "You are helpful"}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
            "max_tokens": 256,
            "temperature": 0.2,
            "top_p": 0.95,
            "top_k": 32,
            "stream": True,
            "stop_sequences": ["STOP"],
            "tools": [
                {
                    "name": "lookup",
                    "description": "lookup tool",
                    "input_schema": {
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                        "required": ["q"],
                    },
                }
            ],
            "tool_choice": {"type": "any"},
        }
    )

    # Can be validated by existing OpenAI request schema.
    validated = ChatCompletionRequest(**payload)
    assert validated.model == "claude-4.5-sonnet-20250929"
    assert validated.stream is True
    assert payload["stop"] == ["STOP"]
    assert payload["tool_choice"] == "required"
    assert payload["tools"][0]["function"]["name"] == "lookup"
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][1]["role"] == "user"


def test_anthropic_request_to_openai_payload_maps_tool_use_and_tool_result():
    payload = anthropic_request_to_openai_payload(
        {
            "model": "claude-4.5-sonnet-20250929",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "abc"}}
                    ],
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "result-ok"}],
                },
            ],
        }
    )

    assert payload["messages"][0]["role"] == "assistant"
    tool_calls = payload["messages"][0]["tool_calls"]
    assert isinstance(tool_calls, list) and len(tool_calls) == 1
    assert tool_calls[0]["id"] == "toolu_1"
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {"q": "abc"}

    assert payload["messages"][1]["role"] == "tool"
    assert payload["messages"][1]["tool_call_id"] == "toolu_1"
    assert payload["messages"][1]["content"] == "result-ok"


def test_openai_response_to_anthropic_message_maps_text_usage_and_stop_reason():
    message = openai_response_to_anthropic_message(
        {
            "id": "chatcmpl_1",
            "model": "claude-4.5-sonnet-20250929",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello"},
                    "finish_reason": "length",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17},
        }
    )

    assert message["id"] == "chatcmpl_1"
    assert message["type"] == "message"
    assert message["content"] == [{"type": "text", "text": "hello"}]
    assert message["stop_reason"] == "max_tokens"
    assert message["usage"] == {"input_tokens": 10, "output_tokens": 7}


def test_openai_response_to_anthropic_message_maps_tool_calls_to_tool_use():
    message = openai_response_to_anthropic_message(
        {
            "model": "claude-4.5-sonnet-20250929",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": "{\"q\":\"abc\"}"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        },
        fallback_model="claude-4.5-sonnet-20250929",
    )

    assert message["stop_reason"] == "tool_use"
    assert message["content"][0]["type"] == "tool_use"
    assert message["content"][0]["id"] == "call_1"
    assert message["content"][0]["name"] == "lookup"
    assert message["content"][0]["input"] == {"q": "abc"}

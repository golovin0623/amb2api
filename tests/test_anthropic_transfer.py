import json

from src.transform.anthropic_transfer import (
    anthropic_request_to_openai_payload,
    convert_openai_sse_payload_to_anthropic_events,
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
    assert payload["tool_choice"] == "auto"
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


def test_openai_response_to_anthropic_message_parses_python_literal_tool_args():
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
                                "id": "call_2",
                                "type": "function",
                                "function": {"name": "Task", "arguments": "{'description': 'scan repo'}"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        },
        fallback_model="claude-4.5-sonnet-20250929",
    )

    assert message["content"][0]["type"] == "tool_use"
    assert message["content"][0]["name"] == "Task"
    assert message["content"][0]["input"]["description"] == "scan repo"
    assert message["content"][0]["input"]["prompt"] == "scan repo"
    assert message["content"][0]["input"]["subagent_type"] == "general-purpose"


def test_openai_response_to_anthropic_message_reads_function_input_when_arguments_missing():
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
                                "id": "call_3",
                                "type": "function",
                                "function": {"name": "Task", "input": {"description": "scan repo"}},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        },
        fallback_model="claude-4.5-sonnet-20250929",
    )

    assert message["content"][0]["type"] == "tool_use"
    assert message["content"][0]["name"] == "Task"
    assert message["content"][0]["input"]["description"] == "scan repo"
    assert message["content"][0]["input"]["prompt"] == "scan repo"
    assert message["content"][0]["input"]["subagent_type"] == "general-purpose"


def test_openai_response_to_anthropic_message_normalizes_task_input_aliases():
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
                                "id": "call_4",
                                "type": "function",
                                "function": {
                                    "name": "Task",
                                    "arguments": "{\"task\":\"scan repo\", \"subagentType\":\"explore\"}",
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        },
        fallback_model="claude-4.5-sonnet-20250929",
    )

    tool_input = message["content"][0]["input"]
    assert tool_input["prompt"] == "scan repo"
    assert tool_input["description"] == "scan repo"
    assert tool_input["subagent_type"] == "Explore"
    assert "task" not in tool_input
    assert "subagentType" not in tool_input


def test_convert_openai_sse_payload_to_anthropic_events_normalizes_task_tool_delta():
    state = {"model": "claude-4.5-sonnet-20250929"}
    payload = json.dumps(
        {
            "id": "chatcmpl_x",
            "model": "claude-4.5-sonnet-20250929",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "id": "call_5",
                                "type": "function",
                                "function": {
                                    "name": "Task",
                                    "input": {"description": "scan repo"},
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        },
        ensure_ascii=False,
    )

    events = convert_openai_sse_payload_to_anthropic_events(payload, state=state, default_model="claude-4.5-sonnet-20250929")
    tool_event = next(
        event for event in events if event.get("type") == "content_block_start" and event.get("content_block", {}).get("type") == "tool_use"
    )
    tool_input = tool_event["content_block"]["input"]
    assert tool_input["description"] == "scan repo"
    assert tool_input["prompt"] == "scan repo"
    assert tool_input["subagent_type"] == "general-purpose"


def test_openai_response_to_anthropic_message_strips_task_raw_key_and_keeps_required_fields():
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
                                "id": "call_6",
                                "type": "function",
                                "function": {"name": "Task", "arguments": "not-a-json-argument"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        },
        fallback_model="claude-4.5-sonnet-20250929",
    )

    tool_input = message["content"][0]["input"]
    assert set(tool_input.keys()).issubset({"description", "prompt", "subagent_type", "run_in_background", "resume"})
    assert isinstance(tool_input["description"], str) and tool_input["description"]
    assert isinstance(tool_input["prompt"], str) and tool_input["prompt"]
    assert tool_input["subagent_type"] == "general-purpose"

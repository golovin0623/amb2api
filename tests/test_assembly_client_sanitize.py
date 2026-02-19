import json

from src.services.assembly_client import (
    _build_claude_tool_fallback_messages,
    _ensure_tool_block_required_fields,
    _is_assistant_prefill_error,
    _is_tool_use_input_validation_error,
    _messages_end_with_user,
    _sanitize_messages,
)


def test_sanitize_messages_normalizes_tool_call_arguments_to_input_object():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "lookup_weather",
                        "arguments": "{\"city\":\"Tokyo\"}",
                    },
                }
            ],
        }
    ]

    sanitized = _sanitize_messages(messages)
    assert len(sanitized) == 1
    fc = sanitized[0]
    assert fc["type"] == "function_call"
    assert fc["tool_call_id"] == "call_1"
    assert fc["name"] == "lookup_weather"
    assert fc["input"] == {"city": "Tokyo"}
    assert json.loads(fc["arguments"]) == {"city": "Tokyo"}
    assert fc["content"] == [
        {
            "type": "tool_use",
            "id": "call_1",
            "name": "lookup_weather",
            "input": {"city": "Tokyo"},
            "tool_use": {
                "id": "call_1",
                "name": "lookup_weather",
                "input": {"city": "Tokyo"},
            },
        }
    ]


def test_sanitize_messages_supports_function_input_when_arguments_missing():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "input": {"path": "/tmp/a.txt"},
                    },
                }
            ],
        }
    ]

    sanitized = _sanitize_messages(messages)
    fc = sanitized[0]
    assert fc["input"] == {"path": "/tmp/a.txt"}
    assert json.loads(fc["arguments"]) == {"path": "/tmp/a.txt"}
    assert fc["content"][0]["type"] == "tool_use"
    assert fc["content"][0]["input"] == {"path": "/tmp/a.txt"}
    assert fc["content"][0]["tool_use"]["input"] == {"path": "/tmp/a.txt"}


def test_sanitize_messages_guarantees_input_for_empty_or_invalid_arguments():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "type": "function",
                    "function": {"name": "do_nothing", "arguments": ""},
                },
                {
                    "type": "function",
                    "function": {"name": "echo", "arguments": "{not-json"},
                },
            ],
        }
    ]

    sanitized = _sanitize_messages(messages)
    assert len(sanitized) == 2

    first = sanitized[0]
    assert first["tool_call_id"]
    assert first["input"] == {}
    assert json.loads(first["arguments"]) == {}
    assert first["content"][0]["type"] == "tool_use"
    assert first["content"][0]["input"] == {}
    assert first["content"][0]["tool_use"]["input"] == {}

    second = sanitized[1]
    assert second["tool_call_id"]
    assert second["input"] == {"raw": "{not-json"}
    assert json.loads(second["arguments"]) == {"raw": "{not-json"}
    assert second["content"][0]["type"] == "tool_use"
    assert second["content"][0]["input"] == {"raw": "{not-json"}
    assert second["content"][0]["tool_use"]["input"] == {"raw": "{not-json"}


def test_sanitize_messages_maps_tool_role_to_function_call_output_content_block():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_3",
                    "type": "function",
                    "function": {
                        "name": "lookup_user",
                        "arguments": "{\"id\":123}",
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_3",
            "content": "{\"ok\":true}",
        },
    ]

    sanitized = _sanitize_messages(messages)
    assert len(sanitized) == 2
    out = sanitized[1]
    assert out["type"] == "function_call_output"
    assert out["tool_call_id"] == "call_3"
    assert out["name"] == "lookup_user"
    assert out["output"] == "{\"ok\":true}"
    assert out["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "call_3",
            "content": "{\"ok\":true}",
            "tool_result": {
                "tool_use_id": "call_3",
                "content": "{\"ok\":true}",
            },
        }
    ]


def test_build_claude_tool_fallback_messages_converts_function_messages_to_role_content():
    sanitized_messages = [
        {"role": "system", "content": "sys"},
        {
            "type": "function_call",
            "tool_call_id": "call_9",
            "name": "Task",
            "input": {"description": "x"},
        },
        {
            "type": "function_call_output",
            "tool_call_id": "call_9",
            "name": "Task",
            "output": "{\"ok\":true}",
        },
    ]

    fallback = _build_claude_tool_fallback_messages(sanitized_messages)
    assert fallback[0] == {"role": "system", "content": "sys"}

    assistant_msg = fallback[1]
    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["content"][0]["tool_use"]["input"] == {"description": "x"}

    user_msg = fallback[2]
    assert user_msg["role"] == "user"
    assert user_msg["content"][0]["tool_result"]["tool_use_id"] == "call_9"
    assert user_msg["content"][0]["tool_result"]["content"] == "{\"ok\":true}"


def test_is_tool_use_input_validation_error_detects_known_shape():
    class DummyResp:
        status_code = 400
        text = "{\"message\":\"ValidationException: messages.3.content.0.tool_use.input: Field required\"}"

        def json(self):
            return {
                "message": "ValidationException: messages.3.content.0.tool_use.input: Field required"
            }

    assert _is_tool_use_input_validation_error(DummyResp()) is True


def test_is_assistant_prefill_error_detects_known_shape():
    class DummyResp:
        status_code = 400
        text = (
            "{\"message\":\"ValidationException: This model does not support assistant message prefill. "
            "The conversation must end with a user message.\"}"
        )

        def json(self):
            return {
                "message": (
                    "ValidationException: This model does not support assistant message prefill. "
                    "The conversation must end with a user message."
                )
            }

    assert _is_assistant_prefill_error(DummyResp()) is True


def test_messages_end_with_user_checks_last_role():
    assert _messages_end_with_user([{"role": "user", "content": "ok"}]) is True
    assert _messages_end_with_user([{"role": "assistant", "content": "no"}]) is False


def test_ensure_tool_block_required_fields_fills_missing_tool_use_input():
    messages = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_x",
                    "name": "Task",
                }
            ],
        }
    ]

    fixed = _ensure_tool_block_required_fields(messages)
    block = fixed[0]["content"][0]
    assert block["input"] == {}
    assert block["tool_use"]["input"] == {}


def test_ensure_tool_block_required_fields_merges_nested_tool_use_shape():
    messages = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "tool_use": {
                        "id": "call_nested",
                        "name": "Task",
                        "input": {"description": "hello"},
                    },
                }
            ],
        }
    ]

    fixed = _ensure_tool_block_required_fields(messages)
    block = fixed[0]["content"][0]
    assert block["id"] == "call_nested"
    assert block["name"] == "Task"
    assert block["input"] == {"description": "hello"}


def test_ensure_tool_block_required_fields_accepts_tool_use_without_type():
    messages = [
        {
            "role": "assistant",
            "content": [
                {
                    "tool_use": {
                        "id": "call_without_type",
                        "name": "Task",
                    }
                }
            ],
        }
    ]

    fixed = _ensure_tool_block_required_fields(messages)
    block = fixed[0]["content"][0]
    assert block["tool_use"]["id"] == "call_without_type"
    assert block["tool_use"]["name"] == "Task"
    assert block["tool_use"]["input"] == {}


def test_build_claude_tool_fallback_messages_merges_assistant_text_with_tool_use():
    sanitized_messages = [
        {"role": "assistant", "content": "thinking..."},
        {
            "type": "function_call",
            "tool_call_id": "call_merge",
            "name": "Task",
            "input": {"description": "merge"},
        },
    ]

    fallback = _build_claude_tool_fallback_messages(sanitized_messages)
    assert len(fallback) == 1
    assert fallback[0]["role"] == "assistant"
    assert isinstance(fallback[0]["content"], list)
    assert fallback[0]["content"][0] == {"type": "text", "text": "thinking..."}
    assert fallback[0]["content"][1]["type"] == "tool_use"
    assert fallback[0]["content"][1]["tool_use"]["input"] == {"description": "merge"}


def test_build_claude_tool_fallback_messages_merges_user_text_with_tool_result():
    sanitized_messages = [
        {"role": "user", "content": "tool output follows"},
        {
            "type": "function_call_output",
            "tool_call_id": "call_merge_result",
            "name": "Task",
            "output": "{\"ok\":true}",
        },
    ]

    fallback = _build_claude_tool_fallback_messages(sanitized_messages)
    assert len(fallback) == 1
    assert fallback[0]["role"] == "user"
    assert isinstance(fallback[0]["content"], list)
    assert fallback[0]["content"][0] == {"type": "text", "text": "tool output follows"}
    assert fallback[0]["content"][1]["type"] == "tool_result"
    assert fallback[0]["content"][1]["tool_result"]["content"] == "{\"ok\":true}"

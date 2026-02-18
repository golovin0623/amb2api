import json

from src.services.assembly_client import _sanitize_messages


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

    second = sanitized[1]
    assert second["tool_call_id"]
    assert second["input"] == {"raw": "{not-json"}
    assert json.loads(second["arguments"]) == {"raw": "{not-json"}

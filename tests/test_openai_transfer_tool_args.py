import json

from src.transform.openai_transfer import assembly_response_to_openai


def test_assembly_response_to_openai_uses_function_input_when_arguments_missing():
    response = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "Task",
                                "input": {"description": "scan repo"},
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }

    openai = assembly_response_to_openai(response, model="claude-opus-4-6")
    tool_call = openai["choices"][0]["message"]["tool_calls"][0]
    assert json.loads(tool_call["function"]["arguments"]) == {"description": "scan repo"}


def test_assembly_response_to_openai_normalizes_python_literal_arguments():
    response = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {
                                "name": "Task",
                                "arguments": "{'description': 'scan repo'}",
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }

    openai = assembly_response_to_openai(response, model="claude-opus-4-6")
    tool_call = openai["choices"][0]["message"]["tool_calls"][0]
    assert json.loads(tool_call["function"]["arguments"]) == {"description": "scan repo"}


def test_assembly_response_to_openai_extracts_tool_use_from_content_blocks():
    response = {
        "choices": [
            {
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "Task",
                            "input": {"description": "scan repo", "subagent_type": "Explore"},
                        }
                    ]
                },
                "finish_reason": "stop",
            }
        ]
    }

    openai = assembly_response_to_openai(response, model="claude-opus-4-6")
    assert openai["choices"][0]["finish_reason"] == "tool_calls"
    tool_call = openai["choices"][0]["message"]["tool_calls"][0]
    assert tool_call["id"] == "toolu_1"
    assert tool_call["function"]["name"] == "Task"
    assert json.loads(tool_call["function"]["arguments"]) == {
        "description": "scan repo",
        "subagent_type": "Explore",
    }


def test_assembly_response_to_openai_extracts_nested_tool_use_block():
    response = {
        "choices": [
            {
                "message": {
                    "content": [
                        {
                            "tool_use": {
                                "id": "toolu_2",
                                "name": "Task",
                                "input": {"description": "scan repo"},
                            }
                        }
                    ]
                },
                "finish_reason": "stop",
            }
        ]
    }

    openai = assembly_response_to_openai(response, model="claude-opus-4-6")
    assert openai["choices"][0]["finish_reason"] == "tool_calls"
    tool_call = openai["choices"][0]["message"]["tool_calls"][0]
    assert tool_call["id"] == "toolu_2"
    assert tool_call["function"]["name"] == "Task"
    assert json.loads(tool_call["function"]["arguments"]) == {"description": "scan repo"}

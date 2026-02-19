import json
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from src.api import openai_router


class _Trace:
    def __init__(self, trace_id: str):
        self.trace_id = trace_id
        self.model = "pending"
        self.metadata = {}

    def mark(self, _):
        return None


class _Tracker:
    def start_trace(self, trace_id, _):
        return _Trace(trace_id)

    async def end_trace(self, *args, **kwargs):
        return None


def _build_app() -> TestClient:
    app = FastAPI()
    app.include_router(openai_router.router)
    return TestClient(app)


def _anthropic_body(stream: bool = False):
    return {
        "model": "claude-4.5-sonnet-20250929",
        "max_tokens": 128,
        "stream": stream,
        "messages": [{"role": "user", "content": "hello"}],
    }


def test_anthropic_messages_non_stream_x_api_key_and_bearer():
    client = _build_app()
    upstream_response = JSONResponse(
        content={
            "id": "resp_1",
            "model": "claude-4.5-sonnet-20250929",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        },
        status_code=200,
    )

    with patch("src.api.openai_router.get_performance_tracker", new=AsyncMock(return_value=_Tracker())):
        with patch("src.api.openai_router.send_assembly_request", new=AsyncMock(return_value=upstream_response)):
            res1 = client.post("/v1/messages", json=_anthropic_body(), headers={"x-api-key": "pwd"})
            assert res1.status_code == 200
            assert res1.json()["type"] == "message"

            res2 = client.post(
                "/v1/messages",
                json=_anthropic_body(),
                headers={"Authorization": "Bearer pwd"},
            )
            assert res2.status_code == 200
            assert res2.json()["type"] == "message"


def test_anthropic_messages_auth_failure_returns_anthropic_error():
    client = _build_app()
    res = client.post("/v1/messages", json=_anthropic_body(), headers={"x-api-key": "wrong"})
    assert res.status_code == 403
    body = res.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "authentication_error"


def test_anthropic_count_tokens_and_models_header_switch():
    client = _build_app()

    count_res = client.post(
        "/v1/messages/count_tokens",
        json=_anthropic_body(),
        headers={"x-api-key": "pwd"},
    )
    assert count_res.status_code == 200
    assert isinstance(count_res.json().get("input_tokens"), int)
    assert count_res.json()["input_tokens"] > 0

    openai_models = client.get("/v1/models")
    assert openai_models.status_code == 200
    assert openai_models.json().get("object") == "list"

    anthropic_models = client.get("/v1/models", headers={"anthropic-version": "2023-06-01"})
    assert anthropic_models.status_code == 200
    payload = anthropic_models.json()
    assert isinstance(payload.get("data"), list)
    if payload["data"]:
        assert payload["data"][0]["type"] == "model"


def test_anthropic_messages_stream_outputs_anthropic_events():
    client = _build_app()

    async def _stream_gen():
        chunk1 = {
            "id": "chatcmpl_stream",
            "model": "claude-4.5-sonnet-20250929",
            "choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}],
        }
        chunk2 = {
            "id": "chatcmpl_stream",
            "model": "claude-4.5-sonnet-20250929",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"completion_tokens": 4},
        }
        yield f"data: {json.dumps(chunk1)}\n\n".encode("utf-8")
        yield f"data: {json.dumps(chunk2)}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"

    fake_openai_stream = StreamingResponse(_stream_gen(), media_type="text/event-stream")

    with patch("src.api.openai_router.get_performance_tracker", new=AsyncMock(return_value=_Tracker())):
        with patch("config.get_enable_real_streaming", new=AsyncMock(return_value=False)):
            with patch("src.api.openai_router.fake_stream_response_for_assembly", new=AsyncMock(return_value=fake_openai_stream)):
                res = client.post(
                    "/v1/messages",
                    json=_anthropic_body(stream=True),
                    headers={"x-api-key": "pwd"},
                )

    assert res.status_code == 200
    body = res.text
    assert "event: message_start" in body
    assert "event: content_block_delta" in body
    assert "event: message_stop" in body


def test_anthropic_messages_enforce_auto_tool_choice_when_task_not_available():
    client = _build_app()
    upstream_response = JSONResponse(
        content={
            "id": "resp_2",
            "model": "claude-4.5-sonnet-20250929",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        },
        status_code=200,
    )
    body = _anthropic_body(stream=False)
    body["tools"] = [
        {
            "name": "lookup",
            "description": "lookup tool",
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
        }
    ]

    send_mock = AsyncMock(return_value=upstream_response)
    with patch("src.api.openai_router.get_performance_tracker", new=AsyncMock(return_value=_Tracker())):
        with patch("src.api.openai_router.send_assembly_request", new=send_mock):
            res = client.post(
                "/v1/messages",
                json=body,
                headers={
                    "x-api-key": "pwd",
                    "user-agent": "claude-cli/2.1.45 (external, cli)",
                },
            )

    assert res.status_code == 200
    assert send_mock.await_count == 1
    request_obj = send_mock.await_args.args[0]
    assert getattr(request_obj, "tool_choice") == "auto"


def test_anthropic_messages_enforce_task_tool_choice_when_task_available():
    client = _build_app()
    upstream_response = JSONResponse(
        content={
            "id": "resp_3",
            "model": "claude-4.5-sonnet-20250929",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        },
        status_code=200,
    )
    body = _anthropic_body(stream=False)
    body["tools"] = [
        {
            "name": "Task",
            "description": "Task tool",
            "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}}},
        }
    ]

    send_mock = AsyncMock(return_value=upstream_response)
    with patch("src.api.openai_router.get_performance_tracker", new=AsyncMock(return_value=_Tracker())):
        with patch("src.api.openai_router.send_assembly_request", new=send_mock):
            res = client.post(
                "/v1/messages",
                json=body,
                headers={
                    "x-api-key": "pwd",
                    "user-agent": "claude-cli/2.1.45 (external, cli)",
                },
            )

    assert res.status_code == 200
    assert send_mock.await_count == 1
    request_obj = send_mock.await_args.args[0]
    assert getattr(request_obj, "tool_choice") == {"type": "function", "function": {"name": "Task"}}


def test_anthropic_messages_enforce_auto_when_task_is_in_multi_toolset():
    client = _build_app()
    upstream_response = JSONResponse(
        content={
            "id": "resp_3b",
            "model": "claude-4.5-sonnet-20250929",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        },
        status_code=200,
    )
    body = _anthropic_body(stream=False)
    body["tools"] = [
        {
            "name": "Task",
            "description": "Task tool",
            "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}}},
        },
        {
            "name": "lookup",
            "description": "lookup tool",
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    ]

    send_mock = AsyncMock(return_value=upstream_response)
    with patch("src.api.openai_router.get_performance_tracker", new=AsyncMock(return_value=_Tracker())):
        with patch("src.api.openai_router.send_assembly_request", new=send_mock):
            res = client.post(
                "/v1/messages",
                json=body,
                headers={
                    "x-api-key": "pwd",
                    "user-agent": "claude-cli/2.1.47 (external, cli)",
                },
            )

    assert res.status_code == 200
    assert send_mock.await_count == 1
    request_obj = send_mock.await_args.args[0]
    assert getattr(request_obj, "tool_choice") == "auto"


def test_anthropic_messages_stream_forces_fake_stream_for_claude_cli_tools():
    client = _build_app()

    async def _stream_gen():
        chunk = {
            "id": "chatcmpl_stream2",
            "model": "claude-4.5-sonnet-20250929",
            "choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(chunk)}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"

    fake_openai_stream = StreamingResponse(_stream_gen(), media_type="text/event-stream")
    body = _anthropic_body(stream=True)
    body["tools"] = [
        {
            "name": "lookup",
            "description": "lookup tool",
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
        }
    ]

    fake_stream_mock = AsyncMock(return_value=fake_openai_stream)
    convert_stream_mock = AsyncMock()

    with patch("src.api.openai_router.get_performance_tracker", new=AsyncMock(return_value=_Tracker())):
        with patch("config.get_enable_real_streaming", new=AsyncMock(return_value=True)):
            with patch("src.api.openai_router.fake_stream_response_for_assembly", new=fake_stream_mock):
                with patch("src.api.openai_router.convert_streaming_response", new=convert_stream_mock):
                    res = client.post(
                        "/v1/messages",
                        json=body,
                        headers={
                            "x-api-key": "pwd",
                            "user-agent": "claude-cli/2.1.45 (external, cli)",
                        },
                    )

    assert res.status_code == 200
    assert fake_stream_mock.await_count == 1
    assert convert_stream_mock.await_count == 0


def test_anthropic_messages_raise_max_tokens_floor_for_claude_cli_tools():
    client = _build_app()
    upstream_response = JSONResponse(
        content={
            "id": "resp_4",
            "model": "claude-4.5-sonnet-20250929",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        },
        status_code=200,
    )
    body = _anthropic_body(stream=False)
    body["max_tokens"] = 128
    body["tools"] = [
        {
            "name": "Task",
            "description": "Task tool",
            "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}}},
        }
    ]

    send_mock = AsyncMock(return_value=upstream_response)
    with patch("src.api.openai_router.get_performance_tracker", new=AsyncMock(return_value=_Tracker())):
        with patch("src.api.openai_router.send_assembly_request", new=send_mock):
            res = client.post(
                "/v1/messages",
                json=body,
                headers={
                    "x-api-key": "pwd",
                    "user-agent": "claude-cli/2.1.45 (external, cli)",
                },
            )

    assert res.status_code == 200
    assert send_mock.await_count == 1
    request_obj = send_mock.await_args.args[0]
    assert getattr(request_obj, "max_tokens") == 4096


def test_anthropic_messages_cap_max_tokens_for_claude_cli_tools():
    client = _build_app()
    upstream_response = JSONResponse(
        content={
            "id": "resp_6",
            "model": "claude-4.5-sonnet-20250929",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        },
        status_code=200,
    )
    body = _anthropic_body(stream=False)
    body["max_tokens"] = 32000
    body["tools"] = [
        {
            "name": "Task",
            "description": "Task tool",
            "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}}},
        }
    ]

    send_mock = AsyncMock(return_value=upstream_response)
    with patch("src.api.openai_router.get_performance_tracker", new=AsyncMock(return_value=_Tracker())):
        with patch("src.api.openai_router.send_assembly_request", new=send_mock):
            res = client.post(
                "/v1/messages",
                json=body,
                headers={
                    "x-api-key": "pwd",
                    "user-agent": "claude-cli/2.1.47 (external, cli)",
                },
            )

    assert res.status_code == 200
    assert send_mock.await_count == 1
    request_obj = send_mock.await_args.args[0]
    assert getattr(request_obj, "max_tokens") == 8192


def test_trim_claude_cli_tool_messages_keeps_system_and_recent_window():
    messages = [{"role": "system", "content": "sys"}]
    for i in range(40):
        role = "user" if i % 2 == 0 else "assistant"
        messages.append({"role": role, "content": f"m{i}"})

    trimmed, dropped = openai_router._trim_claude_cli_tool_messages(messages, max_recent_messages=24)

    assert dropped > 0
    assert isinstance(trimmed, list)
    assert len(trimmed) <= 25  # system + recent window
    assert trimmed[0]["role"] == "system"
    if len(trimmed) > 1:
        assert trimmed[1]["role"] == "user"


def test_anthropic_messages_trim_long_history_for_claude_cli_tools():
    client = _build_app()
    upstream_response = JSONResponse(
        content={
            "id": "resp_5",
            "model": "claude-4.5-sonnet-20250929",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        },
        status_code=200,
    )
    body = _anthropic_body(stream=False)
    convo = []
    for i in range(40):
        role = "user" if i % 2 == 0 else "assistant"
        convo.append({"role": role, "content": f"turn-{i}"})
    body["messages"] = convo
    body["tools"] = [
        {
            "name": "Task",
            "description": "Task tool",
            "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}}},
        }
    ]

    send_mock = AsyncMock(return_value=upstream_response)
    with patch("src.api.openai_router.get_performance_tracker", new=AsyncMock(return_value=_Tracker())):
        with patch("src.api.openai_router.send_assembly_request", new=send_mock):
            res = client.post(
                "/v1/messages",
                json=body,
                headers={
                    "x-api-key": "pwd",
                    "user-agent": "claude-cli/2.1.47 (external, cli)",
                },
            )

    assert res.status_code == 200
    assert send_mock.await_count == 1
    request_obj = send_mock.await_args.args[0]
    assert len(getattr(request_obj, "messages")) <= 24

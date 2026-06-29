"""
Real streaming behavior tests
测试真实流式的 keepalive 与首包前 bootstrap 重试行为
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from src.api import openai_router
from src.services.assembly_stream_handler import convert_streaming_response


class FakeStreamResponse:
    """最小化流式响应对象，仅提供 body_iterator"""

    def __init__(self, iterator):
        self.body_iterator = iterator


def _valid_gemini_chunk_bytes(text: str = "hello") -> bytes:
    payload = (
        '{"candidates":[{"index":0,"content":{"role":"model","parts":[{"text":"'
        + text
        + '"}]}}]}'
    )
    return f"data: {payload}\n\n".encode("utf-8")


class TraceStub:
    def __init__(self):
        self.metadata = {}
        self.marked = []

    def mark(self, stage: str):
        self.marked.append(stage)


class RouterTraceStub:
    def __init__(self, trace_id):
        self.trace_id = trace_id
        self.model = "pending"
        self.metadata = {}

    def mark(self, _):
        return None


class RouterTrackerStub:
    def start_trace(self, trace_id, _):
        return RouterTraceStub(trace_id)

    async def end_trace(self, *args, **kwargs):
        return None


async def _auth_override():
    return "ok"


def _build_openai_test_app():
    app = FastAPI()
    app.include_router(openai_router.router)
    app.dependency_overrides[openai_router.authenticate] = _auth_override
    return app


def _done_stream():
    async def iterator():
        yield b"data: [DONE]\n\n"

    return StreamingResponse(iterator(), media_type="text/event-stream")


@pytest.mark.asyncio
async def test_stream_keepalive_comment_emitted_before_first_chunk():
    async def delayed_stream():
        await asyncio.sleep(0.02)
        yield _valid_gemini_chunk_bytes("hi")
        yield b"data: [DONE]\n\n"

    with patch("src.services.assembly_stream_handler.get_stream_keepalive_seconds", new_callable=AsyncMock) as keepalive_mock, \
         patch("src.services.assembly_stream_handler.get_stream_bootstrap_retries", new_callable=AsyncMock) as bootstrap_mock:
        keepalive_mock.return_value = 0.005
        bootstrap_mock.return_value = 0
        trace = TraceStub()

        response = await convert_streaming_response(
            FakeStreamResponse(delayed_stream()),
            model="gemini-2.5-pro",
            trace=trace,
        )

        chunks = []
        async for item in response.body_iterator:
            chunks.append(item.decode("utf-8", errors="replace"))

        assert any(chunk.startswith(": keepalive") for chunk in chunks)
        assert any('"chat.completion.chunk"' in chunk for chunk in chunks)
        assert chunks[-1].strip() == "data: [DONE]"
        assert trace.metadata["stream_mode"] == "real"
        assert trace.metadata["stream_keepalive_count"] >= 1
        assert trace.metadata["stream_bootstrap_attempts"] == 1
        assert trace.metadata["stream_bootstrap_retries_used"] == 0
        assert trace.metadata["stream_bootstrap_failed"] is False


@pytest.mark.asyncio
async def test_bootstrap_retry_only_before_first_chunk():
    async def failing_stream():
        raise RuntimeError("bootstrap failed")
        yield b""  # pragma: no cover

    async def success_stream():
        yield _valid_gemini_chunk_bytes("retry-ok")
        yield b"data: [DONE]\n\n"

    retry_counter = {"count": 0}

    async def request_provider():
        retry_counter["count"] += 1
        return FakeStreamResponse(success_stream())

    with patch("src.services.assembly_stream_handler.get_stream_keepalive_seconds", new_callable=AsyncMock) as keepalive_mock, \
         patch("src.services.assembly_stream_handler.get_stream_bootstrap_retries", new_callable=AsyncMock) as bootstrap_mock:
        keepalive_mock.return_value = 0
        bootstrap_mock.return_value = 1
        trace = TraceStub()

        response = await convert_streaming_response(
            FakeStreamResponse(failing_stream()),
            model="gemini-2.5-pro",
            trace=trace,
            request_provider=request_provider,
        )

        chunks = []
        async for item in response.body_iterator:
            chunks.append(item.decode("utf-8", errors="replace"))

        assert retry_counter["count"] == 1
        assert any("retry-ok" in chunk for chunk in chunks)
        assert chunks[-1].strip() == "data: [DONE]"
        assert trace.metadata["stream_bootstrap_attempts"] == 2
        assert trace.metadata["stream_bootstrap_retries_used"] == 1
        assert trace.metadata["stream_bootstrap_recovered"] is True
        assert trace.metadata["stream_bootstrap_failed"] is False
        assert "bootstrap failed" in trace.metadata["stream_bootstrap_last_error"]


@pytest.mark.asyncio
async def test_real_streaming_passes_openai_chunks_without_gemini_conversion():
    async def openai_stream():
        yield (
            b'data: {"id":"upstream-1","object":"chat.completion.chunk","created":123,'
            b'"model":"gpt-5.5","choices":[{"index":0,"delta":{"content":"pong"},'
            b'"finish_reason":null}]}\n\n'
        )
        yield (
            b'data: {"id":"upstream-1","object":"chat.completion.chunk","created":123,'
            b'"model":"gpt-5.5","choices":[{"index":0,"delta":{},'
            b'"finish_reason":"stop"}]}\n\n'
        )
        yield b"data: [DONE]\n\n"

    with patch("src.services.assembly_stream_handler.get_stream_keepalive_seconds", new_callable=AsyncMock) as keepalive_mock, \
         patch("src.services.assembly_stream_handler.get_stream_bootstrap_retries", new_callable=AsyncMock) as bootstrap_mock:
        keepalive_mock.return_value = 0
        bootstrap_mock.return_value = 0

        response = await convert_streaming_response(
            FakeStreamResponse(openai_stream()),
            model="gpt-5.5",
        )

        chunks = []
        async for item in response.body_iterator:
            chunks.append(item.decode("utf-8", errors="replace"))

    assert any('"content":"pong"' in chunk for chunk in chunks)
    assert any('"finish_reason":"stop"' in chunk for chunk in chunks)
    assert not any('"choices":[]' in chunk for chunk in chunks)
    assert chunks[-1].strip() == "data: [DONE]"


@pytest.mark.asyncio
async def test_real_streaming_normalizes_null_openai_chunk_fields():
    async def openai_stream():
        yield (
            b'data: {"id":null,"object":null,"created":null,"model":null,'
            b'"choices":[{"index":0,"delta":{"content":"pong"},"finish_reason":null}]}\n\n'
        )
        yield b"data: [DONE]\n\n"

    with patch("src.services.assembly_stream_handler.get_stream_keepalive_seconds", new_callable=AsyncMock) as keepalive_mock, \
         patch("src.services.assembly_stream_handler.get_stream_bootstrap_retries", new_callable=AsyncMock) as bootstrap_mock:
        keepalive_mock.return_value = 0
        bootstrap_mock.return_value = 0

        response = await convert_streaming_response(
            FakeStreamResponse(openai_stream()),
            model="gpt-5.5",
        )

        chunks = []
        async for item in response.body_iterator:
            chunks.append(item.decode("utf-8", errors="replace"))

    assert any('"id":"' in chunk and '"id":null' not in chunk for chunk in chunks)
    assert any('"object":"chat.completion.chunk"' in chunk for chunk in chunks)
    assert any('"created":' in chunk and '"created":null' not in chunk for chunk in chunks)
    assert any('"model":"gpt-5.5"' in chunk for chunk in chunks)
    assert chunks[-1].strip() == "data: [DONE]"


@pytest.mark.asyncio
async def test_real_streaming_preserves_header_bootstrap_retry_count():
    async def openai_stream():
        yield (
            b'data: {"id":"upstream-1","object":"chat.completion.chunk","created":123,'
            b'"model":"gpt-5.5","choices":[{"index":0,"delta":{"content":"pong"},'
            b'"finish_reason":null}]}\n\n'
        )
        yield b"data: [DONE]\n\n"

    with patch("src.services.assembly_stream_handler.get_stream_keepalive_seconds", new_callable=AsyncMock) as keepalive_mock, \
         patch("src.services.assembly_stream_handler.get_stream_bootstrap_retries", new_callable=AsyncMock) as bootstrap_mock:
        keepalive_mock.return_value = 0
        bootstrap_mock.return_value = 0
        trace = TraceStub()
        trace.metadata["stream_bootstrap_retries_used"] = 1

        response = await convert_streaming_response(
            FakeStreamResponse(openai_stream()),
            model="gpt-5.5",
            trace=trace,
            bootstrap_retries_override=0,
        )

        chunks = []
        async for item in response.body_iterator:
            chunks.append(item.decode("utf-8", errors="replace"))

    assert any('"content":"pong"' in chunk for chunk in chunks)
    assert trace.metadata["stream_bootstrap_retries_used"] == 1
    assert trace.metadata["stream_bootstrap_recovered"] is True


def test_openai_streaming_upstream_json_error_preserves_status():
    app = _build_openai_test_app()

    gateway_error = JSONResponse(
        content={"error": {"message": "Upstream HTTP 500", "type": "api_error"}},
        status_code=500,
    )

    with patch("src.api.openai_router.get_performance_tracker", new=AsyncMock(return_value=RouterTrackerStub())), \
         patch("config.get_fake_streaming_enabled", new=AsyncMock(return_value=False)), \
         patch("config.get_enable_real_streaming", new=AsyncMock(return_value=True)), \
         patch("config.get_stream_bootstrap_retries", new=AsyncMock(return_value=0)), \
         patch("src.api.openai_router.send_assembly_request", new=AsyncMock(return_value=gateway_error)):
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.5",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
            headers={"Authorization": "Bearer test"},
        )

    assert response.status_code == 500
    assert response.json()["error"]["type"] == "api_error"


def test_native_streaming_retries_header_stage_5xx_before_forwarding():
    app = _build_openai_test_app()
    gateway_error = JSONResponse(
        content={"error": {"message": "temporary upstream error", "type": "api_error"}},
        status_code=503,
    )
    upstream_response = FakeStreamResponse(iter(()))
    converted_stream = AsyncMock(return_value=_done_stream())

    with patch("src.api.openai_router.get_performance_tracker", new=AsyncMock(return_value=RouterTrackerStub())), \
         patch("config.get_fake_streaming_enabled", new=AsyncMock(return_value=False)), \
         patch("config.get_enable_real_streaming", new=AsyncMock(return_value=True)), \
         patch("config.get_stream_bootstrap_retries", new=AsyncMock(return_value=1)), \
         patch("src.api.openai_router.send_assembly_request", new=AsyncMock(side_effect=[gateway_error, upstream_response])) as send_request, \
         patch("src.api.openai_router.convert_streaming_response", new=converted_stream):
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.5",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
            headers={"Authorization": "Bearer test"},
        )

    assert response.status_code == 200
    assert send_request.await_count == 2
    converted_stream.assert_awaited_once()
    assert converted_stream.await_args.kwargs["bootstrap_retries_override"] == 0


def test_native_streaming_does_not_retry_header_stage_4xx():
    app = _build_openai_test_app()
    gateway_error = JSONResponse(
        content={"error": {"message": "bad request", "type": "api_error"}},
        status_code=400,
    )

    with patch("src.api.openai_router.get_performance_tracker", new=AsyncMock(return_value=RouterTrackerStub())), \
         patch("config.get_fake_streaming_enabled", new=AsyncMock(return_value=False)), \
         patch("config.get_enable_real_streaming", new=AsyncMock(return_value=True)), \
         patch("config.get_stream_bootstrap_retries", new=AsyncMock(return_value=3)), \
         patch("src.api.openai_router.send_assembly_request", new=AsyncMock(return_value=gateway_error)) as send_request:
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.5",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
            headers={"Authorization": "Bearer test"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "bad request"
    assert send_request.await_count == 1


def test_global_fake_streaming_forces_non_stream_upstream_for_native_model():
    app = _build_openai_test_app()
    fake_stream = AsyncMock(return_value=_done_stream())
    send_request = AsyncMock(side_effect=AssertionError("global fake streaming must not open native upstream stream"))

    with patch("src.api.openai_router.get_performance_tracker", new=AsyncMock(return_value=RouterTrackerStub())), \
         patch("config.get_fake_streaming_enabled", new=AsyncMock(return_value=True), create=True), \
         patch("config.get_enable_real_streaming", new=AsyncMock(return_value=True)), \
         patch("src.api.openai_router.fake_stream_response_for_assembly", new=fake_stream), \
         patch("src.api.openai_router.send_assembly_request", new=send_request):
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.5",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
            headers={"Authorization": "Bearer test"},
        )

    assert response.status_code == 200
    fake_stream.assert_awaited_once()
    routed_request = fake_stream.await_args.args[0]
    assert routed_request.model == "gpt-5.5"
    assert routed_request.stream is False
    send_request.assert_not_awaited()


def test_unsupported_streaming_model_uses_fake_stream_when_global_fake_disabled():
    app = _build_openai_test_app()
    fake_stream = AsyncMock(return_value=_done_stream())
    send_request = AsyncMock(side_effect=AssertionError("unsupported native stream model must not send stream=true upstream"))

    with patch("src.api.openai_router.get_performance_tracker", new=AsyncMock(return_value=RouterTrackerStub())), \
         patch("config.get_fake_streaming_enabled", new=AsyncMock(return_value=False), create=True), \
         patch("config.get_enable_real_streaming", new=AsyncMock(return_value=True)), \
         patch("src.api.openai_router.fake_stream_response_for_assembly", new=fake_stream), \
         patch("src.api.openai_router.send_assembly_request", new=send_request):
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-3.1-flash-lite",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
            headers={"Authorization": "Bearer test"},
        )

    assert response.status_code == 200
    fake_stream.assert_awaited_once()
    routed_request = fake_stream.await_args.args[0]
    assert routed_request.model == "gemini-3.1-flash-lite"
    assert routed_request.stream is False
    send_request.assert_not_awaited()


def test_supported_streaming_model_uses_native_stream_when_global_fake_disabled():
    app = _build_openai_test_app()
    upstream_response = FakeStreamResponse(iter(()))
    fake_stream = AsyncMock(side_effect=AssertionError("supported native stream model should not use fake stream"))
    converted_stream = AsyncMock(return_value=_done_stream())

    with patch("src.api.openai_router.get_performance_tracker", new=AsyncMock(return_value=RouterTrackerStub())), \
         patch("config.get_fake_streaming_enabled", new=AsyncMock(return_value=False), create=True), \
         patch("config.get_enable_real_streaming", new=AsyncMock(return_value=True)), \
         patch("src.api.openai_router.send_assembly_request", new=AsyncMock(return_value=upstream_response)) as send_request, \
         patch("src.api.openai_router.convert_streaming_response", new=converted_stream), \
         patch("src.api.openai_router.fake_stream_response_for_assembly", new=fake_stream):
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.5",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
            headers={"Authorization": "Bearer test"},
        )

    assert response.status_code == 200
    send_request.assert_awaited_once()
    assert send_request.await_args.args[1] is True
    converted_stream.assert_awaited_once()
    fake_stream.assert_not_awaited()


def test_legacy_real_streaming_kill_switch_forces_fake_stream_for_native_model():
    app = _build_openai_test_app()
    fake_stream = AsyncMock(return_value=_done_stream())
    send_request = AsyncMock(side_effect=AssertionError("legacy kill switch must not open native upstream stream"))

    with patch("src.api.openai_router.get_performance_tracker", new=AsyncMock(return_value=RouterTrackerStub())), \
         patch("config.get_fake_streaming_enabled", new=AsyncMock(return_value=False), create=True), \
         patch("config.get_enable_real_streaming", new=AsyncMock(return_value=False)), \
         patch("src.api.openai_router.fake_stream_response_for_assembly", new=fake_stream), \
         patch("src.api.openai_router.send_assembly_request", new=send_request):
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.5",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
            headers={"Authorization": "Bearer test"},
        )

    assert response.status_code == 200
    fake_stream.assert_awaited_once()
    routed_request = fake_stream.await_args.args[0]
    assert routed_request.model == "gpt-5.5"
    assert routed_request.stream is False
    send_request.assert_not_awaited()

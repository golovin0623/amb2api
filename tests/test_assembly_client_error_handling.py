import json
from unittest.mock import AsyncMock

import pytest
from fastapi.responses import JSONResponse

from src.models.models import ChatCompletionRequest
from src.services.assembly_client import send_assembly_request


class _FakeUnifiedStats:
    async def record_call(self, *args, **kwargs):
        return None

    def release_reservation(self, *args, **kwargs):
        return None


class _FakeStreamResponse:
    def __init__(self, status_code=500, body=b'{"error":"upstream failed"}'):
        self.status_code = status_code
        self.headers = {}
        self._body = body

    async def aread(self):
        return self._body


class _FakeStreamContext:
    def __init__(self, response=None, exc=None):
        self._response = response or _FakeStreamResponse()
        self._exc = exc
        self.closed = False

    async def __aenter__(self):
        if self._exc:
            raise self._exc
        return self._response

    async def __aexit__(self, *exc):
        self.closed = True
        return False


class _FakeStreamingClient:
    def __init__(self, stream_ctx):
        self._stream_ctx = stream_ctx

    def stream(self, *args, **kwargs):
        return self._stream_ctx


class _Trace:
    def __init__(self):
        self.metadata = {}
        self.key_index = -1
        self.key_masked = ""


def _patch_send_assembly_for_streaming(monkeypatch, stream_ctx):
    monkeypatch.setattr(
        "src.services.assembly_client.get_model_region",
        AsyncMock(return_value=""),
    )
    monkeypatch.setattr(
        "src.services.assembly_client.get_assembly_endpoint",
        AsyncMock(return_value="https://example.test/v1/chat/completions"),
    )
    monkeypatch.setattr(
        "src.services.assembly_client.get_assembly_api_keys",
        AsyncMock(return_value=["key-1"]),
    )
    monkeypatch.setattr(
        "src.services.assembly_client.get_tool_debug_logs_enabled",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "src.services.assembly_client.get_retry_429_max_retries",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        "src.services.assembly_client.get_retry_429_enabled",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "src.services.assembly_client.get_retry_429_interval",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        "src.services.assembly_client.get_prompt_cache_enabled",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "src.services.assembly_client.get_auto_ban_enabled",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "src.services.assembly_client._select_key_with_daily_quota",
        AsyncMock(return_value={"idx": 0, "api_key": "key-1", "reason": "", "blocked": []}),
    )
    monkeypatch.setattr(
        "src.services.assembly_client._update_rate_limit_info",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "src.services.assembly_client.create_streaming_client_with_kwargs",
        AsyncMock(return_value=_FakeStreamingClient(stream_ctx)),
    )
    monkeypatch.setattr(
        "src.stats.unified_stats.get_unified_stats",
        AsyncMock(return_value=_FakeUnifiedStats()),
    )


@pytest.mark.asyncio
async def test_streaming_upstream_error_returns_json_response(monkeypatch):
    stream_ctx = _FakeStreamContext(
        response=_FakeStreamResponse(status_code=500, body=b'{"message":"upstream failed"}')
    )
    _patch_send_assembly_for_streaming(monkeypatch, stream_ctx)

    req = ChatCompletionRequest(
        model="gemini-3.5-flash",
        messages=[{"role": "user", "content": "hi"}],
    )
    response = await send_assembly_request(req, is_streaming=True)

    assert isinstance(response, JSONResponse)
    assert response.status_code == 500
    body = json.loads(response.body)
    assert body["error"]["type"] == "api_error"
    assert "upstream failed" in body["error"]["message"]
    assert stream_ctx.closed is True


@pytest.mark.asyncio
async def test_streaming_success_records_usage_timestamp_on_trace(monkeypatch):
    stream_ctx = _FakeStreamContext(response=_FakeStreamResponse(status_code=200))
    _patch_send_assembly_for_streaming(monkeypatch, stream_ctx)

    req = ChatCompletionRequest(
        model="gemini-3.5-flash",
        messages=[{"role": "user", "content": "hi"}],
    )
    trace = _Trace()
    response = await send_assembly_request(req, is_streaming=True, trace=trace)

    assert response.status_code == 200
    assert isinstance(trace.metadata["usage_recorded_at"], float)


@pytest.mark.asyncio
async def test_request_exception_returns_json_response_without_logger_exc_info(monkeypatch):
    _patch_send_assembly_for_streaming(
        monkeypatch,
        _FakeStreamContext(exc=RuntimeError("network down")),
    )

    req = ChatCompletionRequest(
        model="gemini-3.5-flash",
        messages=[{"role": "user", "content": "hi"}],
    )
    response = await send_assembly_request(req, is_streaming=True)

    assert isinstance(response, JSONResponse)
    assert response.status_code == 500
    body = json.loads(response.body)
    assert body["error"]["type"] == "api_error"
    assert "Request failed (RuntimeError): network down" == body["error"]["message"]

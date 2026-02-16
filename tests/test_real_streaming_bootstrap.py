"""
Real streaming behavior tests
测试真实流式的 keepalive 与首包前 bootstrap 重试行为
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch

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

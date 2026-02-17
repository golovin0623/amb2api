"""
Performance tracker stream metrics tests
测试性能追踪中的流式聚合指标
"""
from unittest.mock import AsyncMock, patch

import pytest

from src.stats.performance_tracker import PerformanceTracker


class FakeAdapter:
    def __init__(self, perf_data):
        self.perf_data = perf_data

    async def get_perf(self, key, default=None):
        return self.perf_data.get(key, default)


def _trace(
    trace_id: str,
    model: str,
    start_time: float,
    stream_meta: dict,
    key_masked: str = "sk-ab...xy",
) -> dict:
    return {
        "trace_id": trace_id,
        "model": model,
        "start_time": start_time,
        "timestamps": {
            "request_received": 0.0,
            "upstream_first_byte": 120.0,
            "first_chunk_sent": 150.0,
            "response_complete": 900.0,
        },
        "metadata": stream_meta,
        "completion_tokens": 90,
        "prompt_tokens": 30,
        "cached_tokens": 8,
        "total_tokens": 120,
        "key_index": 0,
        "key_masked": key_masked,
        "account_email": "test@example.com",
    }


@pytest.mark.asyncio
async def test_get_stats_includes_stream_aggregation_fields():
    traces = [
        _trace(
            "t1",
            "model-a",
            1000.0,
            {
                "stream_mode": "real",
                "stream_keepalive_count": 2,
                "stream_bootstrap_retries_used": 1,
                "stream_bootstrap_failed": False,
            },
        ),
        _trace(
            "t2",
            "model-a",
            1001.0,
            {
                "stream_mode": "real",
                "stream_keepalive_count": 1,
                "stream_bootstrap_retries_used": 0,
                "stream_bootstrap_failed": False,
            },
        ),
        _trace("t3", "model-b", 1002.0, {"stream_mode": "fake"}),
        _trace("t4", "model-a", 1003.0, {}),
    ]
    adapter = FakeAdapter({"perf_traces_0": traces})
    tracker = PerformanceTracker()

    with patch("src.storage.storage_adapter.get_storage_adapter", new=AsyncMock(return_value=adapter)):
        stats = await tracker.get_stats(use_cache=False)

    stream = stats["stream"]
    assert stream["total"] == 4
    assert stream["real"] == 2
    assert stream["fake"] == 1
    assert stream["non_stream"] == 1
    assert stream["keepalive_total"] == 3
    assert stream["keepalive_avg_per_real"] == 1.5
    assert stream["bootstrap_retry_total"] == 1
    assert stream["bootstrap_retry_requests"] == 1
    assert stream["bootstrap_retry_rate"] == 0.5
    assert stream["bootstrap_failure_count"] == 0
    assert stream["bootstrap_failure_rate"] == 0
    tokens = stats["tokens"]
    assert tokens["prompt_total"] == 120
    assert tokens["completion_total"] == 360
    assert tokens["cached_total"] == 32
    assert tokens["total"] == 480
    assert tokens["requests_with_usage"] == 4


@pytest.mark.asyncio
async def test_get_stats_stream_metrics_respect_model_filter_and_type_coercion():
    traces = [
        _trace(
            "t1",
            "model-a",
            1000.0,
            {
                "stream_mode": "real",
                "stream_keepalive_count": "4",
                "stream_bootstrap_retries_used": "2",
                "stream_bootstrap_failed": "true",
            },
        ),
        _trace("t2", "model-b", 1001.0, {"stream_mode": "fake"}),
    ]
    adapter = FakeAdapter({"perf_traces_0": traces})
    tracker = PerformanceTracker()

    with patch("src.storage.storage_adapter.get_storage_adapter", new=AsyncMock(return_value=adapter)):
        stats = await tracker.get_stats(model="model-a", use_cache=False)

    stream = stats["stream"]
    assert stats["count"] == 1
    assert stream["total"] == 1
    assert stream["real"] == 1
    assert stream["fake"] == 0
    assert stream["non_stream"] == 0
    assert stream["keepalive_total"] == 4
    assert stream["bootstrap_retry_total"] == 2
    assert stream["bootstrap_retry_requests"] == 1
    assert stream["bootstrap_retry_rate"] == 1.0
    assert stream["bootstrap_failure_count"] == 1
    assert stream["bootstrap_failure_rate"] == 1.0
    tokens = stats["tokens"]
    assert tokens["prompt_total"] == 30
    assert tokens["completion_total"] == 90
    assert tokens["cached_total"] == 8
    assert tokens["total"] == 120
    assert tokens["requests_with_usage"] == 1


@pytest.mark.asyncio
async def test_get_traces_paginated_supports_key_filter_and_usage_fields():
    traces = [
        _trace("t1", "model-a", 1000.0, {"stream_mode": "real"}, key_masked="sk-aa...11"),
        _trace("t2", "model-b", 1001.0, {"stream_mode": "fake"}, key_masked="sk-bb...22"),
    ]
    # 模拟旧数据缺失 total_tokens，验证 fallback 逻辑
    traces[0].pop("total_tokens", None)
    adapter = FakeAdapter({"perf_traces_0": traces})
    tracker = PerformanceTracker()

    with patch("src.storage.storage_adapter.get_storage_adapter", new=AsyncMock(return_value=adapter)):
        result = await tracker.get_traces_paginated(page=1, page_size=20, key="aa...11")

    assert result["total"] == 1
    assert len(result["traces"]) == 1
    trace = result["traces"][0]
    assert trace["key_masked"] == "sk-aa...11"
    assert trace["prompt_tokens"] == 30
    assert trace["completion_tokens"] == 90
    assert trace["cached_tokens"] == 8
    assert trace["total_tokens"] == 120
    assert trace["usage"]["prompt_tokens"] == 30
    assert trace["usage"]["completion_tokens"] == 90
    assert trace["usage"]["cached_tokens"] == 8
    assert trace["usage"]["total_tokens"] == 120

"""
Tests for performance tracker prompt-caching cache_creation passthrough.

Covers:
- ``_extract_usage_tokens`` reads ``cache_creation`` either flat or nested under
  ``prompt_tokens_details.cache_creation``.
- ``RequestTrace`` round-trip preserves the new fields.
- ``PerformanceTracker.get_stats`` aggregates them into stat totals/averages.
- ``PerformanceTracker.get_traces_paginated`` exposes the new fields.
"""
from unittest.mock import AsyncMock, patch

import pytest

from src.stats.performance_tracker import (
    PerformanceTracker,
    RequestTrace,
    _extract_usage_tokens,
)


class FakeAdapter:
    def __init__(self, perf_data):
        self.perf_data = perf_data

    async def get_perf(self, key, default=None):
        return self.perf_data.get(key, default)


def _trace_with_cache(trace_id: str, c5m: int, c1h: int, *, nested: bool = False) -> dict:
    raw = {
        "trace_id": trace_id,
        "model": "model-a",
        "start_time": 1000.0,
        "timestamps": {
            "request_received": 0.0,
            "upstream_first_byte": 100.0,
            "first_chunk_sent": 150.0,
            "response_complete": 800.0,
        },
        "metadata": {"stream_mode": "real"},
        "completion_tokens": 30,
        "prompt_tokens": 1024,
        "cached_tokens": 600,
        "total_tokens": 1054,
        "key_index": 0,
        "key_masked": "sk-ab...xy",
        "account_email": "user@example.com",
    }
    if nested:
        raw["prompt_tokens_details"] = {
            "cached_tokens": 600,
            "cache_creation": {
                "ephemeral_5m_input_tokens": c5m,
                "ephemeral_1h_input_tokens": c1h,
            },
        }
    else:
        raw["cache_creation_5m_tokens"] = c5m
        raw["cache_creation_1h_tokens"] = c1h
    return raw


def test_extract_usage_tokens_reads_flat_cache_creation():
    out = _extract_usage_tokens({
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "cached_tokens": 50,
        "cache_creation_5m_tokens": 200,
        "cache_creation_1h_tokens": 30,
    })
    assert out["cache_creation_5m_tokens"] == 200
    assert out["cache_creation_1h_tokens"] == 30


def test_extract_usage_tokens_reads_nested_prompt_tokens_details():
    out = _extract_usage_tokens({
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "prompt_tokens_details": {
            "cached_tokens": 50,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 200,
                "ephemeral_1h_input_tokens": 30,
            },
        },
    })
    assert out["cached_tokens"] == 50
    assert out["cache_creation_5m_tokens"] == 200
    assert out["cache_creation_1h_tokens"] == 30


def test_request_trace_dict_round_trip_preserves_cache_creation():
    raw = _trace_with_cache("t1", 200, 30)
    trace = RequestTrace.from_dict(raw)
    assert trace.cache_creation_5m_tokens == 200
    assert trace.cache_creation_1h_tokens == 30

    # to_dict + from_dict must be stable.
    serialized = trace.to_dict()
    assert serialized["cache_creation_5m_tokens"] == 200
    assert serialized["cache_creation_1h_tokens"] == 30
    revived = RequestTrace.from_dict(serialized)
    assert revived.cache_creation_5m_tokens == 200
    assert revived.cache_creation_1h_tokens == 30


@pytest.mark.asyncio
async def test_get_stats_aggregates_cache_creation_totals():
    traces = [
        _trace_with_cache("t1", 200, 30),
        _trace_with_cache("t2", 100, 0, nested=True),
    ]
    perf_data = {"perf_traces_0": traces}

    tracker = PerformanceTracker()
    tracker._initialized = True

    with patch(
        "src.storage.storage_adapter.get_storage_adapter",
        new=AsyncMock(return_value=FakeAdapter(perf_data)),
    ):
        stats = await tracker.get_stats(use_cache=False)

    tokens = stats["tokens"]
    assert tokens["cache_creation_5m_total"] == 300
    assert tokens["cache_creation_1h_total"] == 30
    assert tokens["avg_cache_creation_5m"] == 150.0
    assert tokens["avg_cache_creation_1h"] == 15.0


@pytest.mark.asyncio
async def test_get_traces_paginated_exposes_cache_creation_fields():
    traces = [_trace_with_cache("t1", 200, 30)]
    perf_data = {"perf_traces_0": traces}

    tracker = PerformanceTracker()
    tracker._initialized = True

    with patch(
        "src.storage.storage_adapter.get_storage_adapter",
        new=AsyncMock(return_value=FakeAdapter(perf_data)),
    ):
        page = await tracker.get_traces_paginated(page=1, page_size=10)

    item = page["traces"][0]
    assert item["cache_creation_5m_tokens"] == 200
    assert item["cache_creation_1h_tokens"] == 30
    assert item["usage"]["cache_creation_5m_tokens"] == 200
    assert item["usage"]["cache_creation_1h_tokens"] == 30

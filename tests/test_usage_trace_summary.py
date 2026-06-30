import json
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.api import admin_routes
from src.stats.unified_stats import mask_key
import src.stats.performance_tracker as performance_tracker_module
import src.stats.unified_stats as unified_stats_module


TRACE_RESET_TIME = "2099-01-01T07:00:00+00:00"
TRACE_IN_WINDOW = datetime(2098, 12, 31, 8, tzinfo=timezone.utc).timestamp()
TRACE_BEFORE_WINDOW = datetime(2098, 12, 31, 6, 59, tzinfo=timezone.utc).timestamp()


class _UsageTraceAdapter:
    def __init__(self, *, keys=None, unified_stats=None, traces=None):
        traces = list(traces or [])
        self.config = {
            "assembly_api_keys": list(keys or []),
            "unified_stats": unified_stats or {},
        }
        self.perf = {
            "perf_traces_0": traces,
            "perf_meta": {
                "current_write_shard": 0,
                "shard_counts": {"0": len(traces)} if traces else {},
            },
        }

    async def get_config(self, key, default=None):
        return self.config.get(key, default)

    async def set_config(self, key, value):
        self.config[key] = value
        return True

    async def get_perf(self, key, default=None):
        return self.perf.get(key, default)

    async def set_perf(self, key, value):
        self.perf[key] = value
        return True

    async def delete_perf(self, key):
        self.perf.pop(key, None)
        return True


def _trace(trace_id, model, key_masked, start_time=None, *, success=True):
    if start_time is None:
        start_time = time.time()
    return {
        "trace_id": trace_id,
        "model": model,
        "start_time": start_time,
        "timestamps": {
            "request_received": 0.0,
            "upstream_first_byte": 100.0,
            "first_chunk_sent": 120.0,
            "response_complete": 300.0,
        },
        "metadata": {"usage_success": success},
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
        "key_masked": key_masked,
        "key_index": 0,
    }


def _reset_stats_singletons():
    unified_stats_module._unified_stats = None
    performance_tracker_module._tracker = None


@pytest.mark.asyncio
async def test_usage_aggregated_backfills_empty_unified_stats_from_request_traces():
    key = "45d00000000000000c0c"
    masked = mask_key(key)
    traces = [
        _trace("t1", "gemini-3.1-flash-lite", masked),
        _trace("t2", "gemini-3.1-flash-lite", masked),
        _trace("t3", "gemini-3.1-flash-lite", masked),
        _trace("t4", "gemini-3.1-flash-lite", masked),
        _trace("t5", "gemini-3.1-flash-lite", masked),
        _trace("t6", "claude-haiku-4-5-20251001", masked),
        _trace("t7", "claude-haiku-4-5-20251001", masked),
    ]
    adapter = _UsageTraceAdapter(keys=[key], traces=traces)

    _reset_stats_singletons()
    try:
        with patch("src.api.admin_routes.get_storage_adapter", new=AsyncMock(return_value=adapter)):
            with patch("src.stats.unified_stats.get_storage_adapter", new=AsyncMock(return_value=adapter)):
                with patch("src.storage.storage_adapter.get_storage_adapter", new=AsyncMock(return_value=adapter)):
                    response = await admin_routes.usage_aggregated(token="x")
    finally:
        _reset_stats_singletons()

    payload = json.loads(response.body.decode("utf-8"))
    summary = payload["log_summary"]

    assert payload["total_all_model_calls"] == 7
    assert summary["total"] == {"ok": 7, "fail": 0}
    assert summary["models"]["gemini-3.1-flash-lite"]["ok"] == 5
    assert summary["models"]["claude-haiku-4-5-20251001"]["ok"] == 2
    assert summary["keys"][masked]["ok"] == 7
    assert summary["keys"][masked]["model_counts"] == {
        "gemini-3.1-flash-lite": 5,
        "claude-haiku-4-5-20251001": 2,
    }


@pytest.mark.asyncio
async def test_usage_aggregated_does_not_double_count_when_unified_stats_already_match_traces():
    key = "45d00000000000000c0c"
    masked = mask_key(key)
    traces = [
        _trace(f"t{i}", "gemini-3.1-flash-lite", masked, TRACE_IN_WINDOW + i)
        for i in range(5)
    ]
    adapter = _UsageTraceAdapter(
        keys=[key],
        unified_stats={
            masked: {
                "full_key_hash": "",
                "success_count": 5,
                "failure_count": 0,
                "model_counts": {"gemini-3.1-flash-lite": {"ok": 5, "fail": 0}},
                "daily_limit_total": 1000,
                "daily_limit_models": {},
                "next_reset_time": TRACE_RESET_TIME,
            }
        },
        traces=traces,
    )

    _reset_stats_singletons()
    try:
        with patch("src.api.admin_routes.get_storage_adapter", new=AsyncMock(return_value=adapter)):
            with patch("src.stats.unified_stats.get_storage_adapter", new=AsyncMock(return_value=adapter)):
                with patch("src.storage.storage_adapter.get_storage_adapter", new=AsyncMock(return_value=adapter)):
                    response = await admin_routes.usage_aggregated(token="x")
    finally:
        _reset_stats_singletons()

    payload = json.loads(response.body.decode("utf-8"))
    summary = payload["log_summary"]

    assert payload["total_all_model_calls"] == 5
    assert summary["total"] == {"ok": 5, "fail": 0}
    assert summary["models"]["gemini-3.1-flash-lite"]["ok"] == 5
    assert summary["keys"][masked]["ok"] == 5


@pytest.mark.asyncio
async def test_usage_aggregated_preserves_failed_trace_status():
    key = "45d00000000000000c0c"
    masked = mask_key(key)
    adapter = _UsageTraceAdapter(
        keys=[key],
        unified_stats={
            masked: {
                "full_key_hash": "",
                "success_count": 0,
                "failure_count": 1,
                "model_counts": {"gemini-3.1-flash-lite": {"ok": 0, "fail": 1}},
                "daily_limit_total": 1000,
                "daily_limit_models": {},
                "next_reset_time": TRACE_RESET_TIME,
            }
        },
        traces=[
            _trace("failed", "gemini-3.1-flash-lite", masked, TRACE_IN_WINDOW, success=False),
        ],
    )

    _reset_stats_singletons()
    try:
        with patch("src.api.admin_routes.get_storage_adapter", new=AsyncMock(return_value=adapter)):
            with patch("src.stats.unified_stats.get_storage_adapter", new=AsyncMock(return_value=adapter)):
                with patch("src.storage.storage_adapter.get_storage_adapter", new=AsyncMock(return_value=adapter)):
                    response = await admin_routes.usage_aggregated(token="x")
    finally:
        _reset_stats_singletons()

    payload = json.loads(response.body.decode("utf-8"))
    summary = payload["log_summary"]

    assert payload["total_all_model_calls"] == 1
    assert summary["total"] == {"ok": 0, "fail": 1}
    assert summary["models"]["gemini-3.1-flash-lite"] == {"ok": 0, "fail": 1}
    assert summary["keys"][masked]["ok"] == 0
    assert summary["keys"][masked]["fail"] == 1


@pytest.mark.asyncio
async def test_usage_aggregated_rebuilds_global_totals_from_merged_keys():
    key_a = "45d00000000000000c0c"
    key_b = "01460000000000004af9"
    masked_a = mask_key(key_a)
    masked_b = mask_key(key_b)
    adapter = _UsageTraceAdapter(
        keys=[key_a, key_b],
        unified_stats={
            masked_a: {
                "full_key_hash": "",
                "success_count": 5,
                "failure_count": 0,
                "model_counts": {"gemini-3.1-flash-lite": {"ok": 5, "fail": 0}},
                "daily_limit_total": 1000,
                "daily_limit_models": {},
                "next_reset_time": TRACE_RESET_TIME,
            },
            masked_b: {
                "full_key_hash": "",
                "success_count": 0,
                "failure_count": 0,
                "model_counts": {},
                "daily_limit_total": 1000,
                "daily_limit_models": {},
                "next_reset_time": TRACE_RESET_TIME,
            }
        },
        traces=[
            _trace("t1", "gemini-3.1-flash-lite", masked_b, TRACE_IN_WINDOW),
            _trace("t2", "gemini-3.1-flash-lite", masked_b, TRACE_IN_WINDOW + 1),
            _trace("t3", "gemini-3.1-flash-lite", masked_b, TRACE_IN_WINDOW + 2),
        ],
    )

    _reset_stats_singletons()
    try:
        with patch("src.api.admin_routes.get_storage_adapter", new=AsyncMock(return_value=adapter)):
            with patch("src.stats.unified_stats.get_storage_adapter", new=AsyncMock(return_value=adapter)):
                with patch("src.storage.storage_adapter.get_storage_adapter", new=AsyncMock(return_value=adapter)):
                    response = await admin_routes.usage_aggregated(token="x")
    finally:
        _reset_stats_singletons()

    payload = json.loads(response.body.decode("utf-8"))
    summary = payload["log_summary"]

    assert payload["total_all_model_calls"] == 8
    assert summary["total"] == {"ok": 8, "fail": 0}
    assert summary["models"]["gemini-3.1-flash-lite"]["ok"] == 8
    assert summary["keys"][masked_a]["ok"] == 5
    assert summary["keys"][masked_b]["ok"] == 3


@pytest.mark.asyncio
async def test_usage_aggregated_ignores_traces_for_unconfigured_keys():
    active_key = "45d00000000000000c0c"
    removed_key = "01460000000000004af9"
    active_masked = mask_key(active_key)
    removed_masked = mask_key(removed_key)
    adapter = _UsageTraceAdapter(
        keys=[active_key],
        traces=[
            _trace("active", "gemini-3.1-flash-lite", active_masked),
            _trace("removed", "claude-haiku-4-5-20251001", removed_masked),
        ],
    )

    _reset_stats_singletons()
    try:
        with patch("src.api.admin_routes.get_storage_adapter", new=AsyncMock(return_value=adapter)):
            with patch("src.stats.unified_stats.get_storage_adapter", new=AsyncMock(return_value=adapter)):
                with patch("src.storage.storage_adapter.get_storage_adapter", new=AsyncMock(return_value=adapter)):
                    response = await admin_routes.usage_aggregated(token="x")
    finally:
        _reset_stats_singletons()

    payload = json.loads(response.body.decode("utf-8"))
    summary = payload["log_summary"]

    assert payload["total_all_model_calls"] == 1
    assert active_masked in summary["keys"]
    assert removed_masked not in summary["keys"]
    assert summary["models"] == {"gemini-3.1-flash-lite": {"ok": 1, "fail": 0}}


@pytest.mark.asyncio
async def test_usage_aggregated_ignores_traces_before_current_reset_window():
    key = "45d00000000000000c0c"
    masked = mask_key(key)
    adapter = _UsageTraceAdapter(
        keys=[key],
        unified_stats={
            masked: {
                "full_key_hash": "",
                "success_count": 0,
                "failure_count": 0,
                "model_counts": {},
                "daily_limit_total": 1000,
                "daily_limit_models": {},
                "next_reset_time": TRACE_RESET_TIME,
            }
        },
        traces=[
            _trace("old", "gemini-3.1-flash-lite", masked, TRACE_BEFORE_WINDOW),
            _trace("current", "gemini-3.1-flash-lite", masked, TRACE_IN_WINDOW),
        ],
    )

    _reset_stats_singletons()
    try:
        with patch("src.api.admin_routes.get_storage_adapter", new=AsyncMock(return_value=adapter)):
            with patch("src.stats.unified_stats.get_storage_adapter", new=AsyncMock(return_value=adapter)):
                with patch("src.storage.storage_adapter.get_storage_adapter", new=AsyncMock(return_value=adapter)):
                    response = await admin_routes.usage_aggregated(token="x")
    finally:
        _reset_stats_singletons()

    payload = json.loads(response.body.decode("utf-8"))
    summary = payload["log_summary"]

    assert payload["total_all_model_calls"] == 1
    assert summary["total"] == {"ok": 1, "fail": 0}
    assert summary["keys"][masked]["model_counts"] == {"gemini-3.1-flash-lite": 1}


@pytest.mark.asyncio
async def test_usage_reset_for_key_removes_matching_request_traces():
    key_a = "45d00000000000000c0c"
    key_b = "01460000000000004af9"
    masked_a = mask_key(key_a)
    masked_b = mask_key(key_b)
    adapter = _UsageTraceAdapter(
        keys=[key_a, key_b],
        traces=[
            _trace("ta", "model-a", masked_a, 1000.0),
            _trace("tb", "model-b", masked_b, 1001.0),
        ],
    )

    _reset_stats_singletons()
    try:
        with patch("src.stats.unified_stats.get_storage_adapter", new=AsyncMock(return_value=adapter)):
            with patch("src.storage.storage_adapter.get_storage_adapter", new=AsyncMock(return_value=adapter)):
                response = await admin_routes.usage_reset({"filename": masked_a}, token="x")
    finally:
        _reset_stats_singletons()

    payload = json.loads(response.body.decode("utf-8"))
    remaining = adapter.perf["perf_traces_0"]

    assert payload["message"] == "使用统计已重置"
    assert [trace["trace_id"] for trace in remaining] == ["tb"]
    assert remaining[0]["key_masked"] == masked_b

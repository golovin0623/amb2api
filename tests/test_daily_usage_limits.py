import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from src.api import admin_routes, openai_router
from src.services import assembly_client
from src.stats.unified_stats import UnifiedStats, mask_key


class _MemoryAdapter:
    def __init__(self):
        self._config = {}

    async def get_config(self, key, default=None):
        return self._config.get(key, default)

    async def set_config(self, key, value):
        self._config[key] = value
        return True


@pytest.mark.asyncio
async def test_daily_limits_precedence_and_success_only_counting():
    adapter = _MemoryAdapter()
    key = "sk-test-limit-001"
    masked = mask_key(key)

    with patch("src.stats.unified_stats.get_storage_adapter", new=AsyncMock(return_value=adapter)):
        stats = UnifiedStats()
        await stats.initialize()
        await stats.ensure_keys_exist([key])

        # 总限制更严格：total=10, model=20，达到 10 后应被总限制拦截
        await stats.update_daily_limits(masked_key=masked, total_limit=10, model_limits={"model-a": 20})
        for _ in range(10):
            await stats.record_call(key, "model-b", success=True)
        blocked_by_total = await stats.can_use_key_for_model(key, "model-a")
        assert blocked_by_total["allowed"] is False
        assert blocked_by_total["reason"] == "total_limit_reached"

        # 模型限制更严格：total=100, model=10，达到 10 后应被模型限制拦截
        await stats.reset_stats(masked_key=masked)
        await stats.update_daily_limits(masked_key=masked, total_limit=100, model_limits={"model-a": 10})
        for _ in range(10):
            await stats.record_call(key, "model-a", success=True)
        blocked_by_model = await stats.can_use_key_for_model(key, "model-a")
        assert blocked_by_model["allowed"] is False
        assert blocked_by_model["reason"] == "model_limit_reached"

        # 失败请求不计入配额
        await stats.reset_stats(masked_key=masked)
        await stats.update_daily_limits(masked_key=masked, total_limit=2, model_limits={"model-a": 2})
        for _ in range(5):
            await stats.record_call(key, "model-a", success=False)
        still_allowed = await stats.can_use_key_for_model(key, "model-a")
        assert still_allowed["allowed"] is True
        assert still_allowed["success_count"] == 0

        stats_for_key = await stats.get_stats_for_key(masked)
        assert isinstance(stats_for_key.get("next_reset_time"), str)
        assert stats_for_key["next_reset_time"]


@pytest.mark.asyncio
async def test_select_key_with_daily_quota_skips_quota_exhausted_key():
    keys = ["sk-a", "sk-b"]

    class _FakeUnified:
        async def reserve_key_for_model(self, api_key, model):
            if api_key == "sk-a":
                return {
                    "allowed": False,
                    "reason": "model_limit_reached",
                    "model": model,
                    "success_count": 10,
                    "total_limit": 100,
                    "model_success_count": 10,
                    "model_limit": 10,
                    "next_reset_time": "2099-01-01T07:00:00+00:00",
                }
            return {"allowed": True}

    with patch("src.stats.unified_stats.get_unified_stats", new=AsyncMock(return_value=_FakeUnified())):
        with patch("src.services.assembly_client._next_key_index_async", new=AsyncMock(side_effect=[0, 1])):
            selected = await assembly_client._select_key_with_daily_quota(keys, "model-a")

    assert selected["idx"] == 1
    assert selected["api_key"] == "sk-b"
    assert selected["reason"] == ""


@pytest.mark.asyncio
async def test_select_key_with_daily_quota_returns_quota_exhausted_when_all_blocked():
    keys = ["sk-a", "sk-b"]

    class _FakeUnified:
        async def reserve_key_for_model(self, api_key, model):
            return {
                "allowed": False,
                "reason": "total_limit_reached",
                "model": model,
                "success_count": 10,
                "total_limit": 10,
                "model_success_count": 5,
                "model_limit": 20,
                "next_reset_time": "2099-01-01T07:00:00+00:00",
            }

    with patch("src.stats.unified_stats.get_unified_stats", new=AsyncMock(return_value=_FakeUnified())):
        with patch("src.services.assembly_client._next_key_index_async", new=AsyncMock(side_effect=[0, 1])):
            selected = await assembly_client._select_key_with_daily_quota(keys, "model-a")

    assert selected["idx"] == -1
    assert selected["reason"] == "quota_exhausted"
    assert len(selected["blocked"]) == 2


@pytest.mark.asyncio
async def test_usage_update_limits_accepts_total_and_model_limits():
    class _FakeUnifiedStats:
        def __init__(self):
            self.received = None

        async def update_daily_limits(self, masked_key, total_limit=None, model_limits=None):
            self.received = {
                "masked_key": masked_key,
                "total_limit": total_limit,
                "model_limits": model_limits,
            }
            return {
                "daily_limit_total": total_limit,
                "daily_limit_models": model_limits or {},
                "next_reset_time": "2099-01-01T07:00:00+00:00",
            }

    fake_stats = _FakeUnifiedStats()
    adapter = _MemoryAdapter()
    adapter._config["available_models_selected"] = ["model-a", "model-b"]
    with patch("src.stats.unified_stats.get_unified_stats", new=AsyncMock(return_value=fake_stats)):
        with patch("src.api.admin_routes.get_storage_adapter", new=AsyncMock(return_value=adapter)):
            response = await admin_routes.usage_update_limits(
                {
                    "filename": "sk-a...0001",
                    "total_limit": 12,
                    "model_limits": {"model-a": 6},
                },
                token="x",
            )

    payload = json.loads(response.body.decode("utf-8"))
    assert payload["daily_limit_total"] == 12
    assert payload["daily_limit_models"] == {"model-a": 6}
    assert fake_stats.received["total_limit"] == 12
    assert fake_stats.received["model_limits"] == {"model-a": 6}


@pytest.mark.asyncio
async def test_usage_update_limits_rejects_model_not_in_system_list():
    class _FakeUnifiedStats:
        async def update_daily_limits(self, masked_key, total_limit=None, model_limits=None):
            return {
                "daily_limit_total": total_limit,
                "daily_limit_models": model_limits or {},
                "next_reset_time": "2099-01-01T07:00:00+00:00",
            }

    fake_stats = _FakeUnifiedStats()
    adapter = _MemoryAdapter()
    adapter._config["available_models_selected"] = ["model-a"]

    with patch("src.stats.unified_stats.get_unified_stats", new=AsyncMock(return_value=fake_stats)):
        with patch("src.api.admin_routes.get_storage_adapter", new=AsyncMock(return_value=adapter)):
            with pytest.raises(HTTPException) as exc_info:
                await admin_routes.usage_update_limits(
                    {
                        "filename": "sk-a...0001",
                        "total_limit": 12,
                        "model_limits": {"model-not-exists": 6},
                    },
                    token="x",
                )

    assert exc_info.value.status_code == 400
    assert "不在系统模型列表中" in str(exc_info.value.detail)


def test_openai_router_passthroughs_error_status_from_gateway():
    class _Trace:
        def __init__(self, trace_id):
            self.trace_id = trace_id
            self.model = "pending"

        def mark(self, _):
            return None

    class _Tracker:
        def start_trace(self, trace_id, _):
            return _Trace(trace_id)

        async def end_trace(self, *args, **kwargs):
            return None

    async def _auth_override():
        return "ok"

    app = FastAPI()
    app.include_router(openai_router.router)
    app.dependency_overrides[openai_router.authenticate] = _auth_override

    gateway_error = JSONResponse(
        content={
            "error": {
                "message": "All enabled API keys reached daily usage limits for this model.",
                "code": "daily_limit_exhausted",
            }
        },
        status_code=429,
    )

    with patch("src.api.openai_router.get_performance_tracker", new=AsyncMock(return_value=_Tracker())):
        with patch("src.api.openai_router.send_assembly_request", new=AsyncMock(return_value=gateway_error)):
            client = TestClient(app)
            res = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": False,
                },
                headers={"Authorization": "Bearer test"},
            )

    assert res.status_code == 429
    body = res.json()
    assert body["error"]["code"] == "daily_limit_exhausted"

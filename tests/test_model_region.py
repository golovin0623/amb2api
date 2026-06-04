"""
Tests for AssemblyAI LLM Gateway ``model_region`` routing support.

Background: AssemblyAI's 2026-07 pricing update adds a top-level
``model_region`` parameter. Sending ``"model_region": "global"`` opts into
global routing to keep current pricing, while in-region processing is subject
to the increased provider costs.

Covers:
- ``model_region`` is on the upstream pass-through whitelist (per-request).
- ``_collect_passthrough_params`` forwards a client-supplied ``model_region``.
- Anthropic→OpenAI conversion carries ``model_region`` through.
- ``get_model_region`` config getter precedence/normalization.
- ``send_assembly_request`` injects the global default when the client did not
  set one, and a client-supplied value always wins over the global default.
- Admin ``/config`` round-trips ``model_region`` (including clearing it).
"""
import json
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from src.api import admin_routes
from src.models.models import ChatCompletionRequest
from src.services.assembly_client import (
    _UPSTREAM_PASSTHROUGH_KEYS,
    _collect_passthrough_params,
    send_assembly_request,
)
from src.transform.claude_to_openai import convert_claude_request_to_openai


# ── Per-request pass-through ──


def test_model_region_is_on_passthrough_whitelist():
    assert "model_region" in _UPSTREAM_PASSTHROUGH_KEYS


def test_collect_passthrough_forwards_client_model_region():
    req = ChatCompletionRequest(
        model="gpt-5.5",
        messages=[{"role": "user", "content": "hi"}],
        model_region="global",
    )
    out = _collect_passthrough_params(req)
    assert out["model_region"] == "global"


def test_collect_passthrough_omits_unset_model_region():
    req = ChatCompletionRequest(
        model="gpt-5.5",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert "model_region" not in _collect_passthrough_params(req)


def test_anthropic_request_passes_model_region_through():
    payload = convert_claude_request_to_openai(
        {
            "model": "claude-4.6-sonnet",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            "max_tokens": 16,
            "model_region": "global",
        }
    )
    assert payload["model_region"] == "global"


def test_anthropic_request_omits_model_region_when_absent():
    payload = convert_claude_request_to_openai(
        {
            "model": "claude-4.6-sonnet",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            "max_tokens": 16,
        }
    )
    assert "model_region" not in payload


# ── Config getter ──


@pytest.mark.asyncio
async def test_get_model_region_defaults_to_empty():
    with patch.dict(os.environ, {}, clear=True):
        with patch("config.get_config_value", new_callable=AsyncMock) as mock_cfg:
            mock_cfg.return_value = ""
            assert await config.get_model_region() == ""


@pytest.mark.asyncio
async def test_get_model_region_strips_and_returns_stored_value():
    with patch.dict(os.environ, {}, clear=True):
        with patch("config.get_config_value", new_callable=AsyncMock) as mock_cfg:
            mock_cfg.return_value = "  global  "
            assert await config.get_model_region() == "global"


@pytest.mark.asyncio
async def test_get_model_region_handles_none():
    with patch.dict(os.environ, {}, clear=True):
        with patch("config.get_config_value", new_callable=AsyncMock) as mock_cfg:
            mock_cfg.return_value = None
            assert await config.get_model_region() == ""


# ── send_assembly_request global-default injection ──


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.headers = {}

    def json(self):
        return {
            "id": "resp_1",
            "model": "gpt-5.5",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


class _FakeClientCtx:
    """Async context manager standing in for http_client.get_client()."""

    def __init__(self, capture):
        self._capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, endpoint, content=None, headers=None):
        self._capture["post_data"] = content
        return _FakeResponse(200)


class _FakeUnifiedStats:
    async def record_call(self, *args, **kwargs):
        return None

    def release_reservation(self, *args, **kwargs):
        return None


def _patch_send_assembly(monkeypatch, capture, *, model_region_default):
    """Patch send_assembly_request's collaborators so only payload assembly +
    model_region injection are exercised. ``capture`` receives the upstream body."""

    async def _async_return(value):
        return value

    monkeypatch.setattr(
        "src.services.assembly_client.get_model_region",
        AsyncMock(return_value=model_region_default),
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

    class _FakeHttpClient:
        def get_client(self, timeout=None):
            return _FakeClientCtx(capture)

    monkeypatch.setattr("src.services.assembly_client.http_client", _FakeHttpClient())
    monkeypatch.setattr(
        "src.stats.unified_stats.get_unified_stats",
        AsyncMock(return_value=_FakeUnifiedStats()),
    )


@pytest.mark.asyncio
async def test_global_default_injected_when_client_omits_model_region(monkeypatch):
    capture = {}
    _patch_send_assembly(monkeypatch, capture, model_region_default="global")

    req = ChatCompletionRequest(model="gpt-5.5", messages=[{"role": "user", "content": "hi"}])
    await send_assembly_request(req, is_streaming=False)

    body = json.loads(capture["post_data"])
    assert body["model_region"] == "global"


@pytest.mark.asyncio
async def test_client_model_region_wins_over_global_default(monkeypatch):
    capture = {}
    _patch_send_assembly(monkeypatch, capture, model_region_default="global")

    req = ChatCompletionRequest(
        model="gpt-5.5",
        messages=[{"role": "user", "content": "hi"}],
        model_region="in-region",
    )
    await send_assembly_request(req, is_streaming=False)

    body = json.loads(capture["post_data"])
    assert body["model_region"] == "in-region"


@pytest.mark.asyncio
async def test_no_model_region_injected_when_default_empty(monkeypatch):
    capture = {}
    _patch_send_assembly(monkeypatch, capture, model_region_default="")

    req = ChatCompletionRequest(model="gpt-5.5", messages=[{"role": "user", "content": "hi"}])
    await send_assembly_request(req, is_streaming=False)

    body = json.loads(capture["post_data"])
    assert "model_region" not in body


# ── Admin config round-trip ──


class _FakeAdapter:
    def __init__(self):
        self.values = {"override_env": False}

    async def get_config(self, key, default=None):
        return self.values.get(key, default)

    async def set_config(self, key, value):
        self.values[key] = value
        return True


@pytest.mark.asyncio
async def test_save_config_persists_model_region(monkeypatch):
    adapter = _FakeAdapter()
    monkeypatch.setattr(admin_routes, "get_storage_adapter", AsyncMock(return_value=adapter))

    await admin_routes.save_config({"model_region": "  global "}, token="test-token")
    assert adapter.values["model_region"] == "global"


@pytest.mark.asyncio
async def test_save_config_allows_clearing_model_region(monkeypatch):
    adapter = _FakeAdapter()
    adapter.values["model_region"] = "global"
    monkeypatch.setattr(admin_routes, "get_storage_adapter", AsyncMock(return_value=adapter))

    await admin_routes.save_config({"model_region": ""}, token="test-token")
    assert adapter.values["model_region"] == ""


@pytest.mark.asyncio
async def test_get_config_returns_model_region(monkeypatch):
    adapter = _FakeAdapter()
    adapter.values["model_region"] = "global"
    monkeypatch.setattr(admin_routes, "get_storage_adapter", AsyncMock(return_value=adapter))
    for name in (
        "get_assembly_api_key",
        "get_assembly_api_keys",
        "get_api_password",
        "get_panel_password",
        "get_server_port",
        "get_server_host",
    ):
        monkeypatch.setattr(admin_routes, name, AsyncMock(return_value=""))

    response = await admin_routes.get_config(token="test-token")
    cfg = json.loads(response.body)["config"]
    assert cfg["model_region"] == "global"

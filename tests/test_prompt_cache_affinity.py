from unittest.mock import AsyncMock, patch

import pytest

from src.services import assembly_client
from src.services.assembly_client import (
    _apply_prompt_cache_defaults,
    _build_prompt_cache_affinity_key,
    _rank_indices_by_affinity,
)


def test_prompt_cache_defaults_adds_claude_system_cache_control_only():
    payload = {
        "model": "claude-sonnet-4-6",
        "messages": [
            {"role": "system", "content": "stable system instructions"},
            {"role": "user", "content": "dynamic question"},
        ],
    }

    out = _apply_prompt_cache_defaults(
        payload,
        model="claude-sonnet-4-6",
        auto_mode="conservative",
        default_ttl="5m",
    )

    assert out["messages"][0]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert "cache_control" not in out["messages"][1]
    assert "cache_control" not in payload["messages"][0], "helper must not mutate caller payload"


def test_prompt_cache_defaults_preserves_explicit_cache_control():
    explicit = {"type": "ephemeral", "ttl": "1h"}
    payload = {
        "model": "claude-sonnet-4-6",
        "messages": [
            {"role": "system", "content": "stable system instructions", "cache_control": explicit},
            {"role": "user", "content": "dynamic question"},
        ],
    }

    out = _apply_prompt_cache_defaults(
        payload,
        model="claude-sonnet-4-6",
        auto_mode="conservative",
        default_ttl="5m",
    )

    assert out["messages"][0]["cache_control"] == explicit


def test_prompt_cache_defaults_generates_safe_openai_cache_key_from_stable_prefix():
    payload = {
        "model": "gpt-4.1",
        "messages": [
            {"role": "system", "content": "stable system instructions that should not leak"},
            {"role": "user", "content": "dynamic question"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
                },
            }
        ],
    }

    out = _apply_prompt_cache_defaults(
        payload,
        model="gpt-4.1",
        auto_mode="conservative",
        default_ttl="5m",
    )

    assert out["prompt_cache_key"].startswith("amb2api:")
    assert "stable system instructions" not in out["prompt_cache_key"]
    assert "cache_control" not in out["messages"][0]


def test_prompt_cache_affinity_key_prefers_explicit_prompt_cache_key():
    payload = {
        "model": "gpt-4.1",
        "prompt_cache_key": "support-agent-v1",
        "messages": [{"role": "system", "content": "stable"}],
    }

    affinity_key = _build_prompt_cache_affinity_key(payload, "gpt-4.1")

    assert affinity_key == "prompt_cache_key:gpt-4.1:support-agent-v1"


def test_rank_indices_by_affinity_is_stable_per_key():
    indices = [0, 1, 2, 3]

    first = _rank_indices_by_affinity(indices, "stage-a")
    second = _rank_indices_by_affinity(indices, "stage-a")

    assert first == second
    assert sorted(first) == indices


@pytest.mark.asyncio
async def test_select_key_with_daily_quota_uses_affinity_order():
    keys = ["sk-a", "sk-b", "sk-c"]

    class _FakeUnified:
        async def can_use_key_for_model(self, api_key, model):
            return {"allowed": True}

    with patch("src.stats.unified_stats.get_unified_stats", new=AsyncMock(return_value=_FakeUnified())):
        with patch(
            "src.services.assembly_client._get_affinity_candidate_indices",
            new=AsyncMock(return_value=[2, 0, 1]),
        ):
            first = await assembly_client._select_key_with_daily_quota(
                keys,
                "gpt-4.1",
                affinity_key="stage-a",
            )
            second = await assembly_client._select_key_with_daily_quota(
                keys,
                "gpt-4.1",
                affinity_key="stage-a",
            )

    assert first["idx"] == 2
    assert first["api_key"] == "sk-c"
    assert second["idx"] == 2
    assert second["api_key"] == "sk-c"


@pytest.mark.asyncio
async def test_select_key_with_daily_quota_affinity_skips_quota_blocked_candidate():
    keys = ["sk-a", "sk-b", "sk-c"]

    class _FakeUnified:
        async def can_use_key_for_model(self, api_key, model):
            if api_key == "sk-c":
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
        with patch(
            "src.services.assembly_client._get_affinity_candidate_indices",
            new=AsyncMock(return_value=[2, 1, 0]),
        ):
            selected = await assembly_client._select_key_with_daily_quota(
                keys,
                "gpt-4.1",
                affinity_key="stage-a",
            )

    assert selected["idx"] == 1
    assert selected["api_key"] == "sk-b"

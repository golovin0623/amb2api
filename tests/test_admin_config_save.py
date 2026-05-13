import pytest

from src.api import admin_routes


@pytest.mark.asyncio
async def test_save_config_parses_prompt_cache_string_booleans(monkeypatch):
    class _FakeAdapter:
        def __init__(self):
            self.values = {"override_env": False}

        async def get_config(self, key, default=None):
            return self.values.get(key, default)

        async def set_config(self, key, value):
            self.values[key] = value
            return True

    adapter = _FakeAdapter()

    async def fake_get_storage_adapter():
        return adapter

    monkeypatch.setattr(admin_routes, "get_storage_adapter", fake_get_storage_adapter)

    await admin_routes.save_config(
        {
            "prompt_cache_enabled": "false",
            "prompt_cache_affinity_enabled": "0",
        },
        token="test-token",
    )

    assert adapter.values["prompt_cache_enabled"] is False
    assert adapter.values["prompt_cache_affinity_enabled"] is False


@pytest.mark.asyncio
async def test_save_config_accepts_prompt_cache_true_strings(monkeypatch):
    class _FakeAdapter:
        def __init__(self):
            self.values = {"override_env": False}

        async def get_config(self, key, default=None):
            return self.values.get(key, default)

        async def set_config(self, key, value):
            self.values[key] = value
            return True

    adapter = _FakeAdapter()

    async def fake_get_storage_adapter():
        return adapter

    monkeypatch.setattr(admin_routes, "get_storage_adapter", fake_get_storage_adapter)

    await admin_routes.save_config(
        {
            "prompt_cache_enabled": "true",
            "prompt_cache_affinity_enabled": "on",
        },
        token="test-token",
    )

    assert adapter.values["prompt_cache_enabled"] is True
    assert adapter.values["prompt_cache_affinity_enabled"] is True

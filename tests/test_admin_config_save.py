import json

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


@pytest.mark.asyncio
async def test_get_creds_status_returns_frontend_contract(monkeypatch):
    class _FakeAdapter:
        async def list_credentials(self):
            return ["alpha.json"]

        async def get_credential(self, filename):
            raise AssertionError("credential content must not be loaded by status endpoint")

        async def get_credential_state(self, filename):
            assert filename == "alpha.json"
            return {
                "disabled": True,
                "error_codes": 429,
                "last_success": 1710000000,
                "user_email": "user@example.com",
            }

    async def fake_get_storage_adapter():
        return _FakeAdapter()

    monkeypatch.setattr(admin_routes, "get_storage_adapter", fake_get_storage_adapter)

    response = await admin_routes.get_creds_status(token="test-token")
    data = json.loads(response.body)

    assert data["creds"]["alpha.json"] == {
        "filename": "alpha.json",
        "status": {
            "disabled": True,
            "error_codes": [429],
            "last_success": 1710000000,
            "user_email": "us***@example.com",
        },
        "user_email": "us***@example.com",
    }


@pytest.mark.asyncio
async def test_get_all_config_redacts_sensitive_values(monkeypatch):
    class _FakeAdapter:
        async def get_all_config(self):
            return {
                "api_password": "plain-password",
                "assembly_dashboard_session:user@example.com": {"api_token": "secret-token"},
                "postgres_dsn": "postgres://user:pass@example/db",
                "available_models_selected": ["model-a"],
                "max_tokens_mode": "off",
            }

    async def fake_get_storage_adapter():
        return _FakeAdapter()

    monkeypatch.setattr(admin_routes, "get_storage_adapter", fake_get_storage_adapter)
    monkeypatch.delenv("REDIS_URI", raising=False)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)

    response = await admin_routes.get_all_config(token="test-token")
    data = json.loads(response.body)
    cfg = data["config"]

    assert cfg["api_password"] == "已隐藏敏感值"
    assert cfg["assembly_dashboard_session:*"] == "已隐藏敏感对象（1 项）"
    assert "assembly_dashboard_session:user@example.com" not in cfg
    assert cfg["postgres_dsn"] == "已隐藏敏感值"
    assert cfg["available_models_selected"] == ["model-a"]
    assert cfg["max_tokens_mode"] == "off"

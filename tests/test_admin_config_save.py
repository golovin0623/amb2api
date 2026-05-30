import json

import pytest

import config
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


def _patch_config_get_isolation(monkeypatch, store=None):
    """Make /config/get's adapter + password helpers inert so a test only
    exercises the enable_real_streaming resolution path. ``store`` seeds the
    storage adapter's config values (e.g. override_env, enable_real_streaming)."""

    values = dict(store or {})

    # 该 helper 直接换掉 adapter（不走 set_config 失效钩子），需手动清掉 config 快照缓存，
    # 以免上个用例缓存的值串场。
    config.invalidate_config_cache()

    class _FakeAdapter:
        async def get_config(self, key, default=None):
            return values.get(key, default)

    async def fake_get_storage_adapter():
        return _FakeAdapter()

    # admin_routes reads its own adapter; config.get_config_value reads config's.
    monkeypatch.setattr(admin_routes, "get_storage_adapter", fake_get_storage_adapter)
    monkeypatch.setattr(config, "get_storage_adapter", fake_get_storage_adapter)

    async def _empty(*args, **kwargs):
        return ""

    for name in (
        "get_assembly_api_key",
        "get_assembly_api_keys",
        "get_api_password",
        "get_panel_password",
        "get_server_port",
        "get_server_host",
    ):
        monkeypatch.setattr(admin_routes, name, _empty)


@pytest.mark.asyncio
async def test_get_config_enable_real_streaming_defaults_true(monkeypatch):
    """When nothing is stored, /config/get must report the runtime default for
    enable_real_streaming (True). A False default here caused the panel to render
    the toggle unchecked and silently persist False on the next save, disabling
    native streaming. Regression guard for that mismatch."""

    _patch_config_get_isolation(monkeypatch)
    monkeypatch.delenv("ENABLE_REAL_STREAMING", raising=False)
    monkeypatch.delenv("CONFIG_OVERRIDE_ENV", raising=False)

    response = await admin_routes.get_config(token="test-token")
    cfg = json.loads(response.body)["config"]

    assert cfg["enable_real_streaming"] is True


@pytest.mark.asyncio
async def test_get_config_enable_real_streaming_reflects_env_override(monkeypatch):
    """/config/get must report the *effective* value as a bool. With
    ENABLE_REAL_STREAMING=false in the env, nothing persisted, and env taking
    precedence (override_env off), the panel must show the toggle off — otherwise
    operators see the opposite of runtime and could persist the wrong value.
    Also guards against the env string ("false") leaking through as a truthy
    value. Regression guard for the env-override mismatch."""

    _patch_config_get_isolation(monkeypatch)
    monkeypatch.setenv("ENABLE_REAL_STREAMING", "false")
    monkeypatch.delenv("CONFIG_OVERRIDE_ENV", raising=False)

    response = await admin_routes.get_config(token="test-token")
    cfg = json.loads(response.body)["config"]

    assert cfg["enable_real_streaming"] is False


@pytest.mark.asyncio
async def test_get_config_enable_real_streaming_preserves_panel_override(monkeypatch):
    """When panel priority is on (override_env stored True) and the operator has
    stored enable_real_streaming=True, /config/get must return the stored panel
    value even if ENABLE_REAL_STREAMING=false is set in the env. Resolving via
    get_config_value honors that override_env precedence; reading the env getter
    directly would clobber the stored panel value on the next save."""

    _patch_config_get_isolation(
        monkeypatch,
        store={"override_env": True, "enable_real_streaming": True},
    )
    monkeypatch.setenv("ENABLE_REAL_STREAMING", "false")
    monkeypatch.delenv("CONFIG_OVERRIDE_ENV", raising=False)

    response = await admin_routes.get_config(token="test-token")
    cfg = json.loads(response.body)["config"]

    assert cfg["enable_real_streaming"] is True

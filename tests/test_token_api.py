"""多租户集成测试：/api/tokens CRUD（面板鉴权）+ user token 在 API 面的鉴权与配额。"""
import asyncio
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CONFIG_OVERRIDE_ENV", "true")

import web  # noqa: E402
from src.services.token_manager import TokenManager  # noqa: E402


def _patch_panel_auth(monkeypatch):
    async def _pwd():
        return "panel-pw"
    monkeypatch.setattr("src.api.auth.get_panel_password", _pwd)
    monkeypatch.setattr("src.api.auth.get_api_password", _pwd)


def _patch_token_manager(monkeypatch, target_module):
    tm = TokenManager()
    tm._initialized = True
    tm._tokens = {}

    async def _get():
        return tm

    monkeypatch.setattr(target_module, _get)
    return tm


def test_tokens_crud_requires_auth_and_works(monkeypatch):
    _patch_panel_auth(monkeypatch)
    _patch_token_manager(monkeypatch, "src.api.token_management_api.get_token_manager")
    c = TestClient(web.app)

    # 未鉴权拒绝
    assert c.get("/api/tokens").status_code in (401, 403)

    h = {"Authorization": "Bearer panel-pw"}
    # 新建
    r = c.post("/api/tokens", headers=h, json={"name": "alice", "quota": 2})
    assert r.status_code == 200
    tok = r.json()["token"]
    assert tok.startswith("sk-amb-")
    assert r.json()["name"] == "alice" and r.json()["quota"] == 2

    # 列出
    listing = c.get("/api/tokens", headers=h).json()["tokens"]
    assert any(t["token"] == tok for t in listing)

    # 更新（禁用）
    r = c.put(f"/api/tokens/{tok}", headers=h, json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False

    # 删除
    assert c.delete(f"/api/tokens/{tok}", headers=h).status_code == 200
    assert c.delete(f"/api/tokens/{tok}", headers=h).status_code == 404


class _FakeState:
    pass


class _FakeReq:
    def __init__(self):
        self.state = _FakeState()
        self.headers = {}


def test_user_token_auth_and_quota_enforcement(monkeypatch):
    from src.api import openai_router as orouter
    import fastapi

    tm = TokenManager()
    tm._initialized = True
    tm._tokens = {}

    async def _get_tm():
        return tm

    async def _master_pwd():
        return "master-pw"

    # _resolve_identity / enforce_token_quota 在函数内 import 这些符号
    monkeypatch.setattr("src.services.token_manager.get_token_manager", _get_tm)
    monkeypatch.setattr("config.get_api_password", _master_pwd)

    async def _run():
        meta = await tm.create_token(name="t", quota=1, allowed_models=["gpt-5"])
        tok = meta["token"]

        # 非法 token 拒绝
        assert await orouter._resolve_identity(_FakeReq(), "bad-token") is False

        # 主口令通过（master）
        req_m = _FakeReq()
        assert await orouter._resolve_identity(req_m, "master-pw") is True
        assert req_m.state.identity["master"] is True

        # user token 通过
        req_u = _FakeReq()
        assert await orouter._resolve_identity(req_u, tok) is True
        assert req_u.state.identity["master"] is False

        # master 不受配额限制
        await orouter.enforce_token_quota(req_m, "anything")

        # 模型不在白名单 → 403
        with pytest.raises(fastapi.HTTPException) as ei:
            await orouter.enforce_token_quota(req_u, "claude-x")
        assert ei.value.status_code == 403

        # quota=1：第一次放行，第二次 429
        await orouter.enforce_token_quota(req_u, "gpt-5")
        with pytest.raises(fastapi.HTTPException) as ei2:
            await orouter.enforce_token_quota(req_u, "gpt-5")
        assert ei2.value.status_code == 429

    asyncio.run(_run())


def test_enforce_token_quota_consume_false_checks_whitelist_not_quota(monkeypatch):
    """count_tokens 用 consume=False：强制白名单/禁用/过期，但不消费、不因配额耗尽而拒绝。"""
    from src.api import openai_router as orouter
    import fastapi

    tm = TokenManager()
    tm._initialized = True
    tm._tokens = {}

    async def _get_tm():
        return tm

    async def _master_pwd():
        return "master-pw"

    monkeypatch.setattr("src.services.token_manager.get_token_manager", _get_tm)
    monkeypatch.setattr("config.get_api_password", _master_pwd)

    async def _run():
        # quota=1, 白名单=[gpt-5]
        meta = await tm.create_token(name="ct", quota=1, allowed_models=["gpt-5"])
        tok = meta["token"]
        req = _FakeReq()
        assert await orouter._resolve_identity(req, tok) is True

        # 白名单外 → 403（即便不消费）
        with pytest.raises(fastapi.HTTPException) as ei:
            await orouter.enforce_token_quota(req, "claude-x", consume=False)
        assert ei.value.status_code == 403

        # 白名单内 → 放行，且不消费（used 不变）
        await orouter.enforce_token_quota(req, "gpt-5", consume=False)
        assert (await tm.get(tok))["used"] == 0

        # 耗尽配额后，consume=False 仍放行（免费端点不被配额拦）
        await tm.try_consume(tok, "gpt-5")  # used=1，达到 quota
        await orouter.enforce_token_quota(req, "gpt-5", consume=False)  # 不抛

    asyncio.run(_run())


def test_quota_exhausted_token_still_authenticates_then_429(monkeypatch):
    """配额耗尽的 token 鉴权阶段仍确立身份（不返回 403 密码错误），
    配额拦截发生在 enforce 阶段（429）。回归 Codex P2。"""
    from src.api import openai_router as orouter
    import fastapi

    tm = TokenManager()
    tm._initialized = True
    tm._tokens = {}

    async def _get_tm():
        return tm

    async def _master_pwd():
        return "master-pw"

    monkeypatch.setattr("src.services.token_manager.get_token_manager", _get_tm)
    monkeypatch.setattr("config.get_api_password", _master_pwd)

    async def _run():
        meta = await tm.create_token(name="exh", quota=1)
        tok = meta["token"]
        await tm.try_consume(tok, "gpt-5")  # used=1 == quota → 已耗尽

        # 鉴权仍应确立身份（不因配额耗尽而 403）
        req = _FakeReq()
        assert await orouter._resolve_identity(req, tok) is True
        assert req.state.identity["master"] is False

        # enforce(consume=True) 才返回 429
        with pytest.raises(fastapi.HTTPException) as ei:
            await orouter.enforce_token_quota(req, "gpt-5", consume=True)
        assert ei.value.status_code == 429

        # count_tokens 路径(consume=False)对耗尽 token 放行
        await orouter.enforce_token_quota(req, "gpt-5", consume=False)

    asyncio.run(_run())

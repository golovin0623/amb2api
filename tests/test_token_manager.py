"""per-user token 管理 + 配额（多租户 T1/T3）。"""
import asyncio
import time

from src.services.token_manager import TokenManager, TOKEN_PREFIX


def _fresh():
    tm = TokenManager()
    tm._initialized = True  # 跳过存储加载，隔离
    tm._tokens = {}
    return tm


def test_create_and_list():
    async def _run():
        tm = _fresh()
        meta = await tm.create_token(name="alice", quota=100)
        assert meta["token"].startswith(TOKEN_PREFIX)
        assert meta["name"] == "alice" and meta["quota"] == 100 and meta["used"] == 0
        assert meta["enabled"] is True
        tokens = await tm.list_tokens()
        assert len(tokens) == 1
    asyncio.run(_run())


def test_validate_rules():
    async def _run():
        tm = _fresh()
        meta = await tm.create_token(name="t", quota=5, allowed_models=["gpt-5"])
        tok = meta["token"]
        # 有效
        assert (await tm.validate(tok, "gpt-5"))["valid"]
        # 模型不在白名单
        r = await tm.validate(tok, "claude-x")
        assert not r["valid"] and r["reason"] == "model_not_allowed"
        # 未知 token
        assert (await tm.validate("nope"))["reason"] == "invalid_token"
        # 禁用
        await tm.update_token(tok, {"enabled": False})
        assert (await tm.validate(tok, "gpt-5"))["reason"] == "token_disabled"
        await tm.update_token(tok, {"enabled": True})
        # 过期
        await tm.update_token(tok, {"expires_at": time.time() - 1})
        assert (await tm.validate(tok, "gpt-5"))["reason"] == "token_expired"
    asyncio.run(_run())


def test_try_consume_enforces_quota_atomically():
    async def _run():
        tm = _fresh()
        meta = await tm.create_token(name="t", quota=3)
        tok = meta["token"]
        # 10 并发消费，quota=3 → 恰好 3 个成功
        results = await asyncio.gather(*[tm.try_consume(tok, "gpt-5") for _ in range(10)])
        ok = sum(1 for r in results if r["valid"])
        assert ok == 3, f"quota=3 并发只应放行 3 个，实际 {ok}"
        # used 已到 3，再消费被拒
        assert (await tm.try_consume(tok, "gpt-5"))["reason"] == "quota_exceeded"
        assert (await tm.get(tok))["used"] == 3
    asyncio.run(_run())


def test_unlimited_quota():
    async def _run():
        tm = _fresh()
        meta = await tm.create_token(name="unl", quota=None)
        tok = meta["token"]
        for _ in range(50):
            assert (await tm.try_consume(tok, "gpt-5"))["valid"]
    asyncio.run(_run())


def test_update_and_delete():
    async def _run():
        tm = _fresh()
        tok = (await tm.create_token(name="x"))["token"]
        await tm.update_token(tok, {"name": "renamed", "quota": 9})
        assert (await tm.get(tok))["name"] == "renamed"
        assert (await tm.get(tok))["quota"] == 9
        # reset_used
        await tm.try_consume(tok, "m")
        await tm.update_token(tok, {"reset_used": True})
        assert (await tm.get(tok))["used"] == 0
        assert await tm.delete_token(tok) is True
        assert await tm.get(tok) is None
    asyncio.run(_run())

"""配额预占（修 C）：消除"选 key↔记账"之间的 TOCTOU 超计。

reserve_key_for_model 在锁内把 in-flight 预占计入限额，N 个并发不会同时挤过最后一个槽；
record_call / release_reservation 归还预占；TTL 兜底任何漏释放的路径（自愈、且偏保守）。
"""
import asyncio
import time

from src.stats.unified_stats import UnifiedStats, mask_key, get_key_hash


def _make_stats(limit: int):
    us = UnifiedStats()
    us._initialized = True
    return us


def _seed_key(us, api_key, limit):
    masked = mask_key(api_key)
    us._stats[masked] = {
        "full_key_hash": get_key_hash(api_key),
        "success_count": 0,
        "failure_count": 0,
        "daily_limit_total": limit,
        "daily_limit_models": {},
        "model_counts": {},
        "next_reset_time": time.time() + 100000,  # 远期，避免日重置
    }
    return masked


def test_reservation_prevents_overshoot():
    async def _run():
        us = _make_stats(2)
        api_key = "k" * 40
        _seed_key(us, api_key, 2)

        r1 = await us.reserve_key_for_model(api_key, "gpt-5")
        r2 = await us.reserve_key_for_model(api_key, "gpt-5")
        r3 = await us.reserve_key_for_model(api_key, "gpt-5")
        assert r1["allowed"] and r2["allowed"], "前两次应允许"
        assert not r3["allowed"], "两个 in-flight 预占已占满 limit=2，第三个必须被拦"
        assert r3["reason"] == "total_limit_reached"

    asyncio.run(_run())


def test_release_frees_slot_and_record_call_does_not():
    """预占的释放由 release_reservation 负责（请求循环 finally 调用）；
    record_call 只提交 success_count、不动 pending。两者解耦，避免重复/错释放。"""
    async def _run():
        us = _make_stats(2)
        api_key = "k" * 40
        _seed_key(us, api_key, 2)

        await us.reserve_key_for_model(api_key, "gpt-5")
        await us.reserve_key_for_model(api_key, "gpt-5")  # pending=2, 占满
        assert not (await us.reserve_key_for_model(api_key, "gpt-5"))["allowed"]

        # record_call 不再释放预占：success_count=1，pending 仍=2 → 1+2 >= 2，仍占满
        await us.record_call(api_key, "gpt-5", success=True)
        assert not (await us.reserve_key_for_model(api_key, "gpt-5"))["allowed"]

        # 显式释放两个预占（模拟两次请求各自 finally 归还）
        us.release_reservation(api_key)
        us.release_reservation(api_key)  # pending 2->0
        # success_count=1, pending=0 → 1 < 2，放行
        assert (await us.reserve_key_for_model(api_key, "gpt-5"))["allowed"]

    async def _run_balanced():
        # 一次完整成功请求的净效果：reserve(+1 pending) → record_call(+1 success)
        # → release(-1 pending)，最终 success_count+1、pending 归零。
        us = _make_stats(3)
        api_key = "k" * 40
        _seed_key(us, api_key, 3)
        for _ in range(3):
            r = await us.reserve_key_for_model(api_key, "gpt-5")
            assert r["allowed"]
            await us.record_call(api_key, "gpt-5", success=True)
            us.release_reservation(api_key)
        # 现在 success_count=3 == limit → 即便 pending=0 也应拒绝
        assert not (await us.reserve_key_for_model(api_key, "gpt-5"))["allowed"]

    asyncio.run(_run())
    asyncio.run(_run_balanced())

    asyncio.run(_run())


def test_concurrent_reservations_do_not_overshoot():
    async def _run():
        us = _make_stats(3)
        api_key = "k" * 40
        _seed_key(us, api_key, 3)
        # 10 个并发预占，limit=3 → 恰好 3 个 allowed
        results = await asyncio.gather(
            *[us.reserve_key_for_model(api_key, "gpt-5") for _ in range(10)]
        )
        allowed = sum(1 for r in results if r["allowed"])
        assert allowed == 3, f"limit=3 下并发只应放行 3 个，实际 {allowed}"

    asyncio.run(_run())


def test_model_limit_pending_is_scoped_per_model():
    """每模型限额只计同 (key, model) 的在途预占：A 模型挂起请求不占 B 模型每模型配额。"""
    async def _run():
        us = _make_stats(100)
        api_key = "k" * 40
        masked = mask_key(api_key)
        us._stats[masked] = {
            "full_key_hash": get_key_hash(api_key),
            "success_count": 0,
            "failure_count": 0,
            "daily_limit_total": 100,
            "daily_limit_models": {"gpt-4": 1},
            "model_counts": {},
            "next_reset_time": time.time() + 100000,
        }
        # 预占一个 claude 在途请求（同 key，不同模型）
        assert (await us.reserve_key_for_model(api_key, "claude"))["allowed"]
        # gpt-4 首个请求应放行：claude 的在途不应占用 gpt-4 的每模型配额(=1)
        r = await us.reserve_key_for_model(api_key, "gpt-4")
        assert r["allowed"], f"claude 在途不应阻塞 gpt-4，reason={r.get('reason')}"
        # gpt-4 已有 1 个在途(==限额)，第二个 gpt-4 被每模型限额拦
        r2 = await us.reserve_key_for_model(api_key, "gpt-4")
        assert not r2["allowed"] and r2["reason"] == "model_limit_reached"
        # 释放 gpt-4 的一个预占后，gpt-4 再次可预占
        us.release_reservation(api_key, "gpt-4")
        assert (await us.reserve_key_for_model(api_key, "gpt-4"))["allowed"]

    asyncio.run(_run())


def test_ttl_self_heals_leaked_reservation():
    async def _run():
        us = _make_stats(1)
        us._pending_ttl = 0.05  # 50ms TTL
        api_key = "k" * 40
        _seed_key(us, api_key, 1)

        assert (await us.reserve_key_for_model(api_key, "gpt-5"))["allowed"]
        # 不释放（模拟漏释放路径）；占满
        assert not (await us.reserve_key_for_model(api_key, "gpt-5"))["allowed"]
        await asyncio.sleep(0.08)  # 等预占过期
        assert (await us.reserve_key_for_model(api_key, "gpt-5"))["allowed"], "TTL 过期后应自愈放行"

    asyncio.run(_run())

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


def test_record_call_releases_reservation():
    async def _run():
        us = _make_stats(2)
        api_key = "k" * 40
        masked = _seed_key(us, api_key, 2)

        await us.reserve_key_for_model(api_key, "gpt-5")
        await us.reserve_key_for_model(api_key, "gpt-5")  # pending=2, 占满
        assert not (await us.reserve_key_for_model(api_key, "gpt-5"))["allowed"]

        # 一次成功：success_count=1, pending 2->1 → 1+1=2 >= 2，仍占满
        await us.record_call(api_key, "gpt-5", success=True)
        assert not (await us.reserve_key_for_model(api_key, "gpt-5"))["allowed"]

        # 一次失败：failure_count+1, pending 1->0；success_count 仍=1 → 1<2，放行
        await us.record_call(api_key, "gpt-5", success=False)
        assert (await us.reserve_key_for_model(api_key, "gpt-5"))["allowed"]

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

"""优雅关闭：flush_unified_stats / flush_rate_limiter 把在途改动落盘。

注：每个测试用独立 asyncio.run（新事件循环），而模块单例里的 asyncio.Lock 会
绑定到首次创建它的循环。为隔离，测试开始时重置单例，使其绑定到本测试的循环。
"""
import asyncio
import time


def test_flush_unified_stats_persists_dirty():
    async def _run():
        import src.stats.unified_stats as us
        us._unified_stats = None  # 重置单例，绑定到本循环
        stats = await us.get_unified_stats()
        # 制造"脏"且刚保存过（普通节流保存会被跳过，只有 force 才会写）
        stats._dirty = True
        stats._last_save_time = time.time()
        await us.flush_unified_stats()
        assert stats._dirty is False, "flush 应强制落盘并清除 dirty"

    asyncio.run(_run())


def test_flush_rate_limiter_persists_and_cancels_timer():
    async def _run():
        from src.services import rate_limiter as rl_mod
        rl_mod._rate_limiter = None  # 重置单例，绑定到本循环
        rl = await rl_mod.get_rate_limiter()
        await rl.update_rate_limit(0, 100, 50, 0)  # 安排去抖任务、置脏
        assert rl._dirty is True
        await rl_mod.flush_rate_limiter()
        assert rl._dirty is False, "flush 后应已落盘"
        # 让取消传播后，去抖任务应结束（不会再触发一次保存）
        await asyncio.sleep(0)
        assert rl._save_task is None or rl._save_task.done()

    asyncio.run(_run())

#!/usr/bin/env python3
"""速率限制持久化（重写）：RateLimiter 是单一真相源，数据应跨"重启"往返保留。

历史上 amb2api 维护了两套并行的限流缓存（assembly_client._rate_limit_info 旧系统
与 RateLimiter），两者写同一个 `rate_limit_info` 存储键、互相覆盖。现已统一到
RateLimiter，本测试验证其持久化往返。
"""

import asyncio

from src.services.rate_limiter import RateLimiter
from src.storage.storage_adapter import get_storage_adapter


async def _roundtrip(data):
    """data: {idx: (limit, remaining, reset_time)} -> 返回重载后的 RateLimiter。"""
    adapter = await get_storage_adapter()
    await adapter.set_config("rate_limit_info", {})  # 隔离：清空旧数据

    rl1 = RateLimiter()
    await rl1.initialize()
    for idx, (limit, remaining, reset_time) in data.items():
        await rl1.update_rate_limit(idx, limit, remaining, reset_time)
    if rl1._save_task is not None:
        rl1._save_task.cancel()  # 取消去抖任务，避免悬挂
    await rl1._save_rate_limits()  # 直接 await 落盘（不依赖 fire-and-forget）

    # 模拟重启：全新实例从存储加载
    rl2 = RateLimiter()
    await rl2.initialize()
    return rl2


def test_rate_limit_round_trip_preserves_fields():
    data = {
        0: (100, 50, 0),
        1: (200, 150, 0),
        5: (50, 0, 0),     # exhausted
        10: (300, 200, 0),
    }
    rl2 = asyncio.run(_roundtrip(data))

    loaded = rl2._rate_limits
    for idx, (limit, remaining, _reset) in data.items():
        assert idx in loaded, f"key {idx} 未持久化"
        info = loaded[idx]
        assert isinstance(idx, int)
        assert info.limit == limit
        assert info.remaining == remaining
        assert info.used == limit - remaining


def test_rate_limit_round_trip_empty():
    rl2 = asyncio.run(_roundtrip({}))
    assert rl2._rate_limits == {}


if __name__ == "__main__":
    test_rate_limit_round_trip_preserves_fields()
    test_rate_limit_round_trip_empty()
    print("✅ persistence round-trip OK")

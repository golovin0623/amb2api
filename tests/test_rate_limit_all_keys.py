#!/usr/bin/env python3
"""速率限制展示（重写）：/rate-limits 必须为每个已配置 key 返回状态。

数据源已从旧的 assembly_client._rate_limit_info 字典统一为 RateLimiter。
本测试验证：有数据的 key 显示 active/exhausted，无数据的 key 显示 unused，
且条目数 == 配置的 key 数。
"""

import asyncio

from src.services.rate_limiter import RateLimiter
from src.services.assembly_client import get_rate_limit_info, _mask_key


def _build_endpoint_view(keys, rate_info):
    """复刻 admin_routes.rate_limits 的逐 key 组装逻辑。"""
    result = []
    for idx, key in enumerate(keys):
        masked = _mask_key(key)
        if idx in rate_info:
            info = rate_info[idx]
            result.append({
                "index": idx,
                "key": masked,
                "limit": info.get("limit", 0),
                "remaining": info.get("remaining", 0),
                "used": info.get("used", 0),
                "status": "active" if info.get("remaining", 0) > 0 else "exhausted",
            })
        else:
            result.append({
                "index": idx, "key": masked, "limit": 0,
                "remaining": 0, "used": 0, "status": "unused",
            })
    return result


async def _setup_rate_limiter(data):
    """data: {idx: (limit, remaining)}; 返回一个受控 RateLimiter（不触存储加载）。"""
    rl = RateLimiter()
    rl._initialized = True  # 跳过存储加载，保证隔离
    for idx, (limit, remaining) in data.items():
        await rl.update_rate_limit(idx, limit, remaining, 0)
    if rl._save_task is not None:
        rl._save_task.cancel()  # 取消去抖任务，避免悬挂
    return rl


def test_all_configured_keys_have_status(monkeypatch):
    keys = ["a" * 32, "b" * 36, "c" * 40, "d" * 44]
    # 仅 idx 1（充足）和 idx 2（耗尽）有数据
    rl = asyncio.run(_setup_rate_limiter({1: (100, 40), 2: (100, 0)}))

    async def _fake_get_rl():
        return rl

    monkeypatch.setattr("src.services.assembly_client.get_rate_limiter", _fake_get_rl)

    rate_info = asyncio.run(get_rate_limit_info())
    result = _build_endpoint_view(keys, rate_info)

    # 每个配置的 key 都有一条
    assert len(result) == len(keys)
    statuses = {e["index"]: e["status"] for e in result}
    assert statuses[0] == "unused"
    assert statuses[1] == "active"
    assert statuses[2] == "exhausted"
    assert statuses[3] == "unused"
    # 无数据 key 各项为 0
    assert result[0]["limit"] == 0 and result[3]["remaining"] == 0
    # 有数据 key 的 used 推导正确
    assert result[1]["used"] == 60


def test_no_keys_with_data_all_unused(monkeypatch):
    keys = ["x" * 32, "y" * 32]
    rl = asyncio.run(_setup_rate_limiter({}))

    async def _fake_get_rl():
        return rl

    monkeypatch.setattr("src.services.assembly_client.get_rate_limiter", _fake_get_rl)
    rate_info = asyncio.run(get_rate_limit_info())
    result = _build_endpoint_view(keys, rate_info)
    assert len(result) == 2
    assert all(e["status"] == "unused" for e in result)


if __name__ == "__main__":
    print("Run via pytest")

"""配置快照缓存：命中减少存储读取，且写入/失效后立即可见。"""
import asyncio

import config


class _CountingAdapter:
    def __init__(self, values):
        self.values = dict(values)
        self.reads = 0

    async def get_config(self, key, default=None):
        self.reads += 1
        return self.values.get(key, default)


def test_cache_hit_reduces_storage_reads(monkeypatch):
    adapter = _CountingAdapter({"override_env": True, "calls_per_rotation": 50})

    async def _fake_get_adapter():
        return adapter

    monkeypatch.setattr(config, "get_storage_adapter", _fake_get_adapter)
    monkeypatch.setattr(config, "_CONFIG_CACHE_TTL", 30.0)
    config.invalidate_config_cache()
    monkeypatch.delenv("CALLS_PER_ROTATION", raising=False)
    monkeypatch.delenv("CONFIG_OVERRIDE_ENV", raising=False)

    async def _run():
        v1 = await config.get_config_value("calls_per_rotation", 100)
        v2 = await config.get_config_value("calls_per_rotation", 100)
        v3 = await config.get_config_value("calls_per_rotation", 100)
        return v1, v2, v3

    v1, v2, v3 = asyncio.run(_run())
    assert v1 == v2 == v3 == 50
    # override_env + calls_per_rotation 各读一次即缓存；后续命中不再读存储。
    # 3 次调用若无缓存会是 6 次读取，有缓存应远少于此。
    assert adapter.reads <= 2, f"缓存未生效，读取次数={adapter.reads}"


def test_invalidate_makes_new_value_visible(monkeypatch):
    adapter = _CountingAdapter({"override_env": True, "some_key": "old"})

    async def _fake_get_adapter():
        return adapter

    monkeypatch.setattr(config, "get_storage_adapter", _fake_get_adapter)
    monkeypatch.setattr(config, "_CONFIG_CACHE_TTL", 30.0)
    config.invalidate_config_cache()

    async def _run():
        first = await config.get_config_value("some_key", None)
        # 直接改底层值并失效该键
        adapter.values["some_key"] = "new"
        config.invalidate_config_cache("some_key")
        second = await config.get_config_value("some_key", None)
        return first, second

    first, second = asyncio.run(_run())
    assert first == "old"
    assert second == "new", "失效后应读到新值"


def test_set_config_invalidates_snapshot(monkeypatch):
    """通过 StorageAdapter.set_config 写入应自动失效快照（无需手动 invalidate）。"""
    from src.storage import storage_adapter as sa

    cleared = {"keys": []}

    def _spy(key=None):
        cleared["keys"].append(key)

    monkeypatch.setattr("config.invalidate_config_cache", _spy)

    async def _run():
        adapter = await sa.get_storage_adapter()
        await adapter.set_config("__cache_probe__", "v")

    asyncio.run(_run())
    assert "__cache_probe__" in cleared["keys"], "set_config 应触发快照失效"

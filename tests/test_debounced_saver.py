"""回归：DebouncedSaver 在保存失败时保留脏标记；user_tokens 在 /config/all 被脱敏。"""
import asyncio

from src.core.debounced_saver import DebouncedSaver
from src.api.admin_routes import _is_sensitive_config_key


class _FailingSaver(DebouncedSaver):
    def __init__(self, interval=0.0, fail=False):
        self._init_debounce(interval)
        self.fail = fail
        self.save_calls = 0

    async def _do_save(self) -> bool:
        self.save_calls += 1
        return not self.fail  # fail=True -> 返回 False（保存失败）


def test_flush_preserves_dirty_on_failed_save():
    async def _run():
        s = _FailingSaver(interval=0.0, fail=True)
        s._dirty = True
        await s._flush()
        assert s._dirty is True, "保存失败必须保留脏标记以便重试"
        assert s.save_calls == 1, "失败后不应忙循环重试"
        # 恢复存储后再 flush 应成功清脏
        s.fail = False
        await s._flush()
        assert s._dirty is False
    asyncio.run(_run())


def test_flush_clears_dirty_on_success():
    async def _run():
        s = _FailingSaver(interval=0.0, fail=False)
        s._dirty = True
        await s._flush()
        assert s._dirty is False and s.save_calls == 1
    asyncio.run(_run())


def test_user_tokens_key_is_redacted():
    # 下游 bearer token 存于 user_tokens；/config/all 必须脱敏（与上游 key/session 一致）
    assert _is_sensitive_config_key("user_tokens") is True
    assert _is_sensitive_config_key("USER_TOKENS") is True
    # 既有敏感键仍判定为敏感
    assert _is_sensitive_config_key("assembly_api_keys") is True
    assert _is_sensitive_config_key("api_password") is True
    # 普通键不脱敏
    assert _is_sensitive_config_key("calls_per_rotation") is False

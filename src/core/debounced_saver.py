"""
去抖保存（DebouncedSaver）。

把频繁的"整块写存储"合并为窗口内的一次尾随写，降低写放大。RateLimiter / TokenManager
等"每请求都会改一点、但不需要每次都落盘"的组件复用本 mixin，避免三处各写一份去抖逻辑而漂移。

用法：
    class Foo(DebouncedSaver):
        def __init__(self):
            self._init_debounce(interval_seconds)   # 0 表示不延迟、立即异步保存
        async def _do_save(self):                   # 子类实现真正的持久化
            ...
        def on_change(self):
            self._mark_dirty()                      # 有改动时调用
    # 进程退出：await foo.flush()
"""
import asyncio
from typing import Optional


class DebouncedSaver:
    """合并窗口内多次保存为一次尾随写。子类需实现 ``_do_save``。"""

    _dirty: bool
    _save_task: Optional["asyncio.Task"]
    _save_interval: float

    def _init_debounce(self, interval: float) -> None:
        self._dirty = False
        self._save_task = None
        try:
            self._save_interval = max(0.0, float(interval))
        except (TypeError, ValueError):
            self._save_interval = 5.0

    def _mark_dirty(self) -> None:
        """标记有未保存改动，并安排一次尾随保存（窗口内多次只落盘一次）。"""
        self._dirty = True
        if self._save_interval <= 0:
            # 立即异步保存；存到 _save_task 保持强引用，避免任务被 GC 提前回收
            if self._save_task is None or self._save_task.done():
                self._save_task = asyncio.create_task(self._flush())
            return
        if self._save_task is None or self._save_task.done():
            self._save_task = asyncio.create_task(self._delayed_save())

    async def _delayed_save(self) -> None:
        try:
            await asyncio.sleep(self._save_interval)
            await self._flush()
        except asyncio.CancelledError:
            pass

    async def _flush(self) -> None:
        # 先清脏标记再保存：这样 _do_save() 期间（唯一 await 点）到来的改动会把 _dirty 重新置 True，
        # while 循环再保存一轮，覆盖"保存期间的改动"。最后一次判定到返回之间无 await，无残留窗口。
        # 失败处理：_do_save 返回 False 或抛异常 → 恢复 _dirty 以便下次触发/shutdown flush 重试，
        # 并 break 避免忙循环（持续失败时不空转）。
        while self._dirty:
            self._dirty = False
            try:
                ok = await self._do_save()
            except Exception:
                self._dirty = True   # 保存异常：保留脏标记待重试（具体异常由 _do_save 内部记录）
                break
            if ok is False:
                self._dirty = True   # 保存失败：同上
                break

    async def flush(self) -> None:
        """进程退出前调用：取消挂起的定时器并立即落盘未保存改动。"""
        task = self._save_task
        if task is not None and not task.done():
            task.cancel()
        await self._flush()

    async def _do_save(self) -> None:  # pragma: no cover - 子类实现
        raise NotImplementedError

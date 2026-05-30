"""
下游用户 Token 管理（多租户）。

与上游 AssemblyAI key 不同，这里管理的是**面向下游消费者**发放的 API token：
每个 token 有独立名称、启用状态、可选配额（总调用次数）、可选模型白名单、可选过期。
让运营者可以把访问"分发"给多人/客户，而不只是代理自己。

存储：单个 config 键 `user_tokens` = {token: meta}，走 storage adapter（4 后端通用）。
配额消费 `try_consume` 在锁内原子校验+自增，天然无 TOCTOU。
"""
import asyncio
import os
import secrets
import time
from typing import Any, Dict, List, Optional

from log import log
from ..storage.storage_adapter import get_storage_adapter

_CONFIG_KEY = "user_tokens"
TOKEN_PREFIX = "sk-amb-"


def _now() -> float:
    return time.time()


def mask_token(token: str) -> str:
    if not token:
        return ""
    t = str(token)
    if len(t) <= 12:
        return t[:4] + "***"
    return t[:10] + "..." + t[-4:]


class TokenManager:
    """下游 user token 的增删改查 + 配额消费。"""

    def __init__(self):
        self._tokens: Dict[str, Dict[str, Any]] = {}
        self._initialized = False
        self._lock = asyncio.Lock()
        # 配额自增的去抖保存
        self._dirty = False
        self._save_task: Optional[asyncio.Task] = None
        try:
            self._save_interval = max(0.0, float(os.getenv("TOKEN_SAVE_INTERVAL", "5")))
        except ValueError:
            self._save_interval = 5.0

    async def initialize(self):
        if self._initialized:
            return
        await self._load()
        self._initialized = True

    async def _load(self):
        try:
            adapter = await get_storage_adapter()
            data = await adapter.get_config(_CONFIG_KEY, {})
            if isinstance(data, dict):
                self._tokens = {
                    str(k): v for k, v in data.items() if isinstance(v, dict)
                }
            log.debug(f"Loaded {len(self._tokens)} user tokens")
        except Exception as e:
            log.error(f"Failed to load user tokens: {e}")

    async def _save(self):
        try:
            adapter = await get_storage_adapter()
            await adapter.set_config(_CONFIG_KEY, self._tokens)
        except Exception as e:
            log.error(f"Failed to save user tokens: {e}")

    def _schedule_save(self):
        self._dirty = True
        if self._save_interval <= 0:
            asyncio.create_task(self._flush())
            return
        if self._save_task is None or self._save_task.done():
            self._save_task = asyncio.create_task(self._delayed_save())

    async def _delayed_save(self):
        try:
            await asyncio.sleep(self._save_interval)
            await self._flush()
        except asyncio.CancelledError:
            pass

    async def _flush(self):
        if self._dirty:
            self._dirty = False
            await self._save()

    # ---- CRUD ----------------------------------------------------------------

    async def create_token(
        self,
        name: str = "",
        quota: Optional[int] = None,
        allowed_models: Optional[List[str]] = None,
        expires_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        await self.initialize()
        token = TOKEN_PREFIX + secrets.token_urlsafe(24)
        meta = {
            "token": token,
            "name": name or "token",
            "enabled": True,
            "created_at": _now(),
            "expires_at": expires_at,
            "quota": quota,                 # None = 无限
            "used": 0,
            "allowed_models": allowed_models,  # None = 全部模型
        }
        async with self._lock:
            self._tokens[token] = meta
            await self._save()
        log.info(f"Created user token {mask_token(token)} (name={meta['name']})")
        return meta

    async def list_tokens(self) -> List[Dict[str, Any]]:
        await self.initialize()
        return list(self._tokens.values())

    async def get(self, token: str) -> Optional[Dict[str, Any]]:
        await self.initialize()
        return self._tokens.get(token)

    async def update_token(self, token: str, changes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        await self.initialize()
        async with self._lock:
            meta = self._tokens.get(token)
            if not meta:
                return None
            for field in ("name", "enabled", "quota", "allowed_models", "expires_at"):
                if field in changes:
                    meta[field] = changes[field]
            if changes.get("reset_used"):
                meta["used"] = 0
            await self._save()
            return meta

    async def delete_token(self, token: str) -> bool:
        await self.initialize()
        async with self._lock:
            if token in self._tokens:
                del self._tokens[token]
                await self._save()
                log.info(f"Deleted user token {mask_token(token)}")
                return True
        return False

    # ---- 校验 + 配额 ---------------------------------------------------------

    @staticmethod
    def _check_meta(meta: Optional[Dict[str, Any]], model: Optional[str]) -> str:
        """返回拒绝原因（通过为空串）。validate 与 try_consume 共用，避免两处规则漂移。

        语义要点：
        - allowed_models 为 None = 不限模型；为 []（空白名单）= 拒绝所有模型（用 `is not None` 判定，
          避免空列表被当作"无限制"）。
        - expires_at 为 None = 永不过期；任意非 None 值都按真实时间戳判定（0 视为已过期，fail-safe）。
        """
        if not meta:
            return "invalid_token"
        if not meta.get("enabled", True):
            return "token_disabled"
        exp = meta.get("expires_at")
        if exp is not None:
            try:
                if _now() > float(exp):
                    return "token_expired"
            except (TypeError, ValueError):
                return "token_expired"
        allowed = meta.get("allowed_models")
        if model and allowed is not None and model not in allowed:
            return "model_not_allowed"
        quota = meta.get("quota")
        if quota is not None and meta.get("used", 0) >= int(quota):
            return "quota_exceeded"
        return ""

    async def validate(self, token: str, model: Optional[str] = None) -> Dict[str, Any]:
        """只读校验（不消费配额）。返回 {valid, reason, meta}。"""
        await self.initialize()
        meta = self._tokens.get(token)
        reason = self._check_meta(meta, model)
        return {"valid": reason == "", "reason": reason, "meta": meta}

    async def try_consume(self, token: str, model: Optional[str] = None) -> Dict[str, Any]:
        """原子校验 + 消费一次配额。返回 {valid, reason, meta}。

        在锁内 check-and-increment，天然无 TOCTOU：并发请求不会越过配额上限。
        按"接受的请求"计数（与上游成功与否无关，语义清晰）。
        """
        await self.initialize()
        async with self._lock:
            meta = self._tokens.get(token)
            reason = self._check_meta(meta, model)
            if reason:
                return {"valid": False, "reason": reason, "meta": meta}
            # 消费
            meta["used"] = meta.get("used", 0) + 1
            self._schedule_save()
            return {"valid": True, "reason": "", "meta": meta}


_token_manager: Optional[TokenManager] = None


async def get_token_manager() -> TokenManager:
    global _token_manager
    if _token_manager is None:
        _token_manager = TokenManager()
        await _token_manager.initialize()
    return _token_manager


async def flush_token_manager() -> None:
    """进程退出前刷新 token 配额计数。"""
    global _token_manager
    if _token_manager is not None:
        task = _token_manager._save_task
        if task is not None and not task.done():
            task.cancel()
        await _token_manager._flush()

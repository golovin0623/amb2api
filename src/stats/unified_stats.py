"""
统一统计模块
整合所有统计数据源，确保总调用数与详情统计保持一致

设计原则：
1. 使用脱敏密钥 (masked_key) 作为统一的标识符
2. 所有统计数据存储在一个地方
3. 当密钥被删除时，同步删除其统计数据
"""
import asyncio
import hashlib
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Tuple

from log import log
from ..storage.storage_adapter import get_storage_adapter

DEFAULT_DAILY_LIMIT_TOTAL = 1000

def _get_next_utc_7am() -> datetime:
    """计算下一个 UTC 07:00 时间用于配额重置"""
    now = datetime.now(timezone.utc)
    today_7am = now.replace(hour=7, minute=0, second=0, microsecond=0)
    
    if now < today_7am:
        return today_7am
    else:
        return today_7am + timedelta(days=1)


def _parse_datetime(value: Any) -> Optional[datetime]:
    """解析 ISO 时间字符串为 UTC datetime"""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_non_negative_int(value: Any, default: int = 0) -> int:
    """将任意值转换为非负整数"""
    try:
        parsed = int(value)
        if parsed < 0:
            return default
        return parsed
    except Exception:
        return default


def _as_positive_int(value: Any, default: int) -> int:
    """将任意值转换为正整数"""
    try:
        parsed = int(value)
        if parsed > 0:
            return parsed
    except Exception:
        pass
    return default


def _as_non_negative_float(value: Any, default: float = 0.0) -> float:
    """将任意值转换为非负浮点数"""
    try:
        parsed = float(value)
        if parsed < 0:
            return default
        return parsed
    except Exception:
        return default


def mask_key(key: str) -> str:
    """
    脱敏密钥，与 assembly_client.py 中的 _mask_key 保持一致
    """
    if not key:
        return ""
    k = str(key)
    if len(k) <= 8:
        return k[:2] + "***"
    return k[:4] + "..." + k[-4:]


def get_key_hash(key: str) -> str:
    """获取密钥的哈希值（用于存储）"""
    return hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]


class UnifiedStats:
    """
    统一统计管理器
    
    数据结构：
    {
        "masked_key": {
            "full_key_hash": "xxx",  # 完整密钥的哈希，用于验证
            "success_count": 0,
            "failure_count": 0,
            "model_counts": {"model_name": count},
            "last_call_time": timestamp,
            "created_time": timestamp,
        }
    }
    """
    
    def __init__(self):
        self._stats: Dict[str, Dict[str, Any]] = {}
        self._initialized = False
        self._save_lock = asyncio.Lock()
        self._dirty = False
        self._last_save_time = 0
        self._save_interval = 30  # 保存间隔（秒）
        # 配额预占：记录每个 key 的 in-flight 预占到期时间戳（仅内存）。
        # 用 TTL 自愈：即便某些重试路径漏释放，过期后自动归还，limit 检查始终偏保守
        #（宁可早一点拦，绝不超计），消除"选 key↔记账"之间的 TOCTOU 超计。
        self._pending: Dict[str, list] = {}
        self._reserve_lock = asyncio.Lock()
        try:
            self._pending_ttl = max(1.0, float(os.getenv("QUOTA_RESERVATION_TTL", "120")))
        except ValueError:
            self._pending_ttl = 120.0

    def _live_pending(self, masked: str) -> int:
        """剪除过期预占，返回当前存活的预占数。"""
        lst = self._pending.get(masked)
        if not lst:
            return 0
        now = time.time()
        lst = [exp for exp in lst if exp > now]
        if lst:
            self._pending[masked] = lst
        else:
            self._pending.pop(masked, None)
        return len(lst)

    def _release_one_pending(self, masked: str) -> None:
        """归还一个预占槽（弹出最早到期的一个）。"""
        lst = self._pending.get(masked)
        if not lst:
            return
        now = time.time()
        lst = [exp for exp in lst if exp > now]
        if lst:
            lst.pop(0)
        if lst:
            self._pending[masked] = lst
        else:
            self._pending.pop(masked, None)

    def _build_default_entry(self, key_hash: str = "") -> Dict[str, Any]:
        """构建默认统计结构"""
        return {
            "full_key_hash": key_hash,
            "success_count": 0,
            "failure_count": 0,
            "model_counts": {},
            "last_call_time": 0,
            "created_time": time.time(),
            "daily_limit_total": DEFAULT_DAILY_LIMIT_TOTAL,
            "daily_limit_models": {},
            "next_reset_time": _get_next_utc_7am().isoformat(),
        }

    def _normalize_model_counts(self, raw: Any) -> Tuple[Dict[str, Dict[str, int]], bool]:
        """标准化模型计数结构，兼容旧数据格式"""
        normalized: Dict[str, Dict[str, int]] = {}
        changed = False
        if not isinstance(raw, dict):
            return normalized, True

        for model_name, count_data in raw.items():
            model = str(model_name).strip()
            if not model:
                changed = True
                continue

            if isinstance(count_data, int):
                normalized[model] = {"ok": _as_non_negative_int(count_data), "fail": 0}
                changed = True
                continue

            if not isinstance(count_data, dict):
                changed = True
                continue

            ok = _as_non_negative_int(count_data.get("ok", 0))
            fail = _as_non_negative_int(count_data.get("fail", 0))
            if ok != count_data.get("ok", 0) or fail != count_data.get("fail", 0):
                changed = True
            normalized[model] = {"ok": ok, "fail": fail}

        return normalized, changed

    def _normalize_model_limits(self, raw: Any) -> Dict[str, int]:
        """标准化模型限额结构，仅保留正整数限额"""
        if not isinstance(raw, dict):
            return {}

        normalized: Dict[str, int] = {}
        for model_name, limit in raw.items():
            model = str(model_name).strip()
            if not model:
                continue
            parsed_limit = _as_positive_int(limit, 0)
            if parsed_limit > 0:
                normalized[model] = parsed_limit
        return normalized

    def _normalize_entry(self, entry: Any, key_hash: str = "") -> Tuple[Dict[str, Any], bool]:
        """标准化单个 key 的统计数据"""
        changed = False
        if not isinstance(entry, dict):
            entry = {}
            changed = True

        normalized = dict(entry)
        created_time_default = time.time()

        if not isinstance(normalized.get("full_key_hash"), str):
            normalized["full_key_hash"] = str(normalized.get("full_key_hash") or "")
            changed = True
        if key_hash and not normalized.get("full_key_hash"):
            normalized["full_key_hash"] = key_hash
            changed = True

        success_count = _as_non_negative_int(normalized.get("success_count", 0))
        if success_count != normalized.get("success_count", 0):
            changed = True
        normalized["success_count"] = success_count

        failure_count = _as_non_negative_int(normalized.get("failure_count", 0))
        if failure_count != normalized.get("failure_count", 0):
            changed = True
        normalized["failure_count"] = failure_count

        last_call_time = _as_non_negative_float(normalized.get("last_call_time", 0))
        if last_call_time != normalized.get("last_call_time", 0):
            changed = True
        normalized["last_call_time"] = last_call_time

        created_time = _as_non_negative_float(normalized.get("created_time", created_time_default), created_time_default)
        if created_time != normalized.get("created_time", created_time_default):
            changed = True
        normalized["created_time"] = created_time

        model_counts, model_counts_changed = self._normalize_model_counts(normalized.get("model_counts", {}))
        normalized["model_counts"] = model_counts
        changed = changed or model_counts_changed

        daily_limit_total = _as_positive_int(normalized.get("daily_limit_total", DEFAULT_DAILY_LIMIT_TOTAL), DEFAULT_DAILY_LIMIT_TOTAL)
        if daily_limit_total != normalized.get("daily_limit_total"):
            changed = True
        normalized["daily_limit_total"] = daily_limit_total

        model_limits = self._normalize_model_limits(normalized.get("daily_limit_models", {}))
        if model_limits != normalized.get("daily_limit_models", {}):
            changed = True
        normalized["daily_limit_models"] = model_limits

        reset_time = _parse_datetime(normalized.get("next_reset_time"))
        if reset_time is None:
            reset_time = _get_next_utc_7am()
            changed = True
        normalized["next_reset_time"] = reset_time.isoformat()

        return normalized, changed

    def _reset_usage_only(self, stats: Dict[str, Any]):
        """重置调用计数但保留限额配置"""
        stats["success_count"] = 0
        stats["failure_count"] = 0
        stats["model_counts"] = {}
        stats["last_call_time"] = 0
        stats["next_reset_time"] = _get_next_utc_7am().isoformat()

    def _apply_daily_reset_if_needed(self, stats: Dict[str, Any]) -> bool:
        """根据 next_reset_time 执行每日重置"""
        reset_time = _parse_datetime(stats.get("next_reset_time"))
        if reset_time is None:
            stats["next_reset_time"] = _get_next_utc_7am().isoformat()
            return True

        now = datetime.now(timezone.utc)
        if now >= reset_time:
            self._reset_usage_only(stats)
            return True

        return False

    def _touch_entry(self, masked_key: str, key_hash: str = "") -> Dict[str, Any]:
        """
        获取并标准化某个 key 的统计 entry。
        如果不存在则创建。
        """
        existing = self._stats.get(masked_key)
        if existing is None:
            self._stats[masked_key] = self._build_default_entry(key_hash)
            self._dirty = True
            return self._stats[masked_key]

        normalized, changed = self._normalize_entry(existing, key_hash=key_hash)
        reset_changed = self._apply_daily_reset_if_needed(normalized)
        if changed or reset_changed:
            self._stats[masked_key] = normalized
            self._dirty = True
            return self._stats[masked_key]

        return existing

    def _normalize_all_entries(self) -> bool:
        """标准化并按需重置所有 entry"""
        changed = False
        for masked_key in list(self._stats.keys()):
            normalized, normalized_changed = self._normalize_entry(self._stats.get(masked_key))
            reset_changed = self._apply_daily_reset_if_needed(normalized)
            if normalized_changed or reset_changed:
                self._stats[masked_key] = normalized
                changed = True
        return changed

    @staticmethod
    def _build_model_views(model_counts: Dict[str, Dict[str, int]]) -> Tuple[Dict[str, int], Dict[str, Dict[str, int]]]:
        """
        构建两种模型统计视图：
        1) model_counts: 成功调用次数（用于限额展示）
        2) models: 成功/失败详情
        """
        model_counts_success: Dict[str, int] = {}
        models_detail: Dict[str, Dict[str, int]] = {}
        for model, detail in model_counts.items():
            ok = _as_non_negative_int(detail.get("ok", 0))
            fail = _as_non_negative_int(detail.get("fail", 0))
            model_counts_success[model] = ok
            models_detail[model] = {"ok": ok, "fail": fail}
        return model_counts_success, models_detail
    
    async def initialize(self):
        """初始化统计管理器"""
        if self._initialized:
            return
        await self._load_stats()
        self._initialized = True
        log.info(f"UnifiedStats initialized with {len(self._stats)} keys")
    
    async def _load_stats(self):
        """从存储加载统计数据"""
        try:
            adapter = await get_storage_adapter()
            data = await adapter.get_config("unified_stats", {})
            
            if isinstance(data, dict):
                self._stats = data

            if self._normalize_all_entries():
                self._dirty = True
                await self._save_stats(force=True)

            log.debug(f"Loaded unified stats for {len(self._stats)} keys")
        except Exception as e:
            log.error(f"Failed to load unified stats: {e}")
    
    async def _save_stats(self, force: bool = False):
        """保存统计数据到存储"""
        current_time = time.time()
        
        if not force and not self._dirty:
            return
        if not force and current_time - self._last_save_time < self._save_interval:
            return
        
        async with self._save_lock:
            try:
                adapter = await get_storage_adapter()
                await adapter.set_config("unified_stats", self._stats)
                self._dirty = False
                self._last_save_time = current_time
                log.debug(f"Saved unified stats for {len(self._stats)} keys")
            except Exception as e:
                log.error(f"Failed to save unified stats: {e}")
    
    async def record_call(
        self,
        api_key: str,
        model: str,
        success: bool,
    ):
        """
        记录 API 调用
        
        Args:
            api_key: 完整的 API 密钥
            model: 模型名称
            success: 是否成功
        """
        if not self._initialized:
            await self.initialize()

        masked = mask_key(api_key)
        key_hash = get_key_hash(api_key)

        stats = self._touch_entry(masked, key_hash=key_hash)

        # 释放本次调用对应的预占槽（若存在）
        self._release_one_pending(masked)

        if success:
            stats["success_count"] = stats.get("success_count", 0) + 1
        else:
            stats["failure_count"] = stats.get("failure_count", 0) + 1

        # 更新模型计数（区分成功和失败）
        model_counts = stats.get("model_counts", {})
        if model not in model_counts:
            model_counts[model] = {"ok": 0, "fail": 0}

        if success:
            model_counts[model]["ok"] = model_counts[model].get("ok", 0) + 1
        else:
            model_counts[model]["fail"] = model_counts[model].get("fail", 0) + 1
        stats["model_counts"] = model_counts

        # 更新最后调用时间
        stats["last_call_time"] = time.time()

        self._dirty = True

        log.debug(f"Recorded {'success' if success else 'failure'} call for key {masked}, model={model}")

        # 异步保存
        asyncio.create_task(self._save_stats())

    async def can_use_key_for_model(self, api_key: str, model: str) -> Dict[str, Any]:
        """
        判断密钥是否可以继续用于指定模型（仅按成功请求计数配额）

        规则：
        1. 总配额限制：success_count < daily_limit_total
        2. 模型配额限制：model_ok < daily_limit_models[model]（如果配置了该模型）
        """
        if not self._initialized:
            await self.initialize()

        masked = mask_key(api_key)
        key_hash = get_key_hash(api_key)
        stats = self._touch_entry(masked, key_hash=key_hash)

        success_count = _as_non_negative_int(stats.get("success_count", 0))
        total_limit = _as_positive_int(stats.get("daily_limit_total", DEFAULT_DAILY_LIMIT_TOTAL), DEFAULT_DAILY_LIMIT_TOTAL)
        model_limits = self._normalize_model_limits(stats.get("daily_limit_models", {}))
        model_limit = model_limits.get(model)

        model_data = stats.get("model_counts", {}).get(model, {})
        model_success = _as_non_negative_int(model_data.get("ok", 0)) if isinstance(model_data, dict) else 0

        total_remaining = max(total_limit - success_count, 0)
        model_remaining = None
        if model_limit is not None:
            model_remaining = max(model_limit - model_success, 0)

        if self._dirty:
            asyncio.create_task(self._save_stats())

        allowed = True
        reason = ""
        if success_count >= total_limit:
            allowed = False
            reason = "total_limit_reached"
        elif model_limit is not None and model_success >= model_limit:
            allowed = False
            reason = "model_limit_reached"

        effective_limit = total_limit if model_limit is None else min(total_limit, model_limit)

        return {
            "allowed": allowed,
            "reason": reason,
            "masked_key": masked,
            "next_reset_time": stats.get("next_reset_time"),
            "success_count": success_count,
            "failure_count": _as_non_negative_int(stats.get("failure_count", 0)),
            "total_limit": total_limit,
            "total_remaining": total_remaining,
            "model": model,
            "model_success_count": model_success,
            "model_limit": model_limit,
            "model_remaining": model_remaining,
            "effective_limit": effective_limit,
        }

    async def reserve_key_for_model(self, api_key: str, model: str) -> Dict[str, Any]:
        """原子地"预占"一个配额槽，消除选 key 与记账之间的 TOCTOU 超计。

        在锁内用 (success_count + pending) 对比限额：未满则 pending+1 并返回 allowed=True，
        已满返回 allowed=False（不预占）。返回结构与 can_use_key_for_model 一致，便于复用。
        每次成功预占都必须由 record_call（成功/失败均可）或 release_reservation 释放。
        """
        if not self._initialized:
            await self.initialize()

        masked = mask_key(api_key)
        key_hash = get_key_hash(api_key)

        async with self._reserve_lock:
            stats = self._touch_entry(masked, key_hash=key_hash)
            success_count = _as_non_negative_int(stats.get("success_count", 0))
            total_limit = _as_positive_int(
                stats.get("daily_limit_total", DEFAULT_DAILY_LIMIT_TOTAL),
                DEFAULT_DAILY_LIMIT_TOTAL,
            )
            model_limits = self._normalize_model_limits(stats.get("daily_limit_models", {}))
            model_limit = model_limits.get(model)
            model_data = stats.get("model_counts", {}).get(model, {})
            model_success = (
                _as_non_negative_int(model_data.get("ok", 0))
                if isinstance(model_data, dict) else 0
            )
            pending = self._live_pending(masked)

            allowed = True
            reason = ""
            # 关键：把 in-flight 的 pending 计入，N 个并发不会同时挤过最后一个槽
            if success_count + pending >= total_limit:
                allowed = False
                reason = "total_limit_reached"
            elif model_limit is not None and model_success + pending >= model_limit:
                allowed = False
                reason = "model_limit_reached"

            if allowed:
                self._pending.setdefault(masked, []).append(time.time() + self._pending_ttl)

            total_remaining = max(total_limit - success_count - pending, 0)
            model_remaining = None
            if model_limit is not None:
                model_remaining = max(model_limit - model_success - pending, 0)
            effective_limit = total_limit if model_limit is None else min(total_limit, model_limit)

            return {
                "allowed": allowed,
                "reason": reason,
                "masked_key": masked,
                "next_reset_time": stats.get("next_reset_time"),
                "success_count": success_count,
                "failure_count": _as_non_negative_int(stats.get("failure_count", 0)),
                "total_limit": total_limit,
                "total_remaining": total_remaining,
                "model": model,
                "model_success_count": model_success,
                "model_limit": model_limit,
                "model_remaining": model_remaining,
                "effective_limit": effective_limit,
            }

    def release_reservation(self, api_key: str) -> None:
        """释放一个预占槽（用于预占后未走到 record_call 的兜底路径）。"""
        self._release_one_pending(mask_key(api_key))

    async def update_daily_limits(
        self,
        masked_key: str,
        total_limit: Optional[int] = None,
        model_limits: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        更新指定密钥的限额配置

        Args:
            masked_key: 脱敏密钥
            total_limit: 每日总成功请求上限（可选）
            model_limits: 每日模型成功请求上限映射（可选，传入即覆盖）
        """
        if not self._initialized:
            await self.initialize()

        if masked_key not in self._stats:
            return None

        stats = self._touch_entry(masked_key)
        changed = False

        if total_limit is not None:
            parsed_total = _as_positive_int(total_limit, 0)
            if parsed_total <= 0:
                raise ValueError("total_limit must be a positive integer")
            if stats.get("daily_limit_total") != parsed_total:
                stats["daily_limit_total"] = parsed_total
                changed = True

        if model_limits is not None:
            normalized_limits = self._normalize_model_limits(model_limits)
            if stats.get("daily_limit_models", {}) != normalized_limits:
                stats["daily_limit_models"] = normalized_limits
                changed = True

        if changed:
            self._dirty = True
            await self._save_stats(force=True)

        return {
            "masked_key": masked_key,
            "daily_limit_total": stats.get("daily_limit_total", DEFAULT_DAILY_LIMIT_TOTAL),
            "daily_limit_models": stats.get("daily_limit_models", {}),
            "next_reset_time": stats.get("next_reset_time"),
        }

    async def get_stats_for_key(self, masked_key: str) -> Dict[str, Any]:
        """获取单个密钥的统计信息"""
        if not self._initialized:
            await self.initialize()

        stats = self._touch_entry(masked_key) if masked_key in self._stats else self._build_default_entry("")
        model_counts_success, models_detail = self._build_model_views(stats.get("model_counts", {}))

        if self._dirty:
            await self._save_stats()

        return {
            "masked_key": masked_key,
            "success_count": stats.get("success_count", 0),
            "failure_count": stats.get("failure_count", 0),
            "total_calls": stats.get("success_count", 0) + stats.get("failure_count", 0),
            "model_counts": model_counts_success,
            "models": models_detail,
            "last_call_time": stats.get("last_call_time", 0),
            "daily_limit_total": stats.get("daily_limit_total", DEFAULT_DAILY_LIMIT_TOTAL),
            "daily_limit_models": stats.get("daily_limit_models", {}),
            "next_reset_time": stats.get("next_reset_time"),
        }

    async def get_all_stats(self, valid_keys: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        获取所有统计信息
        
        Args:
            valid_keys: 有效密钥列表（完整密钥），如果提供则只返回这些密钥的统计
        
        Returns:
            统计信息字典
        """
        if not self._initialized:
            await self.initialize()

        if self._normalize_all_entries():
            self._dirty = True

        # 如果提供了有效密钥列表，构建脱敏密钥集合
        valid_masked_keys = None
        if valid_keys is not None:
            valid_masked_keys = {mask_key(k) for k in valid_keys}

        result = {
            "keys": {},
            "total": {
                "success": 0,
                "failure": 0,
                "total_calls": 0,
            },
            "models": {},
        }

        for masked_key, stats in self._stats.items():
            # 如果提供了有效密钥列表，只统计有效密钥
            if valid_masked_keys is not None and masked_key not in valid_masked_keys:
                continue

            success = stats.get("success_count", 0)
            failure = stats.get("failure_count", 0)
            total = success + failure

            model_counts_display, models_detail = self._build_model_views(stats.get("model_counts", {}))

            result["keys"][masked_key] = {
                "ok": success,
                "fail": failure,
                "total": total,
                "model_counts": model_counts_display,
                "models": models_detail,
                "daily_limit_total": stats.get("daily_limit_total", DEFAULT_DAILY_LIMIT_TOTAL),
                "daily_limit_models": stats.get("daily_limit_models", {}),
                "next_reset_time": stats.get("next_reset_time"),
                "last_call_time": stats.get("last_call_time", 0),
            }

            # 累加总数
            result["total"]["success"] += success
            result["total"]["failure"] += failure
            result["total"]["total_calls"] += total

            # 累加模型统计
            for model, detail in models_detail.items():
                if model not in result["models"]:
                    result["models"][model] = {"ok": 0, "fail": 0}
                result["models"][model]["ok"] += detail["ok"]
                result["models"][model]["fail"] += detail["fail"]

        if self._dirty:
            await self._save_stats()

        return result

    async def delete_stats_for_key(self, api_key: str) -> bool:
        """
        删除指定密钥的统计数据
        
        Args:
            api_key: 完整的 API 密钥
        
        Returns:
            是否成功删除
        """
        if not self._initialized:
            await self.initialize()

        masked = mask_key(api_key)

        if masked in self._stats:
            del self._stats[masked]
            self._dirty = True
            await self._save_stats(force=True)
            log.info(f"Deleted stats for key {masked}")
            return True

        return False

    async def delete_stats_for_masked_key(self, masked_key: str) -> bool:
        """
        删除指定脱敏密钥的统计数据
        
        Args:
            masked_key: 脱敏后的密钥
        
        Returns:
            是否成功删除
        """
        if not self._initialized:
            await self.initialize()

        if masked_key in self._stats:
            del self._stats[masked_key]
            self._dirty = True
            await self._save_stats(force=True)
            log.info(f"Deleted stats for masked key {masked_key}")
            return True

        return False

    async def cleanup_invalid_keys(self, valid_keys: List[str]) -> int:
        """
        清理无效密钥的统计数据
        
        Args:
            valid_keys: 有效密钥列表（完整密钥）
        
        Returns:
            删除的统计数量
        """
        if not self._initialized:
            await self.initialize()

        valid_masked_keys = {mask_key(k) for k in valid_keys}

        # 找出需要删除的统计
        to_delete = [k for k in self._stats.keys() if k not in valid_masked_keys]

        for masked_key in to_delete:
            del self._stats[masked_key]

        if to_delete:
            self._dirty = True
            await self._save_stats(force=True)
            log.info(f"Cleaned up stats for {len(to_delete)} invalid keys: {to_delete}")

        return len(to_delete)

    async def reset_stats(self, masked_key: Optional[str] = None):
        """
        重置统计数据
        
        Args:
            masked_key: 脱敏密钥，如果为 None 则重置所有
        """
        if not self._initialized:
            await self.initialize()

        if masked_key is not None:
            if masked_key in self._stats:
                stats = self._touch_entry(masked_key)
                self._reset_usage_only(stats)
                log.info(f"Reset stats for key {masked_key}")
        else:
            for key in self._stats:
                stats = self._touch_entry(key)
                self._reset_usage_only(stats)
            log.info("Reset all stats")

        self._dirty = True
        await self._save_stats(force=True)

    async def ensure_keys_exist(self, keys: List[str]):
        """
        确保指定的密钥在统计中存在（即使没有调用记录）
        
        Args:
            keys: 完整密钥列表
        """
        if not self._initialized:
            await self.initialize()

        for key in keys:
            masked = mask_key(key)
            self._touch_entry(masked, key_hash=get_key_hash(key))

        if self._dirty:
            await self._save_stats()


# 全局实例
_unified_stats: Optional[UnifiedStats] = None


async def get_unified_stats() -> UnifiedStats:
    """获取全局统一统计实例"""
    global _unified_stats
    if _unified_stats is None:
        _unified_stats = UnifiedStats()
        await _unified_stats.initialize()
    return _unified_stats


async def flush_unified_stats() -> None:
    """进程退出前强制落盘统计（30s 节流窗口内的改动不丢）。仅在已实例化时执行。"""
    if _unified_stats is not None and _unified_stats._dirty:
        await _unified_stats._save_stats(force=True)

"""
性能追踪模块
支持 10 万条记录的分片存储和高效查询

架构设计:
- 100 个分片 × 每片 1000 条 = 10 万条最大容量
- 使用 perf_traces_{0-99} 的 hash-key 分片存储
- 支持分页查询和模型筛选
- 链路追踪支持各阶段耗时统计
"""
import time
import asyncio
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field

from log import log


def _to_non_negative_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
        return parsed if parsed >= 0 else default
    except (TypeError, ValueError):
        return default


def _extract_usage_tokens(raw: Dict[str, Any]) -> Dict[str, int]:
    prompt_tokens = _to_non_negative_int(raw.get("prompt_tokens", 0), 0)
    completion_tokens = _to_non_negative_int(raw.get("completion_tokens", 0), 0)
    cached_tokens = _to_non_negative_int(raw.get("cached_tokens", 0), 0)
    # Gateway prompt-caching token-creation buckets (ephemeral 5m / 1h).
    cache_creation_5m = _to_non_negative_int(raw.get("cache_creation_5m_tokens", 0), 0)
    cache_creation_1h = _to_non_negative_int(raw.get("cache_creation_1h_tokens", 0), 0)
    details = raw.get("prompt_tokens_details")
    if isinstance(details, dict):
        if cached_tokens == 0:
            cached_tokens = _to_non_negative_int(details.get("cached_tokens", 0), 0)
        if not cache_creation_5m and not cache_creation_1h:
            creation = details.get("cache_creation")
            if isinstance(creation, dict):
                cache_creation_5m = _to_non_negative_int(
                    creation.get("ephemeral_5m_input_tokens", 0), 0
                )
                cache_creation_1h = _to_non_negative_int(
                    creation.get("ephemeral_1h_input_tokens", 0), 0
                )
    total_tokens = _to_non_negative_int(
        raw.get("total_tokens", prompt_tokens + completion_tokens),
        prompt_tokens + completion_tokens,
    )
    min_total = prompt_tokens + completion_tokens
    if total_tokens < min_total:
        total_tokens = min_total
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": cached_tokens,
        "cache_creation_5m_tokens": cache_creation_5m,
        "cache_creation_1h_tokens": cache_creation_1h,
        "total_tokens": total_tokens,
    }


def _normalize_usage_windows(active_windows: Optional[Dict[str, Any]]) -> Dict[str, tuple]:
    normalized: Dict[str, tuple] = {}
    if not active_windows:
        return normalized
    for key, window in active_windows.items():
        if not isinstance(window, (tuple, list)) or len(window) != 2:
            continue
        try:
            start_ts = float(window[0])
            end_ts = float(window[1])
        except (TypeError, ValueError):
            continue
        normalized[str(key)] = (start_ts, end_ts)
    return normalized


def _trace_usage_timestamp(trace: Dict[str, Any]) -> Optional[float]:
    try:
        start_ts = float(trace.get("start_time", 0))
    except (TypeError, ValueError):
        return None

    timestamps = trace.get("timestamps")
    if isinstance(timestamps, dict) and timestamps.get("response_complete") is not None:
        try:
            complete_ms = float(timestamps.get("response_complete"))
        except (TypeError, ValueError):
            complete_ms = None
        if complete_ms is not None and complete_ms >= 0:
            return start_ts + complete_ms / 1000

    return start_ts


@dataclass
class RequestTrace:
    """单个请求的追踪数据"""
    trace_id: str
    model: str
    start_time: float  # Unix timestamp
    timestamps: Dict[str, float] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    completion_tokens: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    cache_creation_5m_tokens: int = 0
    cache_creation_1h_tokens: int = 0
    total_tokens: int = 0
    
    # Key and account tracking
    key_index: int = -1  # 使用的密钥索引，-1 表示未设置
    key_masked: str = ""  # 脱敏后的密钥（如 "sk-ab...xy"）
    account_email: str = ""  # 归属账户邮箱
    
    # 内部计时基准（用于高精度计时）
    _base_time: float = field(default=0.0, repr=False)
    
    def mark(self, stage: str):
        """标记阶段完成时间（相对时间，毫秒）"""
        if not self._base_time:
            self._base_time = time.perf_counter()
            self.timestamps[stage] = 0.0
        else:
            self.timestamps[stage] = (time.perf_counter() - self._base_time) * 1000
    
    def get_stage_durations(self) -> Dict[str, float]:
        """计算各阶段耗时（毫秒）"""
        stages = [
            "request_received",
            "auth_complete", 
            "preprocessing_complete",
            "key_selection_complete",
            "upstream_request_sent",
            "upstream_first_byte",
            "upstream_response_complete",
            "conversion_complete",
            "first_chunk_sent",
            "response_complete"
        ]
        durations = {}
        prev_time = None
        for stage in stages:
            if stage in self.timestamps:
                if prev_time is not None:
                    durations[stage] = self.timestamps[stage] - prev_time
                else:
                    durations[stage] = 0.0
                prev_time = self.timestamps[stage]
        return durations
    
    def get_metrics(self) -> Dict[str, float]:
        """获取关键指标（毫秒）"""
        ts = self.timestamps
        metrics = {}
        
        # TTFB: 从请求到上游首字节
        if "request_received" in ts and "upstream_first_byte" in ts:
            metrics["ttfb"] = ts["upstream_first_byte"] - ts["request_received"]
        
        # TTFT: 从请求到首块发送
        if "request_received" in ts and "first_chunk_sent" in ts:
            metrics["ttft"] = ts["first_chunk_sent"] - ts["request_received"]
        
        # 总延迟
        if "request_received" in ts and "response_complete" in ts:
            metrics["total_latency"] = ts["response_complete"] - ts["request_received"]
            
            # TPS: tokens per second
            if self.completion_tokens > 0 and metrics["total_latency"] > 0:
                metrics["tps"] = self.completion_tokens / (metrics["total_latency"] / 1000)
        
        return metrics
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（用于存储）"""
        return {
            "trace_id": self.trace_id,
            "model": self.model,
            "start_time": self.start_time,
            "timestamps": self.timestamps,
            "metadata": self.metadata,
            "completion_tokens": self.completion_tokens,
            "prompt_tokens": self.prompt_tokens,
            "cached_tokens": self.cached_tokens,
            "cache_creation_5m_tokens": self.cache_creation_5m_tokens,
            "cache_creation_1h_tokens": self.cache_creation_1h_tokens,
            "total_tokens": self.total_tokens,
            "key_index": self.key_index,
            "key_masked": self.key_masked,
            "account_email": self.account_email
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RequestTrace":
        """从字典创建（用于加载）"""
        trace = cls(
            trace_id=data.get("trace_id", ""),
            model=data.get("model", ""),
            start_time=data.get("start_time", 0.0)
        )
        trace.timestamps = data.get("timestamps", {})
        trace.metadata = data.get("metadata", {})
        usage = _extract_usage_tokens(data)
        trace.prompt_tokens = usage["prompt_tokens"]
        trace.completion_tokens = usage["completion_tokens"]
        trace.cached_tokens = usage["cached_tokens"]
        trace.cache_creation_5m_tokens = usage["cache_creation_5m_tokens"]
        trace.cache_creation_1h_tokens = usage["cache_creation_1h_tokens"]
        trace.total_tokens = usage["total_tokens"]
        trace.key_index = data.get("key_index", -1)
        trace.key_masked = data.get("key_masked", "")
        trace.account_email = data.get("account_email", "")
        return trace


class ShardManager:
    """分片管理器"""
    MAX_SHARDS = 100          # 最大分片数
    RECORDS_PER_SHARD = 1000  # 每分片记录数
    MAX_TOTAL_RECORDS = 100000  # 总容量 10 万
    
    def __init__(self):
        self.current_write_shard = 0
        self.shard_counts: Dict[int, int] = {}
        self.total_records = 0
    
    def get_next_write_shard(self) -> int:
        """获取下一个可写入的分片"""
        count = self.shard_counts.get(self.current_write_shard, 0)
        if count >= self.RECORDS_PER_SHARD:
            # 当前分片已满，轮转到下一个
            self.current_write_shard = (self.current_write_shard + 1) % self.MAX_SHARDS
            log.debug(f"Shard rotated to {self.current_write_shard}")
        return self.current_write_shard
    
    def increment_shard_count(self, shard_idx: int):
        """增加分片计数"""
        old_count = self.shard_counts.get(shard_idx, 0)
        self.shard_counts[shard_idx] = old_count + 1
        
        # 如果超过限制，说明是覆盖旧数据
        if old_count >= self.RECORDS_PER_SHARD:
            self.shard_counts[shard_idx] = self.RECORDS_PER_SHARD
    
    def get_total_records(self) -> int:
        """获取总记录数"""
        return sum(self.shard_counts.values())
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "current_write_shard": self.current_write_shard,
            "shard_counts": {str(k): v for k, v in self.shard_counts.items()}
        }
    
    def from_dict(self, data: Dict[str, Any]):
        """从字典加载"""
        self.current_write_shard = data.get("current_write_shard", 0)
        shard_counts = data.get("shard_counts", {})
        self.shard_counts = {int(k): v for k, v in shard_counts.items()}


class PerformanceTracker:
    """
    性能追踪管理器
    
    提供:
    - 请求追踪的开始/结束
    - 分片持久化存储
    - 分页查询
    - 统计聚合
    """
    
    def __init__(self):
        self.active_traces: Dict[str, RequestTrace] = {}
        self.shard_manager = ShardManager()
        self._initialized = False
        self._lock = asyncio.Lock()
        self._save_lock = asyncio.Lock()
        self._stats_cache: Optional[Dict[str, Any]] = None
        self._stats_cache_time: float = 0
        self._stats_cache_ttl: float = 10.0  # 统计缓存 10 秒
    
    async def initialize(self):
        """初始化，从存储加载元数据"""
        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return
            try:
                from ..storage.storage_adapter import get_storage_adapter
                adapter = await get_storage_adapter()
                meta = await adapter.get_perf("perf_meta", None)
                if meta and isinstance(meta, dict):
                    self.shard_manager.from_dict(meta)
                    log.info(f"Performance tracker initialized: {self.shard_manager.get_total_records()} records in {len(self.shard_manager.shard_counts)} shards")
                else:
                    log.info("Performance tracker initialized: no existing data")
            except Exception as e:
                log.warning(f"Failed to load performance tracker meta: {e}")
            self._initialized = True

    async def _load_all_traces(self) -> List[Dict[str, Any]]:
        """Load trace records from every fixed shard."""
        from ..storage.storage_adapter import get_storage_adapter
        adapter = await get_storage_adapter()

        all_traces: List[Dict[str, Any]] = []
        for i in range(ShardManager.MAX_SHARDS):
            shard_key = f"perf_traces_{i}"
            try:
                shard_data = await adapter.get_perf(shard_key, None)
                if shard_data and isinstance(shard_data, list):
                    all_traces.extend(t for t in shard_data if isinstance(t, dict))
            except Exception as e:
                log.warning(f"Failed to load shard {i}: {e}")
        return all_traces

    async def get_usage_summary(self, active_windows: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Aggregate request traces into the usage log-summary shape."""
        all_traces = await self._load_all_traces()
        usage_windows = _normalize_usage_windows(active_windows)
        summary: Dict[str, Any] = {
            "models": {},
            "keys": {},
            "total": {"ok": 0, "fail": 0},
        }

        for trace in all_traces:
            model = str(trace.get("model") or "unknown").strip() or "unknown"
            key_masked = str(trace.get("key_masked") or "").strip()
            if not key_masked:
                key_index = _to_non_negative_int(trace.get("key_index", -1), -1)
                if key_index >= 0:
                    key_masked = f"Key #{key_index}"

            if usage_windows and key_masked in usage_windows:
                usage_ts = _trace_usage_timestamp(trace)
                if usage_ts is None:
                    continue
                start_ts, end_ts = usage_windows[key_masked]
                if usage_ts < start_ts or usage_ts >= end_ts:
                    continue

            metadata = trace.get("metadata") if isinstance(trace.get("metadata"), dict) else {}
            trace_success = metadata.get("usage_success")
            ok_count = 1 if trace_success is not False else 0
            fail_count = 0 if ok_count else 1

            summary["total"]["ok"] += ok_count
            summary["total"]["fail"] += fail_count

            model_entry = summary["models"].setdefault(model, {"ok": 0, "fail": 0})
            model_entry["ok"] += ok_count
            model_entry["fail"] += fail_count

            if key_masked and key_masked != "-":
                key_entry = summary["keys"].setdefault(
                    key_masked,
                    {
                        "ok": 0,
                        "fail": 0,
                        "models": {},
                        "model_counts": {},
                    },
                )
                key_entry["ok"] += ok_count
                key_entry["fail"] += fail_count
                key_model = key_entry["models"].setdefault(model, {"ok": 0, "fail": 0})
                key_model["ok"] += ok_count
                key_model["fail"] += fail_count
                if ok_count:
                    key_entry["model_counts"][model] = key_entry["model_counts"].get(model, 0) + ok_count

        return summary
    
    def start_trace(self, trace_id: str, model: str) -> RequestTrace:
        """开始追踪"""
        trace = RequestTrace(
            trace_id=trace_id,
            model=model,
            start_time=time.time()
        )
        trace.mark("request_received")
        self.active_traces[trace_id] = trace
        log.debug(f"Started trace {trace_id} for model {model}")
        return trace
    
    def get_active_trace(self, trace_id: str) -> Optional[RequestTrace]:
        """获取活跃的追踪"""
        return self.active_traces.get(trace_id)
    
    async def end_trace(
        self,
        trace_id: str,
        completion_tokens: int = 0,
        prompt_tokens: int = 0,
        cached_tokens: int = 0,
        total_tokens: Optional[int] = None,
        cache_creation_5m_tokens: int = 0,
        cache_creation_1h_tokens: int = 0,
        success: Optional[bool] = None,
    ):
        """结束追踪并持久化"""
        if trace_id not in self.active_traces:
            log.warning(f"Trace {trace_id} not found in active traces")
            return

        trace = self.active_traces.pop(trace_id)
        trace.prompt_tokens = _to_non_negative_int(prompt_tokens, 0)
        trace.completion_tokens = _to_non_negative_int(completion_tokens, 0)
        trace.cached_tokens = _to_non_negative_int(cached_tokens, 0)
        trace.cache_creation_5m_tokens = _to_non_negative_int(cache_creation_5m_tokens, 0)
        trace.cache_creation_1h_tokens = _to_non_negative_int(cache_creation_1h_tokens, 0)
        fallback_total = trace.prompt_tokens + trace.completion_tokens
        if total_tokens is None:
            trace.total_tokens = fallback_total
        else:
            parsed_total = _to_non_negative_int(total_tokens, fallback_total)
            trace.total_tokens = max(parsed_total, fallback_total)
        if success is not None:
            trace.metadata["usage_success"] = bool(success)
        
        # 确保有结束时间
        if "response_complete" not in trace.timestamps:
            trace.mark("response_complete")
        
        # 记录指标
        metrics = trace.get_metrics()
        log.info(f"Trace {trace_id[:8]}... completed: model={trace.model}, "
                 f"ttfb={metrics.get('ttfb', 0):.0f}ms, "
                 f"ttft={metrics.get('ttft', 0):.0f}ms, "
                 f"total={metrics.get('total_latency', 0):.0f}ms, "
                 f"tps={metrics.get('tps', 0):.1f}")
        
        # 异步持久化
        asyncio.create_task(self._save_trace(trace))
        
        # 清除统计缓存
        self._stats_cache = None
    
    async def _save_trace(self, trace: RequestTrace):
        """保存追踪记录到分片"""
        async with self._save_lock:
            try:
                from ..storage.storage_adapter import get_storage_adapter
                adapter = await get_storage_adapter()
                
                shard_idx = self.shard_manager.get_next_write_shard()
                shard_key = f"perf_traces_{shard_idx}"
                
                # 加载当前分片
                shard_data = await adapter.get_perf(shard_key, None)
                if not isinstance(shard_data, list):
                    shard_data = []
                
                # 添加新记录
                shard_data.append(trace.to_dict())
                
                # 如果超过限制，移除最旧的
                if len(shard_data) > ShardManager.RECORDS_PER_SHARD:
                    shard_data = shard_data[-ShardManager.RECORDS_PER_SHARD:]
                
                # 保存分片
                await adapter.set_perf(shard_key, shard_data)
                self.shard_manager.shard_counts[shard_idx] = len(shard_data)
                
                # 更新元数据
                await adapter.set_perf("perf_meta", self.shard_manager.to_dict())
                
                log.debug(f"Saved trace to shard {shard_idx}, shard size: {len(shard_data)}")
            except Exception as e:
                log.error(f"Failed to save trace: {e}")
    
    async def get_traces_paginated(
        self,
        page: int = 1,
        page_size: int = 20,
        model: Optional[str] = None,
        key: Optional[str] = None,
        search: Optional[str] = None,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        分页查询追踪记录
        
        Args:
            page: 页码（从 1 开始）
            page_size: 每页记录数
            model: 模型筛选
            search: 搜索 trace_id
            start_time: 开始时间筛选
            end_time: 结束时间筛选
        
        Returns:
            分页结果字典
        """
        all_traces = await self._load_all_traces()
        
        # 过滤
        if model:
            all_traces = [t for t in all_traces if t.get("model") == model]

        if key:
            key_lower = key.lower()
            all_traces = [
                t for t in all_traces
                if key_lower in str(t.get("key_masked", "")).lower()
            ]
        
        if search:
            search_lower = search.lower()
            all_traces = [t for t in all_traces if search_lower in t.get("trace_id", "").lower()]
        
        if start_time:
            all_traces = [t for t in all_traces if t.get("start_time", 0) >= start_time]
        
        if end_time:
            all_traces = [t for t in all_traces if t.get("start_time", 0) <= end_time]
        
        # 按时间倒序
        all_traces.sort(key=lambda x: x.get("start_time", 0), reverse=True)
        
        # 分页
        total = len(all_traces)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(page, total_pages))
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        
        # 为每条记录添加计算的指标
        page_traces = []
        for t in all_traces[start_idx:end_idx]:
            trace_obj = RequestTrace.from_dict(t)
            metrics = trace_obj.get_metrics()
            durations = trace_obj.get_stage_durations()
            usage = {
                "prompt_tokens": trace_obj.prompt_tokens,
                "completion_tokens": trace_obj.completion_tokens,
                "cached_tokens": trace_obj.cached_tokens,
                "cache_creation_5m_tokens": trace_obj.cache_creation_5m_tokens,
                "cache_creation_1h_tokens": trace_obj.cache_creation_1h_tokens,
                "total_tokens": trace_obj.total_tokens,
            }
            page_traces.append({
                **t,
                "prompt_tokens": trace_obj.prompt_tokens,
                "completion_tokens": trace_obj.completion_tokens,
                "cached_tokens": trace_obj.cached_tokens,
                "cache_creation_5m_tokens": trace_obj.cache_creation_5m_tokens,
                "cache_creation_1h_tokens": trace_obj.cache_creation_1h_tokens,
                "total_tokens": trace_obj.total_tokens,
                "usage": usage,
                "metrics": metrics,
                "durations": durations
            })
        
        return {
            "traces": page_traces,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages
        }
    
    async def get_trace_by_id(self, trace_id: str) -> Optional[Dict[str, Any]]:
        """获取单条追踪详情"""
        from ..storage.storage_adapter import get_storage_adapter
        adapter = await get_storage_adapter()
        
        for i in range(ShardManager.MAX_SHARDS):
            shard_key = f"perf_traces_{i}"
            try:
                shard_data = await adapter.get_perf(shard_key, None)
                if shard_data and isinstance(shard_data, list):
                    for t in shard_data:
                        if t.get("trace_id") == trace_id:
                            trace_obj = RequestTrace.from_dict(t)
                            usage = {
                                "prompt_tokens": trace_obj.prompt_tokens,
                                "completion_tokens": trace_obj.completion_tokens,
                                "cached_tokens": trace_obj.cached_tokens,
                                "cache_creation_5m_tokens": trace_obj.cache_creation_5m_tokens,
                                "cache_creation_1h_tokens": trace_obj.cache_creation_1h_tokens,
                                "total_tokens": trace_obj.total_tokens,
                            }
                            return {
                                **t,
                                "prompt_tokens": trace_obj.prompt_tokens,
                                "completion_tokens": trace_obj.completion_tokens,
                                "cached_tokens": trace_obj.cached_tokens,
                                "cache_creation_5m_tokens": trace_obj.cache_creation_5m_tokens,
                                "cache_creation_1h_tokens": trace_obj.cache_creation_1h_tokens,
                                "total_tokens": trace_obj.total_tokens,
                                "usage": usage,
                                "metrics": trace_obj.get_metrics(),
                                "durations": trace_obj.get_stage_durations()
                            }
            except Exception:
                pass
        
        return None
    
    async def get_stats(self, model: Optional[str] = None, use_cache: bool = True) -> Dict[str, Any]:
        """
        获取聚合统计
        
        Args:
            model: 模型筛选
            use_cache: 是否使用缓存
        
        Returns:
            统计数据字典
        """
        # 检查缓存
        cache_key = f"stats_{model or 'all'}"
        if use_cache and self._stats_cache and time.time() - self._stats_cache_time < self._stats_cache_ttl:
            if cache_key in self._stats_cache:
                return self._stats_cache[cache_key]
        
        all_traces = await self._load_all_traces()
        
        # 模型筛选
        if model:
            all_traces = [t for t in all_traces if t.get("model") == model]
        
        def to_bool(value: Any) -> bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            if isinstance(value, (int, float)):
                return value != 0
            return False

        if not all_traces:
            return {
                "count": 0,
                "ttfb": {"avg": 0, "p50": 0, "p95": 0, "p99": 0},
                "ttft": {"avg": 0, "p50": 0, "p95": 0, "p99": 0},
                "latency": {"avg": 0, "p50": 0, "p95": 0, "p99": 0},
                "tps": {"avg": 0, "p50": 0, "p95": 0},
                "tokens": {
                    "prompt_total": 0,
                    "completion_total": 0,
                    "cached_total": 0,
                    "cache_creation_5m_total": 0,
                    "cache_creation_1h_total": 0,
                    "total": 0,
                    "avg_prompt": 0,
                    "avg_completion": 0,
                    "avg_cached": 0,
                    "avg_cache_creation_5m": 0,
                    "avg_cache_creation_1h": 0,
                    "avg_total": 0,
                    "requests_with_usage": 0,
                },
                "stream": {
                    "total": 0,
                    "real": 0,
                    "fake": 0,
                    "non_stream": 0,
                    "keepalive_total": 0,
                    "keepalive_avg_per_real": 0,
                    "bootstrap_retry_total": 0,
                    "bootstrap_retry_requests": 0,
                    "bootstrap_retry_rate": 0,
                    "bootstrap_failure_count": 0,
                    "bootstrap_failure_rate": 0
                },
                "models": [],
                "time_range": {"start": 0, "end": 0}
            }
        
        # 计算各指标
        ttfbs = []
        ttfts = []
        latencies = []
        tps_list = []
        models_set = set()
        stream_real_count = 0
        stream_fake_count = 0
        stream_non_count = 0
        stream_keepalive_total = 0
        stream_bootstrap_retry_total = 0
        stream_bootstrap_retry_requests = 0
        stream_bootstrap_failure_count = 0
        prompt_tokens_total = 0
        completion_tokens_total = 0
        cached_tokens_total = 0
        cache_creation_5m_total = 0
        cache_creation_1h_total = 0
        total_tokens_total = 0
        requests_with_usage = 0

        for t in all_traces:
            ts = t.get("timestamps", {})
            usage = _extract_usage_tokens(t)
            prompt_tokens = usage["prompt_tokens"]
            completion_tokens = usage["completion_tokens"]
            cached_tokens = usage["cached_tokens"]
            cache_creation_5m = usage["cache_creation_5m_tokens"]
            cache_creation_1h = usage["cache_creation_1h_tokens"]
            total_tokens = usage["total_tokens"]
            has_usage = (
                prompt_tokens > 0
                or completion_tokens > 0
                or cached_tokens > 0
                or cache_creation_5m > 0
                or cache_creation_1h > 0
                or total_tokens > 0
            )
            models_set.add(t.get("model", "unknown"))
            metadata = t.get("metadata") or {}
            stream_mode = str(metadata.get("stream_mode", "none")).lower()

            prompt_tokens_total += prompt_tokens
            completion_tokens_total += completion_tokens
            cached_tokens_total += cached_tokens
            cache_creation_5m_total += cache_creation_5m
            cache_creation_1h_total += cache_creation_1h
            total_tokens_total += total_tokens
            if has_usage:
                requests_with_usage += 1

            if stream_mode == "real":
                stream_real_count += 1
                keepalive_count = _to_non_negative_int(metadata.get("stream_keepalive_count", 0), 0)
                retries_used = _to_non_negative_int(metadata.get("stream_bootstrap_retries_used", 0), 0)
                stream_keepalive_total += keepalive_count
                stream_bootstrap_retry_total += retries_used
                if retries_used > 0:
                    stream_bootstrap_retry_requests += 1
                if to_bool(metadata.get("stream_bootstrap_failed", False)):
                    stream_bootstrap_failure_count += 1
            elif stream_mode == "fake":
                stream_fake_count += 1
            else:
                stream_non_count += 1
            
            # TTFB
            if "request_received" in ts and "upstream_first_byte" in ts:
                ttfb = ts["upstream_first_byte"] - ts["request_received"]
                if ttfb >= 0:
                    ttfbs.append(ttfb)
            
            # TTFT
            if "request_received" in ts and "first_chunk_sent" in ts:
                ttft = ts["first_chunk_sent"] - ts["request_received"]
                if ttft >= 0:
                    ttfts.append(ttft)
            
            # Total latency
            if "request_received" in ts and "response_complete" in ts:
                lat = ts["response_complete"] - ts["request_received"]
                if lat >= 0:
                    latencies.append(lat)
                    # TPS
                    if completion_tokens > 0 and lat > 0:
                        tps_list.append(completion_tokens / (lat / 1000))
        
        def percentile(data: List[float], p: float) -> float:
            if not data:
                return 0.0
            sorted_data = sorted(data)
            idx = int(len(sorted_data) * p / 100)
            return sorted_data[min(idx, len(sorted_data) - 1)]
        
        def avg(data: List[float]) -> float:
            return sum(data) / len(data) if data else 0.0
        
        # 时间范围
        start_times = [t.get("start_time", 0) for t in all_traces]
        
        stats = {
            "count": len(all_traces),
            "ttfb": {
                "avg": round(avg(ttfbs), 1),
                "p50": round(percentile(ttfbs, 50), 1),
                "p95": round(percentile(ttfbs, 95), 1),
                "p99": round(percentile(ttfbs, 99), 1)
            },
            "ttft": {
                "avg": round(avg(ttfts), 1),
                "p50": round(percentile(ttfts, 50), 1),
                "p95": round(percentile(ttfts, 95), 1),
                "p99": round(percentile(ttfts, 99), 1)
            },
            "latency": {
                "avg": round(avg(latencies), 1),
                "p50": round(percentile(latencies, 50), 1),
                "p95": round(percentile(latencies, 95), 1),
                "p99": round(percentile(latencies, 99), 1)
            },
            "tps": {
                "avg": round(avg(tps_list), 1),
                "p50": round(percentile(tps_list, 50), 1),
                "p95": round(percentile(tps_list, 95), 1)
            },
            "tokens": {
                "prompt_total": prompt_tokens_total,
                "completion_total": completion_tokens_total,
                "cached_total": cached_tokens_total,
                "cache_creation_5m_total": cache_creation_5m_total,
                "cache_creation_1h_total": cache_creation_1h_total,
                "total": total_tokens_total,
                "avg_prompt": round(prompt_tokens_total / len(all_traces), 2) if all_traces else 0,
                "avg_completion": round(completion_tokens_total / len(all_traces), 2) if all_traces else 0,
                "avg_cached": round(cached_tokens_total / len(all_traces), 2) if all_traces else 0,
                "avg_cache_creation_5m": round(cache_creation_5m_total / len(all_traces), 2) if all_traces else 0,
                "avg_cache_creation_1h": round(cache_creation_1h_total / len(all_traces), 2) if all_traces else 0,
                "avg_total": round(total_tokens_total / len(all_traces), 2) if all_traces else 0,
                "requests_with_usage": requests_with_usage,
            },
            "stream": {
                "total": stream_real_count + stream_fake_count + stream_non_count,
                "real": stream_real_count,
                "fake": stream_fake_count,
                "non_stream": stream_non_count,
                "keepalive_total": stream_keepalive_total,
                "keepalive_avg_per_real": round(
                    stream_keepalive_total / stream_real_count, 2
                ) if stream_real_count > 0 else 0,
                "bootstrap_retry_total": stream_bootstrap_retry_total,
                "bootstrap_retry_requests": stream_bootstrap_retry_requests,
                "bootstrap_retry_rate": round(
                    stream_bootstrap_retry_requests / stream_real_count, 4
                ) if stream_real_count > 0 else 0,
                "bootstrap_failure_count": stream_bootstrap_failure_count,
                "bootstrap_failure_rate": round(
                    stream_bootstrap_failure_count / stream_real_count, 4
                ) if stream_real_count > 0 else 0,
            },
            "models": list(models_set),
            "time_range": {
                "start": min(start_times) if start_times else 0,
                "end": max(start_times) if start_times else 0
            }
        }
        
        # 更新缓存
        if self._stats_cache is None:
            self._stats_cache = {}
        self._stats_cache[cache_key] = stats
        self._stats_cache_time = time.time()
        
        return stats
    
    async def get_models(self) -> List[str]:
        """获取所有模型列表"""
        stats = await self.get_stats()
        return stats.get("models", [])
    
    async def clear_for_key(self, masked_key: str) -> int:
        """Remove persisted request traces for a masked key."""
        if not masked_key:
            return 0

        from ..storage.storage_adapter import get_storage_adapter
        adapter = await get_storage_adapter()

        removed = 0
        async with self._save_lock:
            for shard_idx in range(ShardManager.MAX_SHARDS):
                shard_key = f"perf_traces_{shard_idx}"
                try:
                    shard_data = await adapter.get_perf(shard_key, None)
                    if not isinstance(shard_data, list) or not shard_data:
                        continue

                    filtered = [
                        trace for trace in shard_data
                        if not isinstance(trace, dict)
                        or str(trace.get("key_masked", "")).strip() != masked_key
                    ]
                    if len(filtered) == len(shard_data):
                        continue

                    removed += len(shard_data) - len(filtered)
                    if filtered:
                        await adapter.set_perf(shard_key, filtered)
                        self.shard_manager.shard_counts[shard_idx] = len(filtered)
                    else:
                        await adapter.delete_perf(shard_key)
                        self.shard_manager.shard_counts.pop(shard_idx, None)
                except Exception as e:
                    log.warning(f"Failed to clear traces for key from shard {shard_idx}: {e}")

            if removed:
                await adapter.set_perf("perf_meta", self.shard_manager.to_dict())
                self._stats_cache = None
        return removed

    async def clear_all(self):
        """清除所有追踪数据"""
        from ..storage.storage_adapter import get_storage_adapter
        adapter = await get_storage_adapter()

        async with self._save_lock:
            # 扫描固定分片，避免 perf_meta 缺失或过期时遗留旧请求明细。
            for shard_idx in range(ShardManager.MAX_SHARDS):
                shard_key = f"perf_traces_{shard_idx}"
                try:
                    await adapter.delete_perf(shard_key)
                except Exception:
                    pass

            self.shard_manager = ShardManager()
            await adapter.set_perf("perf_meta", self.shard_manager.to_dict())
            self._stats_cache = None
        
        log.info("All performance traces cleared")


# 全局实例
_tracker: Optional[PerformanceTracker] = None
_tracker_lock = asyncio.Lock()


async def get_performance_tracker() -> PerformanceTracker:
    """获取全局性能追踪器实例"""
    global _tracker
    if _tracker is None:
        async with _tracker_lock:
            if _tracker is None:
                _tracker = PerformanceTracker()
                await _tracker.initialize()
    return _tracker

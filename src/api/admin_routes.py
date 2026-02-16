import os
from typing import Dict, Any
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi import WebSocket, WebSocketDisconnect
import asyncio
import re
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from log import log
from config import (
    get_api_password,
    get_panel_password,
    get_assembly_api_key,
    get_assembly_api_keys,
    get_server_port,
    get_server_host,
)
from ..storage.storage_adapter import get_storage_adapter
# 统计功能已迁移到 unified_stats 模块
from ..services.assembly_client import fetch_assembly_models, get_rate_limit_info


router = APIRouter()
security = HTTPBearer()


async def authenticate(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    token = credentials.credentials
    password = await get_panel_password()
    if token != password:
        # 兼容 API 密码
        api_pwd = await get_api_password()
        if token != api_pwd:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="密码错误")
    return token


@router.get("/ui")
async def admin_ui():
    # 从 src/api/admin_routes.py 回到项目根目录需要 3 级
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    file_path = os.path.join(base_dir, "front", "control_panel.html")
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                html = f.read()
        except Exception:
            html = "<html><body>无法加载控制面板文件</body></html>"
    else:
        html = "<html><body>控制面板文件未找到</body></html>"
    return HTMLResponse(content=html)


@router.get("/config/get")
async def get_config(token: str = Depends(authenticate)):
    adapter = await get_storage_adapter()
    cfg: Dict[str, Any] = {}
    # 读取关键配置
    cfg["assembly_api_key"] = await get_assembly_api_key()
    cfg["assembly_api_keys"] = await get_assembly_api_keys()

    # 密码
    cfg["api_password"] = await get_api_password()
    cfg["panel_password"] = await get_panel_password()
    cfg["port"] = await get_server_port()
    cfg["host"] = await get_server_host()
    # 其他配置从适配器读取
    try:
        cfg["calls_per_rotation"] = await adapter.get_config("calls_per_rotation", 100)
        cfg["retry_429_enabled"] = await adapter.get_config("retry_429_enabled", True)
        cfg["retry_429_max_retries"] = await adapter.get_config("retry_429_max_retries", 5)
        cfg["retry_429_interval"] = await adapter.get_config("retry_429_interval", 1.0)
        cfg["auto_ban_enabled"] = await adapter.get_config("auto_ban_enabled", False)
        cfg["auto_ban_error_codes"] = await adapter.get_config("auto_ban_error_codes", [401,403])
        cfg["max_tokens_mode"] = await adapter.get_config("max_tokens_mode", "off")
        cfg["fake_stream_enabled"] = await adapter.get_config("fake_stream_enabled", False)
        cfg["fake_stream_speed"] = await adapter.get_config("fake_stream_speed", 100)
        cfg["enable_real_streaming"] = await adapter.get_config("enable_real_streaming", False)
        cfg["stream_keepalive_seconds"] = await adapter.get_config("stream_keepalive_seconds", 0)
        cfg["stream_bootstrap_retries"] = await adapter.get_config("stream_bootstrap_retries", 1)
    except Exception:
        pass
    try:
        adapter_val = await (await get_storage_adapter()).get_config("override_env")
        if isinstance(adapter_val, str):
            cfg["override_env"] = adapter_val.lower() in ("true","1","yes","on")
        else:
            cfg["override_env"] = bool(adapter_val)
    except Exception:
        cfg["override_env"] = False
    env_locked = [k for k in [
        "API_PASSWORD","PANEL_PASSWORD","PORT","HOST",
        "CALLS_PER_ROTATION","RETRY_429_ENABLED","RETRY_429_MAX_RETRIES","RETRY_429_INTERVAL","AUTO_BAN","AUTO_BAN_ERROR_CODES",
        "ENABLE_REAL_STREAMING","STREAM_KEEPALIVE_SECONDS","STREAM_BOOTSTRAP_RETRIES"
    ] if os.getenv(k)]
    return JSONResponse(content={"config": cfg, "env_locked": env_locked})

@router.get("/config/all")
async def get_all_config(token: str = Depends(authenticate)):
    adapter = await get_storage_adapter()
    cfg = await adapter.get_all_config()
    backend = "file"
    if os.getenv("REDIS_URI"):
        backend = "redis"
    elif os.getenv("POSTGRES_DSN"):
        backend = "postgres"
    prefix = os.getenv("REDIS_PREFIX", "AMB2API")
    return JSONResponse(content={"backend": backend, "prefix": prefix, "config": cfg})


@router.post("/config/save")
async def save_config(payload: Dict[str, Any], token: str = Depends(authenticate)):
    adapter = await get_storage_adapter()
    updates = {}
    override_flag = payload.get("override_env")
    if override_flag is not None:
        updates["override_env"] = bool(override_flag)
        ok = await adapter.set_config("override_env", updates["override_env"])
        if not ok:
            raise HTTPException(status_code=500, detail="保存失败: override_env")
    allow_override = updates.get("override_env")
    if allow_override is None:
        try:
            cfg_override = await adapter.get_config("override_env")
            if isinstance(cfg_override, str):
                allow_override = cfg_override.lower() in ("true","1","yes","on")
            else:
                allow_override = bool(cfg_override)
        except Exception:
            allow_override = False
    if payload.get("assembly_api_keys") is not None:
        val = payload.get("assembly_api_keys")
        if isinstance(val, str):
            items = [x.strip() for x in val.replace("\n", ",").split(",") if x.strip()]
        elif isinstance(val, list):
            items = [str(x).strip() for x in val if str(x).strip()]
        else:
            items = []
        updates["assembly_api_keys"] = items
    if payload.get("assembly_api_key") is not None:
        updates["assembly_api_key"] = payload.get("assembly_api_key")

    if payload.get("api_password") is not None:
        updates["api_password"] = payload.get("api_password")
    if payload.get("panel_password") is not None:
        updates["panel_password"] = payload.get("panel_password")
    if payload.get("password") is not None:
        updates["password"] = payload.get("password")
    if payload.get("port") is not None:
        updates["port"] = int(payload.get("port"))
    if payload.get("host") is not None:
        updates["host"] = str(payload.get("host"))
    # 性能与重试配置
    if payload.get("calls_per_rotation") is not None:
        try:
            updates["calls_per_rotation"] = int(payload.get("calls_per_rotation"))
        except Exception:
            updates["calls_per_rotation"] = 100
    if payload.get("retry_429_enabled") is not None:
        updates["retry_429_enabled"] = bool(payload.get("retry_429_enabled"))
    if payload.get("retry_429_max_retries") is not None:
        try:
            updates["retry_429_max_retries"] = int(payload.get("retry_429_max_retries"))
        except Exception:
            updates["retry_429_max_retries"] = 5
    if payload.get("retry_429_interval") is not None:
        try:
            updates["retry_429_interval"] = float(payload.get("retry_429_interval"))
        except Exception:
            updates["retry_429_interval"] = 1.0
    # 自动封禁配置
    if payload.get("auto_ban_enabled") is not None:
        updates["auto_ban_enabled"] = bool(payload.get("auto_ban_enabled"))
    if payload.get("auto_ban_error_codes") is not None:
        val = payload.get("auto_ban_error_codes")
        codes = []
        try:
            if isinstance(val, str):
                codes = [int(x.strip()) for x in val.split(',') if x.strip()]
            elif isinstance(val, list):
                codes = [int(x) for x in val]
        except Exception:
            codes = [401,403]
        updates["auto_ban_error_codes"] = codes
    # Max Tokens 自适应模式
    if payload.get("max_tokens_mode") is not None:
        mode = payload.get("max_tokens_mode")
        if mode in ("off", "low", "medium", "high"):
            updates["max_tokens_mode"] = mode
    # 全局假流式配置
    if payload.get("fake_stream_enabled") is not None:
        updates["fake_stream_enabled"] = bool(payload.get("fake_stream_enabled"))
    if payload.get("fake_stream_speed") is not None:
        try:
            speed = int(payload.get("fake_stream_speed"))
            if speed >= 10 and speed <= 2000:
                updates["fake_stream_speed"] = speed
        except (ValueError, TypeError):
            pass
    # 真实流式与 keepalive/bootstrap 配置
    if payload.get("enable_real_streaming") is not None:
        updates["enable_real_streaming"] = bool(payload.get("enable_real_streaming"))
    if payload.get("stream_keepalive_seconds") is not None:
        try:
            keepalive = float(payload.get("stream_keepalive_seconds"))
            if keepalive >= 0:
                updates["stream_keepalive_seconds"] = keepalive
        except (ValueError, TypeError):
            pass
    if payload.get("stream_bootstrap_retries") is not None:
        try:
            retries = int(payload.get("stream_bootstrap_retries"))
            if retries >= 0 and retries <= 10:
                updates["stream_bootstrap_retries"] = retries
        except (ValueError, TypeError):
            pass
    # 写入
    for k, v in updates.items():
        ok = await adapter.set_config(k, v)
        if not ok:
            log.error(f"Failed to set config: {k}")
            raise HTTPException(status_code=500, detail=f"保存失败: {k}")
    
    # 如果更新了密钥配置，需要重新加载KeyManager
    if "assembly_api_keys" in updates or "disabled_key_indices" in updates or "key_aggregation_mode" in updates or "calls_per_rotation" in updates:
        try:
            from ..services.key_manager import get_key_manager
            key_manager = await get_key_manager()
            await key_manager.reload_config()
            log.info("KeyManager reloaded after config update")
        except Exception as e:
            log.warning(f"Failed to reload KeyManager after config update: {e}")
    
    return JSONResponse(content={"saved": list(updates.keys())})


@router.get("/usage/stats")
async def usage_stats(token: str = Depends(authenticate)):
    """获取使用统计（使用统一统计）"""
    from ..stats.unified_stats import get_unified_stats
    
    # 获取配置的密钥列表
    adapter = await get_storage_adapter()
    cfg_keys = await adapter.get_config("assembly_api_keys", [])
    if isinstance(cfg_keys, str):
        cfg_keys = [x.strip() for x in cfg_keys.replace("\n", ",").split(",") if x.strip()]
    
    # 获取统一统计数据
    unified_stats = await get_unified_stats()
    await unified_stats.ensure_keys_exist(cfg_keys)
    stats_data = await unified_stats.get_all_stats(valid_keys=cfg_keys)
    
    # 转换为前端使用格式（保留旧字段兼容）
    result = {}
    for masked_key, key_data in stats_data.get("keys", {}).items():
        daily_limit_models = key_data.get("daily_limit_models", {}) or {}
        gemini_limit = (
            daily_limit_models.get("gemini-2.5-pro")
            or daily_limit_models.get("gemini-2.5-pro-preview")
            or daily_limit_models.get("gemini_2_5_pro")
            or 100
        )
        gemini_calls = key_data.get("model_counts", {}).get("gemini-2.5-pro", 0)
        result[masked_key] = {
            "total_calls": key_data.get("total", 0),
            "success_calls": key_data.get("ok", 0),
            "failure_calls": key_data.get("fail", 0),
            "gemini_2_5_pro_calls": gemini_calls,
            "daily_limit_total": key_data.get("daily_limit_total", 1000),
            "daily_limit_models": daily_limit_models,
            "daily_limit_gemini_2_5_pro": gemini_limit,
            "model_counts": key_data.get("model_counts", {}),
            "models": key_data.get("models", {}),
            "next_reset_time": key_data.get("next_reset_time"),
            "display_name": masked_key,
        }
    
    return JSONResponse(content=result)


@router.get("/usage/aggregated")
async def usage_aggregated(model: str = None, key: str = None, only: str = None, limit: int = 0, token: str = Depends(authenticate)):
    """
    获取聚合统计数据
    
    使用统一统计模块，确保总调用数与详情统计保持一致
    """
    from ..stats.unified_stats import get_unified_stats
    
    # 获取配置的密钥列表
    adapter = await get_storage_adapter()
    cfg_keys = await adapter.get_config("assembly_api_keys", [])
    if isinstance(cfg_keys, str):
        cfg_keys = [x.strip() for x in cfg_keys.replace("\n", ",").split(",") if x.strip()]
    
    # 获取统一统计数据
    unified_stats = await get_unified_stats()
    
    # 确保所有配置的密钥都在统计中存在
    await unified_stats.ensure_keys_exist(cfg_keys)
    
    # 获取统计数据（只包含有效密钥）
    stats_data = await unified_stats.get_all_stats(valid_keys=cfg_keys)
    
    # 构建响应
    models = stats_data.get("models", {})
    keys = stats_data.get("keys", {})
    total = stats_data.get("total", {})
    
    ok_total = total.get("success", 0)
    fail_total = total.get("failure", 0)
    
    # 应用过滤
    if model:
        # 按模型过滤
        filtered_keys = {}
        for masked_key, key_data in keys.items():
            model_counts = key_data.get("model_counts", {})
            models_detail = key_data.get("models", {})
            if model in model_counts:
                model_ok = model_counts[model]
                model_fail = 0
                if isinstance(models_detail.get(model), dict):
                    model_fail = int(models_detail.get(model, {}).get("fail", 0) or 0)
                filtered_keys[masked_key] = {
                    "ok": model_ok,
                    "fail": model_fail,
                    "models": {model: {"ok": model_ok, "fail": model_fail}},
                    "model_counts": {model: model_ok},
                    "total": model_ok + model_fail,
                    "daily_limit_total": key_data.get("daily_limit_total", 1000),
                    "daily_limit_models": key_data.get("daily_limit_models", {}),
                    "next_reset_time": key_data.get("next_reset_time"),
                    "last_call_time": key_data.get("last_call_time", 0),
                    "masked_key": masked_key,
                    "display_key": masked_key,
                }
        keys = filtered_keys
        models = {model: models.get(model, {"ok": 0, "fail": 0})} if model in models else {}
        ok_total = sum(d.get("ok", 0) for d in keys.values())
        fail_total = sum(d.get("fail", 0) for d in keys.values())
    
    if key:
        # 按密钥过滤
        if key in keys:
            keys = {key: keys[key]}
        else:
            keys = {}
        # 重新计算模型统计
        models = {}
        for key_data in keys.values():
            for model_name, model_stats in key_data.get("models", {}).items():
                if model_name not in models:
                    models[model_name] = {"ok": 0, "fail": 0}
                models[model_name]["ok"] += model_stats.get("ok", 0)
                models[model_name]["fail"] += model_stats.get("fail", 0)
        ok_total = sum(d.get("ok", 0) for d in keys.values())
        fail_total = sum(d.get("fail", 0) for d in keys.values())
    
    # 为每个 key 添加 masked_key 和 display_key 字段
    for masked_key, key_data in keys.items():
        key_data["masked_key"] = masked_key
        key_data["display_key"] = masked_key
    
    # 应用 only 过滤
    if only == "success":
        for d in models.values():
            d["fail"] = 0
        for d in keys.values():
            d["fail"] = 0
            for md in d.get("models", {}).values():
                md["fail"] = 0
        fail_total = 0
    elif only == "fail":
        for d in models.values():
            d["ok"] = 0
        for d in keys.values():
            d["ok"] = 0
            for md in d.get("models", {}).values():
                md["ok"] = 0
        ok_total = 0
    
    # 汇总 next_reset_time（取最早的一个，便于前端展示）
    reset_candidates = []
    for key_data in keys.values():
        raw_reset = key_data.get("next_reset_time")
        if not raw_reset or not isinstance(raw_reset, str):
            continue
        text = raw_reset.strip()
        if not text:
            continue
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            reset_candidates.append(dt.astimezone(timezone.utc))
        except Exception:
            continue

    next_reset_time = min(reset_candidates).isoformat() if reset_candidates else None

    # 构建聚合响应
    agg = {
        "total_files": len(keys),
        "total_gemini_2_5_pro_calls": 0,  # 兼容旧字段
        "total_all_model_calls": ok_total + fail_total,
        "avg_gemini_2_5_pro_per_file": 0,
        "avg_total_per_file": (ok_total + fail_total) / max(len(keys), 1),
        "next_reset_time": next_reset_time,
        "log_summary": {
            "models": models,
            "keys": keys,
            "total": {"ok": ok_total, "fail": fail_total}
        }
    }
    
    return JSONResponse(content=agg)


@router.get("/models/query")
async def models_query(token: str = Depends(authenticate)):
    """查询上游模型列表并按供应商分类返回（含元数据）"""
    data = await fetch_assembly_models()
    models = [str(m) for m in data.get("models", [])]
    meta = data.get("meta", {})
    # 缓存到配置，便于后续列表和操练场使用
    try:
        adapter = await get_storage_adapter()
        await adapter.set_config("available_models", models)
        await adapter.set_config("available_models_meta", meta)
    except Exception:
        pass
    grouped: Dict[str, Any] = {"Anthropic": [], "OpenAI": [], "Google": [], "Other": []}
    for m in models:
        ms = str(m)
        if ms.startswith("claude"):
            grouped["Anthropic"].append(ms)
        elif ms.startswith("gpt") or ms.startswith("chatgpt"):
            grouped["OpenAI"].append(ms)
        elif ms.startswith("gemini"):
            grouped["Google"].append(ms)
        else:
            grouped["Other"].append(ms)
    return JSONResponse(content={"models": models, "grouped": grouped, "meta": meta})


@router.post("/models/save")
async def models_save(payload: Dict[str, Any], token: str = Depends(authenticate)):
    """保存所选模型到配置"""
    selected = payload.get("selected_models") or []
    if not isinstance(selected, list):
        raise HTTPException(status_code=400, detail="selected_models 必须是数组")
    adapter = await get_storage_adapter()
    ok = await adapter.set_config("available_models_selected", [str(m) for m in selected])
    if not ok:
        raise HTTPException(status_code=500, detail="保存失败: available_models_selected")
    return JSONResponse(content={"saved_count": len(selected)})


@router.post("/usage/update-limits")
async def usage_update_limits(payload: Dict[str, Any], token: str = Depends(authenticate)):
    """更新使用限制（总限制 + 按模型限制）"""
    from ..stats.unified_stats import get_unified_stats

    masked_key = str(payload.get("filename", "")).strip()
    if not masked_key:
        raise HTTPException(status_code=400, detail="缺少 filename（masked_key）")

    total_limit = payload.get("total_limit")
    if total_limit is not None:
        try:
            total_limit = int(total_limit)
        except Exception:
            raise HTTPException(status_code=400, detail="total_limit 必须是正整数")
        if total_limit <= 0:
            raise HTTPException(status_code=400, detail="total_limit 必须是正整数")

    model_limits = payload.get("model_limits")
    if model_limits is not None and not isinstance(model_limits, dict):
        raise HTTPException(status_code=400, detail="model_limits 必须是对象")

    normalized_model_limits = None
    if isinstance(model_limits, dict):
        normalized_model_limits = {}
        for model_name, limit in model_limits.items():
            model = str(model_name).strip()
            if not model:
                continue
            try:
                parsed_limit = int(limit)
            except Exception:
                raise HTTPException(status_code=400, detail=f"模型 {model} 的限制必须是正整数")
            if parsed_limit <= 0:
                raise HTTPException(status_code=400, detail=f"模型 {model} 的限制必须是正整数")
            normalized_model_limits[model] = parsed_limit

    unified_stats = await get_unified_stats()
    try:
        updated = await unified_stats.update_daily_limits(
            masked_key=masked_key,
            total_limit=total_limit,
            model_limits=normalized_model_limits if model_limits is not None else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.error(f"Failed to update usage limits for {masked_key}: {e}")
        raise HTTPException(status_code=500, detail="更新限制失败")

    if updated is None:
        raise HTTPException(status_code=404, detail="未找到对应密钥统计")

    return JSONResponse(content={
        "message": "限制已更新",
        "masked_key": masked_key,
        "daily_limit_total": updated.get("daily_limit_total", 1000),
        "daily_limit_models": updated.get("daily_limit_models", {}),
        "next_reset_time": updated.get("next_reset_time"),
    })


@router.post("/usage/reset")
async def usage_reset(payload: Dict[str, Any], token: str = Depends(authenticate)):
    """重置使用统计"""
    from ..stats.unified_stats import get_unified_stats
    
    filename = payload.get("filename")  # 这里 filename 实际上是 masked_key
    unified_stats = await get_unified_stats()
    
    if filename:
        await unified_stats.reset_stats(masked_key=filename)
    else:
        await unified_stats.reset_stats()
    
    return JSONResponse(content={"message": "使用统计已重置"})

@router.get("/storage/info")
async def storage_info(token: str = Depends(authenticate)):
    adapter = await get_storage_adapter()
    info = await adapter.get_backend_info()
    return JSONResponse(content=info)


@router.get("/usage/summary")
async def usage_summary(model: str = None, key: str = None, only: str = None, limit: int = 0, token: str = Depends(authenticate)):
    """获取使用摘要（使用统一统计）"""
    from ..stats.unified_stats import get_unified_stats
    
    # 获取配置的密钥列表
    adapter = await get_storage_adapter()
    cfg_keys = await adapter.get_config("assembly_api_keys", [])
    if isinstance(cfg_keys, str):
        cfg_keys = [x.strip() for x in cfg_keys.replace("\n", ",").split(",") if x.strip()]
    
    # 获取统一统计数据
    unified_stats = await get_unified_stats()
    await unified_stats.ensure_keys_exist(cfg_keys)
    stats_data = await unified_stats.get_all_stats(valid_keys=cfg_keys)
    
    models = stats_data.get("models", {})
    keys = stats_data.get("keys", {})
    total = stats_data.get("total", {})
    
    ok_total = total.get("success", 0)
    fail_total = total.get("failure", 0)
    
    # 应用过滤
    if model:
        filtered_keys = {}
        for masked_key, key_data in keys.items():
            model_counts = key_data.get("model_counts", {})
            if model in model_counts:
                filtered_keys[masked_key] = {
                    "ok": model_counts[model],
                    "fail": 0,
                    "models": {model: {"ok": model_counts[model], "fail": 0}},
                }
        keys = filtered_keys
        models = {model: models.get(model, {"ok": 0, "fail": 0})} if model in models else {}
        ok_total = sum(d.get("ok", 0) for d in keys.values())
        fail_total = sum(d.get("fail", 0) for d in keys.values())
    
    if key:
        if key in keys:
            keys = {key: keys[key]}
        else:
            keys = {}
        models = {}
        for key_data in keys.values():
            for model_name, model_stats in key_data.get("models", {}).items():
                if model_name not in models:
                    models[model_name] = {"ok": 0, "fail": 0}
                models[model_name]["ok"] += model_stats.get("ok", 0)
                models[model_name]["fail"] += model_stats.get("fail", 0)
        ok_total = sum(d.get("ok", 0) for d in keys.values())
        fail_total = sum(d.get("fail", 0) for d in keys.values())
    
    if only == "success":
        for d in models.values():
            d["fail"] = 0
        for d in keys.values():
            d["fail"] = 0
            for md in d.get("models", {}).values():
                md["fail"] = 0
        fail_total = 0
    elif only == "fail":
        for d in models.values():
            d["ok"] = 0
        for d in keys.values():
            d["ok"] = 0
            for md in d.get("models", {}).values():
                md["ok"] = 0
        ok_total = 0
    
    return JSONResponse(content={"models": models, "keys": keys, "total": {"ok": ok_total, "fail": fail_total}})


@router.websocket("/auth/logs/stream")
async def logs_stream(websocket: WebSocket):
    await websocket.accept()
    try:
        token = websocket.query_params.get("token")
        panel_pwd = await get_panel_password()
        api_pwd = await get_api_password()
        if token not in (panel_pwd, api_pwd):
            await websocket.send_text("[ERROR] 未授权的日志访问")
            await websocket.close(code=1008)
            return
        log_file = log.get_log_file()
        pos = 0
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                f.seek(0, os.SEEK_END)
                pos = f.tell()
        except Exception:
            pos = 0
        while True:
            await asyncio.sleep(0.5)
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    f.seek(pos)
                    data = f.read()
                    if data:
                        lines = data.splitlines()
                        for ln in lines:
                            await websocket.send_text(ln)
                        pos = f.tell()
            except FileNotFoundError:
                await websocket.send_text("[INFO] 日志文件未找到")
            except Exception:
                # 避免泄露错误细节
                await websocket.send_text("[ERROR] 读取日志失败")
    except WebSocketDisconnect:
        return


@router.get("/auth/logs/download")
async def logs_download(token: str = Depends(authenticate)):
    log_file = log.get_log_file()
    if not os.path.exists(log_file):
        raise HTTPException(status_code=404, detail="日志文件不存在")
    headers = {"Content-Disposition": "attachment; filename=amb2api_logs.txt"}
    return FileResponse(log_file, media_type="text/plain", headers=headers)


@router.post("/auth/logs/clear")
async def logs_clear(token: str = Depends(authenticate)):
    log_file = log.get_log_file()
    try:
        with open(log_file, "w", encoding="utf-8") as f:
            f.write("")
        return JSONResponse(content={"message": "日志已清空"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"清空失败: {e}")
@router.post("/auth/login")
async def login(payload: Dict[str, Any]):
    password = str(payload.get("password", ""))
    panel_pwd = await get_panel_password()
    if password == panel_pwd:
        # 登录成功后触发预加载（非阻塞）
        log.info("[Preload] Panel login successful, scheduling preload task")
        task = asyncio.create_task(_trigger_preload_on_panel_login())
        task.add_done_callback(_preload_task_done_callback)
        return JSONResponse(content={"token": password})
    api_pwd = await get_api_password()
    if password == api_pwd:
        # 登录成功后触发预加载（非阻塞）
        log.info("[Preload] Panel login successful (api_pwd), scheduling preload task")
        task = asyncio.create_task(_trigger_preload_on_panel_login())
        task.add_done_callback(_preload_task_done_callback)
        return JSONResponse(content={"token": password})
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="密码错误")


def _preload_task_done_callback(task: asyncio.Task):
    """预加载任务完成回调，用于捕获异常"""
    try:
        exc = task.exception()
        if exc:
            log.error(f"[Preload] Task failed with exception: {exc}")
    except asyncio.CancelledError:
        log.warning("[Preload] Task was cancelled")
    except asyncio.InvalidStateError:
        pass  # Task not done yet


async def _trigger_preload_on_panel_login():
    """控制面板登录后触发预加载所有已保存的 AssemblyAI 账户"""
    try:
        log.info("[Preload] _trigger_preload_on_panel_login started")
        
        # 获取已保存的账户列表
        adapter = await get_storage_adapter()
        accounts_data = await adapter.get_config("assembly_accounts_list")
        
        log.info(f"[Preload] Got accounts_data: {type(accounts_data)}, count={len(accounts_data) if accounts_data else 0}")
        
        if not accounts_data or not isinstance(accounts_data, list) or len(accounts_data) == 0:
            log.info("[Preload] No saved accounts found, skipping preload")
            return
        
        # 获取当前选中的账户
        current_account = await adapter.get_config("assembly_current_account")
        current_email = current_account.get("email") if current_account else None
        
        log.info(f"[Preload] Current account: {current_email}")
        
        # 如果没有当前账户，使用第一个账户
        if not current_email and accounts_data:
            current_email = accounts_data[0].get("email")
            log.info(f"[Preload] Using first account as current: {current_email}")
        
        log.info(f"[Preload] Panel login detected, triggering preload for {len(accounts_data)} accounts")
        
        # 获取预加载队列并启动
        from ..services.account_preload import get_preload_queue
        queue = await get_preload_queue()
        
        log.info(f"[Preload] Got queue, started={queue._started}")
        
        if not queue._started:
            log.info("[Preload] Starting queue...")
            await queue.start()
            log.info("[Preload] Queue started")
        
        # 将所有账户加入队列
        accounts = [acc.get("email") for acc in accounts_data if acc.get("email")]
        log.info(f"[Preload] Enqueueing accounts: {accounts}")
        await queue.enqueue_all_accounts(current_account=current_email, accounts=accounts)
        
        log.info(f"[Preload] Preload triggered for {len(accounts)} accounts after panel login")
        
    except Exception as e:
        import traceback
        log.error(f"[Preload] Failed to trigger preload on panel login: {e}")
        log.error(f"[Preload] Traceback: {traceback.format_exc()}")


@router.get("/rate-limits")
async def rate_limits(token: str = Depends(authenticate)):
    """获取所有API Key的速率限制信息"""
    rate_info = await get_rate_limit_info()
    
    # 获取配置的所有keys用于显示完整列表
    keys = await get_assembly_api_keys()
    
    # 构建完整的速率限制信息
    result = []
    for idx, key in enumerate(keys):
        from ..services.assembly_client import _mask_key
        masked = _mask_key(key)
        
        if idx in rate_info:
            info = rate_info[idx]
            result.append({
                "index": idx,
                "key": masked,
                "limit": info.get("limit", 0),
                "remaining": info.get("remaining", 0),
                "used": info.get("used", 0),
                "reset_in_seconds": info.get("reset_in_seconds", 0),
                "last_request_time": info.get("last_request_time", 0),
                "status": "active" if info.get("remaining", 0) > 0 else "exhausted"
            })
        else:
            # 未使用过的key
            result.append({
                "index": idx,
                "key": masked,
                "limit": 0,
                "remaining": 0,
                "used": 0,
                "reset_in_seconds": 0,
                "last_request_time": 0,
                "status": "unused"
            })
    
    return JSONResponse(content={"rate_limits": result})
@router.get("/keys/invalid")
async def invalid_keys(token: str = Depends(authenticate)):
    """
    列出失效 key
    
    失效判断逻辑：
    失效密钥：统计中的密钥，在配置密钥中不存在，所以是失效的历史的密钥
    """
    from ..stats.unified_stats import get_unified_stats, mask_key
    
    adapter = await get_storage_adapter()
    cfg_keys = await adapter.get_config("assembly_api_keys", [])
    if isinstance(cfg_keys, str):
        cfg_keys = [x.strip() for x in cfg_keys.replace("\n", ",").split(",") if x.strip()]
    
    # 构建有效密钥的脱敏集合
    valid_masked_keys = {mask_key(k) for k in cfg_keys}
    
    # 获取统一统计中的所有密钥
    unified_stats = await get_unified_stats()
    all_stats = await unified_stats.get_all_stats()
    
    invalid = []
    
    # 失效密钥：统计中存在但配置中不存在的密钥（历史密钥）
    for masked_key, key_data in all_stats.get("keys", {}).items():
        if masked_key not in valid_masked_keys:
            ok_count = key_data.get("ok", 0)
            fail_count = key_data.get("fail", 0)
            
            invalid.append({
                "key": masked_key,
                "ok": ok_count,
                "fail": fail_count,
                "is_configured": False,
                "ignored": False,
                "status": "invalid/historical",
                "reason": f"历史密钥，已从配置中移除（成功{ok_count}次，失败{fail_count}次）"
            })
    
    return JSONResponse(content={"invalid_keys": invalid, "ignored": []})


@router.post("/keys/delete-invalid")
async def delete_invalid_keys(token: str = Depends(authenticate)):
    """
    批量删除失效密钥数据
    
    操作内容：
    1. 从统一统计中删除失效密钥的统计数据
    2. 从日志文件中删除失效密钥的所有记录
    """
    from ..stats.unified_stats import get_unified_stats
    
    adapter = await get_storage_adapter()
    
    # 1. 获取当前有效密钥列表
    cfg_keys = await adapter.get_config("assembly_api_keys", [])
    if isinstance(cfg_keys, str):
        cfg_keys = [x.strip() for x in cfg_keys.replace("\n", ",").split(",") if x.strip()]
    
    # 2. 清理统一统计中的无效密钥
    unified_stats = await get_unified_stats()
    stats_deleted = await unified_stats.cleanup_invalid_keys(cfg_keys)
    
    # 3. 识别失效密钥（用于清理日志）
    inv = await invalid_keys(token)
    data = inv.body if hasattr(inv, "body") else inv
    invalid_list = []
    try:
        if isinstance(data, dict):
            invalid_list = [item.get("key") for item in data.get("invalid_keys", [])]
        else:
            import json
            data_dict = json.loads(data)
            invalid_list = [item.get("key") for item in data_dict.get("invalid_keys", [])]
    except Exception as e:
        log.warning(f"Failed to parse invalid keys: {e}")
        invalid_list = []
    
    # 4. 从日志文件中删除失效密钥的记录
    log_file = log.get_log_file()
    lines_removed = 0
    if os.path.exists(log_file) and invalid_list:
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            
            # 过滤掉包含失效密钥的行
            new_lines = []
            for line in lines:
                should_keep = True
                for invalid_key in invalid_list:
                    if f"key={invalid_key}" in line:
                        should_keep = False
                        lines_removed += 1
                        break
                if should_keep:
                    new_lines.append(line)
            
            # 写回日志文件
            with open(log_file, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            
            log.info(f"Removed {lines_removed} log lines for {len(invalid_list)} invalid keys")
        except Exception as e:
            log.error(f"Failed to clean log file: {e}")
    
    # 5. 清理旧的统计系统数据（兼容性）
    try:
        from ..services.key_manager import get_key_manager
        from ..stats.stats_tracker import get_stats_tracker
        key_manager = await get_key_manager()
        stats_tracker = await get_stats_tracker()
        all_keys = await key_manager.get_all_keys()
        active_indices = [key.index for key in all_keys]
        await stats_tracker.cleanup_inactive_keys(active_indices)
    except Exception as e:
        log.warning(f"Failed to cleanup old stats: {e}")
    
    return JSONResponse(content={
        "success": True,
        "invalid_deleted": len(invalid_list),
        "log_lines_removed": lines_removed,
        "stats_deleted": stats_deleted
    })

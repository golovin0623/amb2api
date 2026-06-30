"""
AssemblyAI Account Management API

提供账户信息、使用量、成本、发票等数据的查询接口。
使用 Session 认证访问 AssemblyAI Dashboard API。
"""

import asyncio
import json
import time
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Depends, Request, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from log import log
from .auth import authenticate
from ..core.httpx_client import http_client
from ..storage.storage_adapter import get_storage_adapter
from ..services.session_security import (
    decode_jwt_exp,
    decrypt_secret,
    encrypt_secret,
)

# 暴露 AssemblyAI 后台会话/账单/key，必须整组鉴权
router = APIRouter(
    prefix="/api/account", tags=["account"], dependencies=[Depends(authenticate)]
)

# AssemblyAI Dashboard API 基础 URL
ASSEMBLY_DASHBOARD_BASE = "https://www.assemblyai.com"

# Session 存储键
SESSION_STORAGE_KEY = "assembly_dashboard_session"
ACCOUNTS_LIST_KEY = "assembly_accounts_list"  # 存储账户列表
CURRENT_ACCOUNT_KEY = "assembly_current_account"  # 当前选中的账户
# 本地持久会话窗口上限。真正的失效以服务端实际 401 为准（_make_dashboard_request
# 在续期失败后清理死会话）；本地窗口只是兜底，防止「短暂离线 / 重启」后把仍可凭
# 长效 cookie 恢复的会话误删。取 30 天，远长于一次离线，远短于 Stytch 会话绝对上限。
SESSION_EXPIRY_HOURS = 24 * 30  # 持久会话本地兜底窗口（每次成功续期都会前滚）

# Stytch sessionJWT 固定 5 分钟寿命，必须在到期前主动续期。
SESSION_SECRET_KEY = "assembly_session_secret"  # 本地加密密钥（密码兜底续期用）
JWT_REFRESH_LEEWAY_SECONDS = 90  # JWT 剩余寿命低于该值即视为需要续期
RENEW_THROTTLE_SECONDS = 20  # 同一账户续期最小间隔，避免续期风暴
KEEPALIVE_INTERVAL_SECONDS = 210  # 后台保活轮询间隔（< 5 分钟 JWT 寿命）
DEFAULT_JWT_TTL_SECONDS = 300  # 无法解析 exp 时的保守默认寿命
# 长效 cookie（aai_extended_session）主动滚动间隔：即使 JWT 仍新鲜也要定期发一次
# 认证请求滚动 cookie，使服务端会话永不进入"长时间无活动"而被回收。30 分钟远小于
# cookie 的服务端寿命，又足够稀疏，不会形成请求风暴。
COOKIE_KEEPALIVE_INTERVAL_SECONDS = 30 * 60
# 密码兜底续期连续失败后的退避：避免用（可能已失效的）密码反复登录触发风控/锁号。
PASSWORD_RENEW_BACKOFF_SECONDS = 10 * 60
# 连续多少次密码登录失败后才丢弃存储的凭据（容忍偶发的瞬时 401/网络抖动）。
PASSWORD_RENEW_MAX_FAILURES = 3

_cache_store: Dict[str, Dict[str, Any]] = {}
_renew_locks: Dict[str, asyncio.Lock] = {}  # 每账户续期锁，串行化并发续期
_keepalive_task: Optional["asyncio.Task"] = None
_secret_lock: Optional[asyncio.Lock] = None  # 加密密钥初始化锁（防并发生成覆盖）
_cache_ttl_seconds = 300
_api_keys_fetch_task = None
_api_keys_last_fetch_ts = 0.0


def _truncate_log_value(value: Any, limit: int = 200) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated, len={len(text)}]"


def _summarize_headers_for_log(headers: Any) -> Dict[str, Any]:
    """仅记录关键响应头，避免日志噪音和敏感头泄露。"""
    try:
        header_map = {str(k).lower(): v for k, v in dict(headers).items()}
    except Exception:
        return {"header_count": 0}

    keep_keys = [
        "content-type",
        "content-length",
        "cache-control",
        "x-matched-path",
        "x-vercel-cache",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
        "date",
        "server",
    ]
    summary: Dict[str, Any] = {"header_count": len(header_map)}
    for key in keep_keys:
        if key in header_map:
            summary[key] = _truncate_log_value(header_map.get(key), 240)
    return summary


def _dashboard_next_url(path: str, params: Optional[Dict[str, Any]] = None) -> str:
    """Build the Next-Url header for Assembly Dashboard RSC requests."""
    from urllib.parse import urlencode

    next_path = path[10:] if path.startswith("/dashboard") else path
    if not next_path:
        next_path = "/"
    next_params = {k: v for k, v in (params or {}).items() if k != "_rsc"}
    if not next_params:
        return next_path
    return f"{next_path}?{urlencode(next_params, doseq=True)}"

def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    entry = _cache_store.get(key)
    if not entry:
        return None
    ts = entry.get("ts", 0)
    if (time.time() - ts) < _cache_ttl_seconds:
        return entry.get("data")
    return None

def _cache_set(key: str, data: Dict[str, Any]) -> None:
    _cache_store[key] = {"ts": time.time(), "data": data}


def _cache_clear_for_account(account_email: str) -> None:
    """清除指定账户的所有缓存"""
    keys_to_remove = []
    for key in _cache_store.keys():
        if account_email in key:
            keys_to_remove.append(key)
    for key in keys_to_remove:
        del _cache_store[key]
    if keys_to_remove:
        log.info(f"Cleared {len(keys_to_remove)} cache entries for account {account_email}")


# ============================================================================
# 预加载队列集成
# ============================================================================

async def _trigger_preload_after_login(email: str) -> None:
    """登录后触发后台预加载（非阻塞）"""
    try:
        log.info(f"[Preload] Starting preload trigger for {email}")
        from ..services.account_preload import get_preload_queue
        queue = await get_preload_queue()
        log.info(f"[Preload] Got queue instance, started={queue._started}")
        
        # 如果队列未启动，先启动
        if not queue._started:
            log.info(f"[Preload] Starting queue...")
            await queue.start()
            log.info(f"[Preload] Queue started successfully")
        
        # 将所有账户加入队列，当前账户优先
        log.info(f"[Preload] Enqueueing all accounts with current={email}")
        task_ids = await queue.enqueue_all_accounts(current_account=email)
        log.info(f"[Preload] Triggered preload after login for {email}, {len(task_ids)} tasks enqueued")
    except Exception as e:
        import traceback
        log.warning(f"[Preload] Failed to trigger preload after login: {e}")
        log.debug(f"[Preload] Traceback: {traceback.format_exc()}")


async def _reprioritize_preload(email: str) -> None:
    """切换账户时重新排序预加载队列"""
    try:
        from ..services.account_preload import get_preload_queue
        queue = await get_preload_queue()
        
        if queue._started:
            await queue.reprioritize(email)
            log.debug(f"Reprioritized preload queue for {email}")
    except Exception as e:
        log.warning(f"Failed to reprioritize preload: {e}")


async def _clear_preload_cache(account_email: Optional[str] = None) -> None:
    """清除预加载缓存"""
    try:
        from ..services.account_preload import get_preload_queue
        queue = await get_preload_queue()
        
        if account_email:
            await queue.cache.clear_account(account_email)
            await queue.cancel_account(account_email)
        else:
            # 获取当前账户
            adapter = await get_storage_adapter()
            current = await adapter.get_config(CURRENT_ACCOUNT_KEY)
            if current and current.get("email"):
                await queue.cache.clear_account(current["email"])
                await queue.cancel_account(current["email"])
    except Exception as e:
        log.warning(f"Failed to clear preload cache: {e}")


async def _get_preload_cache(account_email: str, data_type: str) -> Optional[Dict[str, Any]]:
    """从预加载缓存获取数据"""
    try:
        from ..services.account_preload import get_preload_queue
        queue = await get_preload_queue()
        
        # 检查缓存状态
        freshness = await queue.cache.get_freshness_info(account_email)
        log.debug(f"[Preload] Cache freshness for {account_email}: {freshness.get(data_type, {})}")
        
        data = await queue.cache.get_account_data(account_email, data_type)
        if data:
            log.info(f"[Preload] Cache HIT for {account_email}:{data_type}")
            return data
        else:
            log.info(f"[Preload] Cache MISS for {account_email}:{data_type}")
    except Exception as e:
        log.warning(f"Failed to get preload cache: {e}")
    return None



class LoginRequest(BaseModel):
    """登录请求模型"""
    email: str
    password: str


class SwitchAccountRequest(BaseModel):
    """切换账户请求模型"""
    email: str


class SessionInfo(BaseModel):
    """Session 信息模型"""
    email: str
    logged_in: bool
    expires_at: Optional[str] = None
    # JWT 剩余寿命（秒）；为 None 表示无法解析
    jwt_seconds_remaining: Optional[int] = None
    # 是否具备自动续期能力（已加密保存账户凭据）
    auto_renew: bool = False


class AccountInfo(BaseModel):
    """账户信息模型"""
    id: int
    email: str
    customer_type: str
    cc_brand: Optional[str] = None
    cc_last4: Optional[str] = None
    created: str
    api_token: Optional[str] = None


async def _get_session(account_email: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """获取保存的 session 信息
    
    Args:
        account_email: 账户邮箱，如果为None则获取当前选中的账户
    """
    try:
        adapter = await get_storage_adapter()
        
        # 如果没有指定账户，获取当前选中的账户
        if not account_email:
            current_account = await adapter.get_config(CURRENT_ACCOUNT_KEY)
            if current_account:
                account_email = current_account.get("email")
        
        if not account_email:
            return None
        
        # 使用邮箱作为key存储session
        session_key = f"{SESSION_STORAGE_KEY}:{account_email}"
        session_data = await adapter.get_config(session_key)
        if session_data:
            # 检查本地兜底窗口是否到期。注意：本地 expires_at 只是兜底，真正的失效以
            # 服务端实际 401 为准（_make_dashboard_request 续期失败后才清理）。
            expires_at = session_data.get("expires_at")
            if expires_at:
                expiry_time = datetime.fromisoformat(expires_at)
                if datetime.now() > expiry_time:
                    # 仍有恢复手段（长效 cookie / 已存凭据）时不硬删：前滚窗口并保留，
                    # 交给真实请求/后台保活去滚动 cookie 或重新登录。这同时覆盖"升级
                    # 场景"——旧版以 7 天 expires_at 落盘、但 cookie 仍有效的会话不会
                    # 在保活滚动之前被误删。
                    has_recovery = (
                        session_data.get("aai_extended_session")
                        or session_data.get("session_token")
                        or session_data.get("enc_password")
                    )
                    if has_recovery:
                        session_data["expires_at"] = (
                            datetime.now() + timedelta(hours=SESSION_EXPIRY_HOURS)
                        ).isoformat()
                        try:
                            await adapter.set_config(session_key, session_data)
                        except Exception as e:
                            log.warning(
                                f"Failed to roll forward expiry for {account_email}: {e}"
                            )
                        return session_data
                    # 无任何恢复手段：确为死会话，清理
                    log.info(f"Session expired for {account_email}, clearing...")
                    await adapter.delete_config(session_key)
                    # 从账户列表中移除
                    await _remove_account_from_list(account_email)
                    return None
            return session_data
    except Exception as e:
        log.error(f"Failed to get session: {e}")
    return None


async def _save_session(session_data: Dict[str, Any]) -> bool:
    """保存 session 信息"""
    try:
        adapter = await get_storage_adapter()
        email = session_data.get("email")
        if not email:
            log.error("Cannot save session: email is missing")
            return False
        
        # 设置过期时间
        session_data["expires_at"] = (
            datetime.now() + timedelta(hours=SESSION_EXPIRY_HOURS)
        ).isoformat()
        
        # 使用邮箱作为key存储session
        session_key = f"{SESSION_STORAGE_KEY}:{email}"
        await adapter.set_config(session_key, session_data)
        
        # 添加到账户列表
        await _add_account_to_list(email, session_data.get("user_info", {}))
        
        # 如果是第一个账户，设置为当前账户
        accounts_list = await _get_accounts_list()
        if len(accounts_list) == 1:
            await adapter.set_config(CURRENT_ACCOUNT_KEY, {"email": email})
        
        log.info(f"Session saved for {email}")
        return True
    except Exception as e:
        log.error(f"Failed to save session: {e}")
        return False


async def _get_accounts_list() -> List[Dict[str, Any]]:
    """获取账户列表"""
    try:
        adapter = await get_storage_adapter()
        accounts = await adapter.get_config(ACCOUNTS_LIST_KEY)
        if accounts and isinstance(accounts, list):
            # 过滤掉已过期的账户
            valid_accounts = []
            for account in accounts:
                email = account.get("email")
                if email:
                    session = await _get_session(email)
                    if session:
                        valid_accounts.append(account)
            # 更新列表
            if len(valid_accounts) != len(accounts):
                await adapter.set_config(ACCOUNTS_LIST_KEY, valid_accounts)
            return valid_accounts
        return []
    except Exception as e:
        log.error(f"Failed to get accounts list: {e}")
        return []


async def _add_account_to_list(email: str, user_info: Dict[str, Any]) -> None:
    """添加账户到列表"""
    try:
        adapter = await get_storage_adapter()
        accounts = await _get_accounts_list()
        
        # 检查是否已存在
        existing = next((acc for acc in accounts if acc.get("email") == email), None)
        if not existing:
            accounts.append({
                "email": email,
                "user_id": user_info.get("id"),
                "customer_type": user_info.get("customer_type", "PAYG"),
                "created": user_info.get("created", datetime.now().isoformat()),
            })
            await adapter.set_config(ACCOUNTS_LIST_KEY, accounts)
    except Exception as e:
        log.error(f"Failed to add account to list: {e}")


async def _remove_account_from_list(email: str) -> None:
    """从列表中移除账户"""
    try:
        adapter = await get_storage_adapter()
        accounts = await _get_accounts_list()
        accounts = [acc for acc in accounts if acc.get("email") != email]
        await adapter.set_config(ACCOUNTS_LIST_KEY, accounts)
        
        # 如果移除的是当前账户，切换到第一个账户
        current = await adapter.get_config(CURRENT_ACCOUNT_KEY)
        if current and current.get("email") == email:
            if accounts:
                await adapter.set_config(CURRENT_ACCOUNT_KEY, {"email": accounts[0].get("email")})
            else:
                await adapter.delete_config(CURRENT_ACCOUNT_KEY)
    except Exception as e:
        log.error(f"Failed to remove account from list: {e}")


async def _clear_session(account_email: Optional[str] = None) -> bool:
    """清除 session 信息
    
    Args:
        account_email: 账户邮箱，如果为None则清除当前账户
    """
    try:
        adapter = await get_storage_adapter()
        
        # 如果没有指定账户，获取当前选中的账户
        if not account_email:
            current_account = await adapter.get_config(CURRENT_ACCOUNT_KEY)
            if current_account:
                account_email = current_account.get("email")
        
        if account_email:
            session_key = f"{SESSION_STORAGE_KEY}:{account_email}"
            await adapter.delete_config(session_key)
            await _remove_account_from_list(account_email)
            log.info(f"Session cleared for {account_email}")
        else:
            log.warning("No account specified to clear session")
        return True
    except Exception as e:
        log.error(f"Failed to clear session: {e}")
        return False


# ============================================================================
# 会话续期 / 保活（Stytch sessionJWT 仅 5 分钟寿命，必须主动续期）
# ============================================================================

async def _get_session_secret() -> bytes:
    """获取（或首次生成）本地加密密钥，用于加密落盘账户密码。

    用延迟初始化的 asyncio.Lock 串行化首次生成，避免并发协程各自生成不同
    密钥互相覆盖，导致先加密的数据无法解密。
    """
    global _secret_lock
    if _secret_lock is None:
        _secret_lock = asyncio.Lock()
    async with _secret_lock:
        adapter = await get_storage_adapter()
        stored = await adapter.get_config(SESSION_SECRET_KEY)
        hexkey = stored.get("secret") if isinstance(stored, dict) else stored
        if not hexkey:
            import secrets as _secrets
            hexkey = _secrets.token_hex(32)
            await adapter.set_config(SESSION_SECRET_KEY, {"secret": hexkey})
        return bytes.fromhex(hexkey)


async def _get_raw_session(account_email: str) -> Optional[Dict[str, Any]]:
    """读取原始 session（不做持久过期清理），续期逻辑专用。"""
    try:
        adapter = await get_storage_adapter()
        session_key = f"{SESSION_STORAGE_KEY}:{account_email}"
        return await adapter.get_config(session_key)
    except Exception as e:
        log.error(f"Failed to read raw session for {account_email}: {e}")
        return None


async def _attach_encrypted_password(
    session_data: Dict[str, Any], password: Optional[str]
) -> None:
    """把账户密码加密后写入 session_data（用于失效时自动重新登录）。"""
    if not password:
        return
    try:
        key = await _get_session_secret()
        session_data["enc_password"] = encrypt_secret(key, password)
    except Exception as e:
        # 加密失败不应阻断登录，只是失去密码兜底续期能力
        log.warning(f"Failed to encrypt account password for auto-renew: {e}")


async def _decrypt_account_password(session_data: Dict[str, Any]) -> Optional[str]:
    enc = session_data.get("enc_password")
    if not enc:
        return None
    try:
        key = await _get_session_secret()
        return decrypt_secret(key, enc)
    except Exception as e:
        log.warning(f"Failed to decrypt stored account password: {e}")
        return None


def _jwt_seconds_remaining(session_data: Dict[str, Any]) -> Optional[int]:
    """返回 sessionJWT 的剩余寿命（秒）。无法判断时返回 None。"""
    exp = session_data.get("jwt_expires_at_ts")
    if exp is None:
        exp = decode_jwt_exp(session_data.get("session_jwt"))
    if exp is None:
        return None
    return int(exp - time.time())


def _session_needs_renewal(session_data: Dict[str, Any]) -> bool:
    """JWT 已过期或即将过期则需要续期。无法解析 exp 时按 logged_in_at 估算。"""
    remaining = _jwt_seconds_remaining(session_data)
    if remaining is not None:
        return remaining <= JWT_REFRESH_LEEWAY_SECONDS
    # 无 exp：依据登录时间 + 默认寿命保守判断
    logged_in_at = session_data.get("logged_in_at")
    if logged_in_at:
        try:
            age = (datetime.now() - datetime.fromisoformat(logged_in_at)).total_seconds()
            return age >= (DEFAULT_JWT_TTL_SECONDS - JWT_REFRESH_LEEWAY_SECONDS)
        except Exception:
            pass
    return True


async def _authenticate_dashboard(email: str, password: str):
    """向 AssemblyAI Dashboard 发起邮箱+密码认证，返回 (result_json, headers)。

    认证失败抛 HTTPException。供登录与"密码兜底续期"复用。
    """
    import httpx

    login_url = f"{ASSEMBLY_DASHBOARD_BASE}/dashboard/api/auth/authenticate"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        "Accept-Encoding": "gzip, deflate",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        "Origin": ASSEMBLY_DASHBOARD_BASE,
        "Referer": f"{ASSEMBLY_DASHBOARD_BASE}/dashboard/login",
        "Sec-Ch-Ua": '"Chromium";v="142", "Google Chrome";v="142", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    login_data = {"email": email, "password": password, "utm": {}}

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(login_url, json=login_data, headers=headers)

    log.debug(f"Auth response status: {resp.status_code}")
    if resp.status_code != 200:
        error_msg = "Invalid email or password"
        try:
            error_data = resp.json()
            error_msg = error_data.get("error") or error_data.get("message") or error_msg
        except Exception:
            pass
        raise HTTPException(status_code=401, detail=error_msg)

    try:
        result = resp.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse response: {e}")

    if not result.get("isAuthenticated"):
        raise HTTPException(status_code=401, detail="Authentication failed")

    return result, resp.headers


# 服务端在每次认证请求后可能滚动的会话 cookie。捕获全部，使长效会话不断前滚。
_ROLLING_COOKIE_NAMES = ("aai_extended_session", "session_jwt", "session_token")


def _extract_session_cookies(headers: Any) -> Dict[str, str]:
    """从响应头的 Set-Cookie 中提取所有滚动会话 cookie。

    用标准库 http.cookies.SimpleCookie 解析，并优先用 httpx.Headers.get_list
    取多个 Set-Cookie，避免逗号拼接导致的正则错配。返回 {cookie_name: value}。
    """
    try:
        if hasattr(headers, "get_list"):
            set_cookies = headers.get_list("set-cookie")
        else:
            raw = headers.get("set-cookie", "") or ""
            set_cookies = [raw] if raw else []
    except Exception:
        return {}

    from http.cookies import SimpleCookie
    found: Dict[str, str] = {}
    for cookie_str in set_cookies:
        try:
            jar = SimpleCookie()
            jar.load(cookie_str)
            for name in _ROLLING_COOKIE_NAMES:
                if name in jar and jar[name].value:
                    found[name] = jar[name].value
        except Exception:
            pass
    return found


def _extract_aai_extended_session(headers: Any) -> Optional[str]:
    """从响应头的 Set-Cookie 中提取 aai_extended_session（向后兼容的便捷封装）。"""
    return _extract_session_cookies(headers).get("aai_extended_session")


def _build_session_data_from_auth(
    email_fallback: str, result: Dict[str, Any], headers: Any
) -> Dict[str, Any]:
    """把认证响应规整为持久化的 session_data（登录/续期共用）。"""
    user = result.get("user", {}) or {}
    session_jwt = result.get("sessionJWT")
    session_token = result.get("sessionToken")
    aai_extended_session = _extract_aai_extended_session(headers)

    jwt_exp = decode_jwt_exp(session_jwt)
    session_data: Dict[str, Any] = {
        "email": user.get("email", email_fallback),
        "user_id": user.get("id"),
        "api_token": user.get("api_token"),
        "session_jwt": session_jwt,
        "session_token": session_token,
        "aai_extended_session": aai_extended_session,
        "customer_type": user.get("customer_type"),
        "auth_type": "dashboard",
        "logged_in_at": datetime.now().isoformat(),
        "jwt_expires_at_ts": jwt_exp,
        "user_info": {
            "id": user.get("id"),
            "email": user.get("email"),
            "customer_type": user.get("customer_type"),
            "cc_brand": user.get("cc_brand"),
            "cc_last4": user.get("cc_last4"),
            "created": user.get("created"),
            "api_token": user.get("api_token"),
            "metronome_id": user.get("metronome_id"),
        },
    }
    return session_data


def _capture_rolling_cookies(session_data: Dict[str, Any], response: Any) -> bool:
    """从 dashboard 响应里捕获刷新后的会话 cookie，滚动续期持久会话。

    捕获 aai_extended_session（长效会话）以及服务端可能一并滚动的 session_jwt /
    session_token。若 session_jwt 被滚动，同步刷新 jwt_expires_at_ts，使续期判定能
    据新 exp 工作（否则会一直按旧的过期 JWT 判为陈旧）。

    返回 True 表示捕获到了任意更新的 cookie（调用方负责落盘）。
    """
    try:
        rolled = _extract_session_cookies(response.headers)
    except Exception:
        rolled = {}

    changed = False
    for name, value in rolled.items():
        if value and value != session_data.get(name):
            session_data[name] = value
            changed = True
            if name == "session_jwt":
                # 新 JWT 带来新的 exp；解析失败则置 None，让续期判定回退到登录时间估算。
                exp = decode_jwt_exp(value)
                session_data["jwt_expires_at_ts"] = exp
                if exp is None:
                    # 滚动到一个无法解析 exp 的 JWT：同步把 logged_in_at 刷为当前时间，
                    # 否则旧的 logged_in_at 会让 _session_needs_renewal 立即判为陈旧，
                    # 触发不必要的频繁续期（两个调用方都受益）。
                    session_data["logged_in_at"] = datetime.now().isoformat()
    return changed


async def _refresh_session_via_cookie(
    account_email: str, session_data: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """用长效 cookie（aai_extended_session / session_token）滚动续期会话，无需密码。

    这是浏览器保持登录的真实机制：每次带 cookie 访问 dashboard，服务端校验 Stytch
    会话并通过 Set-Cookie 滚动 cookie（并可能下发新的 session_jwt）。我们发一个轻量
    的认证请求专门触发这一滚动，使空闲账户的会话也能长期存活，而不必反复用密码登录
    （后者频繁调用会触发风控、最终丢失凭据，正是"几天后账号查不到"的根因）。

    仅当服务端确实滚动了会话 cookie/JWT（证明会话仍有效）才认定续期成功，返回滚动后的
    session_data（仅更新内存，统一交由调用方 _renew_session 落盘，避免重复 I/O）；cookie
    已失效（401/403）、无可用 cookie、或响应未滚动任何会话 cookie（无续期证据）时返回
    None，交由密码兜底或上层清理。
    """
    if session_data.get("auth_type", "dashboard") != "dashboard":
        return None
    if not (session_data.get("aai_extended_session") or session_data.get("session_token")):
        # 没有长效 cookie，无法做无密码续期
        return None

    try:
        client = await _get_dashboard_client()
        # 用一个会在会话失效时返回 401（而非 302 跳登录）的 RSC 数据端点，且仅用 cookie
        # 认证（allow_bearer=False），避免发送可能已过期的 sessionJWT 误触 401。
        url, headers = _prepare_dashboard_request(
            session_data, "/dashboard/code", {"_rsc": "1"}, allow_bearer=False
        )
        resp = await client.get(url, headers=headers, params={"_rsc": "1"})
    except Exception as e:
        log.debug(f"[Session] Cookie refresh request error for {account_email}: {e}")
        return None

    if resp.status_code in (401, 403):
        log.info(
            f"[Session] Long-lived cookie no longer valid for {account_email} "
            f"(status={resp.status_code})"
        )
        return None
    if resp.status_code >= 400:
        # 其它错误（5xx/限流等）不代表会话失效，本轮放弃但不清理。
        log.debug(
            f"[Session] Cookie refresh got status {resp.status_code} for {account_email}"
        )
        return None

    # 必须以"服务端确实滚动了会话 cookie/JWT"作为会话仍有效的证据，才认定续期成功。
    # 仅凭非错误状态码不够：dashboard 客户端 follow_redirects=True，失效会话可能被 302
    # 跳到登录页后返回 200，或根本不再下发 Set-Cookie。若此时仍本地刷新 last_renew_ts/
    # logged_in_at，会把已死的上游会话误标为新鲜，使后续保活一直跳过密码恢复、账号最终
    # 仍然死亡（正是本修复要避免的）。无证据时返回 None，交由密码兜底/上层清理。
    rolled = _capture_rolling_cookies(session_data, resp)
    if not rolled:
        log.info(
            f"[Session] Cookie refresh for {account_email} produced no rolled session "
            f"cookie; treating as not renewed (will fall back to password if available)"
        )
        return None
    session_data.pop("_jwt_stale", None)
    session_data["last_renew_ts"] = time.time()
    # 若本轮滚动的不是可解析 exp 的新 JWT（会话仍被判为陈旧），用刷新成功的时间作为新鲜度
    # 依据：丢弃陈旧 JWT、改按 logged_in_at 估算，避免刚续期就被立即判为陈旧而反复续期。
    if _session_needs_renewal(session_data):
        session_data.pop("session_jwt", None)
        session_data["jwt_expires_at_ts"] = None
        session_data["logged_in_at"] = datetime.now().isoformat()
    log.info(f"[Session] Renewed via long-lived cookie for {account_email}")
    return session_data


async def _renew_session(
    account_email: str, force_password: bool = False
) -> Optional[Dict[str, Any]]:
    """续期某账户会话：优先用长效 cookie 无密码滚动，失败再用加密密码自动重新登录。

    ``force_password=True`` 时跳过密码续期的退避窗口——用于真实请求遇到 401 的按需
    恢复：退避是为了给后台保活循环降频、避免高频登录触发风控，但不应阻断用户主动发起
    的一次请求恢复（否则一次瞬时的密码 401 + cookie 同时失效会在退避窗口内把用户登出）。

    成功返回更新后的 session_data；无法续期返回 None。
    """
    lock = _renew_locks.setdefault(account_email, asyncio.Lock())
    async with lock:
        session_data = await _get_raw_session(account_email)
        if not session_data:
            return None

        # 节流：刚续过且 JWT 仍然新鲜则直接复用
        last = session_data.get("last_renew_ts", 0)
        if (time.time() - last) < RENEW_THROTTLE_SECONDS and not _session_needs_renewal(
            session_data
        ):
            return session_data

        # 1) 首选：长效 cookie 无密码滚动续期（轻量、不触发登录风控）。
        cookie_refreshed = await _refresh_session_via_cookie(account_email, session_data)
        if cookie_refreshed:
            # cookie 续期成功：重置密码失败计数，并在此统一落盘（单次 I/O）。
            cookie_refreshed.pop("pw_renew_fail_count", None)
            cookie_refreshed.pop("pw_renew_fail_ts", None)
            try:
                await _save_session(cookie_refreshed)
            except Exception as e:
                log.warning(
                    f"[Session] Failed to persist cookie-refreshed session for "
                    f"{account_email}: {e}"
                )
            return cookie_refreshed

        # 2) 兜底：用加密保存的密码重新登录。带退避，避免反复登录触发风控/锁号。
        password = await _decrypt_account_password(session_data)
        if not password:
            log.info(
                f"[Session] No long-lived cookie and no stored credentials for "
                f"{account_email}; cannot auto-renew"
            )
            return None

        last_pw_fail = session_data.get("pw_renew_fail_ts", 0)
        if not force_password and (time.time() - last_pw_fail) < PASSWORD_RENEW_BACKOFF_SECONDS:
            log.info(
                f"[Session] Skipping password renew for {account_email} (in backoff window)"
            )
            return None

        try:
            result, headers = await _authenticate_dashboard(account_email, password)
            fresh = _build_session_data_from_auth(account_email, result, headers)
            # 保留加密密码，便于持续续期；清除失败计数
            fresh["enc_password"] = session_data.get("enc_password")
            # 仅当新 JWT 能解析出 exp 时，才保留原始 logged_in_at（续期判定走
            # exp，不依赖登录时间）。若新 JWT 无 exp，_session_needs_renewal 会
            # 回退到 logged_in_at 估算，此时必须保留本次刷新的新鲜时间，否则刚
            # 续期的会话会被立刻判为陈旧，导致重复重新认证。
            if fresh.get("jwt_expires_at_ts") is not None:
                fresh["logged_in_at"] = session_data.get(
                    "logged_in_at", fresh["logged_in_at"]
                )
            fresh["last_renew_ts"] = time.time()
            await _save_session(fresh)
            log.info(f"[Session] Renewed via stored credentials for {account_email}")
            return fresh
        except HTTPException as e:
            log.warning(
                f"[Session] Credential renew failed for {account_email}: {e.detail}"
            )
            # 记录失败时间用于退避。仅在连续多次失败后才丢弃凭据，容忍偶发瞬时 401，
            # 避免一次抖动就永久关闭自动续期。保留会话本身——其长效 cookie 可能仍可
            # 用，留给真实请求在确实 401 时再清理。
            if e.status_code == 401 and session_data.get("enc_password"):
                fails = int(session_data.get("pw_renew_fail_count", 0)) + 1
                session_data["pw_renew_fail_count"] = fails
                session_data["pw_renew_fail_ts"] = time.time()
                if fails >= PASSWORD_RENEW_MAX_FAILURES:
                    session_data.pop("enc_password", None)
                    log.warning(
                        f"[Session] Dropping stored credentials for {account_email} "
                        f"after {fails} consecutive failures"
                    )
                try:
                    await _save_session(session_data)
                except Exception:
                    pass
        except Exception as e:
            log.warning(f"[Session] Credential renew error for {account_email}: {e}")
        return None


async def _ensure_fresh_session(
    account_email: str, session_data: Dict[str, Any]
) -> Dict[str, Any]:
    """保证 session 的 JWT 足够新鲜；必要时续期。返回可用的 session_data。

    若 JWT 已过期且无法续期，则标记 ``_jwt_stale`` 让请求改用 cookie 认证兜底。
    """
    if not _session_needs_renewal(session_data):
        return session_data
    # 有长效 cookie 时不必预先发一次续期请求：真实请求本身会以 cookie 认证发出，
    # 并捕获服务端滚动的 cookie 完成续期（见 _make_dashboard_request）。只需标记
    # JWT 陈旧让请求层退回纯 cookie 认证，避免一次查询触发两次上游请求。
    if session_data.get("aai_extended_session") or session_data.get("session_token"):
        session_data = dict(session_data)
        session_data["_jwt_stale"] = True
        return session_data
    # 无长效 cookie：只能用密码兜底续期以换取新的 JWT。
    renewed = await _renew_session(account_email)
    if renewed:
        return renewed
    # 续期失败：JWT 已陈旧，标记以便请求层退回纯 cookie 认证
    session_data = dict(session_data)
    session_data["_jwt_stale"] = True
    return session_data


async def _keepalive_loop() -> None:
    """后台保活：主动滚动所有 dashboard 账户的长效会话，避免空闲账户被动失效。

    关键修复：旧实现只对"已存密码"的账户做密码重新登录——既漏掉了仅有长效 cookie
    的账户（它们空闲时无人滚动 cookie，几天后失效），又对有密码的账户每 5 分钟用密码
    重登一次，长期高频登录触发风控、最终丢失凭据后同样失效。新实现以"无密码 cookie
    滚动"为主：只要账户有长效 cookie 或存有凭据，就在 JWT 临期或 cookie 久未滚动时
    续期（_renew_session 内部已 cookie 优先），从而让会话长期存活。
    """
    log.info("[Keepalive] Session keep-alive loop running")
    while True:
        try:
            await asyncio.sleep(KEEPALIVE_INTERVAL_SECONDS)
            accounts = await _get_accounts_list()
            now = time.time()
            for acc in accounts:
                email = acc.get("email")
                if not email:
                    continue
                session = await _get_raw_session(email)
                if not session or session.get("auth_type", "dashboard") != "dashboard":
                    continue
                has_cookie = bool(
                    session.get("aai_extended_session") or session.get("session_token")
                )
                renewable = has_cookie or session.get("enc_password")
                if not renewable:
                    continue
                # JWT 临期，或长效 cookie 久未滚动（即使 JWT 仍新鲜也定期滚动，使服务端
                # 会话永不进入"长时间无活动"状态而被回收）。
                cookie_due = (now - session.get("last_renew_ts", 0)) >= COOKIE_KEEPALIVE_INTERVAL_SECONDS
                if _session_needs_renewal(session) or cookie_due:
                    try:
                        await _renew_session(email)
                    except Exception as e:
                        log.warning(f"[Keepalive] Renew failed for {email}: {e}")
        except asyncio.CancelledError:
            log.info("[Keepalive] Session keep-alive loop stopped")
            break
        except Exception as e:
            log.error(f"[Keepalive] Loop error: {e}")
            await asyncio.sleep(30)


async def ensure_keepalive_running() -> None:
    """确保会话保活任务在运行（幂等）。"""
    global _keepalive_task
    if _keepalive_task and not _keepalive_task.done():
        return
    try:
        _keepalive_task = asyncio.create_task(_keepalive_loop())
        log.info("[Keepalive] Session keep-alive task started")
    except RuntimeError:
        # 无事件循环（非异步上下文）时跳过
        log.debug("[Keepalive] No running loop; keep-alive not started")


async def stop_keepalive() -> None:
    """停止会话保活任务（应用关闭时调用）。"""
    global _keepalive_task
    if _keepalive_task and not _keepalive_task.done():
        _keepalive_task.cancel()
        try:
            await _keepalive_task
        except (asyncio.CancelledError, Exception):
            pass
    _keepalive_task = None


def _prepare_dashboard_request(
    session: Dict[str, Any],
    path: str,
    params: Optional[Dict],
    *,
    allow_bearer: bool = True,
    rsc: bool = True,
):
    """构造 Dashboard 请求的 (url, headers)。

    ``allow_bearer=False`` 时不发送可能已失效的 sessionJWT Bearer，
    改为依赖长效 cookie（aai_extended_session / session_token）认证兜底。
    """
    auth_type = session.get("auth_type", "dashboard")
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        "Accept-Encoding": "gzip, deflate",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        "Origin": ASSEMBLY_DASHBOARD_BASE,
        "Referer": f"{ASSEMBLY_DASHBOARD_BASE}/dashboard/",
        "Sec-Ch-Ua": '"Chromium";v="142", "Google Chrome";v="142", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    if auth_type == "api_key":
        api_key = session.get("api_key")
        if not api_key:
            raise HTTPException(status_code=401, detail="Invalid session. Please login again.")
        headers["Authorization"] = api_key
        return f"https://api.assemblyai.com{path}", headers

    # dashboard 认证
    session_jwt = session.get("session_jwt")
    session_token = session.get("session_token")
    aai_extended_session = session.get("aai_extended_session")

    if session_jwt and allow_bearer:
        headers["Authorization"] = f"Bearer {session_jwt}"

    cookies = []
    if aai_extended_session:
        cookies.append(f"aai_extended_session={aai_extended_session}")
    if session_token:
        cookies.append(f"session_token={session_token}")
    # JWT 陈旧（allow_bearer=False）时连 cookie 里的 session_jwt 也一并丢弃，
    # 仅依赖长效 aai_extended_session / session_token，避免失效 JWT 触发 401。
    if session_jwt and allow_bearer:
        cookies.append(f"session_jwt={session_jwt}")
    if cookies:
        headers["Cookie"] = "; ".join(cookies)

    if path.startswith("/dashboard/api/"):
        headers["Accept"] = "application/json, text/plain, */*"
        headers.pop("RSC", None)
        headers.pop("Next-Url", None)
        headers.pop("X-Requested-With", None)
        headers.pop("priority", None)
        if path.startswith("/dashboard/api/accounts/"):
            headers["Referer"] = f"{ASSEMBLY_DASHBOARD_BASE}/dashboard/settings/billing"
        return f"{ASSEMBLY_DASHBOARD_BASE}{path}", headers

    if path.startswith("/dashboard/") and rsc:
        headers["Accept"] = "text/x-component"
        headers["RSC"] = "1"
        try:
            headers["Next-Url"] = _dashboard_next_url(path, params)
        except Exception:
            headers["Next-Url"] = _dashboard_next_url(path)
        headers.setdefault("priority", "u=1, i")
        headers["X-Requested-With"] = "NextJS-RSC"
        if path.endswith("/usage"):
            headers["Referer"] = f"{ASSEMBLY_DASHBOARD_BASE}/dashboard/usage"
        elif path.endswith("/code"):
            headers["Referer"] = f"{ASSEMBLY_DASHBOARD_BASE}/dashboard/code"
        elif path.endswith("/cost"):
            headers["Referer"] = f"{ASSEMBLY_DASHBOARD_BASE}/dashboard/cost"
        elif "/account/billing" in path:
            headers["Referer"] = f"{ASSEMBLY_DASHBOARD_BASE}/dashboard/account/billing"
        elif "/settings/billing" in path:
            headers["Referer"] = f"{ASSEMBLY_DASHBOARD_BASE}/dashboard/settings/billing"
        return f"{ASSEMBLY_DASHBOARD_BASE}{path}", headers

    if path.startswith("/dashboard/"):
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        headers.pop("RSC", None)
        headers.pop("X-Requested-With", None)
        if path.endswith("/usage"):
            headers["Referer"] = f"{ASSEMBLY_DASHBOARD_BASE}/dashboard/usage"
        elif path.endswith("/code"):
            headers["Referer"] = f"{ASSEMBLY_DASHBOARD_BASE}/dashboard/code"
        elif path.endswith("/cost"):
            headers["Referer"] = f"{ASSEMBLY_DASHBOARD_BASE}/dashboard/cost"
        elif "/account/billing" in path:
            headers["Referer"] = f"{ASSEMBLY_DASHBOARD_BASE}/dashboard/account/billing"
        elif "/settings/billing" in path:
            headers["Referer"] = f"{ASSEMBLY_DASHBOARD_BASE}/dashboard/settings/billing"
        return f"{ASSEMBLY_DASHBOARD_BASE}{path}", headers

    # 旧式 cookie 认证
    legacy_cookies = session.get("cookies", {})
    if legacy_cookies:
        headers["Cookie"] = "; ".join([f"{k}={v}" for k, v in legacy_cookies.items()])
    return f"{ASSEMBLY_DASHBOARD_BASE}{path}", headers


async def _make_dashboard_request(
    method: str,
    path: str,
    data: Optional[Dict] = None,
    params: Optional[Dict] = None,
    account_email: Optional[str] = None,
    rsc: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    发送 Dashboard API 请求
    
    Args:
        method: HTTP 方法 (GET, POST, etc.)
        path: API 路径
        data: POST 数据
        params: 查询参数
        account_email: 账户邮箱，如果为None则使用当前账户
        rsc: 是否按 Next.js RSC 请求发送。部分页面（billing）直接请求
            RSC 在缺少完整 router state 时会返回 _not-found。
    
    Returns:
        响应数据或 None
    """
    session = await _get_session(account_email)
    if not session:
        raise HTTPException(status_code=401, detail="Not logged in. Please login first.")

    resolved_email = account_email or session.get("email")

    # 主动续期：Stytch sessionJWT 仅 5 分钟寿命，过期前先刷新避免请求直接 401
    if session.get("auth_type", "dashboard") == "dashboard" and resolved_email:
        session = await _ensure_fresh_session(resolved_email, session)

    try:
        client = await _get_dashboard_client()

        async def _send(sess: Dict[str, Any]):
            # JWT 过期/被标记陈旧时退回纯 cookie 认证，避免发送失效 Bearer
            jwt_stale = bool(sess.get("_jwt_stale")) or (
                sess.get("auth_type", "dashboard") == "dashboard"
                and _session_needs_renewal(sess)
            )
            url, headers = _prepare_dashboard_request(
                sess,
                path,
                params,
                allow_bearer=not jwt_stale,
                rsc=rsc,
            )
            if method.upper() == "GET":
                return await client.get(url, headers=headers, params=params)
            elif method.upper() == "POST":
                return await client.post(url, headers=headers, json=data, params=params)
            raise ValueError(f"Unsupported method: {method}")

        resp = await _send(session)
        log.debug(f"Dashboard API {method} {path}: {resp.status_code}")
        log.debug(f"Dashboard API response headers: {_summarize_headers_for_log(resp.headers)}")

        # 401：先尝试续期并重试一次，续期失败才清除会话。这是用户主动发起的请求，
        # 允许绕过密码续期退避窗口做一次按需恢复（退避只用于给后台保活降频）。
        if resp.status_code == 401:
            renewed = (
                await _renew_session(resolved_email, force_password=True)
                if resolved_email
                else None
            )
            if renewed:
                log.info(f"[Session] Retrying {path} after renewal for {resolved_email}")
                session = renewed
                resp = await _send(session)
            if resp.status_code == 401:
                await _clear_session(resolved_email)
                raise HTTPException(
                    status_code=401, detail="Session expired. Please login again."
                )

        resp_url = getattr(resp, "url", None)
        final_path = str(getattr(resp_url, "path", ""))
        matched_path = resp.headers.get("x-matched-path", "")
        if final_path.endswith("/dashboard/login") or matched_path == "/login":
            log.warning(f"Dashboard request redirected to login for {path}")
            raise HTTPException(
                status_code=401,
                detail="Dashboard session expired or requested page requires login.",
            )

        if resp.status_code >= 400:
            log.error(f"Dashboard API error: {resp.status_code} - {resp.text[:500]}")
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"Dashboard API error: {resp.status_code}",
            )

        # 捕获滚动 cookie（aai_extended_session），延长持久会话
        try:
            if session.get("auth_type", "dashboard") == "dashboard" and _capture_rolling_cookies(
                session, resp
            ):
                session.pop("_jwt_stale", None)
                await _save_session(session)
        except Exception as e:
            log.debug(f"Rolling-cookie capture skipped: {e}")

        try:
            return resp.json()
        except Exception:
            # 可能是 RSC 格式，返回原始文本
            return {"raw": resp.text}

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Dashboard request failed: {e}")
        raise HTTPException(status_code=500, detail=f"Request failed: {str(e)}")


async def _fetch_dashboard_account_balance(account_email: Optional[str] = None) -> Optional[float]:
    """Fetch current balance from the Dashboard JSON API used by AssemblyAI."""
    data = await _make_dashboard_request(
        "GET",
        "/dashboard/api/accounts/balance",
        account_email=account_email,
        rsc=False,
    )
    return _extract_balance_api_amount(data)


async def _fetch_dashboard_billing_page(account_email: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Best-effort billing page scrape for spend trend and fallback balance."""
    page = await _make_dashboard_request(
        "GET",
        "/dashboard/settings/billing",
        account_email=account_email,
        rsc=False,
    )
    if not page or "raw" not in page:
        return None
    raw_text = page["raw"].strip()
    if not raw_text:
        return None
    parsed = _parse_billing_rsc_data(page)
    parsed["_source"] = "settings billing page"
    return parsed


@router.post("/login")
async def login(request: LoginRequest) -> Dict[str, Any]:
    """
    登录 AssemblyAI Dashboard
    
    使用邮箱和密码登录，成功后保存 session token。
    后续请求无需重复登录。
    
    AssemblyAI 使用 /dashboard/api/auth/authenticate 端点进行认证。
    """
    log.info(f"Attempting login for {request.email}")

    try:
        # 认证（与"密码兜底续期"复用同一实现）
        result, resp_headers = await _authenticate_dashboard(
            request.email, request.password
        )
        log.info("Using session JWT from login response directly")

        session_data = _build_session_data_from_auth(
            request.email, result, resp_headers
        )
        # 加密保存密码，支撑会话失效时的自动重新登录（续期兜底）
        await _attach_encrypted_password(session_data, request.password)
        session_data["last_renew_ts"] = time.time()

        email = session_data.get("email", request.email)
        if await _save_session(session_data):
            # 触发后台预加载（非阻塞）
            await _trigger_preload_after_login(email)
            # 确保后台保活任务在运行
            await ensure_keepalive_running()
            return {
                "success": True,
                "message": "Login successful",
                "email": email,
                "user_id": session_data.get("user_id"),
            }
        raise HTTPException(status_code=500, detail="Failed to persist session")
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        error_type = type(e).__name__
        error_msg = str(e) if str(e) else "Unknown error"
        log.error(f"Login failed: [{error_type}] {error_msg}")
        log.error(f"Login traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Login failed: [{error_type}] {error_msg}")


@router.post("/logout")
async def logout(account_email: Optional[str] = None) -> Dict[str, Any]:
    """登出并清除 session
    
    Args:
        account_email: 账户邮箱，如果为None则登出当前账户
    """
    # 清除预加载缓存
    await _clear_preload_cache(account_email)
    
    await _clear_session(account_email)
    return {"success": True, "message": "Logged out successfully"}


@router.get("/session")
async def get_session_info(account_email: Optional[str] = None) -> SessionInfo:
    """获取 session 状态

    Args:
        account_email: 账户邮箱，如果为None则获取当前账户
    """
    session = await _get_session(account_email)
    if session:
        return SessionInfo(
            email=session.get("email", ""),
            logged_in=True,
            expires_at=session.get("expires_at"),
            jwt_seconds_remaining=_jwt_seconds_remaining(session),
            auto_renew=bool(session.get("enc_password")),
        )
    return SessionInfo(email="", logged_in=False)


@router.post("/refresh")
async def refresh_session(account_email: Optional[str] = None) -> Dict[str, Any]:
    """主动续期账户会话（保活）。

    前端可定时调用以保持登录，避免 5 分钟 JWT 过期后被动失效。
    若 JWT 仍然新鲜则直接返回当前状态，不触发上游请求。
    """
    if not account_email:
        try:
            adapter = await get_storage_adapter()
            current = await adapter.get_config(CURRENT_ACCOUNT_KEY)
            if current:
                account_email = current.get("email")
        except Exception:
            pass

    if not account_email:
        raise HTTPException(status_code=401, detail="Not logged in. Please login first.")

    session = await _get_session(account_email)
    if not session:
        raise HTTPException(status_code=401, detail="Session not found. Please login again.")

    renewed = False
    cookie_fallback = False
    if _session_needs_renewal(session):
        fresh = await _renew_session(account_email)
        if fresh:
            session = fresh
            renewed = True
        elif session.get("aai_extended_session") or session.get("session_token"):
            # 无法主动续期（无凭据/旧会话），但仍有长效 cookie 可走兜底认证：
            # 不强制下线，交给真实请求在确实 401 时再清理，避免误判"已过期"。
            cookie_fallback = True
        else:
            # 既无法续期、也无可用的长效 cookie：清除死会话再 401，否则 /session
            # 仅看 7 天 expires_at，前端会把死账户当作已登录并重启保活循环。
            await _clear_session(account_email)
            raise HTTPException(
                status_code=401,
                detail="Session expired and could not be renewed. Please login again.",
            )

    return {
        "success": True,
        "renewed": renewed,
        "cookie_fallback": cookie_fallback,
        "email": session.get("email"),
        "jwt_seconds_remaining": _jwt_seconds_remaining(session),
        "auto_renew": bool(session.get("enc_password")),
        "expires_at": session.get("expires_at"),
    }


@router.get("/accounts")
async def get_accounts_list() -> Dict[str, Any]:
    """获取所有已登录的账户列表"""
    accounts = await _get_accounts_list()
    current_account = None
    try:
        adapter = await get_storage_adapter()
        current = await adapter.get_config(CURRENT_ACCOUNT_KEY)
        if current:
            current_account = current.get("email")
    except Exception:
        pass
    
    return {
        "accounts": accounts,
        "current_account": current_account,
    }


@router.post("/switch")
async def switch_account(request: SwitchAccountRequest) -> Dict[str, Any]:
    """切换当前查看的账户
    
    Args:
        request: 切换账户请求，包含email字段
    """
    email = request.email
    # 验证账户是否存在
    session = await _get_session(email)
    if not session:
        raise HTTPException(status_code=404, detail=f"Account {email} not found or expired")
    
    try:
        adapter = await get_storage_adapter()
        await adapter.set_config(CURRENT_ACCOUNT_KEY, {"email": email})
        
        # 清除新账户的缓存，确保切换后获取最新数据
        _cache_clear_for_account(email)
        
        # 重新排序预加载队列，将新账户提升到最高优先级
        await _reprioritize_preload(email)
        
        return {
            "success": True,
            "message": f"Switched to account {email}",
            "email": email,
        }
    except Exception as e:
        log.error(f"Failed to switch account: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to switch account: {str(e)}")


@router.get("/info")
async def get_account_info(account_email: Optional[str] = None) -> Dict[str, Any]:
    """
    获取账户基本信息（快速响应）
    
    Args:
        account_email: 账户邮箱，如果为None则获取当前账户
    
    返回账户 ID、邮箱、类型、创建时间、支付方式等基础信息。
    不包含 API key 信息，API key 通过 /api/account/api-keys 单独获取。
    """
    session = await _get_session(account_email)
    if not session:
        raise HTTPException(status_code=401, detail="Not logged in. Please login first.")
    
    # 优先使用保存的用户信息
    user_info = session.get("user_info")
    
    # 基础账户信息（快速响应，不请求外部 API）
    result = {
        "id": str((user_info or {}).get("id") or session.get("user_id") or ""),
        "email": str((user_info or {}).get("email") or session.get("email") or ""),
        "customer_type": str((user_info or {}).get("customer_type") or "PAYG"),
        "cc_brand": (user_info or {}).get("cc_brand"),
        "cc_last4": (user_info or {}).get("cc_last4"),
        "created": str((user_info or {}).get("created") or session.get("logged_in_at") or ""),
        "api_token": (user_info or {}).get("api_token") or session.get("api_token"),
    }
    
    return result


@router.get("/api-keys")
async def get_account_api_keys(force: bool = False, account_email: Optional[str] = None) -> Dict[str, Any]:
    """获取账户的 API key 列表"""
    session = await _get_session(account_email)
    if not session:
        raise HTTPException(status_code=401, detail="Not logged in. Please login first.")
    
    session_email = session.get("email") or account_email or "default"
    cache_key = f"api_keys:{session_email}"
    
    # 非强制刷新时使用缓存
    if not force:
        cached = _cache_get(cache_key)
        if cached:
            return cached
    
    # 从 session 获取基础 API token
    user_info = session.get("user_info", {})
    api_token = user_info.get("api_token") or session.get("api_token")
    
    result = {"projects": [], "api_keys": []}
    if api_token:
        result["api_keys"].append({
            "id": None, "project_id": None, "project_name": "Default",
            "api_key": api_token, "name": "Default API Key",
            "is_disabled": False, "created": session.get("logged_in_at"),
        })
    
    # 尝试从 Dashboard 获取完整列表（只尝试一个最可靠的端点）
    try:
        endpoint = "/dashboard/code"
        rsc = await _make_dashboard_request("GET", endpoint, params={"_rsc": "1"}, account_email=account_email)
        if rsc and "raw" in rsc:
            raw = rsc["raw"]
            log.info(f"Got RSC from {endpoint}, length={len(raw)}, has_tokens={'tokens' in raw}")
            projects = _parse_projects_from_rsc(raw)
            if projects:
                full_result = {"projects": projects, "api_keys": []}
                for project in projects:
                    for token in project.get("tokens", []):
                        full_result["api_keys"].append({
                            "id": token.get("id"),
                            "project_id": token.get("project_id"),
                            "project_name": project.get("project", {}).get("name"),
                            "api_key": token.get("api_key"),
                            "name": token.get("name"),
                            "is_disabled": token.get("is_disabled"),
                            "created": token.get("created"),
                        })
                if full_result["api_keys"]:
                    log.info(f"Fetched {len(full_result['api_keys'])} API keys for {session_email}")
                    _cache_set(cache_key, full_result)
                    return full_result
    except Exception as e:
        log.warning(f"Failed to fetch API keys from {endpoint}: {e}")
    
    _cache_set(cache_key, result)
    return result


@router.get("/overview")
async def get_account_overview(force: bool = False, account_email: Optional[str] = None) -> Dict[str, Any]:
    """
    获取账户概览数据（合并 info + billing + api-keys）
    
    一次请求返回概览页面需要的所有数据，减少请求数量，提高加载速度。
    支持预加载缓存优先模式。
    """
    session = await _get_session(account_email)
    if not session:
        raise HTTPException(status_code=401, detail="Not logged in. Please login first.")
    
    session_email = session.get("email") or account_email or "default"
    
    # 优先检查预加载缓存
    if not force:
        preload_cached = await _get_preload_cache(session_email, "overview")
        if preload_cached:
            preload_cached["_cache_source"] = "preload"
            preload_cached["_cached"] = True
            return preload_cached
    
    # 检查传统缓存
    cache_key = f"overview:{session_email}"
    if not force:
        cached = _cache_get(cache_key)
        if cached:
            cached["_cache_source"] = "legacy"
            cached["_cached"] = True
            return cached
    
    # 并发获取所有数据
    async def get_info():
        user_info = session.get("user_info", {})
        return {
            "id": str(user_info.get("id") or session.get("user_id") or ""),
            "email": str(user_info.get("email") or session.get("email") or ""),
            "customer_type": str(user_info.get("customer_type") or "PAYG"),
            "cc_brand": user_info.get("cc_brand"),
            "cc_last4": user_info.get("cc_last4"),
            "created": str(user_info.get("created") or session.get("logged_in_at") or ""),
        }
    
    async def get_billing():
        result = {
            "balance": 0.0,
            "total_spend_30_days": 0.0,
            "spend_trend": [],
            "balance_found": False,
        }
        errors = []

        try:
            balance = await _fetch_dashboard_account_balance(account_email)
            if balance is not None:
                result["balance"] = balance
                result["balance_found"] = True
        except Exception as e:
            errors.append(str(e))
            log.warning(f"Failed to get dashboard balance in overview: {e}")

        try:
            parsed = await _fetch_dashboard_billing_page(account_email)
            if parsed:
                if parsed.get("balance") is not None and not result["balance_found"]:
                    result["balance"] = parsed.get("balance") or 0.0
                    result["balance_found"] = True
                result["total_spend_30_days"] = parsed.get("total_spend_30_days") or 0.0
                result["spend_trend"] = parsed.get("spend_trend", [])
        except Exception as e:
            errors.append(str(e))
            log.warning(f"Failed to get settings billing page in overview: {e}")

        if not result["balance_found"]:
            result["error"] = errors[0] if errors else "Billing balance was not found in the dashboard response."
        return result
    
    async def get_api_keys():
        try:
            user_info = session.get("user_info", {})
            api_token = user_info.get("api_token") or session.get("api_token")
            
            result = []
            if api_token:
                result.append({
                    "id": None, "project_id": None, "project_name": "Default",
                    "api_key": api_token, "name": "Default API Key",
                    "is_disabled": False, "created": session.get("logged_in_at"),
                })
            
            # 尝试从 Dashboard 获取完整列表
            endpoint = "/dashboard/code"
            rsc = await _make_dashboard_request("GET", endpoint, params={"_rsc": "1"}, account_email=account_email)
            if rsc and "raw" in rsc:
                projects = _parse_projects_from_rsc(rsc["raw"])
                if projects:
                    result = []
                    for project in projects:
                        for token in project.get("tokens", []):
                            result.append({
                                "id": token.get("id"),
                                "project_id": token.get("project_id"),
                                "project_name": project.get("project", {}).get("name"),
                                "api_key": token.get("api_key"),
                                "name": token.get("name"),
                                "is_disabled": token.get("is_disabled"),
                                "created": token.get("created"),
                            })
            return result
        except Exception as e:
            log.warning(f"Failed to get api_keys in overview: {e}")
            return []
    
    # 并发执行
    info_task = asyncio.create_task(get_info())
    billing_task = asyncio.create_task(get_billing())
    api_keys_task = asyncio.create_task(get_api_keys())
    
    info, billing, api_keys = await asyncio.gather(info_task, billing_task, api_keys_task)
    
    result = {
        "info": info,
        "billing": billing,
        "api_keys": api_keys,
    }
    
    if billing.get("balance_found", True) and not billing.get("error"):
        _cache_set(cache_key, result)
    log.info(f"Overview data fetched for {session_email}: balance=${billing.get('balance', 0)}, {len(api_keys)} keys")
    return result


@router.get("/api-keys/debug")
async def debug_api_keys(account_email: Optional[str] = None) -> Dict[str, Any]:
    """调试端点：返回原始 RSC 数据"""
    session = await _get_session(account_email)
    if not session:
        raise HTTPException(status_code=401, detail="Not logged in")
    
    session_email = session.get("email") or account_email or "default"
    result = {"session_email": session_email, "endpoints": []}
    
    endpoints = [
        ("/dashboard/code", {"_rsc": "1"}),
        ("/dashboard/cost", {"_rsc": "125dm"}),
    ]
    
    for endpoint, params in endpoints:
        try:
            rsc = await _make_dashboard_request("GET", endpoint, params=params, account_email=account_email)
            if rsc and "raw" in rsc:
                raw = rsc["raw"]
                has_projects = '"projects"' in raw
                has_tokens = '"tokens"' in raw
                # 尝试找到 projects 或 tokens 的位置
                projects_pos = raw.find('"projects"')
                tokens_pos = raw.find('"tokens"')
                
                result["endpoints"].append({
                    "endpoint": endpoint,
                    "raw_length": len(raw),
                    "has_projects": has_projects,
                    "has_tokens": has_tokens,
                    "projects_pos": projects_pos,
                    "tokens_pos": tokens_pos,
                    "sample": raw[:1000] if len(raw) > 0 else "",
                    "around_projects": raw[max(0, projects_pos-50):projects_pos+200] if projects_pos >= 0 else "",
                    "around_tokens": raw[max(0, tokens_pos-50):tokens_pos+200] if tokens_pos >= 0 else "",
                })
            else:
                result["endpoints"].append({"endpoint": endpoint, "error": "No raw data"})
        except Exception as e:
            result["endpoints"].append({"endpoint": endpoint, "error": str(e)})
    
    return result


def _parse_projects_from_rsc(raw_text: str) -> List[Dict[str, Any]]:
    """
    从 RSC 数据中解析项目和 API key 信息
    
    格式: "projects":[{"project":{...},"tokens":[{...}]}]
    """
    import re
    
    projects = []
    
    try:
        # 方法1：查找 "projects":[ 开始的 JSON 数组
        start_marker = '"projects":['
        start_pos = raw_text.find(start_marker)
        
        if start_pos >= 0:
            # 找到 projects 数组的开始位置
            array_start = start_pos + len(start_marker) - 1  # 包含 [
            
            # 找到匹配的 ] 结束位置
            bracket_count = 0
            array_end = -1
            for i in range(array_start, min(array_start + 20000, len(raw_text))):
                if raw_text[i] == '[':
                    bracket_count += 1
                elif raw_text[i] == ']':
                    bracket_count -= 1
                    if bracket_count == 0:
                        array_end = i + 1
                        break
            
            if array_end > array_start:
                json_str = raw_text[array_start:array_end]
                try:
                    projects = json.loads(json_str)
                    # 统计解析到的 tokens 数量
                    total_tokens = sum(len(p.get("tokens", [])) for p in projects)
                    log.info(f"Parsed {len(projects)} projects with {total_tokens} tokens from RSC data")
                    return projects
                except json.JSONDecodeError as e:
                    log.warning(f"Failed to parse projects JSON: {e}, json_str length: {len(json_str)}")
        
        # 方法2：尝试查找 "tokens":[ 数组（备用方案）
        if not projects:
            tokens_marker = '"tokens":['
            tokens_pos = raw_text.find(tokens_marker)
            if tokens_pos >= 0:
                array_start = tokens_pos + len(tokens_marker) - 1
                bracket_count = 0
                array_end = -1
                for i in range(array_start, min(array_start + 10000, len(raw_text))):
                    if raw_text[i] == '[':
                        bracket_count += 1
                    elif raw_text[i] == ']':
                        bracket_count -= 1
                        if bracket_count == 0:
                            array_end = i + 1
                            break
                
                if array_end > array_start:
                    json_str = raw_text[array_start:array_end]
                    try:
                        tokens = json.loads(json_str)
                        if tokens:
                            # 构造一个虚拟的 project 结构
                            projects = [{"project": {"name": "Default"}, "tokens": tokens}]
                            log.info(f"Parsed {len(tokens)} tokens directly from RSC data (fallback)")
                            return projects
                    except json.JSONDecodeError as e:
                        log.warning(f"Failed to parse tokens JSON: {e}")
        
        if not projects:
            log.debug(f"No 'projects' or 'tokens' marker found in RSC data (length: {len(raw_text)})")
        
    except Exception as e:
        log.error(f"Failed to parse projects from RSC: {e}")
    
    return projects


@router.get("/billing")
async def get_billing_info(force: bool = False, account_email: Optional[str] = None) -> Dict[str, Any]:
    """
    获取账单信息
    
    Args:
        force: 是否强制刷新
        account_email: 账户邮箱，如果为None则获取当前账户
    
    返回余额、消费趋势等信息。
    使用 Metronome API 获取准确的账单数据。
    
    响应格式:
    {
        "balance": 58.49,  // 当前余额（美元）
        "total_spend_30_days": 1.65,  // 30天总消费
        "cost_breakdown": {...}  // 按服务分类的成本
    }
    """
    session = await _get_session(account_email)
    if not session:
        raise HTTPException(status_code=401, detail="Not logged in. Please login first.")
    
    # 获取 metronome_id
    user_info = session.get("user_info", {})
    metronome_id = user_info.get("metronome_id")
    
    # 使用账户邮箱作为缓存键的一部分，确保不同账户有独立的缓存
    session_email = session.get("email") or account_email or "default"
    cache_key = f"billing:{session_email}"
    cached = None if force else _cache_get(cache_key)
    if cached:
        return cached

    result = {
        "balance": 0.0,
        "total_spend_30_days": 0.0,
        "cost_breakdown": {},
        "spend_trend": [],
        "balance_found": False,
    }
    
    errors = []

    try:
        balance = await _fetch_dashboard_account_balance(account_email)
        if balance is not None:
            result["balance"] = balance
            result["balance_found"] = True
            result["debug_info"] = {"source": "dashboard api /accounts/balance"}
    except Exception as e:
        errors.append(str(e))
        log.warning(f"Failed to fetch dashboard balance API: {e}")

    try:
        best_result = await _fetch_dashboard_billing_page(account_email)

        if best_result:
            page_balance = best_result.get("balance")
            if page_balance is not None and not result["balance_found"]:
                result["balance"] = best_result.get("balance") or 0.0
                result["balance_found"] = True
            result["total_spend_30_days"] = best_result.get("total_spend_30_days") or 0.0
            result["spend_trend"] = best_result.get("spend_trend", [])
            if best_result.get("by_service"):
                result["cost_breakdown"] = {s.get("service"): s.get("cost", 0.0) for s in best_result.get("by_service", [])}
            result["debug_info"] = {
                "source": result.get("debug_info", {}).get("source")
                or best_result.get("_source", "unknown")
            }
            if result["balance_found"]:
                log.info(f"Parsed billing from dashboard: balance=${result['balance']}, spend=${result['total_spend_30_days']} (source: {result['debug_info']['source']})")
        elif result["balance_found"]:
            log.info(f"Parsed billing balance from dashboard API: balance=${result['balance']}")
        else:
            log.warning("No valid billing data from any source")

    except Exception as e:
        errors.append(str(e))
        log.warning(f"Failed to fetch settings billing page: {e}")

    if not result.get("balance_found"):
        result["error"] = errors[0] if errors else "Billing balance was not found in the dashboard response."
    
    if result.get("balance_found"):
        _cache_set(cache_key, result)
    return result


@router.get("/usage")
async def get_usage_data(
    window_size: str = "month",
    starting_on: Optional[str] = None,
    ending_before: Optional[str] = None,
    group_by: str = "model",
    product: Optional[str] = None,
    regions: Optional[str] = None,
    services: Optional[str] = None,
    force: bool = False,
    account_email: Optional[str] = None,
) -> Dict[str, Any]:
    """
    获取使用量数据（支持完整筛选参数）
    
    Args:
        window_size: 时间窗口 (day, week, month)
        starting_on: 开始日期 (YYYY-MM-DD)
        ending_before: 结束日期 (YYYY-MM-DD)
        group_by: 分组方式 (model, date, region)
        product: 产品类型 (如 "LLM Gateway + LeMUR")
        regions: 区域列表 (逗号分隔，如 US,Europe)
        services: 服务列表 (逗号分隔，如 API,Playground)
    
    Returns:
        使用量数据，包括总 tokens、按模型分类的使用量、按日期分组的数据等
    """
    # 获取 session 以确定账户邮箱
    session = await _get_session(account_email)
    session_email = (session.get("email") if session else None) or account_email or "default"
    
    # 检查是否使用默认参数（无筛选）
    is_default_params = (
        window_size == "month" and
        starting_on is None and
        ending_before is None and
        product is None and
        regions is None and
        services is None
    )
    
    # 优先检查预加载缓存（仅在默认参数且非强制刷新时）
    if not force and is_default_params:
        preload_cached = await _get_preload_cache(session_email, "usage")
        if preload_cached:
            preload_cached["_cache_source"] = "preload"
            preload_cached["_cached"] = True
            return preload_cached
    
    # 构建缓存键（包含所有筛选参数）
    filter_key = f"{window_size}:{starting_on}:{ending_before}:{product}:{regions}:{services}"
    cache_key = f"usage:{session_email}:{filter_key}"
    cached = None if force else _cache_get(cache_key)
    if cached:
        cached["_cache_source"] = "legacy"
        cached["_cached"] = True
        return cached

    result = {
        "total_tokens": 0,
        "items": [],
        "by_model": [],
        "segments": [],
        "daily_by_model": [],  # 按日期和模型分组的数据
        "filters_applied": {},
        "debug_info": {}
    }
    
    try:
        # 构建请求参数
        path = "/dashboard/usage"
        params = {"_rsc": "1mzsd"}
        
        # 时间窗口参数
        if starting_on and ending_before:
            params["window_size"] = window_size if window_size in ["day", "hour"] else "day"
            params["starting_on"] = starting_on
            params["ending_before"] = ending_before
            result["filters_applied"]["date_range"] = f"{starting_on} ~ {ending_before}"
            result["filters_applied"]["window_size"] = params["window_size"]
        
        # 产品筛选 (LLM Gateway + LeMUR)
        if product:
            params["product"] = product
            result["filters_applied"]["product"] = product
        
        # 区域筛选
        if regions:
            params["regions"] = regions
            result["filters_applied"]["regions"] = regions
        
        # 服务筛选
        if services:
            params["services"] = services
            result["filters_applied"]["services"] = services
        
        # 分组方式
        if group_by:
            params["group_by"] = group_by
            result["filters_applied"]["group_by"] = group_by
        
        log.info(f"Usage request params: {params}")
        rsc_data = await _make_dashboard_request("GET", path, params=params, account_email=account_email)
        
        final_result = None
        if rsc_data and "raw" in rsc_data:
            raw_text = rsc_data["raw"]
            if not (raw_text.strip().startswith("<!DOCTYPE") or raw_text.strip().startswith("<html")):
                final_result = _parse_usage_rsc_data(rsc_data)
                final_result["_source"] = f"{path} with {params}"
        
        if final_result:
            result["total_tokens"] = final_result.get("total_tokens", 0)
            result["items"] = final_result.get("items", [])
            result["by_model"] = final_result.get("by_model", [])
            result["segments"] = final_result.get("segments", [])
            result["daily_by_model"] = final_result.get("daily_by_model", [])
            result["debug_info"] = final_result.get("debug_info", {})
            result["debug_info"]["source"] = final_result.get("_source", "unknown")
            
            if "error" in final_result:
                result["error"] = final_result["error"]
            
            log.info(f"Parsed usage from RSC: {result['total_tokens']} tokens, {len(result['by_model'])} models, {len(result['segments'])} segments, {len(result['daily_by_model'])} daily points (source: {final_result.get('_source', 'unknown')})")
        else:
            log.warning("No valid usage data from any source")

    except Exception as e:
        log.error(f"Failed to fetch usage data: {e}")
        result["error"] = str(e)
        result["debug_info"]["exception"] = type(e).__name__
    
    _cache_set(cache_key, result)
    return result


async def _fetch_cost_data_internal(
    account_email: str,
    window_size: str = "month",
    force: bool = True
) -> Dict[str, Any]:
    """
    内部函数：获取成本数据（用于预加载，不需要 Request 对象）
    """
    session = await _get_session(account_email)
    session_email = (session.get("email") if session else None) or account_email or "default"
    
    cache_key = f"cost:{session_email}:{window_size}:None:None:None:None:None"
    
    cached = None if force else _cache_get(cache_key)
    if cached:
        return cached

    result = {
        "total_cost": 0.0,
        "items": [],
        "by_service": [],
        "by_model": [],
        "spend_trend": [],
        "daily_by_model": [],
        "filters_applied": {},
        "debug_info": {}
    }
    
    try:
        params = {"_rsc": "1mzsd"}
        if window_size and window_size != "month":
            params["window_size"] = window_size
            result["filters_applied"]["window_size"] = window_size
        
        rsc_data = await _make_dashboard_request(
            "GET", 
            "/dashboard/cost", 
            params=params,
            account_email=account_email
        )
        
        if rsc_data and "raw" in rsc_data:
            raw_text = rsc_data["raw"]
            if not (raw_text.strip().startswith("<!DOCTYPE") or raw_text.strip().startswith("<html")):
                if "chartExportData" in raw_text or '"data":' in raw_text or "total" in raw_text:
                    final_result = _parse_cost_rsc_data(rsc_data)
                    if final_result:
                        result["total_cost"] = final_result.get("total_cost", 0.0)
                        result["by_service"] = final_result.get("by_service", [])
                        result["by_model"] = final_result.get("by_model", [])
                        result["spend_trend"] = final_result.get("spend_trend", [])
                        result["daily_by_model"] = final_result.get("daily_by_model", [])
                        result["debug_info"]["source"] = "preload"
                        log.info(f"[Preload] Parsed cost: ${result['total_cost']}, {len(result['by_model'])} models")
    except Exception as e:
        log.warning(f"[Preload] Failed to fetch cost data: {e}")
        result["error"] = str(e)
    
    _cache_set(cache_key, result)
    return result


@router.get("/cost")
async def get_cost_data(
    request: Request,
    window_size: str = "month",
    starting_on: Optional[str] = None,
    ending_before: Optional[str] = None,
    group_by: str = "model",
    regions: Optional[str] = None,
    services: Optional[str] = None,
    models: Optional[List[str]] = Query(default=None),
    api_token: Optional[str] = None,
    force: bool = False,
    account_email: Optional[str] = None,
) -> Dict[str, Any]:
    """
    获取成本数据（支持完整筛选参数）
    
    Args:
        window_size: 时间窗口 (day, week, month, hour)
        starting_on: 开始日期 (YYYY-MM-DD)
        ending_before: 结束日期 (YYYY-MM-DD)
        group_by: 分组方式 (model, date, region)
        regions: 区域列表 (逗号分隔，如 US,Europe)
        services: 服务列表 (逗号分隔，如 API,Playground)
        models: 模型列表 (多个models参数，如 models=GPT+4.1+(Output)&models=Claude+3+Haiku+(Output))
        api_token: API Token ID (用于按项目筛选)
        force: 是否强制刷新
        account_email: 账户邮箱
    
    Returns:
        成本数据，包括总成本、按服务/模型分类的成本等
    """
    # 获取 session 以确定账户邮箱
    session = await _get_session(account_email)
    session_email = (session.get("email") if session else None) or account_email or "default"
    
    # 检查是否使用默认参数（无筛选）
    is_default_params = (
        window_size == "month" and
        starting_on is None and
        ending_before is None and
        regions is None and
        services is None and
        models is None and
        api_token is None
    )
    
    # 优先检查预加载缓存（仅在默认参数且非强制刷新时）
    if not force and is_default_params:
        preload_cached = await _get_preload_cache(session_email, "cost")
        if preload_cached:
            preload_cached["_cache_source"] = "preload"
            preload_cached["_cached"] = True
            return preload_cached
    
    # 构建缓存键（包含所有筛选参数）
    filter_key = f"{window_size}:{starting_on}:{ending_before}:{regions}:{services}:{api_token}"
    if models:
        filter_key += f":{','.join(sorted(models))}"
    cache_key = f"cost:{session_email}:{filter_key}"
    
    cached = None if force else _cache_get(cache_key)
    if cached:
        cached["_cache_source"] = "legacy"
        cached["_cached"] = True
        return cached

    result = {
        "total_cost": 0.0,
        "items": [],
        "by_service": [],
        "by_model": [],
        "spend_trend": [],
        "daily_by_model": [],
        "filters_applied": {},
        "debug_info": {}
    }
    
    try:
        # 构建请求参数
        params = {"_rsc": "1mzsd"}
        
        # 时间窗口参数
        if starting_on and ending_before:
            params["window_size"] = window_size if window_size in ["day", "hour"] else "day"
            params["starting_on"] = starting_on
            params["ending_before"] = ending_before
            result["filters_applied"]["date_range"] = f"{starting_on} ~ {ending_before}"
            result["filters_applied"]["window_size"] = params["window_size"]
        elif window_size and window_size != "month":
            params["window_size"] = window_size
            result["filters_applied"]["window_size"] = window_size
        
        # 区域筛选
        if regions:
            params["regions"] = regions
            result["filters_applied"]["regions"] = regions
        
        # 服务筛选
        if services:
            params["services"] = services
            result["filters_applied"]["services"] = services
        
        # API Token 筛选（按项目）
        if api_token:
            params["api_token"] = api_token
            result["filters_applied"]["api_token"] = api_token
        
        # 模型筛选（支持多个）
        # FastAPI 的 Query(default=None) 会自动处理多个同名参数为列表
        models_list = models if models else []
        
        if models_list:
            result["filters_applied"]["models"] = models_list
        
        log.info(f"Cost request params: {params}, models: {models_list}")
        
        # 发送请求到 AssemblyAI Dashboard
        rsc_data = await _make_dashboard_request(
            "GET", 
            "/dashboard/cost", 
            params=params,
            account_email=account_email
        )
        
        final_result = None
        if rsc_data and "raw" in rsc_data:
            raw_text = rsc_data["raw"]
            if not (raw_text.strip().startswith("<!DOCTYPE") or raw_text.strip().startswith("<html")):
                if "chartExportData" in raw_text or '"data":' in raw_text or "total" in raw_text:
                    final_result = _parse_cost_rsc_data(rsc_data)
                    final_result["_source"] = f"cost with {params}"
        
        if final_result:
            result["total_cost"] = final_result.get("total_cost", 0.0)
            result["by_service"] = final_result.get("by_service", [])
            result["by_model"] = final_result.get("by_model", [])
            result["spend_trend"] = final_result.get("spend_trend", [])
            result["daily_by_model"] = final_result.get("daily_by_model", [])
            result["debug_info"]["source"] = final_result.get("_source", "unknown")
            
            # 如果指定了模型筛选，在客户端进行过滤（因为 RSC 可能不支持完整的模型筛选）
            if models_list and result["by_model"]:
                # 将模型名称标准化用于匹配
                def normalize_model_name(name: str) -> str:
                    return name.lower().replace(" ", "").replace("-", "").replace("_", "")
                
                normalized_filters = set(normalize_model_name(m) for m in models_list)
                
                # 过滤模型数据
                filtered_models = []
                for model_data in result["by_model"]:
                    model_name = model_data.get("model", "")
                    # 检查是否匹配任何筛选条件
                    normalized_name = normalize_model_name(model_name)
                    for filter_name in normalized_filters:
                        # 支持部分匹配（去掉 Input/Output 后缀进行匹配）
                        base_filter = filter_name.replace("(input)", "").replace("(output)", "").strip()
                        if base_filter in normalized_name or normalized_name in base_filter:
                            filtered_models.append(model_data)
                            break
                
                if filtered_models:
                    result["by_model"] = filtered_models
                    result["total_cost"] = sum(m.get("cost", 0.0) for m in filtered_models)
            
            log.info(f"Parsed cost from RSC: ${result['total_cost']}, {len(result['by_model'])} models (source: {final_result.get('_source', 'unknown')})")
        else:
            log.warning("No valid cost data from any source")
    
    except Exception as e:
        log.error(f"Failed to fetch cost data: {e}")
        result["error"] = str(e)
    
    _cache_set(cache_key, result)
    return result


@router.get("/cost/filters")
async def get_cost_filters(account_email: Optional[str] = None) -> Dict[str, Any]:
    """
    获取成本筛选器的可用选项
    
    返回可用的服务、区域、模型列表，用于前端筛选器
    """
    session = await _get_session(account_email)
    if not session:
        raise HTTPException(status_code=401, detail="Not logged in. Please login first.")
    
    session_email = session.get("email") or account_email or "default"
    cache_key = f"cost_filters:{session_email}"
    
    cached = _cache_get(cache_key)
    if cached:
        return cached
    
    result = {
        "services": ["API", "Playground"],
        "regions": ["US", "Europe"],
        "window_sizes": [
            {"value": "hour", "label": "按小时"},
            {"value": "day", "label": "按天"},
            {"value": "week", "label": "按周"},
            {"value": "month", "label": "按月"},
        ],
        "model_categories": {
            "LLM Gateway (Input)": [],
            "LLM Gateway (Output)": [],
            "Speech-to-Text": ["Universal", "Slam-1", "Nano", "Best", "Conformer-2"],
            "Streaming Speech-to-Text": ["Universal-Streaming", "Nano Streaming", "Best Streaming"],
            "Speech Understanding": ["Summarization", "Sentiment Analysis", "Entity Detection", "Topic Detection", "Content Moderation", "PII Redaction", "Auto Chapters", "Translation"],
        },
        "models": []
    }
    
    # 尝试从成本数据中获取实际使用的模型列表
    try:
        params = {"_rsc": "1mzsd"}
        rsc_data = await _make_dashboard_request("GET", "/dashboard/cost", params=params, account_email=account_email)
        
        if rsc_data and "raw" in rsc_data:
            raw_text = rsc_data["raw"]
            if "chartExportData" in raw_text:
                parsed = _parse_cost_rsc_data(rsc_data)
                if parsed.get("by_model"):
                    # 提取所有模型名称
                    models_set = set()
                    input_models = []
                    output_models = []
                    
                    for model_data in parsed["by_model"]:
                        model_name = model_data.get("model", "")
                        if model_name:
                            models_set.add(model_name)
                            # 分类输入/输出模型
                            if model_data.get("input_cost", 0) > 0:
                                base_name = model_name
                                if base_name not in input_models:
                                    input_models.append(base_name)
                            if model_data.get("output_cost", 0) > 0:
                                base_name = model_name
                                if base_name not in output_models:
                                    output_models.append(base_name)
                    
                    result["models"] = sorted(list(models_set))
                    result["model_categories"]["LLM Gateway (Input)"] = sorted(input_models)
                    result["model_categories"]["LLM Gateway (Output)"] = sorted(output_models)
    except Exception as e:
        log.warning(f"Failed to fetch model list for filters: {e}")
    
    _cache_set(cache_key, result)
    return result


@router.get("/rates")
async def get_rates(region: str = "US", force: bool = False, account_email: Optional[str] = None) -> Dict[str, Any]:
    # 获取 session 以确定账户邮箱
    session = await _get_session(account_email)
    session_email = (session.get("email") if session else None) or account_email or "default"
    
    # 使用账户邮箱作为缓存键的一部分，确保不同账户有独立的缓存
    cache_key = f"rates:{region}:{session_email}"
    cached = None if force else _cache_get(cache_key)
    if cached:
        return cached

    parsed = {}
    fetch_error = None
    try:
        # 优先从 billing 页面获取费率数据（包含完整的费率表格）
        rsc = await _make_dashboard_request(
            "GET",
            "/dashboard/account/billing",
            params={"view": region},
            account_email=account_email,
            rsc=False,
        )
        parsed = _parse_rates_rsc_data(rsc)
        log.info(f"Parsed rates from billing API: {len(parsed.get('llm_gateway_input', []))} input, {len(parsed.get('llm_gateway_output', []))} output")
    except Exception as e:
        fetch_error = str(e)
        log.warning(f"Failed to fetch rates from API: {e}")
        parsed = {}

    official_error = None
    try:
        official_rates = await _fetch_official_pricing_page_rates()
        if official_rates:
            # 官方公开定价页是当前 LLM Gateway 费率的稳定来源；Dashboard
            # 登录页 RSC 结构经常变化，保留其语音类数据，但用官网 LLM 表覆盖。
            for key, values in official_rates.items():
                if values:
                    parsed[key] = values
            log.info(
                "Parsed rates from official pricing page: "
                f"{len(official_rates.get('llm_gateway_input', []))} input, "
                f"{len(official_rates.get('llm_gateway_output', []))} output"
            )
    except Exception as e:
        official_error = str(e)
        log.warning(f"Failed to fetch official pricing page rates: {e}")

    fallback = {
        "region": region,
        "speech_to_text": [
            {"model": "Slam-1", "rate": 0.27, "unit": "hour", "beta": True},
            {"model": "Universal", "rate": 0.15, "unit": "hour", "beta": True},
            {"model": "Nano", "rate": 0.12, "unit": "hour", "beta": True},
            {"model": "Best", "rate": 0.65, "unit": "hour", "beta": False},
            {"model": "Conformer-2", "rate": 0.37, "unit": "hour", "beta": False},
        ],
        "streaming": [
            {"model": "Universal Streaming", "rate": 0.30, "unit": "hour", "beta": True},
            {"model": "Nano Streaming", "rate": 0.24, "unit": "hour", "beta": True},
            {"model": "Best Streaming", "rate": 1.30, "unit": "hour", "beta": False},
        ],
        "speech_understanding": [
            {"feature": "Summarization", "rate": 0.10, "unit": "hour"},
            {"feature": "Sentiment Analysis", "rate": 0.05, "unit": "hour"},
            {"feature": "Entity Detection", "rate": 0.05, "unit": "hour"},
            {"feature": "Topic Detection", "rate": 0.05, "unit": "hour"},
            {"feature": "Content Moderation", "rate": 0.05, "unit": "hour"},
            {"feature": "PII Redaction", "rate": 0.05, "unit": "hour"},
            {"feature": "Auto Chapters", "rate": 0.10, "unit": "hour"},
        ],
        "llm_gateway_input": [
            # GPT / OpenAI
            {"model": "ChatGPT-4o", "rate": 5.00, "unit": "1M tokens"},
            {"model": "GPT 4.1", "rate": 2.00, "unit": "1M tokens"},
            {"model": "GPT-5", "rate": 1.25, "unit": "1M tokens"},
            {"model": "GPT-5-Mini", "rate": 0.25, "unit": "1M tokens"},
            {"model": "gpt-oss-120b", "rate": 0.15, "unit": "1M tokens"},
            {"model": "gpt-oss-20b", "rate": 0.07, "unit": "1M tokens"},
            {"model": "GPT-5 Nano", "rate": 0.05, "unit": "1M tokens"},
            # Claude
            {"model": "Claude 4 Opus", "rate": 15.00, "unit": "1M tokens"},
            {"model": "Claude 4.5 Sonnet", "rate": 3.00, "unit": "1M tokens"},
            {"model": "Claude 4 Sonnet", "rate": 3.00, "unit": "1M tokens"},
            {"model": "Claude 4.5 Haiku", "rate": 1.00, "unit": "1M tokens"},
            {"model": "Claude 3.5 Haiku", "rate": 0.80, "unit": "1M tokens"},
            {"model": "Claude 3 Haiku", "rate": 0.25, "unit": "1M tokens"},
            # Gemini
            {"model": "Gemini 3 Pro", "rate": 2.00, "unit": "1M tokens"},
            {"model": "Gemini 2.5 Pro", "rate": 1.25, "unit": "1M tokens"},
            {"model": "Gemini 2.5 Flash", "rate": 0.30, "unit": "1M tokens"},
        ],
        "llm_gateway_output": [
            # Claude
            {"model": "Claude 4 Opus", "rate": 75.00, "unit": "1M tokens"},
            {"model": "Claude 4.5 Sonnet", "rate": 15.00, "unit": "1M tokens"},
            {"model": "Claude 4 Sonnet", "rate": 15.00, "unit": "1M tokens"},
            {"model": "Claude 4.5 Haiku", "rate": 5.00, "unit": "1M tokens"},
            {"model": "Claude 3.5 Haiku", "rate": 4.00, "unit": "1M tokens"},
            {"model": "Claude 3 Haiku", "rate": 1.25, "unit": "1M tokens"},
            # GPT / OpenAI
            {"model": "ChatGPT-4o", "rate": 15.00, "unit": "1M tokens"},
            {"model": "GPT-5", "rate": 10.00, "unit": "1M tokens"},
            {"model": "GPT 4.1", "rate": 8.00, "unit": "1M tokens"},
            {"model": "GPT-5-Mini", "rate": 2.00, "unit": "1M tokens"},
            {"model": "gpt-oss-120b", "rate": 0.60, "unit": "1M tokens"},
            {"model": "GPT-5 Nano", "rate": 0.40, "unit": "1M tokens"},
            {"model": "gpt-oss-20b", "rate": 0.30, "unit": "1M tokens"},
            # Gemini
            {"model": "Gemini 3 Pro", "rate": 12.00, "unit": "1M tokens"},
            {"model": "Gemini 2.5 Pro", "rate": 10.00, "unit": "1M tokens"},
            {"model": "Gemini 2.5 Flash", "rate": 2.50, "unit": "1M tokens"},
        ],
        "notes": [
            "LLM Gateway pricing is per million tokens",
        ],
    }

    # 导入价格格式化函数
    from src.core.price_formatter import format_price
    
    # 格式化所有价格，添加 display 字段
    def add_price_display(items, region):
        """为每个项目添加格式化后的价格显示"""
        for item in items:
            if "rate" in item:
                item["rate_display"] = format_price(item["rate"], region)
        return items

    def mark_price_source(items, source):
        result = []
        for item in items or []:
            new_item = dict(item)
            new_item["price_source"] = new_item.get("price_source") or source
            result.append(new_item)
        return result

    def choose_rates_with_source(api_rates, fallback_rates):
        if api_rates:
            return mark_price_source(api_rates, "dashboard")
        return mark_price_source(fallback_rates, "fallback")
    
    def merge_rates_with_fallback(api_rates, fallback_rates):
        """
        合并 API 返回的费率与 fallback 费率
        - 保留 API 返回的所有数据
        - 对于 API 中没有的模型，从 fallback 中补充
        - 使用别名映射处理同一模型的不同名称
        - 使用规范显示名称
        """
        # 模型别名映射（将各种名称映射到规范名称）
        MODEL_ALIASES = {
            # ChatGPT-4o 系列别名
            "chatgpt4olatest": "chatgpt4o",
            "chatgpt4o": "chatgpt4o",
            # GPT OSS 系列别名（标准化后的键）
            "gptoss120b": "gptoss120b",
            "gptoss20b": "gptoss20b",
        }
        
        # 规范显示名称（使用更美观的格式）
        CANONICAL_DISPLAY_NAMES = {
            "chatgpt4o": "ChatGPT 4o",
            "gptoss120b": "GPT OSS 120b",
            "gptoss20b": "GPT OSS 20b",
            "gpt5": "GPT 5",
            "gpt5mini": "GPT 5 Mini",
            "gpt5nano": "GPT 5 Nano",
            "gpt41": "GPT 4.1",
            "claude4opus": "Claude 4 Opus",
            "claude45sonnet": "Claude 4.5 Sonnet",
            "claude4sonnet": "Claude 4 Sonnet",
            "claude45haiku": "Claude 4.5 Haiku",
            "claude35haiku": "Claude 3.5 Haiku",
            "claude3haiku": "Claude 3 Haiku",
            "gemini3pro": "Gemini 3 Pro",
            "gemini25pro": "Gemini 2.5 Pro",
            "gemini25flash": "Gemini 2.5 Flash",
        }
        
        def normalize_model_name(name):
            """标准化模型名称用于匹配：小写，移除空格、连字符、下划线"""
            normalized = name.lower().replace(" ", "").replace("-", "").replace("_", "")
            # 应用别名映射
            return MODEL_ALIASES.get(normalized, normalized)
        
        def get_display_name(normalized_key, original_name):
            """获取规范显示名称"""
            return CANONICAL_DISPLAY_NAMES.get(normalized_key, original_name)
        
        if not api_rates:
            # 如果 API 没有数据，使用 fallback 并应用规范显示名称
            result = []
            for item in fallback_rates:
                new_item = dict(item)
                normalized = normalize_model_name(item.get("model", ""))
                new_item["model"] = get_display_name(normalized, item.get("model", ""))
                new_item["price_source"] = "fallback"
                result.append(new_item)
            return result
        
        # 创建 API 数据的模型名索引（标准化后）
        api_models = {}
        for item in api_rates:
            normalized = normalize_model_name(item.get("model", ""))
            api_models[normalized] = item
        
        # 合并结果，应用规范显示名称
        result = []
        for item in api_rates:
            new_item = dict(item)
            normalized = normalize_model_name(item.get("model", ""))
            new_item["model"] = get_display_name(normalized, item.get("model", ""))
            new_item["price_source"] = new_item.get("price_source") or "dashboard"
            result.append(new_item)
        
        # 从 fallback 中补充 API 没有的模型
        for fb_item in fallback_rates:
            fb_normalized = normalize_model_name(fb_item.get("model", ""))
            if fb_normalized and fb_normalized not in api_models:
                new_item = dict(fb_item)
                new_item["model"] = get_display_name(fb_normalized, fb_item.get("model", ""))
                new_item["price_source"] = "fallback"
                result.append(new_item)
        
        return result
    
    result = {"region": region}
    result["speech_to_text"] = add_price_display(
        choose_rates_with_source(parsed.get("speech_to_text"), fallback["speech_to_text"]), region
    )
    result["streaming"] = add_price_display(
        choose_rates_with_source(parsed.get("streaming"), fallback["streaming"]), region
    )
    result["speech_understanding"] = add_price_display(
        choose_rates_with_source(parsed.get("speech_understanding"), fallback["speech_understanding"]), region
    )
    # LLM Gateway: 合并 API 数据与 fallback，补充缺失的模型
    result["llm_gateway_input"] = add_price_display(
        merge_rates_with_fallback(parsed.get("llm_gateway_input"), fallback["llm_gateway_input"]), region
    )
    result["llm_gateway_output"] = add_price_display(
        merge_rates_with_fallback(parsed.get("llm_gateway_output"), fallback["llm_gateway_output"]), region
    )
    result["notes"] = fallback["notes"]

    categories = [
        "speech_to_text",
        "streaming",
        "speech_understanding",
        "llm_gateway_input",
        "llm_gateway_output",
    ]
    dashboard_counts = {
        key: sum(1 for item in (parsed.get(key) or []) if item.get("price_source") in (None, "dashboard"))
        for key in categories
    }
    official_counts = {
        key: sum(1 for item in (parsed.get(key) or []) if item.get("price_source") == "official_pricing")
        for key in categories
    }
    dashboard_count = sum(dashboard_counts.values())
    official_count = sum(official_counts.values())
    parsed_count = dashboard_count + official_count
    fallback_count = sum(
        1
        for key in categories
        for item in result.get(key, [])
        if item.get("price_source") == "fallback"
    )
    if parsed_count and fallback_count:
        source = "mixed"
    elif official_count and dashboard_count:
        source = "mixed"
    elif official_count:
        source = "official_pricing"
    elif dashboard_count:
        source = "dashboard"
    else:
        source = "fallback"

    warnings = []
    if source == "fallback":
        warnings.append("未能从 AssemblyAI 官方定价页或 Dashboard 解析到实时费率，当前展示内置备用费率，可能滞后。")
    elif source == "mixed":
        warnings.append("部分模型未在 AssemblyAI 官方来源中解析到，已使用内置备用费率补齐。")
    if fetch_error and not official_count:
        warnings.append("Dashboard 费率请求失败，已回退到备用费率。")
    elif fetch_error:
        warnings.append("Dashboard 费率请求失败，已使用官方公开定价页和备用费率补齐。")
    if official_error and not parsed_count:
        warnings.append("AssemblyAI 官方定价页请求失败，已回退到备用费率。")
    elif official_error:
        warnings.append("AssemblyAI 官方定价页请求失败，已使用 Dashboard 和备用费率补齐。")

    result["metadata"] = {
        "source": source,
        "dashboard_counts": dashboard_counts,
        "dashboard_count": dashboard_count,
        "official_counts": official_counts,
        "official_count": official_count,
        "fallback_count": fallback_count,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "cache_ttl_seconds": _cache_ttl_seconds,
    }
    result["warnings"] = warnings

    _cache_set(cache_key, result)
    return result


def _parse_rsc_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    解析 React Server Component (RSC) 格式数据
    
    RSC 数据格式通常是多行 JSON，每行以数字开头
    """
    if "raw" not in data:
        return data
    
    raw_text = data["raw"]
    result = {}
    
    try:
        lines = raw_text.split("\n")
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # RSC 格式: 数字:JSON 或直接 JSON
            if ":" in line and line[0].isdigit():
                # 找到第一个 : 后的内容
                colon_idx = line.index(":")
                json_part = line[colon_idx + 1:]
            else:
                json_part = line
            
            # 尝试解析 JSON
            try:
                if json_part.startswith("{") or json_part.startswith("["):
                    parsed = json.loads(json_part)
                    if isinstance(parsed, dict):
                        result.update(parsed)
                    elif isinstance(parsed, list):
                        if "items" not in result:
                            result["items"] = []
                        result["items"].extend(parsed)
            except json.JSONDecodeError:
                continue
    except Exception as e:
        log.warning(f"Failed to parse RSC data: {e}")
        result["raw_data"] = raw_text[:1000]
    
    return result


def _extract_rsc_json_values(raw_text: str) -> List[Any]:
    """Parse JSON payloads embedded in a Next.js RSC response."""
    values: List[Any] = []

    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue

        json_part = line
        if not line.startswith(("{", "[", '"')):
            prefix, sep, rest = line.partition(":")
            if sep and prefix.isalnum():
                json_part = rest.strip()

        if json_part:
            json_part = json_part.lstrip("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")

        if not json_part or not json_part.startswith(("{", "[", '"')):
            continue

        try:
            values.append(json.loads(json_part))
        except json.JSONDecodeError:
            continue

    return values


def _coerce_amount(value: Any) -> Optional[float]:
    """Convert a dashboard money-like value to float without logging raw input."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    import re

    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _first_amount_in_value(value: Any) -> Optional[float]:
    """Find the first amount in a structured balance object."""
    amount = _coerce_amount(value)
    if amount is not None:
        return amount

    if isinstance(value, dict):
        preferred_keys = (
            "amount",
            "balance",
            "current_balance",
            "currentBalance",
            "remaining_balance",
            "remainingBalance",
            "available_balance",
            "availableBalance",
            "value",
        )
        for key in preferred_keys:
            if key in value:
                amount = _first_amount_in_value(value.get(key))
                if amount is not None:
                    return amount

        for nested in value.values():
            amount = _first_amount_in_value(nested)
            if amount is not None:
                return amount

    if isinstance(value, list):
        for item in value:
            amount = _first_amount_in_value(item)
            if amount is not None:
                return amount

    return None


def _extract_balance_api_amount(value: Any) -> Optional[float]:
    """Extract balance from the Dashboard /api/accounts/balance response."""
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float, str)):
        return _coerce_amount(value)

    preferred_keys = (
        "balance",
        "current_balance",
        "currentBalance",
        "available_balance",
        "availableBalance",
        "remaining_balance",
        "remainingBalance",
        "credit_balance",
        "creditBalance",
    )
    container_keys = (
        "data",
        "account",
        "billing",
        "billing_summary",
        "billingSummary",
        "result",
    )

    if isinstance(value, dict):
        for key in preferred_keys:
            if key in value:
                amount = _first_amount_in_value(value.get(key))
                if amount is not None:
                    return amount

        for key in container_keys:
            if key in value:
                amount = _extract_balance_api_amount(value.get(key))
                if amount is not None:
                    return amount

        return _extract_structured_balance([value])

    if isinstance(value, list):
        amount = _extract_structured_balance(value)
        if amount is not None:
            return amount
        for item in value:
            amount = _extract_balance_api_amount(item)
            if amount is not None:
                return amount

    return None


def _extract_structured_balance(values: List[Any]) -> Optional[float]:
    """Extract balance from JSON objects with balance-ish keys."""
    precise_balance_key_fragments = (
        "currentbalance",
        "availablebalance",
        "remainingbalance",
        "creditbalance",
    )
    reject_key_fragments = ("card", "threshold")
    candidates: List[tuple[int, int, float]] = []
    order = 0

    def key_score(normalized_key: str) -> int:
        if any(fragment in normalized_key for fragment in precise_balance_key_fragments):
            return 100
        if "balance" in normalized_key:
            return 50
        return 0

    def walk(value: Any) -> None:
        nonlocal order
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized_key = str(key).replace("_", "").replace("-", "").lower()
                score = key_score(normalized_key)
                if score and not any(fragment in normalized_key for fragment in reject_key_fragments):
                    amount = _first_amount_in_value(nested)
                    if amount is not None:
                        candidates.append((score, order, amount))
                        order += 1

            for nested in value.values():
                walk(nested)

        if isinstance(value, list):
            for item in value:
                walk(item)

    for value in values:
        walk(value)
    if candidates:
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return candidates[0][2]
    return None


def _flatten_rsc_text(values: List[Any]) -> str:
    """Flatten display strings from parsed RSC JSON values."""
    parts: List[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            if value and value != "$":
                parts.append(value)
            elif value == "$":
                parts.append(value)
            return
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parts.append(str(value))
            return
        if isinstance(value, dict):
            for nested in value.values():
                walk(nested)
            return
        if isinstance(value, list):
            for item in value:
                walk(item)

    for value in values:
        walk(value)

    return " ".join(parts)


def _money_amounts_with_positions(text: str) -> List[tuple[int, float]]:
    import re

    amounts: List[tuple[int, float]] = []
    money_patterns = [
        r"(?:US\$|\${1,2})\s*([0-9][\d,]*(?:\.\d+)?)",
        r"([0-9][\d,]*(?:\.\d+)?)\s*(?:USD|usd)\b",
    ]
    for pattern in money_patterns:
        for match in re.finditer(pattern, text):
            amount = _coerce_amount(match.group(1))
            if amount is not None:
                amounts.append((match.start(), amount))

    amounts.sort(key=lambda item: item[0])
    return amounts


def _extract_labeled_balance(raw_text: str, values: List[Any]) -> Optional[float]:
    import re

    text_sources = [_flatten_rsc_text(values), raw_text]
    specific_pattern = re.compile(
        r"current\s*balance|available\s*balance|remaining\s*balance|credit\s*balance|账户余额|当前余额",
        re.IGNORECASE,
    )
    generic_pattern = re.compile(r"\bbalance\b|余额", re.IGNORECASE)

    for pattern in (specific_pattern, generic_pattern):
        for source in text_sources:
            text = re.sub(r"\s+", " ", source)
            if not text:
                continue

            for label in pattern.finditer(text):
                after = text[label.end(): label.end() + 300]
                amounts = _money_amounts_with_positions(after)
                if amounts:
                    return amounts[0][1]

                before = text[max(0, label.start() - 160): label.start()]
                amounts = _money_amounts_with_positions(before)
                if amounts:
                    return amounts[-1][1]

    return None



def _parse_billing_rsc_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    专门解析 billing 页面的 RSC 数据，提取余额和消费趋势
    
    RSC 响应中的关键数据：
    - 余额: 格式如 '$58.49928' 或 '$$58.49928' 在 span 元素中
    - 消费趋势: PreviewChart 组件的 data 属性
    """
    import re
    
    if "raw" not in data:
        return data
    
    raw_text = data["raw"]
    result = {
        "balance": None,
        "spend_trend": [],
        "total_spend_30_days": 0.0,
        "by_service": [],
        "raw_parsed": False
    }
    
    try:
        log.debug(f"Parsing billing RSC data, length: {len(raw_text)}")

        rsc_values = _extract_rsc_json_values(raw_text)

        # 1. 提取余额 - 优先解析结构化 balance 字段和带标签的金额，避免把
        #    页面其他 $0.00000 指标误当成余额。
        balance = _extract_structured_balance(rsc_values)
        if balance is None:
            balance = _extract_labeled_balance(raw_text, rsc_values)
        if balance is None:
            # 兼容旧格式：billing 页面只有一个 "$58.49928" / "$$58.49928" 文本。
            balance_matches = _money_amounts_with_positions(raw_text)
            if balance_matches:
                balance = balance_matches[0][1]

        if balance is not None:
            result["balance"] = balance
            log.info(f"Extracted balance: ${result['balance']}")
        else:
            log.warning("No balance found in RSC data")
        
        # 2. 提取消费趋势数据 - 查找 PreviewChart 的 data 属性
        # 格式: ["$","$L64",null,{"data":[{"name":"2025-10-26T00:00:00.000Z","value":0},...]}]
        chart_data_pattern = r'"data":\s*\[((?:\{[^}]+\},?\s*)+)\]'
        chart_matches = re.findall(chart_data_pattern, raw_text)
        
        for match in chart_matches:
            try:
                json_str = f"[{match}]"
                chart_data = json.loads(json_str)
                if chart_data and isinstance(chart_data, list):
                    first_items = chart_data[:3] if len(chart_data) >= 3 else chart_data
                    if all("name" in item and "value" in item for item in first_items):
                        spend = []
                        for item in chart_data:
                            try:
                                v = item.get("value", 0)
                                amount = float(v)
                            except Exception:
                                amount = 0.0
                            spend.append({
                                "date": item.get("name", ""),
                                "amount": amount
                            })
                        total = sum(s.get("amount", 0.0) for s in spend)
                        if total > 20:
                            for s in spend:
                                s["amount"] = round(s["amount"] / 100.0, 8)
                            total = sum(s.get("amount", 0.0) for s in spend)
                        result["spend_trend"] = spend
                        log.info(f"Extracted spend trend: {len(spend)} data points")
                        break
            except (json.JSONDecodeError, ValueError) as e:
                log.debug(f"Failed to parse chart data: {e}")
                continue
        
        # 3. 提取总额按服务分类（如果存在）
        try:
            m_total = re.search(r'"total":\s*\{([^}]+)\}', raw_text)
            if m_total:
                import json as _json
                json_str = '{' + m_total.group(1) + '}'
                total_obj = _json.loads(json_str)
                by_service = []
                for k, v in total_obj.items():
                    name = str(k)
                    try:
                        amount = float(v)
                    except Exception:
                        amount = 0.0
                    by_service.append({
                        "service": name,
                        "cost": amount,
                    })
                if by_service:
                    result["by_service"] = by_service
        except Exception:
            pass

        # 4. 计算 30 天总消费
        if result["spend_trend"]:
            result["total_spend_30_days"] = sum(
                item.get("amount", 0.0) for item in result["spend_trend"]
            )
            log.info(f"Total 30-day spend: ${result['total_spend_30_days']:.5f}")
        
        result["raw_parsed"] = True
        
    except Exception as e:
        log.warning(f"Failed to parse billing RSC data: {e}")
        result["error"] = str(e)
    
    return result

def _sanitize_model_name(model_name: str) -> str:
    """
    清理和验证模型名称，防止 XSS 和编码问题
    
    Args:
        model_name: 原始模型名称
    
    Returns:
        清理后的模型名称
    """
    if not model_name:
        return ""
    
    # 移除潜在的 HTML 标签
    import html
    sanitized = html.escape(model_name)
    
    # 限制长度
    max_length = 200
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length] + "..."
    
    return sanitized


def _normalize_usage_token_value(value: Any) -> int:
    """
    Convert Dashboard usage quantities to integer tokens.

    AssemblyAI's usage UI can expose LLM Gateway usage as fractional millions
    (for example ``0.004`` means 4,000 tokens). Token counts themselves are
    integral, so decimal literals from the Dashboard usage view are scaled by
    1M while integer-looking values such as ``12,089`` are preserved.
    """
    import math
    import re

    if value is None or isinstance(value, bool):
        return 0

    decimal_literal = False
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        match = re.search(r"-?\d[\d,]*(?:\.\d+)?", text)
        if not match:
            return 0
        numeric_text = match.group(0).replace(",", "")
        decimal_literal = "." in numeric_text
        try:
            amount = float(numeric_text)
        except ValueError:
            return 0
    elif isinstance(value, int):
        amount = float(value)
    elif isinstance(value, float):
        amount = value
        decimal_literal = True
    else:
        return 0

    if not math.isfinite(amount) or amount <= 0:
        return 0

    if decimal_literal:
        return int(round(amount * 1_000_000))
    return int(amount)


def _normalize_usage_result_tokens(result: Dict[str, Any]) -> None:
    """Normalize all token-valued fields in a parsed usage result in place."""
    for item in result.get("by_model") or []:
        item["tokens"] = _normalize_usage_token_value(item.get("tokens", 0))
        if "input_tokens" in item:
            item["input_tokens"] = _normalize_usage_token_value(item.get("input_tokens", 0))
        if "output_tokens" in item:
            item["output_tokens"] = _normalize_usage_token_value(item.get("output_tokens", 0))

    for segment in result.get("segments") or []:
        segment["value"] = _normalize_usage_token_value(segment.get("value", 0))

    for item in result.get("items") or []:
        if "tokens" in item:
            item["tokens"] = _normalize_usage_token_value(item.get("tokens", 0))
        if "usage" in item:
            item["usage"] = _normalize_usage_token_value(item.get("usage", 0))

    for day in result.get("daily_by_model") or []:
        models = day.get("models")
        if not isinstance(models, dict):
            continue
        for model_values in models.values():
            if isinstance(model_values, dict):
                input_tokens = _normalize_usage_token_value(model_values.get("input", 0))
                output_tokens = _normalize_usage_token_value(model_values.get("output", 0))
                if "total" in model_values and not (input_tokens or output_tokens):
                    total_tokens = _normalize_usage_token_value(model_values.get("total", 0))
                else:
                    total_tokens = input_tokens + output_tokens
                model_values["input"] = input_tokens
                model_values["output"] = output_tokens
                model_values["total"] = total_tokens

    if result.get("by_model"):
        result["total_tokens"] = sum(item.get("tokens", 0) for item in result.get("by_model") or [])
    elif result.get("segments"):
        result["total_tokens"] = sum(item.get("value", 0) for item in result.get("segments") or [])
    else:
        result["total_tokens"] = _normalize_usage_token_value(result.get("total_tokens", 0))


def _extract_segments_from_rsc(raw_text: str) -> List[Dict[str, Any]]:
    """
    从 RSC 数据中提取 segments 数组
    
    Segments 通常在组件 props 中，格式如：
    ["$","$L14",null,{"size":"1","segments":[{"value":22449,"color":"var(--blue-10)"},...]}]
    
    Args:
        raw_text: RSC 原始文本
    
    Returns:
        Segments 列表，每个包含 value 和 color
    """
    import re
    
    segments = []
    
    try:
        # 方法1: 查找完整的 segments 数组（更精确的匹配）
        # 匹配: "segments":[{...},{...}]
        segments_pattern = r'"segments":\s*\[((?:\{[^}]+\},?\s*)+)\]'
        segments_matches = re.findall(segments_pattern, raw_text)
        
        for match in segments_matches:
            try:
                # 添加数组括号
                json_str = f"[{match}]"
                parsed_segments = json.loads(json_str)
                
                # 验证是否是有效的 segments 数据（包含 value 字段）
                if parsed_segments and isinstance(parsed_segments, list) and len(parsed_segments) > 0:
                    first_item = parsed_segments[0] if parsed_segments else {}
                    if isinstance(first_item, dict) and "value" in first_item:
                        segments = parsed_segments
                        log.info(f"Extracted {len(segments)} segments from RSC data (method 1)")
                        break
            except json.JSONDecodeError as e:
                log.debug(f"JSON decode failed for segments: {e}")
                continue
        
        # 方法2: 如果方法1失败，尝试查找独立的 segment 对象
        if not segments:
            segment_pattern = r'\{"value":\s*([0-9][\d,]*(?:\.\d+)?),\s*"color":\s*"([^"]+)"\}'
            segment_matches = re.findall(segment_pattern, raw_text)
            
            if segment_matches:
                segments = [
                    {"value": _normalize_usage_token_value(value), "color": color}
                    for value, color in segment_matches
                ]
                log.info(f"Extracted {len(segments)} segments using fallback method (method 2)")
    
    except Exception as e:
        log.error(f"Failed to extract segments: {e}")
    
    return segments


def _extract_model_names_from_rsc(raw_text: str) -> List[Dict[str, Any]]:
    """
    从 RSC 数据中提取模型名称和对应的 token 数量
    
    模型信息通常在 div 元素中，格式如：
    ["$","div","LLM Gateway + LeMUR-GPT 5 Mini",{"children":[...]}]
    或在 span 中：
    ["$","span",null,{"children":[...," ","GPT 5 Mini"]}]
    
    Args:
        raw_text: RSC 原始文本
    
    Returns:
        模型列表，每个包含 model 和 tokens
    """
    import re

    import json
    
    models = []
    
    # 将解析范围尽量限定在目标视图区域（例如 granularity=day 的 UsageChart），避免误取 30 天摘要卡片
    scan_text = raw_text
    try:
        pos = raw_text.find('"granularity":"day"')
        if pos >= 0:
            # 只扫描该区域后面的内容，降低误匹配概率
            scan_text = raw_text[pos: pos + 60000]
    except Exception:
        scan_text = raw_text
    
    # 方法1: 尝试按行解析 JSON (针对 RSC 流式响应)
    try:
        lines = scan_text.split('\n')
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # 提取 JSON 部分 (处理 "1a:..." 格式)
            json_str = line
            if ':' in line and line[0].isdigit():
                parts = line.split(':', 1)
                if len(parts) > 1:
                    json_str = parts[1]
            
            try:
                # 尝试解析为 JSON
                if not (json_str.startswith('[') or json_str.startswith('{')):
                    continue
                    
                data = json.loads(json_str)
                
                # 检查是否是模型统计行
                # 结构: ["$","div","LLM Gateway + LeMUR-MODEL NAME",{"children":[PART1, PART2]}]
                if (isinstance(data, list) and len(data) >= 4 and 
                    isinstance(data[2], str) and 
                    "LLM Gateway + LeMUR-" in data[2]):
                    
                    # 提取模型名称
                    full_key = data[2]
                    model_name_from_key = full_key.replace("LLM Gateway + LeMUR-", "").strip()
                    
                    # 尝试从 children 中提取 token 数量
                    props = data[3]
                    if isinstance(props, dict) and "children" in props:
                        children = props["children"]
                        if isinstance(children, list) and children:
                            # PART 2: Token Count 通常在第二个 child 中
                            # ["$","span",null,{"children":["12,089"," ",...]}]
                            token_count = 0
                            
                            # 遍历 children 寻找包含数字的部分
                            for child in children:
                                if isinstance(child, list) and len(child) >= 4:
                                    child_props = child[3]
                                    if isinstance(child_props, dict) and "children" in child_props:
                                        grand_children = child_props["children"]
                                        
                                        # 情况A: ["12,089", " ", ...]
                                        if isinstance(grand_children, list) and len(grand_children) > 0:
                                            first_item = grand_children[0]
                                            if isinstance(first_item, str) and re.match(r'^[\d,.]+$', first_item.strip()):
                                                token_count = _normalize_usage_token_value(first_item)
                                                if token_count > 0:
                                                    break
                            
                            if model_name_from_key and token_count > 0:
                                sanitized_name = _sanitize_model_name(model_name_from_key)
                                models.append({
                                    "model": sanitized_name,
                                    "tokens": token_count
                                })
                                continue
            except json.JSONDecodeError:
                continue
            except Exception as e:
                # 单行解析失败不影响其他行
                continue
                
        if models:
            log.info(f"Extracted {len(models)} model entries via JSON parsing")
            return models
            
    except Exception as e:
        log.warning(f"JSON parsing method failed: {e}")

    # 方法2: 正则表达式回退 (保留原有逻辑但稍作改进)
    try:
        # 查找 "LLM Gateway + LeMUR-{model_name}" 格式的 div key
        div_pattern = r'\["[^"]*","div","LLM Gateway \+ LeMUR-([^"]+)"'
        div_matches = re.findall(div_pattern, scan_text)
        
        if div_matches:
            log.debug(f"Found {len(div_matches)} model names in div keys (regex)")
        
        # 改进的正则: 尝试匹配 token 数量
        # 寻找 "children":["12,089" 这样的模式
        # 注意: 这种简单的正则无法可靠地关联模型名称和 token 数，
        # 除非它们在文本中紧邻。在 RSC 中它们通常是分开的组件。
        
        # 如果 JSON 解析失败，尝试旧的正则作为最后的手段
        model_token_pattern = r'"children":\s*\[[^\]]*,\s*" ",\s*"([^"]+)"\][^}]*"children":\s*\["([\d,.]+)"'
        model_token_matches = re.findall(model_token_pattern, scan_text)
        
        for model_name, token_str in model_token_matches:
            if model_name and not model_name.lower() in ['tokens', 'total', 'hours']:
                tokens = _normalize_usage_token_value(token_str)
                sanitized_name = _sanitize_model_name(model_name)
                if sanitized_name and tokens > 0:
                    models.append({
                        "model": sanitized_name,
                        "tokens": tokens
                    })
        
        if models:
            log.debug(f"Extracted {len(models)} model entries via regex")
        if not models:
            try:
                div_token_pattern = r'\["\$","div","LLM Gateway \+ LeMUR-([^"]+)",[\s\S]*?"children":\s*\[[\s\S]*?"([\d,.]+)"[\s\S]*?"tokens"'
                for m in re.finditer(div_token_pattern, scan_text):
                    name = m.group(1)
                    val = _normalize_usage_token_value(m.group(2))
                    sanitized_name = _sanitize_model_name(name)
                    if sanitized_name and val > 0:
                        models.append({"model": sanitized_name, "tokens": val})
            except Exception:
                pass
    
    except Exception as e:
        log.warning(f"Failed to extract model names via regex: {e}")
    
    return models


def _parse_usage_rsc_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    解析使用量页面的 RSC 数据
    
    提取总 tokens、按模型分类的使用量和可视化 segments
    
    Args:
        data: 包含 'raw' 字段的 RSC 响应数据
    
    Returns:
        解析后的使用量数据，包含：
        - total_tokens: 总 token 数
        - by_model: 按模型分类的使用量列表
        - segments: 可视化分段数据
        - daily_by_model: 按日期和模型分组的数据
        - items: 其他数据项
        - error: 错误信息（如果有）
        - debug_info: 调试信息
    """
    import re
    
    if "raw" not in data:
        return data
    
    raw_text = data["raw"]
    result = {
        "total_tokens": 0,
        "items": [],
        "by_model": [],
        "segments": [],
        "daily_by_model": [],  # 按日期和模型分组的数据
        "debug_info": {
            "raw_length": len(raw_text),
            "parsing_method": None,
            "extracted_segments_count": 0,
            "extracted_models_count": 0
        }
    }
    
    try:
        log.debug(f"Parsing usage RSC data, length: {len(raw_text)}")
        
        # 1. 先提取 segments 与按模型统计
        segments = _extract_segments_from_rsc(raw_text)
        if segments:
            result["segments"] = segments
            result["debug_info"]["extracted_segments_count"] = len(segments)
            log.info(f"Extracted {len(segments)} visualization segments")
        
        # 2. 提取模型名称和 token 数量
        models = _extract_model_names_from_rsc(raw_text)
        if models:
            result["by_model"] = models
            result["debug_info"]["extracted_models_count"] = len(models)
            log.info(f"Extracted {len(models)} model entries")
        
        # 3. 如果有 segments 但没有模型名称，尝试关联
        if segments and not models:
            # 尝试从 raw_text 中查找模型名称列表
            # 这些名称通常在 segments 附近
            log.debug("Attempting to associate segments with model names")
            
            # 查找所有可能的模型名称
            model_name_pattern = r'"children":\s*\[[^\]]*"([^"]+)"\][^}]*(?="children":\s*\["[\d,]+")'
            potential_names = re.findall(model_name_pattern, raw_text)
            
            # 过滤和关联
            if len(potential_names) >= len(segments):
                for i, segment in enumerate(segments):
                    if i < len(potential_names):
                        sanitized_name = _sanitize_model_name(potential_names[i])
                        if sanitized_name:
                            result["by_model"].append({
                                "model": sanitized_name,
                                "tokens": segment["value"],
                                "color": segment.get("color")
                            })
                log.debug(f"Associated {len(result['by_model'])} segments with model names")
        
        # 4. 总数优先来自当前解析的模型或分段，以避免误取 30 天摘要的总额
        if result["by_model"]:
            result["total_tokens"] = sum(m.get("tokens", 0) for m in result["by_model"])
            result["debug_info"]["parsing_method"] = "sum_by_model"
            log.info(f"Calculated total tokens from models: {result['total_tokens']}")
        elif segments:
            result["total_tokens"] = sum(s.get("value", 0) for s in segments)
            result["debug_info"]["parsing_method"] = "sum_segments"
            log.info(f"Calculated total tokens from segments: {result['total_tokens']}")
        else:
            # 5. 兜底：从 "Total tokens" 模式提取（可能是摘要卡片）
            total_matches = re.findall(r'"children":\s*\["([\d,]+)",\s*" ",\s*\["\$","span",null,\{"children":\["Total ","tokens"\]\}\]\]', raw_text)
            if total_matches:
                total_str = total_matches[0].replace(",", "")
                result["total_tokens"] = int(total_str)
                result["debug_info"]["parsing_method"] = "pattern1_fallback"
                log.info(f"Extracted total tokens (pattern1 fallback): {result['total_tokens']}")
            else:
                total_pattern2 = r'"([\d]{1,3},[\d]{3}(?:,[\d]{3})?)"[^}]{0,100}[Tt]otal[^}]{0,50}tokens'
                total_matches = re.findall(total_pattern2, raw_text)
                if total_matches:
                    max_tokens = 0
                    for match in total_matches:
                        tokens = int(match.replace(",", ""))
                        if tokens > max_tokens:
                            max_tokens = tokens
                    if max_tokens > 0:
                        result["total_tokens"] = max_tokens
                        result["debug_info"]["parsing_method"] = "pattern2_fallback"
                        log.info(f"Extracted total tokens (pattern2 fallback): {result['total_tokens']}")
        
        # 6. 提取 UsageChart 数据（按日期和模型分组）
        daily_by_model = _extract_usage_chart_data(raw_text)
        if daily_by_model:
            result["daily_by_model"] = daily_by_model
            result["debug_info"]["daily_by_model_count"] = len(daily_by_model)
            log.info(f"Extracted {len(daily_by_model)} daily usage data points")
        
        # 7. 记录调试信息
        if result["total_tokens"] == 0 and not result["by_model"]:
            log.warning("No usage data extracted from RSC response")
            result["debug_info"]["raw_sample"] = raw_text[:500] if len(raw_text) > 500 else raw_text

        _normalize_usage_result_tokens(result)
        
    except Exception as e:
        log.error(f"Failed to parse usage RSC data: {e}")
        result["error"] = str(e)
        result["debug_info"]["error_type"] = type(e).__name__
        result["debug_info"]["raw_sample"] = raw_text[:500] if len(raw_text) > 500 else raw_text
    
    return result


def _extract_usage_chart_data(raw_text: str) -> List[Dict[str, Any]]:
    """
    从 RSC 数据中提取 UsageChart 的 data 数组
    
    UsageChart 数据格式：
    ["$","$L13",null,{"data":[{"GPT 5 (Input)":4423,"GPT 5 (Output)":457,...,"name":"2025-11-23T00:00:00.000Z"},...],"names":[...],...}]
    
    Returns:
        按日期分组的使用量数据列表，每个元素包含：
        - date: 日期字符串
        - models: 模型名称到 tokens 的映射
    """
    import re
    import json
    
    result = []
    
    try:
        # 查找 UsageChart 组件的数据
        # 模式：{"data":[{...}],"names":[...],...}
        chart_pattern = r'\{"data":\s*\[(\{[^\]]+\})\],"names":\s*\[([^\]]+)\]'
        chart_match = re.search(chart_pattern, raw_text)
        
        if not chart_match:
            # 尝试另一种模式：直接查找 data 数组
            data_pattern = r'"data":\s*\[(\[\{[^\]]+\}\]|\{[^}]+\}(?:,\{[^}]+\})*)\]'
            data_match = re.search(data_pattern, raw_text)
            if data_match:
                log.debug("Found data array with alternative pattern")
        
        # 更精确的模式：查找包含日期的数据数组
        # 日期格式：2025-11-23T00:00:00.000Z
        date_data_pattern = r'\{"[^"]+":[\d.]+[^}]*"name":"(\d{4}-\d{2}-\d{2}T[^"]+)"[^}]*\}'
        date_matches = re.findall(date_data_pattern, raw_text)
        
        if date_matches:
            log.debug(f"Found {len(date_matches)} date entries in usage data")
            
            # 提取完整的数据对象
            # 查找包含 "name":"日期" 的 JSON 对象
            full_data_pattern = r'\{(?:[^{}]|\{[^{}]*\})*"name":"(\d{4}-\d{2}-\d{2}T[^"]+)"(?:[^{}]|\{[^{}]*\})*\}'
            
            for match in re.finditer(full_data_pattern, raw_text):
                try:
                    obj_str = match.group(0)
                    # 尝试解析 JSON
                    obj = json.loads(obj_str)
                    
                    date_str = obj.get("name", "")
                    if date_str:
                        # 提取模型数据
                        models = {}
                        for key, value in obj.items():
                            if key != "name" and isinstance(value, (int, float)):
                                # 合并 Input 和 Output
                                base_model = key.replace(" (Input)", "").replace(" (Output)", "")
                                if base_model not in models:
                                    models[base_model] = {"input": 0, "output": 0, "total": 0}
                                
                                if "(Input)" in key:
                                    models[base_model]["input"] += value
                                elif "(Output)" in key:
                                    models[base_model]["output"] += value
                                else:
                                    models[base_model]["total"] += value
                                
                                models[base_model]["total"] = models[base_model]["input"] + models[base_model]["output"]
                        
                        if models:
                            result.append({
                                "date": date_str,
                                "models": models
                            })
                except json.JSONDecodeError:
                    continue
                except Exception as e:
                    log.debug(f"Failed to parse usage data object: {e}")
                    continue
        
        # 去重（按日期）
        seen_dates = set()
        unique_result = []
        for item in result:
            if item["date"] not in seen_dates:
                seen_dates.add(item["date"])
                unique_result.append(item)
        
        result = sorted(unique_result, key=lambda x: x["date"])
        
        if result:
            log.info(f"Extracted {len(result)} daily usage data points from UsageChart")
        
    except Exception as e:
        log.error(f"Failed to extract UsageChart data: {e}")
    
    return result


def _parse_cost_rsc_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    解析成本页面的 RSC 数据
    
    提取总成本、按服务分类的成本和消费趋势
    """
    import re
    import json
    
    if "raw" not in data:
        return data
    
    raw_text = data["raw"]
    result = {
        "total_cost": 0.0,
        "items": [],
        "by_service": [],
        "by_model": [],
        "spend_trend": [],
        "daily_by_model": [],  # 按日期分组的模型数据
    }
    
    try:
        # 1. 优先从 title 属性提取总成本（最准确）
        title_match = re.search(r'"title":\s*"Total spend:\s*\$(\d+\.?\d*)"', raw_text)
        if title_match:
            result["total_cost"] = float(title_match.group(1))
        else:
            cost_matches = re.findall(r'"children":\s*"\$\$?(\d+\.?\d*)"', raw_text)
            if cost_matches:
                costs = [float(c) for c in cost_matches]
                result["total_cost"] = max(costs) if costs else 0.0
            else:
                ts_matches = re.findall(r'Total\s+spend:\s*\$(\d+\.?\d*)', raw_text)
                if ts_matches:
                    result["total_cost"] = float(ts_matches[0])
        
        # 2. 优先从 chartExportData 提取详细数据（最完整）
        # chartExportData 可能包含嵌套的对象，需要更复杂的匹配
        chart_export_start = raw_text.find('"chartExportData":[')
        log.debug(f"Looking for chartExportData, found at position: {chart_export_start}")
        if chart_export_start >= 0:
            # 找到数组的开始位置
            array_start = chart_export_start + len('"chartExportData":')
            # 找到匹配的 ] 结束位置
            bracket_count = 0
            array_end = -1
            for i in range(array_start, min(array_start + 50000, len(raw_text))):
                if raw_text[i] == '[':
                    bracket_count += 1
                elif raw_text[i] == ']':
                    bracket_count -= 1
                    if bracket_count == 0:
                        array_end = i + 1
                        break
            
            if array_end > array_start:
                json_str = raw_text[array_start:array_end]
                try:
                    chart_data = json.loads(json_str)
                    if chart_data:
                        # 按模型聚合（分别统计输入、输出，以及未知方向的额外成本）
                        model_costs: Dict[str, Dict[str, float]] = {}
                        for item in chart_data:
                            model = item.get("model", "")
                            value = float(item.get("value", 0))
                            
                            # 解析模型名和方向
                            base = model
                            is_input = False
                            is_output = False
                            if base.endswith(" (Input)"):
                                is_input = True
                                base = base[:-8].strip()
                            elif base.endswith(" (Output)"):
                                is_output = True
                                base = base[:-9].strip()
                            
                            if base not in model_costs:
                                model_costs[base] = {"input_cost": 0.0, "output_cost": 0.0, "extra_cost": 0.0}
                            
                            if is_input:
                                model_costs[base]["input_cost"] += value
                            elif is_output:
                                model_costs[base]["output_cost"] += value
                            else:
                                # 未标注方向的值累加到额外成本，再合并到总额
                                model_costs[base]["extra_cost"] += value
                        
                        by_model = []
                        for model, costs in model_costs.items():
                            total = costs["input_cost"] + costs["output_cost"] + costs.get("extra_cost", 0.0)
                            by_model.append({
                                "model": model,
                                "input_cost": costs["input_cost"],
                                "output_cost": costs["output_cost"],
                                "cost": total,
                            })
                        by_model.sort(key=lambda x: x.get("cost", 0.0), reverse=True)
                        result["by_model"] = by_model
                        
                        # 如果没有从 title 获取到总成本，从模型数据计算
                        if result["total_cost"] == 0.0:
                            result["total_cost"] = sum(m.get("cost", 0.0) for m in by_model)
                        
                        log.info(f"Parsed {len(by_model)} models from chartExportData")
                except Exception as e:
                    log.warning(f"Failed to parse chartExportData: {e}")
                    # 记录原始数据样本用于调试
                    sample = json_str[:500] if len(json_str) > 500 else json_str
                    log.debug(f"chartExportData sample: {sample}")

        # 提取总额按服务分类（TotalCard）
        try:
            m_total = re.search(r'"total":\s*\{([^}]+)\}', raw_text)
            if m_total:
                import json
                json_str = '{' + m_total.group(1) + '}'
                total_obj = json.loads(json_str)
                by_service = []
                for k, v in total_obj.items():
                    name = str(k)
                    try:
                        amount = float(v)
                    except Exception:
                        amount = 0.0
                    by_service.append({
                        "service": name,
                        "cost": amount,
                    })
                if by_service:
                    result["by_service"] = by_service
                    if result["total_cost"] == 0.0:
                        result["total_cost"] = sum(s.get("cost", 0.0) for s in by_service)
        except Exception:
            pass
        
        # 2. 提取按模型分类的成本（CostChart data 映射）
        # 同时提取按日期分组的模型数据用于图表tooltip
        data_matches = re.findall(r'"data":\s*\[((?:\{[^}]+\},?\s*)+)\]', raw_text)
        log.debug(f"Found {len(data_matches)} data matches")
        daily_by_model = []  # 按日期分组的模型数据
        spend_trend_data = []  # 趋势数据
        
        for match in data_matches:
            try:
                import json
                json_str = '[' + match + ']'
                arr = json.loads(json_str)
                if arr and isinstance(arr, list):
                    first_obj = arr[0] if len(arr) > 0 else {}
                    
                    # 检查数据类型：
                    # 1. 如果只有 name 和 value，是简单趋势数据
                    # 2. 如果有 name 和其他模型键，是按日期分组的模型数据
                    # 3. 如果只有 name，是空数据（筛选条件导致无数据）
                    
                    has_name = "name" in first_obj
                    has_value = "value" in first_obj
                    has_model_keys = any(k not in ("name", "value") for k in first_obj.keys())
                    
                    if has_name and has_value and not has_model_keys:
                        # 简单趋势数据（name + value）
                        if not spend_trend_data:
                            for item in arr:
                                try:
                                    amount = float(item.get("value", 0))
                                except Exception:
                                    amount = 0.0
                                spend_trend_data.append({
                                    "date": item.get("name", ""),
                                    "amount": amount
                                })
                            total = sum(s.get("amount", 0.0) for s in spend_trend_data)
                            if total > 20:
                                for s in spend_trend_data:
                                    s["amount"] = round(s["amount"] / 100.0, 8)
                            log.info(f"Parsed {len(spend_trend_data)} simple trend data points")
                    elif has_name and has_model_keys:
                        # 按日期分组的模型数据（name + 模型键）
                        # 同时提取趋势数据
                        agg: Dict[str, Dict[str, float]] = {}
                        
                        for idx, obj in enumerate(arr):
                            if isinstance(obj, dict):
                                date_str = obj.get("name", "")
                                day_total = 0.0
                                day_models = {}
                                
                                for name, val in obj.items():
                                    if name in ("name", "value"):
                                        continue
                                    base = name
                                    dir_in = False
                                    dir_out = False
                                    if base.endswith(" (Input)"):
                                        dir_in = True
                                        base = base[:-8]
                                    elif base.endswith(" (Output)"):
                                        dir_out = True
                                        base = base[:-9]
                                    if not base or base == "name":
                                        continue
                                    
                                    if base not in agg:
                                        agg[base] = {"input_cost": 0.0, "output_cost": 0.0, "extra_cost": 0.0}
                                    try:
                                        amount = float(val)
                                    except Exception:
                                        amount = 0.0
                                    
                                    day_total += amount
                                    
                                    if dir_in:
                                        agg[base]["input_cost"] += amount
                                    elif dir_out:
                                        agg[base]["output_cost"] += amount
                                    else:
                                        agg[base]["extra_cost"] += amount
                                    
                                    if amount > 0:
                                        if base not in day_models:
                                            day_models[base] = {"input": 0.0, "output": 0.0}
                                        if dir_in:
                                            day_models[base]["input"] += amount
                                        elif dir_out:
                                            day_models[base]["output"] += amount
                                        else:
                                            day_models[base]["output"] += amount
                                
                                # 保存趋势数据
                                spend_trend_data.append({
                                    "date": date_str,
                                    "amount": day_total
                                })
                                
                                if day_models:
                                    daily_by_model.append({
                                        "index": idx,
                                        "models": day_models
                                    })
                        
                        # 生成 by_model 汇总
                        if not result["by_model"] and agg:
                            by_model = []
                            for model, costs in agg.items():
                                total = float(costs.get("input_cost", 0.0)) + float(costs.get("output_cost", 0.0)) + float(costs.get("extra_cost", 0.0))
                                by_model.append({
                                    "model": model,
                                    "input_cost": float(costs.get("input_cost", 0.0)),
                                    "output_cost": float(costs.get("output_cost", 0.0)),
                                    "cost": total,
                                })
                            if by_model:
                                by_model.sort(key=lambda x: x.get("cost", 0.0), reverse=True)
                                result["by_model"] = by_model
                                if result["total_cost"] == 0.0:
                                    result["total_cost"] = sum(m.get("cost", 0.0) for m in by_model)
                                log.info(f"Parsed {len(by_model)} models from data array")
                        
                        log.info(f"Parsed {len(spend_trend_data)} trend points and {len(daily_by_model)} daily model data")
                        break
                    elif has_name and not has_model_keys:
                        # 只有 name，没有数据（筛选条件导致无数据）
                        # 仍然提取日期用于X轴
                        for item in arr:
                            spend_trend_data.append({
                                "date": item.get("name", ""),
                                "amount": 0.0
                            })
                        log.info(f"Parsed {len(spend_trend_data)} empty trend data points (no model data)")
                    else:
                        # 旧格式：对象的键是模型名，值是金额（没有 name 键）
                        agg: Dict[str, Dict[str, float]] = {}
                        
                        for idx, obj in enumerate(arr):
                            if isinstance(obj, dict):
                                day_models = {}
                                for name, val in obj.items():
                                    if name in ("name", "value"):
                                        continue
                                    base = name
                                    dir_in = False
                                    dir_out = False
                                    if base.endswith(" (Input)"):
                                        dir_in = True
                                        base = base[:-8]
                                    elif base.endswith(" (Output)"):
                                        dir_out = True
                                        base = base[:-9]
                                    if not base or base == "name":
                                        continue
                                    
                                    # 聚合到总计
                                    if base not in agg:
                                        agg[base] = {"input_cost": 0.0, "output_cost": 0.0, "extra_cost": 0.0}
                                    try:
                                        amount = float(val)
                                    except Exception:
                                        amount = 0.0
                                    if dir_in:
                                        agg[base]["input_cost"] += amount
                                    elif dir_out:
                                        agg[base]["output_cost"] += amount
                                    else:
                                        agg[base]["extra_cost"] += amount
                                    
                                    # 记录当天的模型数据（只记录有值的）
                                    if amount > 0:
                                        if base not in day_models:
                                            day_models[base] = {"input": 0.0, "output": 0.0}
                                        if dir_in:
                                            day_models[base]["input"] += amount
                                        elif dir_out:
                                            day_models[base]["output"] += amount
                                        else:
                                            day_models[base]["output"] += amount
                                
                                # 保存当天的数据
                                if day_models:
                                    daily_by_model.append({
                                        "index": idx,
                                        "models": day_models
                                    })
                        
                        # 生成 by_model 汇总
                        if not result["by_model"]:
                            by_model = []
                            for model, costs in agg.items():
                                total = float(costs.get("input_cost", 0.0)) + float(costs.get("output_cost", 0.0)) + float(costs.get("extra_cost", 0.0))
                                by_model.append({
                                    "model": model,
                                    "input_cost": float(costs.get("input_cost", 0.0)),
                                    "output_cost": float(costs.get("output_cost", 0.0)),
                                    "cost": total,
                                })
                            if by_model:
                                by_model.sort(key=lambda x: x.get("cost", 0.0), reverse=True)
                                result["by_model"] = by_model
                                if result["total_cost"] == 0.0:
                                    result["total_cost"] = sum(m.get("cost", 0.0) for m in by_model)
                                log.info(f"Parsed {len(by_model)} models from alternative data source")
                        break
            except Exception as e:
                log.debug(f"Failed to parse alternative data source: {e}")
                continue
        
        # 保存按日期分组的模型数据
        if daily_by_model:
            result["daily_by_model"] = daily_by_model
            log.info(f"Parsed {len(daily_by_model)} days of model data")
        
        # 保存趋势数据
        if spend_trend_data:
            result["spend_trend"] = spend_trend_data
        # 如果 by_model 仍为空，尝试从文本提取模型名（兜底）
        if not result["by_model"]:
            models = _extract_model_names_from_rsc(raw_text)
            if models:
                result["by_model"] = [{"model": m.get("model"), "input_cost": 0.0, "output_cost": 0.0, "cost": 0.0} for m in models]
        
    except Exception as e:
        log.warning(f"Failed to parse cost RSC data: {e}")
        result["error"] = str(e)
    
    return result


@router.get("/export/usage")
async def export_usage_data(
    format: str = "json",
    starting_on: Optional[str] = None,
    ending_before: Optional[str] = None,
    account_email: Optional[str] = None,
) -> Dict[str, Any]:
    """
    导出使用量数据
    
    Args:
        format: 导出格式 (json, csv)
        starting_on: 开始日期
        ending_before: 结束日期
    """
    # 获取使用量数据
    usage_data = await get_usage_data(
        window_size="day",
        starting_on=starting_on,
        ending_before=ending_before,
        group_by="date",
        account_email=account_email,
    )
    
    if format == "csv":
        # 转换为 CSV 格式
        csv_lines = ["date,product,model,usage,unit"]
        items = usage_data.get("items", [])
        for item in items:
            csv_lines.append(
                f"{item.get('date', '')},{item.get('product', '')},"
                f"{item.get('model', '')},{item.get('usage', 0)},{item.get('unit', '')}"
            )
        return {"format": "csv", "data": "\n".join(csv_lines)}
    
    return {"format": "json", "data": usage_data}


@router.get("/export/cost")
async def export_cost_data(
    format: str = "json",
    starting_on: Optional[str] = None,
    ending_before: Optional[str] = None,
    window_size: str = "month",
    account_email: Optional[str] = None,
) -> Dict[str, Any]:
    """
    导出成本数据
    
    Args:
        format: 导出格式 (json, csv)
        starting_on: 开始日期
        ending_before: 结束日期
        window_size: 时间窗口
    """
    # 获取成本数据
    cost_data = await get_cost_data(force=True, account_email=account_email)
    
    if format == "csv":
        csv_lines = ["service,total_cost"]
        by_service = cost_data.get("by_service", [])
        for item in by_service:
            csv_lines.append(f"{item.get('service', '')},{item.get('cost', 0)}")
        total_cost = cost_data.get("total_cost", 0)
        csv_lines.append(f"Total,{total_cost}")
        return {"format": "csv", "data": "\n".join(csv_lines)}
    
    return {"format": "json", "data": cost_data}
_dashboard_client = None

async def _get_dashboard_client():
    """
    获取持久化的 AsyncClient（启用HTTP/2与连接池），减少重复TLS握手与DNS耗时
    """
    global _dashboard_client
    if _dashboard_client is None:
        import httpx
        _dashboard_client = httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            http2=True,
            limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=30),
            transport=httpx.AsyncHTTPTransport(retries=2),
        )
    return _dashboard_client


def _normalize_model_name(name: str) -> str:
    """
    标准化模型名称，解决输入/输出名称不一致的问题
    """
    import re
    
    name = name.strip()
    
    # 匹配 "Claude Sonnet X.X" 格式，转换为 "Claude X.X Sonnet"
    pattern1 = r"^(Claude)\s+(Sonnet|Haiku|Opus)\s+(\d+\.?\d*)$"
    match = re.match(pattern1, name, re.IGNORECASE)
    if match:
        return f"{match.group(1)} {match.group(3)} {match.group(2)}"
    
    # 匹配 "Claude X Sonnet" 格式，保持不变
    pattern2 = r"^(Claude)\s+(\d+\.?\d*)\s+(Sonnet|Haiku|Opus)$"
    match = re.match(pattern2, name, re.IGNORECASE)
    if match:
        return f"{match.group(1)} {match.group(2)} {match.group(3)}"
    
    return name


async def _fetch_official_pricing_page_rates() -> Dict[str, Any]:
    """Fetch current public AssemblyAI pricing for LLM Gateway models."""
    client = await _get_dashboard_client()
    response = await client.get(
        f"{ASSEMBLY_DASHBOARD_BASE}/pricing",
        headers={"Accept": "text/html,application/xhtml+xml"},
    )
    response.raise_for_status()
    return _parse_official_pricing_page_rates(response.text)


def _html_text(fragment: str) -> str:
    """Return normalized display text from a small HTML fragment."""
    import html
    import re

    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_official_pricing_page_rates(raw_text: str) -> Dict[str, Any]:
    """
    Parse AssemblyAI's public pricing page for LLM Gateway prices.

    The public pricing page is the official pay-as-you-go source and currently
    renders LLM Gateway as tab ``data-matrix-panel="5"`` with input/output
    columns shown as ``$X / 1M``. We intentionally parse only those rows so
    unrelated speech pricing does not get misclassified as token pricing.
    """
    import re

    result = {
        "llm_gateway_input": [],
        "llm_gateway_output": [],
    }

    if not raw_text:
        return result

    tab_match = re.search(
        r"<button\b[^>]*data-matrix-tab=\"([^\"]+)\"[^>]*>\s*LLM Gateway\s*</button>",
        raw_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    panel_id = tab_match.group(1) if tab_match else "5"
    panel_match = re.search(
        r"<div\b[^>]*data-matrix-panel=\"" + re.escape(panel_id) + r"\"[^>]*>",
        raw_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if panel_match:
        start = panel_match.start()
        next_panel = re.search(
            r"<div\b[^>]*data-matrix-panel=\"[^\"]+\"",
            raw_text[panel_match.end():],
            flags=re.IGNORECASE | re.DOTALL,
        )
        end_candidates = [
            panel_match.end() + next_panel.start() if next_panel else -1,
            raw_text.find("<!-- Volume-based pricing CTA", start),
            raw_text.find("</section>", start),
        ]
    else:
        heading_match = re.search(
            r"<h[1-6]\b[^>]*>\s*LLM Gateway\s*</h[1-6]>",
            raw_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not heading_match:
            return result
        start = heading_match.start()
        next_heading = re.search(
            r"<h[1-6]\b[^>]*>",
            raw_text[heading_match.end():],
            flags=re.IGNORECASE | re.DOTALL,
        )
        end_candidates = [
            heading_match.end() + next_heading.start() if next_heading else -1,
            raw_text.find("<!-- Volume-based pricing CTA", start),
            raw_text.find("</section>", start),
        ]
    end = min([pos for pos in end_candidates if pos > start], default=len(raw_text))
    llm_section = raw_text[start:end]

    seen_models = set()
    for row_match in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", llm_section, flags=re.IGNORECASE | re.DOTALL):
        row = row_match.group(1)
        if "/ 1M" not in _html_text(row):
            continue

        cell_matches = re.findall(r"<td\b[^>]*>(.*?)</td>", row, flags=re.IGNORECASE | re.DOTALL)
        if not cell_matches:
            continue

        first_cell = cell_matches[0]
        model_match = re.search(r"<span\b[^>]*>(.*?)</span>", first_cell, flags=re.IGNORECASE | re.DOTALL)
        model = _html_text(model_match.group(1) if model_match else first_cell)
        model = model.replace("*", "").strip()
        if not model or model.lower() in {"models", "model", "custom"}:
            continue

        amounts = []
        for cell in cell_matches[1:]:
            cell_text = _html_text(cell)
            if not re.search(r"/\s*1M\b", cell_text, flags=re.IGNORECASE):
                continue
            amount = _coerce_amount(cell_text)
            if amount is not None:
                amounts.append(amount)

        if len(amounts) < 2 or model in seen_models:
            continue

        seen_models.add(model)
        result["llm_gateway_input"].append(
            {
                "model": model,
                "rate": amounts[0],
                "unit": "1M tokens",
                "price_source": "official_pricing",
            }
        )
        result["llm_gateway_output"].append(
            {
                "model": model,
                "rate": amounts[1],
                "unit": "1M tokens",
                "price_source": "official_pricing",
            }
        )

    return result


def _parse_rates_rsc_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    解析费率数据的 RSC 响应
    
    从 billing 页面的 RSC 数据中提取费率表格信息。
    优先解析接口返回的数据，解析失败时返回空结果（由调用方使用 fallback）。
    
    Args:
        data: 包含 'raw' 字段的 RSC 响应数据
    
    Returns:
        解析后的费率数据，包含：
        - speech_to_text: 语音转文本模型费率列表
        - streaming: 流式语音转文本模型费率列表
        - speech_understanding: 语音理解功能费率列表
        - llm_gateway_input: LLM Gateway 输入 token 费率列表
        - llm_gateway_output: LLM Gateway 输出 token 费率列表
    """
    import re
    
    if "raw" not in data:
        return {}
    
    raw_text = data["raw"]
    result = {
        "speech_to_text": [],
        "streaming": [],
        "speech_understanding": [],
        "llm_gateway_input": [],
        "llm_gateway_output": [],
    }
    
    try:
        log.debug(f"Parsing rates RSC data from billing page, length: {len(raw_text)}")
        
        # 1. 首先找到所有分类的位置
        stt_pos = raw_text.find('"children":"Speech-to-Text"')
        streaming_pos = raw_text.find('"children":"Streaming Speech-to-Text"')
        understanding_pos = raw_text.find('"children":"Speech Understanding"')
        llm_input_pos = raw_text.find('"children":["LLM Gateway + LeMUR"," Input Tokens"]')
        llm_output_pos = raw_text.find('"children":["LLM Gateway + LeMUR"," Output Tokens"]')
        
        log.debug(f"Category positions: STT={stt_pos}, Streaming={streaming_pos}, "
                  f"Understanding={understanding_pos}, LLM Input={llm_input_pos}, LLM Output={llm_output_pos}")
        
        # 2. 查找所有费率条目
        # 格式: "$$费率"," ",["$","span",null,{"children":[" / ","单位"]}]
        rate_pattern = r'\"\$\$(\d+\.?\d*)\",\" \",\[\"[^\"]*\",\"span\",null,\{\"children\":\[\" / \",\"([^\"]+)\"\]'
        rate_matches = list(re.finditer(rate_pattern, raw_text))
        log.info(f"Found {len(rate_matches)} rate entries in billing RSC data")
        
        # 3. 对于每个费率，向前查找最近的模型名称
        for rate_match in rate_matches:
            rate_str = rate_match.group(1)
            unit = rate_match.group(2)
            rate_pos = rate_match.start()
            
            try:
                rate = float(rate_str)
            except ValueError:
                continue
            
            # 向前查找最近的 "children":"模型名" (在表格单元格中)
            search_start = max(0, rate_pos - 2000)
            search_text = raw_text[search_start:rate_pos]
            
            # 查找最后一个 "children":"XXX" 模式
            model_pattern = r'\"children\":\"([^\"]+)\"'
            model_matches = list(re.finditer(model_pattern, search_text))
            
            if model_matches:
                # 取最后一个匹配（最接近费率的）
                model_name = model_matches[-1].group(1)
                
                # 过滤掉非模型名称
                skip_names = ["Model", "Rate", "table-row", "table-header", "$undefined", 
                              "rt-", "index-module", "style", "className", "ref", "scope",
                              "Speech-to-Text", "Streaming Speech-to-Text", "Speech Understanding",
                              "LLM Gateway + LeMUR", "Input Tokens", "Output Tokens"]
                
                should_skip = False
                for skip in skip_names:
                    if skip in model_name or model_name.startswith("$") or len(model_name) > 50:
                        should_skip = True
                        break
                
                if should_skip:
                    continue
                
                clean_name = model_name.replace("*", "").strip()
                
                # 标准化模型名称（解决输入/输出名称不一致的问题）
                # 例如：输入是 "Claude Sonnet 3.7"，输出是 "Claude 3.7 Sonnet"
                clean_name = _normalize_model_name(clean_name)
                
                # 根据费率位置和单位判断分类
                # 支持 1K tokens 和 1M tokens 两种单位
                if "tokens" in unit.lower():
                    if llm_output_pos >= 0 and rate_pos > llm_output_pos:
                        result["llm_gateway_output"].append({
                            "model": clean_name,
                            "rate": rate,
                            "unit": unit
                        })
                    elif llm_input_pos >= 0 and rate_pos > llm_input_pos:
                        result["llm_gateway_input"].append({
                            "model": clean_name,
                            "rate": rate,
                            "unit": unit
                        })
                elif "hour" in unit:
                    if understanding_pos >= 0 and rate_pos > understanding_pos and (llm_input_pos < 0 or rate_pos < llm_input_pos):
                        result["speech_understanding"].append({
                            "feature": clean_name,
                            "rate": rate,
                            "unit": unit
                        })
                    elif streaming_pos >= 0 and rate_pos > streaming_pos and (understanding_pos < 0 or rate_pos < understanding_pos):
                        result["streaming"].append({
                            "model": clean_name,
                            "rate": rate,
                            "unit": unit
                        })
                    elif stt_pos >= 0 and rate_pos > stt_pos and (streaming_pos < 0 or rate_pos < streaming_pos):
                        result["speech_to_text"].append({
                            "model": clean_name,
                            "rate": rate,
                            "unit": unit
                        })
        
        # 4. 去重
        for key in result:
            seen = set()
            unique = []
            for item in result[key]:
                name = item.get("model") or item.get("feature")
                if name not in seen:
                    seen.add(name)
                    unique.append(item)
            result[key] = unique
        
        # 5. 按费率排序
        result["llm_gateway_input"].sort(key=lambda x: x.get("rate", 0), reverse=True)
        result["llm_gateway_output"].sort(key=lambda x: x.get("rate", 0), reverse=True)
        result["speech_to_text"].sort(key=lambda x: x.get("rate", 0), reverse=True)
        result["streaming"].sort(key=lambda x: x.get("rate", 0), reverse=True)
        result["speech_understanding"].sort(key=lambda x: x.get("rate", 0), reverse=True)
        
        log.info(f"Parsed rates from billing: {len(result['speech_to_text'])} STT, {len(result['streaming'])} streaming, "
                 f"{len(result['speech_understanding'])} understanding, {len(result['llm_gateway_input'])} LLM input, "
                 f"{len(result['llm_gateway_output'])} LLM output")
        
    except Exception as e:
        log.error(f"Failed to parse rates RSC data: {e}")
        import traceback
        log.error(f"Traceback: {traceback.format_exc()}")
    
    return result


# ============================================================================
# 预加载队列相关端点
# ============================================================================

@router.get("/preload/status")
async def get_preload_status() -> Dict[str, Any]:
    """
    获取预加载队列状态
    
    返回队列状态、待处理任务数、最后刷新时间等信息。
    """
    try:
        from ..services.account_preload import get_preload_queue
        queue = await get_preload_queue()
        return queue.get_queue_status()
    except Exception as e:
        log.error(f"Failed to get preload status: {e}")
        return {
            "started": False,
            "error": str(e),
        }


@router.get("/preload/cache-info")
async def get_cache_info(account_email: Optional[str] = None) -> Dict[str, Any]:
    """
    获取账户缓存新鲜度信息
    
    Args:
        account_email: 账户邮箱，如果为None则获取当前账户
    
    返回各数据类型的缓存状态和新鲜度。
    """
    try:
        # 获取账户邮箱
        if not account_email:
            adapter = await get_storage_adapter()
            current = await adapter.get_config(CURRENT_ACCOUNT_KEY)
            if current:
                account_email = current.get("email")
        
        if not account_email:
            return {"error": "No account specified"}
        
        from ..services.account_preload import get_preload_queue
        queue = await get_preload_queue()
        freshness_info = await queue.cache.get_freshness_info(account_email)
        
        return {
            "account_email": account_email,
            "cache_info": freshness_info,
            "cache_stats": queue.cache.get_stats(),
        }
    except Exception as e:
        log.error(f"Failed to get cache info: {e}")
        return {"error": str(e)}


@router.post("/preload/trigger")
async def trigger_preload(account_email: Optional[str] = None) -> Dict[str, Any]:
    """
    手动触发预加载
    
    Args:
        account_email: 账户邮箱，如果为None则预加载所有账户
    
    触发后台预加载任务。
    """
    try:
        from ..services.account_preload import get_preload_queue, start_preload_queue
        
        # 确保队列已启动
        queue = await start_preload_queue()
        
        if account_email:
            # 预加载指定账户
            task_id = await queue.enqueue_account(account_email, priority=0)
            return {
                "success": True,
                "message": f"Triggered preload for {account_email}",
                "task_id": task_id,
            }
        else:
            # 预加载所有账户
            adapter = await get_storage_adapter()
            current = await adapter.get_config(CURRENT_ACCOUNT_KEY)
            current_email = current.get("email") if current else None
            
            task_ids = await queue.enqueue_all_accounts(current_account=current_email)
            return {
                "success": True,
                "message": f"Triggered preload for {len(task_ids)} accounts",
                "task_ids": task_ids,
            }
    except Exception as e:
        log.error(f"Failed to trigger preload: {e}")
        return {"success": False, "error": str(e)}

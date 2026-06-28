"""账户会话自动续期 / 保活机制测试。

覆盖：
- session_security：密码加密往返 + 篡改拒绝 + JWT exp 解析
- _session_needs_renewal / _jwt_seconds_remaining：基于 JWT exp 的续期判定
- _prepare_dashboard_request：allow_bearer=False 时不发送失效 Bearer
- _renew_session：有凭据→重新登录续期；无凭据→无法续期
- _make_dashboard_request：401 先续期重试再放弃，并捕获滚动 cookie
"""

import asyncio
import base64
import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from src.api import account_api
from src.services.session_security import (
    decode_jwt_exp,
    decrypt_secret,
    encrypt_secret,
)


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------

class FakeAdapter:
    def __init__(self):
        self.store = {}

    async def get_config(self, key, default=None):
        return self.store.get(key, default)

    async def set_config(self, key, value):
        self.store[key] = value
        return True

    async def delete_config(self, key):
        self.store.pop(key, None)
        return True


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeClient:
    """按队列依次返回响应，记录每次请求的 headers。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def get(self, url, headers=None, params=None):
        self.calls.append({"method": "GET", "url": url, "headers": headers})
        return self._responses.pop(0)

    async def post(self, url, headers=None, json=None, params=None):
        self.calls.append({"method": "POST", "url": url, "headers": headers})
        return self._responses.pop(0)


def make_jwt(exp_epoch):
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp_epoch}).encode()
    ).rstrip(b"=").decode()
    return f"header.{payload}.sig"


# ---------------------------------------------------------------------------
# session_security
# ---------------------------------------------------------------------------

def test_encrypt_decrypt_roundtrip():
    import os
    key = os.urandom(32)
    secret = "p@ss-üñïçødé-123"
    token = encrypt_secret(key, secret)
    assert token.startswith("v1:")
    assert secret not in token  # 不应明文出现
    assert decrypt_secret(key, token) == secret


def test_decrypt_rejects_wrong_key_and_tamper():
    import os
    key = os.urandom(32)
    token = encrypt_secret(key, "secret")
    assert decrypt_secret(os.urandom(32), token) is None  # 错误密钥
    assert decrypt_secret(key, token[:-2] + "AA") is None  # 篡改密文
    assert decrypt_secret(key, "garbage") is None
    assert decrypt_secret(key, "") is None


def test_decode_jwt_exp():
    exp = int(time.time()) + 300
    assert decode_jwt_exp(make_jwt(exp)) == exp
    assert decode_jwt_exp("not-a-jwt") is None
    assert decode_jwt_exp(None) is None


# ---------------------------------------------------------------------------
# 续期判定
# ---------------------------------------------------------------------------

def test_session_needs_renewal_by_jwt_exp():
    now = int(time.time())
    fresh = {"session_jwt": make_jwt(now + 300)}
    near = {"session_jwt": make_jwt(now + 30)}  # < leeway(90)
    expired = {"session_jwt": make_jwt(now - 10)}
    assert account_api._session_needs_renewal(fresh) is False
    assert account_api._session_needs_renewal(near) is True
    assert account_api._session_needs_renewal(expired) is True


def test_jwt_seconds_remaining_prefers_cached_ts():
    now = int(time.time())
    session = {"jwt_expires_at_ts": now + 120, "session_jwt": make_jwt(now + 999)}
    remaining = account_api._jwt_seconds_remaining(session)
    assert 110 <= remaining <= 120


def test_session_needs_renewal_without_exp_uses_login_age():
    from datetime import datetime, timedelta
    old = {"logged_in_at": (datetime.now() - timedelta(seconds=400)).isoformat()}
    young = {"logged_in_at": datetime.now().isoformat()}
    assert account_api._session_needs_renewal(old) is True
    assert account_api._session_needs_renewal(young) is False


# ---------------------------------------------------------------------------
# 请求头构造
# ---------------------------------------------------------------------------

def test_prepare_dashboard_request_omits_bearer_when_stale():
    now = int(time.time())
    session = {
        "auth_type": "dashboard",
        "session_jwt": make_jwt(now - 5),
        "session_token": "stok",
        "aai_extended_session": "aai-cookie",
    }
    # 允许 Bearer
    _, headers = account_api._prepare_dashboard_request(session, "/dashboard/x", None, allow_bearer=True)
    assert headers.get("Authorization") == f"Bearer {session['session_jwt']}"
    assert "aai_extended_session=aai-cookie" in headers["Cookie"]

    # 不允许 Bearer：仍带长效 cookie 兜底，但连失效的 session_jwt cookie 也丢弃
    _, headers2 = account_api._prepare_dashboard_request(session, "/dashboard/x", None, allow_bearer=False)
    assert "Authorization" not in headers2
    assert "aai_extended_session=aai-cookie" in headers2["Cookie"]
    assert "session_token=stok" in headers2["Cookie"]
    assert "session_jwt" not in headers2["Cookie"]


def test_prepare_dashboard_api_request_uses_json_headers():
    now = int(time.time())
    session = {
        "auth_type": "dashboard",
        "session_jwt": make_jwt(now + 300),
        "session_token": "stok",
        "aai_extended_session": "aai-cookie",
    }

    url, headers = account_api._prepare_dashboard_request(
        session,
        "/dashboard/api/accounts/balance",
        None,
        allow_bearer=True,
        rsc=False,
    )

    assert url == "https://www.assemblyai.com/dashboard/api/accounts/balance"
    assert headers["Accept"] == "application/json, text/plain, */*"
    assert headers["Referer"] == "https://www.assemblyai.com/dashboard/settings/billing"
    assert headers["Authorization"] == f"Bearer {session['session_jwt']}"
    assert "aai_extended_session=aai-cookie" in headers["Cookie"]
    assert "RSC" not in headers
    assert "Next-Url" not in headers
    assert "X-Requested-With" not in headers


@pytest.mark.asyncio
async def test_fetch_dashboard_account_balance_uses_current_api():
    with patch.object(
        account_api,
        "_make_dashboard_request",
        AsyncMock(return_value={"balance": "58.49928"}),
    ) as mock_request:
        amount = await account_api._fetch_dashboard_account_balance("user@example.com")

    assert amount == 58.49928
    mock_request.assert_awaited_once_with(
        "GET",
        "/dashboard/api/accounts/balance",
        account_email="user@example.com",
        rsc=False,
    )


@pytest.mark.asyncio
async def test_billing_keeps_api_balance_when_settings_page_fails():
    session = {"email": "user@example.com", "user_info": {}}

    with patch.object(account_api, "_get_session", AsyncMock(return_value=session)), \
        patch.object(account_api, "_cache_get", return_value=None), \
        patch.object(account_api, "_cache_set") as mock_cache_set, \
        patch.object(
            account_api,
            "_fetch_dashboard_account_balance",
            AsyncMock(return_value=58.49928),
        ), \
        patch.object(
            account_api,
            "_fetch_dashboard_billing_page",
            AsyncMock(side_effect=Exception("Dashboard API error: 404")),
        ):
        result = await account_api.get_billing_info(force=True, account_email="user@example.com")

    assert result["balance_found"] is True
    assert result["balance"] == 58.49928
    assert "error" not in result
    mock_cache_set.assert_called_once()


def test_extract_aai_extended_session_handles_multiple_set_cookie():
    # httpx.Headers 风格：get_list 返回多条 Set-Cookie
    class FakeHeaders:
        def get_list(self, name):
            return [
                "other_cookie=abc; Path=/",
                "aai_extended_session=XYZ123; Path=/; HttpOnly",
            ]

    assert account_api._extract_aai_extended_session(FakeHeaders()) == "XYZ123"

    # 逗号拼接的回退场景（普通 dict-like，无 get_list）——SimpleCookie 正确切分
    joined = {"set-cookie": "aai_extended_session=XYZ123; Path=/, other=abc; Path=/"}
    assert account_api._extract_aai_extended_session(joined) == "XYZ123"


# ---------------------------------------------------------------------------
# 续期
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_renew_session_relogins_with_stored_credentials():
    fake = FakeAdapter()
    email = "user@example.com"
    now = int(time.time())

    with patch.object(account_api, "get_storage_adapter", AsyncMock(return_value=fake)):
        key = await account_api._get_session_secret()
        enc = encrypt_secret(key, "my-password")
        fake.store[f"{account_api.SESSION_STORAGE_KEY}:{email}"] = {
            "email": email,
            "auth_type": "dashboard",
            "session_jwt": make_jwt(now - 10),  # 已过期
            "enc_password": enc,
            "logged_in_at": "2026-01-01T00:00:00",
        }

        auth_result = {
            "isAuthenticated": True,
            "user": {"email": email, "id": 42, "customer_type": "PAYG"},
            "sessionJWT": make_jwt(now + 300),
            "sessionToken": "new-stok",
        }
        auth_headers = {"set-cookie": "aai_extended_session=ROLLED; Path=/"}

        with patch.object(
            account_api,
            "_authenticate_dashboard",
            AsyncMock(return_value=(auth_result, auth_headers)),
        ) as mock_auth:
            renewed = await account_api._renew_session(email)

    assert mock_auth.await_count == 1
    assert mock_auth.await_args.args == (email, "my-password")
    assert renewed is not None
    assert account_api._session_needs_renewal(renewed) is False
    assert renewed["aai_extended_session"] == "ROLLED"
    # 凭据被保留，可继续续期
    assert renewed.get("enc_password") == enc
    # 已落盘
    saved = fake.store[f"{account_api.SESSION_STORAGE_KEY}:{email}"]
    assert saved["session_token"] == "new-stok"


@pytest.mark.asyncio
async def test_renew_session_keeps_fresh_login_time_for_exp_less_jwt():
    """续期返回的 JWT 无 exp 时，应保留本次刷新的新鲜 logged_in_at，否则会被
    age 回退判定为立刻陈旧，导致重复重新认证。"""
    fake = FakeAdapter()
    email = "noexp@example.com"
    now = int(time.time())
    with patch.object(account_api, "get_storage_adapter", AsyncMock(return_value=fake)):
        key = await account_api._get_session_secret()
        fake.store[f"{account_api.SESSION_STORAGE_KEY}:{email}"] = {
            "email": email,
            "auth_type": "dashboard",
            "session_jwt": make_jwt(now - 10),
            "enc_password": encrypt_secret(key, "pw"),
            "logged_in_at": "2020-01-01T00:00:00",  # 远古值
        }
        auth_result = {
            "isAuthenticated": True,
            "user": {"email": email, "id": 1, "customer_type": "PAYG"},
            "sessionJWT": "no-dots-no-exp",  # 无法解析 exp
            "sessionToken": "stok",
        }
        with patch.object(
            account_api,
            "_authenticate_dashboard",
            AsyncMock(return_value=(auth_result, {})),
        ):
            renewed = await account_api._renew_session(email)

    assert renewed is not None
    assert renewed.get("jwt_expires_at_ts") is None  # 无 exp
    # 未沿用远古 logged_in_at，故不会被 age 回退判为陈旧
    assert renewed["logged_in_at"] != "2020-01-01T00:00:00"
    assert account_api._session_needs_renewal(renewed) is False


@pytest.mark.asyncio
async def test_renew_session_without_credentials_returns_none():
    fake = FakeAdapter()
    email = "nocreds@example.com"
    now = int(time.time())
    with patch.object(account_api, "get_storage_adapter", AsyncMock(return_value=fake)):
        fake.store[f"{account_api.SESSION_STORAGE_KEY}:{email}"] = {
            "email": email,
            "auth_type": "dashboard",
            "session_jwt": make_jwt(now - 10),
        }
        with patch.object(account_api, "_authenticate_dashboard", AsyncMock()) as mock_auth:
            renewed = await account_api._renew_session(email)
    assert renewed is None
    assert mock_auth.await_count == 0  # 无凭据不应尝试登录


@pytest.mark.asyncio
async def test_renew_session_prefers_cookie_refresh_over_password():
    """有长效 cookie 时优先无密码 cookie 滚动续期，根本不调用密码登录。"""
    fake = FakeAdapter()
    email = "cookiefirst@example.com"
    now = int(time.time())
    session_key = f"{account_api.SESSION_STORAGE_KEY}:{email}"
    with patch.object(account_api, "get_storage_adapter", AsyncMock(return_value=fake)):
        key = await account_api._get_session_secret()
        fake.store[session_key] = {
            "email": email,
            "auth_type": "dashboard",
            "session_jwt": make_jwt(now - 10),  # 已过期
            "aai_extended_session": "good-cookie",
            "enc_password": encrypt_secret(key, "pw"),
            "logged_in_at": "2026-01-01T00:00:00",
        }
        # cookie 滚动成功，返回带新 JWT 的会话
        refreshed = dict(fake.store[session_key])
        refreshed["session_jwt"] = make_jwt(now + 300)
        refreshed["jwt_expires_at_ts"] = now + 300
        refreshed["last_renew_ts"] = time.time()
        with patch.object(
            account_api, "_refresh_session_via_cookie", AsyncMock(return_value=refreshed)
        ) as mock_cookie, patch.object(
            account_api, "_authenticate_dashboard", AsyncMock()
        ) as mock_auth:
            renewed = await account_api._renew_session(email)

    assert renewed is not None
    assert mock_cookie.await_count == 1
    assert mock_auth.await_count == 0  # 未走密码登录
    assert account_api._session_needs_renewal(renewed) is False
    # _renew_session 统一落盘（_refresh_session_via_cookie 不再自行保存）
    assert fake.store[session_key]["session_jwt"] == refreshed["session_jwt"]


@pytest.mark.asyncio
async def test_renew_session_force_password_bypasses_backoff():
    """force_password=True 时即便处于退避窗口也尝试密码登录（请求层按需恢复）。"""
    fake = FakeAdapter()
    email = "forcepw@example.com"
    now = int(time.time())
    session_key = f"{account_api.SESSION_STORAGE_KEY}:{email}"
    with patch.object(account_api, "get_storage_adapter", AsyncMock(return_value=fake)):
        key = await account_api._get_session_secret()
        fake.store[session_key] = {
            "email": email,
            "auth_type": "dashboard",
            "session_jwt": make_jwt(now - 10),
            "enc_password": encrypt_secret(key, "pw"),
            "logged_in_at": "2026-01-01T00:00:00",
            "pw_renew_fail_ts": time.time(),  # 仍在退避窗口
        }
        auth_result = {
            "isAuthenticated": True,
            "user": {"email": email, "id": 7, "customer_type": "PAYG"},
            "sessionJWT": make_jwt(now + 300),
            "sessionToken": "stok",
        }
        with patch.object(
            account_api, "_refresh_session_via_cookie", AsyncMock(return_value=None)
        ), patch.object(
            account_api, "_authenticate_dashboard", AsyncMock(return_value=(auth_result, {}))
        ) as mock_auth:
            renewed = await account_api._renew_session(email, force_password=True)

    assert renewed is not None
    assert mock_auth.await_count == 1  # 退避被绕过，确实尝试了登录


@pytest.mark.asyncio
async def test_renew_session_tolerates_transient_credential_failure():
    """cookie 续期失败 + 密码登录偶发 401：不立即丢凭据（容忍瞬时抖动），保留会话。"""
    fake = FakeAdapter()
    email = "transient@example.com"
    now = int(time.time())
    session_key = f"{account_api.SESSION_STORAGE_KEY}:{email}"
    fake.store[account_api.ACCOUNTS_LIST_KEY] = [{"email": email}]

    with patch.object(account_api, "get_storage_adapter", AsyncMock(return_value=fake)):
        key = await account_api._get_session_secret()
        fake.store[session_key] = {
            "email": email,
            "auth_type": "dashboard",
            "session_jwt": make_jwt(now - 10),
            "aai_extended_session": "maybe-stale-cookie",
            "enc_password": encrypt_secret(key, "old-password"),
            "logged_in_at": "2026-01-01T00:00:00",
        }
        with patch.object(
            account_api, "_refresh_session_via_cookie", AsyncMock(return_value=None)
        ), patch.object(
            account_api,
            "_authenticate_dashboard",
            AsyncMock(side_effect=account_api.HTTPException(status_code=401, detail="bad creds")),
        ):
            renewed = await account_api._renew_session(email)

    assert renewed is None
    # 单次 401 不丢凭据，仅累计失败计数 + 记录退避时间，会话保留
    assert session_key in fake.store
    assert "enc_password" in fake.store[session_key]
    assert fake.store[session_key].get("pw_renew_fail_count") == 1
    assert fake.store[session_key].get("aai_extended_session") == "maybe-stale-cookie"


@pytest.mark.asyncio
async def test_renew_session_drops_credentials_after_repeated_failures():
    """连续多次密码失败后才丢弃凭据（停止反复登录触发风控），但保留会话。"""
    fake = FakeAdapter()
    email = "changed-pw@example.com"
    now = int(time.time())
    session_key = f"{account_api.SESSION_STORAGE_KEY}:{email}"
    fake.store[account_api.ACCOUNTS_LIST_KEY] = [{"email": email}]

    with patch.object(account_api, "get_storage_adapter", AsyncMock(return_value=fake)):
        key = await account_api._get_session_secret()
        fake.store[session_key] = {
            "email": email,
            "auth_type": "dashboard",
            "session_jwt": make_jwt(now - 10),
            "aai_extended_session": "still-good-cookie",
            "enc_password": encrypt_secret(key, "old-password"),
            "logged_in_at": "2026-01-01T00:00:00",
            # 已累计 MAX-1 次失败；本次再失败即达阈值
            "pw_renew_fail_count": account_api.PASSWORD_RENEW_MAX_FAILURES - 1,
        }
        with patch.object(
            account_api, "_refresh_session_via_cookie", AsyncMock(return_value=None)
        ), patch.object(
            account_api,
            "_authenticate_dashboard",
            AsyncMock(side_effect=account_api.HTTPException(status_code=401, detail="bad creds")),
        ):
            renewed = await account_api._renew_session(email)

    assert renewed is None
    # 达阈值后移除失效凭据停止重试，但会话保留（cookie 兜底）
    assert session_key in fake.store
    assert "enc_password" not in fake.store[session_key]
    assert fake.store[session_key].get("aai_extended_session") == "still-good-cookie"


@pytest.mark.asyncio
async def test_renew_session_password_backoff_skips_relogin():
    """处于密码失败退避窗口内时，跳过密码登录，避免高频重登触发风控。"""
    fake = FakeAdapter()
    email = "backoff@example.com"
    now = int(time.time())
    session_key = f"{account_api.SESSION_STORAGE_KEY}:{email}"
    with patch.object(account_api, "get_storage_adapter", AsyncMock(return_value=fake)):
        key = await account_api._get_session_secret()
        fake.store[session_key] = {
            "email": email,
            "auth_type": "dashboard",
            "session_jwt": make_jwt(now - 10),
            "enc_password": encrypt_secret(key, "pw"),
            "logged_in_at": "2026-01-01T00:00:00",
            "pw_renew_fail_ts": time.time(),  # 刚失败过，仍在退避窗口
        }
        with patch.object(
            account_api, "_refresh_session_via_cookie", AsyncMock(return_value=None)
        ), patch.object(account_api, "_authenticate_dashboard", AsyncMock()) as mock_auth:
            renewed = await account_api._renew_session(email)

    assert renewed is None
    assert mock_auth.await_count == 0  # 退避窗口内不登录


# ---------------------------------------------------------------------------
# 长效 cookie 无密码滚动续期
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_via_cookie_rolls_session_without_password():
    """有长效 cookie 时，无密码即可滚动续期，并捕获服务端下发的新 cookie/JWT。"""
    fake = FakeAdapter()
    email = "rollme@example.com"
    now = int(time.time())
    session_key = f"{account_api.SESSION_STORAGE_KEY}:{email}"
    session = {
        "email": email,
        "auth_type": "dashboard",
        "session_jwt": make_jwt(now - 10),  # 旧 JWT 已过期
        "aai_extended_session": "old-cookie",
        "session_token": "old-stok",
    }
    new_jwt = make_jwt(now + 300)
    resp = FakeResponse(
        200,
        payload={"raw": "ok"},
        headers={"set-cookie": f"aai_extended_session=new-cookie; Path=/, session_jwt={new_jwt}; Path=/"},
    )
    client = FakeClient([resp])
    with patch.object(account_api, "get_storage_adapter", AsyncMock(return_value=fake)), \
         patch.object(account_api, "_get_dashboard_client", AsyncMock(return_value=client)):
        refreshed = await account_api._refresh_session_via_cookie(email, dict(session))

    assert refreshed is not None
    assert refreshed["aai_extended_session"] == "new-cookie"
    assert refreshed["session_jwt"] == new_jwt
    # 新 JWT 带来新 exp，会话不再陈旧
    assert account_api._session_needs_renewal(refreshed) is False
    # 不发送可能过期的 Bearer（仅 cookie 认证）
    assert "Authorization" not in client.calls[0]["headers"]
    # 仅更新内存、不在此落盘（持久化统一由调用方 _renew_session 负责，避免重复 I/O）
    assert session_key not in fake.store


@pytest.mark.asyncio
async def test_refresh_via_cookie_marks_fresh_when_no_new_jwt():
    """服务端只滚动了 aai_extended_session、未下发新 JWT 时，应据刷新时间标记新鲜，
    丢弃陈旧 JWT，避免刚续期就被立即判为陈旧而反复续期。"""
    fake = FakeAdapter()
    email = "nojwt@example.com"
    now = int(time.time())
    session = {
        "email": email,
        "auth_type": "dashboard",
        "session_jwt": make_jwt(now - 10),  # 旧 JWT 已过期
        "aai_extended_session": "old-cookie",
    }
    resp = FakeResponse(
        200,
        payload={"raw": "ok"},
        headers={"set-cookie": "aai_extended_session=rolled-cookie; Path=/"},  # 无 session_jwt
    )
    client = FakeClient([resp])
    with patch.object(account_api, "get_storage_adapter", AsyncMock(return_value=fake)), \
         patch.object(account_api, "_get_dashboard_client", AsyncMock(return_value=client)):
        refreshed = await account_api._refresh_session_via_cookie(email, dict(session))

    assert refreshed is not None
    assert refreshed["aai_extended_session"] == "rolled-cookie"
    # 陈旧 JWT 被丢弃，改按 logged_in_at 估算 -> 不再陈旧
    assert refreshed.get("session_jwt") is None
    assert account_api._session_needs_renewal(refreshed) is False


@pytest.mark.asyncio
async def test_refresh_via_cookie_returns_none_without_rolled_cookie_proof():
    """非错误响应但未滚动任何会话 cookie（如失效会话被跟随重定向到登录页的 200）：
    视为无续期证据，返回 None 以便回退密码兜底，避免把已死会话误标为新鲜。"""
    fake = FakeAdapter()
    email = "noproof@example.com"
    now = int(time.time())
    session = {
        "email": email,
        "auth_type": "dashboard",
        "session_jwt": make_jwt(now - 10),
        "aai_extended_session": "stale-cookie",
    }
    # 200 但没有 Set-Cookie（未滚动任何会话 cookie）
    resp = FakeResponse(200, payload={"raw": "<login page>"}, headers={})
    client = FakeClient([resp])
    with patch.object(account_api, "get_storage_adapter", AsyncMock(return_value=fake)), \
         patch.object(account_api, "_get_dashboard_client", AsyncMock(return_value=client)):
        refreshed = await account_api._refresh_session_via_cookie(email, dict(session))
    assert refreshed is None


@pytest.mark.asyncio
async def test_refresh_via_cookie_returns_none_when_cookie_dead():
    """长效 cookie 已失效（401）时返回 None，交由密码兜底/上层清理。"""
    fake = FakeAdapter()
    email = "deadcookie@example.com"
    session = {
        "email": email,
        "auth_type": "dashboard",
        "aai_extended_session": "expired-cookie",
    }
    client = FakeClient([FakeResponse(401, text="unauthorized")])
    with patch.object(account_api, "get_storage_adapter", AsyncMock(return_value=fake)), \
         patch.object(account_api, "_get_dashboard_client", AsyncMock(return_value=client)):
        refreshed = await account_api._refresh_session_via_cookie(email, dict(session))
    assert refreshed is None


@pytest.mark.asyncio
async def test_refresh_via_cookie_skips_when_no_cookie():
    """没有任何长效 cookie 时直接返回 None，不发请求。"""
    fake = FakeAdapter()
    email = "nocookie@example.com"
    session = {"email": email, "auth_type": "dashboard", "session_jwt": "x"}
    client = FakeClient([])  # 不应被调用
    with patch.object(account_api, "get_storage_adapter", AsyncMock(return_value=fake)), \
         patch.object(account_api, "_get_dashboard_client", AsyncMock(return_value=client)):
        refreshed = await account_api._refresh_session_via_cookie(email, dict(session))
    assert refreshed is None
    assert client.calls == []


# ---------------------------------------------------------------------------
# 请求层 401 续期重试
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dashboard_request_retries_after_401_renewal():
    fake = FakeAdapter()
    email = "retry@example.com"
    now = int(time.time())
    session_key = f"{account_api.SESSION_STORAGE_KEY}:{email}"

    # 当前账户 + 一个新鲜会话（避免请求前主动续期）
    fake.store[account_api.CURRENT_ACCOUNT_KEY] = {"email": email}
    fake.store[session_key] = {
        "email": email,
        "auth_type": "dashboard",
        "session_jwt": make_jwt(now + 300),
        "session_token": "stok",
        "aai_extended_session": "cookie-1",
        "expires_at": "2999-01-01T00:00:00",
    }

    renewed_session = {
        "email": email,
        "auth_type": "dashboard",
        "session_jwt": make_jwt(now + 300),
        "session_token": "stok2",
        "aai_extended_session": "cookie-2",
        "expires_at": "2999-01-01T00:00:00",
    }

    responses = [
        FakeResponse(401, text="unauthorized"),
        FakeResponse(200, payload={"ok": True}, headers={"set-cookie": "aai_extended_session=cookie-3; Path=/"}),
    ]
    client = FakeClient(responses)

    with patch.object(account_api, "get_storage_adapter", AsyncMock(return_value=fake)), \
         patch.object(account_api, "_get_dashboard_client", AsyncMock(return_value=client)), \
         patch.object(account_api, "_renew_session", AsyncMock(return_value=renewed_session)) as mock_renew:
        result = await account_api._make_dashboard_request("GET", "/dashboard/account", account_email=email)

    assert result == {"ok": True}
    assert mock_renew.await_count == 1
    assert len(client.calls) == 2  # 首次 401 + 续期后重试


@pytest.mark.asyncio
async def test_dashboard_request_clears_session_when_renewal_fails():
    fake = FakeAdapter()
    email = "dead@example.com"
    now = int(time.time())
    session_key = f"{account_api.SESSION_STORAGE_KEY}:{email}"
    fake.store[account_api.CURRENT_ACCOUNT_KEY] = {"email": email}
    fake.store[account_api.ACCOUNTS_LIST_KEY] = [{"email": email}]
    fake.store[session_key] = {
        "email": email,
        "auth_type": "dashboard",
        "session_jwt": make_jwt(now + 300),
        "expires_at": "2999-01-01T00:00:00",
    }

    client = FakeClient([FakeResponse(401), FakeResponse(401)])

    with patch.object(account_api, "get_storage_adapter", AsyncMock(return_value=fake)), \
         patch.object(account_api, "_get_dashboard_client", AsyncMock(return_value=client)), \
         patch.object(account_api, "_renew_session", AsyncMock(return_value=None)):
        with pytest.raises(account_api.HTTPException) as exc:
            await account_api._make_dashboard_request("GET", "/dashboard/account", account_email=email)

    assert exc.value.status_code == 401
    # 续期失败后会话被清除
    assert session_key not in fake.store


@pytest.mark.asyncio
async def test_refresh_endpoint_keeps_cookie_only_session_alive():
    """无凭据但有长效 cookie 的会话：/refresh 不应 401，应报告 cookie_fallback。"""
    fake = FakeAdapter()
    email = "cookieonly@example.com"
    now = int(time.time())
    session_key = f"{account_api.SESSION_STORAGE_KEY}:{email}"
    fake.store[account_api.CURRENT_ACCOUNT_KEY] = {"email": email}
    fake.store[session_key] = {
        "email": email,
        "auth_type": "dashboard",
        "session_jwt": make_jwt(now - 10),  # 已过期
        "aai_extended_session": "still-good-cookie",
        "expires_at": "2999-01-01T00:00:00",
        # 无 enc_password -> 无法主动续期
    }
    # cookie 滚动失败（模拟服务端本轮未滚动/瞬时不可用）：仍有长效 cookie 兜底，不下线
    with patch.object(account_api, "get_storage_adapter", AsyncMock(return_value=fake)), \
         patch.object(account_api, "_renew_session", AsyncMock(return_value=None)):
        result = await account_api.refresh_session(account_email=email)
    assert result["success"] is True
    assert result["renewed"] is False
    assert result["cookie_fallback"] is True
    assert result["auto_renew"] is False
    assert session_key in fake.store  # 会话未被清除


@pytest.mark.asyncio
async def test_refresh_endpoint_401_when_truly_dead():
    """JWT 过期、无凭据、且无长效 cookie：/refresh 应 401。"""
    fake = FakeAdapter()
    email = "dead2@example.com"
    now = int(time.time())
    session_key = f"{account_api.SESSION_STORAGE_KEY}:{email}"
    fake.store[account_api.CURRENT_ACCOUNT_KEY] = {"email": email}
    fake.store[session_key] = {
        "email": email,
        "auth_type": "dashboard",
        "session_jwt": make_jwt(now - 10),
        "expires_at": "2999-01-01T00:00:00",
        # 无 cookie、无 enc_password
    }
    with patch.object(account_api, "get_storage_adapter", AsyncMock(return_value=fake)):
        with pytest.raises(account_api.HTTPException) as exc:
            await account_api.refresh_session(account_email=email)
    assert exc.value.status_code == 401
    # 真正失效的死会话在 401 前被清除，避免前端把它当作已登录
    assert session_key not in fake.store


# ---------------------------------------------------------------------------
# 本地兜底窗口：旧版 7 天 expires_at 的升级/恢复
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_session_rolls_forward_expired_local_window_with_cookie():
    """本地 expires_at 已过期但仍有长效 cookie：前滚窗口并保留，不硬删。
    覆盖升级场景——旧版以 7 天 expires_at 落盘、cookie 仍有效的会话不被误删。"""
    fake = FakeAdapter()
    email = "legacy@example.com"
    session_key = f"{account_api.SESSION_STORAGE_KEY}:{email}"
    fake.store[session_key] = {
        "email": email,
        "auth_type": "dashboard",
        "aai_extended_session": "still-valid-cookie",
        "expires_at": "2020-01-01T00:00:00",  # 旧的、已过期的本地窗口
    }
    with patch.object(account_api, "get_storage_adapter", AsyncMock(return_value=fake)):
        session = await account_api._get_session(email)

    assert session is not None  # 未被删除
    assert session_key in fake.store
    # 本地窗口已前滚到未来
    from datetime import datetime as _dt
    assert _dt.fromisoformat(fake.store[session_key]["expires_at"]) > _dt.now()


@pytest.mark.asyncio
async def test_get_session_deletes_expired_window_when_no_recovery():
    """本地窗口过期且无任何恢复手段（无 cookie、无凭据）：确为死会话，删除。"""
    fake = FakeAdapter()
    email = "reallydead@example.com"
    session_key = f"{account_api.SESSION_STORAGE_KEY}:{email}"
    fake.store[account_api.ACCOUNTS_LIST_KEY] = [{"email": email}]
    fake.store[session_key] = {
        "email": email,
        "auth_type": "dashboard",
        "session_jwt": make_jwt(int(time.time()) - 10),
        "expires_at": "2020-01-01T00:00:00",
        # 无 cookie、无 enc_password
    }
    with patch.object(account_api, "get_storage_adapter", AsyncMock(return_value=fake)):
        session = await account_api._get_session(email)

    assert session is None
    assert session_key not in fake.store


# ---------------------------------------------------------------------------
# 滚动 cookie 捕获
# ---------------------------------------------------------------------------

def test_capture_rolling_cookies_updates_jwt_and_exp():
    """服务端滚动 session_jwt 时应同步更新本地 JWT 与解析出的 exp。"""
    now = int(time.time())
    new_jwt = make_jwt(now + 300)
    session = {"aai_extended_session": "old", "session_jwt": make_jwt(now - 10)}
    resp = FakeResponse(
        200,
        headers={
            "set-cookie": f"aai_extended_session=rolled; Path=/, "
            f"session_jwt={new_jwt}; Path=/, session_token=stok2; Path=/"
        },
    )
    changed = account_api._capture_rolling_cookies(session, resp)
    assert changed is True
    assert session["aai_extended_session"] == "rolled"
    assert session["session_jwt"] == new_jwt
    assert session["session_token"] == "stok2"
    assert session["jwt_expires_at_ts"] == now + 300


def test_capture_rolling_cookies_noop_when_unchanged():
    session = {"aai_extended_session": "same"}
    resp = FakeResponse(200, headers={"set-cookie": "aai_extended_session=same; Path=/"})
    assert account_api._capture_rolling_cookies(session, resp) is False


def test_capture_rolling_cookies_refreshes_login_time_on_unparseable_jwt():
    """滚动到无法解析 exp 的 JWT 时，应同步把 logged_in_at 刷为当前时间，避免旧的
    logged_in_at 让续期判定立即判为陈旧（覆盖 _make_dashboard_request 路径）。"""
    session = {
        "aai_extended_session": "old",
        "session_jwt": make_jwt(int(time.time()) - 10),
        "logged_in_at": "2020-01-01T00:00:00",  # 远古值
    }
    resp = FakeResponse(
        200,
        headers={"set-cookie": "session_jwt=no-dots-no-exp; Path=/"},  # 无法解析 exp
    )
    changed = account_api._capture_rolling_cookies(session, resp)
    assert changed is True
    assert session["jwt_expires_at_ts"] is None
    assert session["logged_in_at"] != "2020-01-01T00:00:00"
    # 据刷新后的 logged_in_at，会话不再被判为陈旧
    assert account_api._session_needs_renewal(session) is False


# ---------------------------------------------------------------------------
# 后台保活：覆盖仅有长效 cookie 的空闲账户（旧实现会漏掉它们）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_keepalive_renews_cookie_only_accounts():
    """保活循环应对仅有 cookie（无密码）的账户也滚动续期——这正是旧实现遗漏、
    导致空闲账户几天后失效的根因。"""
    fake = FakeAdapter()
    now = int(time.time())
    cookie_email = "idle-cookie@example.com"
    dead_email = "no-creds@example.com"
    fake.store[account_api.ACCOUNTS_LIST_KEY] = [
        {"email": cookie_email},
        {"email": dead_email},
    ]
    fake.store[f"{account_api.SESSION_STORAGE_KEY}:{cookie_email}"] = {
        "email": cookie_email,
        "auth_type": "dashboard",
        "session_jwt": make_jwt(now - 10),  # JWT 已过期 -> 需要续期
        "aai_extended_session": "idle-cookie",
        "expires_at": "2999-01-01T00:00:00",
        # 无 enc_password
    }
    # 既无 cookie 又无密码：不可续期，保活应跳过（不抛错）
    fake.store[f"{account_api.SESSION_STORAGE_KEY}:{dead_email}"] = {
        "email": dead_email,
        "auth_type": "dashboard",
        "session_jwt": make_jwt(now - 10),
        "expires_at": "2999-01-01T00:00:00",
    }

    renewed_emails = []

    async def fake_renew(email):
        renewed_emails.append(email)
        return fake.store.get(f"{account_api.SESSION_STORAGE_KEY}:{email}")

    # 让循环只跑一轮即退出
    sleep_calls = {"n": 0}

    async def one_shot_sleep(_seconds):
        # 第一次 sleep 后让循环体跑一轮，第二轮 sleep 时取消退出
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 2:
            raise asyncio.CancelledError()

    with patch.object(account_api, "get_storage_adapter", AsyncMock(return_value=fake)), \
         patch.object(account_api, "_renew_session", AsyncMock(side_effect=fake_renew)), \
         patch.object(account_api.asyncio, "sleep", AsyncMock(side_effect=one_shot_sleep)):
        await account_api._keepalive_loop()

    # 仅有 cookie 的账户被续期；无任何凭据的账户被跳过
    assert cookie_email in renewed_emails
    assert dead_email not in renewed_emails

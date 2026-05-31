"""账户会话自动续期 / 保活机制测试。

覆盖：
- session_security：密码加密往返 + 篡改拒绝 + JWT exp 解析
- _session_needs_renewal / _jwt_seconds_remaining：基于 JWT exp 的续期判定
- _prepare_dashboard_request：allow_bearer=False 时不发送失效 Bearer
- _renew_session：有凭据→重新登录续期；无凭据→无法续期
- _make_dashboard_request：401 先续期重试再放弃，并捕获滚动 cookie
"""

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
async def test_renew_session_clears_session_on_401():
    """凭据失效（401，如改了密码）时应清除会话，避免保活循环反复登录。"""
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
            "enc_password": encrypt_secret(key, "old-password"),
            "logged_in_at": "2026-01-01T00:00:00",
        }
        with patch.object(
            account_api,
            "_authenticate_dashboard",
            AsyncMock(side_effect=account_api.HTTPException(status_code=401, detail="bad creds")),
        ):
            renewed = await account_api._renew_session(email)

    assert renewed is None
    # 失效会话被清除
    assert session_key not in fake.store


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
    with patch.object(account_api, "get_storage_adapter", AsyncMock(return_value=fake)):
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

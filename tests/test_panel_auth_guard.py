"""回归测试：面板/管理类路由必须鉴权。

历史上 /api/keys、/api/account、/api/playground 三组路由是裸奔的，
其中 GET /api/keys/export 可无鉴权导出全部上游 key 明文。本测试锁定
"无 Bearer → 拒绝、正确 Bearer → 放行"的契约，防止回归。
"""
import os

import pytest
from fastapi.testclient import TestClient

# 固定口令，避免读到环境/存储里的真实值
os.environ.setdefault("API_PASSWORD", "test-pwd")
os.environ.setdefault("PANEL_PASSWORD", "test-pwd")
os.environ.setdefault("CONFIG_OVERRIDE_ENV", "true")

import web  # noqa: E402

client = TestClient(web.app)

# 每组路由挑一个代表性的、纯读且无副作用的端点
PROTECTED_GET_ENDPOINTS = [
    "/api/keys",            # 列出 key
    "/api/keys/export",     # 敏感：导出 key
    "/api/account/accounts",
    "/api/playground/performance/models",
]


@pytest.mark.parametrize("path", PROTECTED_GET_ENDPOINTS)
def test_protected_endpoint_rejects_without_auth(path):
    resp = client.get(path)
    assert resp.status_code in (401, 403), (
        f"{path} 在无鉴权时应拒绝，实际 {resp.status_code}"
    )


@pytest.mark.parametrize("path", PROTECTED_GET_ENDPOINTS)
def test_protected_endpoint_rejects_wrong_token(path):
    resp = client.get(path, headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code in (401, 403), (
        f"{path} 在错误口令时应拒绝，实际 {resp.status_code}"
    )


def test_export_keys_not_leaked_without_auth():
    """最关键的回归：未鉴权时绝不能返回 key 列表。"""
    resp = client.get("/api/keys/export")
    assert resp.status_code in (401, 403)
    # 响应体不应包含明文 key 结构
    assert "keys" not in resp.text or resp.status_code in (401, 403)


def test_correct_token_passes_auth_layer(monkeypatch):
    """正确口令应通过鉴权层（不被 401/403 拦下）。

    端点自身后续可能因无数据返回别的状态，但绝不应是鉴权失败。
    用 monkeypatch 固定口令，避免依赖 CONFIG_OVERRIDE_ENV 的取值优先级。
    """

    async def _fixed_pwd():
        return "known-token"

    monkeypatch.setattr("src.api.auth.get_panel_password", _fixed_pwd)
    monkeypatch.setattr("src.api.auth.get_api_password", _fixed_pwd)

    resp = client.get(
        "/api/keys/export", headers={"Authorization": "Bearer known-token"}
    )
    assert resp.status_code not in (401, 403), (
        f"正确口令不应被鉴权拦截，实际 {resp.status_code}: {resp.text[:200]}"
    )

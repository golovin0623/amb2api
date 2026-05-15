"""/deploy-hook 鉴权和分发逻辑测试.

实际的 docker compose 调用通过 monkeypatch _spawn_deploy 隔离, 测试只覆盖
HTTP 层 + 签名验证 + 请求体校验.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import deploy_hook_api


SECRET = "test-secret-value-长一点"
TOKEN = "test-bearer-token"
EXPECTED_IMAGE = "golovin0623/amb2api"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DEPLOY_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("DEPLOY_WEBHOOK_TOKEN", TOKEN)
    monkeypatch.setenv("DEPLOY_EXPECTED_IMAGE", EXPECTED_IMAGE)
    spawn_calls: list[dict] = []

    def fake_spawn(image: str, tag: str, sha: str) -> None:
        spawn_calls.append({"image": image, "tag": tag, "sha": sha})

    monkeypatch.setattr(deploy_hook_api, "_spawn_deploy", fake_spawn)

    app = FastAPI()
    app.include_router(deploy_hook_api.router)
    tc = TestClient(app)
    tc.spawn_calls = spawn_calls  # type: ignore[attr-defined]
    return tc


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _payload(**overrides) -> bytes:
    base = {
        "image": EXPECTED_IMAGE,
        "tag": "sha-abc1234",
        "ref": "refs/heads/main",
        "sha": "abc1234567890abcdef000000000000000000000",
    }
    base.update(overrides)
    return json.dumps(base).encode("utf-8")


def test_hmac_signature_accepted(client):
    body = _payload()
    resp = client.post(
        "/deploy-hook",
        content=body,
        headers={
            "X-Hub-Signature-256": _sign(body),
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["image"] == EXPECTED_IMAGE
    assert data["tag"] == "sha-abc1234"
    assert client.spawn_calls == [  # type: ignore[attr-defined]
        {
            "image": EXPECTED_IMAGE,
            "tag": "sha-abc1234",
            "sha": "abc1234567890abcdef000000000000000000000",
        }
    ]


def test_hmac_bad_signature_rejected(client):
    body = _payload()
    resp = client.post(
        "/deploy-hook",
        content=body,
        headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
    )
    assert resp.status_code == 401
    assert client.spawn_calls == []  # type: ignore[attr-defined]


def test_bearer_token_fallback(client):
    body = _payload()
    resp = client.post(
        "/deploy-hook",
        content=body,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert resp.status_code == 200
    assert client.spawn_calls and client.spawn_calls[0]["tag"] == "sha-abc1234"  # type: ignore[attr-defined]


def test_bearer_wrong_token_rejected(client):
    body = _payload()
    resp = client.post(
        "/deploy-hook",
        content=body,
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401


def test_no_auth_header_rejected(client):
    body = _payload()
    resp = client.post("/deploy-hook", content=body)
    assert resp.status_code == 401


def test_image_mismatch_rejected(client):
    body = _payload(image="evil/registry-poisoning")
    resp = client.post(
        "/deploy-hook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body)},
    )
    assert resp.status_code == 400
    assert "image mismatch" in resp.json()["detail"]
    assert client.spawn_calls == []  # type: ignore[attr-defined]


def test_unexpected_tag_rejected(client):
    body = _payload(tag="malicious-floating-tag")
    resp = client.post(
        "/deploy-hook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body)},
    )
    assert resp.status_code == 400


def test_invalid_json_rejected(client):
    bad = b"not-json"
    resp = client.post(
        "/deploy-hook",
        content=bad,
        headers={"X-Hub-Signature-256": _sign(bad)},
    )
    assert resp.status_code == 400


def test_disabled_when_no_secrets(monkeypatch):
    monkeypatch.delenv("DEPLOY_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("DEPLOY_WEBHOOK_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(deploy_hook_api.router)
    with TestClient(app) as tc:
        resp = tc.post("/deploy-hook", json={"image": EXPECTED_IMAGE, "tag": "sha-1"})
        assert resp.status_code == 503


def test_signature_uses_constant_time_compare(client):
    """构造正确长度但每位都错的签名, 触发 compare_digest 而非 == 比较."""
    body = _payload()
    correct = _sign(body)
    wrong = "sha256=" + ("f" * len(correct[len("sha256=") :]))
    resp = client.post(
        "/deploy-hook",
        content=body,
        headers={"X-Hub-Signature-256": wrong},
    )
    assert resp.status_code == 401

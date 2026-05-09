"""Deploy webhook receiver.

接收 ci-cd.yml `deploy` job 发出的请求, 验证签名后异步触发
`docker compose pull && docker compose up -d`.

请求体由 .github/workflows/ci-cd.yml 构造:
    {"image": "...", "tag": "sha-<7>", "ref": "...", "sha": "..."}

认证优先级 (与 CI 端一致):
1. X-Hub-Signature-256: sha256=<hex>  HMAC-SHA256(body, DEPLOY_WEBHOOK_SECRET)
2. Authorization: Bearer <DEPLOY_WEBHOOK_TOKEN>            (向后兼容)

任一通过即可放行. 两个 secret 都未配置时路由会拒绝所有请求 (401), 防止
误启用导致无鉴权的 deploy endpoint.

部署动作通过 `subprocess.Popen(start_new_session=True)` 在独立进程组中
执行, 这样 docker compose up -d 杀掉自身容器时不会把 deploy 进程一起带走
—— 由 host docker daemon 接管完成滚动. 需要镜像内安装 docker CLI 并
挂载 /var/run/docker.sock, 见 Dockerfile 与 docker-compose.yml.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shlex
import subprocess
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from log import log


router = APIRouter()


def _expected_image() -> str:
    return os.environ.get("DEPLOY_EXPECTED_IMAGE", "golovin0623/amb2api")


def _compose_dir() -> str:
    return os.environ.get("DEPLOY_COMPOSE_DIR", "/app")


def _compose_file() -> Optional[str]:
    return os.environ.get("DEPLOY_COMPOSE_FILE") or None


def _deploy_log_path() -> str:
    return os.environ.get("DEPLOY_LOG_PATH", "/tmp/amb2api-deploy.log")


def _verify_signature(body: bytes, header: str, secret: str) -> bool:
    if not header.startswith("sha256="):
        return False
    received = header[len("sha256="):].strip()
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(received, expected)


def _verify_bearer(header: str, token: str) -> bool:
    if not header or not header.lower().startswith("bearer "):
        return False
    received = header.split(" ", 1)[1].strip()
    return hmac.compare_digest(received, token)


def _build_compose_cmd() -> list[str]:
    parts: list[str] = ["docker", "compose"]
    cf = _compose_file()
    if cf:
        parts += ["-f", cf]
    return parts


def _spawn_deploy(image: str, tag: str, sha: str) -> None:
    """非阻塞启动 docker compose pull && up -d.

    通过 start_new_session 脱离当前进程组, 避免新容器替换旧容器时把 deploy
    进程信号传播过来杀死中间步骤. stdout/stderr 重定向到日志文件以便事后排查.
    """
    base = _build_compose_cmd()
    pull = " ".join(shlex.quote(x) for x in (*base, "pull"))
    up = " ".join(shlex.quote(x) for x in (*base, "up", "-d"))
    shell_cmd = f"cd {shlex.quote(_compose_dir())} && {pull} && {up}"

    log_path = _deploy_log_path()
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    log_file = open(log_path, "ab", buffering=0)
    header = (
        f"\n=== deploy {sha or '?'} {datetime.now(timezone.utc).isoformat()} ===\n"
        f"image={image} tag={tag}\ncmd={shell_cmd}\n"
    ).encode("utf-8")
    log_file.write(header)

    subprocess.Popen(
        ["sh", "-c", shell_cmd],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )


@router.post("/deploy-hook")
async def deploy_hook(request: Request):
    secret = os.environ.get("DEPLOY_WEBHOOK_SECRET", "")
    token = os.environ.get("DEPLOY_WEBHOOK_TOKEN", "")
    if not secret and not token:
        # 显式 disable 比"任何人都能 POST"更安全; 配置后再启用.
        raise HTTPException(status_code=503, detail="deploy webhook not configured")

    body = await request.body()

    sig_header = request.headers.get("X-Hub-Signature-256", "")
    auth_header = request.headers.get("Authorization", "")

    authenticated = False
    if secret and sig_header and _verify_signature(body, sig_header, secret):
        authenticated = True
    elif token and auth_header and _verify_bearer(auth_header, token):
        authenticated = True

    if not authenticated:
        log.warning("[deploy-hook] rejected: signature/token mismatch")
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")

    image = str(payload.get("image", ""))
    tag = str(payload.get("tag", ""))
    sha = str(payload.get("sha", ""))[:40]

    expected = _expected_image()
    if image != expected:
        raise HTTPException(
            status_code=400,
            detail=f"image mismatch: expected {expected}, got {image!r}",
        )
    if not tag.startswith(("sha-", "v", "main", "latest")):
        raise HTTPException(status_code=400, detail=f"unexpected tag: {tag!r}")

    log.info(
        f"[deploy-hook] accepted image={image} tag={tag} sha={sha[:7] or '?'}"
    )

    try:
        _spawn_deploy(image=image, tag=tag, sha=sha)
    except Exception as exc:  # noqa: BLE001 — 上报给 CI, 不要静默
        log.error(f"[deploy-hook] spawn failed: {exc}")
        raise HTTPException(status_code=500, detail=f"spawn failed: {exc}")

    return {
        "status": "accepted",
        "image": image,
        "tag": tag,
        "sha": sha[:7] if sha else None,
    }


__all__ = ["router"]

# amb2api GitHub Actions

本目录沿用 [Aetherblog](https://github.com/golovin0623/AetherBlog) 的 CI/CD 风格，
针对 amb2api 单镜像项目做了适配。包含两条工作流：自动化主流水线与手动快速构建。

## 工作流

### 1. `ci-cd.yml` —— CI/CD 主流水线

**触发条件:**

| 事件 | 行为 |
| --- | --- |
| `pull_request` to `main` | 跑 gitleaks / test / config-validate (不构建镜像) |
| `push` to `main` | 全流程: 检查 → 构建 → 推送 → Trivy 扫描 → 部署 |
| `push` tag `v*` | 检查 → 构建 → 推送 (不部署) |
| 手动 `workflow_dispatch` | 完整跑一遍 |

**Job 结构:**

```text
gitleaks ────────────────────────┐
test ──────┐                     │
            ├── docker-build-push ┼── trivy-scan (main only)
config-validate                   │
                                  └── deploy (main only, webhook)
```

- `gitleaks` —— 全历史扫描硬编码 secret / JWT / API key（当前 `continue-on-error`，等基线清理完后切换为强阻断）。
- `test` —— Python 3.12 + `pytest -q`；`hypothesis` 单独安装（`pyproject.toml` 把它放在 `[dependency-groups].dev`，未来 Python 3.13+ 可换 `pip install --group dev`）。
- `config-validate` —— `docker compose -f docker-compose.yml config --quiet`。
- `docker-build-push` —— `linux/amd64,linux/arm64` 多平台构建，**双缓存**：GHA scope `amb2api` + Docker Hub registry `:buildcache`。Docker 凭证未配置时整 job 优雅跳过。
- `trivy-scan` —— 仅 `main`，扫 `sha-<7位>` 镜像的 CRITICAL/HIGH CVE，当前 `exit-code=0` 仅观测。
- `deploy` —— 仅 `main`，调用 `DEPLOY_WEBHOOK_URL`，**优先 HMAC-SHA256 签名**，无 secret 时回落到 Bearer Token。

**镜像 tag 策略:**

| 触发方式 | 推送的 tag |
| --- | --- |
| push `main` | `sha-<7位>`, `main`, `latest` |
| push tag `vX.Y.Z` | `sha-<7位>`, `vX.Y.Z`, `latest` |
| Quick Build (手动) | 用户输入 `version`, `sha-<7位>`, 可选 `latest` |

### 2. `quick-build.yml` —— 紧急快速构建

手动 `workflow_dispatch`，跳过测试 / 安全扫描 / 部署，仅构建并推送镜像。
适用场景：紧急热修复、跳过 CI 验证某个 commit 是否能成功打镜像。
与 `ci-cd.yml` 共用 GHA + registry 双缓存（同 scope `amb2api`），命中率高。

## 必要 Secrets

仓库 `Settings -> Secrets and variables -> Actions` 配置：

| Secret | 必填 | 说明 |
| --- | --- | --- |
| `DOCKER_USERNAME` | 推送镜像所需 | Docker Hub 用户名（默认 `golovin0623`，与 `docker-compose.yml` 镜像引用一致） |
| `DOCKER_PASSWORD` | 推送镜像所需 | Docker Hub Access Token（建议用 token 而非密码） |
| `DEPLOY_WEBHOOK_URL` | 启用自动部署 | 部署 webhook HTTPS endpoint |
| `DEPLOY_WEBHOOK_SECRET` | HMAC 模式（推荐） | 与服务器端共享的 HMAC 密钥；CI 用 SHA256 签名请求体并发送 `X-Hub-Signature-256: sha256=<hex>` |
| `DEPLOY_WEBHOOK_TOKEN` | Bearer 模式（兼容回落） | 直接发 `Authorization: Bearer <token>`；当 `DEPLOY_WEBHOOK_SECRET` 也存在时优先用 HMAC |

> 默认行为：`DOCKER_USERNAME`/`DOCKER_PASSWORD` 或 `DEPLOY_WEBHOOK_URL` 缺失时
> 对应 job 直接 `fail`（避免静默跳过几个月没人发现）。如果你是首次接入仓库
> 还没来得及配 secret，可以在 `Settings → Variables` 里加：
>
> - `ALLOW_MISSING_DOCKER_CREDS=true` —— 允许缺 Docker 凭证时跳过 build/push
> - `ALLOW_MISSING_DEPLOY_WEBHOOK=true` —— 允许缺 webhook URL 时跳过 deploy
>
> 这两个变量是 secrets 之外的"逃生口"，配齐 secrets 后建议删除。

## 启用 deploy webhook（端到端步骤）

amb2api 自带 `/deploy-hook` 路由（`src/api/deploy_hook_api.py`），可以直接
作为接收端，免于额外起服务。整个链路：

```
GitHub Actions → POST <DEPLOY_WEBHOOK_URL> → amb2api 容器 /deploy-hook
                                              → docker compose pull && up -d
                                                (走挂载的 /var/run/docker.sock)
```

### 1. 服务器端：挂 docker socket 与 compose 目录

`docker-compose.yml` 已经声明了所需挂载点。把宿主机上**真正存放
`docker-compose.yml` 的目录**通过 `DEPLOY_HOST_COMPOSE_DIR` 暴露给容器：

```bash
# 服务器上的 .env
DEPLOY_WEBHOOK_SECRET=<openssl rand -hex 32 生成的随机串>
DEPLOY_HOST_COMPOSE_DIR=/srv/amb2api          # 宿主机 compose 文件目录
DEPLOY_COMPOSE_DIR=/deploy                    # 容器内挂载点 (默认)
DEPLOY_EXPECTED_IMAGE=golovin0623/amb2api     # 防止恶意请求触发不相干镜像 pull
```

`DEPLOY_WEBHOOK_SECRET` 必须和 GitHub 仓库 secrets 里的一致。

### 2. 暴露 webhook URL

amb2api 监听端口（默认 7861）需要让 GitHub runner 可达：

- 直接公网：`https://your-domain.com/deploy-hook`
- 反向代理：nginx `location /deploy-hook { proxy_pass http://127.0.0.1:7861; }`

### 3. 仓库 secrets / variables

| 项 | 值 | 说明 |
| --- | --- | --- |
| `secrets.DOCKER_USERNAME` | Docker Hub 用户名 | 用于推 `golovin0623/amb2api` |
| `secrets.DOCKER_PASSWORD` | Docker Hub access token | 同上 |
| `secrets.DEPLOY_WEBHOOK_URL` | `https://your-domain.com/deploy-hook` | CI 调用目标 |
| `secrets.DEPLOY_WEBHOOK_SECRET` | 与服务器 `.env` 一致的随机串 | HMAC 鉴权 |

### 4. 验证

合一次 PR 到 main 后，去 Actions 看 `deploy` job：

```text
Webhook HTTP status: 200
Webhook response: {"status":"accepted","image":"golovin0623/amb2api","tag":"sha-abc1234","sha":"abc1234"}
✅ Deployment completed successfully
```

服务器上 `tail -f /srv/amb2api/deploy.log`（或 `DEPLOY_LOG_PATH` 指定的位置）
能看到 `docker compose pull && up -d` 的实时输出。

### 5. 安全注意

- `/deploy-hook` 不走 `API_PASSWORD` / `PANEL_PASSWORD` —— 鉴权完全靠 HMAC。
  secret 一旦泄漏对方就可以触发 `docker compose up -d`，所以把 secret 当
  生产凭据保管（`openssl rand -hex 32`，定期轮换）。
- 路由在没配 `DEPLOY_WEBHOOK_SECRET` 也没配 `DEPLOY_WEBHOOK_TOKEN` 的情况下
  会 503 拒绝所有请求，避免误启用成无鉴权 endpoint。
- `DEPLOY_EXPECTED_IMAGE` 限制只能拉指定镜像，防止有人构造请求去 pull
  恶意 registry。

### 部署请求体

```json
{
  "image": "<registry>/amb2api",
  "tag": "sha-<7位>",
  "ref": "refs/heads/main",
  "sha": "<完整 commit sha>"
}
```

服务端建议：解析 `tag` 然后 `docker pull` + `docker compose up -d`，
或者按 image+tag 重写 `docker-compose.yml`/`.env` 的 `IMAGE_TAG` 后再 reload。
HMAC 验签参考：

```python
import hmac, hashlib
expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
hmac.compare_digest(request.headers["X-Hub-Signature-256"], expected)
```

## 常用操作

### 创建版本发布

```bash
git checkout main && git pull
git tag -a v0.6.1 -m "Release v0.6.1"
git push origin v0.6.1
# 自动构建并推送:
#   - golovin0623/amb2api:v0.6.1
#   - golovin0623/amb2api:latest
#   - golovin0623/amb2api:sha-<7位>
```

### 服务器端拉取最新镜像

```bash
ssh user@your-server
cd /path/to/amb2api
docker compose pull
docker compose up -d
```

（自动部署 webhook 已配置时会自动完成上面这两步。）

### 手动触发紧急构建

1. GitHub 仓库 → Actions
2. 选择 `Quick Docker Build` workflow
3. `Run workflow` → 输入 `version`（如 `hotfix-20260508`）
4. ~3 分钟内镜像推送完成

### 查看构建状态

```bash
gh run list --workflow=ci-cd.yml --limit 5
gh run watch
```

## 与 `release.sh` / `start.sh` 的关系

- `start.sh` 仍是本地一键启动入口（创建 venv、`uv sync`、装环境变量、`python web.py`）。
- `release.sh`（如有）仍可用于本地手工发布。
- GitHub Actions 是默认自动化路径，团队协作 / 稳定回溯应通过 PR + main push 触发。

## 与 Aetherblog 的差异

| 维度 | Aetherblog | amb2api |
| --- | --- | --- |
| 模块拆分 | monorepo，按 backend/ai/blog/admin 拆 build | 单镜像，无 `paths-filter` |
| 测试栈 | Go + Python + pnpm | 纯 Python (`pytest`) |
| 多架构 | `linux/amd64` | `linux/amd64,linux/arm64` |
| `forbidden-defaults-guard` | 有（防止 `pwd123` / `VERSION=latest` 入库） | 暂未启用（amb2api `pwd` 是合理 dev 默认） |
| `frontend-quality` | 有（lint/typecheck/audit） | 不适用 |
| Webhook 报文 | `{"services": "..."}` | `{"image":..., "tag":..., "ref":..., "sha":...}` |

## 状态徽章

```markdown
![CI/CD](https://github.com/GolovinElics/amb2api/actions/workflows/ci-cd.yml/badge.svg)
```

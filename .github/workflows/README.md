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
                                  └── deploy (main only, SSH)
```

- `gitleaks` —— 全历史扫描硬编码 secret / JWT / API key（当前 `continue-on-error`，等基线清理完后切换为强阻断）。
- `test` —— Python 3.12 + `pytest -q`；`hypothesis` 单独安装（`pyproject.toml` 把它放在 `[dependency-groups].dev`，未来 Python 3.13+ 可换 `pip install --group dev`）。
- `config-validate` —— `docker compose -f docker-compose.yml config --quiet`。
- `docker-build-push` —— `linux/amd64,linux/arm64` 多平台构建，**双缓存**：GHA scope `amb2api` + Docker Hub registry `:buildcache`。Docker 凭证未配置时整 job 优雅跳过。
- `trivy-scan` —— 仅 `main`，扫 `sha-<7位>` 镜像的 CRITICAL/HIGH CVE，当前 `exit-code=0` 仅观测。
- `deploy` —— 仅 `main`，通过 **SSH 登录服务器**执行 `docker compose pull && up -d` 切换到新镜像。未配置 `DEPLOY_SSH_HOST` 时整 job 优雅跳过（不阻断流水线）。

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
| `DEPLOY_SSH_HOST` | 启用自动部署 | 服务器 IP 或域名；**缺失时 `deploy` job 直接跳过** |
| `DEPLOY_SSH_USER` | 启用自动部署 | SSH 用户名（如 `root`） |
| `DEPLOY_SSH_KEY` | 二选一（推荐） | SSH 私钥全文；与 `DEPLOY_SSH_PASSWORD` 二选一 |
| `DEPLOY_SSH_PASSWORD` | 二选一 | SSH 登录密码；与 `DEPLOY_SSH_KEY` 二选一 |
| `DEPLOY_SSH_PORT` | 可选 | SSH 端口，默认 `22` |
| `DEPLOY_PATH` | 可选 | 服务器上 `docker-compose.yml` 所在目录，默认 `/root/amb2api` |

> 未配置 `DEPLOY_SSH_HOST` 时 `deploy` job 优雅跳过（不阻断流水线），方便首次接入时先把构建跑通。

### 部署是怎么发生的

`deploy` job 用 [`appleboy/ssh-action`](https://github.com/appleboy/ssh-action)
通过 SSH 登录服务器，在 `DEPLOY_PATH` 目录下执行：

```bash
docker compose pull     # 拉取刚 push 的最新镜像
docker compose up -d     # 用新镜像重启容器
docker image prune -f    # 清理旧镜像
```

因为 `docker-compose.yml` 里镜像写的是 `golovin0623/amb2api:latest`，
而 `docker-build-push` job 每次都会把 `:latest` 指向最新 commit，所以
服务器 `pull` 到的就是最新版本。整条链路不需要在应用里开 webhook 接口，
也不需要把 `docker.sock` 挂进容器。

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

（配好 SSH secrets 后，每次 push 到 `main` 会自动完成上面这两步。）

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
| 部署方式 | Webhook (`{"services": "..."}`) | SSH 登录跑 `docker compose pull && up -d` |

## 状态徽章

```markdown
![CI/CD](https://github.com/GolovinElics/amb2api/actions/workflows/ci-cd.yml/badge.svg)
```

# amb2api GitHub Actions

本目录包含从 Aetherblog 迁移并按 amb2api 简化后的 CI/CD 工作流。

## 工作流

1. `ci-cd.yml`
- 触发: `push(main)`, `push(tag: v*)`, `pull_request(main)`, 手动触发
- 流程:
  - Python 3.12 安装依赖并执行 `pytest -q`
  - 校验 `docker-compose.yml`
  - 在 `main` 或 `v*` tag 推送时自动构建并推送 Docker 镜像
  - `main` 分支推送成功后可自动触发部署 webhook

2. `quick-build.yml`
- 触发: 手动 `workflow_dispatch`
- 用途: 跳过测试，快速构建并推送镜像（适合紧急修复）

## 必要 Secrets

在仓库 `Settings -> Secrets and variables -> Actions` 配置:

- `DOCKER_USERNAME`: Docker Hub 用户名
- `DOCKER_PASSWORD`: Docker Hub Access Token / 密码

可选:

- `DEPLOY_WEBHOOK_URL`: 部署 webhook 地址（配置后 main 推送会自动部署）
- `DEPLOY_WEBHOOK_TOKEN`: 部署 webhook Bearer Token（可选）

## 镜像标签策略

- `main` 推送: `main`, `latest`, `sha-<7位commit>`
- `v*` 标签推送: `<tag>`, `latest`, `sha-<7位commit>`
- 快速构建: 使用手动输入的 `version`，可选同时推 `latest`

## 与 `release.sh` 的关系

- `release.sh` 仍可用于本地手工发布。
- GitHub Actions 提供自动化发布与部署，适合团队协作与稳定回溯。

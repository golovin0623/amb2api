# AGENTS.md

本文件面向在本仓库内协作的代码代理（AI agents），目标是：快速理解项目、低风险修改、可验证交付。

## 1. 项目定位

`amb2api` 是一个 AssemblyAI LLM Gateway 的 OpenAI 兼容代理，主要提供：

- OpenAI 兼容接口：`/v1/models`、`/v1/chat/completions`
- 管理面板与配置接口：`/ui`、`/config/*`、`/usage/*`
- 密钥管理与速率限制跟踪：`/api/keys/*`
- Assembly 账户相关能力：`/api/account/*`
- 请求操练场：`/api/playground/*`

主入口在 `web.py`，所有路由在此挂载。

## 2. 环境与依赖

- Python 要求：`>=3.12`（见 `pyproject.toml`）
- 包管理：
  - 优先 `uv sync`
  - 备选 `pip install .`
- 本地默认启动脚本：`./start.sh`

注意：

- 当前仓库以 `pyproject.toml` 为准，不依赖 `requirements.txt`。
- 测试依赖位于 `[dependency-groups].dev`（如 `pytest`、`hypothesis`）。

## 3. 快速启动（开发）

推荐：

```bash
./start.sh
```

等价手动流程（最小化）：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip wheel setuptools
python -m pip install -e .
python web.py
```

服务默认监听：

- API: `http://127.0.0.1:7861/v1`
- Panel: `http://127.0.0.1:7861/ui`

## 4. 测试与验证

运行测试前先安装 dev 依赖（否则会缺少 `hypothesis`）：

```bash
python -m pip install -e ".[dev]"
```

然后执行：

```bash
python -m pytest -q
```

针对单文件：

```bash
python -m pytest tests/test_key_management_api.py -q
```

## 5. 代码结构（高频改动区）

- `web.py`：应用入口、生命周期、路由聚合
- `src/api/`：HTTP API 层
  - `openai_router.py`：OpenAI 兼容接口
  - `admin_routes.py`：管理面板与配置、日志流
  - `key_management_api.py`：密钥管理
  - `account_api.py`：Assembly Dashboard 会话与账单/用量
  - `playground_api.py`：请求预览与自定义请求
- `src/services/`：核心业务逻辑（key 轮换、限流、上游请求）
- `src/storage/`：存储抽象与后端实现
- `src/stats/`：统一统计与性能追踪
- `src/transform/`：请求/响应转换与消息优化
- `tests/`：pytest + hypothesis 测试

## 6. 配置与存储约定

### 配置读取优先级

配置由 `config.py` 统一读取，受 `CONFIG_OVERRIDE_ENV` 和持久化配置影响。

- 常见环境变量：`API_PASSWORD`、`PANEL_PASSWORD`、`ASSEMBLY_API_KEYS`、`REDIS_URI`、`POSTGRES_DSN`、`MONGODB_URI`、`PORT`、`HOST`
- 管理面板可写入配置（通过 storage adapter）

### 存储后端优先级

`src/storage/storage_adapter.py` 的优先级是：

1. Redis
2. Postgres
3. MongoDB
4. File（默认）

File 模式下默认写入 `./creds/creds.toml` 与 `./creds/config.toml`。

## 7. 修改原则（必须遵守）

- 优先做最小改动，不做无关重构。
- API 行为变化时，必须同步更新或新增测试。
- 不要在日志中输出完整密钥、会话 token、用户敏感信息。
- 避免在 async 路径引入阻塞式 I/O。
- 变更配置键名时，检查：
  - `config.py`
  - `admin_routes.py`
  - 相关测试
- 变更模型/路由时，检查前端面板是否受影响（`front/control_panel.html`）。

## 8. 提交前检查清单

至少完成：

1. 目标相关测试通过（最小范围 + 必要回归）。
2. 新增/修改接口的错误路径已验证（鉴权失败、参数非法、上游失败）。
3. 不引入明文 secrets、无调试残留日志。
4. 文档与实际命令一致（尤其启动与测试命令）。

## 9. 常见任务建议

- 新增 API 字段：先改 Pydantic model，再改 transform/service，最后补测试。
- 调整 key 轮换策略：优先看 `src/services/key_manager.py` 与 `src/services/rate_limiter.py`，并回归 `tests/test_rate_limit_*.py`。
- 调整统计逻辑：优先看 `src/stats/unified_stats.py`，并回归 `tests/test_usage_aggregation.py`。
- 调整流式行为：重点关注 `openai_router.py` 与 `assembly_stream_handler.py`，补充 fake/real streaming 测试。

---

如发现本文档与代码冲突，以代码为准，并在同一改动中更新 `AGENTS.md`。

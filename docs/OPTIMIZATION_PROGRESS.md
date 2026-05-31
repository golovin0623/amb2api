# 优化进度追踪 (OPTIMIZATION_PROGRESS)

> 目的：抵御上下文压缩导致的"重复劳动 / 丢进度"。每完成一个阶段就 **先本地 commit**，
> 并在此更新清单。恢复工作时**先读本文件 + `git log --oneline`**，再动手。

## 如何恢复工作（context 丢失后照这个走）
1. `cd /home/user/amb2api && git status -sb && git log --oneline -8`
2. 测试环境：`. .venv/bin/activate`（Python 3.12）；若 `.venv` 缺失：
   `python3.12 -m venv .venv && . .venv/bin/activate && pip install -e . && pip install "hypothesis>=6.148.2" "pytest>=9.0.1" "pytest-asyncio>=1.3.0"`
3. 跑测试基线：`python -m pytest -q`（应 **≥233 passed**）
4. 对照下方清单，从第一个 ⬜/⏳ 项继续。**动手前先 grep 验证该项是否已完成**（避免重复）。

## 状态图例
✅ 完成并提交 · ⏳ 进行中 · ⬜ 待办 · 🔒 延后（需产品决策或属高风险独立专项）

---

## 已完成（commit 哈希见 `git log`）

| # | 阶段 | 状态 | 关键提交主题 |
|---|------|------|-------------|
| 1 | 诊断报告（UI/功能/性能/能力/市场） | ✅ | docs: add full product diagnostic report |
| 2 | P0 安全：三组路由鉴权 + CORS + 停记口令 + /health | ✅ | fix(security): P0 |
| 3 | 性能：共享 HTTP 连接池 + HTTP/2 + 有界超时 | ✅ | perf(http) |
| 4 | 性能：配置快照缓存 + 写穿失效 | ✅ | perf(config) |
| 5 | fork 清理：proxy_manager/anti_truncation/openai_transfer Gemini 死函数/config Gemini 辅助 | ✅ | refactor: drop dead gcli2api residue |
| 6 | 日志按大小轮转 + 持久句柄 | ✅ | fix(logging) |
| 7 | 多模态 image_url 透传 | ✅ | fix(multimodal) |
| 8 | 前端文件名 XSS 转义 | ✅ | fix(security/ui) |
| 9 | 删 Gemini 配额模型（usage_stats/state_manager + admin 特例字段） | ✅ | refactor(stats) |
| 10 | 上手文档：.env.example + README 修复 + 死链清理 | ✅ | docs: fix broken onboarding |
| 11 | 存储层死方法移除（4 后端 + Protocol + facade） | ✅ | refactor(storage): remove dead per-file usage-stats |
| 12 | 存储模板 gemini 残键清零（src/ 零 gemini_2_5_pro） | ✅ | refactor(storage): drop inert gemini keys |

| 13 | 限流双系统统一到 RateLimiter（单一真相源） | ✅ | 8ffad4f |
| 14 | 限流保存去抖 5s + lifespan flush | ✅ | bb6cd3e |
| 15 | 优雅关闭刷新 unified_stats | ✅ | baeba37 |
| 16 | 删 2 个永不服务的 HTML（21,849 行死代码） | ✅ | 0e27792 |

净删 ~23,700 行（含 fork 死代码 + 死 HTML）；新增回归测试：auth / shared_http_client / config_cache / log_rotation / multimodal / graceful_flush，重写 2 个限流测试。C、E 经验证为"无需改动/架构级"（见主分析报告第十节）。

---

## 待办 / 进行中（本轮继续）

| # | 项 | 状态 | 风险 | 备注 |
|---|----|------|------|------|
| A | 限流双写竞态：旧 `_rate_limit_info` 与 `RateLimiter` 写同一键 | ✅ | 中 | 已统一到 RateLimiter，删旧系统，重写 2 个测试 (8ffad4f) |
| B | 每请求写风暴合并（rate-limit save 去抖） | ✅ | 中 | RateLimiter 保存去抖 5s + lifespan flush (bb6cd3e) |
| C | 配额 TOCTOU（预占式重设计） | ✅ | 中高 | TTL 自愈预占，并发不超计 (7e8d1d9)；"流 success" 实为 2xx 头，保留 |
| D | 优雅关闭刷新 stats（不丢在途写） | ✅ | 低 | flush_unified_stats + lifespan (baeba37) |
| E | 流式 client 断连检测 | ✅ | 中 | 已验证：Starlette 1.2.0 断连即取消流任务→generator finally 释放上游连接 |

> **E 的结论（经验证）**：Starlette 1.2.0 的 `StreamingResponse` 自带 `listen_for_disconnect`，客户端断连时用 anyio 取消流任务，触发我们 `upstream_stream_generator` 的 `finally: stream_ctx.__aexit__()` 释放上游连接。诊断报告 6.6 的"会一直空跑上游到结束"在此 Starlette 版本不成立。显式 `is_disconnected()` 仅是微优化，但需把 Request 侵入式透传进整条管线，性价比低，不做。

> **C 的结论（经验证）**：`record_call` 的自增是同步的（await 之间无让点 → 事件循环内原子），**不存在丢更新**。真正的 TOCTOU 在"选 key"与"记账"之间（中间隔了整个上游请求），正确修法是**预占式配额**（选 key 时原子 check-and-reserve、失败再回滚），属架构级改动且会改动 `test_daily_usage_limits` 锁定的语义。"连上即 success" 实为"拿到 2xx 响应头"，是合理的成功近似。仓促改配额语义的风险高于现状（有界的轻微超计），故按独立专项延后。

## 🚧 大工程（用户已拍板，全部要做；按 C→多租户→前端 V2 顺序）

| 项 | 状态 | 备注 |
|----|------|------|
| **T. per-user token 多租户** | ✅ | T1-T6 全完成：TokenManager + API 面鉴权 + 原子配额 + /api/tokens CRUD + 面板"用户令牌"页 |
| **U. 前端 V2 收尾** | ⏳(部分) | 已做可验证项；CSS 大重构需浏览器可视 QA（见下） |

### U 已完成（可验证：/ui 200 + node --check）
- [x] 口令 localStorage→sessionStorage + 旧值迁移清理 (1bff40f)
- [x] 删 29 行独立调试 console.log（保留 error/warn）(44669a3)
- [x] 新增"用户令牌"页（T5，见上）

### U 待办——**需要真实浏览器可视 QA，不能盲改**（盲改会有"看不见的破样"风险，违背验收）
- [ ] 删 6600 行内联旧 CSS + `legacy-bridge.css` 的 !important 战争（真正迁移到 v2 而非压制）。内联 CSS 可能含**布局**规则，外部 CSS 未必全覆盖，删了无浏览器验证 = 高风险
- [ ] emoji→SVG 图标、暗色/移动/可访问性打磨、`showStatus`(119处)→统一 toast
- 建议：在能跑浏览器的环境里（或你本地）配合可视回归逐项推进；我可以按页面/组件出小步 PR，由你或 CI 截图验收

### 多租户(T)拆解清单
- [x] T1 数据模型 + TokenManager（quota/模型白名单/过期/启用/已用）+ 存储 (6a8280e)
- [x] T2 鉴权：API 面支持"主口令 || user token"，identity 挂 request.state (e3ade1a)
- [x] T3 配额：try_consume 原子 check-and-increment（无 TOCTOU），超额 429 (6a8280e/e3ade1a)
- [x] T4 管理 API：`/api/tokens` CRUD（面板口令保护）(e3ade1a)
- [x] T5 面板 UI：token 管理页（列表/新建/禁用/删除/清零/复制）(71759b1)
- [x] T6 测试：CRUD + 鉴权放行/拒绝 + 配额耗尽 + 模型白名单 (e3ade1a)

> ✅ 多租户已端到端可用：面板"用户令牌"页或 `POST /api/tokens` 发 token，下游用该
> token 调 `/v1/chat/completions` 或 `/v1/messages`，受配额/模型白名单/过期约束。

## ✅ 代码评审 + 安全评审 + 实跑验收（本轮）

用 skills 做了彻底验收，并迭代修复到干净：

- **code-review（high effort，7 角度）** → 发现并**全部修复**：
  - 配额预占在重试/早返回/异常路径泄漏 + 异常处理器对 keys[0] 过度释放 → 解耦 record_call，改由每次尝试的 `finally` 精确归还一次
  - token `allowed_models=[]` 误放行所有模型、`expires_at=0` 误判永不过期 → `_check_meta` 修正
  - httpx 改代理时立即 aclose 打断在途流 → 改 330s 宽限延迟关闭
  - 清理类：`_check_meta`/`_quota_state` 去重、`DebouncedSaver` mixin 统一去抖、`import os`
- **security-review** → **无 HIGH 新增漏洞**；2 个 LOW 已修：count_tokens 现强制 token 模型白名单（不消费配额）、CORS 混合 `*,origin` 强制关 credentials
- **fix 复审（独立 agent）** → 6 项关注点全部 OK、无新增 bug；预占释放严格 1:1（idx≥0 ⟺ 恰好一次预占）
- **实跑验收**（真实 hypercorn 服务）：/health=ok、/ui=200、错误口令 403、令牌 CRUD、白名单外模型 403、`allowed_models=[]` 拒绝、配额耗尽 429 —— 全部实测通过
- 测试：**248 passed**；所有改动文件 `py_compile` 通过、前端 inline JS `node --check` 通过

> 结论：分支无遗留的代码评审/安全问题；上述发现均已修复并回归。

## 🔒 仍延后（需单独决策）
| 项 | 原因 |
|----|------|
| anthropic_transfer.py 的 `Task` 入参归一化移植 | 生产链路缺这块；产品取舍 |
| 多 worker 横向扩展（进程内计数器迁共享存储） | 架构级，建议多租户稳定后做 |

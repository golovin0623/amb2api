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

净删 ~1583 行 fork 死代码；新增回归测试：auth / shared_http_client / config_cache / log_rotation / multimodal。

---

## 待办 / 进行中（本轮继续）

| # | 项 | 状态 | 风险 | 备注 |
|---|----|------|------|------|
| A | 限流双写竞态：旧 `assembly_client._rate_limit_info` 与 `RateLimiter` 写同一 `rate_limit_info` 键 | ⬜ | 中 | 触及 key 选择；需配套测试 |
| B | 每请求写风暴合并（rate-limit + stats 多次 fire-and-forget save） | ⬜ | 中 | |
| C | 配额 TOCTOU + "流连接建立即计 success" | ⬜ | 中高 | 改配额语义，回归 test_daily_usage_limits |
| D | fire-and-forget task 纳入 TaskManager（优雅关闭不丢写） | ⬜ | 低 | |
| E | 流式 client 断连检测（`is_disconnected`），停止空跑上游 | ⬜ | 中 | 需把 Request 透传进生成器 |

## 🔒 延后（需你拍板 / 独立专项）

| 项 | 原因 |
|----|------|
| anthropic_transfer.py 的 `Task` 入参归一化是否移植进生产 `openai_to_claude` | 产品取舍：生产链路目前**缺**这块归一化，那 671 行只被测试引用 |
| 前端 V2 收尾 or 回滚（`!important` 战争 / 3 个 HTML / localStorage 明文口令 / 暗色·移动·a11y） | 19.8k 行单体，独立前端专项 |
| 多 worker 横向扩展（进程内计数器迁共享存储） | 架构级，需先定方向 |
| per-user token + 配额 + 货币成本核算 | 决定"个人韧性代理"vs"对外分发平台" |

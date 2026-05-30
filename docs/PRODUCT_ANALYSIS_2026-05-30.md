# amb2api 产品深度诊断报告

> 日期：2026-05-30 · 范围：UI/UX、功能、性能、能力、市场对标
> 方法：通读 ~82k 行代码（含 19.8k 行控制面板）、四路并行深审 + 关键结论逐条验证（file:line）

---

## 一、一句话结论

**amb2api 的"协议层"是认真做的（双 OpenAI + Anthropic 兼容面、prompt-cache、tool calling 都有水准），但它外面包着一层"没做完"的壳：一套半途而废的 UI 重构、一身来自 `gcli2api`（Gemini CLI 代理）fork 的死代码、以及几个会让人不敢真正对外部署的安全与性能硬伤。** 你"觉得是半成品"不是错觉——它在代码里有明确、可定位的证据。

**当前可用性定位：可作为"个人/受信内网"的中转 demo；不可直接对公网/多用户生产部署。**

### 成熟度评分（10 分制，越低越"半成品")

| 维度 | 评分 | 一句话 |
|---|---|---|
| 协议兼容 / 格式转换 | **7** | 真本事所在：双面兼容 + 契约冻结 + tool/caching |
| 功能完整度 | **4** | 多模态被丢、n>1/logprobs/抗截断"名存实亡" |
| **安全性** | **2** | 🔴 整组路由零鉴权、明文导出 key、明文密码进日志 |
| 性能 / 可扩展 | **3** | 每请求新建 HTTP 连接、单进程、配置每请求读 12+ 次 |
| 可靠性 / 正确性 | **4** | 流式无超时/无断连、配额竞态、成功在"连上"即计 |
| UI / UX | **3** | 重构停在半路、CSS 自相残杀（1900+ !important） |
| 可运维性 | **3** | 无健康检查、日志不切割、容器 root 运行 |
| 代码卫生 / 可维护 | **3** | fork 死代码 + 4 套统计 + 3 个 HTML + 文档腐烂 |
| 文档 | **3** | README 引用 14 个不存在的文档、装不上 |
| 测试 | **6** | 27 个测试文件 + hypothesis，但覆盖有缺口 |

---

## 二、根因诊断：三条贯穿全身的主线

### 主线 1：它是 `gcli2api` 的 fork，"换壳未换心"

整个项目是从一个 Gemini CLI→API 代理改过来的，但**核心数据模型、配置、死代码从未真正清理**：

- `config.py:1` 模块 docstring 仍写着 *"Configuration constants for the **Geminicli2api** proxy server"*。
- `config.py:13-21,222-236,50-70`：`DEFAULT_SAFETY_SETTINGS`（Google 安全等级）、Gemini `BASE_MODELS`、`get_thinking_budget`、`is_search_model`、`-maxthinking/-nothinking` 等全是 Gemini 时代遗留，AssemblyAI 路径根本不用。
- **配额系统至今按 `gemini_2_5_pro_calls` 建模**：`src/stats/usage_stats.py` 通篇是 `gemini_2_5_pro_calls` / `daily_limit_gemini_2_5_pro`，`admin_routes.py:416-422` 还在为 `gemini-2.5-pro` 单独开特例——对一个服务 GPT/Claude/Qwen/Kimi 的网关毫无意义。
- `get_mongodb_database()` 默认库名仍是 `"gcli2api"`（`config.py:432`）。
- 死模块：`src/services/proxy_manager.py`（260 行，**全仓库零 import**）、`src/transform/anthropic_transfer.py`（671 行，**只被测试引用**，与生产用的 `claude_to_openai.py`/`openai_to_claude.py` 是两套并行 Claude 转换器）、`src/transform/openai_transfer.py` 里成片的 `openai_request_to_gemini_payload`/`gemini_response_to_openai`/`gemini_stream_chunk_to_openai`（死的 Gemini 函数）。
- `src/transform/anti_truncation.py`（588 行）整体按 Gemini 的 `contents/parts/systemInstruction` 结构写——**与 OpenAI messages 根本不兼容**。

> 这是"半成品感"的最大单一来源：你以为在维护一个 AssemblyAI 产品，实际有相当比例的代码在为一个已经不存在的 Gemini 产品服务。

### 主线 2：UI 正停在一场未完成的重构中途

`plan/` 下有 **两份** UI 重构方案（`UI_REFACTOR_PLAN.md` 标注"已完成"，`UI_REFACTOR_V2_PLAN.md` 是 12 阶段路线图）。V2 计划里明确写着"命令面板：未实现"，而代码现状证明 V2 大半没落地：

- **CSS 自相残杀**：`front/assets/css/legacy-bridge.css:1-12` 自己写明用途——*"用 `html body` 前缀 + `!important` 压住 control_panel.html 里 6432 行内联旧 CSS 的全部视觉决策"*。即页面**先加载 6600 行旧内联 CSS，再用 legacy-bridge.css（397 个 `!important`）+ pages.css（1171 个 `!important`）强行盖掉**，全栈 `!important` 超过 **1900** 个。这是"设计上的特异性战争"，是重构半途而废的典型签名。
- **3 个 HTML，2 个是死的**：只有 `control_panel.html` 被 `GET /ui` 服务（`admin_routes.py:112-125`，无设备判断）。`control_panel.legacy.html`（18,731 行）、`control_panel_mobile.html`（3,118 行、tab 集已与桌面端漂移）从不被加载——但 commit `966a0d6` 居然还在往**永远不会被用户打开的** legacy 文件里修 bug。
- V2 计划承诺的 `command-palette.js`、`focus-mode.js` 从未出现在 `front/assets/js/`。

### 主线 3：广度优先、深度欠账，且缺乏端到端验证

功能"列表"很长（66 条路由、7 个面板 tab、播放场、性能瀑布图……），但很多是"看起来有、实际不通"或"没人验证过"：

- **暗色模式直到 ~2 天前才真正生效**：commit `5a4745c` 修复了 ~44 条 `[data-theme="dark"] html body`（这选择器**永远匹配不上**，因为 `data-theme` 在 `<html>` 上）的死规则——意味着"连接/发送/保存"按钮、日志台、trace 表在很长时间里**完全没有暗色样式**。44 条死规则能一路 ship 出去，说明重构后没人真正端到端走查过。
- README 引用了 **14 个不存在的文档**（`RATE_LIMIT_*.md`、`LOGGING_GUIDE.md`…），且 `.env.example` 与 `requirements.txt` **都不存在**——可 README 的"快速开始"让用户 `cp .env.example .env` 和 `pip install -r requirements.txt`，**新用户第一步就失败**。

---

## 三、🔴 安全问题（最严重，决定"能不能对外用"）

> 这一节是"为什么不能真正得心应手地用"的核心：当前状态下，把它暴露到任何不可信网络都等于把上游 API key 和后台拱手让人。

### 3.1 整组路由**零鉴权**，且能明文导出全部 key —— 已逐条验证

- `src/api/key_management_api.py`：**全文件 0 个 `Depends`、0 处鉴权**。其中 `GET /api/keys/export`（:311-319）调用 `key_manager.export_keys()`，后者直接 `return self._cache.keys`（`key_manager.py:505-511`）——**任何能访问该端口的人，无需密码即可拿到全部上游 AssemblyAI key 明文**。同组还有 `POST /api/keys`（加 key）、`POST /api/keys/import`（覆盖全部 key）、`DELETE /api/keys/{index}`、改聚合模式等——全部裸奔。
- `src/api/account_api.py`：同样**无真实鉴权**（文件里唯一的 `Depends` 是 import 语句，"authenticate" 只是 AssemblyAI 后台的 URL）。`/api/account/accounts`、`/billing`、`/cost`、`/api-keys`、`/switch`、`/logout` 任何人可调——泄露后台账单数据，甚至能把账号登出。
- `src/api/playground_api.py`：**0 鉴权**。`/preview`、`/custom`、`/initial-request` 让任何人用**你的** key 发任意请求，且 `/preview` 把真实 key 泄进预览出来的请求头（`playground_api.py:74`）。

> 对照：`admin_routes.py`（20 个 `Depends`）和 `openai_router.py`（7 处鉴权）**是有鉴权的**。所以这不是"还没做鉴权"，而是**新加的几个路由忘了接鉴权**——半成品式的安全模型。

### 3.2 雪上加霜：CORS 全开 + 携带凭证

`web.py:65-71`：`allow_origins=["*"]` **且** `allow_credentials=True`。这本身是无效/危险组合，更糟的是：**它让上面 3.1 的裸奔接口可以被你访问过的任意网站跨域调用**。

### 3.3 明文密码四处泄露

- `web.py:143-144`：启动时把 API 密码、面板密码**明文打进日志**——直接违反项目自己 `AGENTS.md:118`/`CLAUDE.md` 的"绝不记录密钥"规则。
- 前端把面板/API 密码**明文存进 localStorage**（`control_panel.html:9159` `localStorage.setItem('amb2api_auth_token', password)`），永久留存，配合下面的 XSS 可被直接窃取。

### 3.4 XSS 注入面

`control_panel.html` 有 138 处 `innerHTML` 赋值，仅 33 处 `escapeHtml`。明确可利用点：上传凭证的**文件名未转义直插 innerHTML**（:10195-10200），一个名为 `<img src=x onerror=...>.json` 的文件即可执行脚本，进而窃取 3.3 的明文密码。

### 3.5 安全姿态从未被认真对待的旁证

`SECURITY.md` 是**未改动的 GitHub 模板**（声称支持版本 "5.1.x"，而本项目是 0.6.1）。

---

## 四、UI / UX 问题

### 4.1 结构性（见主线 2）
- 自相残杀的 CSS（1900+ `!important`）、3 个 HTML（2 死、1 个还在被误改）、V2 重构停在半路。

### 4.2 一致性碎裂
- **两套通知系统并存**：新的 toast/消息中心做了，但 119 处调用点仍在用旧的 `showStatus()` 横幅。
- **图标迁移 ~5% 完成**：加载了 24KB 的 Lucide `icons.js`，但 markup 里 `data-icon` 只用了 4 次；功能性 emoji（☀️🌙📦📄⚙️…）仍遍布，违背 V2 计划"主面板无 emoji"的要求。
- **三个主题 localStorage 键**（`theme`、`amb2api-theme`…）+ 两套 `toggleTheme` 实现 + V2 代理按钮同时驱动同一状态——冗余且脆弱。
- 内联 `<style>` 与 `tokens.css` 定义了**同名但不同值**的变量（`--text-color`、`--bg-color`…），旧值已死仍 ship。

### 4.3 可访问性 / 响应式 / 健壮性
- 全文 19.8k 行里 `aria-*` 仅 30 处、`role=` 仅 1 处、`alt=` 0 处——对这个体量近乎为零。
- 内联 CSS 有 143 处硬编码 `px` 宽度；内联 SVG 图表写死 `min-width:400px` 会在窄屏溢出。移动端触摸目标也是最近才被动补的。
- **73 个 `fetch()` 仅 5 个 `.catch()`**，几乎无重试、超时极少——多数请求失败会静默挂起。日志 WebSocket（:10479）有 `onclose/onerror` 但**无重连**，断了就死到刷新。
- 生产代码里残留 **80 条 `console.*`** 调试输出。

### 4.4 i18n
**纯中文硬编码、无 i18n 层**（`<html lang="zh-CN">`，2257 行含中文，零 `data-i18n`）。非中文运维者无法使用；又零星混入英文（V2 eyebrow、console），更显未完成。

---

## 五、功能与能力问题

### 5.1 "看起来支持、其实没通"的功能
| 功能 | 真实状态 | 证据 |
|---|---|---|
| **视觉/多模态（图片）** | 🔴 **被静默丢弃** | `_sanitize_messages` 把 list content 压平成纯文本、**丢掉 `image_url`**（`assembly_client.py:328-333`），尽管入站路径假装支持 |
| **流式抗截断 `流式抗截断/`** | 🔴 **名存实亡** | 路由直接降级：`openai_router.py:742-745` 打印"AssemblyAI 暂不支持原生流式抗截断"，强制 `stream=False` 返回普通响应；真正的续写模块（`anti_truncation.py`）还是 Gemini 格式、未接入 |
| **`n > 1`** | ⚠️ 假支持 | 仅对非 Claude 透传，且响应转换把多 choice **合并成一个**，永远只能返回 1 条 |
| **logprobs** | ❌ | model 里有字段，但从不上送也从不回填 |
| `response_format` / json_schema | ⚠️ 仅透传 | 不做结构化校验 |

### 5.2 缺失的"标准网关"能力
- 无 `/v1/embeddings`、无 legacy `/v1/completions`、无图像生成/音频/moderations。
- **无 `/health` `/ready` `/metrics`**，只有 `HEAD /keepalive` 恒返回 200（不检查存储/上游是否可用）。

### 5.3 配置桩（文档说能配，代码写死）
- `get_compatibility_mode_enabled()` 硬编码 `return False`（`config.py:396`），无视它自己文档里的 `COMPATIBILITY_MODE` 环境变量。
- `get_anti_truncation_max_attempts()` 硬编码 `return 3`（`config.py:288`），无视文档声称的可配置。
- `prompt_cache_retention`/`prompt_cache_key` 能透传却无 getter、面板无法配置；`max_tokens_mode`/`fake_stream_*` 绕过 `config.py` getter 直接读 adapter（违反 CLAUDE.md 的"统一走 get_config_value"）。

### 5.4 账户/账单能力 = **抓取 AssemblyAI 私有后台**（最脆弱的"能力"）
`account_api.py`（3210 行，最大后端文件）本质是 AssemblyAI **网页后台的爬虫**：用邮箱密码登录 dashboard（`:518-678`），保存 Stytch 的 `session_jwt`/`session_token`/`aai_extended_session` cookie，**伪装 Chrome-142 UA**（`:405`），抓取 `text/x-component` 的 **RSC（Next.js React Server Components）**流（`:441-443`），再用一堆 `_parse_billing_rsc_data`/`_extract_usage_chart_data`/`_parse_cost_rsc_data` 启发式解析账单/用量/成本。

> 风险：这是逆向一个**未公开、随时会变**的私有 Web 应用。AssemblyAI 一改前端，所有 `_parse_*_rsc_data` 静默失效——账单/成本面板会"无声地"显示空或错。这类能力天生不可靠，是"半成品体验"的重要来源。

### 5.5 统计系统重复
**4 套统计模块**（`unified_stats.py` 731 行 + `stats_tracker.py` 350 行 + `usage_stats.py` 569 行 + `performance_tracker.py` 807 行，共 ~2460 行）。CLAUDE.md 说 `unified_stats` 是唯一真相，但迁移没做完：`stats_tracker`/`usage_stats` 仍被 admin/key API 引用，且旧的 `assembly_client._rate_limit_info` 与新的 `RateLimiter` **写同一个 `rate_limit_info` 存储键**，会互相覆盖。

---

## 六、性能与可靠性问题

> 这一节直接对应"用起来不顺手/不丝滑"的体感。

### 6.1 🔴 每请求新建 HTTP 连接，无连接池（最大单一性能损失）—— 已验证
`src/core/httpx_client.py:31-49`：`get_client`/`get_streaming_client` **每次上游调用都新建一个 `httpx.AsyncClient` 再 `aclose()`**，无共享单例、无连接池复用、无 HTTP/2、无 `limits`。热路径实证：`assembly_client.py:2027`（非流式）、`:1885`（流式）。**每个请求都重新做一次 TCP+TLS 握手**到 `llm-gateway.assemblyai.com`——这是延迟和吞吐的头号杀手，也最能解释"不丝滑"。

### 6.2 流式上游 `timeout=None`（无限等待）
`httpx_client.py:40`（流式）与 `assembly_client.py:2027`（非流式）均无读/连/写/池超时。上游半挂，请求（及假流式心跳循环）**无限期占住**。

### 6.3 配置每请求被反复读取
`config.py:73-98` 每个 getter 都 `await get_storage_adapter().get_config(key)`，**无配置层 TTL 快照**。一次非流式请求要串行 await 12+ 个配置项（`assembly_client.py:1797-1827`…），且 `CONFIG_OVERRIDE_ENV` 未设时每次还额外读一次 `override_env`。即便 Redis 命中内存缓存，也要走 `UnifiedCacheManager` 的单把 `asyncio` 锁——每请求几十次锁获取+await 跳跃，纯属浪费。

### 6.4 每请求"写入风暴"
响应路径每个请求 fire-and-forget 多个保存：`_save_rate_limit_info`（`assembly_client.py:1530`）**和** `RateLimiter._save_rate_limits`（`rate_limiter.py:111`）各写一遍整块 → 互相竞争覆盖；`unified_stats._save_stats`（`:402`，整块序列化，30s 节流）；`performance_tracker._save_trace`（`:330`）。其中 perf-trace 是**读-改-写一个最多 1000 条的分片**（`performance_tracker.py:335-364`），在 file 后端还被并进 `config.toml`——O(N)/请求。

### 6.5 并发状态无锁
`assembly_client.py:1041-1252` 的 `_failed_keys`/`_rr_counter`/`_rate_limit_info`、`key_selector.py:106-189` 的 `_call_counts` 全是进程级、无锁的"先检查后动作"，并发下会选到同一个 key、配额计数错乱；`should_rotate` 还在谓词里 `_call_counts=0` 做副作用。

### 6.6 配额竞态 + "成功"计在连接建立时
`can_use_key_for_model` 只读检查、`record_call` 稍后才加（TOCTOU，配额会超），且**流式分支在上游连上的瞬间就 `record_call(success=True)`**（`assembly_client.py:1998`）——中途报错/被切断的流也算成功，且真实 token 用量只进 perf-trace、**从不回写 unified_stats**。

### 6.7 无横向扩展能力
`web.py:148-167` 单 hypercorn 进程、无 `workers`。一旦开多 worker 或多副本：file 后端整块 TOML 延迟回写 → **后写覆盖前写**（`cache_manager.py:253-292`，无文件锁）；即使用 Redis 共享存储，6.5 的进程内计数器仍各算各的——配额/轮换/统计全部分裂。

### 6.8 日志无切割 + 每行 flush
`log.py:34-56`：追加写 `log.txt`，**每行都 `f.flush()`** 且在全局 `threading.Lock` 下；无大小上限、无轮转。INFO 级每请求多行 → 磁盘无限增长 + 阻塞写；面板的日志 WebSocket 还**反复 `open()` 整文件**（`admin_routes.py:846-854`），随日志增大越来越卡。

### 6.9 假流式全缓冲 + 任务泄漏
假流式本质先跑完整非流式（继承 6.2 的 `timeout=None`）、内存里缓冲整个响应再切块吐出（`assembly_stream_handler.py:466,516-526`）——非真流式。另有 16 处未注册到 TaskManager 的 `asyncio.create_task`，shutdown 不会取消、异常被吞，且未在 `lifespan` 调 `close_storage_adapter`，SIGTERM 时在途写入会丢。

### 6.10 部署卫生
Dockerfile 单阶段、**root 运行**、镜像内无 HEALTHCHECK、无 lockfile（不可复现）；CORS 见 3.2。

---

## 七、市场对标

### 7.1 定位与对手
属于"自托管 LLM API 网关/中转"。更精确说是**单上游（AssemblyAI）+ key 池 + 双协议面**的代理。
- **同量级直接对手**：`gpt-load`、`uni-api`、`openai-forward`（单管理员、配置驱动的 key 池）。
- **被拿来对标的"平台级"**：`new-api`/`one-api`（多用户分发 + 计费）、`LiteLLM`、`Portkey`。

### 7.2 能力对比矩阵（✅全 / ⚠️部分 / ❌无）
| 能力 | **amb2api** | new-api | LiteLLM | gpt-load | uni-api |
|---|---|---|---|---|---|
| OpenAI 兼容面 | ✅ | ✅ | ✅ | ✅ | ✅ |
| Anthropic Messages 面 | ✅ | ✅ | ✅ | ✅(透传) | ✅(后端) |
| 多上游供应商 | ❌(仅 AssemblyAI) | ✅(40+) | ✅(100+) | ✅ | ✅(15+) |
| key 轮换/池化 | ✅ | ✅ | ✅ | ✅(强项) | ✅ |
| **每用户账号/多租户** | ❌(单口令) | ✅ | ✅ | ❌ | ⚠️ |
| **每用户 API token** | ❌ | ✅ | ✅(虚拟 key) | ❌ | ✅ |
| **配额** | ⚠️(按 key 日配额) | ✅(按 token 额度) | ✅(按 key/user/team) | ❌ | ✅ |
| **货币($)成本核算** | ❌ | ✅ | ✅ | ❌ | ⚠️ |
| 充值/计费(Stripe等) | ❌ | ✅ | ⚠️ | ❌ | ⚠️ |
| **通道健康自检+自动禁用** | ❌ | ✅ | ✅ | ✅ | ⚠️ |
| 模型别名映射 | ⚠️(仅前缀) | ✅ | ✅ | ⚠️ | ✅ |
| **可搜索请求日志浏览器** | ⚠️ | ✅ | ✅ | ✅ | ❌ |
| OAuth/注册/SSO | ❌ | ✅ | ✅ | ❌ | ❌ |
| 假流式/keepalive | ✅(差异化) | ⚠️ | ❌ | ❌ | ❌ |
| 抗截断续写 | ⚠️(宣传有/实际降级) | ❌ | ❌ | ❌ | ❌ |
| 响应缓存(精确/语义) | ❌ | ⚠️ | ✅ | ⚠️ | ❌ |
| 可插拔存储后端 | ✅(Redis/PG/Mongo/file) | ⚠️ | ⚠️ | ⚠️ | ⚠️ |
| Web 管理面板 | ✅ | ✅(成熟) | ✅ | ✅ | ❌ |

### 7.3 对手有、而 amb2api 没有（按"产品感"权重排序）
1. **每用户 token + 每 token 配额体系**（头号缺口）：当前只有一个共享口令，**无法把访问"分发"给多人/客户**，只能"代理自己"。这是"自用网关"与"可运营产品"的分界线。
2. **货币成本核算 + 充值/计费**：只统计 token 与按 key 日配额，不折算成钱、无充值。
3. **多上游**：硬绑 AssemblyAI；AssemblyAI 一变就无退路（设计取舍，但也是天花板）。
4. **通道/key 健康自检 + 自动禁用**：现在只对 429/400 被动轮换，不主动探活。性价比最高的运维补强。
5. **可搜索的请求日志浏览器**（把每条请求关联到 user/token/model/cost）。
6. **完整模型别名映射** / OAuth 注册 / 响应缓存 / 外部可观测性回调（Langfuse 等）。

### 7.4 amb2api 真正的差异化优势（要守住的）
1. **一等公民的双 OpenAI + Anthropic 面**，且共享同一条上游管线、契约冻结（`docs/anthropic_compat_contract.md`）——领先所有轻量对手。
2. **假流式/keepalive**：心跳防中间层空闲超时，轻量对手都没有。
3. **AssemblyAI Gateway 专用适配**这一小众但可防守的生态位。
4. **最广的可插拔存储**（Redis→PG→Mongo→file 自动选择）。
5. Gemini thinking 的 `reasoning_content`/`thoughtSignature` 往返保真。

> 注意：抗截断本是潜在差异化点，但当前是"宣传有、代码降级为普通请求"（见 5.1）——要么补成真功能、要么别再宣传。

---

## 八、修复优先级路线图

### P0 — 安全 + 能用（不做就不能对外）
1. 给 `key_management_api.py` / `account_api.py` / `playground_api.py` **全部接上鉴权**（复用 `admin_routes` 的 `Depends`）；下掉/鉴权化 `GET /api/keys/export`。
2. CORS 收敛：用显式 origin 白名单，去掉 `*`+credentials 组合（`web.py:65-71`）。
3. **停止把密码写进日志**（`web.py:143-144`）；前端别再明文存 localStorage（`control_panel.html:9159`）。
4. **HTTP 客户端改共享单例 + 连接池 + HTTP/2**（`httpx_client.py`）；给上游设合理超时（连/读/写/总）。
5. 加真正的 `GET /health`（探存储+至少一个可用 key），把 Docker healthcheck 从 `/v1/models` 切过去。
6. 修 XSS：统一 `escapeHtml`（尤其文件名 `:10195`）。

### P1 — 顺手 + 正确（解决"不丝滑")
7. **config 加短 TTL 快照缓存**，消除每请求 12+ 次存储读。
8. 合并每请求写入（统一一处 rate-limit 保存、用量异步批量落盘）；perf-trace 改增量/Redis list。
9. **多模态修复或显式报错**：要么真正透传图片，要么对含图片请求返回清晰 400，别静默丢。
10. 修配额竞态 + 把"成功"计在流真正完成时、回写真实 token 用量。
11. 日志切割（按大小/时间轮转）、去掉每行 flush、清掉 80 条 console.*。
12. 补完暗色/移动端/可访问性遗留；统一到一套通知系统。
13. **删死代码**：`proxy_manager.py`、`anthropic_transfer.py`（或反过来定为唯一实现）、`openai_transfer.py` 的 Gemini 函数、`anti_truncation.py`、Gemini 配额字段——一次性清掉 fork 残留。

### P2 — 产品化（决定方向后再投入）
14. **先回答一个问题：amb2api 是"个人/单组织的韧性代理"，还是"对外分发平台"？**
    - 若**分发**：补 per-user token + 配额 + 货币成本核算（直接对标 new-api 在 AssemblyAI 这个上游的位置）。
    - 若**个人韧性代理**：砍掉计费幻想，把抗截断做成真功能、双面兼容打磨到极致，对标 gpt-load/uni-api 并以差异化取胜。
15. UI：要么按 V2 计划**收尾**（删旧内联 CSS、真正迁移而非 `!important` 压制、删 2 个死 HTML），要么**回滚**到单一稳定版本——别停在"两套并存"。
16. 通道健康自检 + 自动禁用（性价比最高的运维补强）。
17. 重写 README/文档：删掉 14 个不存在的引用、补 `.env.example`、修正安装命令。

---

## 九、附录：做得对的地方（可信度校准）

为避免只列问题而失真，以下是确有水准、应保留的部分：
- 双协议转换层认真：tool calling、stop、prompt-cache 透传 + **缓存亲和度**（同前缀优先同 key）、thought-signature 往返，且有 27 个测试文件（含 hypothesis 属性测试）护住契约。
- Anthropic 错误壳映射基本一致（`authentication_error`/`rate_limit_error`/`invalid_request_error`）。
- 凭证文件读写用了 `aiofiles`（非阻塞）；预加载队列有信号量上限、不在启动时打爆上游；perf-trace 是环形缓冲（容量有界）；stats 30s 节流。
- 工程方法有章法：CSV 任务追踪、契约冻结文档、分阶段计划——基础是好的，问题在"执行没收尾"。

---

### 关键文件索引（便于跟进）
- 安全：`src/api/key_management_api.py`、`src/api/account_api.py`、`src/api/playground_api.py`、`web.py`
- 性能：`src/core/httpx_client.py`、`config.py`、`src/services/assembly_client.py`、`src/stats/performance_tracker.py`、`log.py`
- 功能：`src/services/assembly_client.py`（多模态）、`src/api/openai_router.py`（流式）
- UI：`front/control_panel.html`、`front/assets/css/legacy-bridge.css:1-12`、`plan/UI_REFACTOR_V2_PLAN.md`

---

## 十、实施进展（本轮优化已落地）

> 以下为本轮"从里到外换新"已完成并合入 `claude/trusting-davinci-ajESQ` 的改动；全程绿测（233 passed）。

### ✅ 已完成

| # | 项 | 关键改动 | 对应问题 |
|---|----|---------|---------|
| 1 | **整组路由鉴权** | 抽出 `src/api/auth.py`，给 keys/account/playground 三组路由加 `Depends(authenticate)`；新增回归测试 | 三 3.1 🔴 |
| 2 | **CORS 收敛** | 禁止 `*`+credentials 组合；`CORS_ALLOW_ORIGINS` 白名单，默认不带凭证 | 三 3.2 |
| 3 | **停止明文口令落日志** | 移除启动期口令打印，仅在用默认弱口令时告警 | 三 3.3 |
| 4 | **健康检查** | 新增 `GET /health`（探存储），Docker healthcheck 切到 /health | 五 5.2 |
| 5 | **XSS** | 上传文件名经 `escapeHtml` 再插入 innerHTML | 三 3.4 |
| 6 | **共享 HTTP 连接池** | `httpx_client.py` 改进程级共享客户端 + 连接池 + HTTP/2 + **有界超时**（不再 `timeout=None`）；流式只关流不关客户端；优雅关闭 | 六 6.1 / 6.2 |
| 7 | **配置快照缓存** | `config.py` 加短 TTL 缓存 + 写穿失效，消除每请求 12+ 次存储读 | 六 6.3 |
| 8 | **日志轮转** | 按大小轮转（`LOG_MAX_BYTES`/`LOG_BACKUP_COUNT`）+ 持久句柄，告别无限增长 | 六 6.8 |
| 9 | **多模态修复** | `_sanitize_messages` 透传 `image_url`，不再静默丢图 | 五 5.1 |
| 10 | **删 fork 死代码** | 删 `proxy_manager.py`、`anti_truncation.py`、`openai_transfer.py` 的 Gemini 死函数、config 的 Gemini 辅助；移除"名存实亡"的 `流式抗截断/` 特性 | 二·主线1、五 5.1/5.3 |
| 11 | **删 Gemini 配额模型** | 删 `usage_stats.py`(569行) + `state_manager.py` + 死工具；admin `/rate-limits`、`/usage/aggregated` 去掉 `gemini_2_5_pro` 特例字段 | 二·主线1、五 5.5 |
| 12 | **修复上手文档** | 补 `.env.example`；修 README 安装/测试命令（无 requirements.txt）；删 14 个失效文档链接；修 `config.py` 残留 "Geminicli2api" docstring | 二·主线3、八 P2#17 |

累计净删除 ~1900 行 fork 死代码；新增 4 个回归测试文件。

### ⏳ 有意延后（高风险或需产品决策，单独立项更稳妥）

- **统计/配额子系统的剩余整合**：旧 `assembly_client._rate_limit_info` 与 `RateLimiter` 双写同一存储键（6.4）、每请求写风暴合并（6.4）、配额 TOCTOU 与"连接建立即计成功"（6.6）。这些触及配额正确性与 key 选择，需配套测试单独推进。
- **存储层 usage-stats 死方法**：`update/get/get_all_usage_stats` 已确认零调用，但分散在 4 个后端 + Protocol；其中 `STATE_FIELDS` 仍服务于**活的**凭证状态，混杂着 `gemini_2_5_pro_calls` 等惰性残键。属低风险但跨后端面广，宜单独清理。
- **前端 V2 收尾或回滚**：CSS `!important` 战争、3 个 HTML（2 死）、localStorage 明文口令、暗色/移动端/可访问性、统一通知系统——19.8k 行单体，建议作为独立的前端专项（收尾 V2 或回滚到单一稳定版）。
- **多上游 / 横向扩展**：进程内计数器（key 选择/限流/统计）在多 worker 下会分裂（6.7），需迁移到共享存储后再开多副本。
- **产品方向决策（P2 #14）**：是否做 per-user token + 配额 + 货币成本核算——决定 amb2api 是"个人韧性代理"还是"对外分发平台"。需你拍板后再投入。

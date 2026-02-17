# Anthropic/Claude 全兼容接入实施计划（amb2api）

## 简要总结
目标是在保持现有 OpenAI 路径稳定的前提下，新增 Anthropic 协议入口，让 Claude/Anthropic 客户端可直接接入同一个 `base_url` 与同一套 API 密码，并由网关自动完成双向协议转换（请求转 OpenAI、响应再转 Anthropic）。

🎯 任务：为 `amb2api` 增加 Anthropic 原生兼容入口与转换层，支持 Claude Code/Anthropic SDK 直接接入。

📋 执行计划：
1. Phase 1: 协议范围冻结与兼容基线  
明确首版支持接口为 `POST /v1/messages`、`POST /v1/messages/count_tokens`、`GET /v1/models`（Anthropic 头触发 Anthropic 格式），并列出与当前 OpenAI 路由共用的处理链路。
2. Phase 2: 新增转换模块 `src/transform/anthropic_transfer.py`  
实现 Anthropic 请求到 OpenAI 请求的字段映射，覆盖 `system`、`messages`、`tools`、`tool_choice`、`stop_sequences`、`stream`。
3. Phase 3: 响应与流式转换实现  
实现 OpenAI 非流式响应转 Anthropic `message`；实现 OpenAI SSE chunk 转 Anthropic 事件流（`message_start/content_block_delta/message_stop`）。
4. Phase 4: 路由接入与鉴权兼容  
在 `src/api/openai_router.py` 新增 `POST /v1/messages` 与 `POST /v1/messages/count_tokens`，新增兼容鉴权逻辑（优先 `x-api-key`，兼容 `Authorization: Bearer`）。
5. Phase 5: 模型列表双格式输出  
增强 `GET /v1/models`：默认返回 OpenAI 格式；带 `anthropic-version` 时返回 Anthropic 风格模型结构，避免影响既有 OpenAI 客户端。
6. Phase 6: 错误与状态码统一映射  
将现有 OpenAI 错误包映射为 Anthropic 错误结构，保持原始 HTTP 状态码，保证 SDK 重试策略可用。
7. Phase 7: 测试矩阵补齐  
新增协议转换单测与路由集成测试，覆盖非流式、流式、工具调用、鉴权失败、参数非法、count_tokens、models 双格式。
8. Phase 8: 文档与上线准备  
更新 `README.md` 与启动日志说明，提供 Anthropic/Claude 接入示例；执行最小回归并形成上线检查清单。

🧠 当前思考摘要：
- 当前仓库仅有 OpenAI 入口，缺少 `x-api-key`、`/v1/messages`、`anthropic-version` 处理，必须新增协议层而非仅改文档。
- 现有请求处理链（限流、key 轮换、真实/假流式、统计）已成熟，最佳策略是“外层协议适配 + 内核复用”。
- 风险集中在工具调用与流式事件语义，不在主请求转发本身。
- 为避免回归，应保持 OpenAI 路径默认行为不变，仅按请求特征触发 Anthropic 输出。

## 重要接口与类型变更
1. 新增接口 `POST /v1/messages`  
输入 Anthropic messages 格式，内部映射到 `ChatCompletionRequest` 后复用现有链路。
2. 新增接口 `POST /v1/messages/count_tokens`  
返回 `{"input_tokens": int}`，用于 Anthropic 客户端能力探测与预算估算。
3. 增强接口 `GET /v1/models`  
按请求头区分输出格式：默认 OpenAI；Anthropic 头触发 Anthropic。
4. 新增转换文件 `src/transform/anthropic_transfer.py`  
包含请求映射、响应映射、SSE 映射、错误映射、token 估算函数。

## 测试场景与验收标准
1. `POST /v1/messages` 非流式成功返回 Anthropic message 结构。
2. `POST /v1/messages` 流式返回合法 Anthropic 事件序列，包含终止事件。
3. `tool_use -> tool_result` 多轮回合正确映射，不丢 `tool_use.id`。
4. `x-api-key` 与 `Authorization: Bearer` 均可认证；失败时返回 Anthropic 错误壳。
5. `POST /v1/messages/count_tokens` 返回结构稳定、数值类型正确。
6. `GET /v1/models` 默认 OpenAI 不变；Anthropic 请求头下返回 Anthropic 格式。
7. 现有 OpenAI 回归通过，至少覆盖 `tests/test_daily_usage_limits.py::test_openai_router_passthroughs_error_status_from_gateway` 与流式核心测试。

⚠️ 风险与阻塞：
- Anthropic 与 OpenAI 在流式事件粒度上不完全等价，若映射不严格会导致 Claude 客户端异常。
- `count_tokens` 首版为近似估算，可能与官方 tokenizer 存在偏差。
- 若后续 Anthropic 新字段快速演进，需要持续补映射逻辑。
- 双格式 `GET /v1/models` 的头部判定需谨慎，避免误伤现有 OpenAI 调用方。

## 假设与默认值
- 默认优先满足 Claude Code/Anthropic SDK 主路径，不在首版扩展其他历史 Anthropic 旧接口。
- 不新增控制面板开关，先以内建兼容策略上线。
- 现有 `API_PASSWORD` 作为 Anthropic 接口鉴权凭据。
- 保持当前 OpenAI 路由契约和行为不变。

📎 Plan 文件：
- 路径：`plan/2026-02-17_18-04-23-anthropic-compat-plan.md`
- 状态：无法创建（当前处于 Plan 模式，受“仅允许非变更操作”约束，不能写入仓库文件）；已提供完整可落地计划文本。

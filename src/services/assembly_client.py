import json
import asyncio
from typing import Dict, Any, Optional, List, Set
import itertools
import uuid

from fastapi.responses import StreamingResponse, JSONResponse

from log import log
from ..models.models import ChatCompletionRequest
from ..core.httpx_client import http_client, create_streaming_client_with_kwargs
# 统计功能已迁移到 unified_stats 模块
from ..storage.storage_adapter import get_storage_adapter
from .rate_limiter import get_rate_limiter
from .key_selector import get_key_selector
from config import (
    get_assembly_endpoint,
    get_assembly_api_keys,
    get_retry_429_enabled,
    get_retry_429_max_retries,
    get_retry_429_interval,
    get_auto_ban_enabled,
    get_auto_ban_error_codes,
    get_tool_debug_logs_enabled,
)


# 缓存密钥到账户的映射，避免每次请求都查询
_key_account_cache: Dict[str, str] = {}
_key_account_cache_time: float = 0
_KEY_ACCOUNT_CACHE_TTL: float = 300.0  # 5分钟缓存


async def _find_account_for_key(api_key: str) -> str:
    """
    查找 API 密钥属于哪个登录账户
    
    通过扫描所有已登录账户的 session 数据和 API keys 缓存，
    找到拥有该密钥的账户邮箱。
    
    Args:
        api_key: API 密钥
        
    Returns:
        账户邮箱，如果找不到则返回空字符串
    """
    import time
    global _key_account_cache, _key_account_cache_time
    
    current_time = time.time()
    
    # 检查缓存是否有效
    if api_key in _key_account_cache and (current_time - _key_account_cache_time) < _KEY_ACCOUNT_CACHE_TTL:
        return _key_account_cache[api_key]
    
    try:
        adapter = await get_storage_adapter()
        
        # 获取所有配置，查找 session 数据
        all_config = await adapter.get_all_config()
        
        # 遍历所有 assembly_dashboard_session:xxx 键
        SESSION_KEY_PREFIX = "assembly_dashboard_session:"
        for key, value in all_config.items():
            if not key.startswith(SESSION_KEY_PREFIX):
                continue
            
            if not isinstance(value, dict):
                continue
            
            account_email = value.get("email", "")
            if not account_email:
                continue
            
            # 检查 session 中的 api_token
            user_info = value.get("user_info", {})
            session_api_token = user_info.get("api_token") or value.get("api_token")
            if session_api_token and session_api_token == api_key:
                _key_account_cache[api_key] = account_email
                _key_account_cache_time = current_time
                log.debug(f"Found key owner from session: {account_email}")
                return account_email
        
        # 还可以检查已缓存的 API keys 数据
        for key, value in all_config.items():
            if not key.startswith("api_keys:"):
                continue
            
            if not isinstance(value, dict):
                continue
            
            api_keys_list = value.get("api_keys", [])
            for key_info in api_keys_list:
                if isinstance(key_info, dict) and key_info.get("api_key") == api_key:
                    # 从缓存键中提取账户邮箱
                    account_email = key.replace("api_keys:", "")
                    _key_account_cache[api_key] = account_email
                    _key_account_cache_time = current_time
                    log.debug(f"Found key owner from api_keys cache: {account_email}")
                    return account_email
        
        log.debug(f"Could not find account for key {api_key[:8]}...")
        return ""
    except Exception as e:
        log.warning(f"Failed to find account for key: {e}")
        return ""


def _sanitize_messages(messages) -> list:
    """
    清理和标准化消息格式，转换为 AssemblyAI LLM Gateway 格式
    
    关键转换：
    1. OpenAI 的 role="assistant" + tool_calls -> AssemblyAI 的 type="function_call"
    2. OpenAI 的 role="tool" -> AssemblyAI 的 type="function_call_output"
    3. 普通消息保持 role 格式（user、assistant、system）
    
    参考文档：https://www.assemblyai.com/docs/llm-gateway/tool-calling
    """
    sanitized = []
    
    # 用于存储 tool_call_id 到 function name 的映射
    tool_call_id_to_name = {}

    def _normalize_tool_input(tc_dict: Dict[str, Any], func_info: Dict[str, Any]) -> Dict[str, Any]:
        """
        统一解析 tool 调用入参，保证返回 dict。

        兼容来源：
        - function.arguments (OpenAI 标准)
        - function.input / tool_call.input (部分兼容客户端)
        """
        raw_args: Any = func_info.get("arguments")
        if raw_args is None and "input" in func_info:
            raw_args = func_info.get("input")
        if raw_args is None and "input" in tc_dict:
            raw_args = tc_dict.get("input")

        if isinstance(raw_args, dict):
            return raw_args
        if isinstance(raw_args, str):
            text = raw_args.strip()
            if not text:
                return {}
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
                return {"value": parsed}
            except Exception:
                return {"raw": text}
        if raw_args is None:
            return {}
        return {"value": raw_args}
    
    for m in messages:
        role = getattr(m, "role", None) or (m.get("role") if isinstance(m, dict) else "user")
        content = getattr(m, "content", None) if hasattr(m, "content") else (m.get("content") if isinstance(m, dict) else None)
        
        # 处理多模态内容
        if isinstance(content, list):
            parts_text = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                    parts_text.append(part["text"])
            content = "\n".join(parts_text) if parts_text else ""
        
        # 确保 content 是字符串
        if content is None:
            content = ""
        
        # 获取 tool_calls（如果存在）
        tool_calls = getattr(m, "tool_calls", None) if hasattr(m, "tool_calls") else (m.get("tool_calls") if isinstance(m, dict) else None)
        
        # 获取 tool_call_id（对于 tool 角色的消息）
        tool_call_id = getattr(m, "tool_call_id", None) if hasattr(m, "tool_call_id") else (m.get("tool_call_id") if isinstance(m, dict) else None)
        
        # 情况1: assistant 消息带有 tool_calls -> 转换为 function_call 格式
        if role == "assistant" and tool_calls:
            # 先添加 assistant 的文本内容（如果有）
            if content and content.strip():
                sanitized.append({"role": "assistant", "content": content})
            
            # 将每个 tool_call 转换为 function_call 格式
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    # 处理不同的 tool_call 格式
                    if hasattr(tc, "model_dump"):
                        tc_dict = tc.model_dump()
                    elif hasattr(tc, "dict"):
                        tc_dict = tc.dict()
                    elif isinstance(tc, dict):
                        tc_dict = tc
                    else:
                        continue
                    
                    tc_id = tc_dict.get("id", "")
                    if not tc_id:
                        tc_id = f"call_{uuid.uuid4().hex[:24]}"
                    func_info = tc_dict.get("function", {})
                    func_name = func_info.get("name", "")
                    if not isinstance(func_name, str) or not func_name:
                        func_name = "unknown_function"
                    normalized_input = _normalize_tool_input(tc_dict, func_info)
                    func_args = json.dumps(normalized_input, ensure_ascii=False)
                    
                    # 记录映射，用于后续处理 tool 消息
                    if tc_id:
                        tool_call_id_to_name[tc_id] = func_name
                    
                    # 创建 AssemblyAI function_call 格式
                    tool_use_payload = {
                        "id": tc_id,
                        "name": func_name,
                        "input": normalized_input,
                    }
                    function_call_msg = {
                        "type": "function_call",
                        "tool_call_id": tc_id,
                        "name": func_name,
                        # 保持旧字段兼容
                        "arguments": func_args,
                        # 显式提供结构化输入，避免上游转换丢失 tool_use.input
                        "input": normalized_input,
                        # 兼容需要从 messages[*].content[*].tool_use.input 校验的上游
                        "content": [
                            {
                                "type": "tool_use",
                                "id": tc_id,
                                "name": func_name,
                                "input": normalized_input,
                                # 兼容部分上游 schema: content[*].tool_use.input
                                "tool_use": tool_use_payload,
                            }
                        ],
                    }
                    sanitized.append(function_call_msg)
                    log.debug(f"Converted tool_call to function_call: {func_name} (id={tc_id})")
        
        # 情况2: tool 消息 -> 转换为 function_call_output 格式
        elif role == "tool" and tool_call_id:
            # 获取对应的 function name
            func_name = tool_call_id_to_name.get(tool_call_id, "")
            
            # 如果没有找到映射的 name，尝试从消息本身获取
            if not func_name:
                func_name = getattr(m, "name", None) if hasattr(m, "name") else (m.get("name") if isinstance(m, dict) else "")
            
            # 创建 AssemblyAI function_call_output 格式
            output_text = content if isinstance(content, str) and content else "{}"
            tool_result_payload = {
                "tool_use_id": tool_call_id,
                "content": output_text,
            }
            function_output_msg = {
                "type": "function_call_output",
                "tool_call_id": tool_call_id,
                "name": func_name or "unknown_function",
                "output": output_text,
                # 与 tool_use content 对齐，兼容上游对 content 块的校验
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call_id,
                        "content": output_text,
                        # 兼容部分上游 schema: content[*].tool_result.*
                        "tool_result": tool_result_payload,
                    }
                ],
            }
            sanitized.append(function_output_msg)
            log.debug(f"Converted tool message to function_call_output: {func_name} (id={tool_call_id})")
        
        # 情况3: 普通消息（user、assistant、system）
        else:
            message = {"role": role, "content": content}
            sanitized.append(message)
    
    return sanitized


def _extract_response_error_message(resp: Any) -> str:
    """Best-effort extract upstream error message from response body."""
    body: Any = None
    try:
        body = resp.json() if hasattr(resp, "json") else None
    except Exception:
        body = None

    if not isinstance(body, dict):
        text = ""
        try:
            text = resp.text if hasattr(resp, "text") else ""
        except Exception:
            text = ""
        if text:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    body = parsed
            except Exception:
                return text

    if isinstance(body, dict):
        if isinstance(body.get("error"), dict):
            msg = body["error"].get("message")
            if isinstance(msg, str):
                return msg
        msg = body.get("message")
        if isinstance(msg, str):
            return msg
    return ""


def _is_tool_use_input_validation_error(resp: Any) -> bool:
    """Detect known Anthropic validation error for missing tool_use.input."""
    if getattr(resp, "status_code", 0) != 400:
        return False
    msg = _extract_response_error_message(resp).lower()
    return "tool_use.input" in msg and "field required" in msg


def _is_assistant_prefill_error(resp: Any) -> bool:
    """Detect Anthropic prefill restriction error."""
    if getattr(resp, "status_code", 0) != 400:
        return False
    msg = _extract_response_error_message(resp).lower()
    return "assistant message prefill" in msg and "must end with a user message" in msg


def _build_claude_tool_fallback_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Convert function_call/function_call_output messages to role/content blocks.

    Some upstream validators require path:
    messages[*].content[*].tool_use.input
    """
    converted: List[Dict[str, Any]] = []

    def _append_block_to_role_message(role: str, block: Dict[str, Any]) -> None:
        """Append tool block to last same-role message, creating message when needed."""
        if converted and isinstance(converted[-1], dict) and converted[-1].get("role") == role:
            last = converted[-1]
            last_content = last.get("content")
            if isinstance(last_content, str):
                text = last_content
                content_list: List[Dict[str, Any]] = []
                if text:
                    content_list.append({"type": "text", "text": text})
                content_list.append(block)
                last["content"] = content_list
                return
            if isinstance(last_content, list):
                last_content.append(block)
                last["content"] = last_content
                return
            last["content"] = [block]
            return
        converted.append({"role": role, "content": [block]})
    for msg in messages:
        if not isinstance(msg, dict):
            continue

        msg_type = msg.get("type")
        if msg_type == "function_call":
            tool_call_id = msg.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                tool_call_id = f"call_{uuid.uuid4().hex[:24]}"

            tool_name = msg.get("name")
            if not isinstance(tool_name, str) or not tool_name:
                tool_name = "unknown_function"

            tool_input = msg.get("input")
            if not isinstance(tool_input, dict):
                tool_input = {}

            tool_use_payload = {
                "id": tool_call_id,
                "name": tool_name,
                "input": tool_input,
            }
            _append_block_to_role_message(
                "assistant",
                {
                    "type": "tool_use",
                    "id": tool_call_id,
                    "name": tool_name,
                    "input": tool_input,
                    "tool_use": tool_use_payload,
                },
            )
            continue

        if msg_type == "function_call_output":
            tool_call_id = msg.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                tool_call_id = f"call_{uuid.uuid4().hex[:24]}"

            output_text = msg.get("output")
            if isinstance(output_text, str):
                normalized_output = output_text
            elif output_text is None:
                normalized_output = "{}"
            else:
                normalized_output = json.dumps(output_text, ensure_ascii=False)

            tool_result_payload = {
                "tool_use_id": tool_call_id,
                "content": normalized_output,
            }
            _append_block_to_role_message(
                "user",
                {
                    "type": "tool_result",
                    "tool_use_id": tool_call_id,
                    "content": normalized_output,
                    "tool_result": tool_result_payload,
                },
            )
            continue

        converted.append(msg)

    return converted


def _messages_end_with_user(messages: List[Dict[str, Any]]) -> bool:
    """Return True when last message role is user."""
    if not messages:
        return False
    last = messages[-1]
    return isinstance(last, dict) and last.get("role") == "user"


def _normalize_tool_input_value(raw_input: Any) -> Dict[str, Any]:
    """Normalize arbitrary tool input to dict."""
    if isinstance(raw_input, dict):
        return raw_input
    if raw_input is None:
        return {}
    if isinstance(raw_input, str):
        text = raw_input.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
            return {"value": parsed}
        except Exception:
            return {"raw": raw_input}
    return {"value": raw_input}


def _ensure_tool_block_required_fields(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Ensure Anthropic-style tool blocks always carry required fields.

    Critical path:
    - messages[*].content[*].tool_use.input
    """
    fixed_messages: List[Dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue

        fixed_msg = dict(msg)
        content = fixed_msg.get("content")
        if not isinstance(content, list):
            fixed_messages.append(fixed_msg)
            continue

        fixed_blocks: List[Any] = []
        for block in content:
            if not isinstance(block, dict):
                fixed_blocks.append(block)
                continue

            fixed_block = dict(block)
            block_type = fixed_block.get("type")

            if block_type == "tool_use" or "tool_use" in fixed_block:
                nested_tool_use = fixed_block.get("tool_use")
                if not isinstance(nested_tool_use, dict):
                    nested_tool_use = {}

                tool_id = fixed_block.get("id") or nested_tool_use.get("id") or f"call_{uuid.uuid4().hex[:24]}"
                tool_name = fixed_block.get("name") or nested_tool_use.get("name") or "unknown_function"
                tool_input = _normalize_tool_input_value(
                    fixed_block.get("input") if "input" in fixed_block else nested_tool_use.get("input")
                )

                fixed_block["id"] = tool_id
                fixed_block["name"] = tool_name
                fixed_block["input"] = tool_input
                fixed_block["tool_use"] = {
                    "id": tool_id,
                    "name": tool_name,
                    "input": tool_input,
                }

            elif block_type == "tool_result" or "tool_result" in fixed_block:
                nested_tool_result = fixed_block.get("tool_result")
                if not isinstance(nested_tool_result, dict):
                    nested_tool_result = {}

                tool_use_id = (
                    fixed_block.get("tool_use_id")
                    or nested_tool_result.get("tool_use_id")
                    or f"call_{uuid.uuid4().hex[:24]}"
                )
                tool_content = fixed_block.get("content")
                if tool_content is None:
                    tool_content = nested_tool_result.get("content", "{}")
                if not isinstance(tool_content, str):
                    tool_content = json.dumps(tool_content, ensure_ascii=False)

                fixed_block["tool_use_id"] = tool_use_id
                fixed_block["content"] = tool_content
                fixed_block["tool_result"] = {
                    "tool_use_id": tool_use_id,
                    "content": tool_content,
                }

            fixed_blocks.append(fixed_block)

        fixed_msg["content"] = fixed_blocks
        fixed_messages.append(fixed_msg)

    return fixed_messages


def _summarize_message_shapes(messages: List[Dict[str, Any]]) -> str:
    """Return compact, non-sensitive shape summary for debugging message schema issues."""
    parts: List[str] = []
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            parts.append(f"{idx}:<non-dict>")
            continue

        role = msg.get("role", "-")
        msg_type = msg.get("type", "-")
        content = msg.get("content")
        if isinstance(content, list) and content:
            b0 = content[0] if isinstance(content[0], dict) else {}
            b0_type = b0.get("type", "-") if isinstance(b0, dict) else "-"
            has_tool_use = isinstance(b0.get("tool_use"), dict) if isinstance(b0, dict) else False
            has_tool_use_input = (
                isinstance(b0.get("tool_use"), dict)
                and "input" in b0.get("tool_use", {})
            ) if isinstance(b0, dict) else False
            top_input_type = type(b0.get("input")).__name__ if isinstance(b0, dict) and "input" in b0 else "-"
            parts.append(
                f"{idx}:role={role},type={msg_type},content=list,b0.type={b0_type},"
                f"b0.tool_use={has_tool_use},b0.tool_use.input={has_tool_use_input},b0.input={top_input_type}"
            )
        else:
            ctype = type(content).__name__ if content is not None else "None"
            parts.append(f"{idx}:role={role},type={msg_type},content={ctype}")
    return " | ".join(parts)


_rr_counter = itertools.count()
_failed_keys = {}  # 记录失败的 Key 和失败时间
_rate_limit_info = {}  # 记录每个 Key 的速率限制信息
_rate_limit_loaded = False  # 标记是否已加载速率限制信息
_load_lock = asyncio.Lock()  # 加载锁，防止并发加载

async def _load_rate_limit_info():
    """从存储中加载速率限制信息"""
    global _rate_limit_info, _rate_limit_loaded
    
    # 使用锁防止并发加载
    async with _load_lock:
        if _rate_limit_loaded:
            return
        
        try:
            adapter = await get_storage_adapter()
            data = await adapter.get_config("rate_limit_info")
            if data and isinstance(data, dict):
                # 转换字符串key为整数key
                _rate_limit_info = {}
                for k, v in data.items():
                    try:
                        idx = int(k)
                        _rate_limit_info[idx] = v
                    except (ValueError, TypeError):
                        log.warning(f"Invalid rate limit key: {k}, skipping")
                log.info(f"Loaded rate limit info for {len(_rate_limit_info)} keys from storage")
            else:
                log.info("No existing rate limit info found in storage")
            _rate_limit_loaded = True
        except Exception as e:
            log.error(f"Failed to load rate limit info from storage: {e}")
            # 即使失败也标记为已加载，使用内存数据继续运行
            _rate_limit_loaded = True

async def _save_rate_limit_info():
    """保存速率限制信息到存储"""
    try:
        adapter = await get_storage_adapter()
        # 转换整数key为字符串key以便JSON序列化
        data_to_save = {str(k): v for k, v in _rate_limit_info.items()}
        await adapter.set_config("rate_limit_info", data_to_save)
        log.debug(f"Saved rate limit info for {len(_rate_limit_info)} keys")
    except Exception as e:
        log.error(f"Failed to save rate limit info: {e}")

def _next_key_index(n: int) -> int:
    """
    智能 Key 选择：
    1. 优先选择未失败的 Key
    2. 如果所有 Key 都失败过，选择失败时间最早的
    3. 失败记录会在 60 秒后自动清除
    """
    import time
    current_time = time.time()
    
    # 清理过期的失败记录（60秒后清除）
    expired_keys = [k for k, t in _failed_keys.items() if current_time - t > 60]
    for k in expired_keys:
        del _failed_keys[k]
    
    # 如果没有失败记录，使用 Round-Robin
    if not _failed_keys:
        i = next(_rr_counter)
        return i % n
    
    # 找到未失败的 Key
    available_indices = [i for i in range(n) if i not in _failed_keys]
    if available_indices:
        # 从可用的 Key 中轮询
        i = next(_rr_counter)
        return available_indices[i % len(available_indices)]
    
    # 所有 Key 都失败过，选择失败时间最早的
    oldest_idx = min(_failed_keys.keys(), key=lambda k: _failed_keys[k])
    return oldest_idx


async def _next_key_index_async(n: int, excluded_indices: Optional[Set[int]] = None) -> int:
    """
    异步智能 Key 选择（集成速率限制和轮换策略）：
    1. 使用 KeySelector 进行智能选择
    2. 考虑速率限制状态
    3. 考虑轮换策略
    4. 考虑密钥禁用状态
    5. 失败记录会在 60 秒后自动清除

    Args:
        n: key 总数
        excluded_indices: 本次选择需要临时排除的 key 索引集合
    """
    import time
    from ..models.models_key import KeyInfo, KeyStatus
    from .key_manager import get_key_manager

    excluded = excluded_indices or set()
    
    current_time = time.time()
    
    # 清理过期的失败记录（60秒后清除）
    expired_keys = [k for k, t in _failed_keys.items() if current_time - t > 60]
    for k in expired_keys:
        del _failed_keys[k]
    
    # 获取速率限制管理器、密钥选择器和密钥管理器
    try:
        rate_limiter = await get_rate_limiter()
        key_selector = get_key_selector()
        key_manager = await get_key_manager()
    except Exception as e:
        log.warning(f"Failed to get rate limiter, key selector or key manager, falling back to sync selection: {e}")
        fallback = _next_key_index(n)
        if fallback not in excluded:
            return fallback
        available_indices = [i for i in range(n) if i not in excluded]
        if not available_indices:
            return -1
        i = next(_rr_counter)
        return available_indices[i % len(available_indices)]
    
    # 同步 KeyManager 的聚合模式与 calls_per_rotation 配置到 KeySelector
    try:
        aggregation_mode = await key_manager.get_aggregation_mode()
        if key_selector.mode != aggregation_mode:
            key_selector.mode = aggregation_mode
            log.debug(f"Synced aggregation_mode to KeySelector: {aggregation_mode.value}")

        calls_per_rotation = await key_manager.get_calls_per_rotation()
        if key_selector.calls_per_rotation != calls_per_rotation:
            key_selector.calls_per_rotation = calls_per_rotation
            log.debug(f"Synced calls_per_rotation to KeySelector: {calls_per_rotation}")
    except Exception as e:
        log.warning(f"Failed to sync key selector config: {e}")
    
    # 同步失败记录到 KeySelector
    for idx, fail_time in _failed_keys.items():
        if idx not in key_selector.get_failed_keys():
            await key_selector.mark_key_failed(idx, "sync from assembly_client")
    
    # 从 KeyManager 获取所有密钥信息（包含禁用状态）
    try:
        all_keys_info = await key_manager.get_all_keys()
        # 构建索引到启用状态的映射
        enabled_map = {key.index: key.enabled for key in all_keys_info}
    except Exception as e:
        log.warning(f"Failed to get key enabled status from KeyManager: {e}")
        enabled_map = {}
    
    # 构建 KeyInfo 列表
    keys = []
    for i in range(n):
        # 检查速率限制状态
        is_excluded = i in excluded
        is_exhausted = await rate_limiter.is_key_exhausted(i) or is_excluded
        status = KeyStatus.EXHAUSTED if is_exhausted else KeyStatus.ACTIVE
        
        # 从 KeyManager 获取实际的禁用状态，默认为启用
        is_enabled = enabled_map.get(i, True) and not is_excluded
        
        keys.append(KeyInfo(
            index=i,
            key=f"key_{i}",  # 占位符
            enabled=is_enabled,
            status=status
        ))
    
    # 获取所有启用的密钥索引（用于后续回退逻辑）
    enabled_indices = [i for i in range(n) if enabled_map.get(i, True) and i not in excluded]
    
    # 如果没有启用的密钥，返回 -1 表示无可用密钥
    if not enabled_indices:
        log.error("No enabled keys available - all keys are disabled")
        return -1  # 特殊值表示无可用密钥
    
    # 使用 KeySelector 选择密钥
    selected = await key_selector.select_next_key(keys)
    
    if selected is not None:
        # 检查是否需要轮换
        should_rotate = await key_selector.should_rotate_with_rate_limit(selected.index, rate_limiter)
        if should_rotate:
            log.debug(f"Key {selected.index} triggered rotation, selecting next key")
            # 重新选择下一个密钥
            next_selected = await key_selector.select_next_key(keys)
            if next_selected is not None:
                return next_selected.index
        return selected.index
    
    # 所有启用的 Key 都用尽或失败，尝试找最早重置的（只考虑启用的密钥）
    next_available = await rate_limiter.get_next_available_key(enabled_indices)
    if next_available is not None:
        return next_available
    
    # 回退到失败时间最早的（只考虑启用的密钥）
    enabled_failed = {k: v for k, v in _failed_keys.items() if k in enabled_indices}
    if enabled_failed:
        oldest_idx = min(enabled_failed.keys(), key=lambda k: enabled_failed[k])
        return oldest_idx
    
    # 最后回退到 Round-Robin（只在启用的密钥中选择）
    i = next(_rr_counter)
    return enabled_indices[i % len(enabled_indices)]


def _build_no_available_keys_response():
    """返回无可用密钥错误响应"""
    from fastapi.responses import JSONResponse
    return JSONResponse(
        content={
            "error": {
                "message": "No enabled API keys available. All keys have been disabled.",
                "type": "invalid_request_error",
                "code": "no_available_keys"
            }
        },
        status_code=503
    )


def _build_quota_exhausted_response(blocked_keys: List[Dict[str, Any]]):
    """返回全部密钥达到每日限额的错误响应"""
    from fastapi.responses import JSONResponse

    next_reset_candidates = []
    for item in blocked_keys:
        next_reset = item.get("next_reset_time")
        if isinstance(next_reset, str) and next_reset.strip():
            next_reset_candidates.append(next_reset)
    next_reset_time = min(next_reset_candidates) if next_reset_candidates else None

    return JSONResponse(
        content={
            "error": {
                "message": "All enabled API keys reached daily usage limits for this model.",
                "type": "rate_limit_error",
                "code": "daily_limit_exhausted",
                "next_reset_time": next_reset_time,
                "blocked_keys": blocked_keys,
            }
        },
        status_code=429
    )


async def _select_key_with_daily_quota(keys: List[str], model: str) -> Dict[str, Any]:
    """
    选择一个可用且未触达每日限额的 key。

    Returns:
        {
            "idx": int,
            "api_key": str,
            "reason": "" | "quota_exhausted" | "no_available_keys",
            "blocked": [...]
        }
    """
    from ..stats.unified_stats import get_unified_stats

    unified_stats = await get_unified_stats()
    excluded_indices: Set[int] = set()
    blocked_by_quota: List[Dict[str, Any]] = []

    for _ in range(len(keys)):
        idx = await _next_key_index_async(len(keys), excluded_indices=excluded_indices)
        if idx < 0 or idx >= len(keys):
            break

        api_key = keys[idx]
        quota_state = await unified_stats.can_use_key_for_model(api_key, model)
        if quota_state.get("allowed", True):
            return {
                "idx": idx,
                "api_key": api_key,
                "reason": "",
                "blocked": [],
            }

        excluded_indices.add(idx)
        blocked_by_quota.append({
            "idx": idx,
            "key": _mask_key(api_key),
            "reason": quota_state.get("reason", "quota_reached"),
            "success_count": quota_state.get("success_count", 0),
            "total_limit": quota_state.get("total_limit"),
            "model": quota_state.get("model"),
            "model_success_count": quota_state.get("model_success_count"),
            "model_limit": quota_state.get("model_limit"),
            "next_reset_time": quota_state.get("next_reset_time"),
        })

    if blocked_by_quota:
        return {
            "idx": -1,
            "api_key": "",
            "reason": "quota_exhausted",
            "blocked": blocked_by_quota,
        }

    return {
        "idx": -1,
        "api_key": "",
        "reason": "no_available_keys",
        "blocked": [],
    }

def _mark_key_failed(idx: int):
    """标记 Key 失败"""
    import time
    _failed_keys[idx] = time.time()
    log.debug(f"Marked key index {idx} as failed, total failed keys: {len(_failed_keys)}")

def _mask_key(key: str) -> str:
    if not key:
        return ""
    k = str(key)
    if len(k) <= 8:
        return k[:2] + "***"
    return k[:4] + "..." + k[-4:]


def _truncate_for_log(text: Any, limit: int = 2000) -> str:
    s = "" if text is None else str(text)
    if len(s) <= limit:
        return s
    return f"{s[:limit]}... [truncated, total_len={len(s)}]"


def _sanitize_headers_for_log(headers: Any, max_value_len: int = 240) -> Dict[str, str]:
    try:
        source = dict(headers)
    except Exception:
        return {"_raw": _truncate_for_log(headers, 400)}

    sanitized: Dict[str, str] = {}
    redact_keys = {"authorization", "cookie", "set-cookie"}

    for k, v in source.items():
        key_lower = str(k).lower()
        if key_lower in redact_keys:
            sanitized[str(k)] = "[REDACTED]"
            continue
        sanitized[str(k)] = _truncate_for_log(v, max_value_len)

    return sanitized

async def _update_rate_limit_info(idx: int, api_key: str, headers: dict):
    """更新速率限制信息 - 使用新的 RateLimiter"""
    import time
    try:
        limit = headers.get('x-ratelimit-limit')
        remaining = headers.get('x-ratelimit-remaining')
        reset = headers.get('x-ratelimit-reset')
        
        if limit is not None or remaining is not None:
            masked_key = _mask_key(api_key)
            
            # 同时更新旧的内存缓存（兼容性）
            if idx not in _rate_limit_info:
                _rate_limit_info[idx] = {
                    "key": masked_key,
                    "full_key": api_key,
                }
            
            info = _rate_limit_info[idx]
            info["last_request_time"] = time.time()
            
            limit_val = 0
            remaining_val = 0
            reset_time = 0
            
            if limit is not None:
                try:
                    limit_val = int(limit)
                    info["limit"] = limit_val
                except (ValueError, TypeError):
                    pass
            
            if remaining is not None:
                try:
                    remaining_val = int(remaining)
                    info["remaining"] = remaining_val
                    info["used"] = info.get("limit", 0) - remaining_val
                except (ValueError, TypeError):
                    pass
            
            if reset is not None:
                try:
                    reset_val = int(reset)
                    if reset_val > 0:
                        reset_time = int(time.time() + reset_val)
                        info["reset_time"] = reset_time
                    else:
                        reset_time = int(time.time() + 60)
                        info["reset_time"] = reset_time
                except (ValueError, TypeError):
                    pass
            
            log.debug(f"Updated rate limit info for key {masked_key}: limit={limit_val}, remaining={remaining_val}, reset_in={reset}s")
            
            # 使用新的 RateLimiter 更新
            try:
                rate_limiter = await get_rate_limiter()
                await rate_limiter.update_rate_limit(idx, limit_val, remaining_val, reset_time)
            except Exception as e:
                log.warning(f"Failed to update RateLimiter: {e}")
            
            # 异步保存到存储（兼容旧系统）
            asyncio.create_task(_save_rate_limit_info())
    except Exception as e:
        log.error(f"Failed to update rate limit info: {e}")

async def initialize_rate_limit_system():
    """初始化速率限制系统，在应用启动时调用"""
    log.info("Initializing rate limit system...")
    await _load_rate_limit_info()
    log.info("Rate limit system initialized")

async def get_rate_limit_info() -> dict:
    """获取所有key的速率限制信息"""
    import time
    
    # 确保数据已加载
    await _load_rate_limit_info()
    
    current_time = time.time()
    result = {}
    
    for idx, info in _rate_limit_info.items():
        key_info = {
            "key": info.get("key", "unknown"),
            "limit": info.get("limit", 0),
            "remaining": info.get("remaining", 0),
            "used": info.get("used", 0),
            "last_request_time": info.get("last_request_time", 0),
        }
        
        # 计算重置剩余时间
        reset_time = info.get("reset_time", 0)
        if reset_time > current_time:
            key_info["reset_in_seconds"] = int(reset_time - current_time)
        else:
            key_info["reset_in_seconds"] = 0
            # 如果已经过了重置时间，重置计数器
            if info.get("limit", 0) > 0:
                info["remaining"] = info["limit"]
                info["used"] = 0
                key_info["remaining"] = info["limit"]
                key_info["used"] = 0
        
        result[idx] = key_info
    
    return result

async def fetch_assembly_models() -> Dict[str, Any]:
    """
    查询 AssemblyAI LLM Gateway 的可用模型列表。
    尝试使用第一个可用的 API Key 调用 /v1/models。
    返回 {"models": ["id1","id2",...]} 格式。
    """
    endpoint = await get_assembly_endpoint()
    # 将 chat/completions 替换为 models 列表端点
    models_url = endpoint.replace("/chat/completions", "/models")
    keys = await get_assembly_api_keys()
    if not keys:
        return {"models": []}
    api_key = keys[0]
    try:
        async with http_client.get_client(timeout=30.0) as client:
            headers = {"Authorization": api_key}
            resp = await client.get(models_url, headers=headers)
            if 200 <= resp.status_code < 400:
                try:
                    data = resp.json()
                except Exception:
                    import json as _json
                    data = _json.loads(resp.text or "{}")
                # 支持多种返回结构
                models = []
                meta: Dict[str, Any] = {}
                if isinstance(data, dict):
                    if "data" in data and isinstance(data["data"], list):
                        for item in data["data"]:
                            mid = item.get("id") if isinstance(item, dict) else str(item)
                            if mid:
                                models.append(mid)
                                # 解析元数据
                                tp = item.get("top_provider", {}) if isinstance(item, dict) else {}
                                meta[mid] = {
                                    "name": item.get("name") or mid,
                                    "description": item.get("description") or "",
                                    "context_length": tp.get("context_length") or item.get("context_length"),
                                    "max_tokens": tp.get("max_completion_tokens"),
                                    "default_parameters": item.get("default_parameters") or {},
                                }
                    elif "models" in data and isinstance(data["models"], list):
                        for item in data["models"]:
                            mid = item.get("id") if isinstance(item, dict) else str(item)
                            if mid:
                                models.append(mid)
                                tp = item.get("top_provider", {}) if isinstance(item, dict) else {}
                                meta[mid] = {
                                    "name": item.get("name") or mid,
                                    "description": item.get("description") or "",
                                    "context_length": tp.get("context_length") or item.get("context_length"),
                                    "max_tokens": tp.get("max_completion_tokens"),
                                    "default_parameters": item.get("default_parameters") or {},
                                }
                elif isinstance(data, list):
                    for item in data:
                        mid = item.get("id") if isinstance(item, dict) else str(item)
                        if mid:
                            models.append(mid)
                            tp = item.get("top_provider", {}) if isinstance(item, dict) else {}
                            meta[mid] = {
                                "name": item.get("name") or mid,
                                "description": item.get("description") or "",
                                "context_length": tp.get("context_length") or item.get("context_length"),
                                "max_tokens": tp.get("max_completion_tokens"),
                                "default_parameters": item.get("default_parameters") or {},
                            }
                return {"models": models, "meta": meta}
            else:
                log.error(f"Fetch models failed: {resp.status_code}")
                return {"models": [], "meta": {}}
    except Exception as e:
        log.error(f"Fetch models error: {e}")
        return {"models": [], "meta": {}}

async def send_assembly_request(
    openai_request: ChatCompletionRequest,
    is_streaming: bool = False,
    trace = None,  # Optional: 性能追踪对象，用于记录使用的密钥信息
):
    """
    调用 AssemblyAI LLM Gateway，支持与 OpenAI 兼容的请求格式。
    目前实现非流式调用；如需流式，建议结合假流式。
    
    Args:
        openai_request: 请求对象
        is_streaming: 是否流式
        trace: 可选的性能追踪对象，用于记录使用的密钥信息
    """
    # 构造请求体
    sanitized_messages = _sanitize_messages(openai_request.messages)
    sanitized_messages = _ensure_tool_block_required_fields(sanitized_messages)
    
    # 详细日志：显示消息结构
    log.debug(f"Message structure before sending to AssemblyAI:")
    for i, msg in enumerate(sanitized_messages):
        role = msg.get("role", "unknown")
        content_preview = str(msg.get("content", ""))[:100]
        has_tool_calls = "tool_calls" in msg
        has_tool_call_id = "tool_call_id" in msg
        log.debug(f"  [{i}] role={role}, content={content_preview}..., tool_calls={has_tool_calls}, tool_call_id={has_tool_call_id}")
    
    # 检测模型类型
    model_lower = openai_request.model.lower()
    is_claude = "claude" in model_lower
    is_gemini = "gemini" in model_lower
    # Claude 4.5 系列模型对参数更严格
    is_claude_45 = is_claude and ("4-5" in model_lower or "4.5" in model_lower or "45" in model_lower)

    has_function_call_messages = any(
        isinstance(m, dict) and m.get("type") in {"function_call", "function_call_output"}
        for m in sanitized_messages
    )
    native_tool_messages: List[Dict[str, Any]] = []
    if is_claude and has_function_call_messages:
        native_tool_messages = _build_claude_tool_fallback_messages(sanitized_messages)

    # 默认策略：Claude + 工具历史时，优先走原生 role/content tool 块；
    # 但若末条不是 user，会触发 assistant prefill 限制，改用 function_call 主路径。
    native_messages_end_with_user = _messages_end_with_user(native_tool_messages)
    use_claude_tool_native_messages = (
        is_claude and has_function_call_messages and native_messages_end_with_user
    )
    request_messages = (
        native_tool_messages
        if use_claude_tool_native_messages
        else sanitized_messages
    )
    request_messages = _ensure_tool_block_required_fields(request_messages)
    if use_claude_tool_native_messages:
        log.warning(
            "[CLAUDE_TOOL_NATIVE] Using role/content tool blocks as primary payload for Claude tool history"
        )
        log.warning(f"[CLAUDE_TOOL_NATIVE] Shape: {_summarize_message_shapes(request_messages)}")
    elif is_claude and has_function_call_messages and native_tool_messages:
        log.warning(
            "[CLAUDE_TOOL_NATIVE] Skipped primary native tool blocks because conversation does not end with user"
        )
        log.warning(f"[CLAUDE_TOOL_NATIVE] Shape: {_summarize_message_shapes(sanitized_messages)}")

    payload: Dict[str, Any] = {
        "model": openai_request.model,
        "messages": request_messages,
    }
    
    # 透传常用参数
    for key in [
        "max_tokens",
        "stop",
        "seed",
        "response_format",
        "tools",
        "tool_choice",
    ]:
        val = getattr(openai_request, key, None)
        if val is not None:
            payload[key] = val
    
    # temperature 和 top_p 处理
    temp = getattr(openai_request, "temperature", None)
    top_p = getattr(openai_request, "top_p", None)
    
    if is_claude_45:
        # Claude 4.5 特殊处理：
        # 1. temperature=0 改为 0.01（Claude 不支持 0）
        # 2. 不同时传递 temperature 和 top_p（可能冲突）
        if temp is not None:
            if temp == 0:
                temp = 0.01
            payload["temperature"] = temp
            # 如果设置了 temperature，不传递 top_p
        elif top_p is not None:
            payload["top_p"] = top_p
    elif is_gemini:
        # Gemini 模型特殊处理：
        # temperature=1.0 可能导致某些模型返回空响应，降低到 0.7
        if temp is not None:
            if temp >= 1.0:
                log.info(f"[GEMINI] Adjusting temperature from {temp} to 0.7 for better compatibility")
                temp = 0.7
            payload["temperature"] = temp
        if top_p is not None:
            payload["top_p"] = top_p
    else:
        # 其他模型：正常传递
        if temp is not None:
            payload["temperature"] = temp
        if top_p is not None:
            payload["top_p"] = top_p
    
    # n 参数：Claude 可能不支持
    n_val = getattr(openai_request, "n", None)
    if n_val is not None and n_val != 1 and not is_claude:
        payload["n"] = n_val
    
    # frequency_penalty 和 presence_penalty：Claude 不支持
    if not is_claude:
        for penalty_key in ["frequency_penalty", "presence_penalty"]:
            val = getattr(openai_request, penalty_key, None)
            if val is not None and val != 0:
                log.debug(f"Including {penalty_key}={val} in request")
                payload[penalty_key] = val
    else:
        log.debug(f"Skipping unsupported params for Claude model: {openai_request.model}")
    
    # 记录完整的 payload（用于调试）
    payload_debug = {k: v for k, v in payload.items() if k != 'messages'}
    log.debug(f"Final payload for {openai_request.model}: {payload_debug}")

    endpoint = await get_assembly_endpoint()
    keys = await get_assembly_api_keys()
    tool_debug_enabled = await get_tool_debug_logs_enabled()
    if not keys:
        return JSONResponse(content={"error": {"message": "No AssemblyAI API keys configured", "type": "config_error"}}, status_code=500)

    max_retries = await get_retry_429_max_retries()
    retry_enabled = await get_retry_429_enabled()
    retry_interval = await get_retry_429_interval()

    if is_streaming:
        payload["stream"] = True

    post_data = json.dumps(payload)
    
    # 对于 Claude 4.5，记录完整的请求以便调试
    if is_claude_45:
        log.debug(f"Claude 4.5 request payload: {_truncate_for_log(post_data, 1200)}")

    claude_tool_fallback_used = False

    for attempt in range(max_retries + 1):
        idx = -1
        api_key = ""
        try:
            # 选择可用且未触达每日限额的 key
            selection = await _select_key_with_daily_quota(keys, openai_request.model)
            idx = selection.get("idx", -1)
            api_key = selection.get("api_key", "")

            if idx < 0 or idx >= len(keys):
                if selection.get("reason") == "quota_exhausted":
                    log.warning("All enabled keys reached daily usage limits")
                    return _build_quota_exhausted_response(selection.get("blocked", []))
                log.error("No enabled API keys available during retry - all keys are disabled or invalid")
                return _build_no_available_keys_response()

            headers = {"Authorization": api_key, "Content-Type": "application/json"}

            # 记录密钥信息到性能追踪
            if trace:
                trace.key_index = idx
                trace.key_masked = _mask_key(api_key)

                async def _update_trace_account():
                    try:
                        trace.account_email = await _find_account_for_key(api_key)
                    except Exception as e:
                        log.debug(f"Failed to update trace account: {e}")

                asyncio.create_task(_update_trace_account())

            # INFO 级别：简要日志
            log.info(f"REQ model={openai_request.model} key={_mask_key(api_key)} attempt={attempt+1}/{max_retries+1} key_idx={idx}")

            # [TOOL_DEBUG] 完整请求信息 - 用于调试工具调用
            if tool_debug_enabled:
                log.debug(f"[TOOL_DEBUG] REQ Endpoint: {endpoint}")
                log.debug(f"[TOOL_DEBUG] REQ Payload: {_truncate_for_log(post_data, 4000)}")

            if is_streaming:
                stream_client = await create_streaming_client_with_kwargs()
                stream_ctx = stream_client.stream("POST", endpoint, content=post_data, headers=headers)
                try:
                    resp = await stream_ctx.__aenter__()
                except Exception:
                    await stream_client.aclose()
                    raise

                # 更新速率限制信息（流式连接建立时即可读取响应头）
                await _update_rate_limit_info(idx, api_key, resp.headers)

                # 检查是否需要重试（429 或 400 速率限制错误）
                should_retry = False
                retry_reason = ""
                error_body = None
                error_msg = ""
                response_text = ""

                # 检查自动封禁配置
                auto_ban_enabled = await get_auto_ban_enabled()
                auto_ban_codes = await get_auto_ban_error_codes() if auto_ban_enabled else []

                if resp.status_code == 400 or resp.status_code >= 400:
                    try:
                        raw_body = await resp.aread()
                        response_text = raw_body.decode("utf-8", errors="replace") if isinstance(raw_body, bytes) else str(raw_body)
                        try:
                            error_body = json.loads(response_text)
                            if isinstance(error_body, dict):
                                error_msg = str(error_body.get("message", "")).lower()
                        except Exception:
                            error_body = None
                    except Exception:
                        response_text = ""

                # 如果启用了自动封禁且响应码在封禁列表中，自动禁用该密钥
                if auto_ban_enabled and resp.status_code in auto_ban_codes:
                    log.warning(f"[AUTO_BAN] Key {_mask_key(api_key)} (idx={idx}) triggered auto-ban due to error code {resp.status_code}")
                    try:
                        from .key_manager import get_key_manager
                        key_manager = await get_key_manager()
                        await key_manager.update_key_state(idx, {
                            "exhausted": True,
                            "error": f"Auto-banned: HTTP {resp.status_code}",
                            "disable_reason": f"自动封禁: HTTP {resp.status_code}",
                        })
                        log.info(f"[AUTO_BAN] Key {idx} has been automatically disabled")
                    except Exception as ban_err:
                        log.error(f"[AUTO_BAN] Failed to auto-ban key {idx}: {ban_err}")
                    _mark_key_failed(idx)
                    if retry_enabled and attempt < max_retries:
                        should_retry = True
                        retry_reason = f"Auto-ban triggered (HTTP {resp.status_code})"

                if resp.status_code == 429:
                    should_retry = True
                    retry_reason = "429 Too Many Requests"
                    _mark_key_failed(idx)
                elif resp.status_code == 400 and not should_retry:
                    # 先解析错误消息，判断是真正的速率限制还是请求错误
                    if any(keyword in error_msg for keyword in ["processing error", "invalid", "unsupported", "not supported"]):
                        log.warning(f"Request error (not rate limit): {error_msg}")
                        should_retry = False
                    elif any(keyword in error_msg for keyword in ["rate limit", "rate_limit", "quota exceeded", "too many requests"]):
                        should_retry = True
                        retry_reason = f"400 Rate Limit: {error_body.get('message', 'Unknown') if isinstance(error_body, dict) else 'Unknown'}"
                        _mark_key_failed(idx)
                        log.warning(f"Key {_mask_key(api_key)} hit rate limit: {error_msg}")
                    else:
                        ratelimit_remaining = resp.headers.get('x-ratelimit-remaining')
                        if ratelimit_remaining is not None:
                            try:
                                remaining = int(ratelimit_remaining)
                                if remaining == 0:
                                    ratelimit_limit = resp.headers.get('x-ratelimit-limit', '?')
                                    should_retry = True
                                    retry_reason = f"400 Rate Limit Exhausted (remaining: 0/{ratelimit_limit})"
                                    _mark_key_failed(idx)
                                    log.warning(f"Key {_mask_key(api_key)} rate limit exhausted: 0/{ratelimit_limit} remaining")
                            except (ValueError, TypeError):
                                pass

                if should_retry and retry_enabled and attempt < max_retries:
                    log.warning(f"[RETRY] {retry_reason}, switching to next key ({attempt + 1}/{max_retries})")
                    try:
                        await stream_ctx.__aexit__(None, None, None)
                    finally:
                        await stream_client.aclose()
                    await asyncio.sleep(retry_interval)
                    continue

                status_cat = "OK" if 200 <= resp.status_code < 400 else f"FAIL({resp.status_code})"
                log.info(f"RES model={openai_request.model} key={_mask_key(api_key)} status={status_cat}")
                if tool_debug_enabled:
                    log.debug(f"[TOOL_DEBUG] RES Status Code: {resp.status_code}")
                    log.debug(f"[TOOL_DEBUG] RES Headers: {_sanitize_headers_for_log(resp.headers)}")

                # 流式请求在响应头阶段失败：直接返回错误，供上层按 bootstrap 策略处理
                if not (200 <= resp.status_code < 400):
                    error_message = response_text or f"Upstream HTTP {resp.status_code}"
                    try:
                        await stream_ctx.__aexit__(None, None, None)
                    finally:
                        await stream_client.aclose()
                    return JSONResponse(
                        content={"error": {"message": error_message, "type": "api_error"}},
                        status_code=resp.status_code
                    )

                # 记录成功调用（流式连接已建立）
                try:
                    from ..stats.unified_stats import get_unified_stats
                    unified_stats = await get_unified_stats()
                    await unified_stats.record_call(api_key, openai_request.model, success=True)
                    log.debug(f"Recorded streaming usage stats for key {_mask_key(api_key)}, model {openai_request.model}")
                except Exception as e:
                    log.error(f"Failed to record streaming usage statistics: {e}", exc_info=True)

                async def upstream_stream_generator():
                    try:
                        async for line in resp.aiter_lines():
                            if line is None:
                                continue
                            line = line.strip()
                            if not line:
                                continue
                            if line.startswith("data:"):
                                yield f"{line}\n\n".encode("utf-8")
                            else:
                                yield f"data: {line}\n\n".encode("utf-8")
                    finally:
                        try:
                            await stream_ctx.__aexit__(None, None, None)
                        finally:
                            await stream_client.aclose()

                return StreamingResponse(
                    upstream_stream_generator(),
                    media_type="text/event-stream",
                )

            # 非流式请求保持原行为
            async with http_client.get_client(timeout=None) as client:
                resp = await client.post(endpoint, content=post_data, headers=headers)

                # Claude 工具调用历史兼容兜底：命中 tool_use.input 校验错误时，切换另一种编码重发一次
                if (
                    is_claude
                    and has_function_call_messages
                    and not claude_tool_fallback_used
                    and (
                        _is_tool_use_input_validation_error(resp)
                        or _is_assistant_prefill_error(resp)
                    )
                ):
                    claude_tool_fallback_used = True
                    fallback_payload = dict(payload)
                    if use_claude_tool_native_messages:
                        fallback_payload["messages"] = _ensure_tool_block_required_fields(sanitized_messages)
                        fallback_label = "function_call"
                    else:
                        fallback_payload["messages"] = _ensure_tool_block_required_fields(
                            native_tool_messages
                            if native_tool_messages
                            else _build_claude_tool_fallback_messages(sanitized_messages)
                        )
                        fallback_label = "role/content tool blocks"
                    fallback_post_data = json.dumps(fallback_payload)
                    log.warning(
                        f"[CLAUDE_TOOL_FALLBACK] Retrying once with {fallback_label} "
                        "after Claude tool format validation error"
                    )
                    log.warning(
                        f"[CLAUDE_TOOL_FALLBACK] Shape: {_summarize_message_shapes(fallback_payload['messages'])}"
                    )
                    if tool_debug_enabled:
                        log.debug(
                            f"[CLAUDE_TOOL_FALLBACK] REQ Payload: {_truncate_for_log(fallback_post_data, 4000)}"
                        )
                    resp = await client.post(endpoint, content=fallback_post_data, headers=headers)

            # 更新速率限制信息
            await _update_rate_limit_info(idx, api_key, resp.headers)

            # 检查是否需要重试（429 或 400 速率限制错误）
            should_retry = False
            retry_reason = ""

            # 检查自动封禁配置
            auto_ban_enabled = await get_auto_ban_enabled()
            auto_ban_codes = await get_auto_ban_error_codes() if auto_ban_enabled else []

            # 如果启用了自动封禁且响应码在封禁列表中，自动禁用该密钥
            if auto_ban_enabled and resp.status_code in auto_ban_codes:
                log.warning(f"[AUTO_BAN] Key {_mask_key(api_key)} (idx={idx}) triggered auto-ban due to error code {resp.status_code}")
                try:
                    from .key_manager import get_key_manager
                    key_manager = await get_key_manager()
                    await key_manager.update_key_state(idx, {
                        "exhausted": True,
                        "error": f"Auto-banned: HTTP {resp.status_code}",
                        "disable_reason": f"自动封禁: HTTP {resp.status_code}",
                    })
                    log.info(f"[AUTO_BAN] Key {idx} has been automatically disabled")
                except Exception as ban_err:
                    log.error(f"[AUTO_BAN] Failed to auto-ban key {idx}: {ban_err}")
                _mark_key_failed(idx)
                # 继续重试使用其他密钥
                if retry_enabled and attempt < max_retries:
                    should_retry = True
                    retry_reason = f"Auto-ban triggered (HTTP {resp.status_code})"
                    await asyncio.sleep(retry_interval)
                    continue

            if resp.status_code == 429:
                should_retry = True
                retry_reason = "429 Too Many Requests"
                _mark_key_failed(idx)
            elif resp.status_code == 400:
                # 先解析错误消息，判断是真正的速率限制还是请求错误
                error_body = None
                error_msg = ""
                try:
                    error_body = resp.json() if hasattr(resp, 'json') else json.loads(resp.text)
                    error_msg = error_body.get("message", "").lower()
                except Exception:
                    pass

                # 如果是 "LLM processing error" 或类似的请求错误，不要重试，直接返回
                # 这类错误通常是请求格式问题，重试没有意义
                if any(keyword in error_msg for keyword in ["processing error", "invalid", "unsupported", "not supported"]):
                    log.warning(f"Request error (not rate limit): {error_msg}")
                    # 不标记 key 失败，不重试，直接返回让上层处理
                    should_retry = False
                # 检查是否是真正的速率限制错误（消息中明确包含 rate/limit/quota）
                elif any(keyword in error_msg for keyword in ["rate limit", "rate_limit", "quota exceeded", "too many requests"]):
                    should_retry = True
                    retry_reason = f"400 Rate Limit: {error_body.get('message', 'Unknown') if error_body else 'Unknown'}"
                    _mark_key_failed(idx)
                    log.warning(f"Key {_mask_key(api_key)} hit rate limit: {error_msg}")
                # 只有当响应头显示 remaining=0 时才认为是速率限制
                else:
                    ratelimit_remaining = resp.headers.get('x-ratelimit-remaining')
                    if ratelimit_remaining is not None:
                        try:
                            remaining = int(ratelimit_remaining)
                            # 只有 remaining=0 才是真正的速率限制耗尽
                            if remaining == 0:
                                ratelimit_limit = resp.headers.get('x-ratelimit-limit', '?')
                                should_retry = True
                                retry_reason = f"400 Rate Limit Exhausted (remaining: 0/{ratelimit_limit})"
                                _mark_key_failed(idx)
                                log.warning(f"Key {_mask_key(api_key)} rate limit exhausted: 0/{ratelimit_limit} remaining")
                        except (ValueError, TypeError):
                            pass

            if should_retry and retry_enabled and attempt < max_retries:
                log.warning(f"[RETRY] {retry_reason}, switching to next key ({attempt + 1}/{max_retries})")
                await asyncio.sleep(retry_interval)
                continue  # 下次循环会自动选择新的 Key

            status_cat = "OK" if 200 <= resp.status_code < 400 else f"FAIL({resp.status_code})"

            # INFO 级别：简要响应
            log.info(f"RES model={openai_request.model} key={_mask_key(api_key)} status={status_cat}")

            # [TOOL_DEBUG] 完整响应信息 - 用于调试工具调用
            if tool_debug_enabled:
                log.debug(f"[TOOL_DEBUG] RES Status Code: {resp.status_code}")
                log.debug(f"[TOOL_DEBUG] RES Headers: {_sanitize_headers_for_log(resp.headers)}")
                try:
                    response_text = resp.text if hasattr(resp, 'text') else str(resp.content)
                    log.debug(f"[TOOL_DEBUG] RES Body: {_truncate_for_log(response_text, 6000)}")
                except Exception as e:
                    log.debug(f"[TOOL_DEBUG] RES Body: [Unable to decode: {e}]")

            # 记录调用统计（使用统一统计模块）
            if 200 <= resp.status_code < 400:
                try:
                    from ..stats.unified_stats import get_unified_stats
                    unified_stats = await get_unified_stats()
                    await unified_stats.record_call(api_key, openai_request.model, success=True)
                    log.debug(f"Successfully recorded usage stats for key {_mask_key(api_key)}, model {openai_request.model}")
                except Exception as e:
                    log.error(f"Failed to record usage statistics: {e}", exc_info=True)
            else:
                # 记录失败的调用
                try:
                    from ..stats.unified_stats import get_unified_stats
                    unified_stats = await get_unified_stats()
                    await unified_stats.record_call(api_key, openai_request.model, success=False)
                except Exception as e:
                    log.warning(f"Failed to record failure statistics: {e}")
            return resp
        except Exception as e:
            error_msg = str(e) if str(e) else repr(e)
            error_type = type(e).__name__
            
            # 记录失败统计（连接错误等异常也要记录）
            try:
                from ..stats.unified_stats import get_unified_stats
                unified_stats = await get_unified_stats()
                # 尝试获取当前使用的密钥
                current_key = keys[idx] if isinstance(idx, int) and 0 <= idx < len(keys) else (keys[0] if keys else "unknown")
                await unified_stats.record_call(current_key, openai_request.model, success=False)
                log.debug(f"Recorded connection failure for key {_mask_key(current_key)}, model {openai_request.model}")
            except Exception as stats_err:
                log.warning(f"Failed to record connection failure statistics: {stats_err}")
            
            if attempt < max_retries:
                log.warning(f"[RETRY] AssemblyAI request failed ({error_type}), retrying ({attempt + 1}/{max_retries}): {error_msg}")
                await asyncio.sleep(retry_interval)
                continue
            else:
                log.error(f"AssemblyAI request failed ({error_type}): {error_msg}", exc_info=True)
                from fastapi.responses import JSONResponse
                return JSONResponse(content={"error": {"message": f"Request failed ({error_type}): {error_msg}", "type": "api_error"}}, status_code=500)

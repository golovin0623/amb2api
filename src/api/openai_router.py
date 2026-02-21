"""
OpenAI Router - Handles OpenAI format API requests
处理OpenAI格式请求的路由模块
"""
import json
import time
import uuid
import asyncio
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Depends, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from config import get_available_models_async, is_fake_streaming_model, is_anti_truncation_model
from log import log
from ..services.assembly_client import send_assembly_request
from ..services.assembly_stream_handler import fake_stream_response_for_assembly, convert_streaming_response
from ..models.models import ChatCompletionRequest, ModelList, Model
from ..transform.openai_transfer import assembly_response_to_openai
from ..transform.claude_to_openai import convert_claude_request_to_openai
from ..transform.openai_to_claude import (
    openai_response_to_anthropic,
    convert_openai_sse_to_anthropic_events,
    anthropic_events_to_sse_bytes,
    openai_error_to_anthropic_error,
    estimate_input_tokens,
    openai_models_to_anthropic,
)
from ..stats.performance_tracker import get_performance_tracker



# 创建路由器
router = APIRouter()
security = HTTPBearer()

# AssemblyAI 适配不需要 Google 凭证管理器

async def authenticate(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """验证用户密码"""
    from config import get_api_password
    password = await get_api_password()
    token = credentials.credentials
    if token != password:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="密码错误")
    return token

@router.get("/v1/models")
async def list_models(request: Request):
    """返回 OpenAI/Anthropic 兼容的模型列表。"""
    models = await get_available_models_async("openai")
    if request.headers.get("anthropic-version"):
        return JSONResponse(content=openai_models_to_anthropic([str(m) for m in models]))
    return ModelList(data=[Model(id=m) for m in models])


async def authenticate_anthropic_request(request: Request) -> str:
    """Anthropic-compatible auth: x-api-key first, then Bearer."""
    from config import get_api_password

    password = await get_api_password()

    token = request.headers.get("x-api-key", "").strip()
    if not token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header.split(" ", 1)[1].strip()

    if token != password:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="密码错误")
    return token


def _extract_response_text(response: Any) -> str:
    """Decode generic response object to text."""
    if hasattr(response, "text") and isinstance(getattr(response, "text"), str):
        return response.text
    if hasattr(response, "body"):
        body = response.body
        if isinstance(body, bytes):
            return body.decode("utf-8", errors="replace")
        return str(body)
    if hasattr(response, "content"):
        content = response.content
        if isinstance(content, bytes):
            return content.decode("utf-8", errors="replace")
        return str(content)
    return str(response)


def _parse_response_json(text: str, response: Any) -> Any:
    """Best-effort parse of response body as JSON."""
    parsed = None
    try:
        parsed = json.loads(text.strip())
    except Exception:
        if "data:" in text:
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            for line in reversed(lines):
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    continue
                try:
                    parsed = json.loads(payload)
                    break
                except Exception:
                    pass
        if parsed is None and hasattr(response, "json"):
            try:
                parsed = response.json()
            except Exception:
                parsed = None
    return parsed


def _build_openai_fallback_text_response(model: str, text: str) -> Dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text.strip()},
                "finish_reason": "stop",
            }
        ],
    }


def _to_non_negative_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
        return parsed if parsed >= 0 else default
    except (TypeError, ValueError):
        return default


def _extract_usage_metrics(usage: Any) -> Dict[str, int]:
    usage_map = usage if isinstance(usage, dict) else {}
    prompt_tokens = _to_non_negative_int(
        usage_map.get("prompt_tokens", usage_map.get("input_tokens", 0)),
        0,
    )
    completion_tokens = _to_non_negative_int(
        usage_map.get("completion_tokens", usage_map.get("output_tokens", 0)),
        0,
    )
    cached_tokens = _to_non_negative_int(
        usage_map.get("cached_tokens", usage_map.get("input_cached_tokens", 0)),
        0,
    )
    if cached_tokens == 0:
        prompt_details = usage_map.get("prompt_tokens_details")
        if isinstance(prompt_details, dict):
            cached_tokens = _to_non_negative_int(prompt_details.get("cached_tokens", 0), 0)
    total_tokens = _to_non_negative_int(
        usage_map.get("total_tokens", prompt_tokens + completion_tokens),
        prompt_tokens + completion_tokens,
    )
    total_tokens = max(total_tokens, prompt_tokens + completion_tokens)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": cached_tokens,
        "total_tokens": total_tokens,
    }


def _extract_openai_response_diagnostics(openai_response: Any) -> Dict[str, Any]:
    """Extract non-sensitive diagnostics from OpenAI-style response."""
    finish_reason = "-"
    tool_calls_count = 0

    if isinstance(openai_response, dict):
        choices = openai_response.get("choices")
        if isinstance(choices, list) and choices:
            first_choice = choices[0]
            if isinstance(first_choice, dict):
                fr = first_choice.get("finish_reason")
                if fr is not None:
                    finish_reason = str(fr)
                message = first_choice.get("message")
                if isinstance(message, dict):
                    tool_calls = message.get("tool_calls")
                    if isinstance(tool_calls, list):
                        tool_calls_count = sum(1 for tc in tool_calls if isinstance(tc, dict))

    return {
        "finish_reason": finish_reason,
        "tool_calls_count": tool_calls_count,
    }


def _summarize_upstream_response_shape(response_data: Any) -> Dict[str, Any]:
    """Summarize non-sensitive upstream response shape for tool-call diagnostics."""
    summary: Dict[str, Any] = {
        "response_type": type(response_data).__name__,
        "choices_count": 0,
        "choice_shapes": [],
    }

    if not isinstance(response_data, dict):
        return summary

    choices = response_data.get("choices")
    if not isinstance(choices, list):
        return summary

    summary["choices_count"] = len(choices)
    choice_shapes = []
    for idx, choice in enumerate(choices[:3]):
        if not isinstance(choice, dict):
            choice_shapes.append({"index": idx, "choice_type": type(choice).__name__})
            continue

        msg = choice.get("message")
        msg_role = msg.get("role") if isinstance(msg, dict) else None
        msg_content = msg.get("content") if isinstance(msg, dict) else None
        msg_content_kind = type(msg_content).__name__
        content_block_types = []
        if isinstance(msg_content, list):
            for block in msg_content[:5]:
                if isinstance(block, dict):
                    block_type = block.get("type")
                    content_block_types.append(str(block_type) if block_type is not None else "-")
                else:
                    content_block_types.append(type(block).__name__)

        choice_tool_calls = choice.get("tool_calls")
        choice_tool_calls_count = (
            sum(1 for tc in choice_tool_calls if isinstance(tc, dict))
            if isinstance(choice_tool_calls, list)
            else 0
        )

        message_tool_calls = msg.get("tool_calls") if isinstance(msg, dict) else None
        message_tool_calls_count = (
            sum(1 for tc in message_tool_calls if isinstance(tc, dict))
            if isinstance(message_tool_calls, list)
            else 0
        )

        has_xml_function_calls = isinstance(msg_content, str) and "<function_calls>" in msg_content

        choice_shapes.append(
            {
                "index": idx,
                "finish_reason": choice.get("finish_reason"),
                "message_role": msg_role,
                "message_content_kind": msg_content_kind,
                "content_block_types": content_block_types,
                "choice_tool_calls_count": choice_tool_calls_count,
                "message_tool_calls_count": message_tool_calls_count,
                "has_xml_function_calls": has_xml_function_calls,
            }
        )

    summary["choice_shapes"] = choice_shapes
    return summary


def _update_stream_diagnostics_from_payload(payload_text: str, diag: Dict[str, Any]) -> None:
    """Update streaming diagnostics from one OpenAI SSE payload text."""
    payload = payload_text.strip()
    if not payload or payload == "[DONE]":
        return

    try:
        parsed = json.loads(payload)
    except Exception:
        return

    if not isinstance(parsed, dict):
        return

    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        return

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return

    diag["chunk_count"] = int(diag.get("chunk_count", 0)) + 1

    delta = first_choice.get("delta")
    if isinstance(delta, dict):
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            diag["tool_calls_count"] = int(diag.get("tool_calls_count", 0)) + sum(
                1 for tc in tool_calls if isinstance(tc, dict)
            )

    finish_reason = first_choice.get("finish_reason")
    if finish_reason is not None:
        diag["finish_reason"] = str(finish_reason)


async def _convert_openai_stream_to_anthropic(
    openai_stream_response: StreamingResponse, model: str
) -> StreamingResponse:
    """Re-map OpenAI SSE stream body to Anthropic SSE events."""

    async def anthropic_stream_generator():
        state: Dict[str, Any] = {"model": model}
        stream_diag: Dict[str, Any] = {
            "chunk_count": 0,
            "tool_calls_count": 0,
            "finish_reason": "-",
        }
        buf = ""
        async for chunk in openai_stream_response.body_iterator:
            if isinstance(chunk, bytes):
                buf += chunk.decode("utf-8", errors="replace")
            else:
                buf += str(chunk)

            parts = buf.split("\n\n")
            buf = parts.pop()
            for part in parts:
                line = part.strip()
                if not line:
                    continue
                if line.startswith(":"):
                    yield f"{line}\n\n".encode("utf-8")
                    continue
                for single_line in line.splitlines():
                    single_line = single_line.strip()
                    if not single_line.startswith("data:"):
                        continue
                    payload = single_line[5:].strip()
                    _update_stream_diagnostics_from_payload(payload, stream_diag)
                    events = convert_openai_sse_to_anthropic_events(
                        payload, state=state, default_model=model
                    )
                    if events:
                        yield anthropic_events_to_sse_bytes(events)

        if buf.strip():
            line = buf.strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                _update_stream_diagnostics_from_payload(payload, stream_diag)
                events = convert_openai_sse_to_anthropic_events(
                    payload, state=state, default_model=model
                )
                if events:
                    yield anthropic_events_to_sse_bytes(events)

        # Ensure message_stop exists even if upstream only ended with [DONE].
        events = convert_openai_sse_to_anthropic_events(
            "[DONE]", state=state, default_model=model
        )
        if events:
            yield anthropic_events_to_sse_bytes(events)

        log.info(
            f"[ANTHROPIC_DIAG] stream=1 model={model} finish_reason={stream_diag.get('finish_reason', '-')} "
            f"tool_calls_count={stream_diag.get('tool_calls_count', 0)} chunk_count={stream_diag.get('chunk_count', 0)}"
        )

    return StreamingResponse(anthropic_stream_generator(), media_type="text/event-stream")


@router.post("/v1/messages")
async def anthropic_messages(
    request: Request,
):
    """处理 Anthropic Messages API 请求。"""
    try:
        await authenticate_anthropic_request(request)
    except HTTPException as e:
        return JSONResponse(
            content=openai_error_to_anthropic_error(
                e.status_code, {"message": str(e.detail)}, fallback_message=str(e.detail)
            ),
            status_code=e.status_code,
        )

    trace_id = str(uuid.uuid4())
    tracker = await get_performance_tracker()
    trace = tracker.start_trace(trace_id, "pending")
    trace.mark("auth_complete")

    try:
        raw_data = await request.json()
    except Exception as e:
        return JSONResponse(
            content=openai_error_to_anthropic_error(
                400, {"message": f"Invalid JSON: {str(e)}"}, fallback_message="Invalid JSON"
            ),
            status_code=400,
        )

    try:
        openai_payload = convert_claude_request_to_openai(raw_data)
    except Exception as e:
        return JSONResponse(
            content=openai_error_to_anthropic_error(
                400, {"message": str(e)}, fallback_message="Invalid request"
            ),
            status_code=400,
        )

    try:
        request_data = ChatCompletionRequest(**openai_payload)
        trace.model = request_data.model
    except Exception as e:
        return JSONResponse(
            content=openai_error_to_anthropic_error(
                400, {"message": f"Request validation error: {str(e)}"}, fallback_message="Request validation error"
            ),
            status_code=400,
        )

    if getattr(request_data, "max_tokens", None) is not None and request_data.max_tokens > 65535:
        request_data.max_tokens = 65535

    setattr(request_data, "top_k", 64)
    trace.mark("preprocessing_complete")

    model = request_data.model
    is_streaming = bool(getattr(request_data, "stream", False))
    use_fake_streaming = is_fake_streaming_model(model)
    message_count = len(getattr(request_data, "messages", []) or [])

    log.info(
        f"[ANTHROPIC_REQ_DIAG] stream={1 if is_streaming else 0} model={model} "
        f"max_tokens={getattr(request_data, 'max_tokens', None)} messages={message_count}"
    )

    if use_fake_streaming and is_streaming:
        request_data.stream = False
        openai_stream = await fake_stream_response_for_assembly(request_data, trace=trace)
        return await _convert_openai_stream_to_anthropic(openai_stream, model)

    if is_streaming:
        from config import get_enable_real_streaming

        enable_real_streaming = await get_enable_real_streaming()
        if enable_real_streaming:
            async def request_provider():
                return await send_assembly_request(request_data, True, trace=trace)

            upstream_response = await request_provider()
            openai_stream = await convert_streaming_response(
                upstream_response,
                model,
                trace=trace,
                request_provider=request_provider,
            )
        else:
            openai_stream = await fake_stream_response_for_assembly(request_data, trace=trace)

        return await _convert_openai_stream_to_anthropic(openai_stream, model)

    upstream_response = await send_assembly_request(request_data, False, trace=trace)
    response_status = getattr(upstream_response, "status_code", 200)
    if response_status >= 400:
        parsed_error = None
        try:
            raw = _extract_response_text(upstream_response)
            parsed_error = json.loads(raw)
        except Exception:
            parsed_error = None

        if trace:
            try:
                await tracker.end_trace(trace.trace_id)
            except Exception:
                pass

        message = "Upstream request failed"
        if isinstance(parsed_error, dict):
            if isinstance(parsed_error.get("error"), dict):
                message = str(parsed_error["error"].get("message") or message)
            else:
                message = str(parsed_error.get("message") or message)

        return JSONResponse(
            content=openai_error_to_anthropic_error(
                response_status, parsed_error, fallback_message=message
            ),
            status_code=response_status,
        )

    text = _extract_response_text(upstream_response)
    parsed = _parse_response_json(text, upstream_response)
    if isinstance(parsed, dict):
        upstream_shape = _summarize_upstream_response_shape(parsed)
        log.info(
            f"[ANTHROPIC_UPSTREAM_SHAPE] stream=0 model={model} "
            f"choices={upstream_shape.get('choices_count', 0)} "
            f"shape={json.dumps(upstream_shape.get('choice_shapes', []), ensure_ascii=False)}"
        )
        openai_response = assembly_response_to_openai(parsed, model)
    else:
        openai_response = _build_openai_fallback_text_response(model, text)

    non_stream_diag = _extract_openai_response_diagnostics(openai_response)
    log.info(
        f"[ANTHROPIC_DIAG] stream=0 model={model} finish_reason={non_stream_diag['finish_reason']} "
        f"tool_calls_count={non_stream_diag['tool_calls_count']}"
    )

    anthropic_response = openai_response_to_anthropic(openai_response, fallback_model=model)

    usage_metrics = _extract_usage_metrics(openai_response.get("usage", {}) if isinstance(openai_response, dict) else {})

    if trace:
        trace.mark("conversion_complete")
        trace.mark("first_chunk_sent")
        await tracker.end_trace(
            trace.trace_id,
            completion_tokens=usage_metrics["completion_tokens"],
            prompt_tokens=usage_metrics["prompt_tokens"],
            cached_tokens=usage_metrics["cached_tokens"],
            total_tokens=usage_metrics["total_tokens"],
        )

    return JSONResponse(content=anthropic_response)


@router.post("/v1/messages/count_tokens")
async def anthropic_count_tokens(
    request: Request,
):
    """Anthropic count_tokens compatibility endpoint."""
    try:
        await authenticate_anthropic_request(request)
    except HTTPException as e:
        return JSONResponse(
            content=openai_error_to_anthropic_error(
                e.status_code, {"message": str(e.detail)}, fallback_message=str(e.detail)
            ),
            status_code=e.status_code,
        )

    try:
        raw_data = await request.json()
    except Exception as e:
        return JSONResponse(
            content=openai_error_to_anthropic_error(
                400, {"message": f"Invalid JSON: {str(e)}"}, fallback_message="Invalid JSON"
            ),
            status_code=400,
        )

    try:
        openai_payload = convert_claude_request_to_openai(raw_data)
    except Exception as e:
        return JSONResponse(
            content=openai_error_to_anthropic_error(
                400, {"message": str(e)}, fallback_message="Invalid request"
            ),
            status_code=400,
        )

    messages = openai_payload.get("messages", [])
    input_tokens = estimate_input_tokens(messages)
    return JSONResponse(content={"input_tokens": input_tokens})

@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    token: str = Depends(authenticate)
):
    """处理OpenAI格式的聊天完成请求"""
    
    # 性能追踪：开始追踪
    trace_id = str(uuid.uuid4())
    tracker = await get_performance_tracker()
    trace = tracker.start_trace(trace_id, "pending")  # 模型名稍后更新
    
    # 标记认证完成
    trace.mark("auth_complete")
    
    # 获取原始请求数据
    try:
        raw_data = await request.json()
        # 记录请求中的所有参数（排除 messages 内容以减少日志量）
        params_to_log = {k: v for k, v in raw_data.items() if k != 'messages'}
        log.info(f"Request params: model={raw_data.get('model')}, stream={raw_data.get('stream')}, extra_keys={list(params_to_log.keys())}")
        log.debug(f"Full request params (excluding messages): {json.dumps(params_to_log, ensure_ascii=False)[:500]}...")
    except Exception as e:
        log.error(f"Failed to parse JSON request: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")
    
    # 创建请求对象
    try:
        request_data = ChatCompletionRequest(**raw_data)
        # 更新追踪的模型名称
        trace.model = request_data.model
        
        log.debug(f"Request validated - model: {request_data.model}, messages: {len(request_data.messages)}, stream: {getattr(request_data, 'stream', False)}")
        
        # 详细记录接收到的消息结构
        log.debug(f"Received messages structure:")
        for i, m in enumerate(request_data.messages):
            role = getattr(m, "role", "unknown")
            has_tool_calls = bool(getattr(m, "tool_calls", None))
            has_tool_call_id = bool(getattr(m, "tool_call_id", None))
            content_preview = str(getattr(m, "content", ""))[:50]
            log.debug(f"  [{i}] role={role}, tool_calls={has_tool_calls}, tool_call_id={has_tool_call_id}, content={content_preview}...")
    except Exception as e:
        log.error(f"Request validation failed: {e}")
        raise HTTPException(status_code=400, detail=f"Request validation error: {str(e)}")
    
    # 健康检查
    if (len(request_data.messages) == 1 and 
        getattr(request_data.messages[0], "role", None) == "user" and
        getattr(request_data.messages[0], "content", None) == "Hi"):
        return JSONResponse(content={
            "choices": [{"message": {"role": "assistant", "content": "amb2api正常工作中"}}]
        })
    
    # 限制max_tokens
    if getattr(request_data, "max_tokens", None) is not None and request_data.max_tokens > 65535:
        request_data.max_tokens = 65535
    
    # Max Tokens 自适应处理
    try:
        from ..storage.storage_adapter import get_storage_adapter
        from ..models.model_limits import get_model_max_tokens
        
        adapter = await get_storage_adapter()
        max_tokens_mode = await adapter.get_config("max_tokens_mode", "off")
        
        if max_tokens_mode != "off":
            model_max = await get_model_max_tokens(request_data.model)
            
            if max_tokens_mode == "high":
                target_max_tokens = model_max
            elif max_tokens_mode == "medium":
                target_max_tokens = model_max // 2
            else:  # low
                target_max_tokens = min(4096, model_max)
            
            original_max_tokens = getattr(request_data, "max_tokens", None)
            request_data.max_tokens = target_max_tokens
            log.info(f"Max tokens adaptive: mode={max_tokens_mode}, model_max={model_max}, original={original_max_tokens}, target={target_max_tokens}")
    except Exception as e:
        log.warning(f"Max tokens adaptive processing failed: {e}")
        
    # 覆写 top_k 为 64
    setattr(request_data, "top_k", 64)

    # 过滤空消息（但保留有 tool_calls 的消息和 assistant/tool 消息）
    filtered_messages = []
    for m in request_data.messages:
        content = getattr(m, "content", None)
        tool_calls = getattr(m, "tool_calls", None)
        role = getattr(m, "role", "unknown")
        
        # 如果有 tool_calls，即使 content 为空也保留
        if tool_calls:
            log.debug(f"Keeping message with tool_calls: role={role}, content={'[empty]' if not content else content[:50]+'...'}")
            filtered_messages.append(m)
            continue
        
        # 保留 assistant 和 tool 消息，即使 content 为空
        # 这对于多轮对话很重要
        if role in ["assistant", "tool"]:
            filtered_messages.append(m)
            continue
        
        # 对于其他角色，检查 content 是否有效
        if content:
            if isinstance(content, str) and content.strip():
                filtered_messages.append(m)
            elif isinstance(content, list) and len(content) > 0:
                has_valid_content = False
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text" and part.get("text", "").strip():
                            has_valid_content = True
                            break
                        elif part.get("type") == "image_url" and part.get("image_url", {}).get("url"):
                            has_valid_content = True
                            break
                if has_valid_content:
                    filtered_messages.append(m)
    
    request_data.messages = filtered_messages
    
    log.debug(f"After filtering: {len(request_data.messages)} messages")
    for i, m in enumerate(request_data.messages):
        role = getattr(m, "role", "unknown")
        has_tool_calls = bool(getattr(m, "tool_calls", None))
        content_preview = str(getattr(m, "content", ""))[:50]
        log.debug(f"  [{i}] role={role}, has_tool_calls={has_tool_calls}, content={content_preview}...")
    
    # AssemblyAI 支持完整的 OpenAI 协议，不需要重建消息
    
    # 优化消息历史，避免超出 token 限制
    from ..transform.message_optimizer import optimize_messages
    try:
        optimized_messages = optimize_messages(request_data.messages)
        request_data.messages = optimized_messages
        log.debug(f"Messages optimized: {len(filtered_messages)} -> {len(optimized_messages)}")
    except Exception as e:
        log.warning(f"Message optimization failed: {e}, using original messages")
    
    # 标记预处理完成
    trace.mark("preprocessing_complete")
    
    # 处理模型名称和功能检测
    model = request_data.model
    use_fake_streaming = is_fake_streaming_model(model)
    use_anti_truncation = is_anti_truncation_model(model)
    
    # AssemblyAI 直接使用传入模型名，无需特征前缀转换
    
    # 处理假流式
    if use_fake_streaming and getattr(request_data, "stream", False):
        request_data.stream = False
        return await fake_stream_response_for_assembly(request_data, trace=trace)
    
    # 处理抗截断 (仅流式传输时有效)
    is_streaming = getattr(request_data, "stream", False)
    if use_anti_truncation and is_streaming:
        log.warning("AssemblyAI 暂不支持原生流式抗截断，将作为普通请求处理")
        request_data.stream = False
        is_streaming = False
    
    # 发送到 AssemblyAI（非流式）
    is_streaming = getattr(request_data, "stream", False)
    if is_streaming:
        # 检查是否启用真实流式
        from config import get_enable_real_streaming
        enable_real_streaming = await get_enable_real_streaming()
        
        if enable_real_streaming:
            log.info("使用真实流式模式（实验性）")
            # 真实流式模式：首包前支持 bootstrap 重试，首包后不重试
            async def request_provider():
                return await send_assembly_request(request_data, True, trace=trace)

            response = await request_provider()
            return await convert_streaming_response(
                response,
                model,
                trace=trace,
                request_provider=request_provider,
            )
        else:
            log.info("使用假流式模式")
            return await fake_stream_response_for_assembly(request_data, trace=trace)
    
    log.info(f"REQ model={model}")
    log.debug(f"Sending request to AssemblyAI - stream: {is_streaming}, messages: {len(request_data.messages)}")
    
    response = await send_assembly_request(request_data, False, trace=trace)

    # 上游或网关已返回错误响应时，直接透传状态码和错误体
    response_status = getattr(response, "status_code", 200)
    if response_status >= 400:
        try:
            if hasattr(response, "body") and response.body:
                raw = response.body.decode("utf-8", errors="replace") if isinstance(response.body, bytes) else str(response.body)
                parsed_error = json.loads(raw)
            elif hasattr(response, "text"):
                parsed_error = response.json() if hasattr(response, "json") else json.loads(response.text or "{}")
            else:
                parsed_error = None
        except Exception:
            parsed_error = None

        if trace:
            try:
                await tracker.end_trace(trace.trace_id)
            except Exception:
                pass

        if isinstance(parsed_error, dict):
            return JSONResponse(content=parsed_error, status_code=response_status)
        return JSONResponse(
            content={"error": {"message": f"Upstream request failed with status {response_status}", "type": "api_error"}},
            status_code=response_status,
        )
    
    # 性能追踪：上游响应完成
    if trace:
        trace.mark("upstream_first_byte")
        trace.mark("upstream_response_complete")
    
    # 如果是流式响应，直接返回
    if is_streaming:
        log.debug(f"Converting to streaming response for model: {model}")
        return await convert_streaming_response(response, model, trace=trace)
    
    # 转换非流式响应（AssemblyAI → OpenAI）
    usage_metrics = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_tokens": 0,
        "total_tokens": 0,
    }
    try:
        try:
            if hasattr(response, 'text') and isinstance(getattr(response, 'text'), str):
                text = response.text
            elif hasattr(response, 'body'):
                body = response.body
                text = body.decode('utf-8', errors='replace') if isinstance(body, bytes) else str(body)
            elif hasattr(response, 'content'):
                content = response.content
                text = content.decode('utf-8', errors='replace') if isinstance(content, bytes) else str(content)
            else:
                text = str(response)
        except Exception as de:
            log.warning(f"Response decode failed: {de}")
            text = str(response)
        parsed = None
        try:
            parsed = json.loads(text.strip())
        except Exception:
            if 'data:' in text:
                lines = [l.strip() for l in text.splitlines() if l.strip()]
                for l in reversed(lines):
                    if not l.startswith('data:'):
                        continue
                    payload = l[5:].strip()
                    if payload == '[DONE]':
                        continue
                    try:
                        parsed = json.loads(payload)
                        break
                    except Exception:
                        pass
            if parsed is None and hasattr(response, 'json'):
                try:
                    parsed = response.json()
                except Exception:
                    parsed = None

        if isinstance(parsed, dict):
            # 检查是否是错误响应
            if 'code' in parsed and parsed.get('code') != 200:
                error_message = parsed.get('message', 'Unknown error')
                log.error(f"AssemblyAI returned error: {parsed.get('code')} - {error_message}")
                raise HTTPException(
                    status_code=parsed.get('code', 500),
                    detail=f"AssemblyAI error: {error_message}"
                )
            
            # 提取 token 数量
            usage_metrics = _extract_usage_metrics(parsed.get('usage', {}))
            
            # AssemblyAI 返回 OpenAI 格式，直接使用或进行微调
            openai_response = assembly_response_to_openai(parsed, model)
            converted_usage_metrics = _extract_usage_metrics(openai_response.get("usage", {}))
            if converted_usage_metrics["total_tokens"] > 0 or converted_usage_metrics["cached_tokens"] > 0:
                usage_metrics = converted_usage_metrics
        else:
            openai_response = {
                "id": str(uuid.uuid4()),
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": text.strip()},
                    "finish_reason": "stop"
                }]
            }
        
        # 性能追踪：格式转换完成
        if trace:
            trace.mark("conversion_complete")
            trace.mark("first_chunk_sent")
        
        # 如果最终choices为空，构造一个兜底消息避免前端空白
        try:
            if isinstance(openai_response, dict):
                ch = openai_response.get('choices')
                if isinstance(ch, list) and len(ch) == 0:
                    fallback_content = ''
                    if isinstance(parsed, dict):
                        fallback_content = str(parsed.get('output_text') or parsed.get('text') or '')
                    if not fallback_content:
                        fallback_content = text.strip()
                    openai_response['choices'] = [{
                        'index': 0,
                        'message': {'role': 'assistant', 'content': fallback_content},
                        'finish_reason': 'stop'
                    }]
        except Exception:
            pass

        log.info(f"RES model={model} status=OK")
        log.debug(f"RES Details - Converted response: {json.dumps(openai_response, ensure_ascii=False)[:1000]}...")
        
        # 性能追踪：响应完成
        if trace:
            await tracker.end_trace(
                trace.trace_id,
                completion_tokens=usage_metrics["completion_tokens"],
                prompt_tokens=usage_metrics["prompt_tokens"],
                cached_tokens=usage_metrics["cached_tokens"],
                total_tokens=usage_metrics["total_tokens"],
            )
        
        return JSONResponse(content=openai_response)
    except Exception as e:
        try:
            sample = (text[:200] + '...') if isinstance(text, str) and len(text) > 200 else text
            log.error(f"RES model={model} status=FAIL conversion_error sample={sample}")
            log.debug(f"RES Details - Conversion error: {str(e)}, Full text: {text[:500]}...")
        except Exception:
            log.error(f"RES model={model} status=FAIL conversion_error")
        raise HTTPException(status_code=500, detail="Response conversion failed")

"""
AssemblyAI Fake Stream Handler
Handles fake streaming simulation for AssemblyAI responses.
"""
import json
import ast
import time
import uuid
import asyncio
from typing import Optional, Callable, Awaitable, Any

from fastapi.responses import StreamingResponse, JSONResponse

from log import log
from src.models.models import ChatCompletionRequest
from src.core.task_manager import create_managed_task
from src.services.assembly_client import send_assembly_request
from src.transform.xml_parser import parse_xml_tool_calls
from src.transform.openai_transfer import gemini_stream_chunk_to_openai
from config import (
    get_config_value,
    get_stream_keepalive_seconds,
    get_stream_bootstrap_retries,
    get_tool_debug_logs_enabled,
)


def _summarize_upstream_response_shape(response_data: Any) -> dict:
    """Summarize non-sensitive upstream response shape for tool-call diagnostics."""
    summary: dict = {
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


async def convert_streaming_response(
    gemini_response,
    model: str,
    trace=None,
    request_provider: Optional[Callable[[], Awaitable[Any]]] = None,
) -> StreamingResponse:
    """转换流式响应为OpenAI格式（支持 keepalive 与首包前 bootstrap 重试）"""
    response_id = str(uuid.uuid4())

    keepalive_seconds = await get_stream_keepalive_seconds()
    bootstrap_retries = await get_stream_bootstrap_retries()
    if keepalive_seconds < 0:
        keepalive_seconds = 0
    if bootstrap_retries < 0:
        bootstrap_retries = 0

    if trace:
        trace.metadata["stream_mode"] = "real"
        trace.metadata["stream_keepalive_seconds"] = keepalive_seconds
        trace.metadata["stream_bootstrap_retries_config"] = bootstrap_retries
        trace.metadata["stream_keepalive_count"] = 0
        trace.metadata["stream_bootstrap_attempts"] = 0
        trace.metadata["stream_bootstrap_retries_used"] = 0
        trace.metadata["stream_bootstrap_recovered"] = False
        trace.metadata["stream_bootstrap_failed"] = False
        trace.metadata.pop("stream_bootstrap_last_error", None)
        trace.metadata.pop("stream_bootstrap_last_error_type", None)

    async def openai_stream_generator():
        nonlocal gemini_response
        first_chunk_sent = False
        prompt_tokens = 0
        completion_tokens = 0
        cached_tokens = 0
        total_tokens = 0

        def _safe_non_negative_int(value: Any, default: int = 0) -> int:
            try:
                parsed = int(value)
                return parsed if parsed >= 0 else default
            except (TypeError, ValueError):
                return default

        async def _iter_stream_chunks(stream_response):
            """统一抽象不同响应对象为 chunk 异步迭代器。"""
            if hasattr(stream_response, 'body_iterator'):
                async for chunk in stream_response.body_iterator:
                    yield chunk
                return
            raise TypeError(f"Unexpected response type: {type(stream_response)}")

        async def _convert_chunk(chunk) -> Optional[bytes]:
            """将上游 chunk 转换为 OpenAI SSE chunk。"""
            nonlocal prompt_tokens, completion_tokens, cached_tokens, total_tokens
            if not chunk:
                return None

            if isinstance(chunk, bytes):
                if not chunk.startswith(b'data: '):
                    return None
                payload = chunk[len(b'data: '):]
            else:
                chunk_str = str(chunk)
                if not chunk_str.startswith('data: '):
                    return None
                payload = chunk_str[len('data: '):].encode()

            payload_text = payload.decode('utf-8', errors='replace').strip()
            if payload_text == "[DONE]":
                return None

            gemini_chunk = json.loads(payload_text)
            openai_chunk = gemini_stream_chunk_to_openai(gemini_chunk, model, response_id)
            usage_raw = (
                (openai_chunk.get("usage") if isinstance(openai_chunk, dict) else None)
                or (gemini_chunk.get("usage") if isinstance(gemini_chunk, dict) else None)
                or {}
            )
            if isinstance(usage_raw, dict):
                prompt_tokens = _safe_non_negative_int(
                    usage_raw.get("prompt_tokens", usage_raw.get("input_tokens", prompt_tokens)),
                    prompt_tokens,
                )
                completion_tokens = _safe_non_negative_int(
                    usage_raw.get("completion_tokens", usage_raw.get("output_tokens", completion_tokens)),
                    completion_tokens,
                )
                cached_tokens = _safe_non_negative_int(
                    usage_raw.get("cached_tokens", usage_raw.get("input_cached_tokens", cached_tokens)),
                    cached_tokens,
                )
                candidate_total = _safe_non_negative_int(
                    usage_raw.get("total_tokens", prompt_tokens + completion_tokens),
                    prompt_tokens + completion_tokens,
                )
                total_tokens = max(total_tokens, candidate_total, prompt_tokens + completion_tokens)
            return f"data: {json.dumps(openai_chunk, separators=(',',':'))}\n\n".encode()

        total_attempts = bootstrap_retries + 1
        attempt = 0
        try:
            while attempt < total_attempts:
                attempt += 1
                if trace:
                    trace.metadata["stream_bootstrap_attempts"] = attempt
                try:
                    chunk_iter = _iter_stream_chunks(gemini_response)
                    pending_chunk_task = None
                    try:
                        while True:
                            try:
                                if pending_chunk_task is None:
                                    pending_chunk_task = asyncio.create_task(chunk_iter.__anext__())

                                if keepalive_seconds > 0:
                                    try:
                                        chunk = await asyncio.wait_for(
                                            asyncio.shield(pending_chunk_task),
                                            timeout=keepalive_seconds,
                                        )
                                        pending_chunk_task = None
                                    except asyncio.TimeoutError:
                                        # SSE 注释帧 keepalive，不影响客户端内容解析
                                        if trace:
                                            trace.metadata["stream_keepalive_count"] = int(
                                                trace.metadata.get("stream_keepalive_count", 0)
                                            ) + 1
                                        yield b": keepalive\n\n"
                                        continue
                                else:
                                    chunk = await pending_chunk_task
                                    pending_chunk_task = None
                            except asyncio.TimeoutError:
                                # 兜底分支（理论上不会触发）
                                yield b": keepalive\n\n"
                                continue
                            except StopAsyncIteration:
                                break

                            try:
                                converted = await _convert_chunk(chunk)
                            except json.JSONDecodeError:
                                continue

                            if not converted:
                                continue

                            if not first_chunk_sent:
                                first_chunk_sent = True
                                if trace:
                                    trace.mark("upstream_first_byte")
                                    trace.mark("first_chunk_sent")
                            yield converted
                    finally:
                        if pending_chunk_task is not None and not pending_chunk_task.done():
                            pending_chunk_task.cancel()

                    # 当前尝试正常结束
                    break
                except Exception as stream_err:
                    if not first_chunk_sent and request_provider and attempt < total_attempts:
                        if trace:
                            trace.metadata["stream_bootstrap_retries_used"] = attempt
                            trace.metadata["stream_bootstrap_last_error"] = str(stream_err)[:256]
                            trace.metadata["stream_bootstrap_last_error_type"] = type(stream_err).__name__
                        log.warning(f"[STREAM_BOOTSTRAP_RETRY] attempt={attempt}/{total_attempts} err={stream_err}")
                        gemini_response = await request_provider()
                        continue
                    raise

            if trace:
                trace.metadata["stream_bootstrap_retries_used"] = max(0, attempt - 1)
                trace.metadata["stream_bootstrap_recovered"] = (
                    trace.metadata["stream_bootstrap_retries_used"] > 0 and first_chunk_sent
                )

            if trace:
                trace.mark("upstream_response_complete")

            # 发送结束标记
            yield "data: [DONE]\n\n".encode()

        except Exception as e:
            if trace:
                trace.metadata["stream_bootstrap_retries_used"] = max(0, attempt - 1)
                trace.metadata["stream_bootstrap_failed"] = (
                    (not first_chunk_sent) and attempt >= total_attempts
                )
                trace.metadata["stream_bootstrap_last_error"] = str(e)[:256]
                trace.metadata["stream_bootstrap_last_error_type"] = type(e).__name__
            log.error(f"Stream conversion error: {e}")
            error_chunk = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": f"Stream error: {str(e)}"},
                    "finish_reason": "stop"
                }]
            }
            yield f"data: {json.dumps(error_chunk)}\n\n".encode()
            yield "data: [DONE]\n\n".encode()
        finally:
            trace_id = getattr(trace, "trace_id", "") if trace else ""
            if trace_id:
                from src.stats.performance_tracker import get_performance_tracker

                tracker = await get_performance_tracker()
                await tracker.end_trace(
                    trace_id,
                    completion_tokens=completion_tokens,
                    prompt_tokens=prompt_tokens,
                    cached_tokens=cached_tokens,
                    total_tokens=total_tokens,
                )

    return StreamingResponse(openai_stream_generator(), media_type="text/event-stream")

async def fake_stream_response_for_assembly(openai_request: ChatCompletionRequest, trace=None) -> StreamingResponse:
    """AssemblyAI 的假流式：周期心跳 + 最终内容块"""
    async def stream_generator():
        nonlocal trace
        completion_tokens = 0
        prompt_tokens = 0
        cached_tokens = 0
        total_tokens = 0
        tool_debug_logs_enabled = await get_tool_debug_logs_enabled()

        def _safe_non_negative_int(value: Any) -> int:
            try:
                parsed = int(value)
                return parsed if parsed >= 0 else 0
            except (TypeError, ValueError):
                return 0

        def _normalize_tool_arguments_for_openai(raw_args: Any) -> str:
            """Normalize tool arguments to OpenAI-compatible JSON string."""
            if raw_args is None:
                return "{}"

            if isinstance(raw_args, dict):
                return json.dumps(raw_args, ensure_ascii=False)

            if isinstance(raw_args, str):
                text = raw_args.strip()
                if not text:
                    return "{}"

                if text.startswith("```") and text.endswith("```"):
                    lines = text.splitlines()
                    if len(lines) >= 3:
                        text = "\n".join(lines[1:-1]).strip()

                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        return json.dumps(parsed, ensure_ascii=False)
                    return json.dumps({"value": parsed}, ensure_ascii=False)
                except Exception:
                    pass

                try:
                    parsed = ast.literal_eval(text)
                    if isinstance(parsed, dict):
                        return json.dumps(parsed, ensure_ascii=False)
                    return json.dumps({"value": parsed}, ensure_ascii=False)
                except Exception:
                    return json.dumps({"raw": raw_args}, ensure_ascii=False)

            return json.dumps({"value": raw_args}, ensure_ascii=False)

        def _iter_choice_tool_calls(choice: Any, msg: Any):
            """Yield tool call candidates from choice/message tool_calls and content.tool_use blocks."""
            if not isinstance(choice, dict) or not isinstance(msg, dict):
                return

            direct_choice_tool_calls = choice.get("tool_calls")
            if isinstance(direct_choice_tool_calls, list):
                for tc in direct_choice_tool_calls:
                    if isinstance(tc, dict):
                        yield tc

            direct_message_tool_calls = msg.get("tool_calls")
            if isinstance(direct_message_tool_calls, list):
                for tc in direct_message_tool_calls:
                    if isinstance(tc, dict):
                        yield tc

            content_blocks = msg.get("content")
            if isinstance(content_blocks, list):
                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    nested_tool_use = block.get("tool_use")
                    if block_type != "tool_use" and not isinstance(nested_tool_use, dict):
                        continue

                    nested = nested_tool_use if isinstance(nested_tool_use, dict) else {}
                    tool_id = block.get("id") or nested.get("id") or f"call_{uuid.uuid4().hex[:24]}"
                    tool_name = block.get("name") or nested.get("name") or "unknown_function"
                    tool_input = block.get("input") if "input" in block else nested.get("input")
                    if tool_input is None:
                        tool_input = {}
                    yield {
                        "id": tool_id,
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "input": tool_input,
                        },
                    }

        if trace:
            trace.metadata["stream_mode"] = "fake"
            trace.metadata["stream_keepalive_count"] = 0
            trace.metadata["stream_bootstrap_attempts"] = 1
            trace.metadata["stream_bootstrap_retries_used"] = 0
            trace.metadata["stream_bootstrap_recovered"] = False
            trace.metadata["stream_bootstrap_failed"] = False
            trace.metadata["fake_stream_heartbeat_count"] = 0
        
        try:
            log.debug(f"Starting fake stream for model: {openai_request.model}")
            
            # 发送心跳
            heartbeat = {
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": ""},
                    "finish_reason": None
                }]
            }
            yield f"data: {json.dumps(heartbeat)}\n\n".encode()
            log.debug("Sent initial heartbeat")
            
            # 异步发送实际请求
            async def get_response():
                return await send_assembly_request(openai_request, False, trace=trace)
            
            # 创建请求任务
            response_task = create_managed_task(get_response(), name="openai_fake_stream_request")
            
            try:
                # 每3秒发送一次心跳，直到收到响应
                heartbeat_count = 0
                while not response_task.done():
                    await asyncio.sleep(3.0)
                    if not response_task.done():
                        heartbeat_count += 1
                        if trace:
                            trace.metadata["fake_stream_heartbeat_count"] = heartbeat_count
                        yield f"data: {json.dumps(heartbeat)}\n\n".encode()
                        log.debug(f"Sent heartbeat #{heartbeat_count}")
                
                # 获取响应结果
                response = await response_task
                
                # 性能追踪：上游响应完成
                if trace:
                    trace.mark("upstream_first_byte")
                    trace.mark("upstream_response_complete")
                
                log.debug(f"Received response after {heartbeat_count} heartbeats")
                
            except asyncio.CancelledError:
                # 取消任务并传播取消
                response_task.cancel()
                try:
                    await response_task
                except asyncio.CancelledError:
                    pass
                raise
            except Exception as e:
                # 取消任务并处理其他异常
                response_task.cancel()
                try:
                    await response_task
                except asyncio.CancelledError:
                    pass
                log.error(f"Fake streaming request failed: {e}")
                raise
            
            # 发送实际请求
            # response 已在上面获取
            
            # 处理结果
            # JSONResponse 的内容存储在 body 属性中（bytes）
            if isinstance(response, JSONResponse):
                # JSONResponse 的 body 是 bytes，需要解码
                body_str = response.body.decode('utf-8') if isinstance(response.body, bytes) else str(response.body)
            elif hasattr(response, 'body'):
                body_str = response.body.decode('utf-8') if isinstance(response.body, bytes) else str(response.body)
            elif hasattr(response, 'content'):
                body_str = response.content.decode('utf-8') if isinstance(response.content, bytes) else str(response.content)
            elif hasattr(response, 'text'):
                body_str = response.text
            else:
                body_str = str(response)
            
            try:
                response_data = json.loads(body_str)
                log.debug(f"Parsed response data: {json.dumps(response_data, ensure_ascii=False)[:500]}...")
                upstream_shape = _summarize_upstream_response_shape(response_data)
                log.info(
                    f"[ANTHROPIC_UPSTREAM_SHAPE] stream=fake model={openai_request.model} "
                    f"choices={upstream_shape.get('choices_count', 0)} "
                    f"shape={json.dumps(upstream_shape.get('choice_shapes', []), ensure_ascii=False)}"
                )

                # 检查是否是错误响应（支持多种错误格式）
                error_message = None
                error_type = "error"
                error_code = ""
                
                # 格式1: {"error": {"message": "...", "type": "...", "code": "..."}}
                if "error" in response_data:
                    error_info = response_data["error"]
                    error_message = error_info.get("message", "Unknown error")
                    error_type = error_info.get("type", "error")
                    error_code = error_info.get("code", "")
                # 格式2: {"code": 400, "message": "..."} (AssemblyAI 格式)
                elif "code" in response_data and response_data.get("code") != 200:
                    error_message = response_data.get("message", "Unknown error")
                    error_code = response_data.get("code", "")
                    error_type = "api_error"
                
                if error_message:
                    log.warning(f"Error response in fake stream: {error_message} (type: {error_type}, code: {error_code})")
                    
                    # 构建用户友好的错误消息
                    user_message = error_message
                    if error_code == "no_available_keys":
                        user_message = "所有 API 密钥已被禁用，无法处理请求。请在管理面板中启用至少一个密钥。"
                    elif "processing error" in str(error_message).lower():
                        user_message = f"模型处理错误: {error_message}。请检查请求格式或尝试其他模型。"
                    elif error_type == "invalid_request_error":
                        user_message = f"请求错误: {error_message}"
                    elif error_type == "api_error":
                        user_message = f"API 错误: {error_message}"
                    
                    # 以流式格式返回错误信息（符合 OpenAI 格式）
                    error_chunk = {
                        "id": str(uuid.uuid4()),
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": openai_request.model,
                        "choices": [{
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "content": user_message
                            },
                            "finish_reason": "stop"
                        }]
                    }
                    yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n".encode()
                    yield b"data: [DONE]\n\n"
                    return

                # 从响应中提取内容和工具调用（适配 AssemblyAI 的多 choices 格式）
                all_content_parts = []
                all_tool_calls = []
                has_tool_use = False
                upstream_finish_reason = None
                seen_tool_signatures = set()
                
                if "choices" in response_data and response_data["choices"]:
                    for choice in response_data["choices"]:
                        msg = choice.get("message", {})
                        content = msg.get("content")
                        if content and isinstance(content, str) and content.strip():
                            all_content_parts.append(content)
                        elif isinstance(content, list):
                            for part in content:
                                if isinstance(part, dict):
                                    t = part.get("type")
                                    if t in ("text", "output_text") and part.get("text"):
                                        all_content_parts.append(part["text"])
                        
                        # 收集并修复工具调用
                        extracted_count = 0
                        for tc in _iter_choice_tool_calls(choice, msg):
                            extracted_count += 1
                            fixed_tc = {
                                "id": tc.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                                "type": tc.get("type", "function"),
                                "function": {}
                            }
                            func = tc.get("function", {})
                            fixed_tc["function"]["name"] = func.get("name", "")
                            args = func.get("arguments")
                            if args is None and "input" in func:
                                args = func.get("input")
                            if args is None and "input" in tc:
                                args = tc.get("input")
                            
                            # [修复] 参数名映射: 模型生成的 XML 可能使用错误的参数名
                            # 例如 read_file: file_path -> path
                            if fixed_tc["function"]["name"] == "read_file":
                                if isinstance(args, dict) and "file_path" in args:
                                    args["path"] = args.pop("file_path")
                                elif isinstance(args, str):
                                    try:
                                        args_dict = json.loads(args)
                                        if "file_path" in args_dict:
                                            args_dict["path"] = args_dict.pop("file_path")
                                            args = args_dict
                                    except json.JSONDecodeError:
                                        pass
                            
                            fixed_tc["function"]["arguments"] = _normalize_tool_arguments_for_openai(args)
                            signature = (
                                fixed_tc["id"],
                                fixed_tc["function"].get("name", ""),
                                fixed_tc["function"].get("arguments", "{}"),
                            )
                            if signature in seen_tool_signatures:
                                continue
                            seen_tool_signatures.add(signature)
                            all_tool_calls.append(fixed_tc)

                        if extracted_count > 0:
                            has_tool_use = True
                        
                        # 检查 finish_reason
                        fr = choice.get("finish_reason", "")
                        if fr in ("tool_use", "tool_calls"):
                            has_tool_use = True
                        if fr and not upstream_finish_reason:
                            upstream_finish_reason = fr
                
                content = " ".join(all_content_parts) if all_content_parts else ""
                
                # [XML Parser] 检查并解析 XML 工具调用
                if "<function_calls>" in content:
                    log.info(f"[XML Parser] Detected XML tool calls in content, parsing...")
                    content, xml_tool_calls = parse_xml_tool_calls(content)
                    if xml_tool_calls:
                        log.info(f"[XML Parser] Extracted {len(xml_tool_calls)} XML tool calls")
                        if not all_tool_calls:
                            all_tool_calls = []
                        all_tool_calls.extend(xml_tool_calls)
                        has_tool_use = True
                
                tool_calls = all_tool_calls if all_tool_calls else None
                reasoning_content = ""
  
                # 如果没有正常内容但有思维内容，给出警告
                if not content and reasoning_content:
                    log.warning("Fake stream response contains only thinking content")
                    content = "[模型正在思考中，请稍后再试或重新提问]"
                
                if tool_debug_logs_enabled:
                    log.debug(f"[TOOL_DEBUG] Extracted content length: {len(content)}, tool_calls count: {len(all_tool_calls)}")
                
                # 性能追踪：格式转换完成
                if trace:
                    trace.mark("conversion_complete")
                
                # 如果有内容或工具调用，都需要返回
                if content or tool_calls:
                    # 构建响应块，包括思维内容（如果有）和工具调用
                    
                    # 转换usageMetadata为OpenAI格式（兼容多种格式）
                    usage_raw = response_data.get("usage") or {}
                    prompt_tokens = _safe_non_negative_int(
                        usage_raw.get("prompt_tokens", usage_raw.get("input_tokens", 0))
                    )
                    completion_tokens = _safe_non_negative_int(
                        usage_raw.get("completion_tokens", usage_raw.get("output_tokens", 0))
                    )
                    cached_tokens = _safe_non_negative_int(
                        usage_raw.get("cached_tokens", usage_raw.get("input_cached_tokens", 0))
                    )
                    total_tokens = _safe_non_negative_int(
                        usage_raw.get("total_tokens", prompt_tokens + completion_tokens)
                    )
                    total_tokens = max(total_tokens, prompt_tokens + completion_tokens)
                    
                    usage = {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "cached_tokens": cached_tokens,
                        "total_tokens": total_tokens,
                        # 添加详细的 token 信息以支持 LobeChat 等客户端显示
                        "prompt_tokens_details": {
                            "cached_tokens": cached_tokens,
                            "audio_tokens": 0
                        },
                        "completion_tokens_details": {
                            "reasoning_tokens": 0,
                            "audio_tokens": 0,
                            "accepted_prediction_tokens": 0,
                            "rejected_prediction_tokens": 0
                        }
                    } if usage_raw else None
                    
                    # 确定 finish_reason（保留上游的 length/max_tokens）
                    if has_tool_use:
                        finish_reason = "tool_calls"
                    elif upstream_finish_reason in ("length", "max_tokens"):
                        finish_reason = upstream_finish_reason
                    else:
                        finish_reason = "stop"
                    
                    # 检查是否启用全局假流式渐进输出
                    try:
                        fake_stream_enabled = await get_config_value("fake_stream_enabled", False)
                        fake_stream_speed = await get_config_value("fake_stream_speed", 100)
                        if fake_stream_speed is None:
                            fake_stream_speed = 100
                        else:
                            fake_stream_speed = int(fake_stream_speed)
                    except Exception:
                        fake_stream_enabled = False
                        fake_stream_speed = 100
                    
                    if fake_stream_enabled and content and not tool_calls:
                        # 渐进式流式输出：按速度逐块返回内容
                        log.debug(f"Fake stream progressive output: speed={fake_stream_speed} chars/s, content_len={len(content)}")
                        
                        # 计算每块大小和间隔
                        # 例如: 100 chars/s, 每 50ms 输出 5 个字符
                        interval_ms = 50  # 每 50ms 输出一次
                        chars_per_chunk = max(1, int(fake_stream_speed * interval_ms / 1000))
                        
                        response_id = str(uuid.uuid4())
                        
                        # 逐块输出内容（所有 chunk 的 finish_reason 都为 null）
                        for i in range(0, len(content), chars_per_chunk):
                            chunk_content = content[i:i + chars_per_chunk]
                            is_last_content_chunk = (i + chars_per_chunk >= len(content))
                            
                            chunk_delta = {"role": "assistant", "content": chunk_content}
                            
                            # 最后一块内容添加 reasoning_content（如果有）
                            if is_last_content_chunk and reasoning_content:
                                chunk_delta["reasoning_content"] = reasoning_content
                            
                            content_chunk = {
                                "id": response_id,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": openai_request.model,
                                "choices": [{
                                    "index": 0,
                                    "delta": chunk_delta,
                                    "finish_reason": None  # 内容 chunk 不设置 finish_reason
                                }]
                            }
                            
                            yield f"data: {json.dumps(content_chunk)}\n\n".encode()
                            
                            # 性能追踪：首块发送
                            if i == 0 and trace:
                                trace.mark("first_chunk_sent")
                            
                            # 等待间隔（非最后一块）
                            if not is_last_content_chunk:
                                await asyncio.sleep(interval_ms / 1000)
                        
                        # 发送单独的结束 chunk（包含 finish_reason 和 usage）
                        finish_chunk = {
                            "id": response_id,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": openai_request.model,
                            "choices": [{
                                "index": 0,
                                "delta": {},  # 空 delta
                                "finish_reason": finish_reason
                            }]
                        }
                        if usage:
                            finish_chunk["usage"] = usage
                        yield f"data: {json.dumps(finish_chunk)}\n\n".encode()
                        yield b"data: [DONE]\n\n"
                    else:
                        # 一次性输出（原逻辑，但分离结束 chunk）
                        response_id = str(uuid.uuid4())
                        delta = {"role": "assistant"}
                        
                        # 添加 content（如果有）
                        if content:
                            delta["content"] = content
                        
                        # 添加 reasoning_content（如果有）
                        if reasoning_content:
                            delta["reasoning_content"] = reasoning_content
                        
                        # 添加 tool_calls（如果有），确保每个 tool_call 带 index
                        if tool_calls:
                            for i, tc in enumerate(tool_calls):
                                tc["index"] = i
                            delta["tool_calls"] = tool_calls

                        # 发送内容 chunk（finish_reason 为 null）
                        content_chunk = {
                            "id": response_id,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": openai_request.model,
                            "choices": [{
                                "index": 0,
                                "delta": delta,
                                "finish_reason": None
                            }]
                        }
                        yield f"data: {json.dumps(content_chunk)}\n\n".encode()
                        
                        # 性能追踪：首块发送
                        if trace:
                            trace.mark("first_chunk_sent")

                        # 发送单独的结束 chunk（包含 finish_reason 和 usage）
                        finish_chunk = {
                            "id": response_id,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": openai_request.model,
                            "choices": [{
                                "index": 0,
                                "delta": {},
                                "finish_reason": finish_reason
                            }]
                        }
                        if usage:
                            finish_chunk["usage"] = usage
                        yield f"data: {json.dumps(finish_chunk)}\n\n".encode()
                        yield b"data: [DONE]\n\n"
                else:
                    log.warning(f"No content found in response: {response_data}")
                    # 如果完全没有内容，提供默认回复
                    error_chunk = {
                        "id": str(uuid.uuid4()),
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": "amb2api-streaming",
                        "choices": [{
                            "index": 0,
                            "delta": {"role": "assistant", "content": "[响应为空，请重新尝试]"},
                            "finish_reason": "stop"
                        }]
                    }
                    yield f"data: {json.dumps(error_chunk)}\n\n".encode()
            except json.JSONDecodeError:
                log.error(f"Failed to decode response as JSON: {body_str[:100]}...")
                # 尝试直接返回文本
                error_chunk = {
                    "id": str(uuid.uuid4()),
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": openai_request.model,
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": body_str},
                        "finish_reason": "stop"
                    }]
                }
                yield f"data: {json.dumps(error_chunk)}\n\n".encode()
                
        except Exception as e:
            log.error(f"Fake stream generator error: {e}")
            error_chunk = {
                "id": str(uuid.uuid4()),
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": openai_request.model,
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": f"[System Error] Stream processing failed: {str(e)}"},
                    "finish_reason": "stop"
                }]
            }
            yield f"data: {json.dumps(error_chunk)}\n\n".encode()
            yield b"data: [DONE]\n\n"
        finally:
            trace_id = getattr(trace, "trace_id", "") if trace else ""
            if trace_id:
                from src.stats.performance_tracker import get_performance_tracker
                tracker = await get_performance_tracker()
                await tracker.end_trace(
                    trace_id,
                    completion_tokens=completion_tokens,
                    prompt_tokens=prompt_tokens,
                    cached_tokens=cached_tokens,
                    total_tokens=total_tokens,
                )

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream"
    )

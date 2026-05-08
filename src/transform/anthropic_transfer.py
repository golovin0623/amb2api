"""
Anthropic-compatible transfer module.

Converts between Anthropic Messages API format and OpenAI Chat Completions format.
Handles request mapping, response mapping, and SSE event conversion.
"""
import ast
import json
import uuid
from typing import Any, Dict, List, Optional


# ── Finish Reason Mapping ──

_FINISH_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "max_tokens": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}


def _map_finish_reason(fr: Any) -> Optional[str]:
    if isinstance(fr, str):
        return _FINISH_REASON_MAP.get(fr, "end_turn")
    return None


# ── Task Tool Normalization ──

_TASK_ALLOWED_KEYS = {"description", "prompt", "subagent_type", "run_in_background", "resume"}


def _normalize_task_input(raw: Any) -> Dict[str, Any]:
    """Normalize Task tool arguments to standard schema."""
    if isinstance(raw, dict):
        args: Dict[str, Any] = dict(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        parsed: Any = None
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            pass
        if parsed is None:
            try:
                parsed = ast.literal_eval(text)
            except Exception:
                pass
        if isinstance(parsed, dict):
            args = parsed
        else:
            desc = text if text else "Task"
            return {
                "description": desc,
                "prompt": desc,
                "subagent_type": "general-purpose",
            }
    else:
        args = {}

    # Map "task" alias to prompt + description
    if "task" in args:
        task_val = args.pop("task")
        if "prompt" not in args:
            args["prompt"] = task_val
        if "description" not in args:
            args["description"] = task_val

    # Map "subagentType" camelCase to snake_case (capitalize value)
    if "subagentType" in args:
        val = args.pop("subagentType")
        if "subagent_type" not in args:
            args["subagent_type"] = val.capitalize() if isinstance(val, str) else val

    # Cross-fill prompt/description
    if "description" in args and "prompt" not in args:
        args["prompt"] = args["description"]
    elif "prompt" in args and "description" not in args:
        args["description"] = args["prompt"]

    # Defaults
    if "subagent_type" not in args:
        args["subagent_type"] = "general-purpose"
    if not args.get("description"):
        args["description"] = "Task"
    if not args.get("prompt"):
        args["prompt"] = args["description"]

    return {k: v for k, v in args.items() if k in _TASK_ALLOWED_KEYS}


# ── Tool Argument Parsing ──

def _parse_tool_args(raw: Any) -> Dict[str, Any]:
    """Parse tool arguments (JSON string, Python literal, or dict) to dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


# ── Request Conversion ──

def anthropic_request_to_openai_payload(request: Dict[str, Any]) -> Dict[str, Any]:
    """Convert Anthropic Messages API request to OpenAI Chat Completions payload."""
    messages = _convert_messages(
        request.get("messages", []),
        request.get("system"),
    )

    payload: Dict[str, Any] = {
        "model": request.get("model", ""),
        "messages": messages,
        "stream": bool(request.get("stream", False)),
    }

    max_tokens = request.get("max_tokens")
    if max_tokens is not None:
        payload["max_tokens"] = int(max_tokens)

    for key in ("temperature", "top_p", "top_k"):
        val = request.get(key)
        if val is not None:
            payload[key] = val

    if request.get("stop_sequences"):
        payload["stop"] = request["stop_sequences"]

    tools = _convert_tools(request.get("tools"))
    if tools:
        payload["tools"] = tools

    tc = _convert_tool_choice(request.get("tool_choice"))
    if tc is not None:
        payload["tool_choice"] = tc

    # Prompt caching pass-through (AssemblyAI Gateway-level fields).
    for key in ("cache_control", "prompt_cache_retention", "prompt_cache_key"):
        val = request.get(key)
        if val is not None:
            payload[key] = val

    return payload


def _last_block_cache_control(blocks: Any) -> Optional[Dict[str, Any]]:
    """Return cache_control from the last block that declares it."""
    if not isinstance(blocks, list):
        return None
    last = None
    for block in blocks:
        if isinstance(block, dict) and isinstance(block.get("cache_control"), dict):
            last = block["cache_control"]
    return last


def _convert_messages(
    messages: List[Dict[str, Any]], system: Any
) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []

    sys_msg = _build_system_message(system)
    if sys_msg is not None:
        result.append(sys_msg)

    for msg in messages:
        role = msg.get("role", "")
        if role == "user":
            if _is_tool_result_message(msg):
                result.extend(_convert_tool_results(msg))
            else:
                result.append(_convert_user_message(msg))
        elif role == "assistant":
            result.append(_convert_assistant_message(msg))

    return result


def _build_system_message(system: Any) -> Optional[Dict[str, Any]]:
    """Build a system message and lift cache_control from the last block if present."""
    if not system:
        return None
    if isinstance(system, str):
        text = system.strip()
        return {"role": "system", "content": text} if text else None
    if isinstance(system, list):
        parts: List[str] = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        text = "\n\n".join(parts).strip()
        if not text:
            return None
        msg: Dict[str, Any] = {"role": "system", "content": text}
        cc = _last_block_cache_control(system)
        if cc is not None:
            msg["cache_control"] = cc
        return msg
    text = str(system).strip()
    return {"role": "system", "content": text} if text else None


def _extract_system_text(system: Any) -> str:
    """Compat shim — keep for any external callers."""
    msg = _build_system_message(system)
    if msg is None:
        return ""
    content = msg.get("content")
    return content if isinstance(content, str) else ""


def _convert_user_message(msg: Dict[str, Any]) -> Dict[str, Any]:
    content = msg.get("content")
    if content is None:
        return {"role": "user", "content": ""}
    if isinstance(content, str):
        out = {"role": "user", "content": content}
        cc = msg.get("cache_control")
        if isinstance(cc, dict):
            out["cache_control"] = cc
        return out
    parts: List[Dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype == "text":
            parts.append({"type": "text", "text": block.get("text", "")})
    if len(parts) == 1 and parts[0].get("type") == "text":
        out = {"role": "user", "content": parts[0]["text"]}
    else:
        out = {"role": "user", "content": parts or ""}
    cache_control = msg.get("cache_control") or _last_block_cache_control(content)
    if isinstance(cache_control, dict):
        out["cache_control"] = cache_control
    return out


def _convert_assistant_message(msg: Dict[str, Any]) -> Dict[str, Any]:
    content = msg.get("content")
    if content is None:
        return {"role": "assistant", "content": None}
    if isinstance(content, str):
        out = {"role": "assistant", "content": content}
        cc = msg.get("cache_control")
        if isinstance(cc, dict):
            out["cache_control"] = cc
        return out
    text_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                },
            })
    result: Dict[str, Any] = {"role": "assistant"}
    result["content"] = "".join(text_parts) if text_parts else None
    if tool_calls:
        result["tool_calls"] = tool_calls
    cache_control = msg.get("cache_control") or _last_block_cache_control(content)
    if isinstance(cache_control, dict):
        result["cache_control"] = cache_control
    return result


def _is_tool_result_message(msg: Dict[str, Any]) -> bool:
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "tool_result"
        for b in content
    )


def _convert_tool_results(msg: Dict[str, Any]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    content = msg.get("content", [])
    if not isinstance(content, list):
        return results
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        item = {
            "role": "tool",
            "tool_call_id": block.get("tool_use_id", ""),
            "content": _flatten_content(block.get("content")),
        }
        cc = block.get("cache_control")
        if isinstance(cc, dict):
            item["cache_control"] = cc
        results.append(item)
    return results


def _flatten_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts).strip()
    return str(content)


def _convert_tools(tools: Any) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(tools, list) or not tools:
        return None
    result = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name", "")
        if not name:
            continue
        result.append({
            "type": "function",
            "function": {
                "name": name,
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {}),
            },
        })
    return result or None


def _convert_tool_choice(tc: Any) -> Any:
    if tc is None:
        return None
    if not isinstance(tc, dict):
        return None
    ctype = tc.get("type", "")
    if ctype in ("auto", "any"):
        return "auto"
    if ctype == "none":
        return "none"
    if ctype == "tool" and tc.get("name"):
        return {"type": "function", "function": {"name": tc["name"]}}
    return "auto"


# ── Non-Stream Response Conversion ──

def openai_response_to_anthropic_message(
    openai_resp: Dict[str, Any], fallback_model: Optional[str] = None
) -> Dict[str, Any]:
    """Convert OpenAI non-stream response to Anthropic message."""
    choices = openai_resp.get("choices") or []
    first = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = first.get("message") or {}

    content_blocks = _build_content_blocks(message)
    if not content_blocks:
        content_blocks = [{"type": "text", "text": ""}]

    usage = openai_resp.get("usage") or {}
    model = openai_resp.get("model") or fallback_model or ""
    resp_id = openai_resp.get("id") or f"msg_{uuid.uuid4().hex}"

    return {
        "id": resp_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": _map_finish_reason(first.get("finish_reason")),
        "stop_sequence": None,
        "usage": _build_anthropic_usage(usage),
    }


def _build_anthropic_usage(usage: Any) -> Dict[str, Any]:
    """Build Anthropic-shape usage, surfacing cache_read/cache_creation fields."""
    if not isinstance(usage, dict):
        usage = {}

    def _safe_int(value: Any) -> int:
        try:
            parsed = int(value)
            return parsed if parsed >= 0 else 0
        except (TypeError, ValueError):
            return 0

    input_tokens = _safe_int(usage.get("prompt_tokens", usage.get("input_tokens", 0)))
    output_tokens = _safe_int(usage.get("completion_tokens", usage.get("output_tokens", 0)))

    prompt_details = usage.get("prompt_tokens_details")
    if not isinstance(prompt_details, dict):
        prompt_details = {}
    cached_tokens = _safe_int(
        usage.get("cached_tokens", prompt_details.get("cached_tokens", 0))
    )

    cache_creation = prompt_details.get("cache_creation")
    if not isinstance(cache_creation, dict):
        cache_creation = {}
    creation_5m = _safe_int(cache_creation.get("ephemeral_5m_input_tokens", 0))
    creation_1h = _safe_int(cache_creation.get("ephemeral_1h_input_tokens", 0))
    creation_total = creation_5m + creation_1h

    out: Dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if cached_tokens:
        out["cache_read_input_tokens"] = cached_tokens
    if creation_total:
        out["cache_creation_input_tokens"] = creation_total
        out["cache_creation"] = {
            "ephemeral_5m_input_tokens": creation_5m,
            "ephemeral_1h_input_tokens": creation_1h,
        }
    return out


def _build_content_blocks(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []

    content = message.get("content")
    if isinstance(content, str) and content:
        blocks.append({"type": "text", "text": content})

    tool_calls = message.get("tool_calls") or []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        func = tc.get("function") or {}
        tool_id = tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}"
        tool_name = func.get("name", "")

        # Prefer standard "arguments" key; fall back to "input" for non-standard
        # implementations (e.g. AssemblyAI gateway) that use "input" instead.
        raw_args = func.get("arguments")
        if raw_args is None:
            raw_args = func.get("input")

        if tool_name == "Task":
            tool_input = _normalize_task_input(raw_args)
        else:
            tool_input = _parse_tool_args(raw_args if raw_args is not None else "{}")

        blocks.append({
            "type": "tool_use",
            "id": tool_id,
            "name": tool_name,
            "input": tool_input,
        })

    return blocks


# ── SSE Conversion ──

def convert_openai_sse_payload_to_anthropic_events(
    payload_text: str,
    state: Dict[str, Any],
    default_model: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Convert raw OpenAI SSE payload text to Anthropic events."""
    clean = payload_text.strip()
    if not clean:
        return []
    if clean == "[DONE]":
        events: List[Dict[str, Any]] = []
        if state.get("text_started"):
            events.append({"type": "content_block_stop", "index": 0})
            state["text_started"] = False
        if state.get("message_started") and not state.get("message_stopped"):
            events.append({"type": "message_stop"})
            state["message_stopped"] = True
        return events
    try:
        chunk = json.loads(clean)
    except Exception:
        return []
    if not isinstance(chunk, dict):
        return []
    return _openai_chunk_to_anthropic_events(chunk, state=state, default_model=default_model)


def _openai_chunk_to_anthropic_events(
    chunk: Dict[str, Any],
    state: Dict[str, Any],
    default_model: Optional[str] = None,
) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []

    model = chunk.get("model") or default_model or state.get("model", "")
    state["model"] = model
    msg_id = chunk.get("id") or state.get("message_id") or f"msg_{uuid.uuid4().hex}"
    state["message_id"] = msg_id

    if not state.get("message_started"):
        events.append({
            "type": "message_start",
            "message": {
                "id": msg_id, "type": "message", "role": "assistant",
                "model": model, "content": [],
                "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })
        state["message_started"] = True

    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return events
    first = choices[0]
    if not isinstance(first, dict):
        return events

    delta = first.get("delta") or {}

    text = delta.get("content")
    if isinstance(text, str) and text:
        if not state.get("text_started"):
            events.append({
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            })
            state["text_started"] = True
        events.append({
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        })

    tc_deltas = delta.get("tool_calls")
    if isinstance(tc_deltas, list):
        if "tool_calls" not in state:
            state["tool_calls"] = {}
        for tcd in tc_deltas:
            if not isinstance(tcd, dict):
                continue
            events.extend(_process_tool_call_delta(tcd, state))

    finish_reason = first.get("finish_reason")
    if finish_reason is not None:
        events.extend(_build_finish_events(chunk, state, finish_reason))

    return events


def _process_tool_call_delta(
    tcd: Dict[str, Any], state: Dict[str, Any]
) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    tc_index = tcd.get("index", 0)
    tc_map = state["tool_calls"]

    if tc_index not in tc_map:
        tc_map[tc_index] = {
            "id": None, "name": None,
            "args_buf": "", "started": False,
            "claude_index": None,
        }

    tc = tc_map[tc_index]

    if tcd.get("id"):
        tc["id"] = tcd["id"]

    func = tcd.get("function") or {}
    if func.get("name"):
        tc["name"] = func["name"]

    # Support "input" dict key as well as incremental "arguments" string.
    # Some upstream implementations (e.g. AssemblyAI gateway) use "input"
    # instead of the standard OpenAI "arguments" field.
    raw_input = func.get("input")

    if tc["id"] and tc["name"] and not tc["started"]:
        if state.get("text_started"):
            events.append({"type": "content_block_stop", "index": 0})
            state["text_started"] = False

        idx = 1 + tc_index
        tc["claude_index"] = idx
        tc["started"] = True

        if tc["name"] == "Task" and raw_input is not None:
            tool_input: Dict[str, Any] = _normalize_task_input(raw_input)
        else:
            tool_input = {}

        events.append({
            "type": "content_block_start",
            "index": idx,
            "content_block": {
                "type": "tool_use", "id": tc["id"],
                "name": tc["name"], "input": tool_input,
            },
        })

    args_chunk = func.get("arguments")
    if args_chunk and tc["started"] and tc["claude_index"] is not None:
        tc["args_buf"] += args_chunk
        events.append({
            "type": "content_block_delta",
            "index": tc["claude_index"],
            "delta": {"type": "input_json_delta", "partial_json": args_chunk},
        })

    return events


def _build_finish_events(
    chunk: Dict[str, Any], state: Dict[str, Any], finish_reason: str
) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []

    if state.get("text_started"):
        events.append({"type": "content_block_stop", "index": 0})
        state["text_started"] = False

    for tc in (state.get("tool_calls") or {}).values():
        if tc.get("started") and tc.get("claude_index") is not None:
            events.append({"type": "content_block_stop", "index": tc["claude_index"]})

    delta_usage = _build_anthropic_usage(chunk.get("usage"))
    delta_usage.pop("input_tokens", None)

    events.append({
        "type": "message_delta",
        "delta": {
            "stop_reason": _map_finish_reason(finish_reason),
            "stop_sequence": None,
        },
        "usage": delta_usage,
    })
    events.append({"type": "message_stop"})
    state["message_stopped"] = True

    return events

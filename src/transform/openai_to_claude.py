"""
OpenAI-to-Claude response converter.
Converts OpenAI Chat Completions responses to Anthropic Messages API format.
Handles both non-stream and streaming (SSE) responses.
"""
import json
import uuid
from typing import Any, Dict, List, Optional
from log import log


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


# ── Tool Arguments Parsing ──

def _parse_tool_arguments(raw: Any) -> Dict[str, Any]:
    """Parse OpenAI tool call arguments (JSON string or dict) to dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
        return {"raw_arguments": raw}
    return {}


# ── Non-Stream Response ──

def openai_response_to_anthropic(
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


def _build_anthropic_usage(usage: Any) -> Dict[str, int]:
    """Build Anthropic-shape usage from OpenAI-shape usage, surfacing cache fields."""
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

    # text content
    content = message.get("content")
    if isinstance(content, str) and content:
        blocks.append({"type": "text", "text": content})

    # tool calls
    tool_calls = message.get("tool_calls") or []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        func = tc.get("function") or {}
        tool_id = tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}"
        blocks.append({
            "type": "tool_use",
            "id": tool_id,
            "name": func.get("name", ""),
            "input": _parse_tool_arguments(func.get("arguments", "{}")),
        })

    return blocks


# ── Streaming: Chunk → Anthropic Events ──

def openai_chunk_to_anthropic_events(
    chunk: Dict[str, Any],
    state: Dict[str, Any],
    default_model: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Convert one OpenAI stream chunk to list of Anthropic SSE events."""
    events: List[Dict[str, Any]] = []

    model = chunk.get("model") or default_model or state.get("model", "")
    state["model"] = model
    msg_id = chunk.get("id") or state.get("message_id") or f"msg_{uuid.uuid4().hex}"
    state["message_id"] = msg_id

    # message_start (once)
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

    # ── text delta ──
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

    # ── tool call deltas (incremental JSON) ──
    tc_deltas = delta.get("tool_calls")
    if isinstance(tc_deltas, list):
        if "tool_calls" not in state:
            state["tool_calls"] = {}
        for tcd in tc_deltas:
            if not isinstance(tcd, dict):
                continue
            events.extend(_process_tool_call_delta(tcd, state))

    # ── finish ──
    finish_reason = first.get("finish_reason")
    if finish_reason is not None:
        events.extend(_build_finish_events(chunk, state, finish_reason))

    return events


def _process_tool_call_delta(
    tcd: Dict[str, Any], state: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Process one incremental tool_call delta, return Anthropic events."""
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

    # start content_block when we have id + name
    if tc["id"] and tc["name"] and not tc["started"]:
        # close text block first if open
        if state.get("text_started"):
            events.append({"type": "content_block_stop", "index": 0})
            state["text_started"] = False

        idx = 1 + tc_index  # text is index 0
        tc["claude_index"] = idx
        tc["started"] = True
        events.append({
            "type": "content_block_start",
            "index": idx,
            "content_block": {
                "type": "tool_use", "id": tc["id"],
                "name": tc["name"], "input": {},
            },
        })

    # accumulate arguments and send incremental partial_json
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
    """Build content_block_stop + message_delta + message_stop events."""
    events: List[Dict[str, Any]] = []

    # close text block
    if state.get("text_started"):
        events.append({"type": "content_block_stop", "index": 0})
        state["text_started"] = False

    # close tool blocks
    for tc in (state.get("tool_calls") or {}).values():
        if tc.get("started") and tc.get("claude_index") is not None:
            events.append({"type": "content_block_stop", "index": tc["claude_index"]})

    delta_usage = _build_anthropic_usage(chunk.get("usage"))
    # Streaming usage delta only carries output and cache fields; drop input duplicate.
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


# ── SSE Payload → Events ──

def convert_openai_sse_to_anthropic_events(
    payload_text: str, state: Dict[str, Any], default_model: Optional[str] = None
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
    return openai_chunk_to_anthropic_events(chunk, state=state, default_model=default_model)


# ── SSE Serialization ──

def anthropic_events_to_sse_bytes(events: List[Dict[str, Any]]) -> bytes:
    """Encode Anthropic events to SSE wire format."""
    parts: List[str] = []
    for ev in events:
        etype = ev.get("type")
        if not isinstance(etype, str):
            continue
        data = json.dumps(ev, ensure_ascii=False, separators=(",", ":"))
        parts.append(f"event: {etype}\ndata: {data}\n\n")
    return "".join(parts).encode("utf-8")


# ── Error Conversion ──

def openai_error_to_anthropic_error(
    status_code: int,
    parsed_error: Any = None,
    fallback_message: str = "Upstream request failed",
) -> Dict[str, Any]:
    """Convert error to Anthropic error format."""
    if status_code in (401, 403):
        etype = "authentication_error"
    elif status_code == 429:
        etype = "rate_limit_error"
    elif status_code == 400:
        etype = "invalid_request_error"
    else:
        etype = "api_error"

    message = fallback_message
    if isinstance(parsed_error, dict):
        err = parsed_error.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or message)
        elif parsed_error.get("message") is not None:
            message = str(parsed_error["message"])

    return {"type": "error", "error": {"type": etype, "message": message}}


# ── Token Estimation ──

def estimate_input_tokens(messages: Any) -> int:
    """Rough token estimation for count_tokens endpoint."""
    if not isinstance(messages, list):
        return 0
    total = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += len(block.get("text", ""))
    return max(1, total // 4)


# ── Model List Conversion ──

def openai_models_to_anthropic(models: List[str]) -> Dict[str, Any]:
    """Convert model ID list to Anthropic /v1/models format."""
    return {
        "data": [
            {"id": m, "type": "model", "display_name": m, "created_at": "2024-01-01T00:00:00Z"}
            for m in models
        ],
        "has_more": False,
        "first_id": models[0] if models else None,
        "last_id": models[-1] if models else None,
    }

"""
Claude-to-OpenAI request converter.
Converts Anthropic Messages API requests to OpenAI Chat Completions format.
Based on claude-code-proxy with amb2api adaptations.
"""
import json
from typing import Dict, Any, List, Optional
from log import log


def convert_claude_request_to_openai(claude_request: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a raw Anthropic Messages API request dict to OpenAI payload."""
    messages = _convert_messages(
        claude_request.get("messages", []),
        claude_request.get("system"),
    )

    openai_req: Dict[str, Any] = {
        "model": claude_request.get("model", ""),
        "messages": messages,
        "stream": bool(claude_request.get("stream", False)),
    }

    # max_tokens
    max_tokens = claude_request.get("max_tokens")
    if max_tokens is not None:
        openai_req["max_tokens"] = int(max_tokens)

    # optional params
    for key in ("temperature", "top_p", "top_k"):
        val = claude_request.get(key)
        if val is not None:
            openai_req[key] = val

    if claude_request.get("stop_sequences"):
        openai_req["stop"] = claude_request["stop_sequences"]

    # tools
    tools = _convert_tools(claude_request.get("tools"))
    if tools:
        openai_req["tools"] = tools

    # tool_choice
    tc = _convert_tool_choice(claude_request.get("tool_choice"))
    if tc is not None:
        openai_req["tool_choice"] = tc

    # Prompt caching + region routing pass-through (Gateway-level fields).
    for key in ("cache_control", "prompt_cache_retention", "prompt_cache_key", "model_region"):
        val = claude_request.get(key)
        if val is not None:
            openai_req[key] = val

    return openai_req


def _last_block_cache_control(blocks: Any) -> Optional[Dict[str, Any]]:
    """Return cache_control from the last block that declares it (Anthropic semantic)."""
    if not isinstance(blocks, list):
        return None
    last = None
    for block in blocks:
        if isinstance(block, dict) and isinstance(block.get("cache_control"), dict):
            last = block["cache_control"]
    return last


# ── Messages ──

def _convert_messages(
    messages: List[Dict[str, Any]],
    system: Any,
) -> List[Dict[str, Any]]:
    """Convert Anthropic messages array to OpenAI messages."""
    result: List[Dict[str, Any]] = []

    # system prompt
    sys_msg = _build_system_message(system)
    if sys_msg is not None:
        result.append(sys_msg)

    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "")

        if role == "user":
            result.append(_convert_user_message(msg))
        elif role == "assistant":
            result.append(_convert_assistant_message(msg))
            # peek next: if it's a user message with tool_results, convert them
            if i + 1 < len(messages):
                next_msg = messages[i + 1]
                if _is_tool_result_message(next_msg):
                    i += 1
                    result.extend(_convert_tool_results(next_msg))
        i += 1

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
        cache_control = _last_block_cache_control(system)
        if cache_control is not None:
            msg["cache_control"] = cache_control
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

    # multimodal content blocks
    parts: List[Dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype == "text":
            parts.append({"type": "text", "text": block.get("text", "")})
        elif btype == "image":
            img = _convert_image_block(block)
            if img:
                parts.append(img)
        # tool_result blocks in user messages are handled separately

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
            "content": _flatten_tool_result_content(block.get("content")),
        }
        cc = block.get("cache_control")
        if isinstance(cc, dict):
            item["cache_control"] = cc
        results.append(item)
    return results


def _flatten_tool_result_content(content: Any) -> str:
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
            elif isinstance(item, dict):
                parts.append(item.get("text", json.dumps(item, ensure_ascii=False)))
        return "\n".join(parts).strip()
    if isinstance(content, dict):
        return content.get("text", json.dumps(content, ensure_ascii=False))
    return str(content)


# ── Image ──

def _convert_image_block(block: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    source = block.get("source")
    if not isinstance(source, dict):
        return None
    stype = source.get("type")
    if stype == "base64":
        media = source.get("media_type", "image/png")
        data = source.get("data", "")
        if not data:
            return None
        return {"type": "image_url", "image_url": {"url": f"data:{media};base64,{data}"}}
    if stype == "url":
        url = source.get("url", "")
        if url:
            return {"type": "image_url", "image_url": {"url": url}}
    return None


# ── Tools ──

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
    if ctype == "auto":
        return "auto"
    if ctype == "any":
        return "required"
    if ctype == "tool" and tc.get("name"):
        return {"type": "function", "function": {"name": tc["name"]}}
    return "auto"

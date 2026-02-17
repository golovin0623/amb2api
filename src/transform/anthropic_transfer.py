"""
Anthropic Transfer Module
Handles conversion between Anthropic Messages API payloads and internal OpenAI-like payloads.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional


def _normalize_content_blocks(content: Any) -> List[Dict[str, Any]]:
    """Normalize Anthropic content into block list form."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return [{"type": "text", "text": str(content)}]


def _extract_text_from_blocks(blocks: List[Dict[str, Any]]) -> str:
    """Extract plain text from Anthropic blocks."""
    parts: List[str] = []
    for block in blocks:
        if block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n".join(parts).strip()


def _anthropic_image_block_to_openai_part(block: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert Anthropic image block to OpenAI image_url content part."""
    source = block.get("source")
    if not isinstance(source, dict):
        return None

    source_type = source.get("type")
    if source_type == "base64":
        media_type = source.get("media_type") or "image/png"
        data = source.get("data")
        if not isinstance(data, str) or not data:
            return None
        return {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}}

    if source_type == "url":
        url = source.get("url")
        if not isinstance(url, str) or not url:
            return None
        return {"type": "image_url", "image_url": {"url": url}}

    return None


def _map_anthropic_tool_choice_to_openai(tool_choice: Any) -> Any:
    """Convert Anthropic tool_choice to OpenAI tool_choice."""
    if not isinstance(tool_choice, dict):
        return tool_choice

    choice_type = tool_choice.get("type")
    if choice_type == "auto":
        return "auto"
    if choice_type == "any":
        return "required"
    if choice_type == "tool":
        name = tool_choice.get("name")
        if isinstance(name, str) and name:
            return {"type": "function", "function": {"name": name}}
        return "required"
    return tool_choice


def _tool_result_block_to_openai_tool_message(block: Dict[str, Any]) -> Dict[str, Any]:
    """Convert Anthropic tool_result block to OpenAI role=tool message."""
    tool_call_id = block.get("tool_use_id")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        tool_call_id = f"call_{uuid.uuid4().hex[:24]}"

    block_content = block.get("content")
    if isinstance(block_content, str):
        content_text = block_content
    elif isinstance(block_content, list):
        content_text = _extract_text_from_blocks(_normalize_content_blocks(block_content))
        if not content_text:
            content_text = json.dumps(block_content, ensure_ascii=False)
    elif block_content is None:
        content_text = ""
    else:
        content_text = json.dumps(block_content, ensure_ascii=False)

    if block.get("is_error"):
        content_text = json.dumps({"is_error": True, "content": content_text}, ensure_ascii=False)

    return {"role": "tool", "tool_call_id": tool_call_id, "content": content_text}


def _map_anthropic_tools_to_openai(anthropic_tools: Any) -> Optional[List[Dict[str, Any]]]:
    """Convert Anthropic tools schema to OpenAI function tools schema."""
    if not isinstance(anthropic_tools, list):
        return None

    mapped: List[Dict[str, Any]] = []
    for item in anthropic_tools:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        description = item.get("description") or ""
        input_schema = item.get("input_schema")
        if not isinstance(input_schema, dict):
            input_schema = {"type": "object", "properties": {}}

        mapped.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": input_schema,
                },
            }
        )
    return mapped or None


def anthropic_request_to_openai_payload(anthropic_request: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert Anthropic messages request payload to OpenAI-compatible payload.

    Output is designed to be consumable by ChatCompletionRequest.
    """
    model = anthropic_request.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("Anthropic request missing required field: model")

    openai_messages: List[Dict[str, Any]] = []

    # 1) system -> first OpenAI system message
    system = anthropic_request.get("system")
    if isinstance(system, str):
        system_text = system.strip()
    elif isinstance(system, list):
        system_text = _extract_text_from_blocks(_normalize_content_blocks(system))
    else:
        system_text = ""

    if system_text:
        openai_messages.append({"role": "system", "content": system_text})

    # 2) messages conversion
    messages = anthropic_request.get("messages")
    if not isinstance(messages, list):
        raise ValueError("Anthropic request missing required field: messages")

    for message in messages:
        if not isinstance(message, dict):
            continue

        role = message.get("role")
        if role not in ("user", "assistant"):
            role = "user"

        blocks = _normalize_content_blocks(message.get("content"))
        text_blocks: List[str] = []
        image_parts: List[Dict[str, Any]] = []
        tool_calls: List[Dict[str, Any]] = []
        tool_messages: List[Dict[str, Any]] = []

        for block in blocks:
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    text_blocks.append(text)
            elif block_type == "image":
                image_part = _anthropic_image_block_to_openai_part(block)
                if image_part:
                    image_parts.append(image_part)
            elif block_type == "tool_use":
                tool_id = block.get("id")
                if not isinstance(tool_id, str) or not tool_id:
                    tool_id = f"call_{uuid.uuid4().hex[:24]}"
                tool_name = block.get("name")
                if not isinstance(tool_name, str):
                    tool_name = "unknown_tool"
                tool_input = block.get("input", {})
                if not isinstance(tool_input, dict):
                    tool_input = {"value": tool_input}

                tool_calls.append(
                    {
                        "id": tool_id,
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(tool_input, ensure_ascii=False),
                        },
                    }
                )
            elif block_type == "tool_result":
                tool_messages.append(_tool_result_block_to_openai_tool_message(block))

        # Build role user/assistant message when it has normal content or tool_calls.
        content_text = "\n".join(text_blocks).strip()
        content_value: Any = None
        if image_parts:
            content_parts: List[Dict[str, Any]] = []
            if content_text:
                content_parts.append({"type": "text", "text": content_text})
            content_parts.extend(image_parts)
            content_value = content_parts
        elif content_text:
            content_value = content_text

        if role == "assistant":
            assistant_message: Dict[str, Any] = {"role": "assistant", "content": content_value}
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls
            if content_value is not None or tool_calls:
                openai_messages.append(assistant_message)
        else:
            if content_value is not None:
                openai_messages.append({"role": "user", "content": content_value})

        # tool_result blocks are represented as role=tool messages
        if tool_messages:
            openai_messages.extend(tool_messages)

    payload: Dict[str, Any] = {"model": model, "messages": openai_messages}

    max_tokens = anthropic_request.get("max_tokens")
    if isinstance(max_tokens, int) and max_tokens > 0:
        payload["max_tokens"] = max_tokens

    for key in ("temperature", "top_p", "top_k", "stream"):
        val = anthropic_request.get(key)
        if val is not None:
            payload[key] = val

    stop_sequences = anthropic_request.get("stop_sequences")
    if isinstance(stop_sequences, list) and stop_sequences:
        payload["stop"] = [str(s) for s in stop_sequences]

    tools = _map_anthropic_tools_to_openai(anthropic_request.get("tools"))
    if tools:
        payload["tools"] = tools

    tool_choice = anthropic_request.get("tool_choice")
    if tool_choice is not None:
        payload["tool_choice"] = _map_anthropic_tool_choice_to_openai(tool_choice)

    return payload


def _parse_openai_tool_arguments(arguments: Any) -> Dict[str, Any]:
    """Parse OpenAI function arguments into object for Anthropic tool_use.input."""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        arguments = arguments.strip()
        if not arguments:
            return {}
        try:
            parsed = json.loads(arguments)
            if isinstance(parsed, dict):
                return parsed
            return {"value": parsed}
        except Exception:
            return {"raw": arguments}
    if arguments is None:
        return {}
    return {"value": arguments}


def _map_openai_finish_reason_to_anthropic(finish_reason: Any) -> Optional[str]:
    """Map OpenAI finish_reason to Anthropic stop_reason."""
    if finish_reason == "stop":
        return "end_turn"
    if finish_reason == "length":
        return "max_tokens"
    if finish_reason == "tool_calls":
        return "tool_use"
    if finish_reason == "content_filter":
        return "refusal"
    return None


def openai_response_to_anthropic_message(
    openai_response: Dict[str, Any], fallback_model: Optional[str] = None
) -> Dict[str, Any]:
    """Convert OpenAI non-stream response to Anthropic message response."""
    choices = openai_response.get("choices")
    if not isinstance(choices, list):
        choices = []
    first_choice = choices[0] if choices else {}
    if not isinstance(first_choice, dict):
        first_choice = {}

    message = first_choice.get("message")
    if not isinstance(message, dict):
        message = {}

    content_blocks: List[Dict[str, Any]] = []
    content = message.get("content")
    if isinstance(content, str):
        if content:
            content_blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type in ("text", "output_text"):
                text = part.get("text")
                if isinstance(text, str) and text:
                    content_blocks.append({"type": "text", "text": text})

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function_info = tool_call.get("function")
            if not isinstance(function_info, dict):
                function_info = {}
            tool_id = tool_call.get("id")
            if not isinstance(tool_id, str) or not tool_id:
                tool_id = f"toolu_{uuid.uuid4().hex[:24]}"
            tool_name = function_info.get("name")
            if not isinstance(tool_name, str) or not tool_name:
                tool_name = "unknown_tool"
            tool_input = _parse_openai_tool_arguments(function_info.get("arguments"))
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": tool_name,
                    "input": tool_input,
                }
            )

    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})

    usage = openai_response.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    input_tokens = usage.get("prompt_tokens")
    output_tokens = usage.get("completion_tokens")
    if not isinstance(input_tokens, int):
        input_tokens = 0
    if not isinstance(output_tokens, int):
        output_tokens = 0

    response_id = openai_response.get("id")
    if not isinstance(response_id, str) or not response_id:
        response_id = f"msg_{uuid.uuid4().hex}"

    model = openai_response.get("model")
    if not isinstance(model, str) or not model:
        model = fallback_model or ""

    return {
        "id": response_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": _map_openai_finish_reason_to_anthropic(first_choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


def openai_chunk_to_anthropic_events(
    openai_chunk: Dict[str, Any],
    state: Dict[str, Any],
    default_model: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Convert one OpenAI stream chunk JSON to Anthropic event list."""
    events: List[Dict[str, Any]] = []

    model = openai_chunk.get("model")
    if not isinstance(model, str) or not model:
        model = default_model or state.get("model") or ""
    state["model"] = model

    message_id = openai_chunk.get("id")
    if not isinstance(message_id, str) or not message_id:
        message_id = state.get("message_id") or f"msg_{uuid.uuid4().hex}"
    state["message_id"] = message_id

    if not state.get("message_started"):
        events.append(
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            }
        )
        state["message_started"] = True

    choices = openai_chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return events
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return events

    delta = first_choice.get("delta")
    if not isinstance(delta, dict):
        delta = {}

    # text delta mapping
    text_delta = delta.get("content")
    if isinstance(text_delta, str) and text_delta:
        if not state.get("text_block_started"):
            events.append(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                }
            )
            state["text_block_started"] = True

        events.append(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text_delta},
            }
        )

    # tool delta mapping (best effort for chunked tool_calls)
    tool_calls = delta.get("tool_calls")
    if isinstance(tool_calls, list):
        for idx, tool_call in enumerate(tool_calls, start=1):
            if not isinstance(tool_call, dict):
                continue
            function_info = tool_call.get("function")
            if not isinstance(function_info, dict):
                function_info = {}
            tool_name = function_info.get("name")
            if not isinstance(tool_name, str) or not tool_name:
                tool_name = "unknown_tool"
            tool_id = tool_call.get("id")
            if not isinstance(tool_id, str) or not tool_id:
                tool_id = f"toolu_{uuid.uuid4().hex[:24]}"
            tool_input = _parse_openai_tool_arguments(function_info.get("arguments"))

            events.append(
                {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": tool_name,
                        "input": tool_input,
                    },
                }
            )
            events.append({"type": "content_block_stop", "index": idx})

    finish_reason = first_choice.get("finish_reason")
    if finish_reason is not None:
        if state.get("text_block_started"):
            events.append({"type": "content_block_stop", "index": 0})
            state["text_block_started"] = False

        usage = openai_chunk.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        output_tokens = usage.get("completion_tokens")
        if not isinstance(output_tokens, int):
            output_tokens = usage.get("output_tokens", 0)
        if not isinstance(output_tokens, int):
            output_tokens = 0

        events.append(
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": _map_openai_finish_reason_to_anthropic(finish_reason),
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": output_tokens},
            }
        )
        events.append({"type": "message_stop"})
        state["message_stopped"] = True

    return events


def convert_openai_sse_payload_to_anthropic_events(
    payload_text: str, state: Dict[str, Any], default_model: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Convert OpenAI SSE payload text to Anthropic event objects."""
    clean = payload_text.strip()
    if not clean:
        return []
    if clean == "[DONE]":
        events: List[Dict[str, Any]] = []
        if state.get("text_block_started"):
            events.append({"type": "content_block_stop", "index": 0})
            state["text_block_started"] = False
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


def anthropic_events_to_sse_bytes(events: List[Dict[str, Any]]) -> bytes:
    """Encode Anthropic event objects to SSE bytes."""
    out: List[str] = []
    for event in events:
        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type:
            continue
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        out.append(f"event: {event_type}\ndata: {payload}\n\n")
    return "".join(out).encode("utf-8")


def openai_error_to_anthropic_error_body(
    status_code: int,
    parsed_error: Any = None,
    fallback_message: str = "Upstream request failed",
) -> Dict[str, Any]:
    """Normalize error payload to Anthropic-style error object."""
    if status_code in (401, 403):
        error_type = "authentication_error"
    elif status_code == 429:
        error_type = "rate_limit_error"
    elif status_code == 400:
        error_type = "invalid_request_error"
    else:
        error_type = "api_error"

    message = fallback_message
    if isinstance(parsed_error, dict):
        if isinstance(parsed_error.get("error"), dict):
            message = str(parsed_error["error"].get("message") or message)
        elif parsed_error.get("message") is not None:
            message = str(parsed_error.get("message"))

    return {"type": "error", "error": {"type": error_type, "message": message}}


def estimate_openai_messages_input_tokens(messages: Any) -> int:
    """
    Estimate prompt/input tokens from OpenAI-like messages.

    This is a conservative approximation for count_tokens compatibility.
    """
    if not isinstance(messages, list):
        return 0

    def _content_text_length(content: Any) -> int:
        if isinstance(content, str):
            return len(content)
        if isinstance(content, list):
            total = 0
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        total += len(str(part.get("text", "")))
                    elif part.get("type") == "image_url":
                        # Account for multimodal prompt overhead.
                        total += 256
                elif isinstance(part, str):
                    total += len(part)
            return total
        if content is None:
            return 0
        return len(str(content))

    text_chars = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        text_chars += _content_text_length(message.get("content"))
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function_info = tool_call.get("function")
                if isinstance(function_info, dict):
                    text_chars += len(str(function_info.get("name", "")))
                    text_chars += len(str(function_info.get("arguments", "")))

    # ~4 chars/token + per-message overhead
    estimated = (text_chars // 4) + (len(messages) * 4)
    return max(1, estimated)

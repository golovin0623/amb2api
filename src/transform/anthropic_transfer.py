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


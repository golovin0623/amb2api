"""Anthropic Messages API Pydantic models for Claude Code proxy."""

from typing import List, Dict, Any, Optional, Union, Literal
from pydantic import BaseModel, Field


# ── Content Blocks ──

class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str

class ImageBlock(BaseModel):
    type: Literal["image"] = "image"
    source: Dict[str, Any]

class ToolUseBlock(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: Dict[str, Any]

class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: Union[str, List[Dict[str, Any]], Dict[str, Any], None] = None
    is_error: Optional[bool] = None

class ThinkingBlock(BaseModel):
    type: Literal["thinking"] = "thinking"
    thinking: str

ContentBlock = Union[TextBlock, ImageBlock, ToolUseBlock, ToolResultBlock, ThinkingBlock]


# ── Messages ──

class ClaudeMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: Union[str, List[ContentBlock]]


# ── Tools ──

class ClaudeTool(BaseModel):
    name: str
    description: Optional[str] = None
    input_schema: Dict[str, Any]


# ── Request ──

class ClaudeMessagesRequest(BaseModel):
    model: str
    max_tokens: int
    messages: List[ClaudeMessage]
    system: Optional[Union[str, List[Dict[str, Any]]]] = None
    stop_sequences: Optional[List[str]] = None
    stream: Optional[bool] = False
    temperature: Optional[float] = 1.0
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None
    tools: Optional[List[ClaudeTool]] = None
    tool_choice: Optional[Dict[str, Any]] = None
    thinking: Optional[Dict[str, Any]] = None

    class Config:
        extra = "allow"


# ── Token Count Request ──

class ClaudeTokenCountRequest(BaseModel):
    model: str
    messages: List[ClaudeMessage]
    system: Optional[Union[str, List[Dict[str, Any]]]] = None
    tools: Optional[List[ClaudeTool]] = None
    tool_choice: Optional[Dict[str, Any]] = None
    thinking: Optional[Dict[str, Any]] = None

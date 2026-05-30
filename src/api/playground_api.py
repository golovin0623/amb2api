"""
操练场增强 API 端点模块
提供请求报文预览和自定义报文发送功能
"""
from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field

from log import log
from .auth import authenticate
from ..transform.request_generator import get_request_generator, create_request_generator
from ..services.assembly_client import send_assembly_request
from ..models.models import ChatCompletionRequest
from config import get_assembly_endpoint, get_assembly_api_keys


# 操练场会用服务端 key 发真实请求，必须鉴权
router = APIRouter(
    prefix="/api/playground", tags=["Playground"], dependencies=[Depends(authenticate)]
)


# Request/Response Models
class PreviewRequest(BaseModel):
    """请求预览请求"""
    model: str = Field(..., description="模型名称")
    messages: list = Field(..., description="消息列表")
    temperature: Optional[float] = Field(None, description="温度")
    max_tokens: Optional[int] = Field(None, description="最大 token 数")
    max_completion_tokens: Optional[int] = Field(None, description="最大 completion token 数")
    top_p: Optional[float] = Field(None, description="Top P")
    stream: Optional[bool] = Field(None, description="是否流式")
    stream_options: Optional[Dict[str, Any]] = Field(None, description="流式选项")
    tools: Optional[list] = Field(None, description="工具列表")
    tool_choice: Optional[Any] = Field(None, description="工具选择")
    parallel_tool_calls: Optional[bool] = Field(None, description="是否允许并行工具调用")
    reasoning_effort: Optional[str] = Field(None, description="推理强度")
    verbosity: Optional[str] = Field(None, description="输出详略")
    cache_control: Optional[Dict[str, Any]] = Field(None, description="顶层缓存控制")
    prompt_cache_retention: Optional[str] = Field(None, description="OpenAI prompt cache retention")
    prompt_cache_key: Optional[str] = Field(None, description="OpenAI prompt cache key")


class CustomRequest(BaseModel):
    """自定义请求"""
    request_json: str = Field(..., description="JSON 格式的请求体")
    validate_only: bool = Field(False, description="仅验证不发送")


class PreviewResponse(BaseModel):
    """请求预览响应"""
    method: str
    url: str
    headers: Dict[str, str]
    body: Dict[str, Any]
    body_json: str


class ValidationResponse(BaseModel):
    """验证响应"""
    valid: bool
    error: Optional[str] = None


# API Endpoints
@router.post("/preview", response_model=PreviewResponse)
async def generate_request_preview(request: PreviewRequest):
    """生成请求报文预览"""
    try:
        # 获取配置
        try:
            endpoint = await get_assembly_endpoint()
        except Exception:
            endpoint = "https://llm-gateway.assemblyai.com/v1/chat/completions"
        
        try:
            keys = await get_assembly_api_keys()
            api_key = keys[0] if keys else "sk-your-api-key"
        except Exception:
            api_key = "sk-your-api-key"
        
        generator = create_request_generator(endpoint, api_key)
        
        # 转换 messages 格式
        messages = []
        for msg in request.messages:
            if isinstance(msg, dict):
                messages.append(msg)
            else:
                messages.append({"role": msg.get("role", "user"), "content": msg.get("content", "")})
        
        params = {
            "model": request.model,
            "messages": messages,
        }
        
        if request.temperature is not None:
            params["temperature"] = request.temperature
        if request.max_tokens is not None:
            params["max_tokens"] = request.max_tokens
        if request.max_completion_tokens is not None:
            params["max_completion_tokens"] = request.max_completion_tokens
        if request.top_p is not None:
            params["top_p"] = request.top_p
        if request.stream is not None:
            params["stream"] = request.stream
        if request.stream_options is not None:
            params["stream_options"] = request.stream_options
        if request.tools is not None:
            params["tools"] = request.tools
        if request.tool_choice is not None:
            params["tool_choice"] = request.tool_choice
        if request.parallel_tool_calls is not None:
            params["parallel_tool_calls"] = request.parallel_tool_calls
        if request.reasoning_effort is not None:
            params["reasoning_effort"] = request.reasoning_effort
        if request.verbosity is not None:
            params["verbosity"] = request.verbosity
        if request.cache_control is not None:
            params["cache_control"] = request.cache_control
        if request.prompt_cache_retention is not None:
            params["prompt_cache_retention"] = request.prompt_cache_retention
        if request.prompt_cache_key is not None:
            params["prompt_cache_key"] = request.prompt_cache_key
        
        preview = generator.generate_request_preview(params)
        
        return PreviewResponse(
            method=preview["method"],
            url=preview["url"],
            headers=preview["headers"],
            body=preview["body"],
            body_json=preview["body_json"]
        )
    except Exception as e:
        log.error(f"Failed to generate request preview: {e}")
        import traceback
        log.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/validate", response_model=ValidationResponse)
async def validate_custom_request(request: CustomRequest):
    """验证自定义请求格式"""
    try:
        generator = get_request_generator()
        
        is_valid, error = generator.validate_custom_request(request.request_json)
        
        return ValidationResponse(
            valid=is_valid,
            error=error if not is_valid else None
        )
    except Exception as e:
        log.error(f"Failed to validate custom request: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/custom")
async def send_custom_request(request: CustomRequest):
    """发送自定义请求"""
    try:
        generator = get_request_generator()
        
        # 验证请求
        is_valid, error = generator.validate_custom_request(request.request_json)
        
        if not is_valid:
            raise HTTPException(status_code=400, detail=f"Invalid request: {error}")
        
        if request.validate_only:
            return {"success": True, "message": "Request is valid"}
        
        # 解析请求
        parsed = generator.parse_custom_request(request.request_json)
        if not parsed:
            raise HTTPException(status_code=400, detail="Failed to parse request")
        
        # 构建 ChatCompletionRequest
        try:
            chat_request = ChatCompletionRequest(**parsed)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid request format: {str(e)}")
        
        # 发送请求
        response = await send_assembly_request(chat_request, is_streaming=False)
        
        # 处理响应
        if hasattr(response, 'json'):
            return response.json()
        elif hasattr(response, 'body'):
            import json
            return json.loads(response.body)
        else:
            return {"error": "Unexpected response format"}
            
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to send custom request: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/initial-request")
async def generate_initial_request(request: PreviewRequest):
    """根据操练场参数生成初始自定义请求"""
    try:
        generator = get_request_generator()
        
        params = {
            "model": request.model,
            "messages": request.messages,
        }
        
        if request.temperature is not None:
            params["temperature"] = request.temperature
        if request.max_tokens is not None:
            params["max_tokens"] = request.max_tokens
        if request.max_completion_tokens is not None:
            params["max_completion_tokens"] = request.max_completion_tokens
        if request.top_p is not None:
            params["top_p"] = request.top_p
        if request.stream is not None:
            params["stream"] = request.stream
        if request.stream_options is not None:
            params["stream_options"] = request.stream_options
        if request.tools is not None:
            params["tools"] = request.tools
        if request.tool_choice is not None:
            params["tool_choice"] = request.tool_choice
        if request.parallel_tool_calls is not None:
            params["parallel_tool_calls"] = request.parallel_tool_calls
        if request.reasoning_effort is not None:
            params["reasoning_effort"] = request.reasoning_effort
        if request.verbosity is not None:
            params["verbosity"] = request.verbosity
        if request.cache_control is not None:
            params["cache_control"] = request.cache_control
        if request.prompt_cache_retention is not None:
            params["prompt_cache_retention"] = request.prompt_cache_retention
        if request.prompt_cache_key is not None:
            params["prompt_cache_key"] = request.prompt_cache_key
        
        initial_json = generator.generate_initial_custom_request(params)
        
        return {
            "request_json": initial_json
        }
    except Exception as e:
        log.error(f"Failed to generate initial request: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# 性能监控 API 端点
# ============================================================

@router.get("/performance/stats")
async def get_performance_stats(model: Optional[str] = None):
    """
    获取性能统计数据
    
    Args:
        model: 可选，按模型筛选
    
    Returns:
        统计数据，包含 TTFB/TTFT/TPS/延迟的平均值和百分位数
    """
    try:
        from ..stats.performance_tracker import get_performance_tracker
        tracker = await get_performance_tracker()
        stats = await tracker.get_stats(model=model)
        return stats
    except Exception as e:
        log.error(f"Failed to get performance stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/performance/traces")
async def get_performance_traces(
    page: int = 1,
    page_size: int = 20,
    model: Optional[str] = None,
    key: Optional[str] = None,
    search: Optional[str] = None,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None
):
    """
    分页查询追踪记录
    
    Args:
        page: 页码（从 1 开始）
        page_size: 每页记录数（默认 20）
        model: 模型筛选
        search: 搜索 trace_id
        start_time: 开始时间戳筛选
        end_time: 结束时间戳筛选
    
    Returns:
        分页结果，包含 traces、page、page_size、total、total_pages
    """
    try:
        from ..stats.performance_tracker import get_performance_tracker
        tracker = await get_performance_tracker()
        result = await tracker.get_traces_paginated(
            page=page,
            page_size=page_size,
            model=model,
            key=key,
            search=search,
            start_time=start_time,
            end_time=end_time
        )
        return result
    except Exception as e:
        log.error(f"Failed to get performance traces: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/performance/trace/{trace_id}")
async def get_performance_trace_detail(trace_id: str):
    """
    获取单条追踪详情
    
    Args:
        trace_id: 追踪 ID
    
    Returns:
        追踪详情，包含 timestamps、metrics、durations
    """
    try:
        from ..stats.performance_tracker import get_performance_tracker
        tracker = await get_performance_tracker()
        trace = await tracker.get_trace_by_id(trace_id)
        if not trace:
            raise HTTPException(status_code=404, detail="Trace not found")
        return trace
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to get trace detail: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/performance/models")
async def get_performance_models():
    """
    获取所有有追踪记录的模型列表
    
    Returns:
        模型名称列表
    """
    try:
        from ..stats.performance_tracker import get_performance_tracker
        tracker = await get_performance_tracker()
        models = await tracker.get_models()
        return {"models": models}
    except Exception as e:
        log.error(f"Failed to get performance models: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/performance/clear")
async def clear_performance_data():
    """
    清除所有性能追踪数据
    
    Returns:
        操作结果
    """
    try:
        from ..stats.performance_tracker import get_performance_tracker
        tracker = await get_performance_tracker()
        await tracker.clear_all()
        return {"success": True, "message": "All performance data cleared"}
    except Exception as e:
        log.error(f"Failed to clear performance data: {e}")
        raise HTTPException(status_code=500, detail=str(e))

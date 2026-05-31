"""
请求生成器模块
生成请求报文预览和验证自定义请求
"""
import json
from typing import Dict, Any, Tuple, Optional, List

from log import log


class RequestGenerator:
    """请求生成器"""

    REASONING_EFFORT_VALUES = ("none", "minimal", "low", "medium", "high", "xhigh")
    VERBOSITY_VALUES = ("low", "medium", "high")
    
    # OpenAI 兼容的请求参数
    SUPPORTED_PARAMS = [
        "model",
        "messages",
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "stop",
        "frequency_penalty",
        "presence_penalty",
        "n",
        "seed",
        "response_format",
        "tools",
        "tool_choice",
        "cache_control",
        "prompt_cache_retention",
        "prompt_cache_key",
        "stream",
        "stream_options",
        "parallel_tool_calls",
        "reasoning_effort",
        "verbosity",
        # AssemblyAI Gateway 原生扩展
        "reasoning",
        "fallbacks",
        "fallback_config",
        "post_processing_steps",
        "transcript_id",
    ]
    
    def __init__(self, endpoint: str = "", api_key: str = ""):
        """
        初始化请求生成器
        
        Args:
            endpoint: API 端点
            api_key: API 密钥（用于预览）
        """
        self._endpoint = endpoint
        self._api_key = api_key
    
    def set_endpoint(self, endpoint: str):
        """设置 API 端点"""
        self._endpoint = endpoint
    
    def set_api_key(self, api_key: str):
        """设置 API 密钥"""
        self._api_key = api_key
    
    def _mask_key(self, key: str) -> str:
        """脱敏密钥"""
        if not key:
            return ""
        k = str(key)
        if len(k) <= 8:
            return k[:2] + "***"
        return k[:4] + "..." + k[-4:]
    
    def generate_request_preview(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        生成请求报文预览
        
        Args:
            params: 请求参数
        
        Returns:
            包含请求头、请求体等信息的字典
        """
        # 构建请求体
        body = {}
        for key in self.SUPPORTED_PARAMS:
            if key in params and params[key] is not None:
                body[key] = params[key]
        
        # 确保必需字段存在
        if "model" not in body:
            body["model"] = "gpt-4"
        if "messages" not in body:
            body["messages"] = [{"role": "user", "content": "Hello"}]
        
        # 构建请求头
        masked_key = self._mask_key(self._api_key) if self._api_key else "<API_KEY>"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {masked_key}",
        }
        
        return {
            "method": "POST",
            "url": self._endpoint or "<ENDPOINT>",
            "headers": headers,
            "body": body,
            "body_json": json.dumps(body, indent=2, ensure_ascii=False),
        }
    
    def validate_custom_request(self, request_json: str) -> Tuple[bool, str]:
        """
        验证自定义请求格式
        
        Args:
            request_json: JSON 格式的请求字符串
        
        Returns:
            (是否有效, 错误信息或空字符串)
        """
        if not request_json or not request_json.strip():
            return False, "Request body cannot be empty"
        
        try:
            data = json.loads(request_json)
        except json.JSONDecodeError as e:
            return False, f"Invalid JSON format: {str(e)}"
        
        if not isinstance(data, dict):
            return False, "Request body must be a JSON object"
        
        # 检查必需字段
        if "model" not in data:
            return False, "Missing required field: model"
        
        if "messages" not in data:
            return False, "Missing required field: messages"
        
        # 验证 messages 格式
        messages = data.get("messages")
        if not isinstance(messages, list):
            return False, "Field 'messages' must be an array"
        
        if len(messages) == 0:
            return False, "Field 'messages' cannot be empty"
        
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                return False, f"Message at index {i} must be an object"
            if "role" not in msg:
                return False, f"Message at index {i} missing 'role' field"
            if "content" not in msg and "tool_calls" not in msg:
                return False, f"Message at index {i} missing 'content' or 'tool_calls' field"
        
        # 验证可选字段类型
        if "temperature" in data:
            temp = data["temperature"]
            if not isinstance(temp, (int, float)) or temp < 0 or temp > 2:
                return False, "Field 'temperature' must be a number between 0 and 2"
        
        if "max_tokens" in data:
            max_tokens = data["max_tokens"]
            if not isinstance(max_tokens, int) or max_tokens < 1:
                return False, "Field 'max_tokens' must be a positive integer"

        if "max_completion_tokens" in data:
            max_completion_tokens = data["max_completion_tokens"]
            if not isinstance(max_completion_tokens, int) or max_completion_tokens < 1:
                return False, "Field 'max_completion_tokens' must be a positive integer"
        
        if "top_p" in data:
            top_p = data["top_p"]
            if not isinstance(top_p, (int, float)) or top_p < 0 or top_p > 1:
                return False, "Field 'top_p' must be a number between 0 and 1"
        
        if "n" in data:
            n = data["n"]
            if not isinstance(n, int) or n < 1:
                return False, "Field 'n' must be a positive integer"
        
        if "stream" in data:
            if not isinstance(data["stream"], bool):
                return False, "Field 'stream' must be a boolean"

        if "stream_options" in data and not isinstance(data["stream_options"], dict):
            return False, "Field 'stream_options' must be an object"

        if "parallel_tool_calls" in data and not isinstance(data["parallel_tool_calls"], bool):
            return False, "Field 'parallel_tool_calls' must be a boolean"

        if "reasoning_effort" in data and data["reasoning_effort"] not in self.REASONING_EFFORT_VALUES:
            values = ", ".join(self.REASONING_EFFORT_VALUES)
            return False, f"Field 'reasoning_effort' must be one of: {values}"

        if "verbosity" in data and data["verbosity"] not in self.VERBOSITY_VALUES:
            values = ", ".join(self.VERBOSITY_VALUES)
            return False, f"Field 'verbosity' must be one of: {values}"
        
        if "tools" in data:
            tools = data["tools"]
            if not isinstance(tools, list):
                return False, "Field 'tools' must be an array"

        if "cache_control" in data and not isinstance(data["cache_control"], dict):
            return False, "Field 'cache_control' must be an object"

        if "prompt_cache_retention" in data and not isinstance(data["prompt_cache_retention"], str):
            return False, "Field 'prompt_cache_retention' must be a string"

        if "prompt_cache_key" in data and not isinstance(data["prompt_cache_key"], str):
            return False, "Field 'prompt_cache_key' must be a string"

        if "reasoning" in data and not isinstance(data["reasoning"], dict):
            return False, "Field 'reasoning' must be an object"

        if "response_format" in data and not isinstance(data["response_format"], dict):
            return False, "Field 'response_format' must be an object"

        if "fallbacks" in data:
            fbs = data["fallbacks"]
            if not isinstance(fbs, list):
                return False, "Field 'fallbacks' must be an array"
            # 官方 schema：每个 fallback 是带 model 的对象，拒绝旧的字符串写法，
            # 否则自定义报文会在本地通过、却在网关被判非法。
            for i, fb in enumerate(fbs):
                model_id = fb.get("model") if isinstance(fb, dict) else None
                if not isinstance(model_id, str) or not model_id.strip():
                    return False, (
                        f"Field 'fallbacks[{i}]' must be an object with a 'model' string "
                        "(e.g. {\"model\": \"gpt-5\"})"
                    )

        if "fallback_config" in data and not isinstance(data["fallback_config"], dict):
            return False, "Field 'fallback_config' must be an object"

        if "post_processing_steps" in data and not isinstance(data["post_processing_steps"], list):
            return False, "Field 'post_processing_steps' must be an array"

        if "transcript_id" in data and not isinstance(data["transcript_id"], str):
            return False, "Field 'transcript_id' must be a string"

        return True, ""
    
    def generate_initial_custom_request(self, params: Dict[str, Any]) -> str:
        """
        根据操练场参数生成初始自定义请求
        
        Args:
            params: 操练场参数
        
        Returns:
            JSON 格式的请求字符串
        """
        preview = self.generate_request_preview(params)
        return preview["body_json"]
    
    def parse_custom_request(self, request_json: str) -> Optional[Dict[str, Any]]:
        """
        解析自定义请求
        
        Args:
            request_json: JSON 格式的请求字符串
        
        Returns:
            解析后的请求字典，如果解析失败则返回 None
        """
        valid, error = self.validate_custom_request(request_json)
        if not valid:
            log.warning(f"Invalid custom request: {error}")
            return None
        
        try:
            return json.loads(request_json)
        except Exception:
            return None
    
    def merge_with_defaults(
        self, 
        custom_request: Dict[str, Any],
        defaults: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        将自定义请求与默认值合并
        
        Args:
            custom_request: 自定义请求
            defaults: 默认值
        
        Returns:
            合并后的请求
        """
        result = defaults.copy()
        result.update(custom_request)
        return result


# 全局实例
_request_generator: Optional[RequestGenerator] = None


def get_request_generator() -> RequestGenerator:
    """获取全局请求生成器实例"""
    global _request_generator
    if _request_generator is None:
        _request_generator = RequestGenerator()
    return _request_generator


def create_request_generator(endpoint: str = "", api_key: str = "") -> RequestGenerator:
    """创建新的请求生成器实例"""
    return RequestGenerator(endpoint, api_key)

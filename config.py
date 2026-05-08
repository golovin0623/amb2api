"""
Configuration constants for the Geminicli2api proxy server.
Centralizes all configuration to avoid duplication across modules.
"""
import os
from typing import Any, Optional

from src.storage.storage_adapter import get_storage_adapter

# Client Configuration

# 需要自动封禁的错误码 (默认值，可通过环境变量或配置覆盖)
AUTO_BAN_ERROR_CODES = [401, 403]

# Default Safety Settings for Google API
DEFAULT_SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "BLOCK_NONE"}
]

# Helper function to get base model name from any variant
def get_base_model_name(model_name):
    """Convert variant model name to base model name."""
    # Remove all possible suffixes in order
    suffixes = ["-maxthinking", "-nothinking", "-search"]
    for suffix in suffixes:
        if model_name.endswith(suffix):
            return model_name[:-len(suffix)]
    return model_name

# Helper function to check if model uses search grounding
def is_search_model(model_name):
    """Check if model name indicates search grounding should be enabled."""
    return "-search" in model_name

# Helper function to check if model uses no thinking
def is_nothinking_model(model_name):
    """Check if model name indicates thinking should be disabled."""
    return "-nothinking" in model_name

# Helper function to check if model uses max thinking
def is_maxthinking_model(model_name):
    """Check if model name indicates maximum thinking budget should be used."""
    return "-maxthinking" in model_name

# Helper function to get thinking budget for a model
def get_thinking_budget(model_name):
    """Get the appropriate thinking budget for a model based on its name and variant."""
    
    if is_nothinking_model(model_name):
        return 128  # Limited thinking for pro
    elif is_maxthinking_model(model_name):
        return 32768
    else:
        # Default thinking budget for regular models
        return None  # Default for all models

# Helper function to check if thinking should be included in output
def should_include_thoughts(model_name):
    """Check if thoughts should be included in the response."""
    if is_nothinking_model(model_name):
        # For nothinking mode, still include thoughts if it's a pro model
        base_model = get_base_model_name(model_name)
        return "pro" in base_model
    else:
        # For all other modes, include thoughts
        return True

# Dynamic Configuration System - Optimized for memory efficiency
async def get_config_value(key: str, default: Any = None, env_var: Optional[str] = None) -> Any:
    override_env = False
    env_override = os.getenv("CONFIG_OVERRIDE_ENV")
    if env_override:
        if env_override.lower() in ("true", "1", "yes", "on"):
            override_env = True
    if not override_env:
        try:
            storage_adapter = await get_storage_adapter()
            ov = await storage_adapter.get_config("override_env")
            if isinstance(ov, str):
                override_env = ov.lower() in ("true", "1", "yes", "on")
            else:
                override_env = bool(ov)
        except Exception:
            override_env = False
    if (not override_env) and env_var and os.getenv(env_var):
        return os.getenv(env_var)
    try:
        storage_adapter = await get_storage_adapter()
        value = await storage_adapter.get_config(key)
        if value is not None:
            return value
    except Exception:
        pass
    return default


# Configuration getters - all async
async def get_proxy_config():
    """Get proxy configuration."""
    proxy_url = await get_config_value("proxy", env_var="PROXY")
    return proxy_url if proxy_url else None

async def get_calls_per_rotation() -> int:
    """Get calls per rotation setting."""
    env_value = os.getenv("CALLS_PER_ROTATION")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass
    
    return int(await get_config_value("calls_per_rotation", 100))

async def get_auto_ban_enabled() -> bool:
    """Get auto ban enabled setting."""
    env_value = os.getenv("AUTO_BAN")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")
    
    return bool(await get_config_value("auto_ban_enabled", False))

async def get_auto_ban_error_codes() -> list:
    """
    Get auto ban error codes.
    
    Environment variable: AUTO_BAN_ERROR_CODES (comma-separated, e.g., "400,403")
    TOML config key: auto_ban_error_codes
    Default: [400, 403]
    """
    env_value = os.getenv("AUTO_BAN_ERROR_CODES")
    if env_value:
        try:
            return [int(code.strip()) for code in env_value.split(",") if code.strip()]
        except ValueError:
            pass
    
    codes = await get_config_value("auto_ban_error_codes")
    if codes and isinstance(codes, list):
        return codes
    return AUTO_BAN_ERROR_CODES

async def get_retry_429_max_retries() -> int:
    """Get max retries for 429 errors."""
    env_value = os.getenv("RETRY_429_MAX_RETRIES")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass
    
    return int(await get_config_value("retry_429_max_retries", 5))

async def get_retry_429_enabled() -> bool:
    """Get 429 retry enabled setting."""
    env_value = os.getenv("RETRY_429_ENABLED")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")
    
    return bool(await get_config_value("retry_429_enabled", True))

async def get_retry_429_interval() -> float:
    """Get 429 retry interval in seconds."""
    env_value = os.getenv("RETRY_429_INTERVAL")
    if env_value:
        try:
            return float(env_value)
        except ValueError:
            pass
    
    return float(await get_config_value("retry_429_interval", 1))


# Model name lists for different features
BASE_MODELS = [
    "gemini-2.5-pro-preview-06-05",
    "gemini-2.5-pro", 
    "gemini-2.5-pro-preview-05-06",
    "gemini-2.5-flash",
    "gemini-flash-latest",
    "gemini-2.5-flash-image",
    "gemini-2.5-flash-image-preview",
    "gemini-2.5-flash-preview-09-2025"
]

PUBLIC_API_MODELS = [
    "gemini-2.5-flash-image",
    "gemini-2.5-flash-image-preview"
]

async def get_available_models_async(router_type: str = "openai"):
    """异步版本：优先返回已选模型或缓存模型"""
    selected = await get_config_value("available_models_selected")
    if isinstance(selected, list) and selected:
        return [str(m) for m in selected]
    cached = await get_config_value("available_models")
    if isinstance(cached, list) and cached:
        return [str(m) for m in cached]
    # 默认模型列表（Gateway 已支持的常用模型；面板可通过"刷新模型"接口同步真实列表）
    return [
        "gpt-5",
        "gpt-5-nano",
        "gpt-5-mini",
        "gpt-4.1",
        "claude-4.5-sonnet-20250929",
        "claude-4-sonnet-20250514",
        "claude-3.5-haiku-20241022",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        # 2026-04 Gateway relaunch 新增模型
        "qwen3-coder",
        "qwen3-235b",
        "kimi-k2.5",
    ]

def is_fake_streaming_model(model_name: str) -> bool:
    """Check if model name indicates fake streaming should be used."""
    return model_name.startswith("假流式/")

def is_anti_truncation_model(model_name: str) -> bool:
    """Check if model name indicates anti-truncation should be used."""
    return model_name.startswith("流式抗截断/")

def get_base_model_from_feature_model(model_name: str) -> str:
    """Get base model name from feature model name."""
    # Remove feature prefixes
    for prefix in ["假流式/", "流式抗截断/"]:
        if model_name.startswith(prefix):
            return model_name[len(prefix):]
    return model_name

async def get_anti_truncation_max_attempts() -> int:
    """
    Get maximum attempts for anti-truncation continuation.
    
    Environment variable: ANTI_TRUNCATION_MAX_ATTEMPTS
    TOML config key: anti_truncation_max_attempts
    Default: 3
    """
    return 3

# Server Configuration
async def get_server_host() -> str:
    """
    Get server host setting.
    
    Environment variable: HOST
    TOML config key: host
    Default: 0.0.0.0
    """
    return str(await get_config_value("host", "0.0.0.0", "HOST"))

async def get_server_port() -> int:
    """
    Get server port setting.
    
    Environment variable: PORT
    TOML config key: port
    Default: 7861
    """
    env_value = os.getenv("PORT")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass
    
    return int(await get_config_value("port", 7861))

async def get_api_password() -> str:
    """
    Get API password setting for chat endpoints.
    
    Environment variable: API_PASSWORD
    TOML config key: api_password
    Default: Uses PASSWORD env var for compatibility, otherwise 'pwd'
    """
    # 优先使用 API_PASSWORD，如果没有则使用通用 PASSWORD 保证兼容性
    api_password = await get_config_value("api_password", None, "API_PASSWORD")
    if api_password is not None:
        return str(api_password)
    
    # 兼容性：使用通用密码
    return str(await get_config_value("password", "pwd", "PASSWORD"))

async def get_panel_password() -> str:
    """
    Get panel password setting for web interface.
    
    Environment variable: PANEL_PASSWORD
    TOML config key: panel_password
    Default: Uses PASSWORD env var for compatibility, otherwise 'pwd'
    """
    # 优先使用 PANEL_PASSWORD，如果没有则使用通用 PASSWORD 保证兼容性
    panel_password = await get_config_value("panel_password", None, "PANEL_PASSWORD")
    if panel_password is not None:
        return str(panel_password)
    
    # 兼容性：使用通用密码
    return str(await get_config_value("password", "pwd", "PASSWORD"))

async def get_server_password() -> str:
    """
    Get server password setting (deprecated, use get_api_password or get_panel_password).
    
    Environment variable: PASSWORD
    TOML config key: password
    Default: pwd
    """
    return str(await get_config_value("password", "pwd", "PASSWORD"))

async def get_credentials_dir() -> str:
    """
    Get credentials directory setting.
    
    Environment variable: CREDENTIALS_DIR
    TOML config key: credentials_dir
    Default: ./creds
    """
    return str(await get_config_value("credentials_dir", "./creds", "CREDENTIALS_DIR"))


async def get_auto_load_env_creds() -> bool:
    """
    Get auto load environment credentials setting.
    
    Environment variable: AUTO_LOAD_ENV_CREDS
    TOML config key: auto_load_env_creds
    Default: False
    """
    env_value = os.getenv("AUTO_LOAD_ENV_CREDS")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")
    
    return bool(await get_config_value("auto_load_env_creds", False))

async def get_compatibility_mode_enabled() -> bool:
    """
    Get compatibility mode setting.
    
    兼容性模式：启用后所有system消息全部转换成user，停用system_instructions。
    该选项可能会降低模型理解能力，但是能避免流式空回的情况。
    
    Environment variable: COMPATIBILITY_MODE
    TOML config key: compatibility_mode_enabled
    Default: True
    """
    return False







# MongoDB Configuration
async def get_mongodb_uri() -> str:
    """
    Get MongoDB connection URI setting.
    
    MongoDB连接URI，用于分布式部署时的数据存储。
    设置此项后将不再使用本地/creds和TOML文件。
    
    Environment variable: MONGODB_URI
    TOML config key: mongodb_uri
    Default: None (使用本地文件存储)
    
    示例格式:
    - mongodb://username:password@localhost:27017/database
    - mongodb+srv://username:password@cluster.mongodb.net/database
    """
    return str(await get_config_value("mongodb_uri", "", "MONGODB_URI"))

async def get_mongodb_database() -> str:
    """
    Get MongoDB database name setting.
    
    MongoDB数据库名称。
    
    Environment variable: MONGODB_DATABASE
    TOML config key: mongodb_database
    Default: gcli2api
    """
    return str(await get_config_value("mongodb_database", "gcli2api", "MONGODB_DATABASE"))

async def is_mongodb_mode() -> bool:
    """
    Check if MongoDB mode is enabled.
    
    检查是否启用了MongoDB模式。
    如果配置了MongoDB URI，则启用MongoDB模式，不再使用本地文件。
    
    Returns:
        bool: True if MongoDB mode is enabled, False otherwise
    """
    mongodb_uri = await get_mongodb_uri()
    return bool(mongodb_uri and mongodb_uri.strip())

# AssemblyAI Configuration
async def get_assembly_endpoint() -> str:
    """
    Get AssemblyAI LLM Gateway endpoint setting.
    
    Environment variable: ASSEMBLY_ENDPOINT
    TOML config key: assembly_endpoint
    Default: https://llm-gateway.assemblyai.com/v1/chat/completions
    """
    return str(await get_config_value("assembly_endpoint", "https://llm-gateway.assemblyai.com/v1/chat/completions", "ASSEMBLY_ENDPOINT"))

async def get_assembly_api_key() -> str:
    """
    Get AssemblyAI API key for upstream authentication.
    
    Environment variable: ASSEMBLY_API_KEY
    TOML config key: assembly_api_key
    Default: empty string
    """
    return str(await get_config_value("assembly_api_key", "", "ASSEMBLY_API_KEY"))

async def get_assembly_api_keys() -> list:
    value = await get_config_value("assembly_api_keys", None, "ASSEMBLY_API_KEYS")
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        parts = [p.strip() for p in value.split(",") if p.strip()]
        return parts
    single = await get_config_value("assembly_api_key", "", "ASSEMBLY_API_KEY")
    return [single] if single else []

async def get_enable_real_streaming() -> bool:
    """
    Get real streaming enabled setting.

    启用真实流式模式。AssemblyAI LLM Gateway 已经原生支持原生流式（含 tool calling
    与 prompt caching usage），默认为 True；如遇个别上游模型异常，可临时改回 False
    使用假流式兜底。

    Environment variable: ENABLE_REAL_STREAMING
    TOML config key: enable_real_streaming
    Default: True (use native streaming)
    """
    env_value = os.getenv("ENABLE_REAL_STREAMING")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")
    return bool(await get_config_value("enable_real_streaming", True))


async def get_tool_debug_logs_enabled() -> bool:
    """
    Get tool debug logging switch.

    控制是否输出 [TOOL_DEBUG] 详细请求/响应日志（可能包含较大报文）。

    Environment variable: ENABLE_TOOL_DEBUG_LOGS
    TOML config key: enable_tool_debug_logs
    Default: False
    """
    env_value = os.getenv("ENABLE_TOOL_DEBUG_LOGS")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")
    return bool(await get_config_value("enable_tool_debug_logs", False))


async def get_stream_keepalive_seconds() -> float:
    """
    Get streaming keepalive interval in seconds.

    仅用于真实流式传输。每隔 N 秒向客户端发送 SSE keepalive 注释帧，
    防止中间层空闲超时。设置为 0 表示禁用。

    Environment variable: STREAM_KEEPALIVE_SECONDS
    TOML config key: stream_keepalive_seconds
    Default: 0 (disabled)
    """
    env_value = os.getenv("STREAM_KEEPALIVE_SECONDS")
    if env_value is not None and env_value != "":
        try:
            return max(0.0, float(env_value))
        except ValueError:
            pass
    return float(await get_config_value("stream_keepalive_seconds", 0))


async def get_stream_bootstrap_retries() -> int:
    """
    Get bootstrap retries for real streaming before first chunk.

    首包前允许重试次数。仅在尚未向客户端发送首个有效 chunk 时生效；
    首包后不会自动重试，避免重复输出。

    Environment variable: STREAM_BOOTSTRAP_RETRIES
    TOML config key: stream_bootstrap_retries
    Default: 1
    """
    env_value = os.getenv("STREAM_BOOTSTRAP_RETRIES")
    if env_value is not None and env_value != "":
        try:
            return max(0, int(env_value))
        except ValueError:
            pass
    return int(await get_config_value("stream_bootstrap_retries", 1))


# ============================================================================
# Account Preload Queue Configuration
# ============================================================================

async def get_preload_max_concurrent() -> int:
    """
    Get preload queue max concurrent tasks setting.
    
    预加载队列最大并发任务数。
    
    Environment variable: PRELOAD_MAX_CONCURRENT
    TOML config key: preload_max_concurrent
    Default: 2
    """
    env_value = os.getenv("PRELOAD_MAX_CONCURRENT")
    if env_value:
        try:
            return max(1, int(env_value))
        except ValueError:
            pass
    return int(await get_config_value("preload_max_concurrent", 2))


async def get_preload_refresh_interval() -> float:
    """
    Get preload queue refresh interval setting.
    
    预加载队列自动刷新间隔（秒）。
    
    Environment variable: PRELOAD_REFRESH_INTERVAL
    TOML config key: preload_refresh_interval
    Default: 300.0 (5 minutes)
    """
    env_value = os.getenv("PRELOAD_REFRESH_INTERVAL")
    if env_value:
        try:
            return max(10.0, float(env_value))
        except ValueError:
            pass
    return float(await get_config_value("preload_refresh_interval", 300.0))


async def get_preload_cache_ttl() -> float:
    """
    Get preload cache TTL setting.
    
    预加载缓存 TTL（秒）。
    
    Environment variable: PRELOAD_CACHE_TTL
    TOML config key: preload_cache_ttl
    Default: 300.0 (5 minutes)
    """
    env_value = os.getenv("PRELOAD_CACHE_TTL")
    if env_value:
        try:
            return max(10.0, float(env_value))
        except ValueError:
            pass
    return float(await get_config_value("preload_cache_ttl", 300.0))


async def get_preload_max_cached_accounts() -> int:
    """
    Get preload max cached accounts setting.
    
    预加载缓存最大账户数。
    
    Environment variable: PRELOAD_MAX_CACHED_ACCOUNTS
    TOML config key: preload_max_cached_accounts
    Default: 20
    """
    env_value = os.getenv("PRELOAD_MAX_CACHED_ACCOUNTS")
    if env_value:
        try:
            return max(1, int(env_value))
        except ValueError:
            pass
    return int(await get_config_value("preload_max_cached_accounts", 20))


async def get_preload_config() -> dict:
    """
    Get all preload queue configuration as a dictionary.
    
    返回预加载队列的所有配置。
    """
    return {
        "max_concurrent": await get_preload_max_concurrent(),
        "refresh_interval": await get_preload_refresh_interval(),
        "cache_ttl": await get_preload_cache_ttl(),
        "max_cached_accounts": await get_preload_max_cached_accounts(),
    }

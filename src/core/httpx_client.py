"""
通用 HTTP 客户端模块。

进程级共享一个 ``httpx.AsyncClient``（连接池复用 + HTTP/2 + 有界超时），
避免每个上游请求都重新建立 TCP/TLS 连接——这是吞吐与延迟的关键优化。

要点：
- ``get_shared_client()`` 返回懒加载的单例；代理配置变化时自动重建。
- 共享客户端**不应**被 ``aclose()`` 在单次请求里关闭（流式只关闭响应/流上下文，
  连接会归还连接池）。
- 默认超时有界（不再 ``timeout=None``），``read`` 放宽以容纳慢速生成。
- 进程退出时由 ``close_shared_client()`` 统一关闭（web.py lifespan）。
"""
import asyncio
import httpx
from typing import Optional, Dict, Any, AsyncGenerator
from contextlib import asynccontextmanager

from config import get_proxy_config
from log import log

# HTTP/2 best-effort：h2 不可用时自动降级为 HTTP/1.1
try:  # pragma: no cover - 取决于运行环境是否安装 h2
    import h2  # noqa: F401

    _HTTP2_AVAILABLE = True
except Exception:  # noqa: BLE001
    _HTTP2_AVAILABLE = False

# 默认超时：connect/write/pool 收紧，read 放宽到 300s 容纳长文本生成；
# 关键是不再无限等待（旧代码 timeout=None 会让上游半挂时永久占住请求）。
_DEFAULT_TIMEOUT = httpx.Timeout(connect=15.0, read=300.0, write=60.0, pool=15.0)
_DEFAULT_LIMITS = httpx.Limits(
    max_connections=200, max_keepalive_connections=50, keepalive_expiry=30.0
)

_shared_client: Optional[httpx.AsyncClient] = None
_shared_client_proxy: Optional[str] = None
_client_lock = asyncio.Lock()


def _build_client(proxy: Optional[str]) -> httpx.AsyncClient:
    kwargs: Dict[str, Any] = {
        "timeout": _DEFAULT_TIMEOUT,
        "limits": _DEFAULT_LIMITS,
        "follow_redirects": True,
    }
    if _HTTP2_AVAILABLE:
        kwargs["http2"] = True
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.AsyncClient(**kwargs)


async def get_shared_client() -> httpx.AsyncClient:
    """返回进程级共享的 AsyncClient。代理变化或被关闭时自动重建。"""
    global _shared_client, _shared_client_proxy
    proxy = await get_proxy_config()
    client = _shared_client
    if client is not None and _shared_client_proxy == proxy and not client.is_closed:
        return client
    async with _client_lock:
        # double-check
        client = _shared_client
        if client is not None and _shared_client_proxy == proxy and not client.is_closed:
            return client
        old = _shared_client
        _shared_client = _build_client(proxy)
        _shared_client_proxy = proxy
        if old is not None and not old.is_closed:
            try:
                await old.aclose()
            except Exception:  # noqa: BLE001
                pass
        log.debug(
            f"共享 HTTP 客户端已构建 (http2={_HTTP2_AVAILABLE}, proxy={'yes' if proxy else 'no'})"
        )
        return _shared_client


async def close_shared_client() -> None:
    """进程退出时优雅关闭共享客户端。"""
    global _shared_client
    async with _client_lock:
        client = _shared_client
        _shared_client = None
        if client is not None and not client.is_closed:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass


class HttpxClientManager:
    """兼容旧调用约定的薄包装；底层统一走共享客户端。"""

    async def get_client_kwargs(self, timeout: float = 30.0, **kwargs) -> Dict[str, Any]:
        """保留接口兼容；现在仅用于少数仍需独立客户端的场景。"""
        client_kwargs = {"timeout": timeout, **kwargs}
        current_proxy_config = await get_proxy_config()
        if current_proxy_config:
            client_kwargs["proxy"] = current_proxy_config
        return client_kwargs

    @asynccontextmanager
    async def get_client(
        self, timeout: float = 30.0, **kwargs
    ) -> AsyncGenerator[httpx.AsyncClient, None]:
        """产出共享客户端；**不**在退出时关闭它（连接归还连接池）。"""
        client = await get_shared_client()
        yield client

    @asynccontextmanager
    async def get_streaming_client(
        self, timeout: float = None, **kwargs
    ) -> AsyncGenerator[httpx.AsyncClient, None]:
        """流式同样复用共享客户端；调用方只需关闭 stream 上下文/响应，勿关客户端。"""
        client = await get_shared_client()
        yield client


# 全局HTTP客户端管理器实例
http_client = HttpxClientManager()


# 通用的异步方法（per-call timeout 透传到请求层，保留各调用方的超时意图）
async def get_async(url: str, headers: Optional[Dict[str, str]] = None,
                   timeout: float = 30.0, **kwargs) -> httpx.Response:
    client = await get_shared_client()
    return await client.get(url, headers=headers, timeout=timeout)


async def post_async(url: str, data: Any = None, json: Any = None,
                    headers: Optional[Dict[str, str]] = None,
                    timeout: float = 30.0, **kwargs) -> httpx.Response:
    client = await get_shared_client()
    return await client.post(url, data=data, json=json, headers=headers, timeout=timeout)


async def put_async(url: str, data: Any = None, json: Any = None,
                   headers: Optional[Dict[str, str]] = None,
                   timeout: float = 30.0, **kwargs) -> httpx.Response:
    client = await get_shared_client()
    return await client.put(url, data=data, json=json, headers=headers, timeout=timeout)


async def delete_async(url: str, headers: Optional[Dict[str, str]] = None,
                      timeout: float = 30.0, **kwargs) -> httpx.Response:
    client = await get_shared_client()
    return await client.delete(url, headers=headers, timeout=timeout)


# 错误处理装饰器
def handle_http_errors(func):
    """HTTP错误处理装饰器"""
    async def wrapper(*args, **kwargs):
        try:
            response = await func(*args, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as e:
            log.error(f"HTTP错误: {e.response.status_code} - {e.response.text}")
            raise
        except httpx.RequestError as e:
            log.error(f"请求错误: {e}")
            raise
        except Exception as e:
            log.error(f"未知错误: {e}")
            raise
    return wrapper


@handle_http_errors
async def safe_get_async(url: str, headers: Optional[Dict[str, str]] = None,
                        timeout: float = 30.0, **kwargs) -> httpx.Response:
    return await get_async(url, headers=headers, timeout=timeout, **kwargs)


@handle_http_errors
async def safe_post_async(url: str, data: Any = None, json: Any = None,
                         headers: Optional[Dict[str, str]] = None,
                         timeout: float = 30.0, **kwargs) -> httpx.Response:
    return await post_async(url, data=data, json=json, headers=headers, timeout=timeout, **kwargs)


@handle_http_errors
async def safe_put_async(url: str, data: Any = None, json: Any = None,
                        headers: Optional[Dict[str, str]] = None,
                        timeout: float = 30.0, **kwargs) -> httpx.Response:
    return await put_async(url, data=data, json=json, headers=headers, timeout=timeout, **kwargs)


@handle_http_errors
async def safe_delete_async(url: str, headers: Optional[Dict[str, str]] = None,
                           timeout: float = 30.0, **kwargs) -> httpx.Response:
    return await delete_async(url, headers=headers, timeout=timeout, **kwargs)


# 流式请求支持
class StreamingContext:
    """流式请求上下文管理器（复用共享客户端，退出时只关闭响应/流，不关客户端）。"""

    def __init__(self, client: httpx.AsyncClient, stream_context):
        self.client = client
        self.stream_context = stream_context
        self.response = None

    async def __aenter__(self):
        self.response = await self.stream_context.__aenter__()
        return self.response

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # 只关闭 stream 上下文（释放连接回连接池），共享客户端保持存活
        if self.stream_context:
            await self.stream_context.__aexit__(exc_type, exc_val, exc_tb)


@asynccontextmanager
async def get_streaming_post_context(url: str, data: Any = None, json: Any = None,
                                   headers: Optional[Dict[str, str]] = None,
                                   timeout: float = None, **kwargs) -> AsyncGenerator[StreamingContext, None]:
    """获取流式POST请求的上下文管理器（复用共享客户端）。"""
    client = await get_shared_client()
    stream_ctx = client.stream("POST", url, data=data, json=json, headers=headers)
    streaming_context = StreamingContext(client, stream_ctx)
    yield streaming_context


async def create_streaming_client_with_kwargs(**kwargs) -> httpx.AsyncClient:
    """返回共享客户端用于流式处理。

    注意：调用方拿到的是**共享**客户端，**不要**对其调用 ``aclose()``；
    只需关闭 ``client.stream(...)`` 返回的 stream 上下文/响应即可。
    """
    return await get_shared_client()

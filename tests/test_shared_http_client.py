"""回归测试：上游 HTTP 客户端必须是进程级共享单例（连接池复用），且超时有界。

历史问题：每个上游请求都新建并关闭一个 httpx.AsyncClient，导致每请求都重做
TCP/TLS 握手、无连接复用、无 HTTP/2，且流式 timeout=None 会无限等待。
"""
import asyncio

import httpx

from src.core import httpx_client as hc


def test_shared_client_is_singleton():
    async def _run():
        await hc.close_shared_client()
        c1 = await hc.get_shared_client()
        c2 = await hc.get_shared_client()
        assert c1 is c2, "重复获取应返回同一个共享客户端（连接池复用）"
        assert not c1.is_closed
        return c1

    asyncio.run(_run())


def test_shared_client_timeout_is_bounded():
    async def _run():
        await hc.close_shared_client()
        c = await hc.get_shared_client()
        # 不再是 None（无限等待）：read 超时必须是有限值
        assert c.timeout.read is not None
        assert c.timeout.connect is not None
        assert c.timeout.read <= 600.0

    asyncio.run(_run())


def test_streaming_helper_returns_shared_client():
    async def _run():
        await hc.close_shared_client()
        shared = await hc.get_shared_client()
        stream_client = await hc.create_streaming_client_with_kwargs()
        assert stream_client is shared, (
            "流式应复用共享客户端，调用方不得对其 aclose()"
        )

    asyncio.run(_run())


def test_get_client_context_does_not_close_shared():
    async def _run():
        await hc.close_shared_client()
        async with hc.http_client.get_client(timeout=30.0) as client:
            assert isinstance(client, httpx.AsyncClient)
            assert not client.is_closed
        # 退出 context 后共享客户端仍存活
        assert not client.is_closed
        assert (await hc.get_shared_client()) is client

    asyncio.run(_run())

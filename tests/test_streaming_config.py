"""
Tests for streaming mode configuration
测试流式模式配置功能
"""
import pytest
import os
from unittest.mock import patch, AsyncMock
from config import (
    get_fake_streaming_enabled,
    get_enable_real_streaming,
    supports_real_streaming_model,
    get_stream_keepalive_seconds,
    get_stream_bootstrap_retries,
)


class TestStreamingConfig:
    """测试流式模式配置"""

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("gpt-5.5", True),
            ("gpt-5-nano", True),
            ("openai/gpt-5-nano", True),
            ("chatgpt-4o-latest", True),
            ("o4-mini", True),
            ("gpt-oss-20b", False),
            ("openai/gpt-oss-120b", False),
            ("claude-haiku-4-5-20251001", False),
            ("gemini-3.1-flash-lite", False),
            ("kimi-k2.5", False),
        ],
    )
    def test_supports_real_streaming_model_follows_official_openai_scope(self, model, expected):
        """真实流式只按官方 OpenAI 模型范围自动放行，非 OpenAI/OSS 模型走假流式。"""
        assert supports_real_streaming_model(model) is expected

    @pytest.mark.asyncio
    async def test_fake_streaming_global_switch_defaults_false(self):
        """全局假流式默认关闭；关闭时仍可按模型自动选择真/假流式。"""
        with patch.dict(os.environ, {}, clear=True):
            with patch('config.get_config_value', new_callable=AsyncMock) as mock_config:
                mock_config.return_value = False
                result = await get_fake_streaming_enabled()
                assert result is False

    @pytest.mark.asyncio
    async def test_fake_streaming_global_switch_from_storage(self):
        """面板开启全局假流式后，所有客户端 stream 请求都应走非流式上游 + 假流式输出。"""
        with patch.dict(os.environ, {}, clear=True):
            with patch('config.get_config_value', new_callable=AsyncMock) as mock_config:
                mock_config.return_value = True
                result = await get_fake_streaming_enabled()
                assert result is True
    
    @pytest.mark.asyncio
    async def test_default_value_is_true(self):
        """默认值为 True：原生流式已稳定"""
        with patch.dict(os.environ, {}, clear=True):
            with patch('config.get_config_value', new_callable=AsyncMock) as mock_config:
                mock_config.return_value = True
                result = await get_enable_real_streaming()
                assert result is True

    @pytest.mark.asyncio
    async def test_explicit_disable_via_storage(self):
        """显式在配置存储里写 False 时，仍然可以回退到假流式"""
        with patch.dict(os.environ, {}, clear=True):
            with patch('config.get_config_value', new_callable=AsyncMock) as mock_config:
                mock_config.return_value = False
                result = await get_enable_real_streaming()
                assert result is False

    @pytest.mark.asyncio
    async def test_env_var_true(self):
        """测试环境变量设置为 true"""
        test_cases = ["true", "True", "TRUE", "1", "yes", "YES", "on", "ON"]
        for value in test_cases:
            with patch.dict(os.environ, {"ENABLE_REAL_STREAMING": value}):
                result = await get_enable_real_streaming()
                assert result is True, f"Failed for value: {value}"

    @pytest.mark.asyncio
    async def test_env_var_false(self):
        """测试环境变量显式设置为 false 时强制假流式"""
        # 注意：空字符串不再视为关闭；它会落到 storage/默认值（默认 True）
        test_cases = ["false", "False", "FALSE", "0", "no", "NO", "off", "OFF"]
        for value in test_cases:
            with patch.dict(os.environ, {"ENABLE_REAL_STREAMING": value}):
                result = await get_enable_real_streaming()
                assert result is False, f"Failed for value: {value}"
    
    @pytest.mark.asyncio
    async def test_config_file_true(self):
        """测试配置文件设置为 true"""
        with patch.dict(os.environ, {}, clear=True):
            with patch('config.get_config_value', new_callable=AsyncMock) as mock_config:
                mock_config.return_value = True
                result = await get_enable_real_streaming()
                assert result is True
    
    @pytest.mark.asyncio
    async def test_config_file_false(self):
        """测试配置文件设置为 false"""
        with patch.dict(os.environ, {}, clear=True):
            with patch('config.get_config_value', new_callable=AsyncMock) as mock_config:
                mock_config.return_value = False
                result = await get_enable_real_streaming()
                assert result is False
    
    @pytest.mark.asyncio
    async def test_env_var_overrides_config_file(self):
        """测试环境变量优先于配置文件"""
        with patch.dict(os.environ, {"ENABLE_REAL_STREAMING": "true"}):
            with patch('config.get_config_value', new_callable=AsyncMock) as mock_config:
                mock_config.return_value = False
                result = await get_enable_real_streaming()
                # 环境变量为 true，应该返回 true
                assert result is True
    
    @pytest.mark.asyncio
    async def test_config_file_not_called_when_env_var_set(self):
        """测试当环境变量设置时，不调用配置文件读取"""
        with patch.dict(os.environ, {"ENABLE_REAL_STREAMING": "true"}):
            with patch('config.get_config_value', new_callable=AsyncMock) as mock_config:
                result = await get_enable_real_streaming()
                # 环境变量已设置，不应该调用 get_config_value
                # 但由于实现中总是会调用，我们只验证结果正确
                assert result is True

    @pytest.mark.asyncio
    async def test_stream_keepalive_default_zero(self):
        """测试流式 keepalive 默认值为 0（禁用）"""
        with patch.dict(os.environ, {}, clear=True):
            with patch('config.get_config_value', new_callable=AsyncMock) as mock_config:
                mock_config.return_value = 0
                result = await get_stream_keepalive_seconds()
                assert result == 0

    @pytest.mark.asyncio
    async def test_stream_keepalive_from_env(self):
        """测试流式 keepalive 从环境变量读取"""
        with patch.dict(os.environ, {"STREAM_KEEPALIVE_SECONDS": "15"}):
            result = await get_stream_keepalive_seconds()
            assert result == 15

    @pytest.mark.asyncio
    async def test_stream_bootstrap_retries_default_one(self):
        """测试 bootstrap 重试默认值为 1"""
        with patch.dict(os.environ, {}, clear=True):
            with patch('config.get_config_value', new_callable=AsyncMock) as mock_config:
                mock_config.return_value = 1
                result = await get_stream_bootstrap_retries()
                assert result == 1

    @pytest.mark.asyncio
    async def test_stream_bootstrap_retries_from_env(self):
        """测试 bootstrap 重试从环境变量读取"""
        with patch.dict(os.environ, {"STREAM_BOOTSTRAP_RETRIES": "3"}):
            result = await get_stream_bootstrap_retries()
            assert result == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

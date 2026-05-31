"""
AssemblyAI Gateway 原生扩展字段透传测试。

验证 send_assembly_request 上游白名单 _collect_passthrough_params 会把嵌套
reasoning / fallbacks / fallback_config / post_processing_steps / transcript_id
等原生扩展原样透传给网关（而不是在构建上游 payload 时被白名单丢弃）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.assembly_client import (
    _collect_passthrough_params,
    _UPSTREAM_PASSTHROUGH_KEYS,
)
from src.models.models import ChatCompletionRequest


def test_passthrough_keys_include_gateway_native_extensions():
    for key in ("reasoning", "fallbacks", "fallback_config",
                "post_processing_steps", "transcript_id"):
        assert key in _UPSTREAM_PASSTHROUGH_KEYS, f"{key} 应在上游透传白名单中"


def test_collect_passthrough_forwards_gateway_native_extensions():
    req = ChatCompletionRequest(
        model="gpt-5.5",
        messages=[{"role": "user", "content": "hi"}],
        reasoning={"effort": "high", "max_tokens": 2048},
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "x", "schema": {"type": "object"}, "strict": True},
        },
        fallbacks=["gpt-5", "gpt-4.1"],
        fallback_config={"retry": True, "depth": 1},
        post_processing_steps=[{"type": "json-repair"}],
        transcript_id="abc123",
    )

    out = _collect_passthrough_params(req)

    assert out["reasoning"] == {"effort": "high", "max_tokens": 2048}
    assert out["response_format"]["type"] == "json_schema"
    assert out["fallbacks"] == ["gpt-5", "gpt-4.1"]
    assert out["fallback_config"] == {"retry": True, "depth": 1}
    assert out["post_processing_steps"] == [{"type": "json-repair"}]
    assert out["transcript_id"] == "abc123"


def test_collect_passthrough_omits_unset_fields():
    req = ChatCompletionRequest(
        model="gpt-4.1",
        messages=[{"role": "user", "content": "hi"}],
    )

    out = _collect_passthrough_params(req)

    for key in ("reasoning", "fallbacks", "fallback_config",
                "post_processing_steps", "transcript_id"):
        assert key not in out

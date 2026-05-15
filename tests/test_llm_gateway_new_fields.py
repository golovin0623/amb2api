"""
Tests for AssemblyAI LLM Gateway 2026-04 关 OpenAI 兼容侧新字段透传：

请求侧（ChatCompletionRequest 接收 + assembly_client 白名单透传到上游 payload）：
- ``stream_options``                 OpenAI 标准 - 流式末尾 usage chunk
- ``parallel_tool_calls``           OpenAI 标准 - 控制并行 tool_calls
- ``max_completion_tokens``         GPT-5 / o-series 正式字段
- ``reasoning_effort``              GPT-5 - "none" / "minimal" / "low" / "medium" / "high" / "xhigh"
- ``verbosity``                     GPT-5 - "low" / "medium" / "high"

响应侧：
- ``completion_tokens_details.reasoning_tokens`` 由 ``assembly_response_to_openai``
  原样透传到 OpenAI 形状响应。
"""
from src.models.models import OpenAIChatCompletionRequest
from src.transform.openai_transfer import assembly_response_to_openai


# ---- 请求字段 -------------------------------------------------------------

def _minimal_req(**extra):
    return OpenAIChatCompletionRequest(
        model="gpt-5",
        messages=[{"role": "user", "content": "hi"}],
        **extra,
    )


def test_chat_request_accepts_stream_options():
    req = _minimal_req(stream=True, stream_options={"include_usage": True})
    assert req.stream_options == {"include_usage": True}


def test_chat_request_accepts_parallel_tool_calls():
    req = _minimal_req(parallel_tool_calls=False)
    assert req.parallel_tool_calls is False
    req2 = _minimal_req(parallel_tool_calls=True)
    assert req2.parallel_tool_calls is True


def test_chat_request_accepts_max_completion_tokens():
    req = _minimal_req(max_completion_tokens=4096)
    assert req.max_completion_tokens == 4096


def test_chat_request_accepts_reasoning_effort_levels():
    for level in ("none", "minimal", "low", "medium", "high", "xhigh"):
        req = _minimal_req(reasoning_effort=level)
        assert req.reasoning_effort == level


def test_chat_request_accepts_verbosity_levels():
    for level in ("low", "medium", "high"):
        req = _minimal_req(verbosity=level)
        assert req.verbosity == level


def test_chat_request_omits_new_fields_when_unset():
    req = _minimal_req()
    assert req.stream_options is None
    assert req.parallel_tool_calls is None
    assert req.max_completion_tokens is None
    assert req.reasoning_effort is None
    assert req.verbosity is None


def test_assembly_client_passthrough_whitelist_includes_new_keys():
    """白名单是纯字符串 list，直接读源码确保 5 个新键都在表里。"""
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "src" / "services" / "assembly_client.py"
    text = src.read_text()
    for key in (
        '"stream_options"',
        '"parallel_tool_calls"',
        '"max_completion_tokens"',
        '"reasoning_effort"',
        '"verbosity"',
    ):
        assert key in text, f"{key} missing from assembly_client.py passthrough whitelist"


# ---- 响应字段 -------------------------------------------------------------

def _gateway_response_with_reasoning_tokens(reasoning_tokens: int = 256):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "gpt-5",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 280,
            "total_tokens": 380,
            "completion_tokens_details": {
                "reasoning_tokens": reasoning_tokens,
                "accepted_prediction_tokens": 0,
                "rejected_prediction_tokens": 0,
                "audio_tokens": 0,
            },
        },
    }


def test_assembly_response_passes_through_completion_tokens_details():
    raw = _gateway_response_with_reasoning_tokens(reasoning_tokens=512)
    out = assembly_response_to_openai(raw, "gpt-5")
    usage = out["usage"]
    assert "completion_tokens_details" in usage
    assert usage["completion_tokens_details"]["reasoning_tokens"] == 512
    assert usage["completion_tokens_details"]["audio_tokens"] == 0


def test_assembly_response_omits_completion_tokens_details_when_absent():
    raw = _gateway_response_with_reasoning_tokens()
    raw["usage"].pop("completion_tokens_details", None)
    out = assembly_response_to_openai(raw, "gpt-5")
    usage = out["usage"]
    assert "completion_tokens_details" not in usage


def test_assembly_response_ignores_non_dict_completion_details():
    raw = _gateway_response_with_reasoning_tokens()
    raw["usage"]["completion_tokens_details"] = "not-a-dict"
    out = assembly_response_to_openai(raw, "gpt-5")
    assert "completion_tokens_details" not in out["usage"]

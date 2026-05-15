# Feature: api-key-management-enhancement, Property 29: 请求报文生成完整性
# Feature: api-key-management-enhancement, Property 30: 自定义报文初始化
# Feature: api-key-management-enhancement, Property 31: 自定义报文验证
# Feature: api-key-management-enhancement, Property 32: 自定义报文错误阻止
"""
请求生成器属性测试
测试请求报文生成、验证和错误处理
"""
import pytest
import json
from hypothesis import given, strategies as st, settings, HealthCheck, assume

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.transform.request_generator import RequestGenerator, get_request_generator


class TestRequestGenerator:
    """请求生成器测试"""
    
    @pytest.fixture
    def generator(self):
        """创建请求生成器实例"""
        return RequestGenerator(
            endpoint="https://api.example.com/v1/chat/completions",
            api_key="sk-test-key-12345678"
        )
    
    # Feature: api-key-management-enhancement, Property 29: 请求报文生成完整性
    # **Validates: Requirements 9.1, 9.3, 9.4**
    @given(
        model=st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=("L", "N", "Pd"))),
        temperature=st.floats(min_value=0, max_value=2),
        max_tokens=st.integers(min_value=1, max_value=4096),
        top_p=st.floats(min_value=0, max_value=1)
    )
    @settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_request_generation_completeness(self, generator, model, temperature, max_tokens, top_p):
        """测试请求报文生成完整性"""
        params = {
            "model": model,
            "messages": [{"role": "user", "content": "Hello"}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": top_p,
        }
        
        preview = generator.generate_request_preview(params)
        
        # 验证必需字段存在
        assert "method" in preview, "Preview should contain 'method'"
        assert "url" in preview, "Preview should contain 'url'"
        assert "headers" in preview, "Preview should contain 'headers'"
        assert "body" in preview, "Preview should contain 'body'"
        assert "body_json" in preview, "Preview should contain 'body_json'"
        
        # 验证方法和 URL
        assert preview["method"] == "POST", "Method should be POST"
        assert preview["url"] == generator._endpoint, "URL should match endpoint"
        
        # 验证请求头
        assert "Content-Type" in preview["headers"], "Headers should contain Content-Type"
        assert preview["headers"]["Content-Type"] == "application/json"
        assert "Authorization" in preview["headers"], "Headers should contain Authorization"
        
        # 验证请求体包含所有参数
        body = preview["body"]
        assert body["model"] == model, "Body should contain model"
        assert body["messages"] == params["messages"], "Body should contain messages"
        assert body["temperature"] == temperature, "Body should contain temperature"
        assert body["max_tokens"] == max_tokens, "Body should contain max_tokens"
        assert body["top_p"] == top_p, "Body should contain top_p"
        
        # 验证 JSON 格式正确
        parsed_json = json.loads(preview["body_json"])
        assert parsed_json == body, "body_json should match body"
    
    # Feature: api-key-management-enhancement, Property 30: 自定义报文初始化
    # **Validates: Requirements 10.2**
    @given(
        model=st.text(min_size=1, max_size=30, alphabet=st.characters(whitelist_categories=("L", "N"))),
        content=st.text(min_size=1, max_size=100)
    )
    @settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_custom_request_initialization(self, generator, model, content):
        """测试自定义报文初始化"""
        params = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
        }
        
        initial_request = generator.generate_initial_custom_request(params)
        
        # 验证生成的是有效 JSON
        parsed = json.loads(initial_request)
        
        # 验证包含必需字段
        assert "model" in parsed, "Initial request should contain model"
        assert "messages" in parsed, "Initial request should contain messages"
        
        # 验证值正确
        assert parsed["model"] == model, "Model should match"
        assert parsed["messages"][0]["content"] == content, "Content should match"
    
    # Feature: api-key-management-enhancement, Property 31: 自定义报文验证
    # **Validates: Requirements 10.3**
    @given(
        model=st.text(min_size=1, max_size=30, alphabet=st.characters(whitelist_categories=("L", "N"))),
        role=st.sampled_from(["user", "assistant", "system"]),
        content=st.text(min_size=1, max_size=100)
    )
    @settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_custom_request_validation(self, generator, model, role, content):
        """测试自定义报文验证"""
        # 有效请求
        valid_request = json.dumps({
            "model": model,
            "messages": [{"role": role, "content": content}]
        })
        
        is_valid, error = generator.validate_custom_request(valid_request)
        assert is_valid, f"Valid request should pass validation: {error}"
        assert error == "", "Error should be empty for valid request"
    
    # Feature: api-key-management-enhancement, Property 32: 自定义报文错误阻止
    # **Validates: Requirements 10.4**
    def test_custom_request_error_blocking(self, generator):
        """测试自定义报文错误阻止"""
        invalid_requests = [
            # 空请求
            ("", "Request body cannot be empty"),
            ("   ", "Request body cannot be empty"),
            
            # 无效 JSON
            ("{invalid}", "Invalid JSON format"),
            ("not json", "Invalid JSON format"),
            
            # 非对象
            ("[]", "Request body must be a JSON object"),
            ('"string"', "Request body must be a JSON object"),
            
            # 缺少必需字段
            ('{"messages": []}', "Missing required field: model"),
            ('{"model": "gpt-4"}', "Missing required field: messages"),
            
            # messages 格式错误
            ('{"model": "gpt-4", "messages": "not array"}', "Field 'messages' must be an array"),
            ('{"model": "gpt-4", "messages": []}', "Field 'messages' cannot be empty"),
            ('{"model": "gpt-4", "messages": ["not object"]}', "Message at index 0 must be an object"),
            ('{"model": "gpt-4", "messages": [{"content": "hi"}]}', "Message at index 0 missing 'role' field"),
            ('{"model": "gpt-4", "messages": [{"role": "user"}]}', "Message at index 0 missing 'content'"),
            
            # 参数类型错误
            ('{"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}], "temperature": "hot"}', 
             "Field 'temperature' must be a number"),
            ('{"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}], "temperature": 3}', 
             "Field 'temperature' must be a number between 0 and 2"),
            ('{"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}], "max_tokens": "many"}', 
             "Field 'max_tokens' must be a positive integer"),
            ('{"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 0}', 
             "Field 'max_tokens' must be a positive integer"),
        ]
        
        for request_json, expected_error_part in invalid_requests:
            is_valid, error = generator.validate_custom_request(request_json)
            assert not is_valid, f"Invalid request should fail: {request_json}"
            assert expected_error_part in error, \
                f"Error should contain '{expected_error_part}', got '{error}' for request: {request_json}"

    def test_custom_request_accepts_openai_reasoning_effort_values(self, generator):
        """自定义报文校验应接受当前 OpenAI 兼容 reasoning_effort 值。"""
        for level in ("none", "minimal", "low", "medium", "high", "xhigh"):
            request_json = json.dumps({
                "model": "gpt-5",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": level,
            })

            is_valid, error = generator.validate_custom_request(request_json)

            assert is_valid, f"{level} should pass validation: {error}"

    def test_custom_request_rejects_unknown_reasoning_effort(self, generator):
        request_json = json.dumps({
            "model": "gpt-5",
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "extreme",
        })

        is_valid, error = generator.validate_custom_request(request_json)

        assert not is_valid
        assert "none, minimal, low, medium, high, xhigh" in error
    
    def test_parse_custom_request(self, generator):
        """测试解析自定义请求"""
        # 有效请求
        valid_json = '{"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}'
        parsed = generator.parse_custom_request(valid_json)
        assert parsed is not None, "Valid request should be parsed"
        assert parsed["model"] == "gpt-4"
        
        # 无效请求
        invalid_json = '{"model": "gpt-4"}'  # 缺少 messages
        parsed = generator.parse_custom_request(invalid_json)
        assert parsed is None, "Invalid request should return None"
    
    def test_merge_with_defaults(self, generator):
        """测试与默认值合并"""
        defaults = {
            "model": "gpt-4",
            "temperature": 0.7,
            "max_tokens": 1000,
        }
        
        custom = {
            "model": "gpt-3.5-turbo",
            "messages": [{"role": "user", "content": "hi"}],
        }
        
        merged = generator.merge_with_defaults(custom, defaults)
        
        # 自定义值应该覆盖默认值
        assert merged["model"] == "gpt-3.5-turbo", "Custom model should override default"
        
        # 默认值应该保留
        assert merged["temperature"] == 0.7, "Default temperature should be preserved"
        assert merged["max_tokens"] == 1000, "Default max_tokens should be preserved"
        
        # 新字段应该添加
        assert merged["messages"] == custom["messages"], "Custom messages should be added"
    
    def test_mask_key(self, generator):
        """测试密钥脱敏"""
        # 短密钥
        assert generator._mask_key("abc") == "ab***"
        assert generator._mask_key("abcdefgh") == "ab***"
        
        # 长密钥
        assert generator._mask_key("sk-1234567890abcdef") == "sk-1...cdef"
        
        # 空密钥
        assert generator._mask_key("") == ""
        assert generator._mask_key(None) == ""


class TestRequestGeneratorIntegration:
    """请求生成器集成测试"""
    
    def test_global_request_generator(self):
        """测试全局请求生成器实例"""
        generator1 = get_request_generator()
        generator2 = get_request_generator()
        
        # 应该返回同一个实例
        assert generator1 is generator2
    
    @given(
        params_list=st.lists(
            st.fixed_dictionaries({
                "model": st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N"))),
                "temperature": st.floats(min_value=0, max_value=2),
            }),
            min_size=1,
            max_size=10
        )
    )
    @settings(max_examples=20)
    def test_multiple_request_generations(self, params_list):
        """测试多次请求生成"""
        generator = RequestGenerator(
            endpoint="https://api.example.com/v1/chat/completions",
            api_key="sk-test"
        )
        
        for params in params_list:
            params["messages"] = [{"role": "user", "content": "test"}]
            
            preview = generator.generate_request_preview(params)
            
            # 验证每次生成都是独立的
            assert preview["body"]["model"] == params["model"]
            assert preview["body"]["temperature"] == params["temperature"]
            
            # 验证 JSON 可以解析
            parsed = json.loads(preview["body_json"])
            assert parsed == preview["body"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

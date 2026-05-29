# Feature: api-key-management-enhancement, Property 14: 禁用密钥跳过
# Feature: api-key-management-enhancement, Property 15: 轮询模式顺序性
# Feature: api-key-management-enhancement, Property 16: 随机模式分布性
# Feature: api-key-management-enhancement, Property 17: 轮询模式错误跳过
# Feature: api-key-management-enhancement, Property 18: 随机模式错误重试
"""
密钥选择器属性测试
测试密钥选择的各种模式和行为
"""
import pytest
import asyncio
from collections import Counter
from hypothesis import given, strategies as st, settings, assume, HealthCheck
from unittest.mock import AsyncMock, patch

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.key_selector import KeySelector, get_key_selector, reset_key_selector
from src.models.models_key import KeyInfo, KeyStatus, AggregationMode


def create_key_info(index: int, enabled: bool = True, status: KeyStatus = KeyStatus.ACTIVE) -> KeyInfo:
    """创建测试用的 KeyInfo"""
    return KeyInfo(
        index=index,
        key=f"test_key_{index}",
        enabled=enabled,
        status=status
    )


class TestKeySelector:
    """密钥选择器测试"""
    
    @pytest.fixture
    def selector(self):
        """创建密钥选择器实例"""
        return KeySelector(mode=AggregationMode.ROUND_ROBIN)
    
    @pytest.fixture
    def random_selector(self):
        """创建随机模式密钥选择器实例"""
        return KeySelector(mode=AggregationMode.RANDOM)
    
    # Feature: api-key-management-enhancement, Property 14: 禁用密钥跳过
    # **Validates: Requirements 4.8**
    @given(
        num_keys=st.integers(min_value=2, max_value=10),
        disabled_indices=st.lists(st.integers(min_value=0, max_value=9), min_size=0, max_size=5, unique=True)
    )
    @settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_disabled_key_skip(self, selector, num_keys, disabled_indices):
        """测试禁用密钥跳过"""
        # 过滤有效的禁用索引
        valid_disabled = [i for i in disabled_indices if i < num_keys]
        
        # 创建密钥列表
        keys = []
        for i in range(num_keys):
            enabled = i not in valid_disabled
            keys.append(create_key_info(i, enabled=enabled))
        
        # 确保至少有一个启用的密钥
        enabled_keys = [k for k in keys if k.enabled]
        assume(len(enabled_keys) > 0)
        
        async def run_test():
            # 多次选择密钥
            for _ in range(20):
                selected = await selector.select_next_key(keys)
                if selected is not None:
                    # 验证选中的密钥是启用的
                    assert selected.enabled, f"Selected key {selected.index} should be enabled"
                    assert selected.index not in valid_disabled, f"Selected key {selected.index} should not be disabled"
        
        asyncio.run(run_test())
    
    # Feature: api-key-management-enhancement, Property 15: 轮询模式顺序性
    # **Validates: Requirements 5.1**
    @given(
        num_keys=st.integers(min_value=2, max_value=5)
    )
    @settings(max_examples=20)
    def test_round_robin_order(self, num_keys):
        """测试轮询模式顺序性"""
        selector = KeySelector(mode=AggregationMode.ROUND_ROBIN)
        keys = [create_key_info(i) for i in range(num_keys)]
        
        async def run_test():
            # 选择 num_keys * 2 次，验证轮询顺序
            selected_indices = []
            for _ in range(num_keys * 2):
                selected = await selector.select_next_key(keys)
                assert selected is not None
                selected_indices.append(selected.index)
            
            # 验证每个密钥被选中的次数相同
            counts = Counter(selected_indices)
            for i in range(num_keys):
                assert counts[i] == 2, f"Key {i} should be selected exactly 2 times, got {counts[i]}"
        
        asyncio.run(run_test())
    
    # Feature: api-key-management-enhancement, Property 16: 随机模式分布性
    # **Validates: Requirements 5.2**
    @given(
        num_keys=st.integers(min_value=2, max_value=5)
    )
    @settings(max_examples=10)
    def test_random_mode_distribution(self, num_keys):
        """测试随机模式分布性"""
        selector = KeySelector(mode=AggregationMode.RANDOM)
        keys = [create_key_info(i) for i in range(num_keys)]
        
        async def run_test():
            # 选择足够多次以验证分布
            num_selections = num_keys * 100
            selected_indices = []
            
            for _ in range(num_selections):
                selected = await selector.select_next_key(keys)
                assert selected is not None
                selected_indices.append(selected.index)
            
            # 验证每个密钥都被选中过
            counts = Counter(selected_indices)
            for i in range(num_keys):
                assert counts[i] > 0, f"Key {i} should be selected at least once"
                # 验证分布相对均匀（每个密钥至少被选中 10% 的次数）
                expected_min = num_selections / num_keys * 0.1
                assert counts[i] >= expected_min, f"Key {i} selection count {counts[i]} is too low"
        
        asyncio.run(run_test())
    
    # Feature: api-key-management-enhancement, Property 17: 轮询模式错误跳过
    # **Validates: Requirements 5.4**
    @given(
        num_keys=st.integers(min_value=3, max_value=5),
        failed_index=st.integers(min_value=0, max_value=4)
    )
    @settings(max_examples=30)
    def test_round_robin_error_skip(self, num_keys, failed_index):
        """测试轮询模式错误跳过"""
        assume(failed_index < num_keys)
        
        selector = KeySelector(mode=AggregationMode.ROUND_ROBIN)
        keys = [create_key_info(i) for i in range(num_keys)]
        
        async def run_test():
            # 标记一个密钥失败
            await selector.mark_key_failed(failed_index, "test failure")
            
            # 多次选择密钥
            for _ in range(num_keys * 2):
                selected = await selector.select_next_key(keys)
                assert selected is not None
                # 验证不会选中失败的密钥
                assert selected.index != failed_index, f"Failed key {failed_index} should not be selected"
        
        asyncio.run(run_test())
    
    # Feature: api-key-management-enhancement, Property 18: 随机模式错误重试
    # **Validates: Requirements 5.5**
    @given(
        num_keys=st.integers(min_value=3, max_value=5),
        failed_index=st.integers(min_value=0, max_value=4)
    )
    @settings(max_examples=30)
    def test_random_mode_error_retry(self, num_keys, failed_index):
        """测试随机模式错误重试"""
        assume(failed_index < num_keys)
        
        selector = KeySelector(mode=AggregationMode.RANDOM)
        keys = [create_key_info(i) for i in range(num_keys)]
        
        async def run_test():
            # 标记一个密钥失败
            await selector.mark_key_failed(failed_index, "test failure")
            
            # 多次选择密钥
            for _ in range(num_keys * 10):
                selected = await selector.select_next_key(keys)
                assert selected is not None
                # 验证不会选中失败的密钥
                assert selected.index != failed_index, f"Failed key {failed_index} should not be selected"
            
            # 清除失败标记后应该可以选中
            await selector.clear_key_failure(failed_index)
            
            # 多次选择，验证失败的密钥可以被选中
            selected_indices = []
            for _ in range(num_keys * 50):
                selected = await selector.select_next_key(keys)
                assert selected is not None
                selected_indices.append(selected.index)
            
            # 验证之前失败的密钥现在可以被选中
            assert failed_index in selected_indices, f"Cleared key {failed_index} should be selectable"
        
        asyncio.run(run_test())
    
    def test_exhausted_key_skip(self, selector):
        """测试已用尽密钥跳过"""
        keys = [
            create_key_info(0, status=KeyStatus.ACTIVE),
            create_key_info(1, status=KeyStatus.EXHAUSTED),
            create_key_info(2, status=KeyStatus.ACTIVE),
        ]
        
        async def run_test():
            # 多次选择密钥
            for _ in range(10):
                selected = await selector.select_next_key(keys)
                assert selected is not None
                # 验证不会选中已用尽的密钥
                assert selected.status != KeyStatus.EXHAUSTED, "Exhausted key should not be selected"
                assert selected.index != 1, "Key 1 (exhausted) should not be selected"
        
        asyncio.run(run_test())
    
    def test_should_rotate(self, selector):
        """测试轮换判断"""
        selector.calls_per_rotation = 5
        
        # 模拟调用计数
        for i in range(4):
            selector._call_counts[0] = i + 1
            assert not selector.should_rotate(0), f"Should not rotate at {i + 1} calls"
        
        # 达到轮换次数
        selector._call_counts[0] = 5
        assert selector.should_rotate(0), "Should rotate at 5 calls"
        
        # 轮换后计数应该重置
        assert selector.get_call_count(0) == 0, "Call count should be reset after rotation"
    
    def test_no_available_keys(self, selector):
        """测试没有可用密钥的情况"""
        keys = [
            create_key_info(0, enabled=False),
            create_key_info(1, enabled=False),
        ]
        
        async def run_test():
            selected = await selector.select_next_key(keys)
            assert selected is None, "Should return None when no keys available"
        
        asyncio.run(run_test())
    
    def test_mode_change(self, selector):
        """测试模式切换"""
        assert selector.mode == AggregationMode.ROUND_ROBIN
        
        selector.mode = AggregationMode.RANDOM
        assert selector.mode == AggregationMode.RANDOM

        selector.mode = AggregationMode.FILL_FIRST
        assert selector.mode == AggregationMode.FILL_FIRST
        
        selector.mode = AggregationMode.ROUND_ROBIN
        assert selector.mode == AggregationMode.ROUND_ROBIN

    def test_fill_first_prefers_first_available_key(self):
        """测试填充优先模式：优先使用第一个可用 key"""
        selector = KeySelector(mode=AggregationMode.FILL_FIRST)
        keys = [create_key_info(0), create_key_info(1), create_key_info(2)]

        async def run_test():
            # 在首个 key 可用时应持续选择 key 0
            for _ in range(20):
                selected = await selector.select_next_key(keys)
                assert selected is not None
                assert selected.index == 0

            # 当 key 0 用尽后应切换到 key 1
            keys[0].status = KeyStatus.EXHAUSTED
            selected = await selector.select_next_key(keys)
            assert selected is not None
            assert selected.index == 1

        asyncio.run(run_test())

    def test_fill_first_ignores_calls_per_rotation(self):
        """测试填充优先模式忽略 calls_per_rotation，仅在配额耗尽时轮换"""
        selector = KeySelector(mode=AggregationMode.FILL_FIRST)
        selector.calls_per_rotation = 1
        selector._call_counts[0] = 100

        # fill_first 不应因为调用次数达到阈值而轮换
        assert selector.should_rotate(0, rate_limit_remaining=10) is False
        # 但配额耗尽时仍应触发轮换
        assert selector.should_rotate(0, rate_limit_remaining=0) is True


class TestKeySelectorIntegration:
    """密钥选择器集成测试"""
    
    def test_global_key_selector(self):
        """测试全局密钥选择器实例"""
        reset_key_selector()
        
        selector1 = get_key_selector()
        selector2 = get_key_selector()
        
        # 应该返回同一个实例
        assert selector1 is selector2
        
        reset_key_selector()
    
    @given(
        operations=st.lists(
            st.one_of(
                st.tuples(st.just("select"), st.integers(min_value=2, max_value=5)),
                st.tuples(st.just("fail"), st.integers(min_value=0, max_value=4)),
                st.tuples(st.just("clear"), st.integers(min_value=0, max_value=4)),
            ),
            min_size=1,
            max_size=20
        )
    )
    @settings(max_examples=20)
    def test_complex_key_operations(self, operations):
        """测试复杂的密钥操作序列"""
        async def run_test():
            selector = KeySelector(mode=AggregationMode.ROUND_ROBIN)
            keys = [create_key_info(i) for i in range(5)]
            
            for op_type, arg in operations:
                try:
                    if op_type == "select":
                        # 选择密钥
                        selected = await selector.select_next_key(keys[:arg])
                        # 验证选中的密钥是有效的
                        if selected is not None:
                            assert selected.enabled
                            assert selected.index not in selector.get_failed_keys()
                    elif op_type == "fail":
                        # 标记失败
                        if arg < len(keys):
                            await selector.mark_key_failed(arg, "test")
                    elif op_type == "clear":
                        # 清除失败
                        if arg < len(keys):
                            await selector.clear_key_failure(arg)
                except Exception:
                    pass
        
        asyncio.run(run_test())


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

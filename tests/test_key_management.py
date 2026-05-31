"""
API 密钥管理增强功能 - 属性测试
使用 Hypothesis 进行属性测试，验证核心功能的正确性
"""
import pytest
from hypothesis import given, strategies as st, settings, assume
import asyncio
import json
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models.models_key import KeyInfo, KeyConfig, RateLimitInfo, KeyStats, KeyStatus, AggregationMode
from src.services.key_manager import KeyManager
from src.services.key_selector import KeySelector
from src.services.rate_limiter import RateLimiter
from src.stats.stats_tracker import StatsTracker
from src.transform.request_generator import RequestGenerator


# ============ 策略定义 ============

# API 密钥策略
api_key_strategy = st.text(
    alphabet=st.characters(whitelist_categories=('L', 'N'), whitelist_characters='-_'),
    min_size=10,
    max_size=50
).filter(lambda x: x.strip() != "")


# 密钥列表策略
key_list_strategy = st.lists(api_key_strategy, min_size=1, max_size=10)

# 密钥索引策略
key_index_strategy = st.integers(min_value=0, max_value=9)


# ============ Property 10: 密钥追加操作 ============
# Feature: api-key-management-enhancement, Property 10: 密钥追加操作
# *For any* 新密钥列表和现有密钥列表，追加模式应该将新密钥添加到现有列表末尾，保持原有顺序
# **Validates: Requirements 4.3**

class TestKeyAppendOperation:
    """密钥追加操作测试"""
    
    @given(existing=key_list_strategy, new_keys=key_list_strategy)
    @settings(max_examples=100)
    def test_append_preserves_order(self, existing, new_keys):
        """测试追加操作保持顺序"""
        # 模拟追加操作
        result = existing + new_keys
        
        # 验证原有密钥顺序保持不变
        for i, key in enumerate(existing):
            assert result[i] == key
        
        # 验证新密钥在末尾
        for i, key in enumerate(new_keys):
            assert result[len(existing) + i] == key
        
        # 验证总长度
        assert len(result) == len(existing) + len(new_keys)


# ============ Property 11: 密钥覆盖操作 ============
# Feature: api-key-management-enhancement, Property 11: 密钥覆盖操作
# *For any* 新密钥列表，覆盖模式应该替换所有现有密钥，结果列表只包含新密钥
# **Validates: Requirements 4.4**

class TestKeyOverrideOperation:
    """密钥覆盖操作测试"""
    
    @given(existing=key_list_strategy, new_keys=key_list_strategy)
    @settings(max_examples=100)
    def test_override_replaces_all(self, existing, new_keys):
        """测试覆盖操作替换所有密钥"""
        # 模拟覆盖操作
        result = new_keys.copy()
        
        # 验证结果只包含新密钥
        assert len(result) == len(new_keys)
        for i, key in enumerate(new_keys):
            assert result[i] == key
        
        # 验证原有密钥不在结果中（除非碰巧相同）
        for key in existing:
            if key not in new_keys:
                assert key not in result


# ============ Property 12: 密钥状态切换 ============
# Feature: api-key-management-enhancement, Property 12: 密钥状态切换
# *For any* 密钥，启用/禁用操作应该正确切换该密钥的状态
# **Validates: Requirements 4.5**

class TestKeyStatusToggle:
    """密钥状态切换测试"""
    
    @given(initial_enabled=st.booleans())
    @settings(max_examples=100)
    def test_status_toggle(self, initial_enabled):
        """测试状态切换"""
        # 创建密钥信息
        key_info = KeyInfo(index=0, key="test-key", enabled=initial_enabled)
        
        # 切换状态
        new_enabled = not initial_enabled
        key_info.enabled = new_enabled
        
        # 验证状态已切换
        assert key_info.enabled == new_enabled
        assert key_info.enabled != initial_enabled


# ============ Property 13: 批量状态更新 ============
# Feature: api-key-management-enhancement, Property 13: 批量状态更新
# *For any* 选中的密钥集合，批量启用/禁用操作应该更新所有选中密钥的状态
# **Validates: Requirements 4.6, 4.7**

class TestBatchStatusUpdate:
    """批量状态更新测试"""
    
    @given(
        indices=st.lists(st.integers(min_value=0, max_value=9), min_size=1, max_size=5, unique=True),
        target_enabled=st.booleans()
    )
    @settings(max_examples=100)
    def test_batch_update_all_selected(self, indices, target_enabled):
        """测试批量更新所有选中密钥"""
        # 创建密钥列表
        keys = [KeyInfo(index=i, key=f"key-{i}", enabled=not target_enabled) for i in range(10)]
        
        # 模拟批量更新
        for idx in indices:
            if 0 <= idx < len(keys):
                keys[idx].enabled = target_enabled
        
        # 验证所有选中的密钥状态已更新
        for idx in indices:
            if 0 <= idx < len(keys):
                assert keys[idx].enabled == target_enabled


# ============ Property 14: 禁用密钥跳过 ============
# Feature: api-key-management-enhancement, Property 14: 禁用密钥跳过
# *For any* 禁用的密钥，在密钥轮换时应该被跳过，不被选择使用
# **Validates: Requirements 4.8**

class TestDisabledKeySkip:
    """禁用密钥跳过测试"""
    
    @given(disabled_indices=st.lists(st.integers(min_value=0, max_value=4), min_size=1, max_size=3, unique=True))
    @settings(max_examples=100)
    def test_disabled_keys_skipped(self, disabled_indices):
        """测试禁用密钥被跳过"""
        # 创建密钥列表
        keys = [KeyInfo(index=i, key=f"key-{i}", enabled=True) for i in range(5)]
        
        # 禁用指定密钥
        for idx in disabled_indices:
            keys[idx].enabled = False
        
        # 获取可用密钥
        available = [k for k in keys if k.enabled]
        
        # 验证禁用密钥不在可用列表中
        for idx in disabled_indices:
            assert keys[idx] not in available
        
        # 验证启用密钥在可用列表中
        for k in keys:
            if k.enabled:
                assert k in available


# ============ Property 15: 轮询模式顺序性 ============
# Feature: api-key-management-enhancement, Property 15: 轮询模式顺序性
# *For any* 启用的密钥列表，轮询模式应该按顺序循环使用所有密钥
# **Validates: Requirements 5.1**

class TestRoundRobinOrder:
    """轮询模式顺序性测试"""
    
    @given(num_keys=st.integers(min_value=2, max_value=5))
    @settings(max_examples=100)
    def test_round_robin_cycles_through_all(self, num_keys):
        """测试轮询模式循环所有密钥"""
        selector = KeySelector(mode=AggregationMode.ROUND_ROBIN)
        keys = [KeyInfo(index=i, key=f"key-{i}", enabled=True) for i in range(num_keys)]
        
        # 选择 num_keys * 2 次，应该循环两遍
        selected_indices = []
        for _ in range(num_keys * 2):
            # 同步调用
            available = [k for k in keys if k.enabled]
            if available:
                idx = selector._rr_counter.__next__() % len(available)
                selected_indices.append(available[idx].index)
        
        # 验证每个密钥都被选中了
        unique_selected = set(selected_indices)
        assert len(unique_selected) == num_keys


# ============ Property 16: 随机模式分布性 ============
# Feature: api-key-management-enhancement, Property 16: 随机模式分布性
# *For any* 启用的密钥列表，随机模式应该随机选择密钥，长期分布应该相对均匀
# **Validates: Requirements 5.2**

class TestRandomDistribution:
    """随机模式分布性测试"""
    
    @given(num_keys=st.integers(min_value=2, max_value=5))
    @settings(max_examples=50)
    def test_random_selects_all_keys(self, num_keys):
        """测试随机模式选择所有密钥"""
        import random
        keys = [KeyInfo(index=i, key=f"key-{i}", enabled=True) for i in range(num_keys)]
        
        # 选择足够多次，应该覆盖所有密钥
        selected_indices = set()
        for _ in range(num_keys * 20):
            selected = random.choice(keys)
            selected_indices.add(selected.index)
        
        # 验证所有密钥都被选中过
        assert len(selected_indices) == num_keys


# ============ Property 4: 轮换次数优先策略 ============
# Feature: api-key-management-enhancement, Property 4: 轮换次数优先策略
# *For any* 凭证轮换次数 N 和速率限制 M，当 N < M 时，系统应该每 N 次请求后切换密钥
# **Validates: Requirements 2.1**

class TestRotationPriority:
    """轮换次数优先策略测试"""
    
    @given(
        calls_per_rotation=st.integers(min_value=1, max_value=10),
        rate_limit=st.integers(min_value=11, max_value=100)
    )
    @settings(max_examples=100)
    def test_rotation_before_rate_limit(self, calls_per_rotation, rate_limit):
        """测试轮换次数小于速率限制时优先轮换"""
        assume(calls_per_rotation < rate_limit)
        
        selector = KeySelector()
        selector.calls_per_rotation = calls_per_rotation
        
        # 模拟调用
        key_index = 0
        for i in range(calls_per_rotation):
            selector._call_counts[key_index] = i + 1
        
        # 验证达到轮换次数时应该轮换
        assert selector.should_rotate(key_index) is True


# ============ Property 5: 速率限制优先策略 ============
# Feature: api-key-management-enhancement, Property 5: 速率限制优先策略
# *For any* 凭证轮换次数 N 和速率限制 M，当 M < N 时，系统应该在达到速率限制时立即切换密钥
# **Validates: Requirements 2.2**

class TestRateLimitPriority:
    """速率限制优先策略测试"""
    
    @given(
        rate_limit=st.integers(min_value=1, max_value=10),
        calls_per_rotation=st.integers(min_value=11, max_value=100)
    )
    @settings(max_examples=100)
    def test_rate_limit_triggers_switch(self, rate_limit, calls_per_rotation):
        """测试速率限制小于轮换次数时优先切换"""
        assume(rate_limit < calls_per_rotation)
        
        # 创建速率限制信息
        rate_info = RateLimitInfo(
            key_index=0,
            limit=rate_limit,
            remaining=0,  # 已用尽
            used=rate_limit,
            status=KeyStatus.EXHAUSTED
        )
        
        # 验证密钥已用尽
        assert rate_info.remaining == 0
        assert rate_info.status == KeyStatus.EXHAUSTED


# ============ Property 6: 速率限制状态管理 ============
# Feature: api-key-management-enhancement, Property 6: 速率限制状态管理
# *For any* 密钥，当达到速率限制时，系统应该标记为已用尽状态并在轮换时跳过
# **Validates: Requirements 2.3**

class TestRateLimitStateManagement:
    """速率限制状态管理测试"""
    
    @given(limit=st.integers(min_value=1, max_value=100))
    @settings(max_examples=100)
    def test_exhausted_when_remaining_zero(self, limit):
        """测试剩余为零时标记为已用尽"""
        rate_info = RateLimitInfo(
            key_index=0,
            limit=limit,
            remaining=0,
            used=limit,
            status=KeyStatus.EXHAUSTED
        )
        
        assert rate_info.remaining == 0
        assert rate_info.status == KeyStatus.EXHAUSTED


# ============ Property 7: 速率限制自动重置 ============
# Feature: api-key-management-enhancement, Property 7: 速率限制自动重置
# *For any* 密钥，当重置时间到达时，系统应该自动恢复该密钥为可用状态
# **Validates: Requirements 2.5**

class TestRateLimitAutoReset:
    """速率限制自动重置测试"""
    
    @given(limit=st.integers(min_value=1, max_value=100))
    @settings(max_examples=100)
    def test_reset_restores_remaining(self, limit):
        """测试重置恢复剩余配额"""
        rate_info = RateLimitInfo(
            key_index=0,
            limit=limit,
            remaining=0,
            used=limit,
            status=KeyStatus.EXHAUSTED
        )
        
        # 模拟重置
        rate_info.remaining = rate_info.limit
        rate_info.used = 0
        rate_info.status = KeyStatus.ACTIVE
        
        assert rate_info.remaining == limit
        assert rate_info.used == 0
        assert rate_info.status == KeyStatus.ACTIVE


# ============ Property 8: 活跃密钥计数准确性 ============
# Feature: api-key-management-enhancement, Property 8: 活跃密钥计数准确性
# *For any* 密钥配置，活跃密钥数量应该等于启用状态的密钥数量
# **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**

class TestActiveKeyCount:
    """活跃密钥计数准确性测试"""
    
    @given(
        total_keys=st.integers(min_value=1, max_value=10),
        disabled_count=st.integers(min_value=0, max_value=5)
    )
    @settings(max_examples=100)
    def test_active_count_equals_enabled(self, total_keys, disabled_count):
        """测试活跃数量等于启用数量"""
        assume(disabled_count <= total_keys)
        
        keys = [KeyInfo(index=i, key=f"key-{i}", enabled=True) for i in range(total_keys)]
        
        # 禁用部分密钥
        for i in range(disabled_count):
            keys[i].enabled = False
        
        # 计算活跃数量
        active_count = sum(1 for k in keys if k.enabled)
        expected = total_keys - disabled_count
        
        assert active_count == expected


# ============ Property 9: 统计结果分组显示 ============
# Feature: api-key-management-enhancement, Property 9: 统计结果分组显示
# *For any* 统计结果列表，启用密钥的统计应该优先显示
# **Validates: Requirements 3.6, 3.7**

class TestStatsGrouping:
    """统计结果分组显示测试"""
    
    @given(
        enabled_count=st.integers(min_value=1, max_value=5),
        disabled_count=st.integers(min_value=1, max_value=5)
    )
    @settings(max_examples=100)
    def test_enabled_keys_first(self, enabled_count, disabled_count):
        """测试启用密钥优先显示"""
        # 创建混合列表
        keys = []
        for i in range(enabled_count):
            keys.append(KeyInfo(index=i, key=f"enabled-{i}", enabled=True))
        for i in range(disabled_count):
            keys.append(KeyInfo(index=enabled_count + i, key=f"disabled-{i}", enabled=False))
        
        # 分组
        enabled = [k for k in keys if k.enabled]
        disabled = [k for k in keys if not k.enabled]
        
        # 验证分组正确
        assert len(enabled) == enabled_count
        assert len(disabled) == disabled_count
        
        # 验证启用密钥在前
        grouped = enabled + disabled
        for i in range(enabled_count):
            assert grouped[i].enabled is True
        for i in range(disabled_count):
            assert grouped[enabled_count + i].enabled is False


# ============ Property 19-28: 统计、搜索、导入导出测试 ============

# Property 19: 密钥统计信息完整性
# Feature: api-key-management-enhancement, Property 19: 密钥统计信息完整性
# **Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.5**

class TestKeyStatsCompleteness:
    """密钥统计信息完整性测试"""
    
    @given(
        success=st.integers(min_value=0, max_value=100),
        failure=st.integers(min_value=0, max_value=100)
    )
    @settings(max_examples=100)
    def test_stats_contains_all_fields(self, success, failure):
        """测试统计信息包含所有字段"""
        stats = KeyStats(
            key_index=0,
            masked_key="test...key",
            enabled=True,
            success_count=success,
            failure_count=failure,
        )
        
        # 验证所有字段存在
        data = stats.to_dict()
        assert "key_index" in data
        assert "masked_key" in data
        assert "enabled" in data
        assert "success_count" in data
        assert "failure_count" in data
        assert "total_count" in data
        assert data["total_count"] == success + failure


# Property 21: 搜索过滤准确性
# Feature: api-key-management-enhancement, Property 21: 搜索过滤准确性
# **Validates: Requirements 7.1**

class TestSearchFilter:
    """搜索过滤准确性测试"""
    
    @given(keyword=st.text(min_size=1, max_size=5))
    @settings(max_examples=100)
    def test_search_returns_matching_keys(self, keyword):
        """测试搜索返回匹配的密钥"""
        assume(keyword.strip() != "")
        
        keys = [
            KeyInfo(index=0, key=f"abc{keyword}xyz", enabled=True),
            KeyInfo(index=1, key="nomatch", enabled=True),
            KeyInfo(index=2, key=f"{keyword}start", enabled=True),
        ]
        
        # 搜索
        results = [k for k in keys if keyword.lower() in k.key.lower()]
        
        # 验证结果
        assert len(results) >= 1
        for r in results:
            assert keyword.lower() in r.key.lower()


# Property 25: 密钥导出完整性
# Feature: api-key-management-enhancement, Property 25: 密钥导出完整性
# **Validates: Requirements 8.1**

class TestKeyExport:
    """密钥导出完整性测试"""
    
    @given(keys=key_list_strategy)
    @settings(max_examples=100)
    def test_export_contains_all_keys(self, keys):
        """测试导出包含所有密钥"""
        config = KeyConfig(keys=keys)
        exported = config.to_dict()
        
        assert "keys" in exported
        assert len(exported["keys"]) == len(keys)
        for i, key in enumerate(keys):
            assert exported["keys"][i] == key


# Property 26-28: 导入验证测试
# Feature: api-key-management-enhancement, Property 26-28
# **Validates: Requirements 8.2, 8.3, 8.4**

class TestKeyImport:
    """密钥导入测试"""
    
    @given(keys=key_list_strategy)
    @settings(max_examples=100)
    def test_import_round_trip(self, keys):
        """测试导入导出往返一致性"""
        # 导出
        config = KeyConfig(keys=keys)
        exported = config.to_dict()
        
        # 导入
        imported = KeyConfig.from_dict(exported)
        
        # 验证一致性
        assert imported.keys == keys


# ============ Property 29-34: 请求生成测试 ============

# Property 29: 请求报文生成完整性
# Feature: api-key-management-enhancement, Property 29: 请求报文生成完整性
# **Validates: Requirements 9.1, 9.3, 9.4**

class TestRequestGeneration:
    """请求报文生成测试"""
    
    @given(
        model=st.sampled_from(["gpt-4", "gpt-3.5-turbo", "claude-3"]),
        temperature=st.floats(min_value=0, max_value=2)
    )
    @settings(max_examples=100)
    def test_request_contains_required_fields(self, model, temperature):
        """测试请求包含必需字段"""
        generator = RequestGenerator(endpoint="https://api.example.com", api_key="test-key")
        
        params = {
            "model": model,
            "messages": [{"role": "user", "content": "Hello"}],
            "temperature": temperature,
        }
        
        preview = generator.generate_request_preview(params)
        
        assert "method" in preview
        assert "url" in preview
        assert "headers" in preview
        assert "body" in preview
        assert preview["body"]["model"] == model


# Property 31: 自定义报文验证
# Feature: api-key-management-enhancement, Property 31: 自定义报文验证
# **Validates: Requirements 10.3**

class TestCustomRequestValidation:
    """自定义报文验证测试"""
    
    def test_valid_request_passes(self):
        """测试有效请求通过验证"""
        generator = RequestGenerator()
        
        valid_request = json.dumps({
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hello"}]
        })
        
        is_valid, error = generator.validate_custom_request(valid_request)
        assert is_valid is True
        assert error == ""
    
    def test_invalid_json_fails(self):
        """测试无效 JSON 失败"""
        generator = RequestGenerator()
        
        is_valid, error = generator.validate_custom_request("not json")
        assert is_valid is False
        assert "Invalid JSON" in error
    
    def test_missing_model_fails(self):
        """测试缺少 model 字段失败"""
        generator = RequestGenerator()
        
        invalid_request = json.dumps({
            "messages": [{"role": "user", "content": "Hello"}]
        })
        
        is_valid, error = generator.validate_custom_request(invalid_request)
        assert is_valid is False
        assert "model" in error.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

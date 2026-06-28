#!/usr/bin/env python3
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

def test_cost_merge_input_output_unknown():
    from src.api.account_api import _parse_cost_rsc_data
    # 构造包含 chartExportData 的 RSC 片段：输入、输出、未标注方向
    raw = (
        '{"chartExportData":[\n'
        ' {"model":"Claude 3.5 Haiku (Input)","value":1.2},\n'
        ' {"model":"Claude 3.5 Haiku (Output)","value":3.4},\n'
        ' {"model":"Claude 3.5 Haiku","value":0.1},\n'
        ' {"model":"Gemini 2.5 Pro (Input)","value":2.5}\n'
        ' ]}'
    )
    res = _parse_cost_rsc_data({"raw": raw})
    by_model = {item["model"]: item for item in res.get("by_model", [])}
    assert "Claude 3.5 Haiku" in by_model
    claude = by_model["Claude 3.5 Haiku"]
    # 输入+输出+未知合计
    assert abs(claude["input_cost"] - 1.2) < 1e-6
    assert abs(claude["output_cost"] - 3.4) < 1e-6
    assert abs(claude["cost"] - (1.2 + 3.4 + 0.1)) < 1e-6
    # 另一个模型只有输入也应有总额
    assert "Gemini 2.5 Pro" in by_model
    gem = by_model["Gemini 2.5 Pro"]
    assert abs(gem["input_cost"] - 2.5) < 1e-6
    assert abs(gem["cost"] - 2.5) < 1e-6


def test_billing_balance_reads_legacy_money_child_string():
    from src.api.account_api import _parse_billing_rsc_data

    raw = '1:["$","span",null,{"children":"$58.49928"}]'

    result = _parse_billing_rsc_data({"raw": raw})

    assert result["balance"] == 58.49928


def test_billing_balance_prefers_labeled_current_balance_over_first_money_value():
    from src.api.account_api import _parse_billing_rsc_data

    raw = "\n".join(
        [
            '1:["$","div",null,{"children":"30-day spend"}]',
            '2:["$","span",null,{"children":"$0.00000"}]',
            '3:["$","div",null,{"children":"Current balance"}]',
            '4:["$","span",null,{"children":["$","58.49928"]}]',
        ]
    )

    result = _parse_billing_rsc_data({"raw": raw})

    assert result["balance"] == 58.49928


def test_billing_balance_prefers_specific_label_over_earlier_generic_balance_label():
    from src.api.account_api import _parse_billing_rsc_data

    raw = "\n".join(
        [
            '1:["$","div",null,{"children":"Balance and usage overview"}]',
            '2:["$","div",null,{"children":"30-day spend"}]',
            '3:["$","span",null,{"children":"$0.00000"}]',
            '4:["$","div",null,{"children":"Current balance"}]',
            '5:["$","span",null,{"children":["$","58.49928"]}]',
        ]
    )

    result = _parse_billing_rsc_data({"raw": raw})

    assert result["balance"] == 58.49928


def test_billing_balance_reads_structured_balance_amounts():
    from src.api.account_api import _parse_billing_rsc_data

    raw = '1:{"billing":{"currentBalance":{"amount":"12.34567","currency":"USD"}}}'

    result = _parse_billing_rsc_data({"raw": raw})

    assert result["balance"] == 12.34567


def test_billing_balance_reads_structured_balance_after_rsc_alpha_tag():
    from src.api.account_api import _parse_billing_rsc_data

    raw = '1:I{"billing":{"currentBalance":{"amount":"23.45678","currency":"USD"}}}'

    result = _parse_billing_rsc_data({"raw": raw})

    assert result["balance"] == 23.45678


def test_billing_balance_prefers_precise_structured_balance_keys():
    from src.api.account_api import _parse_billing_rsc_data

    raw = "\n".join(
        [
            '1:{"billingSummary":{"balance":0}}',
            '2:{"billing":{"currentBalance":{"amount":"58.49928","currency":"USD"}}}',
        ]
    )

    result = _parse_billing_rsc_data({"raw": raw})

    assert result["balance"] == 58.49928


def test_dashboard_next_url_uses_app_route_without_dashboard_base_path():
    from src.api.account_api import _dashboard_next_url

    next_url = _dashboard_next_url(
        "/dashboard/account/billing",
        {"view": "US", "_rsc": "10s30"},
    )

    assert next_url == "/account/billing?view=US"


def test_rates_parser_extracts_dashboard_llm_gateway_prices():
    from src.api.account_api import _parse_rates_rsc_data

    raw = (
        '"children":["LLM Gateway + LeMUR"," Input Tokens"]'
        '"children":"GPT 5"'
        '"$$1.25"," ",["$","span",null,{"children":[" / ","1M tokens"]}]'
        '"children":["LLM Gateway + LeMUR"," Output Tokens"]'
        '"children":"GPT 5"'
        '"$$10.00"," ",["$","span",null,{"children":[" / ","1M tokens"]}]'
    )

    result = _parse_rates_rsc_data({"raw": raw})

    assert result["llm_gateway_input"] == [
        {"model": "GPT 5", "rate": 1.25, "unit": "1M tokens"}
    ]
    assert result["llm_gateway_output"] == [
        {"model": "GPT 5", "rate": 10.0, "unit": "1M tokens"}
    ]


@pytest.mark.asyncio
async def test_rates_response_marks_fallback_when_dashboard_parse_empty(monkeypatch):
    from src.api import account_api

    async def fake_get_session(account_email=None):
        return {"email": "user@example.com"}

    async def fake_dashboard_request(*args, **kwargs):
        return {"raw": "<html>billing page without recognizable rates</html>"}

    monkeypatch.setattr(account_api, "_get_session", fake_get_session)
    monkeypatch.setattr(account_api, "_make_dashboard_request", fake_dashboard_request)
    account_api._cache_store.clear()

    result = await account_api.get_rates(region="US", force=True, account_email="user@example.com")

    assert result["metadata"]["source"] == "fallback"
    assert result["metadata"]["fallback_count"] > 0
    assert result["metadata"]["dashboard_counts"]["llm_gateway_input"] == 0
    assert any("备用费率" in warning for warning in result["warnings"])
    assert all(item["price_source"] == "fallback" for item in result["llm_gateway_input"])

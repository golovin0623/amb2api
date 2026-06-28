#!/usr/bin/env python3
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

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


def test_billing_balance_reads_structured_balance_amounts():
    from src.api.account_api import _parse_billing_rsc_data

    raw = '1:{"billing":{"currentBalance":{"amount":"12.34567","currency":"USD"}}}'

    result = _parse_billing_rsc_data({"raw": raw})

    assert result["balance"] == 12.34567


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

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


def test_dashboard_balance_api_reads_top_level_balance():
    from src.api.account_api import _extract_balance_api_amount

    assert _extract_balance_api_amount({"balance": 58.49928}) == 58.49928


def test_dashboard_balance_api_reads_nested_amount_object():
    from src.api.account_api import _extract_balance_api_amount

    payload = {"data": {"currentBalance": {"amount": "12.34567", "currency": "USD"}}}

    assert _extract_balance_api_amount(payload) == 12.34567


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


def test_official_pricing_parser_extracts_llm_gateway_tables():
    from src.api.account_api import _parse_official_pricing_page_rates

    raw = """
    <button data-matrix-tab="7">LLM Gateway</button>
    <div data-matrix-panel="5">
      <table>
        <tr>
          <td><span>Speech Model</span></td>
          <td><strong>$0.12</strong><span> / 1M</span></td>
          <td><strong>$0.24</strong><span> / 1M</span></td>
        </tr>
      </table>
    </div>
    <div data-matrix-panel="7">
      <table>
        <tbody>
          <tr>
            <td><span>GPT 5.5</span></td>
            <td><strong>$2.00</strong><span> / 1M</span></td>
            <td><strong>$16.00</strong><span> / 1M</span></td>
          </tr>
          <tr>
            <td><span>Gemini 2.5 Flash Lite</span></td>
            <td><em>$0.10</em><small> / 1M</small></td>
            <td>$0.40/1M</td>
          </tr>
        </tbody>
      </table>
      <div class="lg:hidden">
        <div><span>GPT 5.5</span><span>$2.00</span><span> / 1M</span></div>
      </div>
    </div>
    <!-- Volume-based pricing CTA -->
    """

    result = _parse_official_pricing_page_rates(raw)

    assert result["llm_gateway_input"] == [
        {"model": "GPT 5.5", "rate": 2.0, "unit": "1M tokens", "price_source": "official_pricing"},
        {"model": "Gemini 2.5 Flash Lite", "rate": 0.1, "unit": "1M tokens", "price_source": "official_pricing"},
    ]
    assert result["llm_gateway_output"] == [
        {"model": "GPT 5.5", "rate": 16.0, "unit": "1M tokens", "price_source": "official_pricing"},
        {"model": "Gemini 2.5 Flash Lite", "rate": 0.4, "unit": "1M tokens", "price_source": "official_pricing"},
    ]


def test_official_pricing_parser_falls_back_to_llm_gateway_heading_without_panel_attrs():
    from src.api.account_api import _parse_official_pricing_page_rates

    raw = """
    <section>
      <h3>Speech to Text</h3>
      <table>
        <tr>
          <td><span>Nano</span></td>
          <td>$0.12 / 1M</td>
          <td>$0.24 / 1M</td>
        </tr>
      </table>
    </section>
    <section>
      <h3>LLM Gateway</h3>
      <table>
        <tr>
          <td><span>Claude 4.5 Haiku</span></td>
          <td>$1.00 / 1M</td>
          <td>$5.00 / 1M</td>
        </tr>
      </table>
    </section>
    <section>
      <h3>Guardrails</h3>
      <table>
        <tr>
          <td><span>Guardrail Model</span></td>
          <td>$9.99 / 1M</td>
          <td>$9.99 / 1M</td>
        </tr>
      </table>
    </section>
    """

    result = _parse_official_pricing_page_rates(raw)

    assert result["llm_gateway_input"] == [
        {"model": "Claude 4.5 Haiku", "rate": 1.0, "unit": "1M tokens", "price_source": "official_pricing"},
    ]
    assert result["llm_gateway_output"] == [
        {"model": "Claude 4.5 Haiku", "rate": 5.0, "unit": "1M tokens", "price_source": "official_pricing"},
    ]


def test_usage_token_normalization_preserves_raw_integers_and_scales_decimal_units():
    from src.api.account_api import _normalize_usage_token_value

    assert _normalize_usage_token_value("12,089") == 12089
    assert _normalize_usage_token_value(12089) == 12089
    assert _normalize_usage_token_value("0.004") == 4000
    assert _normalize_usage_token_value(0.004) == 4000
    assert _normalize_usage_token_value("1.0") == 1_000_000
    assert _normalize_usage_token_value(1.0) == 1
    assert _normalize_usage_token_value(4423.0) == 4423
    assert _normalize_usage_token_value(float("nan")) == 0
    assert _normalize_usage_token_value(float("inf")) == 0


def test_usage_parser_scales_fractional_million_tokens_to_tokens():
    from src.api.account_api import _parse_usage_rsc_data

    raw = "\n".join(
        [
            '1:["$","div","LLM Gateway + LeMUR-Claude 4.5 Haiku",{"children":[["$","span",null,{"children":["0.004"," ",["$","span",null,{"children":["tokens"]}]]}]]}]',
            '2:["$","div","LLM Gateway + LeMUR-Gemini 3.5 Flash",{"children":[["$","span",null,{"children":["0.006"," ",["$","span",null,{"children":["tokens"]}]]}]]}]',
            '3:["$","$L14",null,{"segments":[{"value":0.004,"color":"#f60"},{"value":0.006,"color":"#0cc"}]}]',
        ]
    )

    result = _parse_usage_rsc_data({"raw": raw})

    assert result["total_tokens"] == 10000
    assert result["by_model"] == [
        {"model": "Claude 4.5 Haiku", "tokens": 4000},
        {"model": "Gemini 3.5 Flash", "tokens": 6000},
    ]
    assert result["segments"] == [
        {"value": 4000, "color": "#f60"},
        {"value": 6000, "color": "#0cc"},
    ]


@pytest.mark.asyncio
async def test_rates_response_uses_official_pricing_when_dashboard_parse_empty(monkeypatch):
    from src.api import account_api

    async def fake_get_session(account_email=None):
        return {"email": "user@example.com"}

    async def fake_dashboard_request(*args, **kwargs):
        return {"raw": "<html>billing page without recognizable rates</html>"}

    async def fake_official_pricing():
        return {
            "llm_gateway_input": [
                {"model": "GPT 5.5", "rate": 2.0, "unit": "1M tokens", "price_source": "official_pricing"},
            ],
            "llm_gateway_output": [
                {"model": "GPT 5.5", "rate": 16.0, "unit": "1M tokens", "price_source": "official_pricing"},
            ],
        }

    monkeypatch.setattr(account_api, "_get_session", fake_get_session)
    monkeypatch.setattr(account_api, "_make_dashboard_request", fake_dashboard_request)
    monkeypatch.setattr(account_api, "_fetch_official_pricing_page_rates", fake_official_pricing)
    account_api._cache_store.clear()

    result = await account_api.get_rates(region="US", force=True, account_email="user@example.com")

    assert result["metadata"]["source"] == "mixed"
    assert result["metadata"]["official_counts"]["llm_gateway_input"] == 1
    assert result["llm_gateway_input"][0]["model"] == "GPT 5.5"
    assert result["llm_gateway_input"][0]["price_source"] == "official_pricing"
    assert any(item["price_source"] == "fallback" for item in result["speech_to_text"])


@pytest.mark.asyncio
async def test_rates_response_preserves_dashboard_llm_rates_for_non_us_region(monkeypatch):
    from src.api import account_api

    async def fake_get_session(account_email=None):
        return {"email": "user@example.com"}

    async def fake_dashboard_request(*args, **kwargs):
        return {"raw": "<html>billing page</html>"}

    def fake_parse_rates(_data):
        return {
            "llm_gateway_input": [
                {"model": "GPT 5", "rate": 1.5, "unit": "1M tokens"},
            ],
            "llm_gateway_output": [
                {"model": "GPT 5", "rate": 11.0, "unit": "1M tokens"},
            ],
        }

    async def fake_official_pricing():
        return {
            "llm_gateway_input": [
                {"model": "GPT 5", "rate": 1.25, "unit": "1M tokens", "price_source": "official_pricing"},
            ],
            "llm_gateway_output": [
                {"model": "GPT 5", "rate": 10.0, "unit": "1M tokens", "price_source": "official_pricing"},
            ],
        }

    monkeypatch.setattr(account_api, "_get_session", fake_get_session)
    monkeypatch.setattr(account_api, "_make_dashboard_request", fake_dashboard_request)
    monkeypatch.setattr(account_api, "_parse_rates_rsc_data", fake_parse_rates)
    monkeypatch.setattr(account_api, "_fetch_official_pricing_page_rates", fake_official_pricing)
    account_api._cache_store.clear()

    result = await account_api.get_rates(region="EU", force=True, account_email="user@example.com")

    assert result["llm_gateway_input"][0]["rate"] == 1.5
    assert result["llm_gateway_input"][0]["price_source"] == "dashboard"
    assert result["llm_gateway_output"][0]["rate"] == 11.0
    assert result["llm_gateway_output"][0]["price_source"] == "dashboard"
    assert result["metadata"]["official_count"] == 0


@pytest.mark.asyncio
async def test_rates_response_warns_when_official_pricing_fails_after_dashboard_parse(monkeypatch):
    from src.api import account_api

    async def fake_get_session(account_email=None):
        return {"email": "user@example.com"}

    async def fake_dashboard_request(*args, **kwargs):
        return {"raw": "<html>billing page</html>"}

    def fake_parse_rates(_data):
        return {
            "llm_gateway_input": [
                {"model": "GPT 5", "rate": 1.25, "unit": "1M tokens"},
            ],
            "llm_gateway_output": [
                {"model": "GPT 5", "rate": 10.0, "unit": "1M tokens"},
            ],
        }

    async def fake_official_pricing():
        raise RuntimeError("official unavailable")

    monkeypatch.setattr(account_api, "_get_session", fake_get_session)
    monkeypatch.setattr(account_api, "_make_dashboard_request", fake_dashboard_request)
    monkeypatch.setattr(account_api, "_parse_rates_rsc_data", fake_parse_rates)
    monkeypatch.setattr(account_api, "_fetch_official_pricing_page_rates", fake_official_pricing)
    account_api._cache_store.clear()

    result = await account_api.get_rates(region="US", force=True, account_email="user@example.com")

    assert result["metadata"]["dashboard_count"] == 2
    assert any("AssemblyAI 官方定价页请求失败" in warning for warning in result["warnings"])
    assert any("Dashboard" in warning for warning in result["warnings"])


@pytest.mark.asyncio
async def test_rates_response_marks_fallback_when_dashboard_parse_empty(monkeypatch):
    from src.api import account_api

    async def fake_get_session(account_email=None):
        return {"email": "user@example.com"}

    async def fake_dashboard_request(*args, **kwargs):
        return {"raw": "<html>billing page without recognizable rates</html>"}

    async def fake_official_pricing():
        return {}

    monkeypatch.setattr(account_api, "_get_session", fake_get_session)
    monkeypatch.setattr(account_api, "_make_dashboard_request", fake_dashboard_request)
    monkeypatch.setattr(account_api, "_fetch_official_pricing_page_rates", fake_official_pricing)
    account_api._cache_store.clear()

    result = await account_api.get_rates(region="US", force=True, account_email="user@example.com")

    assert result["metadata"]["source"] == "fallback"
    assert result["metadata"]["fallback_count"] > 0
    assert result["metadata"]["dashboard_counts"]["llm_gateway_input"] == 0
    assert any("备用费率" in warning for warning in result["warnings"])
    assert all(item["price_source"] == "fallback" for item in result["llm_gateway_input"])

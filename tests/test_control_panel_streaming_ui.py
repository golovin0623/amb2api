from pathlib import Path


def test_control_panel_only_exposes_global_fake_streaming_toggle():
    html = Path("front/control_panel.html").read_text(encoding="utf-8")

    assert 'id="fakeStreamEnabled"' in html
    assert "启用全局假流式" in html

    assert 'id="enableRealStreaming"' not in html
    assert "启用真实流式" not in html
    assert 'id="streamKeepaliveSeconds"' not in html
    assert 'id="streamBootstrapRetries"' not in html

    assert "enable_real_streaming: true" in html


def test_account_cost_ui_uses_precise_currency_formatter_for_small_amounts():
    html = Path("front/control_panel.html").read_text(encoding="utf-8")

    assert "function formatAccountCurrency" in html
    assert "formatAccountCurrency(totalCost" in html
    assert "formatAccountCurrency(item.cost" in html
    assert "totalEl.textContent = formatAccountCurrency(detailTotal" in html
    assert "formatAccountCurrency(item.amount" in html
    assert "const sign = amount < 0 ? '-' : '';" in html
    assert "sign + '$' + abs.toLocaleString" in html
    assert "sign + '$' + absText" in html


def test_account_charts_share_coordinate_helpers_and_hide_tooltips_on_scroll():
    html = Path("front/control_panel.html").read_text(encoding="utf-8")

    assert "const ACCOUNT_CHART_X_PADDING = 0.05;" in html
    assert "function getAccountChartPointRatio" in html
    assert "function getAccountChartPointLeft" in html
    assert "function getNearestAccountChartIndex" in html
    assert "function getAccountChartLabelStyle" in html

    assert "getAccountChartPointRatio(i, dataPointCount) * width" in html
    assert "getAccountChartPointRatio(i, arr.length) * width" in html
    assert "getNearestAccountChartIndex(event.clientX, rect, dailyByModel.length)" in html
    assert "getNearestAccountChartIndex(event.clientX, rect, trend.length)" in html
    assert "getAccountChartLabelStyle(idx, dates.length)" in html

    assert 'ontouchend="finishUsageTouch()"' in html
    assert 'ontouchcancel="finishUsageTouch()"' in html
    assert 'ontouchend="finishCostTouch()"' in html
    assert 'ontouchcancel="finishCostTouch()"' in html
    assert "window.addEventListener('scroll', hideAccountChartTooltips" in html
    assert "shouldDismissAccountChartTouch" in html
    assert "dismissed: false" in html
    assert "start.dismissed = true;" in html
    assert "shouldDismissAccountChartTouch(event, 'usage', hideUsageTooltip)" in html
    assert "shouldDismissAccountChartTouch(event, 'cost', hideCostTooltip)" in html

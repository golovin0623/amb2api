from pathlib import Path


def test_control_panel_only_exposes_global_fake_streaming_toggle():
    html = Path("front/control_panel.html").read_text(encoding="utf-8")

    assert 'id="fakeStreamEnabled"' in html
    assert "启用全局假流式" in html

    assert 'id="enableRealStreaming"' not in html
    assert "启用真实流式" not in html
    assert 'id="streamKeepaliveSeconds"' not in html
    assert 'id="streamBootstrapRetries"' not in html

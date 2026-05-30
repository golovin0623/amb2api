"""多模态：含图片的消息应透传 image_url，而不是被静默丢成纯文本。"""
from src.services.assembly_client import _sanitize_messages


def test_image_url_preserved_in_user_message():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is in this image?"},
                {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
            ],
        }
    ]
    out = _sanitize_messages(messages)
    assert len(out) == 1
    content = out[0]["content"]
    # 应保留为多模态数组，而非被压平成纯文本字符串
    assert isinstance(content, list), f"图片消息被压平了: {content!r}"
    types = [p.get("type") for p in content]
    assert "image_url" in types, "image_url 块被丢弃"
    assert "text" in types
    # image_url 数据应原样保留
    img = next(p for p in content if p.get("type") == "image_url")
    assert img["image_url"]["url"] == "https://example.com/cat.png"


def test_text_only_list_still_flattens_to_string():
    """纯文本数组仍压平为字符串（向后兼容）。"""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]}
    ]
    out = _sanitize_messages(messages)
    assert out[0]["content"] == "hello\nworld"


def test_plain_string_unchanged():
    messages = [{"role": "user", "content": "just text"}]
    out = _sanitize_messages(messages)
    assert out[0]["content"] == "just text"

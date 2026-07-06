import pytest

from app.utils.content_utils import (
    MAX_IMAGE_DATA_URL_CHARS,
    extract_text,
    has_image_part,
    normalize_content,
    prepend_text_part,
    validate_image_url,
)


def test_normalize_content_string_passthrough():
    assert normalize_content("hello") == "hello"


def test_normalize_content_text_and_image_list():
    content = [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
    ]
    assert normalize_content(content) == content


def test_normalize_content_input_image_converted_to_image_url():
    content = [{"type": "input_image", "image_url": "https://x/y.png"}]
    result = normalize_content(content)
    assert result == [{"type": "image_url", "image_url": {"url": "https://x/y.png"}}]


def test_normalize_content_rejects_unknown_part_type():
    with pytest.raises(ValueError, match="unsupported_content_type"):
        normalize_content([{"type": "file", "file": {}}])


def test_normalize_content_rejects_invalid_image_url_scheme():
    with pytest.raises(ValueError, match="invalid_image_url"):
        normalize_content([{"type": "image_url", "image_url": {"url": "ftp://x/y"}}])


def test_normalize_content_rejects_non_image_data_url():
    with pytest.raises(ValueError, match="invalid_image_url"):
        normalize_content([{"type": "image_url", "image_url": {"url": "data:application/pdf;base64,aGVsbG8="}}])


def test_normalize_content_rejects_image_too_large():
    big = "data:image/png;base64," + "A" * (MAX_IMAGE_DATA_URL_CHARS + 1)
    with pytest.raises(ValueError, match="image_too_large"):
        normalize_content([{"type": "image_url", "image_url": {"url": big}}])


def test_normalize_content_rejects_invalid_base64():
    with pytest.raises(ValueError, match="invalid_image_url"):
        normalize_content([{"type": "image_url", "image_url": {"url": "data:image/png;base64,!!!not-base64!!!"}}])


def test_normalize_content_rejects_non_list_non_str():
    with pytest.raises(ValueError, match="unsupported_content_type"):
        normalize_content(123)


def test_extract_text_from_string():
    assert extract_text("hello") == "hello"


def test_extract_text_from_list_returns_only_text_parts():
    content = [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
        {"type": "text", "text": "分析这张图"},
    ]
    assert extract_text(content) == "分析这张图"


def test_extract_text_from_list_no_text_part():
    content = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}}]
    assert extract_text(content) == ""


def test_extract_text_non_str_non_list():
    assert extract_text(None) == ""
    assert extract_text(123) == ""


def test_has_image_part_string_returns_false():
    assert has_image_part("hello") is False


def test_has_image_part_list_with_image_url():
    assert has_image_part([{"type": "image_url", "image_url": {"url": "x"}}]) is True


def test_has_image_part_list_with_input_image():
    assert has_image_part([{"type": "input_image", "image_url": "x"}]) is True


def test_has_image_part_list_text_only():
    assert has_image_part([{"type": "text", "text": "hi"}]) is False


def test_prepend_text_part_string_nonempty():
    assert prepend_text_part("hello", "prefix:") == "prefix:hello"


def test_prepend_text_part_string_empty():
    assert prepend_text_part("", "prefix:") == "prefix:"


def test_prepend_text_part_list_inserts_text_at_head():
    content = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}}]
    result = prepend_text_part(content, "<memory>")
    assert result[0] == {"type": "text", "text": "<memory>"}
    assert result[1] == content[0]


def test_validate_image_url_accepts_http():
    assert validate_image_url("https://x/y.png") == "https://x/y.png"


def test_validate_image_url_accepts_valid_data_url():
    url = "data:image/png;base64,aGVsbG8="
    assert validate_image_url(url) == url


def test_validate_image_url_rejects_empty():
    with pytest.raises(ValueError, match="invalid_image_url"):
        validate_image_url("")

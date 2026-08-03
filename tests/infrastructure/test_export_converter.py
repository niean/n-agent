from __future__ import annotations

import re

import pytest

from app.infrastructure.artifact.export_converter import convert_to_html


# ---------------------------------------------------------------------------
# Structural / shell tests
# ---------------------------------------------------------------------------


def test_basic_markdown_produces_full_html_document():
    out = convert_to_html("# Hello\n\nSome paragraph text.")
    assert out.startswith("<!DOCTYPE html>")
    assert "<html" in out
    assert "<head>" in out
    assert "<body>" in out
    # heading converted
    assert "<h1>Hello</h1>" in out
    assert "Some paragraph text." in out


def test_output_has_utf8_charset_meta():
    out = convert_to_html("# x")
    assert '<meta charset="utf-8">' in out


def test_output_has_strict_csp_meta_blocking_scripts():
    out = convert_to_html("# x")
    assert "Content-Security-Policy" in out
    csp = re.search(r'content="([^"]*)"', out)
    assert csp is not None
    policy = csp.group(1).lower()
    # default-src none blocks everything not explicitly allowed
    assert "default-src 'none'" in policy
    # no script execution permitted
    assert "script-src" not in policy or "unsafe-inline" not in policy
    assert "'unsafe-inline'" not in policy
    assert "'unsafe-eval'" not in policy


def test_empty_input_still_returns_html_shell():
    out = convert_to_html("")
    assert out.startswith("<!DOCTYPE html>")
    assert '<meta charset="utf-8">' in out
    assert "Content-Security-Policy" in out


# ---------------------------------------------------------------------------
# Markdown feature tests
# ---------------------------------------------------------------------------


def test_markdown_features_render():
    md = (
        "# Title\n\n"
        "A paragraph with **bold** and *italic*.\n\n"
        "- item one\n- item two\n\n"
        "```python\nprint('hi')\n```\n\n"
        "> quote me\n"
    )
    out = convert_to_html(md)
    assert "<h1>Title</h1>" in out
    assert "<strong>bold</strong>" in out
    assert "<em>italic</em>" in out
    assert "<ul>" in out and "<li>item one</li>" in out
    assert "<pre><code" in out
    assert "print(" in out
    assert "<blockquote>" in out


def test_table_extension_supported():
    md = "| a | b |\n|---|---|\n| 1 | 2 |\n"
    out = convert_to_html(md)
    assert "<table>" in out
    assert "<td>1</td>" in out


def test_code_block_escapes_html_content():
    md = "```\n<script>alert(1)</script>\n```\n"
    out = convert_to_html(md)
    # the script must not appear as a live tag inside code
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out


# ---------------------------------------------------------------------------
# Security: raw HTML escaping
# ---------------------------------------------------------------------------


def test_raw_html_script_is_escaped_not_rendered():
    out = convert_to_html("<script>alert(1)</script>")
    assert "<script>" not in out
    assert "<script" not in out.lower()
    # visible as escaped text
    assert "&lt;script&gt;" in out
    assert "alert(1)" in out


def test_raw_html_iframe_is_escaped():
    out = convert_to_html('<iframe src="javascript:alert(1)"></iframe>')
    assert "<iframe" not in out.lower()
    assert "javascript:alert(1)" not in out


def test_raw_html_style_tag_is_escaped():
    out = convert_to_html("<style>body{background:red}</style>")
    assert "<style" not in out.lower()
    assert "background:red" in out


def test_raw_html_with_event_attribute_not_executable():
    out = convert_to_html('<img src="http://e.com/x.png" onerror="alert(1)">')
    # onerror must be stripped
    assert "onerror" not in out.lower()
    # the http image is preserved without the dangerous attribute
    assert '<img' in out.lower()
    assert 'src="http://e.com/x.png"' in out


def test_raw_html_anchor_with_onclick_stripped():
    out = convert_to_html('<a href="http://e.com" onclick="alert(1)">x</a>')
    assert "onclick" not in out.lower()
    assert 'href="http://e.com"' in out


# ---------------------------------------------------------------------------
# Security: URL scheme validation
# ---------------------------------------------------------------------------


def test_javascript_url_link_degraded_to_text():
    out = convert_to_html("[click me](javascript:alert(1))")
    assert "<a " not in out.lower()
    assert "javascript:" not in out.lower()
    assert "click me" in out


def test_javascript_url_case_insensitive_blocked():
    out = convert_to_html("[x](JaVaScRiPt:alert(1))")
    assert "<a " not in out.lower()
    assert "javascript:" not in out.lower()


def test_javascript_url_with_tab_blocked():
    # browsers strip tabs inside URLs, so java\tscript: becomes javascript:
    out = convert_to_html("[x](java\tscript:alert(1))")
    assert "<a " not in out.lower()
    assert "javascript:" not in out.lower()


def test_javascript_url_with_newline_blocked():
    out = convert_to_html("[x](java\nscript:alert(1))")
    assert "<a " not in out.lower()
    assert "javascript:" not in out.lower()


def test_data_url_link_blocked():
    out = convert_to_html("[x](data:text/html,<script>alert(1)</script>)")
    assert "<a " not in out.lower()
    assert "data:" not in out.lower()
    assert "<script" not in out.lower()


def test_data_url_image_blocked():
    out = convert_to_html("![pic](data:image/svg+xml,<svg onload=alert(1)>)")
    assert "<img" not in out.lower()
    assert "data:" not in out.lower()
    assert "onload" not in out.lower()
    assert "<svg" not in out.lower()
    # alt text preserved as plain text
    assert "pic" in out


def test_javascript_url_image_blocked():
    out = convert_to_html("![pic](javascript:alert(1))")
    assert "<img" not in out.lower()
    assert "javascript:" not in out.lower()
    assert "pic" in out


def test_vbscript_url_blocked():
    out = convert_to_html("[x](vbscript:msgbox(1))")
    assert "<a " not in out.lower()
    assert "vbscript:" not in out.lower()


# ---------------------------------------------------------------------------
# Allowed URL schemes preserved
# ---------------------------------------------------------------------------


def test_http_link_preserved():
    out = convert_to_html("[a](http://example.com/path)")
    assert 'href="http://example.com/path"' in out
    assert ">a</a>" in out


def test_https_link_preserved():
    out = convert_to_html("[b](https://example.org)")
    assert 'href="https://example.org"' in out
    assert ">b</a>" in out


def test_mailto_link_preserved():
    out = convert_to_html("[c](mailto:user@example.com)")
    assert 'href="mailto:user@example.com"' in out
    assert ">c</a>" in out


def test_http_and_https_image_preserved():
    out = convert_to_html("![pic](http://e.com/a.png)")
    assert "<img" in out.lower()
    assert 'src="http://e.com/a.png"' in out
    assert 'alt="pic"' in out


# ---------------------------------------------------------------------------
# Relative links / images degrade to text
# ---------------------------------------------------------------------------


def test_relative_link_degrades_to_text():
    out = convert_to_html("[my text](foo.md)")
    assert "<a " not in out.lower()
    assert "foo.md" not in out
    assert "my text" in out


def test_absolute_path_link_degrades_to_text():
    out = convert_to_html("[mylink](/etc/passwd)")
    assert "<a " not in out.lower()
    assert "/etc/passwd" not in out
    # link text preserved as plain text (mylink is distinctive, not in shell)
    assert "mylink" in out


def test_anchor_link_degrades_to_text():
    out = convert_to_html("[x](#section)")
    assert "<a " not in out.lower()
    assert "#section" not in out
    assert "x" in out


def test_protocol_relative_link_degrades_to_text():
    out = convert_to_html("[x](//evil.com/path)")
    assert "<a " not in out.lower()
    assert "evil.com" not in out


def test_relative_image_degrades_to_alt_text():
    out = convert_to_html("![alt desc](img.png)")
    assert "<img" not in out.lower()
    assert "img.png" not in out
    assert "alt desc" in out


# ---------------------------------------------------------------------------
# Malformed / edge cases
# ---------------------------------------------------------------------------


def test_malformed_tags_not_executable():
    out = convert_to_html("<scr<script>ipt>alert(1)</scr</script>ipt>")
    assert "<script>alert(1)" not in out
    assert "<script" not in out.lower()


def test_nested_raw_html_safe():
    out = convert_to_html("<div><script>alert(1)</script></div>")
    assert "<script" not in out.lower()
    assert "<div" not in out.lower()
    assert "alert(1)" in out


def test_attribute_injection_via_link_title_safe():
    # markdown title parsing should not let attribute injection through
    out = convert_to_html('[x](http://e.com "title with &quot; quote")')
    # the link is preserved as http
    assert 'href="http://e.com"' in out
    # no unescaped quote breaks out of the attribute
    assert 'onclick' not in out.lower()


def test_combined_attack_vectors_all_neutralized():
    md = (
        "<script>alert(1)</script>\n\n"
        "<iframe src='javascript:alert(2)'></iframe>\n\n"
        '<img src="x" onerror="alert(3)">\n\n'
        '<a href="javascript:alert(4)">click</a>\n\n'
        "[md](javascript:alert(5))\n\n"
        "![pic](javascript:alert(6))\n\n"
        "![svg](data:image/svg+xml,<svg onload=alert(7)>)\n"
    )
    out = convert_to_html(md)
    low = out.lower()
    assert "<script" not in low
    assert "<iframe" not in low
    assert "<style" not in low
    assert "<object" not in low
    assert "<embed" not in low
    assert "<svg" not in low
    assert "onerror=" not in low
    assert "onload=" not in low
    assert "onclick=" not in low
    assert "javascript:" not in low
    assert "data:image" not in low
    assert "vbscript:" not in low


# ---------------------------------------------------------------------------
# Arbitrary text not mis-handled (html/code-like content passed directly)
# ---------------------------------------------------------------------------


def test_arbitrary_html_content_sanitized_not_rendered_live():
    # as if an html-kind artifact content were passed; must not render live HTML
    out = convert_to_html("<div onclick='steal()'>hi</div><p>ok</p>")
    low = out.lower()
    assert "onclick" not in low
    assert "<div" not in low


def test_code_like_content_with_html_fragments_safe():
    out = convert_to_html("x = '<script>alert(1)</script>'\ny = '</p>'")
    assert "<script" not in out.lower()
    assert "alert(1)" in out


# ---------------------------------------------------------------------------
# Pure function behavior
# ---------------------------------------------------------------------------


def test_convert_is_deterministic():
    md = "# Hello\n\n[link](http://e.com)\n"
    a = convert_to_html(md)
    b = convert_to_html(md)
    assert a == b


def test_non_string_input_raises_type_error():
    with pytest.raises(TypeError):
        convert_to_html(b"bytes not allowed")  # type: ignore[arg-type]

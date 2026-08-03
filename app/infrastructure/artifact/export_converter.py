"""Safe HTML export converter for Artifact content.

Pure function: converts markdown/document content to a standalone HTML document
with a strict Content-Security-Policy. No IO, no network.

Security approach:

1. Raw HTML is NOT trusted. The ``markdown`` library passes raw HTML through by
   default, so the converter post-processes every output with a whitelist-based
   sanitizer (``html.parser.HTMLParser``). Tags outside the markdown-generated
   whitelist are escaped to visible text; attributes outside the per-tag
   whitelist (notably all ``on*`` event handlers, ``style``, ``srcset``) are
   stripped.
2. URL schemes are whitelisted: ``http``/``https``/``mailto`` for links and
   ``http``/``https`` for images. Every other scheme (``javascript:``,
   ``data:``, ``vbscript:``, ``file:`` ...) and every schemeless/relative URL
   degrades to non-navigable plain text. Tab/newline/CR and other control
   characters are stripped from URLs before the scheme check so browser-equivalent
   trickery (``java<TAB>script:``) cannot bypass validation.
3. The output is a fixed UTF-8 HTML shell whose CSP sets ``default-src 'none'``
   (no script execution, no styles, no frames, no connections) and only permits
   ``img-src http: https:``.

v1 only supports markdown/document -> HTML. The caller (ArtifactService) is
responsible for capability gating; the converter is safe even if invoked
directly on arbitrary text.
"""
from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit

import markdown

__all__ = ["convert_to_html"]

_HTML_SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src http: https:; base-uri 'none'; form-action 'none'">
<title>Artifact</title>
</head>
<body>
{body}
</body>
</html>
"""

# Tags the markdown library (fenced_code + tables) may legitimately emit.
# Anything else in the output came from raw HTML and is escaped to text.
_ALLOWED_TAGS = frozenset({
    "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li",
    "blockquote", "pre", "code",
    "em", "strong", "del",
    "a", "img",
    "hr", "br",
    "table", "thead", "tbody", "tr", "th", "td",
})

_VOID_TAGS = frozenset({"img", "hr", "br"})

_ALLOWED_ATTRS = {
    "a": frozenset({"href", "title"}),
    "img": frozenset({"src", "alt", "title"}),
    "th": frozenset({"align"}),
    "td": frozenset({"align"}),
}

_LINK_SCHEMES = frozenset({"http", "https", "mailto"})
_IMG_SCHEMES = frozenset({"http", "https"})

# Control characters browsers strip/ignore inside URLs before parsing the
# scheme; removing them prevents ``java\tscript:`` style bypasses.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _strip_url(url: str) -> str:
    return _CTRL_RE.sub("", url).strip()


def _is_safe_url(url: str | None, allowed_schemes: frozenset[str]) -> bool:
    if not url:
        return False
    cleaned = _strip_url(url)
    if not cleaned:
        return False
    # protocol-relative URL (//host/path) -> not whitelisted -> degrade
    if cleaned.startswith("//"):
        return False
    scheme = urlsplit(cleaned).scheme.lower()
    if not scheme:
        # schemeless => relative URL -> degrade to text
        return False
    return scheme in allowed_schemes


def _first_attr(attrs, name: str) -> str | None:
    name = name.lower()
    for key, value in attrs:
        if key.lower() == name:
            return value if value is not None else ""
    return None


class _Sanitizer(HTMLParser):
    """Whitelist-based HTML sanitizer.

    ``convert_charrefs=True`` (the default) decodes all character references in
    text, so ``handle_data`` receives unicode and re-escapes it. Attribute
    values are always entity-decoded by HTMLParser regardless of this setting.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._open: list[str] = []

    # -- start / self-closing tags -----------------------------------------

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, self_closing=False)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, self_closing=True)

    def _start(self, tag, attrs, *, self_closing: bool):
        tag = tag.lower()
        if tag not in _ALLOWED_TAGS:
            # raw/disallowed HTML -> escape to inert visible text without
            # preserving attributes (avoids surfacing dangerous-looking
            # payloads like javascript: URLs or on* handlers as text).
            # escape() (not manual &lt;...&gt;) so a malformed tag name that
            # itself contains '<' (e.g. HTMLParser tokenizing "<scr<script>")
            # cannot leak a live '<script' into the output.
            self._out.append(escape(f"<{tag}>", quote=False))
            return
        if tag == "a":
            href = _first_attr(attrs, "href")
            if not _is_safe_url(href, _LINK_SCHEMES):
                # degrade to plain text; inner data still emitted by handle_data
                return
        elif tag == "img":
            src = _first_attr(attrs, "src")
            if not _is_safe_url(src, _IMG_SCHEMES):
                alt = _first_attr(attrs, "alt") or ""
                if alt:
                    self._out.append(escape(alt))
                return
        clean = self._clean_attrs(tag, attrs)
        attr_str = "".join(f' {k}="{escape(v, quote=True)}"' for k, v in clean)
        if tag in _VOID_TAGS:
            self._out.append(f"<{tag}{attr_str} />")
        elif self_closing:
            self._out.append(f"<{tag}{attr_str}></{tag}>")
        else:
            self._out.append(f"<{tag}{attr_str}>")
            self._open.append(tag)

    # -- end tags ----------------------------------------------------------

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _VOID_TAGS:
            return
        if tag not in _ALLOWED_TAGS:
            self._out.append(escape(f"</{tag}>"))
            return
        # only close tags we actually opened; skip stray end tags
        if tag in self._open:
            while self._open:
                top = self._open.pop()
                self._out.append(f"</{top}>")
                if top == tag:
                    break

    # -- text / entities ---------------------------------------------------

    def handle_data(self, data):
        # convert_charrefs=True already decoded entities; re-escape for output
        self._out.append(escape(data, quote=False))

    def handle_entityref(self, name):
        # not reached when convert_charrefs=True, but kept for safety
        self._out.append(f"&{name};")

    def handle_charref(self, name):
        self._out.append(f"&#{name};")

    # -- comments / declarations / PI: dropped ----------------------------

    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass

    # -- helpers -----------------------------------------------------------

    def _clean_attrs(self, tag, attrs):
        allowed = _ALLOWED_ATTRS.get(tag, frozenset())
        seen: set[str] = set()
        result: list[tuple[str, str]] = []
        for key, value in attrs:
            key = key.lower()
            if key not in allowed or key in seen:
                continue
            seen.add(key)
            result.append((key, value if value is not None else ""))
        return result

    def get_html(self) -> str:
        # close any tags left open by malformed input
        while self._open:
            self._out.append(f"</{self._open.pop()}>")
        return "".join(self._out)


def _sanitize(html_fragment: str) -> str:
    san = _Sanitizer()
    san.feed(html_fragment)
    san.close()
    return san.get_html()


def convert_to_html(content: str) -> str:
    """Convert markdown content to a standalone, safe HTML document.

    Pure function: no file IO, no network. Raw HTML in the markdown is
    escaped/sanitized; only ``http``/``https``/``mailto`` links and
    ``http``/``https`` images are preserved; relative links and images degrade
    to plain text. The output is a full UTF-8 HTML document with a strict
    Content-Security-Policy that blocks script execution.

    Args:
        content: Markdown text (document/markdown artifact content).

    Returns:
        A complete HTML document string.

    Raises:
        TypeError: if ``content`` is not a ``str``.
    """
    if not isinstance(content, str):
        raise TypeError("content must be a str")
    raw_html = markdown.markdown(
        content,
        extensions=["fenced_code", "tables"],
    )
    body = _sanitize(raw_html)
    return _HTML_SHELL.format(body=body)

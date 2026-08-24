import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from app.interfaces.http.dashboard import STATIC_DIR

from _css_utils import _css_rule_bodies, _css_media_blocks

CHAT_JS = STATIC_DIR / "chat.js"
HARNESS_JS = Path(__file__).parent / "chat_frontend_harness.js"

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


def _js_function_body(source, name):
    """按函数名提取完整函数体（花括号配对），从声明行到闭合花括号。"""
    pattern = re.compile(r"function\s+" + re.escape(name) + r"\s*\(")
    match = pattern.search(source)
    if match is None:
        return None
    open_brace = source.find("{", match.end())
    depth, k = 1, open_brace + 1
    while k < len(source) and depth > 0:
        if source[k] == "{":
            depth += 1
        elif source[k] == "}":
            depth -= 1
        k += 1
    return source[open_brace + 1 : k - 1]


class _HtmlTree(HTMLParser):
    """基于事件栈构建 HTML 树，用于结构断言（唯一性/祖先/兄弟顺序）。"""

    def __init__(self, html):
        super().__init__()
        self.root = {"tag": "#root", "attrs": {}, "children": [], "parent": None}
        self._stack = [self.root]
        self.feed(html)

    def _append(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "children": [], "parent": self._stack[-1]}
        self._stack[-1]["children"].append(node)
        return node

    def handle_starttag(self, tag, attrs):
        node = self._append(tag, attrs)
        if tag not in VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._append(tag, attrs)

    def handle_endtag(self, tag):
        if len(self._stack) > 1 and self._stack[-1]["tag"] == tag:
            self._stack.pop()

    def find_all(self, pred):
        found = []

        def walk(node):
            for child in node["children"]:
                if pred(child):
                    found.append(child)
                walk(child)

        walk(self.root)
        return found

    def by_id(self, element_id):
        nodes = self.find_all(lambda n: n["attrs"].get("id") == element_id)
        return nodes[0] if len(nodes) == 1 else (nodes or None)

    def ancestors(self, node):
        chain = []
        cur = node["parent"]
        while cur is not None:
            chain.append(cur)
            cur = cur["parent"]
        return chain

    def sibling_index(self, node):
        parent = node["parent"]
        if parent is None:
            return -1
        return parent["children"].index(node)


def test_chat_session_panel_collapse_assets():
    """T1: 会话面板左右独立折叠 -- 静态资源（HTML 结构 / CSS 四态网格 / chat.js 函数约束）。"""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    styles = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    chat_js = CHAT_JS.read_text(encoding="utf-8")
    tree = _HtmlTree(html)

    # 1. 三个关键节点各出现一次
    for element_id in ("chat-session-panel", "chat-session-toggle-btn", "chat-session-expand-btn"):
        nodes = tree.find_all(lambda n, eid=element_id: n["attrs"].get("id") == eid)
        assert len(nodes) == 1, f"{element_id} must appear exactly once, got {len(nodes)}"

    hide_btn = tree.by_id("chat-session-toggle-btn")
    expand_btn = tree.by_id("chat-session-expand-btn")

    # 2. 两个按钮的通用属性
    for btn, label, expanded in (
        (hide_btn, "隐藏会话列表", "true"),
        (expand_btn, "展开会话列表", "false"),
    ):
        assert btn["tag"] == "button"
        assert btn["attrs"].get("type") == "button"
        assert btn["attrs"].get("aria-label") == label
        assert btn["attrs"].get("title") == label
        assert btn["attrs"].get("aria-controls") == "chat-session-panel"
        assert btn["attrs"].get("aria-expanded") == expanded
        svgs = [c for c in btn["children"] if c["tag"] == "svg"]
        assert len(svgs) == 1
        assert svgs[0]["attrs"].get("aria-hidden") == "true"
        for path_element in svgs[0]["children"]:
            if path_element["tag"] in ("path", "rect"):
                assert path_element["attrs"].get("stroke") == "currentColor"

    # 3. 展开按钮初始 hidden；是 #chat-shell 直接子节点；位于会话 section 之后、.chat-stack 之前
    assert "hidden" in expand_btn["attrs"]
    shell = tree.by_id("chat-shell")
    assert expand_btn["parent"] is shell
    session_panel = tree.by_id("chat-session-panel")
    chat_stack = [n for n in shell["children"] if "chat-stack" in (n["attrs"].get("class") or "")][0]
    assert shell["children"].index(session_panel) < shell["children"].index(expand_btn) < shell["children"].index(chat_stack)

    # 4. 隐藏按钮位于会话头部 actions 容器内，且位于筛选按钮之前（同容器）
    hide_ancestors = tree.ancestors(hide_btn)
    actions = [n for n in hide_ancestors if "chat-session-panel__actions" in (n["attrs"].get("class") or "")]
    assert actions, "hide button must live inside .chat-session-panel__actions"
    actions_container = actions[-1]
    action_children = actions_container["children"]
    filter_btn = tree.by_id("chat-session-filter-btn")
    assert filter_btn["parent"] is actions_container
    assert action_children.index(hide_btn) < action_children.index(filter_btn)

    # 5. 图标尺寸与面板子元素类
    assert any(c["attrs"].get("class") == "chat-session-toggle-btn__icon" for c in hide_btn["children"])
    assert any(c["attrs"].get("class") == "chat-session-expand-btn__icon" for c in expand_btn["children"])
    hide_svg = hide_btn["children"][0]
    assert any(
        "session-toggle__panel" in (c["attrs"].get("class") or "") for c in hide_svg["children"]
    )

    # 6. 宽屏四态网格
    assert _css_rule_bodies(styles, ".chat-shell")[0].find(
        "grid-template-columns: 280px minmax(0, 2fr) 280px"
    ) != -1
    assert "padding-right: 16px" in _css_rule_bodies(styles, ".chat-shell")[0]
    assert not _css_rule_bodies(styles, ".chat-shell:not(.chat-shell--side-collapsed)")
    assert "grid-template-columns: 280px minmax(0, 1fr)" in _css_rule_bodies(styles, ".chat-shell.chat-shell--side-collapsed")[0]
    assert "grid-template-columns: minmax(0, 1fr) 280px" in _css_rule_bodies(
        styles, ".chat-shell.chat-shell--sessions-collapsed"
    )[0]
    assert "grid-template-columns: minmax(0, 1fr)" in _css_rule_bodies(
        styles, ".chat-shell.chat-shell--sessions-collapsed.chat-shell--side-collapsed"
    )[0]

    # 7. 面板隐藏与展开按钮规则
    assert "display: none" in _css_rule_bodies(styles, ".chat-shell.chat-shell--side-collapsed #chat-side-panel")[0]
    assert "display: none" in _css_rule_bodies(styles, ".chat-shell.chat-shell--sessions-collapsed #chat-session-panel")[0]
    assert "display: none" in _css_rule_bodies(styles, "#chat-session-expand-btn[hidden]")[0]
    chat_stack_rules = _css_rule_bodies(styles, ".chat-stack")
    assert any("min-width: 0" in body for body in chat_stack_rules)
    expand_rules = _css_rule_bodies(styles, "#chat-session-expand-btn")
    assert any("position: absolute" in body for body in expand_rules)
    collapsed_header_rules = _css_rule_bodies(
        styles, ".chat-shell.chat-shell--sessions-collapsed .chat-stack > .panel-header"
    )
    assert collapsed_header_rules and "padding-left: 42px" in collapsed_header_rules[0]

    # 8. 图标尺寸 18px
    assert "width: 18px" in _css_rule_bodies(styles, ".chat-session-toggle-btn__icon")[0]
    assert "height: 18px" in _css_rule_bodies(styles, ".chat-session-toggle-btn__icon")[0]
    assert "width: 18px" in _css_rule_bodies(styles, ".chat-session-expand-btn__icon")[0]
    assert "height: 18px" in _css_rule_bodies(styles, ".chat-session-expand-btn__icon")[0]

    # 9. 1100px 媒体查询统一单列
    media_blocks = _css_media_blocks(styles, "@media (max-width: 1100px)")
    media = next((b for b in media_blocks if "chat" in b), "")
    for combo in (
        ".chat-shell",
        ".chat-shell.chat-shell--side-collapsed",
        ".chat-shell.chat-shell--sessions-collapsed",
        ".chat-shell.chat-shell--sessions-collapsed.chat-shell--side-collapsed",
    ):
        assert combo in media, f"1100px media must cover {combo}"
    assert "grid-template-columns: 1fr" in media

    # 10. 三个按钮均有 :focus-visible 规则
    for focus_selector in (
        ".chat-side-toggle-btn:focus-visible",
        ".chat-session-toggle-btn:focus-visible",
        "#chat-session-expand-btn:focus-visible",
    ):
        rules = _css_rule_bodies(styles, focus_selector)
        assert rules, f"missing focus-visible rule for {focus_selector}"
        assert "outline: 2px solid var(--color-primary)" in rules[0]

    # 11. reduced-motion 覆盖按钮与 SVG 面板子元素
    reduced_blocks = _css_media_blocks(styles, "@media (prefers-reduced-motion: reduce)")
    reduced = next(
        (b for b in reduced_blocks if "chat" in b), ""
    )
    assert reduced, "missing prefers-reduced-motion media block for chat toggles"
    for selector in (
        ".chat-side-toggle-btn",
        ".chat-session-toggle-btn",
        "#chat-session-expand-btn",
        ".side-toggle__panel",
        ".session-toggle__panel",
    ):
        assert selector in reduced, f"reduced-motion must cover {selector}"
        assert "transition: none" in reduced

    # 12. chat.js 折叠函数体约束（T4 实现，RED 直至实现完成）
    forbidden = (
        "innerHTML", "eval", "Function", "fetch", "localStorage",
        "currentSessionId", "setTimeout", "setInterval", "requestAnimationFrame",
        "chat-side-toggle-btn", "chat-side-panel", "chat-shell--side-collapsed",
    )
    for fn in ("syncSessionsCollapse", "bindSessionsToggle"):
        body = _js_function_body(chat_js, fn)
        assert body is not None, f"chat.js must define {fn}"
        for token in forbidden:
            assert token not in body, f"{fn} must not contain {token}"


def test_chat_js_node_syntax_check():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    result = subprocess.run(
        [node, "--check", str(CHAT_JS)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_chat_frontend_harness():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    result = subprocess.run(
        [node, str(HARNESS_JS)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all tests passed" in result.stdout


def test_chat_session_source_filter_uses_standard_modal():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    source = CHAT_JS.read_text(encoding="utf-8")
    styles = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")

    assert 'id="chat-session-filter-btn"' in html
    assert 'aria-label="筛选会话类型"' in html
    assert 'class="chat-session-filter-btn__icon"' in html
    assert "grid-template-columns: repeat(4, minmax(0, 1fr))" in styles
    # 勾选框与文案必须同行：option 为横向 flex，且 checkbox 不被表单基线样式拉伸
    assert ".providers-form .session-source-filter__option { display: inline-flex; flex-direction: row;" in styles
    assert '.providers-form .session-source-filter__option input[type="checkbox"]' in styles
    assert "modal-backdrop" in source
    assert "modal-dialog" in source
    assert "providers-form" in source
    assert "nagent.chat.session-source-filter.v1" in source


def test_chat_session_search_assets():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    styles = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    source = CHAT_JS.read_text(encoding="utf-8")
    tree = _HtmlTree(html)

    search_nodes = tree.find_all(lambda n: n["attrs"].get("id") == "chat-session-search-btn")
    assert len(search_nodes) == 1
    search_btn = search_nodes[0]
    assert search_btn["tag"] == "button"
    assert search_btn["attrs"].get("type") == "button"
    assert search_btn["attrs"].get("aria-label") == "搜索会话"
    assert search_btn["attrs"].get("title") == "搜索会话"
    assert "aria-controls" not in search_btn["attrs"]
    assert "aria-expanded" not in search_btn["attrs"]
    assert any(c["tag"] == "svg" and c["attrs"].get("aria-hidden") == "true" for c in search_btn["children"])

    actions = [n for n in tree.ancestors(search_btn) if "chat-session-panel__actions" in (n["attrs"].get("class") or "")][-1]
    action_ids = [n["attrs"].get("id") for n in actions["children"]]
    assert action_ids.index("chat-session-toggle-btn") < action_ids.index("chat-session-search-btn") < action_ids.index("chat-session-filter-btn")
    assert not tree.find_all(lambda n: n["attrs"].get("id") == "chat-session-search-modal")
    dialog_bodies = _css_rule_bodies(styles, "#chat-session-search-modal .session-search-modal__dialog")
    form_bodies = _css_rule_bodies(styles, "#chat-session-search-modal .session-search-modal__form")
    results_bodies = _css_rule_bodies(styles, "#chat-session-search-modal #chat-session-search-results")
    modal_bodies = _css_rule_bodies(styles, ".modal-dialog")
    assert dialog_bodies and form_bodies and results_bodies and modal_bodies

    dialog = dialog_bodies[0]
    for declaration in ("display: flex", "flex-direction: column", "height: 480px", "min-height: 0", "overflow: hidden"):
        assert declaration in dialog
    assert "overflow-y: auto" not in dialog

    assert not any("max-height" in body or "overflow-y: auto" in body for body in form_bodies)
    assert "flex: 1 1 auto" in form_bodies[0]
    assert "min-height: 0" in form_bodies[0]

    results = results_bodies[0]
    for declaration in ("display: flex", "flex: 1 1 auto", "flex-direction: column", "min-height: 0", "overflow-y: auto"):
        assert declaration in results
    assert not any("max-height" in body or re.search(r"(?<![-\\w])height\\s*:", body) for body in results_bodies)
    assert "max-height: calc(100vh - 48px)" in modal_bodies[0]
    assert "chat-session-search-btn:focus-visible" in styles
    assert "function openSessionSearchModal()" in source
    assert "currentSessionSearch" in source


def test_browser_result_link_uses_browser_session_path():
    """The header browser-view link opens the dedicated browser-session page.

    The link now lives on the chat header (session-id suffix), not on the
    tool-call card, and is only rendered when the session used a browser tool.
    """
    source = (STATIC_DIR / "chat.js").read_text(encoding="utf-8")
    assert "'/browser/session?nagent=' + encodeURIComponent(id)" in source
    # 浏览器视图链接仅在 header (appendHeaderLink) 渲染，工具调用卡片不再渲染。
    # 制品消息的详情链接 (el.appendChild(link)) 属制品工作台需求 (prd 03-specs L90)，不在本约束范围。
    assert "appendHeaderLink(header, '浏览器视图', '/browser/session?nagent=' + encodeURIComponent(id))" in source
    # 会话承接浏览器工具调用时，标题的会话 ID 后缀 `(浏览器视图)` 链接。
    assert "function sessionHasBrowserTool(" in source
    assert "buildHeaderLinks(detail, currentSessionId)" in source

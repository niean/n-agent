"""Shared CSS parsing helpers for static-asset frontend tests.

Extracted from test_chat_frontend.py / test_static_assets.py so both files use
one implementation of rule-body and media-block extraction.
"""

import re


def _strip_css_comments(css):
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _css_rule_bodies(css, selector):
    """按选择器提取完整 CSS 规则体（花括号配对，支持逗号组合选择器）。"""
    css = _strip_css_comments(css)
    bodies = []
    idx = 0
    while True:
        i = css.find(selector, idx)
        if i == -1:
            return bodies
        end = i + len(selector)
        brace = css.find("{", end)
        between = css[end:brace] if brace != -1 else "x"
        boundary_ok = i == 0 or css[i - 1] in "}\n{;, \t"
        # between 只能是组合选择器的剩余部分（不含声明区字符）
        selector_tail_ok = between != "x" and not re.search(r"[;{}]", between)
        if boundary_ok and selector_tail_ok:
            # 回溯取完整选择器组，确认目标选择器是其中独立一项
            group_start = max(css.rfind("}", 0, i), css.rfind("{", 0, i), css.rfind(";", 0, i)) + 1
            group = css[group_start:brace]
            items = [item.strip() for item in group.split(",")]
            if selector in items:
                depth, k = 1, brace + 1
                while k < len(css) and depth > 0:
                    if css[k] == "{":
                        depth += 1
                    elif css[k] == "}":
                        depth -= 1
                    k += 1
                bodies.append(css[brace + 1 : k - 1])
                idx = k
            else:
                idx = end
        else:
            idx = end


def _css_media_blocks(css, media_query):
    css = _strip_css_comments(css)
    blocks = []
    idx = 0
    while True:
        i = css.find(media_query, idx)
        if i == -1:
            return blocks
        j = css.find("{", i)
        depth, k = 1, j + 1
        while k < len(css) and depth > 0:
            if css[k] == "{":
                depth += 1
            elif css[k] == "}":
                depth -= 1
            k += 1
        blocks.append(css[j + 1 : k - 1])
        idx = k

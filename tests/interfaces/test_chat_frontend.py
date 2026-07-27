import shutil
import subprocess
from pathlib import Path

import pytest

from app.interfaces.http.dashboard import STATIC_DIR

CHAT_JS = STATIC_DIR / "chat.js"
HARNESS_JS = Path(__file__).parent / "chat_frontend_harness.js"


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


def test_browser_result_link_uses_browser_session_path():
    """The header browser-view link opens the dedicated browser-session page.

    The link now lives on the chat header (session-id suffix), not on the
    tool-call card, and is only rendered when the session used a browser tool.
    """
    source = (STATIC_DIR / "chat.js").read_text(encoding="utf-8")
    assert "link.href = '/browser/session?nagent='" in source
    # 工具调用卡片不再渲染浏览器视图链接。
    assert "el.appendChild(link);" not in source
    # 会话承接浏览器工具调用时，标题的会话 ID 后缀 `(浏览器视图)` 链接。
    assert "function sessionHasBrowserTool(" in source
    assert "setHeader(currentSessionId, sessionHasBrowserTool(detail))" in source

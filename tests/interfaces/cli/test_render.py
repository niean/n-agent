from __future__ import annotations

from app.interfaces.cli.render import make_console, render_markdown, render_status, render_table, render_tool_call


class _FlushRecorder:
    def __init__(self) -> None:
        self.flushed = 0
        self.output = ""

    def write(self, value: str) -> int:
        self.output += value
        return len(value)

    def flush(self) -> None:
        self.flushed += 1


def test_render_markdown_no_color_respects_env(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    console = make_console()
    render_markdown("# hello", console)


def test_make_console_disables_color_by_default():
    console = make_console(force_terminal=True)
    assert console.no_color is True


def test_make_console_allows_explicit_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("N_AGENT_CLI_COLOR", "always")
    console = make_console(force_terminal=True)
    assert console.no_color is False


def test_render_table_basic():
    console = make_console(force_terminal=False)
    render_table([{"a": "1", "b": "2"}], ["a", "b"], console)


def test_render_status_levels():
    console = make_console(force_terminal=False)
    for level in ("info", "success", "warning", "error"):
        render_status("msg", level, console)


def test_render_tool_call_flushes_immediately():
    output = _FlushRecorder()
    console = make_console(force_terminal=False)
    console.file = output

    render_tool_call({"status": "pending", "name": "calculator", "arguments": {"expression": "2**10"}}, console)

    assert output.flushed >= 1
    assert "[pending] calculator" in output.output

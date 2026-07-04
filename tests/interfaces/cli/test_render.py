from __future__ import annotations

import datetime
from enum import Enum
from types import SimpleNamespace

from app.interfaces.cli.render import (
    make_console,
    render_action,
    render_data,
    render_doctor_data,
    render_doctor_report,
    render_markdown,
    render_object,
    render_paginated,
    render_status,
    render_table,
    render_tool_call,
    resolve_format,
)


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


def test_render_object_json(capsys):
    console = make_console(force_terminal=False)
    render_object({"id": "p1", "name": "P1"}, console, fmt="json")
    out = capsys.readouterr().out
    assert '"id": "p1"' in out
    assert '"name": "P1"' in out


def test_render_object_yaml(capsys):
    console = make_console(force_terminal=False)
    render_object({"id": "p1", "name": "P1"}, console, fmt="yaml")
    out = capsys.readouterr().out
    assert "id: p1" in out
    assert "name: P1" in out


def test_render_object_table(capsys):
    console = make_console(force_terminal=False)
    render_object({"id": "p1", "name": "P1"}, console, fmt="table")
    out = capsys.readouterr().out
    assert "p1" in out
    assert "P1" in out


def test_render_paginated_splits_rows(capsys):
    console = make_console(force_terminal=False)
    rows = [{"i": i} for i in range(5)]
    render_paginated(rows, ["i"], console, page_size=2)
    out = capsys.readouterr().out
    assert "0" in out and "4" in out


def test_render_paginated_empty(capsys):
    console = make_console(force_terminal=False)
    render_paginated([], ["i"], console, page_size=2)
    out = capsys.readouterr().out
    assert "empty" in out


def test_render_doctor_report_marks_fail(capsys):
    console = make_console(force_terminal=False)
    items = [
        {"dimension": "A", "status": "PASS", "detail": "ok"},
        {"dimension": "B", "status": "WARN", "detail": "slow"},
        {"dimension": "C", "status": "FAIL", "detail": "boom"},
    ]
    render_doctor_report(items, console)
    out = capsys.readouterr().out
    assert "A" in out and "B" in out and "C" in out
    assert "PASS" in out and "WARN" in out and "FAIL" in out


def test_resolve_format_default_is_json():
    args = SimpleNamespace(json=False, form=False, yaml=False)
    assert resolve_format(args) == "json"


def test_resolve_format_json_flag_is_noop():
    args = SimpleNamespace(json=True, form=False, yaml=False)
    assert resolve_format(args) == "json"


def test_resolve_format_form_flag():
    args = SimpleNamespace(json=False, form=True, yaml=False)
    assert resolve_format(args) == "form"


def test_resolve_format_yaml_flag():
    args = SimpleNamespace(json=False, form=False, yaml=True)
    assert resolve_format(args) == "yaml"


def test_resolve_format_yaml_takes_precedence_over_form():
    args = SimpleNamespace(json=False, form=True, yaml=True)
    assert resolve_format(args) == "yaml"


def test_resolve_format_missing_attributes_defaults_to_json():
    args = SimpleNamespace()
    assert resolve_format(args) == "json"


def test_render_data_json_dict(capsys):
    render_data({"id": "x", "name": "X"}, make_console(force_terminal=False), fmt="json")
    out = capsys.readouterr().out
    assert '"id": "x"' in out
    assert '"name": "X"' in out


def test_render_data_yaml_dict(capsys):
    render_data({"id": "x", "name": "X"}, make_console(force_terminal=False), fmt="yaml")
    out = capsys.readouterr().out
    assert "id: x" in out
    assert "name: X" in out


def test_render_data_form_dict_uses_render_object(capsys):
    render_data({"id": "x", "name": "X"}, make_console(force_terminal=False), fmt="form")
    out = capsys.readouterr().out
    assert "x" in out
    assert "X" in out


def test_render_data_json_list(capsys):
    render_data([{"id": "1"}, {"id": "2"}], make_console(force_terminal=False), fmt="json")
    out = capsys.readouterr().out
    assert '"id": "1"' in out
    assert '"id": "2"' in out


def test_render_data_yaml_list(capsys):
    render_data([{"id": "1"}], make_console(force_terminal=False), fmt="yaml")
    out = capsys.readouterr().out
    assert "id: '1'" in out or "id: 1" in out


def test_render_data_form_list_renders_table(capsys):
    render_data(
        [{"id": "1", "name": "A"}],
        make_console(force_terminal=False),
        fmt="form",
        headers=["id", "name"],
    )
    out = capsys.readouterr().out
    assert "1" in out
    assert "A" in out


def test_render_data_form_empty_list(capsys):
    render_data([], make_console(force_terminal=False), fmt="form")
    out = capsys.readouterr().out
    assert "empty" in out.lower()


def test_render_action_json(capsys):
    render_action({"deleted": "abc"}, make_console(force_terminal=False), fmt="json")
    out = capsys.readouterr().out
    assert '"deleted": "abc"' in out


def test_render_action_yaml(capsys):
    render_action({"deleted": "abc"}, make_console(force_terminal=False), fmt="yaml")
    out = capsys.readouterr().out
    assert "deleted: abc" in out


def test_render_action_form(capsys):
    render_action({"deleted": "abc"}, make_console(force_terminal=False), fmt="form")
    out = capsys.readouterr().out
    assert "deleted: abc" in out


def test_render_doctor_data_json(capsys):
    items = [{"dimension": "A", "status": "PASS", "detail": "ok"}]
    render_doctor_data(items, make_console(force_terminal=False), fmt="json")
    out = capsys.readouterr().out
    assert '"dimension": "A"' in out
    assert '"status": "PASS"' in out


def test_render_doctor_data_yaml(capsys):
    items = [{"dimension": "A", "status": "PASS", "detail": "ok"}]
    render_doctor_data(items, make_console(force_terminal=False), fmt="yaml")
    out = capsys.readouterr().out
    assert "dimension: A" in out
    assert "status: PASS" in out


def test_render_doctor_data_form_uses_doctor_report(capsys):
    items = [{"dimension": "A", "status": "PASS", "detail": "ok"}]
    render_doctor_data(items, make_console(force_terminal=False), fmt="form")
    out = capsys.readouterr().out
    assert "PASS" in out
    assert "A" in out


class _Status(Enum):
    SUCCEEDED = "succeeded"


class _Execution:
    def __init__(self, id, status, started_at, output):
        self.id = id
        self.status = status
        self.started_at = started_at
        self.output = output


def test_render_data_yaml_serializes_datetime_enum_and_dataclass(capsys):
    data = [
        {
            "id": "exec-1",
            "status": _Status.SUCCEEDED,
            "started_at": datetime.datetime(2026, 7, 4, 8, 17, 56, tzinfo=datetime.timezone.utc),
            "output": "hello",
        }
    ]
    render_data(data, make_console(force_terminal=False), fmt="yaml")
    out = capsys.readouterr().out
    assert "id: exec-1" in out
    assert "_Status.SUCCEEDED" in out
    assert "2026-07-04" in out


def test_render_action_yaml_serializes_non_serializable_value(capsys):
    payload = {"run_at": datetime.datetime(2026, 7, 4, 8, 17, 56, tzinfo=datetime.timezone.utc)}
    render_action(payload, make_console(force_terminal=False), fmt="yaml")
    out = capsys.readouterr().out
    assert "2026-07-04" in out


def test_render_object_yaml_serializes_non_serializable_value(capsys):
    obj = {"run_at": datetime.datetime(2026, 7, 4, 8, 17, 56, tzinfo=datetime.timezone.utc)}
    render_object(obj, make_console(force_terminal=False), fmt="yaml")
    out = capsys.readouterr().out
    assert "2026-07-04" in out


def test_render_doctor_data_yaml_serializes_non_serializable_value(capsys):
    items = [{"dimension": "A", "status": "PASS", "at": datetime.datetime(2026, 7, 4, 8, 17, 56, tzinfo=datetime.timezone.utc)}]
    render_doctor_data(items, make_console(force_terminal=False), fmt="yaml")
    out = capsys.readouterr().out
    assert "2026-07-04" in out

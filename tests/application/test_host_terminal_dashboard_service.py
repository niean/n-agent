from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.application.host_terminal_dashboard_service import (
    HostTerminalDashboardService,
    _derive_target,
    _result_summary,
    _sanitize_arguments,
)
from app.domain.host_terminal_policy import (
    HostCommandRule,
    HostExactArgRule,
    HostOneOfArgRule,
    HostSkillScriptRule,
    HostTerminalPolicySnapshot,
    HostTerminalResourceLimits,
)


def _limits():
    return HostTerminalResourceLimits(
        default_timeout_seconds=120,
        max_timeout_seconds=120,
        max_stdout_bytes=8192,
        max_stderr_bytes=8192,
        max_args=1,
        max_arg_length=256,
        max_total_args_length=256,
        max_concurrency=1,
    )


def _snapshot():
    return HostTerminalPolicySnapshot(
        schema_version=1,
        version="example-v1",
        content_digest="a" * 64,
        loaded_at=datetime(2026, 7, 19, 2, 0, tzinfo=timezone.utc),
        limits=_limits(),
        command_rules=(
            HostCommandRule(
                rule_id="c1",
                executable="/bin/echo",
                positional_args=(HostExactArgRule(value="hi"),),
            ),
        ),
        skill_script_rules=(
            HostSkillScriptRule(
                rule_id="s1",
                skill_name="photo-and-upload",
                script_relative_path="scripts/photo-upload.py",
                sha256="b" * 64,
                positional_args=(HostOneOfArgRule(values=("a", "b")),),
            ),
        ),
    )


class FakeLoader:
    def __init__(self, snapshot, last_error=None):
        self._snapshot = snapshot
        self.last_error_code = last_error

    @property
    def snapshot(self):
        return self._snapshot


class FakeExecutor:
    def __init__(self, health="ok"):
        self.last_health_code = health


class FakeMemoryStore:
    def __init__(self, calls=None, error=False):
        self._calls = list(calls or [])
        self._error = error

    async def list_recent_tool_calls(self, tool_name=None, limit=50):
        if self._error:
            raise RuntimeError("db down")
        out = [c for c in self._calls if tool_name is None or c.tool_name == tool_name]
        return out[:limit]

    async def list_tool_calls(self, session_id):
        if self._error:
            raise RuntimeError("db down")
        # Simulate the real store: ascending by created_at.
        return sorted(self._calls, key=lambda c: c.created_at)


def _call(
    tid="t1",
    tool_name="host_terminal",
    arguments=None,
    result=None,
    status="success",
    created_at=None,
):
    return SimpleNamespace(
        id=tid,
        session_id="sess-1",
        tool_name=tool_name,
        arguments=arguments
        or {
            "target_type": "skill_script",
            "skill": "photo-and-upload",
            "script": "scripts/photo-upload.py",
            "args": [],
        },
        result=result,
        status=status,
        duration_ms=1200,
        created_at=created_at or datetime(2026, 7, 19, 2, 0, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_status_unavailable_when_not_assembled():
    svc = HostTerminalDashboardService(None, None, None, "host_terminal_disabled")
    status = await svc.get_status()
    assert status["enabled"] is False
    assert status["health_code"] == "host_terminal_disabled"
    assert status["policy_version"] is None
    assert status["limits_summary"] is None


@pytest.mark.asyncio
async def test_status_enabled_when_assembled():
    svc = HostTerminalDashboardService(
        FakeLoader(_snapshot()), FakeExecutor("ok"), FakeMemoryStore(), None
    )
    status = await svc.get_status()
    assert status["enabled"] is True
    assert status["health_code"] == "ok"
    assert status["policy_version"] == "example-v1"
    assert status["policy_content_digest"].startswith("aaaaaaaa")
    assert status["limits_summary"]["max_concurrency"] == 1
    assert status["policy_last_error"] is None


@pytest.mark.asyncio
async def test_status_snapshot_none_uses_loader_last_error():
    loader = FakeLoader(None, last_error="host_policy_load_failed")
    svc = HostTerminalDashboardService(loader, FakeExecutor(), FakeMemoryStore(), "host_bridge_not_checked")
    status = await svc.get_status()
    assert status["enabled"] is False
    assert status["policy_last_error"] == "host_policy_load_failed"


@pytest.mark.asyncio
async def test_policy_null_when_snapshot_none():
    loader = FakeLoader(None, last_error="host_policy_load_failed")
    svc = HostTerminalDashboardService(loader, FakeExecutor(), FakeMemoryStore(), "host_bridge_not_checked")
    policy = await svc.get_policy()
    assert policy["enabled"] is False
    assert policy["version"] is None
    assert policy["limits"] is None
    assert policy["command_rules"] == []
    assert policy["skill_script_rules"] == []
    assert policy["policy_last_error"] == "host_policy_load_failed"


@pytest.mark.asyncio
async def test_policy_returns_rules_with_short_sha_and_all_limits():
    svc = HostTerminalDashboardService(
        FakeLoader(_snapshot()), FakeExecutor(), FakeMemoryStore(), None
    )
    policy = await svc.get_policy()
    assert policy["enabled"] is True
    assert set(policy["limits"].keys()) == {
        "default_timeout_seconds",
        "max_timeout_seconds",
        "max_stdout_bytes",
        "max_stderr_bytes",
        "max_args",
        "max_arg_length",
        "max_total_args_length",
        "max_concurrency",
    }
    assert len(policy["command_rules"]) == 1
    assert policy["command_rules"][0]["executable"] == "/bin/echo"
    assert policy["command_rules"][0]["positional_args"] == ["hi"]
    assert len(policy["skill_script_rules"]) == 1
    sha = policy["skill_script_rules"][0]["sha256"]
    assert sha.startswith("bbbbbbbb") and sha.endswith("…")
    assert policy["skill_script_rules"][0]["positional_args"] == ["a|b"]


@pytest.mark.asyncio
async def test_history_derives_target_and_desensitizes_result():
    calls = [
        _call(
            result={"capture_size": 10, "upload_http": 200, "signed_url": "https://secret/signed"},
        )
    ]
    svc = HostTerminalDashboardService(
        FakeLoader(_snapshot()), FakeExecutor(), FakeMemoryStore(calls), None
    )
    history = await svc.list_history()
    assert len(history) == 1
    item = history[0]
    assert item["target_type"] == "skill_script"
    assert item["target"] == "photo-and-upload/scripts/photo-upload.py"
    assert item["result_summary"] == "photo uploaded"
    assert "signed_url" not in item["arguments"]
    assert "secret" not in str(item)


@pytest.mark.asyncio
async def test_history_filters_non_host_terminal_calls():
    calls = [_call(tid="t1"), _call(tid="t2", tool_name="execute_code")]
    svc = HostTerminalDashboardService(
        FakeLoader(_snapshot()), FakeExecutor(), FakeMemoryStore(calls), None
    )
    history = await svc.list_history()
    assert [h["id"] for h in history] == ["t1"]


@pytest.mark.asyncio
async def test_history_session_descending_and_limited():
    base = datetime(2026, 7, 19, 2, 0, tzinfo=timezone.utc)
    calls = [
        _call(tid="old", created_at=base),
        _call(tid="new", created_at=datetime(2026, 7, 19, 3, 0, tzinfo=timezone.utc)),
        _call(tid="mid", created_at=datetime(2026, 7, 19, 2, 30, tzinfo=timezone.utc)),
    ]
    svc = HostTerminalDashboardService(
        FakeLoader(_snapshot()), FakeExecutor(), FakeMemoryStore(calls), None
    )
    history = await svc.list_history(session_id="sess-1", limit=2)
    assert [h["id"] for h in history] == ["new", "mid"]


@pytest.mark.asyncio
async def test_history_empty_when_memory_store_none():
    svc = HostTerminalDashboardService(
        FakeLoader(_snapshot()), FakeExecutor(), None, None
    )
    assert await svc.list_history() == []


@pytest.mark.asyncio
async def test_history_memory_error_propagates():
    svc = HostTerminalDashboardService(
        FakeLoader(_snapshot()), FakeExecutor(), FakeMemoryStore(error=True), None
    )
    with pytest.raises(RuntimeError):
        await svc.list_history()


@pytest.mark.asyncio
async def test_history_malformed_record_degrades():
    calls = [
        SimpleNamespace(id="bad", session_id="s", tool_name="host_terminal",
                        arguments="not a dict", result=None, status="error",
                        duration_ms=1, created_at="not-a-date"),
        _call(tid="good"),
    ]
    svc = HostTerminalDashboardService(
        FakeLoader(_snapshot()), FakeExecutor(), FakeMemoryStore(calls), None
    )
    history = await svc.list_history()
    ids = {h["id"] for h in history}
    assert ids == {"bad", "good"}
    bad = next(h for h in history if h["id"] == "bad")
    assert bad["target_type"] == "-"
    assert bad["target"] == "-"
    assert bad["created_at"] == "not-a-date" or bad["created_at"] is None


@pytest.mark.asyncio
async def test_history_arguments_whitelist_strips_extra_fields():
    calls = [
        _call(
            arguments={
                "target_type": "command",
                "command": "/bin/echo",
                "args": ["hi"],
                "timeout": 30,
                "secret_field": "sensitive",
            }
        )
    ]
    svc = HostTerminalDashboardService(
        FakeLoader(_snapshot()), FakeExecutor(), FakeMemoryStore(calls), None
    )
    history = await svc.list_history()
    assert "secret_field" not in history[0]["arguments"]
    assert history[0]["arguments"]["command"] == "/bin/echo"


@pytest.mark.asyncio
async def test_history_result_summary_no_stdout_stderr_exception():
    calls = [
        _call(tid="a", result={"success": True, "stdout": "LEAK"}),
        _call(tid="b", result={"error": "host_execution_failed", "stderr": "LEAK"}),
        _call(tid="c", result={"error": "boom", "exception": "traceback"}),
    ]
    svc = HostTerminalDashboardService(
        FakeLoader(_snapshot()), FakeExecutor(), FakeMemoryStore(calls), None
    )
    history = {h["id"]: h["result_summary"] for h in await svc.list_history()}
    assert history["a"] == "success"
    assert history["b"] == "error: host_execution_failed"
    assert "LEAK" not in str(history)
    assert "traceback" not in str(history).lower()


@pytest.mark.asyncio
async def test_history_result_summary_unwraps_persisted_wrapper_shape():
    # Real tool_call records persist result as {tool_call_id, name, status, content, duration_ms};
    # signed_url lives under content and must be summarized without leaking.
    calls = [
        _call(
            tid="photo",
            result={
                "tool_call_id": "call_photo",
                "name": "host_terminal",
                "status": "success",
                "content": {
                    "capture_size": 44429,
                    "upload_http": 200,
                    "signed_url": "https://secret/signed-url-with-token",
                },
                "duration_ms": 3817,
            },
        ),
        _call(
            tid="plain",
            result={
                "tool_call_id": "call_plain",
                "name": "host_terminal",
                "status": "success",
                "content": {"success": True},
                "duration_ms": 100,
            },
        ),
    ]
    svc = HostTerminalDashboardService(
        FakeLoader(_snapshot()), FakeExecutor(), FakeMemoryStore(calls), None
    )
    history = {h["id"]: h["result_summary"] for h in await svc.list_history()}
    assert history["photo"] == "photo uploaded"
    assert history["plain"] == "success"
    assert "secret" not in str(history)
    assert "signed-url" not in str(history)


def test_derive_target_command_basename():
    assert _derive_target({"target_type": "command", "command": "/bin/echo", "args": []}) == (
        "command",
        "echo",
    )


def test_derive_target_invalid():
    assert _derive_target("not a dict") == ("-", "-")


def test_result_summary_unknown():
    assert _result_summary(None) == "-"
    assert _result_summary({"weird": 1}) == "-"


def test_sanitize_arguments_non_dict():
    assert _sanitize_arguments("x") == {}


def test_sanitize_arguments_strips_extra():
    out = _sanitize_arguments(
        {"target_type": "command", "command": "/bin/echo", "args": [], "extra": "x"}
    )
    assert "extra" not in out
    assert out["target_type"] == "command"

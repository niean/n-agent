from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.infrastructure.sandbox.manager import SandboxManager
from app.infrastructure.sandbox.released_registry import SQLiteReleasedSandboxRegistry


class _FakeSandbox:
    """Minimal sandbox stub that records cleanup calls without touching Docker."""

    def __init__(self) -> None:
        self.container_status: str | None = "running"
        self.cleanup_calls = 0

    async def cleanup_container(self) -> None:
        self.cleanup_calls += 1
        self.container_status = None


def _make_settings() -> SimpleNamespace:
    return SimpleNamespace(
        sandbox_docker_image="python:3.11-slim",
        sandbox_docker_cpus=1.0,
        sandbox_docker_memory_mb=512,
        sandbox_docker_network=False,
        sandbox_docker_host_workspace_root=None,
        sandbox_docker_host_locals_root=None,
    )


def _make_manager(tmp_path: Path, idle_seconds: int = 900) -> SandboxManager:
    return SandboxManager(
        sandbox_type="local",
        workspace_root=tmp_path,
        idle_seconds=idle_seconds,
        settings=_make_settings(),
        callback_registry=SimpleNamespace(),
        scratch_root=tmp_path / "scratch",
    )


def _seed_session(
    mgr: SandboxManager,
    sid: str,
    sandbox: _FakeSandbox,
    created: datetime,
    last_used: datetime,
) -> None:
    mgr._sandboxes[sid] = sandbox
    mgr._scratch_roots[sid] = mgr.scratch_root / sid
    mgr._scratch_roots[sid].mkdir(parents=True, exist_ok=True)
    mgr._created_at[sid] = created
    mgr._last_used[sid] = last_used
    mgr._locks[sid] = asyncio.Lock()


@pytest.mark.asyncio
async def test_reaper_keeps_recently_used_sandbox(tmp_path: Path):
    """Sandbox used within idle_seconds should not be reaped, regardless of age."""
    mgr = _make_manager(tmp_path, idle_seconds=900)
    now = datetime.now(timezone.utc)
    # Created 2 hours ago but used 10s ago — should survive.
    sandbox = _FakeSandbox()
    _seed_session(
        mgr,
        "sess-old-but-active",
        sandbox,
        created=now - timedelta(hours=2),
        last_used=now - timedelta(seconds=10),
    )

    await mgr._reap_once()

    assert sandbox.cleanup_calls == 0
    assert "sess-old-but-active" in mgr._sandboxes


@pytest.mark.asyncio
async def test_reaper_cooperative_release_for_idle_sandbox(tmp_path: Path):
    mgr = _make_manager(tmp_path, idle_seconds=900)
    now = datetime.now(timezone.utc)
    # Created 5 minutes ago but idle for 30 minutes
    sandbox = _FakeSandbox()
    _seed_session(
        mgr,
        "sess-idle",
        sandbox,
        created=now - timedelta(minutes=5),
        last_used=now - timedelta(minutes=30),
    )

    await mgr._reap_once()

    assert sandbox.cleanup_calls == 1
    assert "sess-idle" not in mgr._sandboxes


@pytest.mark.asyncio
async def test_released_sandbox_history_uses_persistent_registry(tmp_path: Path):
    registry = SQLiteReleasedSandboxRegistry(tmp_path / "sessions.db")
    mgr = SandboxManager(
        sandbox_type="local",
        workspace_root=tmp_path,
        idle_seconds=900,
        settings=_make_settings(),
        callback_registry=SimpleNamespace(),
        scratch_root=tmp_path / "scratch",
        released_registry=registry,
    )
    now = datetime.now(timezone.utc)
    sandbox = _FakeSandbox()
    _seed_session(
        mgr,
        "sess-persisted",
        sandbox,
        created=now - timedelta(minutes=5),
        last_used=now - timedelta(minutes=30),
    )

    await mgr.release("sess-persisted", reason="idle")

    fresh_registry = SQLiteReleasedSandboxRegistry(tmp_path / "sessions.db")
    fresh_mgr = SandboxManager(
        sandbox_type="local",
        workspace_root=tmp_path,
        idle_seconds=900,
        settings=_make_settings(),
        callback_registry=SimpleNamespace(),
        scratch_root=tmp_path / "scratch-2",
        released_registry=fresh_registry,
    )
    released = fresh_mgr.list_released()
    assert [item.session_id for item in released] == ["sess-persisted"]
    assert released[0].reason == "idle"
    assert fresh_mgr._released == []


@pytest.mark.asyncio
async def test_delete_released_delegates_to_persistent_registry(tmp_path: Path):
    registry = SQLiteReleasedSandboxRegistry(tmp_path / "sessions.db")
    mgr = SandboxManager(
        sandbox_type="local",
        workspace_root=tmp_path,
        idle_seconds=900,
        settings=_make_settings(),
        callback_registry=SimpleNamespace(),
        scratch_root=tmp_path / "scratch",
        released_registry=registry,
    )
    now = datetime.now(timezone.utc)
    sandbox = _FakeSandbox()
    _seed_session(
        mgr,
        "sess-delete",
        sandbox,
        created=now - timedelta(minutes=5),
        last_used=now - timedelta(minutes=30),
    )
    await mgr.release("sess-delete", reason="manual")

    target_id = mgr.list_released()[0].id

    deleted = mgr.delete_released(target_id)

    assert deleted is True
    assert mgr.list_released() == []


def test_delete_released_unknown_id_returns_false(tmp_path: Path):
    registry = SQLiteReleasedSandboxRegistry(tmp_path / "sessions.db")
    mgr = SandboxManager(
        sandbox_type="local",
        workspace_root=tmp_path,
        idle_seconds=900,
        settings=_make_settings(),
        callback_registry=SimpleNamespace(),
        scratch_root=tmp_path / "scratch",
        released_registry=registry,
    )

    assert mgr.delete_released("nonexistent-id") is False
    assert mgr.delete_released("") is False


@pytest.mark.asyncio
async def test_reaper_skips_active_sandbox_under_idle_threshold(tmp_path: Path):
    mgr = _make_manager(tmp_path, idle_seconds=900)
    now = datetime.now(timezone.utc)
    sandbox = _FakeSandbox()
    _seed_session(
        mgr,
        "sess-active",
        sandbox,
        created=now - timedelta(minutes=2),
        last_used=now - timedelta(seconds=30),
    )

    await mgr._reap_once()

    assert sandbox.cleanup_calls == 0
    assert "sess-active" in mgr._sandboxes


def test_idle_seconds_must_be_positive(tmp_path: Path):
    with pytest.raises(ValueError, match="idle_seconds"):
        _make_manager(tmp_path, idle_seconds=0)


@pytest.mark.asyncio
async def test_cleanup_orphan_containers_skips_local_sandbox(tmp_path: Path, monkeypatch):
    """Local sandbox type has no docker containers to clean; should no-op."""
    mgr = _make_manager(tmp_path)  # sandbox_type='local'
    called = []

    async def _fake_exec(*args, **kwargs):
        called.append(args)
        return 0

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    count = await mgr.cleanup_orphan_containers()
    assert count == 0
    assert called == []  # no docker calls at all


@pytest.mark.asyncio
async def test_cleanup_orphan_containers_removes_leftover_docker_containers(tmp_path: Path, monkeypatch):
    """Docker sandbox type should kill+rm all nagent-sandbox-* containers."""
    settings = _make_settings()
    mgr = SandboxManager(
        sandbox_type="docker",
        workspace_root=tmp_path,
        idle_seconds=900,
        settings=settings,
        callback_registry=SimpleNamespace(),
        scratch_root=tmp_path / "scratch",
    )

    calls = []

    class _FakeProc:
        def __init__(self, stdout: bytes = b"") -> None:
            self.stdout = stdout
            self.stderr = b""
            self.returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (self.stdout, self.stderr)

    async def _fake_exec(*args, **kwargs):
        calls.append(args)
        if "ps" in args:
            return _FakeProc(b"nagent-sandbox-sess-a-1234\nnagent-sandbox-sess-b-5678\n")
        return _FakeProc(b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(asyncio, "wait_for", lambda coro, timeout: coro)

    count = await mgr.cleanup_orphan_containers()
    assert count == 2
    # Expect: 1 ps + 2 kill + 2 rm = 5 docker calls
    assert len(calls) == 5
    assert calls[0][1] == "ps"
    kill_names = [c[2] for c in calls if c[1] == "kill"]
    rm_names = [c[3] for c in calls if c[1] == "rm"]
    assert sorted(kill_names) == ["nagent-sandbox-sess-a-1234", "nagent-sandbox-sess-b-5678"]
    assert sorted(rm_names) == ["nagent-sandbox-sess-a-1234", "nagent-sandbox-sess-b-5678"]


def test_safe_session_segment_truncates_to_fit_uds_path_limit():
    """UDS path must stay under UNIX_PATH_MAX=108 bytes even with long session_id."""
    from app.infrastructure.sandbox.manager import _safe_session_segment

    # Simulate a long feishu-style session_id (chat_id + user_id, ~50 chars)
    long_session_id = "oc_" + "a" * 50 + "_user_" + "b" * 30
    safe = _safe_session_segment(long_session_id)
    # sess- prefix + 24 chars max = 29 chars
    assert len(safe) <= 29
    assert safe.startswith("sess-")

    # Worst-case UDS path: scratch_root(50) + /sess- + 24 + /call- + 8 + /rpc.sock
    # = 50 + 1 + 5 + 24 + 1 + 5 + 8 + 1 + 8 = 103 bytes < 108
    scratch_root_len = 50
    call_uuid = "call-12345678"  # 13 chars (call- + 8 hex)
    uds_path = (
        "x" * scratch_root_len
        + "/" + safe
        + "/" + call_uuid
        + "/rpc.sock"
    )
    assert len(uds_path) < 108, f"UDS path too long: {len(uds_path)} bytes"


def test_new_call_staging_uses_short_uuid():
    """call-uuid must be short (8 hex) to keep UDS path under 108 bytes."""
    import asyncio
    mgr = _make_manager(Path("/tmp"), idle_seconds=900)
    # Seed a session so new_call_staging can find the scratch root
    sandbox = _FakeSandbox()
    now = datetime.now(timezone.utc)
    _seed_session(mgr, "sess-uds-test", sandbox, now, now)

    staging = mgr.new_call_staging("sess-uds-test")
    # call- + 8 hex = 13 chars
    assert staging.name.startswith("call-")
    suffix = staging.name.removeprefix("call-")
    assert len(suffix) == 8

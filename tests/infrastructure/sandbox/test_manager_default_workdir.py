"""SandboxManager.default_workdir and constructor param passing tests.

Covers T2 of plan-260707-terminal-in-sandbox:
- default_workdir for Docker returns /scratch/<safe>
- default_workdir for Local returns host <scratch_root>/<safe> and creates the dir
- default_workdir creates host-side scratch/<safe> for Docker (bind-mount target)
- default_workdir is callable BEFORE get_or_create() (terminal tool may use it first)
- get_or_create() passes settings.sandbox_max_stdout_bytes / sandbox_max_stderr_bytes
  to DockerSandbox and LocalSandbox constructors
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.infrastructure.sandbox.manager import SandboxManager, _safe_session_segment


def _make_settings(**overrides) -> SimpleNamespace:
    defaults = dict(
        sandbox_docker_image="python:3.11-slim",
        sandbox_docker_cpus=1.0,
        sandbox_docker_memory_mb=512,
        sandbox_docker_network=False,
        sandbox_docker_host_workspace_root=None,
        sandbox_max_stdout_bytes=50000,
        sandbox_max_stderr_bytes=10000,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_manager(
    tmp_path: Path,
    *,
    sandbox_type: str = "local",
    settings: SimpleNamespace | None = None,
    scratch_root: Path | None = None,
) -> SandboxManager:
    return SandboxManager(
        sandbox_type=sandbox_type,
        workspace_root=tmp_path / "workspace",
        idle_seconds=900,
        settings=settings or _make_settings(),
        callback_registry=SimpleNamespace(),
        scratch_root=scratch_root or (tmp_path / "scratch"),
    )


def test_default_workdir_docker_returns_container_scratch_path(tmp_path: Path):
    """Docker default_workdir returns /scratch/<safe>, host-side scratch/<safe> exists."""
    mgr = _make_manager(tmp_path, sandbox_type="docker")
    session_id = "oc_abc123_user_def456"
    workdir = mgr.default_workdir(session_id)
    safe = _safe_session_segment(session_id)
    assert workdir == f"/scratch/{safe}"


def test_default_workdir_docker_creates_host_scratch(tmp_path: Path):
    """default_workdir for Docker creates host-side scratch/<safe> for bind-mount target."""
    mgr = _make_manager(tmp_path, sandbox_type="docker")
    session_id = "sess-docker-new"
    safe = _safe_session_segment(session_id)
    host_dir = mgr.scratch_root / safe
    assert not host_dir.exists()
    mgr.default_workdir(session_id)
    assert host_dir.is_dir()


def test_default_workdir_docker_callable_before_get_or_create(tmp_path: Path):
    """default_workdir must be callable BEFORE get_or_create() — terminal tool may use it first."""
    mgr = _make_manager(tmp_path, sandbox_type="docker")
    session_id = "sess-pre-create"
    workdir = mgr.default_workdir(session_id)
    assert workdir.startswith("/scratch/")
    # Manager should not have created a sandbox instance yet
    assert session_id not in mgr._sandboxes


def test_default_workdir_local_returns_host_path(tmp_path: Path):
    """Local default_workdir returns host <scratch_root>/<safe>."""
    mgr = _make_manager(tmp_path, sandbox_type="local")
    session_id = "oc_abc123_user_def456"
    workdir = mgr.default_workdir(session_id)
    safe = _safe_session_segment(session_id)
    assert workdir == str(mgr.scratch_root / safe)


def test_default_workdir_local_creates_directory(tmp_path: Path):
    """Local default_workdir creates the host directory."""
    mgr = _make_manager(tmp_path, sandbox_type="local")
    session_id = "sess-local-new"
    safe = _safe_session_segment(session_id)
    target = mgr.scratch_root / safe
    assert not target.exists()
    mgr.default_workdir(session_id)
    assert target.is_dir()


def test_default_workdir_uses_safe_session_segment(tmp_path: Path):
    """default_workdir must use _safe_session_segment — never raw session_id."""
    mgr = _make_manager(tmp_path, sandbox_type="local")
    # Session id with dangerous chars
    session_id = "../etc/passwd"
    workdir = mgr.default_workdir(session_id)
    safe = _safe_session_segment(session_id)
    # workdir must contain the safe segment, not the raw "../etc/passwd"
    assert safe in workdir
    assert "../etc" not in workdir
    # And the directory must be created inside scratch_root
    assert (mgr.scratch_root / safe).is_dir()


def test_default_workdir_idempotent(tmp_path: Path):
    """Calling default_workdir twice with same session_id doesn't error."""
    mgr = _make_manager(tmp_path, sandbox_type="local")
    session_id = "sess-idempotent"
    w1 = mgr.default_workdir(session_id)
    w2 = mgr.default_workdir(session_id)
    assert w1 == w2


@pytest.mark.asyncio
async def test_get_or_create_docker_passes_truncation_params(tmp_path: Path):
    """get_or_create() for Docker passes max_stdout_bytes/max_stderr_bytes from settings."""
    settings = _make_settings(sandbox_max_stdout_bytes=12345, sandbox_max_stderr_bytes=678)
    mgr = _make_manager(tmp_path, sandbox_type="docker", settings=settings)
    sandbox = await mgr.get_or_create("sess-docker-trunc")
    assert sandbox.max_stdout_bytes == 12345
    assert sandbox.max_stderr_bytes == 678


@pytest.mark.asyncio
async def test_get_or_create_local_passes_truncation_params(tmp_path: Path):
    """get_or_create() for Local passes max_stdout_bytes/max_stderr_bytes from settings."""
    settings = _make_settings(sandbox_max_stdout_bytes=12345, sandbox_max_stderr_bytes=678)
    mgr = _make_manager(tmp_path, sandbox_type="local", settings=settings)
    sandbox = await mgr.get_or_create("sess-local-trunc")
    assert sandbox.max_stdout_bytes == 12345
    assert sandbox.max_stderr_bytes == 678

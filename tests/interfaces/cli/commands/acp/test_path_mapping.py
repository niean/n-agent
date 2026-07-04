from pathlib import Path

import pytest

from app.config import Settings
from app.interfaces.cli.commands.acp.path_mapping import map_cwd


def _settings(
    tmp_path: Path,
    host_root: Path | None = None,
    container_root: Path | None = None,
) -> Settings:
    return Settings(
        provider_base_url="https://example.test/v1",
        provider_api_key="test-key",
        provider_model="test-model",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path / "container-ws"),
        acp_host_workspace_root=host_root,
        acp_container_workspace_root=container_root,
        _env_file=None,
    )


def test_map_cwd_translates_host_root_to_container_root(tmp_path: Path):
    host_root = tmp_path / "host-ws"
    host_root.mkdir()
    container_root = tmp_path / "container-ws"
    container_root.mkdir()
    settings = _settings(tmp_path, host_root=host_root, container_root=container_root)

    result = map_cwd(str(host_root / "project-a"), settings)

    assert result == str(container_root.resolve() / "project-a")


def test_map_cwd_uses_container_root_when_cwd_already_in_container(tmp_path: Path):
    container_root = tmp_path / "container-ws"
    container_root.mkdir()
    settings = _settings(tmp_path, host_root=None, container_root=container_root)

    result = map_cwd(str(container_root / "project-b"), settings)

    assert result == str(container_root.resolve() / "project-b")


def test_map_cwd_returns_container_root_for_empty_cwd(tmp_path: Path):
    container_root = tmp_path / "container-ws"
    settings = _settings(tmp_path, container_root=container_root)

    assert map_cwd("", settings) == str(container_root.resolve())
    assert map_cwd(None, settings) == str(container_root.resolve())


def test_map_cwd_returns_none_when_not_mappable(tmp_path: Path):
    host_root = tmp_path / "host-ws"
    host_root.mkdir()
    container_root = tmp_path / "container-ws"
    settings = _settings(tmp_path, host_root=host_root, container_root=container_root)

    result = map_cwd("/etc/passwd", settings)
    assert result is None


def test_map_cwd_handles_trailing_slash(tmp_path: Path):
    host_root = tmp_path / "host-ws"
    host_root.mkdir()
    container_root = tmp_path / "container-ws"
    container_root.mkdir()
    settings = _settings(tmp_path, host_root=host_root, container_root=container_root)

    result = map_cwd(str(host_root / "project-a") + "/", settings)

    assert result == str(container_root.resolve() / "project-a")


def test_map_cwd_rejects_relative_path(tmp_path: Path):
    container_root = tmp_path / "container-ws"
    settings = _settings(tmp_path, container_root=container_root)

    assert map_cwd("./project-a", settings) is None
    assert map_cwd("../project-a", settings) is None


def test_map_cwd_falls_back_to_workspace_root_when_container_root_unset(tmp_path: Path):
    host_root = tmp_path / "host-ws"
    host_root.mkdir()
    fallback_ws = tmp_path / "fallback-ws"
    settings = Settings(
        provider_base_url="https://example.test/v1",
        provider_api_key="test-key",
        provider_model="test-model",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(fallback_ws),
        acp_host_workspace_root=host_root,
        acp_container_workspace_root=None,
        _env_file=None,
    )

    result = map_cwd(str(host_root / "project-c"), settings)

    assert result == str(fallback_ws.resolve() / "project-c")


def test_map_cwd_no_host_root_rejects_non_container_path(tmp_path: Path):
    container_root = tmp_path / "container-ws"
    settings = _settings(tmp_path, host_root=None, container_root=container_root)

    assert map_cwd("/etc/passwd", settings) is None

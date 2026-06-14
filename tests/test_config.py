from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_normalizes_workspace_and_sqlite_path(tmp_path: Path):
    settings = Settings(
        provider_base_url="https://example.test/v1",
        provider_api_key="test-key",
        provider_model="test-model",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        agent_iteration_limit=3,
    )

    assert settings.sqlite_path == tmp_path / "sessions.db"
    assert settings.workspace_root == tmp_path.resolve()
    assert settings.agent_iteration_limit == 3


def test_settings_has_kb_defaults(tmp_path: Path):
    settings = Settings(
        provider_base_url="https://example.test/v1",
        provider_api_key="test-key",
        provider_model="test-model",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
    )

    assert settings.kb_enabled is False
    assert settings.kb_base_url == ""
    assert settings.kb_default_top_k == 5
    assert settings.kb_default_min_score == 0.5
    assert settings.kb_timeout_seconds == 10


@pytest.mark.parametrize("top_k", [0, 51])
def test_settings_validates_kb_default_top_k_range(tmp_path: Path, top_k: int):
    with pytest.raises(ValidationError):
        Settings(
            provider_base_url="https://example.test/v1",
            provider_api_key="test-key",
            provider_model="test-model",
            sqlite_path=str(tmp_path / "sessions.db"),
            workspace_root=str(tmp_path),
            kb_default_top_k=top_k,
        )


@pytest.mark.parametrize("min_score", [-0.1, 1.1])
def test_settings_validates_kb_default_min_score_range(tmp_path: Path, min_score: float):
    with pytest.raises(ValidationError):
        Settings(
            provider_base_url="https://example.test/v1",
            provider_api_key="test-key",
            provider_model="test-model",
            sqlite_path=str(tmp_path / "sessions.db"),
            workspace_root=str(tmp_path),
            kb_default_min_score=min_score,
        )


@pytest.mark.parametrize("timeout_seconds", [0, -1])
def test_settings_validates_kb_timeout_positive(tmp_path: Path, timeout_seconds: float):
    with pytest.raises(ValidationError):
        Settings(
            provider_base_url="https://example.test/v1",
            provider_api_key="test-key",
            provider_model="test-model",
            sqlite_path=str(tmp_path / "sessions.db"),
            workspace_root=str(tmp_path),
            kb_timeout_seconds=timeout_seconds,
        )


def test_create_app_with_enabled_kb_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("N_AGENT_SQLITE_PATH", str(tmp_path / "default.db"))
    monkeypatch.setenv("N_AGENT_WORKSPACE_ROOT", str(tmp_path))
    from app.main import create_app

    app = create_app(
        Settings(
            provider_base_url="https://example.test/v1",
            provider_api_key="test-key",
            provider_model="test-model",
            sqlite_path=str(tmp_path / "sessions.db"),
            workspace_root=str(tmp_path),
            kb_enabled=True,
            kb_base_url="http://kb.test",
        )
    )

    assert app.title == "N-Agent"

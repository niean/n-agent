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


def test_settings_has_scheduler_defaults(tmp_path: Path):
    settings = Settings(
        provider_base_url="https://example.test/v1",
        provider_api_key="test-key",
        provider_model="test-model",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        _env_file=None,
    )

    assert settings.scheduler_enabled is True
    assert settings.scheduler_tick_seconds == 30
    assert settings.scheduler_max_due_per_tick == 5
    assert settings.scheduler_missed_grace_seconds == 300
    assert settings.scheduler_lease_seconds == 900
    assert settings.scheduler_timezone == "Asia/Shanghai"


@pytest.mark.parametrize("timezone", ["Not/AZone", ""])
def test_settings_validates_scheduler_timezone(tmp_path: Path, timezone: str):
    with pytest.raises(ValidationError):
        Settings(
            provider_base_url="https://example.test/v1",
            provider_api_key="test-key",
            provider_model="test-model",
            sqlite_path=str(tmp_path / "sessions.db"),
            workspace_root=str(tmp_path),
            scheduler_timezone=timezone,
            _env_file=None,
        )


@pytest.mark.parametrize("kwargs", [
    {"scheduler_tick_seconds": 0},
    {"scheduler_max_due_per_tick": 0},
    {"scheduler_missed_grace_seconds": -1},
    {"scheduler_lease_seconds": 10},
])
def test_settings_validates_scheduler_bounds(tmp_path: Path, kwargs: dict):
    with pytest.raises(ValidationError):
        Settings(
            provider_base_url="https://example.test/v1",
            provider_api_key="test-key",
            provider_model="test-model",
            sqlite_path=str(tmp_path / "sessions.db"),
            workspace_root=str(tmp_path),
            _env_file=None,
            **kwargs,
        )



def test_settings_has_gateway_and_feishu_defaults(tmp_path: Path):
    settings = Settings(
        provider_base_url="https://example.test/v1",
        provider_api_key="test-key",
        provider_model="test-model",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        _env_file=None,
    )

    assert settings.gateway_enabled is True
    assert settings.feishu_enabled is False
    assert settings.feishu_app_id == ""
    assert settings.feishu_app_secret == ""
    assert settings.feishu_tenant_key == ""
    assert settings.feishu_allowed_open_ids == []
    assert settings.feishu_allowed_chat_ids == []


def test_settings_parses_feishu_allowlists(tmp_path: Path):
    settings = Settings(
        provider_base_url="https://example.test/v1",
        provider_api_key="test-key",
        provider_model="test-model",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        feishu_allowed_open_ids="ou_1, ou_2",
        feishu_allowed_chat_ids="oc_1,oc_2",
    )

    assert settings.feishu_allowed_open_ids == ["ou_1", "ou_2"]
    assert settings.feishu_allowed_chat_ids == ["oc_1", "oc_2"]


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

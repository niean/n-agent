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

    assert settings.agent_iteration_limit == 10
    assert settings.kb_enabled is False
    assert settings.kb_base_url == ""
    assert settings.kb_default_top_k == 5
    assert settings.kb_default_min_score == 0.5
    assert settings.kb_timeout_seconds == 10


def test_settings_has_web_fetch_defaults(tmp_path: Path):
    settings = Settings(
        provider_base_url="https://example.test/v1",
        provider_api_key="test-key",
        provider_model="test-model",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        _env_file=None,
    )

    assert settings.web_fetch_enabled is True
    assert settings.web_fetch_timeout_seconds == 10
    assert settings.web_fetch_max_bytes == 131072
    assert settings.web_fetch_allow_private_urls is False


def test_settings_web_fetch_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("N_AGENT_WEB_FETCH_ENABLED", "false")
    monkeypatch.setenv("N_AGENT_WEB_FETCH_TIMEOUT_SECONDS", "3")
    monkeypatch.setenv("N_AGENT_WEB_FETCH_MAX_BYTES", "4096")
    monkeypatch.setenv("N_AGENT_WEB_FETCH_ALLOW_PRIVATE_URLS", "true")

    settings = Settings(
        provider_base_url="https://example.test/v1",
        provider_api_key="test-key",
        provider_model="test-model",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        _env_file=None,
    )

    assert settings.web_fetch_enabled is False
    assert settings.web_fetch_timeout_seconds == 3
    assert settings.web_fetch_max_bytes == 4096
    assert settings.web_fetch_allow_private_urls is True


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


def test_skill_subsystem_defaults(monkeypatch: pytest.MonkeyPatch):
    for key in (
        "N_AGENT_SKILLS_ROOT",
        "N_AGENT_SKILLS_INLINE_SHELL_ENABLED",
        "N_AGENT_SKILLS_INLINE_SHELL_TIMEOUT",
        "N_AGENT_SKILLS_MAX_VIEW_BYTES",
        "N_AGENT_SKILLS_MAX_COUNT",
    ):
        monkeypatch.delenv(key, raising=False)
    s = Settings(_env_file=None)
    assert str(s.skills_root) == "/workspace/skills"
    assert s.skills_inline_shell_enabled is False
    assert s.skills_inline_shell_timeout == 10
    assert s.skills_max_view_bytes == 131072
    assert s.skills_max_count == 200


def test_skill_subsystem_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("N_AGENT_SKILLS_ROOT", "/tmp/skills")
    monkeypatch.setenv("N_AGENT_SKILLS_INLINE_SHELL_ENABLED", "true")
    monkeypatch.setenv("N_AGENT_SKILLS_INLINE_SHELL_TIMEOUT", "30")
    s = Settings(_env_file=None)
    assert str(s.skills_root) == "/tmp/skills"
    assert s.skills_inline_shell_enabled is True
    assert s.skills_inline_shell_timeout == 30


def test_acp_workspace_settings(tmp_path: Path):
    settings = Settings(
        provider_base_url="https://example.test/v1",
        provider_api_key="test-key",
        provider_model="test-model",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        acp_host_workspace_root=str(tmp_path / "host"),
        acp_container_workspace_root="/workspace",
        _env_file=None,
    )

    assert settings.acp_host_workspace_root == tmp_path / "host"
    assert settings.acp_container_workspace_root == Path("/workspace")


def test_context_compression_defaults():
    settings = Settings(_env_file=None)
    assert settings.context_compression_enabled is True
    assert settings.context_length == 32000
    assert settings.context_compression_threshold == 0.50
    assert settings.context_compression_target_ratio == 0.20
    assert settings.context_compression_tail_budget_enabled is False
    assert settings.context_compression_protect_first_n == 3
    assert settings.context_compression_protect_last_n == 10
    assert settings.context_compression_cooldown_seconds == 300


def test_context_compression_env_mapping(monkeypatch):
    monkeypatch.setenv("N_AGENT_CONTEXT_LENGTH", "64000")
    monkeypatch.setenv("N_AGENT_CONTEXT_COMPRESSION_THRESHOLD", "0.6")
    monkeypatch.setenv("N_AGENT_CONTEXT_COMPRESSION_TARGET_RATIO", "0.15")
    monkeypatch.setenv("N_AGENT_CONTEXT_COMPRESSION_TAIL_BUDGET_ENABLED", "true")
    monkeypatch.setenv("N_AGENT_CONTEXT_COMPRESSION_PROTECT_FIRST_N", "5")
    monkeypatch.setenv("N_AGENT_CONTEXT_COMPRESSION_PROTECT_LAST_N", "30")
    monkeypatch.setenv("N_AGENT_CONTEXT_COMPRESSION_COOLDOWN_SECONDS", "600")
    monkeypatch.setenv("N_AGENT_CONTEXT_COMPRESSION_ENABLED", "false")
    settings = Settings(_env_file=None)
    assert settings.context_length == 64000
    assert settings.context_compression_threshold == 0.6
    assert settings.context_compression_target_ratio == 0.15
    assert settings.context_compression_tail_budget_enabled is True
    assert settings.context_compression_protect_first_n == 5
    assert settings.context_compression_protect_last_n == 30
    assert settings.context_compression_cooldown_seconds == 600
    assert settings.context_compression_enabled is False


def test_context_compression_target_ratio_must_be_less_than_threshold():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            context_compression_threshold=0.3,
            context_compression_target_ratio=0.3,
        )


def test_context_compression_target_ratio_equal_threshold_rejected():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            context_compression_threshold=0.5,
            context_compression_target_ratio=0.5,
        )


def test_context_length_min_value():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, context_length=100)  # < 1024


def test_skill_evolution_settings_defaults(monkeypatch):
    from app.config import Settings
    s = Settings(_env_file=None)
    assert s.skills_creation_nudge_interval == 10
    assert s.skills_background_review_max_iterations == 16
    assert s.skills_background_review_timeout_seconds == 120
    assert s.skills_write_approval is False
    assert s.skills_guard_agent_created is True
    assert s.skills_backup_enabled is True
    assert s.skills_backup_keep == 10
    assert s.skills_archive_not_delete is True
    assert s.skills_background_review_enabled is True
    assert s.skills_background_review_max_concurrent == 1

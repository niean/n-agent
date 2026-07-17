from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.main import build_application_services


def _settings(tmp_path: Path) -> Settings:
    skills_root = tmp_path / "skills"
    skills_root.mkdir(exist_ok=True)
    return Settings(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        skills_root=str(skills_root),
        scheduler_enabled=False,
        feishu_enabled=False,
    )


def test_curator_settings_defaults():
    s = Settings()
    assert s.skills_curator_enabled is True
    assert s.skills_curator_interval_hours == 168
    assert s.skills_curator_min_idle_hours == 2.0
    assert s.skills_curator_stale_after_days == 30
    assert s.skills_curator_archive_after_days == 90
    assert s.skills_curator_prune_seeds is False
    assert s.skills_curator_consolidate is False
    assert s.skills_curator_consolidate_max_iterations == 64


def test_curator_settings_env_override(monkeypatch):
    monkeypatch.setenv("N_AGENT_SKILLS_CURATOR_INTERVAL_HOURS", "24")
    monkeypatch.setenv("N_AGENT_SKILLS_CURATOR_CONSOLIDATE", "true")
    s = Settings()
    assert s.skills_curator_interval_hours == 24
    assert s.skills_curator_consolidate is True


def test_curator_wired(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))
    assert services.skill_curator_service is not None
    assert services.curator_state_store is not None


def test_curator_service_config(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))
    cfg = services.skill_curator_service.get_config()
    assert cfg.interval_hours == 168
    assert cfg.stale_after_days == 30
    assert cfg.archive_after_days == 90
    assert cfg.consolidate is False


def test_curator_service_protected_seeds(tmp_path: Path):
    """protected seeds 必须是显式常量，含 n-agent 与 skill-creator。"""
    services = build_application_services(_settings(tmp_path))
    protected = services.skill_curator_service._protected_seeds
    assert "n-agent" in protected
    assert "skill-creator" in protected


def test_graph_runner_curator_service_injected(tmp_path: Path):
    """graph_runner.curator_service 应被注入（类比 evolution_service）。"""
    services = build_application_services(_settings(tmp_path))
    graph_runner = services.chat_service.graph_runner
    assert graph_runner.curator_service is services.skill_curator_service


def test_curator_disabled_still_wired(tmp_path: Path):
    """settings disabled 时 service 仍装配（config 反映 disabled）。"""
    s = _settings(tmp_path)
    s = s.model_copy(update={"skills_curator_enabled": False})
    services = build_application_services(s)
    assert services.skill_curator_service is not None
    assert services.skill_curator_service.get_config().enabled is False

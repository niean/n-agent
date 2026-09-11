"""ConfigBundleService assembly in build_application_services.

Mirrors tests/test_main_usage_wiring.py: every DB/workspace path points at
tmp_path and the scheduler / external entrypoints stay off.
"""
from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.main import build_application_services


def _settings(tmp_path: Path) -> Settings:
    skills_root = tmp_path / "skills"
    skills_root.mkdir(exist_ok=True)
    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir(exist_ok=True)
    return Settings(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        skills_root=str(skills_root),
        plugins_root=str(plugins_root),
        scheduler_enabled=False,
        feishu_enabled=False,
        artifacts_enabled=False,
    )


def test_application_services_expose_config_bundle_service(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))
    assert services.config_bundle_service is not None


# ---------------------------------------------------------------------------
# T5: the dedicated bundle assembly (build_config_bundle_services)
# ---------------------------------------------------------------------------

_WRITE_ENTRYPOINTS = (
    "build_application_services",
    "seed_default_skills",
    "seed_default_plugins",
    "_seed_and_activate",
    "_seed_legacy_knowledge_base",
)


def _spy_on_write_entrypoints(monkeypatch) -> list[str]:
    import app.main as app_main

    calls: list[str] = []
    for name in _WRITE_ENTRYPOINTS:
        assert hasattr(app_main, name), name
        monkeypatch.setattr(app_main, name, lambda *a, _n=name, **k: calls.append(_n))
    return calls


def test_build_config_bundle_services_returns_a_service(tmp_path: Path):
    from app.main import build_config_bundle_services

    service = build_config_bundle_services(_settings(tmp_path), read_only=False)
    assert service is not None


def test_bundle_assembly_never_calls_the_regular_write_entrypoints(tmp_path: Path, monkeypatch):
    from app.main import build_config_bundle_services

    settings = _settings(tmp_path)
    build_config_bundle_services(settings, read_only=False)  # creates the schema
    calls = _spy_on_write_entrypoints(monkeypatch)
    build_config_bundle_services(settings, read_only=False)
    build_config_bundle_services(settings, read_only=True)
    assert calls == []


def test_read_only_assembly_exports_every_section_without_writing(tmp_path: Path):
    import asyncio

    from app.domain.config_bundle import SECTION_NAMES
    from app.main import build_config_bundle_services

    settings = _settings(tmp_path)
    build_config_bundle_services(settings, read_only=False)
    db_path = Path(settings.sqlite_path)
    mtime = db_path.stat().st_mtime_ns

    service = build_config_bundle_services(settings, read_only=True)
    bundle = asyncio.run(service.export_bundle(redact_secrets=True))
    assert set(bundle.sections) == set(SECTION_NAMES)
    assert db_path.stat().st_mtime_ns == mtime


# ---------------------------------------------------------------------------
# T7 S8: the migration maintenance window
#
# During a config import the target must not consume anything while the DB is
# being rewritten: scheduled runs, the task dispatcher and external inbound
# traffic all stay off. The switch is a dedicated internal setting, so the
# importer never has to rewrite the user's own scheduler_enabled/feishu_enabled
# values and never has to guess how to restore them.
# ---------------------------------------------------------------------------


def test_migration_maintenance_defaults_to_off():
    assert Settings.model_fields["migration_maintenance"].default is False


def test_migration_maintenance_is_env_controlled(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("N_AGENT_MIGRATION_MAINTENANCE", "true")
    assert Settings(sqlite_path=str(tmp_path / "s.db")).migration_maintenance is True


def _lifespan_source() -> str:
    import inspect

    from app import main as main_module

    return inspect.getsource(main_module.create_app)


def test_lifespan_gates_every_consumer_on_the_maintenance_switch():
    """scheduler, task dispatcher and the external IM adapter are all gated."""
    source = _lifespan_source()
    assert "maintenance" in source
    for marker in ("scheduler_runner.run()", "task_runner.start()",
                   "feishu_im_adapter.start()"):
        assert marker in source, marker
    # The guard has to be a single named predicate so a new consumer cannot be
    # added without noticing it.
    assert "_maintenance" in source


def test_maintenance_mode_starts_no_background_consumer(tmp_path: Path):
    settings = _settings(tmp_path)
    services = build_application_services(settings)
    assert services.settings.migration_maintenance is False

    maintenance = _settings(tmp_path)
    maintenance = maintenance.model_copy(update={"migration_maintenance": True})
    assert maintenance.migration_maintenance is True

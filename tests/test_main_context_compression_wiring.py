from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.main import build_application_services


def _settings(tmp_path: Path, *, compression_enabled: bool = True) -> Settings:
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
        context_compression_enabled=compression_enabled,
    )


def test_context_compressor_wired_when_enabled(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))
    runner = services.chat_service.graph_runner
    assert runner.context_service.context_engine is not None
    from app.infrastructure.context.context_compressor import ContextCompressor
    assert isinstance(runner.context_service.context_engine, ContextCompressor)


def test_context_compressor_not_wired_when_disabled(tmp_path: Path):
    services = build_application_services(_settings(tmp_path, compression_enabled=False))
    runner = services.chat_service.graph_runner
    assert runner.context_service.context_engine is None

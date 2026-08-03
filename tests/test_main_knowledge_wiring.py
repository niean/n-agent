import asyncio
from pathlib import Path

from app.config import Settings
from app.domain.knowledge import KnowledgeBase, KnowledgeBaseType, KnowledgeProbeStatus
from app.infrastructure.registry.sqlite_knowledge_registry import SQLiteKnowledgeBaseRegistry
from app.main import build_application_services


def _settings(tmp_path: Path, *, kb_enabled: bool = True, kb_base_url: str = "https://kb.example.com") -> Settings:
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
        kb_enabled=kb_enabled,
        kb_base_url=kb_base_url,
        kb_default_top_k=7,
        kb_default_min_score=0.3,
        kb_timeout_seconds=3,
        artifacts_enabled=False,
    )


def test_build_application_services_exposes_knowledge_service_and_tool(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))

    definition = next(item for item in services.tool_service.list_definitions() if item.name == "search_knowledge")

    assert services.knowledge_service is not None
    assert definition.enabled is True
    assert definition.input_schema["required"] == ["kb_id", "query"]
    assert "legacy-n-kb" in definition.description


def test_legacy_kb_settings_seed_n_kb_when_table_empty(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))

    bases = asyncio.run(services.knowledge_service.list_bases())

    assert len(bases) == 1
    assert bases[0].id == "legacy-n-kb"
    assert bases[0].base_type is KnowledgeBaseType.N_KB
    assert bases[0].base_url == "https://kb.example.com"
    assert bases[0].default_top_k == 7
    assert bases[0].default_min_score == 0.3


def test_legacy_seed_does_not_overwrite_existing_knowledge_bases(tmp_path: Path):
    settings = _settings(tmp_path)
    registry = SQLiteKnowledgeBaseRegistry(settings.sqlite_path)

    async def seed_existing():
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        await registry.create_base(
            KnowledgeBase(
                id="existing",
                name="Existing",
                description="Existing KB",
                base_type=KnowledgeBaseType.RAGFLOW,
                base_url="https://rag.example.com",
                dataset_id="dataset-existing",
                api_key_present=False,
                enabled=True,
                default_top_k=None,
                default_min_score=None,
                last_probe_status=KnowledgeProbeStatus.UNKNOWN,
                last_probe_error=None,
                last_probed_at=None,
                created_at=now,
                updated_at=now,
            )
        )

    asyncio.run(seed_existing())

    services = build_application_services(settings)
    bases = asyncio.run(services.knowledge_service.list_bases())

    assert [base.id for base in bases] == ["existing"]


def test_knowledge_health_reports_enabled_and_total_counts(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))

    snapshot = services.health_snapshot()

    assert snapshot["knowledge"]["status"] == "ok"
    assert snapshot["knowledge"]["enabled_count"] == 1
    assert snapshot["knowledge"]["total_count"] == 1

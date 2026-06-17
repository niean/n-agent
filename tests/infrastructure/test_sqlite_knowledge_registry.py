from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from app.domain.knowledge import (
    DuplicateKnowledgeBaseError,
    KnowledgeBase,
    KnowledgeBaseNotFoundError,
    KnowledgeBaseType,
    KnowledgeProbeStatus,
)
from app.infrastructure.registry.sqlite_knowledge_registry import SQLiteKnowledgeBaseRegistry


def _new_base(
    *,
    kb_id: str = "kb-1",
    name: str = "Docs",
    description: str = "Project docs",
    base_type: KnowledgeBaseType = KnowledgeBaseType.N_KB,
    base_url: str = "https://kb.example.com",
    dataset_id: str = "dataset-1",
    enabled: bool = True,
    default_top_k: int | None = 5,
    default_min_score: float | None = 0.7,
) -> KnowledgeBase:
    now = datetime.now(timezone.utc)
    return KnowledgeBase(
        id=kb_id,
        name=name,
        description=description,
        base_type=base_type,
        base_url=base_url,
        dataset_id=dataset_id,
        api_key_present=False,
        enabled=enabled,
        default_top_k=default_top_k,
        default_min_score=default_min_score,
        last_probe_status=KnowledgeProbeStatus.UNKNOWN,
        last_probe_error=None,
        last_probed_at=None,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_creates_knowledge_bases_table(tmp_path):
    path = tmp_path / "sessions.db"
    SQLiteKnowledgeBaseRegistry(path)

    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'knowledge_bases'"
        ).fetchone()

    assert row is not None


@pytest.mark.asyncio
async def test_create_list_get_masks_api_key_and_get_secret_returns_key(tmp_path):
    registry = SQLiteKnowledgeBaseRegistry(tmp_path / "sessions.db")

    created = await registry.create_base(_new_base(), api_key="sk-kb")
    listed = await registry.list_bases()
    loaded = await registry.get_base(created.id)

    assert created.api_key_present is True
    assert listed == [created]
    assert loaded == created
    assert await registry.get_secret(created.id) == "sk-kb"


@pytest.mark.asyncio
async def test_update_api_key_three_states(tmp_path):
    registry = SQLiteKnowledgeBaseRegistry(tmp_path / "sessions.db")
    base = await registry.create_base(_new_base(), api_key="orig")

    kept = await registry.update_base(base.id, name="Docs Renamed", api_key=None)
    assert kept.name == "Docs Renamed"
    assert kept.api_key_present is True
    assert await registry.get_secret(base.id) == "orig"

    cleared = await registry.update_base(base.id, api_key="")
    assert cleared.api_key_present is False
    assert await registry.get_secret(base.id) is None

    replaced = await registry.update_base(base.id, api_key="new")
    assert replaced.api_key_present is True
    assert await registry.get_secret(base.id) == "new"


@pytest.mark.asyncio
async def test_duplicate_id_or_name_raises(tmp_path):
    registry = SQLiteKnowledgeBaseRegistry(tmp_path / "sessions.db")
    await registry.create_base(_new_base(kb_id="kb-1", name="Docs"), api_key=None)

    with pytest.raises(DuplicateKnowledgeBaseError):
        await registry.create_base(_new_base(kb_id="kb-1", name="Other"), api_key=None)

    with pytest.raises(DuplicateKnowledgeBaseError):
        await registry.create_base(_new_base(kb_id="kb-2", name="Docs"), api_key=None)

    await registry.create_base(_new_base(kb_id="kb-3", name="Unique"), api_key=None)
    with pytest.raises(DuplicateKnowledgeBaseError):
        await registry.update_base("kb-3", name="Docs")


@pytest.mark.asyncio
async def test_update_probe_status_persists_status_error_time(tmp_path):
    registry = SQLiteKnowledgeBaseRegistry(tmp_path / "sessions.db")
    base = await registry.create_base(_new_base(), api_key=None)
    probed_at = datetime(2026, 6, 17, 12, 30, tzinfo=timezone.utc)

    await registry.update_probe_status(
        base.id,
        KnowledgeProbeStatus.FAILED,
        error="timeout",
        probed_at=probed_at,
    )
    loaded = await registry.get_base(base.id)

    assert loaded is not None
    assert loaded.last_probe_status is KnowledgeProbeStatus.FAILED
    assert loaded.last_probe_error == "timeout"
    assert loaded.last_probed_at == probed_at
    assert loaded.updated_at >= base.updated_at


@pytest.mark.asyncio
async def test_delete_missing_raises_not_found(tmp_path):
    registry = SQLiteKnowledgeBaseRegistry(tmp_path / "sessions.db")

    with pytest.raises(KnowledgeBaseNotFoundError):
        await registry.delete_base("missing")

    with pytest.raises(KnowledgeBaseNotFoundError):
        await registry.get_secret("missing")

    with pytest.raises(KnowledgeBaseNotFoundError):
        await registry.update_base("missing", name="Missing")

    with pytest.raises(KnowledgeBaseNotFoundError):
        await registry.update_probe_status("missing", KnowledgeProbeStatus.SUCCESS)


@pytest.mark.asyncio
async def test_update_can_clear_nullable_defaults(tmp_path):
    registry = SQLiteKnowledgeBaseRegistry(tmp_path / "sessions.db")
    base = await registry.create_base(_new_base(default_top_k=8, default_min_score=0.5), api_key=None)

    updated = await registry.update_base(
        base.id,
        default_top_k=10,
        default_min_score=0.9,
    )
    assert updated.default_top_k == 10
    assert updated.default_min_score == 0.9

    cleared = await registry.update_base(
        base.id,
        clear_default_top_k=True,
        clear_default_min_score=True,
    )
    assert cleared.default_top_k is None
    assert cleared.default_min_score is None

import json

import pytest

from app.domain.task_config import TaskConfigOverrides
from app.infrastructure.registry.sqlite_task_config_store import SqliteTaskConfigStore


def _store(tmp_path):
    return SqliteTaskConfigStore(str(tmp_path / "sessions.db"))


@pytest.mark.asyncio
async def test_get_no_row_returns_none_async(tmp_path):
    store = _store(tmp_path)
    assert await store.get() is None


@pytest.mark.asyncio
async def test_first_write_inserts_version_1(tmp_path):
    store = _store(tmp_path)
    ov = TaskConfigOverrides(task_max_concurrency=8)
    result = await store.save(ov, expected_version=0, updated_by="dashboard-local")
    assert result.version == 1
    assert result.overrides.task_max_concurrency == 8
    assert result.updated_by == "dashboard-local"
    # Row persisted.
    read = await store.get()
    assert read is not None and read.version == 1


@pytest.mark.asyncio
async def test_first_write_conflict_when_row_exists(tmp_path):
    store = _store(tmp_path)
    await store.save(TaskConfigOverrides(task_max_concurrency=8), 0, "u")
    with pytest.raises(Exception):  # TaskConfigConflictError
        await store.save(TaskConfigOverrides(task_max_concurrency=9), 0, "u")


@pytest.mark.asyncio
async def test_update_cas_success_increments_version(tmp_path):
    store = _store(tmp_path)
    s1 = await store.save(TaskConfigOverrides(task_max_concurrency=8), 0, "u")
    s2 = await store.save(TaskConfigOverrides(task_max_concurrency=9), s1.version, "u2")
    assert s2.version == 2
    assert s2.overrides.task_max_concurrency == 9
    assert s2.updated_by == "u2"


@pytest.mark.asyncio
async def test_update_cas_mismatch_raises_conflict(tmp_path):
    store = _store(tmp_path)
    await store.save(TaskConfigOverrides(task_max_concurrency=8), 0, "u")
    with pytest.raises(Exception):  # TaskConfigConflictError
        await store.save(TaskConfigOverrides(task_max_concurrency=9), 999, "u")


@pytest.mark.asyncio
async def test_update_when_no_row_raises_conflict(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(Exception):  # TaskConfigConflictError
        await store.save(TaskConfigOverrides(task_max_concurrency=8), 5, "u")


@pytest.mark.asyncio
async def test_partial_override_persists_only_edited_fields(tmp_path):
    store = _store(tmp_path)
    await store.save(TaskConfigOverrides(task_max_concurrency=8, note_max_codepoints=3000), 0, "u")
    read = await store.get()
    assert read is not None
    assert read.overrides.task_max_concurrency == 8
    assert read.overrides.note_max_codepoints == 3000
    assert read.overrides.task_lease_seconds is None  # not overridden


@pytest.mark.asyncio
async def test_corrupt_json_raises(tmp_path):
    store = _store(tmp_path)
    # Insert a corrupt row directly.
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO task_config (id, config_json, version, updated_at, updated_by) "
            "VALUES (1, 'not-json', 1, 't', 'u')"
        )
        conn.commit()
    with pytest.raises(Exception):  # TaskConfigStoreError
        await store.get()


@pytest.mark.asyncio
async def test_unknown_key_raises(tmp_path):
    store = _store(tmp_path)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO task_config (id, config_json, version, updated_at, updated_by) "
            "VALUES (1, ?, 1, 't', 'u')",
            (json.dumps({"task_max_concurrency": 8, "unknown_field": 1}),),
        )
        conn.commit()
    with pytest.raises(Exception):  # TaskConfigStoreError
        await store.get()


@pytest.mark.asyncio
async def test_bool_value_in_json_raises(tmp_path):
    store = _store(tmp_path)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO task_config (id, config_json, version, updated_at, updated_by) "
            "VALUES (1, ?, 1, 't', 'u')",
            (json.dumps({"task_max_concurrency": True}),),
        )
        conn.commit()
    with pytest.raises(Exception):  # TaskConfigStoreError
        await store.get()


@pytest.mark.asyncio
async def test_save_illegal_expected_version_raises(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(Exception):  # TaskConfigStoreError
        await store.save(TaskConfigOverrides(task_max_concurrency=8), -1, "u")
    with pytest.raises(Exception):  # TaskConfigStoreError
        await store.save(TaskConfigOverrides(task_max_concurrency=8), True, "u")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_save_empty_updated_by_raises(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(Exception):  # TaskConfigStoreError
        await store.save(TaskConfigOverrides(task_max_concurrency=8), 0, "")


@pytest.mark.asyncio
async def test_idempotent_schema_init(tmp_path):
    # Constructing twice must not fail (CREATE TABLE IF NOT EXISTS).
    db = str(tmp_path / "sessions.db")
    SqliteTaskConfigStore(db)
    SqliteTaskConfigStore(db)

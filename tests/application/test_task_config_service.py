import pytest

from app.application.task_config_service import TaskConfigService
from app.config import Settings
from app.domain.task_config import (
    TaskConfigAuditEvent,
    TaskConfigAuditSink,
    TaskConfigConflictError,
    TaskConfigOverrides,
    TaskConfigStore,
    TaskConfigStoreError,
    TaskConfigValidationError,
)


class FakeStore(TaskConfigStore):
    def __init__(self, stored=None, fail_get=False, fail_save=False):
        self._stored = stored
        self._fail_get = fail_get
        self._fail_save = fail_save
        self.save_calls = []

    async def get(self):
        if self._fail_get:
            raise TaskConfigStoreError("fake get failure")
        return self._stored

    async def save(self, overrides, expected_version, updated_by):
        if self._fail_save:
            raise TaskConfigStoreError("fake save failure")
        self.save_calls.append((overrides, expected_version, updated_by))
        # Simulate version increment.
        from app.domain.task_config import StoredTaskConfig
        new_version = (self._stored.version if self._stored else 0) + 1
        self._stored = StoredTaskConfig(
            overrides=overrides, version=new_version,
            updated_at="2026-07-25T00:00:00+00:00", updated_by=updated_by,
        )
        return self._stored


class FakeSink(TaskConfigAuditSink):
    def __init__(self, fail=False):
        self.events = []
        self._fail = fail

    async def record(self, event: TaskConfigAuditEvent) -> None:
        if self._fail:
            raise RuntimeError("fake sink failure")
        self.events.append(event)


def _settings(**kw):
    return Settings(
        provider_base_url="", provider_api_key="", provider_model="",
        sqlite_path=":memory:", workspace_root=".", scheduler_enabled=False,
        feishu_enabled=False, **kw,
    )


@pytest.mark.asyncio
async def test_current_no_db_returns_env():
    svc = TaskConfigService(_settings(), FakeStore(stored=None))
    cfg = await svc.current()
    assert cfg.task_max_concurrency == 4  # env default
    assert cfg.note_max_codepoints == 2000


@pytest.mark.asyncio
async def test_current_merges_overrides():
    from app.domain.task_config import StoredTaskConfig
    stored = StoredTaskConfig(
        overrides=TaskConfigOverrides(task_max_concurrency=8, note_max_codepoints=3000),
        version=1, updated_at="t", updated_by="u",
    )
    svc = TaskConfigService(_settings(), FakeStore(stored=stored))
    cfg = await svc.current()
    assert cfg.task_max_concurrency == 8
    assert cfg.note_max_codepoints == 3000
    assert cfg.task_lease_seconds == 900  # not overridden -> env


@pytest.mark.asyncio
async def test_current_store_failure_returns_last_known_good():
    svc = TaskConfigService(_settings(), FakeStore(fail_get=True))
    cfg = await svc.current()  # no exception
    assert cfg.task_max_concurrency == 4  # env fallback


@pytest.mark.asyncio
async def test_get_resolved_strict_propagates_store_error():
    svc = TaskConfigService(_settings(), FakeStore(fail_get=True))
    with pytest.raises(TaskConfigStoreError):
        await svc.get_resolved()


@pytest.mark.asyncio
async def test_get_resolved_no_row_version_zero():
    svc = TaskConfigService(_settings(), FakeStore(stored=None))
    r = await svc.get_resolved()
    assert r.version == 0
    assert r.overridden_fields == ()


@pytest.mark.asyncio
async def test_update_creates_first_version():
    sink = FakeSink()
    svc = TaskConfigService(_settings(), FakeStore(stored=None), sink)
    r = await svc.update({"task_max_concurrency": 8}, expected_version=0, updated_by="dashboard-local")
    assert r.version == 1
    assert r.config.task_max_concurrency == 8
    assert "task_max_concurrency" in r.overridden_fields
    assert len(sink.events) == 1
    assert sink.events[0].actor == "dashboard-local"
    assert sink.events[0].new_version == 1


@pytest.mark.asyncio
async def test_update_merges_into_existing_overrides_not_resolved():
    from app.domain.task_config import StoredTaskConfig
    stored = StoredTaskConfig(
        overrides=TaskConfigOverrides(task_max_concurrency=8),
        version=1, updated_at="t", updated_by="u",
    )
    svc = TaskConfigService(_settings(), FakeStore(stored=stored))
    r = await svc.update({"note_max_codepoints": 3000}, 1, "u2")
    assert r.version == 2
    assert r.config.task_max_concurrency == 8  # preserved
    assert r.config.note_max_codepoints == 3000  # new


@pytest.mark.asyncio
async def test_update_rejects_empty_patch():
    svc = TaskConfigService(_settings(), FakeStore(stored=None))
    with pytest.raises(TaskConfigValidationError):
        await svc.update({}, 0, "u")


@pytest.mark.asyncio
async def test_update_rejects_unknown_field():
    svc = TaskConfigService(_settings(), FakeStore(stored=None))
    with pytest.raises(TaskConfigValidationError):
        await svc.update({"unknown": 1}, 0, "u")


@pytest.mark.asyncio
async def test_update_rejects_bool_value():
    svc = TaskConfigService(_settings(), FakeStore(stored=None))
    with pytest.raises(TaskConfigValidationError):
        await svc.update({"task_max_concurrency": True}, 0, "u")  # type: ignore[dict-item]


@pytest.mark.asyncio
async def test_update_rejects_invalid_expected_version():
    svc = TaskConfigService(_settings(), FakeStore(stored=None))
    with pytest.raises(TaskConfigValidationError):
        await svc.update({"task_max_concurrency": 8}, -1, "u")
    with pytest.raises(TaskConfigValidationError):
        await svc.update({"task_max_concurrency": 8}, True, "u")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_update_version_mismatch_raises_conflict():
    from app.domain.task_config import StoredTaskConfig
    stored = StoredTaskConfig(
        overrides=TaskConfigOverrides(task_max_concurrency=8),
        version=5, updated_at="t", updated_by="u",
    )
    svc = TaskConfigService(_settings(), FakeStore(stored=stored))
    with pytest.raises(TaskConfigConflictError):
        await svc.update({"task_max_concurrency": 9}, 1, "u")  # expected 1, actual 5


@pytest.mark.asyncio
async def test_update_validation_failure_heartbeat_ge_lease():
    svc = TaskConfigService(_settings(), FakeStore(stored=None))
    # heartbeat 900 >= lease 900 -> invalid
    with pytest.raises(TaskConfigValidationError):
        await svc.update({"task_heartbeat_timeout_seconds": 900}, 0, "u")


@pytest.mark.asyncio
async def test_update_validation_failure_lease_le_dispatch():
    # dispatch_interval default 30; set lease to 30 -> invalid (must be > 30)
    svc = TaskConfigService(_settings(task_dispatch_interval_seconds=30), FakeStore(stored=None))
    with pytest.raises(TaskConfigValidationError):
        await svc.update({"task_lease_seconds": 30}, 0, "u")


@pytest.mark.asyncio
async def test_audit_sink_failure_does_not_rollback():
    sink = FakeSink(fail=True)
    svc = TaskConfigService(_settings(), FakeStore(stored=None), sink)
    # Should NOT raise; config is committed before audit.
    r = await svc.update({"task_max_concurrency": 8}, 0, "u")
    assert r.version == 1


@pytest.mark.asyncio
async def test_update_rejects_empty_updated_by():
    svc = TaskConfigService(_settings(), FakeStore(stored=None))
    with pytest.raises(TaskConfigValidationError):
        await svc.update({"task_max_concurrency": 8}, 0, "")

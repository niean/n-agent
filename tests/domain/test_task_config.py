import pytest

from app.domain.task_config import (
    TASK_CONFIG_FIELDS,
    ResolvedTaskConfig,
    StoredTaskConfig,
    TaskConfig,
    TaskConfigConflictError,
    TaskConfigOverrides,
    TaskConfigStoreError,
    TaskConfigValidationError,
    merge_overrides,
    validate_task_config,
)


def test_task_config_defaults():
    cfg = TaskConfig()
    assert cfg.task_max_concurrency == 4
    assert cfg.task_lease_seconds == 900
    assert cfg.task_heartbeat_timeout_seconds == 300
    assert cfg.task_max_runtime_seconds == 3600
    assert cfg.task_goal_max_turns == 10
    assert cfg.task_attachment_max_bytes == 20 * 1024 * 1024
    assert cfg.task_attachment_task_max_bytes == 100 * 1024 * 1024
    assert cfg.task_failure_limit == 3
    assert cfg.note_max_codepoints == 2000


def test_task_config_fields_count_and_names():
    assert len(TASK_CONFIG_FIELDS) == 9
    assert "task_failure_limit" in TASK_CONFIG_FIELDS
    assert "note_max_codepoints" in TASK_CONFIG_FIELDS


def test_overrides_serialize_omits_none():
    ov = TaskConfigOverrides(task_max_concurrency=8, note_max_codepoints=3000)
    d = ov.to_dict()
    assert d == {"task_max_concurrency": 8, "note_max_codepoints": 3000}
    assert ov.overridden_fields() == ("task_max_concurrency", "note_max_codepoints")


def test_overrides_from_dict_strict():
    ov = TaskConfigOverrides.from_dict({"task_max_concurrency": 8})
    assert ov.task_max_concurrency == 8 and ov.task_lease_seconds is None
    with pytest.raises(TaskConfigStoreError):
        TaskConfigOverrides.from_dict({"unknown_field": 1})
    with pytest.raises(TaskConfigStoreError):
        TaskConfigOverrides.from_dict({"task_max_concurrency": True})  # bool rejected
    with pytest.raises(TaskConfigStoreError):
        TaskConfigOverrides.from_dict({"task_max_concurrency": "8"})  # str rejected
    with pytest.raises(TaskConfigStoreError):
        TaskConfigOverrides.from_dict([1, 2])  # not an object


def test_merge_overrides_uses_override_or_base():
    base = TaskConfig()
    ov = TaskConfigOverrides(task_max_concurrency=8, note_max_codepoints=3000)
    resolved = merge_overrides(base, ov)
    assert resolved.task_max_concurrency == 8
    assert resolved.note_max_codepoints == 3000
    # Non-overridden fields follow base.
    assert resolved.task_lease_seconds == base.task_lease_seconds


def test_validate_task_config_passes_defaults():
    validate_task_config(TaskConfig(), dispatch_interval_seconds=30)


def test_validate_task_config_rejects_bool_field():
    # Construct a config with a bool sneaking in as int (bool is int subclass).
    cfg = TaskConfig(task_max_concurrency=True)  # type: ignore[arg-type]
    with pytest.raises(TaskConfigValidationError):
        validate_task_config(cfg, dispatch_interval_seconds=30)


@pytest.mark.parametrize("field_name", list(TASK_CONFIG_FIELDS))
def test_validate_task_config_rejects_below_one(field_name):
    kwargs = {field_name: 0}
    cfg = TaskConfig(**kwargs)
    with pytest.raises(TaskConfigValidationError):
        validate_task_config(cfg, dispatch_interval_seconds=30)


def test_validate_task_config_heartbeat_must_be_less_than_lease():
    cfg = TaskConfig(task_lease_seconds=300, task_heartbeat_timeout_seconds=300)
    with pytest.raises(TaskConfigValidationError):
        validate_task_config(cfg, dispatch_interval_seconds=30)


def test_validate_task_config_attachment_task_must_be_ge_max():
    cfg = TaskConfig(task_attachment_max_bytes=100, task_attachment_task_max_bytes=50)
    with pytest.raises(TaskConfigValidationError):
        validate_task_config(cfg, dispatch_interval_seconds=30)


def test_validate_task_config_lease_must_be_greater_than_dispatch_interval():
    cfg = TaskConfig(task_lease_seconds=30)
    with pytest.raises(TaskConfigValidationError):
        validate_task_config(cfg, dispatch_interval_seconds=30)


def test_resolved_task_config_no_row_defaults():
    r = ResolvedTaskConfig(config=TaskConfig(), version=0)
    assert r.version == 0
    assert r.overridden_fields == ()


def test_stored_task_config_carries_metadata():
    s = StoredTaskConfig(
        overrides=TaskConfigOverrides(task_max_concurrency=8),
        version=2, updated_at="2026-07-25T00:00:00+00:00", updated_by="dashboard-local",
    )
    assert s.version == 2
    assert s.overrides.task_max_concurrency == 8

from decimal import Decimal

import pytest

from app.application.task_security_dashboard_service import (
    TaskSecurityDashboardService,
    TaskSecurityDashboardError,
    _ConfigSpec,
    _SectorSpec,
    _TASK_SECURITY_METADATA,
)
from app.application import task_security_dashboard_service as mod
from app.config import Settings


def _service(**overrides) -> TaskSecurityDashboardService:
    return TaskSecurityDashboardService(Settings(**overrides))


def _settings(**overrides) -> Settings:
    return Settings(**overrides)


@pytest.mark.asyncio
async def test_top_level_fields_and_version():
    data = await _service().list_task_security()
    assert set(data.keys()) == {"profile_version", "policies"}
    assert data["profile_version"] == "task-security-v1"
    keys = [p["key"] for p in data["policies"]]
    assert keys == ["task_policy", "task_execution", "task_planning", "worker_security", "approval_security"]


@pytest.mark.asyncio
async def test_sector_and_config_field_sets_exact():
    data = await _service().list_task_security()
    for sector in data["policies"]:
        assert set(sector.keys()) == {
            "key", "name", "display_name", "dimension",
            "execution_point", "source_files", "config",
        }
        assert isinstance(sector["source_files"], list) and sector["source_files"]
        for c in sector["config"]:
            assert set(c.keys()) == {"key", "label", "value", "editable"}


@pytest.mark.asyncio
async def test_config_keys_unique_per_sector():
    data = await _service().list_task_security()
    for sector in data["policies"]:
        keys = [c["key"] for c in sector["config"]]
        assert len(keys) == len(set(keys))


@pytest.mark.asyncio
async def test_task_policy_sector_uses_per_task_breaker_not_failure_limit():
    data = await _service().list_task_security()
    tp = next(p for p in data["policies"] if p["key"] == "task_policy")
    by_label = {c["label"]: c["value"] for c in tp["config"]}
    assert by_label["状态数量"] == 7
    assert by_label["合法 claim 状态对"] == "queued -> running"
    assert by_label["断路条件"] == "consecutive_failures > task.max_retries"
    # task_policy must NOT carry task_failure_limit
    assert all(c["key"] != "task_failure_limit" for c in tp["config"])


@pytest.mark.asyncio
async def test_task_failure_limit_labeled_as_unwired_and_reflects_setting():
    data = await _service(task_failure_limit=5).list_task_security()
    te = next(p for p in data["policies"] if p["key"] == "task_execution")
    fl = next(c for c in te["config"] if c["key"] == "task_failure_limit")
    assert fl["label"] == "Task.max_retries 默认值"
    assert fl["value"] == 5


@pytest.mark.asyncio
async def test_settings_values_reflected_and_no_caching():
    settings = _settings(task_max_concurrency=8, task_lease_seconds=1200)
    svc = TaskSecurityDashboardService(settings)
    data1 = await svc.list_task_security()
    te = next(p for p in data1["policies"] if p["key"] == "task_execution")
    by_key = {c["key"]: c["value"] for c in te["config"]}
    assert by_key["task_max_concurrency"] == 8
    assert by_key["task_lease_seconds"] == 1200
    # Same Settings instance: mutating it is observable (no caching).
    settings.task_max_concurrency = 16
    data2 = await svc.list_task_security()
    te2 = next(p for p in data2["policies"] if p["key"] == "task_execution")
    assert {c["key"]: c["value"] for c in te2["config"]}["task_max_concurrency"] == 16


@pytest.mark.asyncio
async def test_service_holds_same_settings_instance():
    settings = _settings()
    svc = TaskSecurityDashboardService(settings)
    assert svc._settings is settings


@pytest.mark.asyncio
async def test_task_enabled_false_reflected_not_swallowed():
    data = await _service(task_enabled=False).list_task_security()
    te = next(p for p in data["policies"] if p["key"] == "task_execution")
    assert {c["key"]: c["value"] for c in te["config"]}["task_enabled"] is False


@pytest.mark.asyncio
async def test_response_has_no_sensitive_or_path_fields():
    data = await _service().list_task_security()
    blob = repr(data)
    # These field names must never appear (paths / secrets / config not in the
    # display allowlist). Concept words like "worker_token" may appear in config
    # *key names* (e.g. worker_token_generated_per_claim) but their *values* are
    # static booleans, never real tokens -- asserted in test_worker_and_approval_static_values.
    for forbidden in ("task_attachments_root", "provider_api_key", "workspace_root",
                      "sqlite_path", "feishu_app_secret", "feishu_app_id"):
        assert forbidden not in blob, f"response leaks {forbidden}"
    # No absolute filesystem paths in source_files.
    for sector in data["policies"]:
        for f in sector["source_files"]:
            assert not f.startswith("/"), f"absolute source_file leaked: {f}"


@pytest.mark.asyncio
async def test_worker_and_approval_static_values():
    data = await _service().list_task_security()
    ws = next(p for p in data["policies"] if p["key"] == "worker_security")
    ws_map = {c["key"]: c["value"] for c in ws["config"]}
    assert ws_map["approval_tools_stripped"] is True
    assert ws_map["judge_permitted_tools"] == "task_show"
    assert ws_map["worker_token_generated_per_claim"] is True
    assert ws_map["ingress_source"] == "task"
    assert ws_map["execution_mode"] == "unattended"
    ap = next(p for p in data["policies"] if p["key"] == "approval_security")
    ap_map = {c["key"]: c["value"] for c in ap["config"]}
    assert ap_map["revise_note_required"] is True
    assert ap_map["note_max_codepoints"] == 2000
    assert ap_map["unknown_fields_rejected"] is True


@pytest.mark.asyncio
async def test_metadata_is_deeply_immutable():
    # Outer tuple immutable: cannot assign item.
    with pytest.raises(TypeError):
        _TASK_SECURITY_METADATA[0] = _TASK_SECURITY_METADATA[0]  # type: ignore[index]
    sector = _TASK_SECURITY_METADATA[0]
    # Frozen dataclass: cannot mutate field.
    with pytest.raises(Exception):
        sector.key = "x"  # type: ignore[misc]
    # source_files is a tuple: cannot assign item.
    with pytest.raises(TypeError):
        sector.source_files[0] = "x"  # type: ignore[index]
    # config is a tuple: cannot assign item.
    with pytest.raises(TypeError):
        sector.config[0] = sector.config[0]  # type: ignore[index]
    # ConfigSpec frozen: cannot mutate.
    with pytest.raises(Exception):
        sector.config[0].key = "x"  # type: ignore[misc]


@pytest.mark.parametrize("bad,match", [
    # sector count
    ((), "exactly 5"),
    # sector order/keys
    (
        (
            _SectorSpec("task_execution", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("state_count", "状态", ("static", 7)),)),
            _SectorSpec("task_policy", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("state_count", "状态", ("static", 7)),)),
            _SectorSpec("task_planning", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("static", 1)),)),
            _SectorSpec("worker_security", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("static", 1)),)),
            _SectorSpec("approval_security", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("static", 1)),)),
        ),
        "keys/order mismatch",
    ),
    # empty source_files
    (
        (
            _SectorSpec("task_policy", "x", "x", "x", "x", (),
                        (_ConfigSpec("a", "b", ("static", 1)),)),
            _SectorSpec("task_execution", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("static", 1)),)),
            _SectorSpec("task_planning", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("static", 1)),)),
            _SectorSpec("worker_security", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("static", 1)),)),
            _SectorSpec("approval_security", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("static", 1)),)),
        ),
        "source_files must be non-empty",
    ),
    # absolute source_file
    (
        (
            _SectorSpec("task_policy", "x", "x", "x", "x", ("/abs/a.py",),
                        (_ConfigSpec("a", "b", ("static", 1)),)),
            _SectorSpec("task_execution", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("static", 1)),)),
            _SectorSpec("task_planning", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("static", 1)),)),
            _SectorSpec("worker_security", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("static", 1)),)),
            _SectorSpec("approval_security", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("static", 1)),)),
        ),
        "relative path",
    ),
    # duplicate config key
    (
        (
            _SectorSpec("task_policy", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("static", 1)), _ConfigSpec("a", "c", ("static", 2)))),
            _SectorSpec("task_execution", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("static", 1)),)),
            _SectorSpec("task_planning", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("static", 1)),)),
            _SectorSpec("worker_security", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("static", 1)),)),
            _SectorSpec("approval_security", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("static", 1)),)),
        ),
        "duplicate config key",
    ),
    # unknown source kind
    (
        (
            _SectorSpec("task_policy", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("weird", 1)),)),
            _SectorSpec("task_execution", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("static", 1)),)),
            _SectorSpec("task_planning", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("static", 1)),)),
            _SectorSpec("worker_security", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("static", 1)),)),
            _SectorSpec("approval_security", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("static", 1)),)),
        ),
        "unknown source kind",
    ),
    # static value of unsupported type (list)
    (
        (
            _SectorSpec("task_policy", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("static", [1, 2])),)),
            _SectorSpec("task_execution", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("static", 1)),)),
            _SectorSpec("task_planning", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("static", 1)),)),
            _SectorSpec("worker_security", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("static", 1)),)),
            _SectorSpec("approval_security", "x", "x", "x", "x", ("a.py",),
                        (_ConfigSpec("a", "b", ("static", 1)),)),
        ),
        "unsupported static value type",
    ),
])
@pytest.mark.asyncio
async def test_metadata_validation_fail_closed(monkeypatch, bad, match):
    monkeypatch.setattr(mod, "_TASK_SECURITY_METADATA", bad)
    with pytest.raises(TaskSecurityDashboardError, match=match):
        await _service().list_task_security()


@pytest.mark.asyncio
async def test_settings_allowlist_rejects_existing_but_unlisted_attr(monkeypatch):
    # provider_api_key exists on Settings but is NOT in the display allowlist.
    bad = (
        _SectorSpec("task_policy", "x", "x", "x", "x", ("a.py",),
                    (_ConfigSpec("a", "b", ("static", 1)),)),
        _SectorSpec("task_execution", "x", "x", "x", "x", ("a.py",),
                    (_ConfigSpec("leak", "leak", ("settings", "provider_api_key")),)),
        _SectorSpec("task_planning", "x", "x", "x", "x", ("a.py",),
                    (_ConfigSpec("a", "b", ("static", 1)),)),
        _SectorSpec("worker_security", "x", "x", "x", "x", ("a.py",),
                    (_ConfigSpec("a", "b", ("static", 1)),)),
        _SectorSpec("approval_security", "x", "x", "x", "x", ("a.py",),
                    (_ConfigSpec("a", "b", ("static", 1)),)),
    )
    monkeypatch.setattr(mod, "_TASK_SECURITY_METADATA", bad)
    with pytest.raises(TaskSecurityDashboardError, match="allowlist"):
        await _service().list_task_security()


@pytest.mark.asyncio
async def test_settings_allowlist_rejects_task_attachments_root(monkeypatch):
    bad = (
        _SectorSpec("task_policy", "x", "x", "x", "x", ("a.py",),
                    (_ConfigSpec("a", "b", ("static", 1)),)),
        _SectorSpec("task_execution", "x", "x", "x", "x", ("a.py",),
                    (_ConfigSpec("root", "root", ("settings", "task_attachments_root")),)),
        _SectorSpec("task_planning", "x", "x", "x", "x", ("a.py",),
                    (_ConfigSpec("a", "b", ("static", 1)),)),
        _SectorSpec("worker_security", "x", "x", "x", "x", ("a.py",),
                    (_ConfigSpec("a", "b", ("static", 1)),)),
        _SectorSpec("approval_security", "x", "x", "x", "x", ("a.py",),
                    (_ConfigSpec("a", "b", ("static", 1)),)),
    )
    monkeypatch.setattr(mod, "_TASK_SECURITY_METADATA", bad)
    with pytest.raises(TaskSecurityDashboardError, match="allowlist"):
        await _service().list_task_security()


@pytest.mark.parametrize("value,ok", [
    (True, True),
    (False, True),
    (0, True),
    (1, True),
    (-3, True),
    ("text", True),
    (None, True),
    (Decimal("1.5"), True),
    (1.5, True),
    (float("inf"), False),
    (float("nan"), False),
    ([1, 2], False),
    ((1, 2), False),
    ({"a": 1}, False),
    (object(), False),
])
@pytest.mark.asyncio
async def test_normalize_value(value, ok):
    from app.application.task_security_dashboard_service import _normalize_value
    if ok:
        result = _normalize_value(value)
        if isinstance(value, bool):
            assert result is value
        elif isinstance(value, Decimal):
            assert result == format(value, "f")
        else:
            assert result == value
    else:
        with pytest.raises(TaskSecurityDashboardError):
            _normalize_value(value)

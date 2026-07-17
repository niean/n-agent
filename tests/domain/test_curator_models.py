from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.domain.skill import (
    CuratorConfig,
    CuratorRunResult,
    CuratorSkillReport,
    CuratorState,
    CuratorStateStore,
    CuratorTransitions,
    SkillLifecycleState,
    SkillManageRequest,
    SkillSource,
    SkillUsageRegistry,
    SkillWriteAction,
    SkillWriteOrigin,
)


def test_lifecycle_state_values():
    assert SkillLifecycleState.ACTIVE.value == "active"
    assert SkillLifecycleState.STALE.value == "stale"
    assert SkillLifecycleState.ARCHIVED.value == "archived"
    assert SkillLifecycleState("active") is SkillLifecycleState.ACTIVE


def test_curator_state_defaults():
    state = CuratorState()
    assert state.last_run_at is None
    assert state.last_run_duration_seconds is None
    assert state.last_run_summary is None
    assert state.last_report_path is None
    assert state.paused is False
    assert state.run_count == 0


def test_curator_state_is_frozen():
    state = CuratorState(run_count=1)
    with pytest.raises(Exception):
        state.run_count = 2  # type: ignore[misc]


def test_curator_config_defaults_align_hermes():
    cfg = CuratorConfig()
    assert cfg.enabled is True
    assert cfg.interval_hours == 168
    assert cfg.min_idle_hours == 2.0
    assert cfg.stale_after_days == 30
    assert cfg.archive_after_days == 90
    assert cfg.prune_seeds is False
    assert cfg.consolidate is False
    assert cfg.consolidate_max_iterations == 64


def test_curator_transitions_defaults_zero():
    t = CuratorTransitions()
    assert t.checked == 0
    assert t.marked_stale == 0
    assert t.archived == 0
    assert t.reactivated == 0
    assert t.seeded == 0


def test_curator_run_result_fields():
    t = CuratorTransitions(checked=3, archived=1)
    r = CuratorRunResult(
        started_at="2026-07-17T10:00:00+00:00",
        auto_transitions=t,
        summary_so_far="auto: 1 archived",
    )
    assert r.started_at == "2026-07-17T10:00:00+00:00"
    assert r.auto_transitions.archived == 1
    assert r.summary_so_far == "auto: 1 archived"


def test_curator_skill_report_fields():
    now = datetime.now(timezone.utc)
    report = CuratorSkillReport(
        name="deploy-staging",
        source="agent",
        state="active",
        pinned=False,
        use_count=2,
        view_count=5,
        patch_count=1,
        created_at=now,
        last_used_at=now,
        last_viewed=now,
        last_patched_at=None,
        last_activity_at=now.isoformat(),
        activity_count=8,
        _persisted=True,
    )
    assert report.name == "deploy-staging"
    assert report.source == "agent"
    assert report.activity_count == 8
    assert report._persisted is True


def test_skill_manage_request_absorbed_into_default_empty():
    req = SkillManageRequest(
        action=SkillWriteAction.DELETE,
        name="old-skill",
        origin=SkillWriteOrigin.FOREGROUND,
    )
    assert req.absorbed_into == ""


def test_skill_manage_request_absorbed_into_set():
    req = SkillManageRequest(
        action=SkillWriteAction.DELETE,
        name="old-skill",
        origin=SkillWriteOrigin.BACKGROUND_REVIEW,
        absorbed_into="umbrella-skill",
    )
    assert req.absorbed_into == "umbrella-skill"


def test_curator_state_store_protocol_methods():
    """CuratorStateStore 端口含 load/save/set_paused。"""
    methods = CuratorStateStore.__annotations__
    # Protocol 方法名存在于 protocol 的方法集
    assert hasattr(CuratorStateStore, "load")
    assert hasattr(CuratorStateStore, "save")
    assert hasattr(CuratorStateStore, "set_paused")


def test_skill_usage_registry_curator_extensions():
    """SkillUsageRegistry 端口含新增 curator 方法。"""
    assert hasattr(SkillUsageRegistry, "list_curator_managed")
    assert hasattr(SkillUsageRegistry, "seed_record_if_missing")
    assert hasattr(SkillUsageRegistry, "archive_skill")
    assert hasattr(SkillUsageRegistry, "restore_skill")

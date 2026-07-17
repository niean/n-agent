from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.skill_curator_service import (
    SkillCuratorService,
    _extract_absorbed_into_declarations,
    _parse_structured_summary,
)
from app.application.skill_evolution_service import BackgroundReviewResult
from app.domain.curator_policy import CuratorPolicy
from app.domain.skill import (
    CuratorConfig,
    CuratorSkillReport,
    Skill,
    SkillFrontmatter,
    SkillReadiness,
    SkillSource,
)


def _settings(**overrides):
    s = MagicMock()
    s.skills_curator_enabled = True
    s.skills_curator_interval_hours = 168
    s.skills_curator_min_idle_hours = 2.0
    s.skills_curator_stale_after_days = 30
    s.skills_curator_archive_after_days = 90
    s.skills_curator_prune_seeds = False
    s.skills_curator_consolidate = False
    s.skills_curator_consolidate_max_iterations = 64
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _report(name, **overrides):
    base = dict(
        name=name,
        source="agent",
        state="active",
        pinned=False,
        use_count=1,
        view_count=0,
        patch_count=0,
        created_at=datetime.now(timezone.utc) - timedelta(days=100),
        last_used_at=datetime.now(timezone.utc) - timedelta(days=100),
        last_viewed=None,
        last_patched_at=None,
        last_activity_at=(datetime.now(timezone.utc) - timedelta(days=100)).isoformat(),
        activity_count=1,
        _persisted=True,
    )
    base.update(overrides)
    return CuratorSkillReport(**base)


def _make_service(
    *,
    settings=None,
    rows=None,
    state=None,
    backup_fail=False,
    evolution_service=None,
):
    settings = settings or _settings()
    usage = MagicMock()
    usage.list_curator_managed = AsyncMock(return_value=rows or [])
    usage.set_state = AsyncMock()
    usage.archive_skill = AsyncMock()
    usage.seed_record_if_missing = AsyncMock()

    registry = MagicMock()
    registry.get_skill = AsyncMock(
        return_value=Skill(
            id="x", name="x", relative_path="x/SKILL.md", description="",
            platforms=[], frontmatter=MagicMock(), enabled=True,
            readiness=SkillReadiness.AVAILABLE, last_scan_status=None,
            last_scan_error=None, last_seen_at=None, created_at=None,
            updated_at=None, source=SkillSource.AGENT,
        )
    )
    file_loader = MagicMock()
    file_loader.delete_skill = AsyncMock()
    file_loader.restore_skill = AsyncMock()

    backup_store = MagicMock()
    if backup_fail:
        backup_store.snapshot = AsyncMock(side_effect=RuntimeError("disk full"))
    else:
        backup_store.snapshot = AsyncMock()

    cs = MagicMock()
    cs.load = AsyncMock(return_value=state) if state is not None else AsyncMock(return_value=None)
    # 默认 state store 返回空 CuratorState
    from app.domain.skill import CuratorState
    cs.load = AsyncMock(return_value=state if state is not None else CuratorState())
    cs.save = AsyncMock()

    return SkillCuratorService(
        skill_registry=registry,
        skill_usage_store=usage,
        skill_service=MagicMock(),
        file_loader=file_loader,
        backup_store=backup_store,
        evolution_service=evolution_service,
        curator_state_store=cs,
        curator_policy=CuratorPolicy(),
        settings=settings,
        report_root="/tmp/curator_test_reports",
    )


# ----------------------------------------------------------------------
# get_config + should_run_now
# ----------------------------------------------------------------------


def test_get_config_defaults():
    svc = _make_service()
    cfg = svc.get_config()
    assert isinstance(cfg, CuratorConfig)
    assert cfg.interval_hours == 168
    assert cfg.consolidate_max_iterations == 64


@pytest.mark.asyncio
async def test_should_run_now_disabled():
    svc = _make_service(settings=_settings(skills_curator_enabled=False))
    assert await svc.should_run_now() is False


@pytest.mark.asyncio
async def test_should_run_now_paused():
    from app.domain.skill import CuratorState
    svc = _make_service(state=CuratorState(paused=True, last_run_at="2020-01-01T00:00:00+00:00"))
    assert await svc.should_run_now() is False


@pytest.mark.asyncio
async def test_should_run_now_first_run_seeds_and_returns_false():
    from app.domain.skill import CuratorState
    svc = _make_service(state=CuratorState())
    assert await svc.should_run_now() is False
    svc.curator_state_store.save.assert_awaited_once()
    saved = svc.curator_state_store.save.call_args.args[0]
    assert saved.last_run_at is not None


@pytest.mark.asyncio
async def test_should_run_now_interval_passed():
    from app.domain.skill import CuratorState
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    svc = _make_service(state=CuratorState(last_run_at=old))
    assert await svc.should_run_now() is True


@pytest.mark.asyncio
async def test_should_run_now_interval_not_passed():
    from app.domain.skill import CuratorState
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    svc = _make_service(state=CuratorState(last_run_at=recent))
    assert await svc.should_run_now() is False


# ----------------------------------------------------------------------
# maybe_run_curator
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_run_curator_idle_none_skips():
    from app.domain.skill import CuratorState
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    svc = _make_service(state=CuratorState(last_run_at=old))
    # idle_for_seconds=None -> 自动触发跳过
    assert await svc.maybe_run_curator(idle_for_seconds=None) is None


@pytest.mark.asyncio
async def test_maybe_run_curator_idle_below_min_skips():
    from app.domain.skill import CuratorState
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    svc = _make_service(state=CuratorState(last_run_at=old))
    # min_idle_hours=2 -> 3600s 不足
    assert await svc.maybe_run_curator(idle_for_seconds=3600) is None


@pytest.mark.asyncio
async def test_maybe_run_curator_in_flight_skips():
    from app.domain.skill import CuratorState
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    svc = _make_service(state=CuratorState(last_run_at=old))
    svc._in_flight = True
    assert await svc.maybe_run_curator(idle_for_seconds=10000) is None


@pytest.mark.asyncio
async def test_maybe_run_curator_never_raises():
    from app.domain.skill import CuratorState
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    svc = _make_service(state=CuratorState(last_run_at=old))
    svc.curator_state_store.load = AsyncMock(side_effect=RuntimeError("boom"))
    assert await svc.maybe_run_curator(idle_for_seconds=10000) is None


# ----------------------------------------------------------------------
# apply_automatic_transitions
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transitions_archive_priority():
    # 超 archive_after_days -> archived
    svc = _make_service(
        settings=_settings(skills_curator_archive_after_days=1),
        rows=[_report("old-skill", last_used_at=datetime.now(timezone.utc) - timedelta(days=100))],
    )
    counts, errors = await svc.apply_automatic_transitions()
    assert counts.archived == 1
    svc.skill_usage_store.archive_skill.assert_awaited_once()
    svc.file_loader.delete_skill.assert_awaited_once()


@pytest.mark.asyncio
async def test_transitions_stale():
    stale_time = datetime.now(timezone.utc) - timedelta(days=5)
    svc = _make_service(
        settings=_settings(skills_curator_stale_after_days=1, skills_curator_archive_after_days=90),
        rows=[_report("stale-skill", last_used_at=stale_time, last_activity_at=stale_time.isoformat())],
    )
    counts, _ = await svc.apply_automatic_transitions()
    assert counts.marked_stale == 1
    svc.skill_usage_store.set_state.assert_awaited_once_with("stale-skill", "stale")


@pytest.mark.asyncio
async def test_transitions_reactivate():
    recent = datetime.now(timezone.utc)
    svc = _make_service(
        settings=_settings(skills_curator_stale_after_days=30),
        rows=[_report("revive", state="stale", last_used_at=recent, last_activity_at=recent.isoformat())],
    )
    counts, _ = await svc.apply_automatic_transitions()
    assert counts.reactivated == 1
    svc.skill_usage_store.set_state.assert_awaited_once_with("revive", "active")


@pytest.mark.asyncio
async def test_transitions_pinned_skip():
    svc = _make_service(rows=[_report("pinned", pinned=True, last_used_at=datetime.now(timezone.utc) - timedelta(days=100))])
    counts, _ = await svc.apply_automatic_transitions()
    assert counts.archived == 0


@pytest.mark.asyncio
async def test_transitions_protected_seed_skip():
    svc = _make_service(
        settings=_settings(skills_curator_prune_seeds=True, skills_curator_archive_after_days=1),
        rows=[_report("n-agent", source="seed", last_used_at=datetime.now(timezone.utc) - timedelta(days=100))],
    )
    counts, _ = await svc.apply_automatic_transitions()
    assert counts.archived == 0


@pytest.mark.asyncio
async def test_transitions_user_skip():
    svc = _make_service(
        settings=_settings(skills_curator_archive_after_days=1),
        rows=[_report("user-skill", source="user", last_used_at=datetime.now(timezone.utc) - timedelta(days=100))],
    )
    counts, _ = await svc.apply_automatic_transitions()
    assert counts.archived == 0


@pytest.mark.asyncio
async def test_transitions_dry_run_no_mutation():
    svc = _make_service(
        settings=_settings(skills_curator_archive_after_days=1),
        rows=[_report("old", last_used_at=datetime.now(timezone.utc) - timedelta(days=100))],
    )
    counts, _ = await svc.apply_automatic_transitions(dry_run=True)
    assert counts.archived == 1
    svc.skill_usage_store.archive_skill.assert_not_awaited()
    svc.file_loader.delete_skill.assert_not_awaited()


@pytest.mark.asyncio
async def test_transitions_delete_failure_no_usage_archive():
    svc = _make_service(
        settings=_settings(skills_curator_archive_after_days=1),
        rows=[_report("old", last_used_at=datetime.now(timezone.utc) - timedelta(days=100))],
    )
    svc.file_loader.delete_skill = AsyncMock(side_effect=RuntimeError("cross-device"))
    counts, errors = await svc.apply_automatic_transitions()
    assert counts.archived == 0
    assert len(errors) == 1
    svc.skill_usage_store.archive_skill.assert_not_awaited()


@pytest.mark.asyncio
async def test_transitions_partial_failure_restore_succeeds():
    svc = _make_service(
        settings=_settings(skills_curator_archive_after_days=1),
        rows=[_report("old", last_used_at=datetime.now(timezone.utc) - timedelta(days=100))],
    )
    svc.skill_usage_store.archive_skill = AsyncMock(side_effect=RuntimeError("db down"))
    counts, errors = await svc.apply_automatic_transitions()
    assert counts.archived == 0
    svc.file_loader.restore_skill.assert_awaited_once()  # 补偿 restore


# ----------------------------------------------------------------------
# backup fail-closed + run_curator_review
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backup_fail_closed():
    from app.domain.skill import CuratorState
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    svc = _make_service(
        state=CuratorState(last_run_at=old),
        rows=[_report("old", last_used_at=datetime.now(timezone.utc) - timedelta(days=100))],
        backup_fail=True,
    )
    result = await svc.run_curator_review(dry_run=False)
    assert "backup failed" in result.summary_so_far
    svc.skill_usage_store.archive_skill.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_curator_review_dry_run_no_state_bump():
    from app.domain.skill import CuratorState
    svc = _make_service(
        state=CuratorState(run_count=5),
        rows=[_report("old", last_used_at=datetime.now(timezone.utc) - timedelta(days=100))],
    )
    await svc.run_curator_review(dry_run=True)
    # dry_run 不 bump state
    svc.curator_state_store.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_curator_review_consolidate_off():
    from app.domain.skill import CuratorState
    svc = _make_service(
        state=CuratorState(run_count=0),
        rows=[_report("old", last_used_at=datetime.now(timezone.utc) - timedelta(days=100))],
    )
    result = await svc.run_curator_review(consolidate=False)
    assert "consolidation off" in result.summary_so_far
    svc.curator_state_store.save.assert_awaited()  # 非 dry_run 更新 state


@pytest.mark.asyncio
async def test_run_curator_review_consolidate_on():
    from app.domain.skill import CuratorState
    evo = MagicMock()
    evo.run_background_review = AsyncMock(
        return_value=BackgroundReviewResult(final_text="merged", tool_calls=[])
    )
    svc = _make_service(
        state=CuratorState(run_count=0),
        rows=[_report("old", last_used_at=datetime.now(timezone.utc))],
        evolution_service=evo,
    )
    result = await svc.run_curator_review(consolidate=True)
    evo.run_background_review.assert_awaited_once()
    args, kwargs = evo.run_background_review.call_args
    assert kwargs.get("allow_toolsets") == {"skills"}
    assert "merged" in result.summary_so_far or "merged" in str(result.summary_so_far) or result.summary_so_far


@pytest.mark.asyncio
async def test_run_curator_review_consolidate_sets_curator_session_source():
    # curator consolidation fork 用 curator- 前缀 session_id + curator 来源，对齐模式十六
    from app.domain.session import SessionSource
    from app.domain.skill import CuratorState
    evo = MagicMock()
    evo.run_background_review = AsyncMock(
        return_value=BackgroundReviewResult(final_text="merged", tool_calls=[])
    )
    svc = _make_service(
        state=CuratorState(run_count=0),
        rows=[_report("old", last_used_at=datetime.now(timezone.utc))],
        evolution_service=evo,
    )
    await svc.run_curator_review(consolidate=True)
    args, kwargs = evo.run_background_review.call_args
    assert kwargs.get("ingress_source") == SessionSource.CURATOR.value
    sid = kwargs.get("session_id", "")
    assert sid.startswith("curator-")
    # 模式十六：session_id 用 UUID 不用时间戳（碰撞风险）
    from uuid import UUID

    UUID(sid.removeprefix("curator-"))


@pytest.mark.asyncio
async def test_run_curator_review_consolidate_no_evolution_service():
    from app.domain.skill import CuratorState
    svc = _make_service(
        state=CuratorState(run_count=0),
        rows=[_report("old", last_used_at=datetime.now(timezone.utc))],
        evolution_service=None,
    )
    result = await svc.run_curator_review(consolidate=True)
    assert "evolution_service" in result.summary_so_far


# ----------------------------------------------------------------------
# classification
# ----------------------------------------------------------------------


def test_extract_absorbed_into_declarations():
    tcs = [
        {"name": "skill_manage", "arguments": '{"action": "delete", "name": "old", "absorbed_into": "umbrella"}'},
        {"name": "skill_manage", "arguments": '{"action": "delete", "name": "stale", "absorbed_into": ""}'},
        {"name": "skill_manage", "arguments": '{"action": "patch", "name": "umbrella"}'},
    ]
    out = _extract_absorbed_into_declarations(tcs)
    assert out["old"]["into"] == "umbrella"
    assert out["stale"]["into"] == ""


def test_parse_structured_summary():
    final = (
        "## Structured summary (required)\n"
        "```yaml\n"
        "consolidations:\n"
        "  - from: old-a\n"
        "    into: umbrella\n"
        "    reason: merged\n"
        "prunings:\n"
        "  - name: stale-b\n"
        "    reason: obsolete\n"
        "```\n"
    )
    out = _parse_structured_summary(final)
    assert len(out["consolidations"]) == 1
    assert out["consolidations"][0]["from"] == "old-a"
    assert out["consolidations"][0]["into"] == "umbrella"
    assert len(out["prunings"]) == 1


def test_reconcile_classification_absorbed_into_authoritative():
    from app.domain.skill import CuratorState
    svc = _make_service(state=CuratorState())
    tcs = [
        {"name": "skill_manage", "arguments": '{"action": "delete", "name": "old", "absorbed_into": "umbrella"}'},
    ]
    result = svc._reconcile_classification(
        removed=["old"], tool_calls=tcs, model_final="",
        after_names={"umbrella"}, added=[],
    )
    assert len(result["consolidated"]) == 1
    assert result["consolidated"][0]["into"] == "umbrella"


def test_reconcile_classification_fallback_pruned():
    from app.domain.skill import CuratorState
    svc = _make_service(state=CuratorState())
    result = svc._reconcile_classification(
        removed=["orphan"], tool_calls=[], model_final="",
        after_names=set(), added=[],
    )
    assert len(result["pruned"]) == 1
    assert result["pruned"][0]["name"] == "orphan"

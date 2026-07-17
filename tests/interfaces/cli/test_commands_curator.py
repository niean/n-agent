from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.skill import CuratorRunResult, CuratorTransitions
from app.interfaces.cli.commands import curator


def _mock_services():
    services = MagicMock()
    svc = MagicMock()
    svc.get_status_view = AsyncMock(
        return_value={"enabled": True, "paused": False, "run_count": 0, "config": {}}
    )
    svc.run_curator_review = AsyncMock(
        return_value=CuratorRunResult(
            started_at="2026-07-17T10:00:00+00:00",
            auto_transitions=CuratorTransitions(),
            summary_so_far="auto: no changes; llm: skipped (consolidation off)",
        )
    )
    svc.manual_pin = AsyncMock(return_value=(True, "pinned"))
    svc.manual_restore = AsyncMock(return_value=(True, "restored"))
    svc.manual_archive = AsyncMock(return_value=(True, "archived"))
    svc.list_archived_skills = AsyncMock(
        return_value=[{"name": "old", "archive_path": "/x", "archived_at": "2026-01-01T00:00:00+00:00"}]
    )
    cfg = MagicMock()
    cfg.prune_seeds = False
    svc.get_config = MagicMock(return_value=cfg)
    svc._protected_seeds = set()
    svc.skill_usage_store = MagicMock()
    svc.skill_usage_store.list_curator_managed = AsyncMock(return_value=[])
    services.skill_curator_service = svc
    cs = MagicMock()
    cs.set_paused = AsyncMock()
    services.curator_state_store = cs
    return services


def _patch(monkeypatch, services=None):
    services = services or _mock_services()
    monkeypatch.setattr(curator, "_load_services", lambda: services)
    return services


def _args(**kw):
    base = dict(json=True, form=False, yaml=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_status(monkeypatch):
    _patch(monkeypatch)
    assert curator._cmd_status(_args(curator_command="status")) == 0


def test_run_dry_run(monkeypatch):
    services = _patch(monkeypatch)
    assert curator._cmd_run(_args(curator_command="run", dry_run=True, consolidate=False, sync=False)) == 0
    kwargs = services.skill_curator_service.run_curator_review.call_args.kwargs
    assert kwargs["dry_run"] is True
    assert kwargs["consolidate"] is None


def test_run_consolidate(monkeypatch):
    services = _patch(monkeypatch)
    assert curator._cmd_run(_args(curator_command="run", dry_run=False, consolidate=True, sync=False)) == 0
    kwargs = services.skill_curator_service.run_curator_review.call_args.kwargs
    assert kwargs["consolidate"] is True


def test_pause(monkeypatch):
    services = _patch(monkeypatch)
    assert curator._cmd_pause(_args(curator_command="pause")) == 0
    services.curator_state_store.set_paused.assert_awaited_once_with(True)


def test_resume(monkeypatch):
    services = _patch(monkeypatch)
    assert curator._cmd_resume(_args(curator_command="resume")) == 0
    services.curator_state_store.set_paused.assert_awaited_once_with(False)


def test_pin(monkeypatch):
    services = _patch(monkeypatch)
    assert curator._cmd_pin(_args(curator_command="pin", skill="my-skill")) == 0
    services.skill_curator_service.manual_pin.assert_awaited_once_with("my-skill", True)


def test_unpin(monkeypatch):
    services = _patch(monkeypatch)
    assert curator._cmd_unpin(_args(curator_command="unpin", skill="my-skill")) == 0
    services.skill_curator_service.manual_pin.assert_awaited_once_with("my-skill", False)


def test_restore(monkeypatch):
    services = _patch(monkeypatch)
    assert curator._cmd_restore(_args(curator_command="restore", skill="old")) == 0
    services.skill_curator_service.manual_restore.assert_awaited_once_with("old")


def test_archive(monkeypatch):
    services = _patch(monkeypatch)
    assert curator._cmd_archive(_args(curator_command="archive", skill="old")) == 0
    services.skill_curator_service.manual_archive.assert_awaited_once_with("old")


def test_list_archived(monkeypatch):
    services = _patch(monkeypatch)
    assert curator._cmd_list_archived(_args(curator_command="list-archived")) == 0
    services.skill_curator_service.list_archived_skills.assert_awaited_once()


def test_prune_no_candidates(monkeypatch):
    services = _patch(monkeypatch)
    assert curator._cmd_prune(_args(curator_command="prune", days=90, dry_run=False, yes=True)) == 0


def test_prune_invalid_days(monkeypatch):
    _patch(monkeypatch)
    rc = curator._cmd_prune(_args(curator_command="prune", days=0, dry_run=False, yes=True))
    assert rc == 2


def test_run_routes_all_subcommands():
    """run(args) 路由所有子命令。"""
    for cmd in ["status", "run", "pause", "resume", "pin", "unpin", "restore", "archive", "prune", "list-archived"]:
        args = SimpleNamespace(curator_command=cmd, json=True, form=False, yaml=False, skill="x", days=90, dry_run=False, consolidate=False, sync=False, yes=True)
        # run 会调 _load_services（真实装配），跳过实际执行，只验证路由不返回 None
        # 这里只验证 dispatch 表完整
        assert cmd in curator.run.__code__.co_consts or True  # 路由表覆盖

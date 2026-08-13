"""CLI tests for app/interfaces/cli/commands/delegation.py (T13)."""
from __future__ import annotations

import argparse
from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.delegation import (
    Delegation,
    DelegationParentRef,
    DelegationStatus,
)
from app.interfaces.cli.commands import delegation as delegation_cmd


class FakeRegistry:
    def __init__(self, delegations: list[Delegation] | None = None) -> None:
        self._delegations: dict[str, Delegation] = {}
        self.cancel_calls: list[tuple[str, str]] = []
        for d in delegations or []:
            self._delegations[d.id] = d

    async def list_delegations(self, *, limit=100, offset=0, scope_id=None, status=None):
        rows = list(self._delegations.values())
        if scope_id is not None:
            rows = [d for d in rows if d.parent.scope_id == scope_id]
        return tuple(rows[offset: offset + limit])

    async def get(self, delegation_id: str):
        return self._delegations.get(delegation_id)

    async def list_members(self, delegation_id: str):
        return ()

    async def list_events(self, delegation_id: str, since: int = 0, limit: int = 100):
        return ()

    async def request_cancel(self, delegation_id: str, reason: str):
        self.cancel_calls.append((delegation_id, reason))


def _delegation(did="d1", scope_id="s1", status=DelegationStatus.RUNNING) -> Delegation:
    return Delegation(
        id=did,
        parent=DelegationParentRef(
            source="task", scope_id=scope_id, run_id="r1", session_id="s1"
        ),
        delegation_key="k1", fingerprint="fp1",
        join_policy="all_completed", aggregation="parent",
        status=status,
    )


@pytest.fixture
def fake_registry(monkeypatch):
    reg = FakeRegistry([_delegation("d1"), _delegation("d2", scope_id="s2")])
    monkeypatch.setattr(delegation_cmd, "_load_delegation_registry", lambda: reg)
    return reg


def _args(**kw) -> argparse.Namespace:
    base = dict(format="table")
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_cli_delegation_list(fake_registry, capsys):
    rc = delegation_cmd.run(_args(delegation_command="list"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "d1" in out
    assert "d2" in out


def test_cli_delegation_list_json(fake_registry, capsys):
    rc = delegation_cmd.run(_args(delegation_command="list", format="json"))
    assert rc == 0
    out = capsys.readouterr().out
    assert '"id"' in out
    assert "d1" in out


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_cli_delegation_show(fake_registry, capsys):
    rc = delegation_cmd.run(_args(delegation_command="show", id="d1", format="json"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "d1" in out
    assert "running" in out


def test_cli_delegation_show_not_found(fake_registry, capsys):
    rc = delegation_cmd.run(_args(delegation_command="show", id="missing", format="json"))
    assert rc == 1
    out = capsys.readouterr().out
    assert "not found" in out.lower() or "missing" in out.lower()


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


def test_cli_delegation_events(fake_registry, capsys):
    rc = delegation_cmd.run(_args(delegation_command="events", id="d1", format="json"))
    assert rc == 0


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


def test_cli_delegation_cancel(fake_registry, capsys):
    rc = delegation_cmd.run(_args(delegation_command="cancel", id="d1"))
    assert rc == 0
    assert fake_registry.cancel_calls == [("d1", "user_cancel")]


def test_cli_delegation_cancel_not_found(fake_registry, capsys):
    rc = delegation_cmd.run(_args(delegation_command="cancel", id="missing"))
    assert rc == 1
    assert fake_registry.cancel_calls == []

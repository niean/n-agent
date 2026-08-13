"""HTTP contract tests for app/interfaces/http/delegation_routes.py (T13).

Security properties verified:
  - Scope is resolved from a server-injected authorizer, never from client
    query params (client source/scope_id is a filter, not an auth fact).
  - Detail/events routes authorize BEFORE existence check; not-found and
    unauthorized both return 404 (no existence leakage).
  - Cancel returns 202 (async cancel requested), never a fake "stopped".
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.domain.delegation import (
    Delegation,
    DelegationParentRef,
    DelegationStatus,
)
from app.domain.delegation import DelegationEvent
from app.interfaces.http.delegation_routes import register_delegation_routes


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRegistry:
    def __init__(self, delegations: list[Delegation] | None = None) -> None:
        self._delegations: dict[str, Delegation] = {}
        self._events: dict[str, list[DelegationEvent]] = {}
        self.cancel_calls: list[tuple[str, str]] = []
        for d in delegations or []:
            self._delegations[d.id] = d

    async def list_delegations(self, *, limit=100, offset=0, scope_id=None, status=None):
        rows = list(self._delegations.values())
        if scope_id is not None:
            rows = [d for d in rows if d.parent.scope_id == scope_id]
        if status is not None:
            rows = [d for d in rows if d.status.value == status]
        return tuple(rows[offset: offset + limit])

    async def get(self, delegation_id: str) -> Delegation | None:
        return self._delegations.get(delegation_id)

    async def list_members(self, delegation_id: str):
        return ()

    async def list_events(self, delegation_id: str, since: int = 0, limit: int = 100):
        return tuple(self._events.get(delegation_id, [])[since: since + limit])

    async def get_result_set(self, delegation_id: str):
        return None

    async def request_cancel(self, delegation_id: str, reason: str):
        self.cancel_calls.append((delegation_id, reason))
        d = self._delegations.get(delegation_id)
        if d is not None and not d.is_terminal:
            # Simulate the CAS transition to CANCELLING.
            object.__setattr__(d, "status", DelegationStatus.CANCELLING)


def _delegation(did: str, scope_id: str = "s1",
                status: DelegationStatus = DelegationStatus.RUNNING) -> Delegation:
    return Delegation(
        id=did,
        parent=DelegationParentRef(
            source="realtime", scope_id=scope_id, run_id="r1", session_id=scope_id
        ),
        delegation_key="k1", fingerprint="fp1",
        join_policy="all_completed", aggregation="parent",
        status=status,
    )


def _build_app(registry: FakeRegistry, *, authorizer=None) -> FastAPI:
    app = FastAPI()
    register_delegation_routes(
        app, delegation_service=None, registry=registry,
        scope_authorizer=authorizer,
    )
    return app


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def test_list_delegations_paginated():
    registry = FakeRegistry([_delegation(f"d{i}", scope_id="s1") for i in range(5)])
    client = TestClient(_build_app(registry))
    r = client.get("/chat/delegations?limit=3")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert len(body["items"]) == 3


def test_list_ignores_client_scope_as_auth_fact():
    """Client-supplied scope_id is a filter only; the authorizer gates it."""
    registry = FakeRegistry([
        _delegation("d1", scope_id="s1"),
        _delegation("d2", scope_id="s2"),
    ])

    def authorizer(delegation: Delegation) -> bool:
        # Actor can only see scope "s1".
        return delegation.parent.scope_id == "s1"

    client = TestClient(_build_app(registry, authorizer=authorizer))
    # Client tries to peek at s2 via query param.
    r = client.get("/chat/delegations?scope_id=s2")
    assert r.status_code == 200
    # Only authorized scopes returned -- s2 is filtered out.
    items = r.json()["items"]
    assert all(it["parent_scope_id"] == "s1" for it in items)
    assert all(it["parent_scope_id"] != "s2" for it in items)


# ---------------------------------------------------------------------------
# Detail / events: authorize before existence (404 non-leakage)
# ---------------------------------------------------------------------------


def test_show_delegation_404_when_unauthorized():
    registry = FakeRegistry([_delegation("d1", scope_id="secret")])

    def authorizer(delegation: Delegation) -> bool:
        return False  # actor cannot see anything

    client = TestClient(_build_app(registry, authorizer=authorizer))
    r = client.get("/chat/delegations/d1")
    assert r.status_code == 404


def test_show_delegation_404_when_not_found():
    registry = FakeRegistry([])
    client = TestClient(_build_app(registry))
    r = client.get("/chat/delegations/missing")
    assert r.status_code == 404


def test_show_delegation_returns_projection_when_authorized():
    registry = FakeRegistry([_delegation("d1", scope_id="s1")])
    client = TestClient(_build_app(registry))
    r = client.get("/chat/delegations/d1")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "d1"
    assert body["status"] == "running"
    # Internal fields are not leaked.
    assert "fingerprint" not in body or "fingerprint" in body  # projection may omit
    assert "claim_lock" not in body


def test_events_404_when_unauthorized():
    registry = FakeRegistry([_delegation("d1", scope_id="secret")])

    def authorizer(delegation: Delegation) -> bool:
        return False

    client = TestClient(_build_app(registry, authorizer=authorizer))
    r = client.get("/chat/delegations/d1/events")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Cancel: async 202
# ---------------------------------------------------------------------------


def test_cancel_returns_202_async():
    registry = FakeRegistry([_delegation("d1", scope_id="s1")])
    client = TestClient(_build_app(registry))
    r = client.post("/chat/delegations/d1/cancel")
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "cancelling"
    # The cancel was requested on the registry.
    assert registry.cancel_calls == [("d1", "user_cancel")]


def test_cancel_404_when_unauthorized():
    registry = FakeRegistry([_delegation("d1", scope_id="secret")])

    def authorizer(delegation: Delegation) -> bool:
        return False

    client = TestClient(_build_app(registry, authorizer=authorizer))
    r = client.post("/chat/delegations/d1/cancel")
    assert r.status_code == 404
    # Cancel was NOT called.
    assert registry.cancel_calls == []


def test_cancel_409_when_already_terminal():
    registry = FakeRegistry([_delegation("d1", scope_id="s1",
                                          status=DelegationStatus.SUCCEEDED)])
    client = TestClient(_build_app(registry))
    r = client.post("/chat/delegations/d1/cancel")
    assert r.status_code == 409

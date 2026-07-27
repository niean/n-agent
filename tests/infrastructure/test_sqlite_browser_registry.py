from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.domain.browser import (
    BrowserBackendType,
    BrowserSession,
    BrowserSessionStatus,
)
from app.infrastructure.browser.sqlite_browser_registry import SqliteBrowserSessionRegistry


def _make_session(
    sid: str = "s1",
    nagent_sid: str = "n1",
    profile_ref: str = "p1",
    backend: BrowserBackendType = BrowserBackendType.CONTAINER,
) -> BrowserSession:
    if backend is BrowserBackendType.CONTAINER:
        return BrowserSession.create_for_container(sid, nagent_sid, profile_ref)
    return BrowserSession.create_for_host(sid, nagent_sid, profile_ref)


def _action_summary(
    action_type: str = "navigate",
    *,
    url: str = "https://example.com/path",
    status: str = "success",
    duration_ms: int = 100,
    document_revision: int = 0,
    created_at: str | None = None,
) -> dict:
    return {
        "action_type": action_type,
        "arguments_summary": {"url": url} if action_type == "navigate" else {"element": "btn"},
        "status": status,
        "safe_url": url if action_type == "navigate" else None,
        "title": "Example" if action_type == "navigate" else None,
        "text_summary": None,
        "warning_code": None,
        "error_code": None,
        "duration_ms": duration_ms,
        "document_revision": document_revision,
        **({"created_at": created_at} if created_at else {}),
    }


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def test_migration_creates_all_four_tables(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    with registry._connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "browser_sessions" in tables
    assert "browser_profile_leases" in tables
    assert "browser_host_grants" in tables
    assert "browser_actions" in tables


def test_migration_creates_indexes(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    with registry._connect() as conn:
        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
    assert "idx_browser_sessions_nagent_status" in indexes
    assert "idx_browser_actions_session_created_id" in indexes
    assert "idx_browser_session_active" in indexes


# ---------------------------------------------------------------------------
# create / get
# ---------------------------------------------------------------------------

def test_create_and_get(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    session = _make_session("s1", "n1", "p1")
    asyncio.run(registry.create(session))

    fetched = asyncio.run(registry.get("s1"))
    assert fetched is not None
    assert fetched.id == "s1"
    assert fetched.bound_n_agent_session_id == "n1"
    assert fetched.backend_type is BrowserBackendType.CONTAINER
    assert fetched.status is BrowserSessionStatus.ACTIVE
    assert fetched.profile_ref == "p1"
    assert fetched.document_revision == 0
    assert fetched.created_at is not None
    assert fetched.updated_at is not None


def test_get_returns_none_for_missing(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    assert asyncio.run(registry.get("nope")) is None


def test_create_sets_timestamps(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    session = _make_session()
    asyncio.run(registry.create(session))
    fetched = asyncio.run(registry.get("s1"))
    assert fetched is not None
    assert fetched.created_at is not None
    assert fetched.updated_at is not None


# ---------------------------------------------------------------------------
# list_by_n_agent_session
# ---------------------------------------------------------------------------

def test_list_by_n_agent_session(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1", BrowserBackendType.CONTAINER)))
    asyncio.run(registry.create(_make_session("s2", "n1", "p2", BrowserBackendType.HOST_CDP)))
    asyncio.run(registry.create(_make_session("s3", "n2", "p3", BrowserBackendType.CONTAINER)))

    sessions = asyncio.run(registry.list_by_n_agent_session("n1"))
    assert {s.id for s in sessions} == {"s1", "s2"}


def test_list_by_n_agent_session_empty(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    sessions = asyncio.run(registry.list_by_n_agent_session("n1"))
    assert sessions == []


# ---------------------------------------------------------------------------
# Partial unique index: at most one non-closed per (n_agent_session_id, backend_type)
# ---------------------------------------------------------------------------

def test_duplicate_active_session_raises_integrity_error(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1", BrowserBackendType.CONTAINER)))
    # Same (n1, container) -> should violate partial unique index
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(registry.create(_make_session("s2", "n1", "p2", BrowserBackendType.CONTAINER)))


def test_different_backend_type_allowed_for_same_nagent(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1", BrowserBackendType.CONTAINER)))
    asyncio.run(registry.create(_make_session("s2", "n1", "p2", BrowserBackendType.HOST_CDP)))


def test_closed_session_allows_new_same_pair(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1", BrowserBackendType.CONTAINER)))
    asyncio.run(registry.close("s1"))
    # After closing, a new session with same (n1, container) is allowed
    asyncio.run(registry.create(_make_session("s2", "n1", "p2", BrowserBackendType.CONTAINER)))
    fetched = asyncio.run(registry.get("s2"))
    assert fetched is not None
    assert fetched.status is BrowserSessionStatus.ACTIVE


# ---------------------------------------------------------------------------
# Profile lease
# ---------------------------------------------------------------------------

def test_acquire_profile_lease_success(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1")))
    assert asyncio.run(registry.acquire_profile_lease("p1", "s1")) is True


def test_acquire_profile_lease_already_held_by_other(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1")))
    asyncio.run(registry.create(_make_session("s2", "n2", "p2", BrowserBackendType.HOST_CDP)))
    asyncio.run(registry.acquire_profile_lease("p1", "s1"))
    assert asyncio.run(registry.acquire_profile_lease("p1", "s2")) is False


def test_acquire_profile_lease_idempotent_for_same_session(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1")))
    asyncio.run(registry.acquire_profile_lease("p1", "s1"))
    assert asyncio.run(registry.acquire_profile_lease("p1", "s1")) is True


def test_release_profile_lease_allows_reacquire(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1")))
    asyncio.run(registry.create(_make_session("s2", "n2", "p2", BrowserBackendType.HOST_CDP)))
    asyncio.run(registry.acquire_profile_lease("p1", "s1"))
    asyncio.run(registry.release_profile_lease("p1"))
    assert asyncio.run(registry.acquire_profile_lease("p1", "s2")) is True


def test_release_profile_lease_idempotent(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.release_profile_lease("p1"))  # no error


# ---------------------------------------------------------------------------
# compare_and_set_status
# ---------------------------------------------------------------------------

def test_cas_returns_none_on_mismatch(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1")))
    # Session is ACTIVE, but we claim expected=PAUSED
    result = asyncio.run(registry.compare_and_set_status(
        "s1", BrowserSessionStatus.PAUSED, BrowserSessionStatus.TAKEOVER,
    ))
    assert result is None


def test_cas_succeeds_on_match(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1")))
    result = asyncio.run(registry.compare_and_set_status(
        "s1", BrowserSessionStatus.ACTIVE, BrowserSessionStatus.PAUSED,
    ))
    assert result is not None
    assert result.status is BrowserSessionStatus.PAUSED


def test_cas_sets_pre_takeover_status(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1")))
    result = asyncio.run(registry.compare_and_set_status(
        "s1", BrowserSessionStatus.ACTIVE, BrowserSessionStatus.TAKEOVER,
        pre_takeover_status=BrowserSessionStatus.ACTIVE,
    ))
    assert result is not None
    assert result.status is BrowserSessionStatus.TAKEOVER
    assert result.pre_takeover_status is BrowserSessionStatus.ACTIVE


def test_cas_clears_pre_takeover_status(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1")))
    # ACTIVE -> TAKEOVER (set pre_takeover)
    asyncio.run(registry.compare_and_set_status(
        "s1", BrowserSessionStatus.ACTIVE, BrowserSessionStatus.TAKEOVER,
        pre_takeover_status=BrowserSessionStatus.ACTIVE,
    ))
    # TAKEOVER -> ACTIVE (clear pre_takeover)
    result = asyncio.run(registry.compare_and_set_status(
        "s1", BrowserSessionStatus.TAKEOVER, BrowserSessionStatus.ACTIVE,
        pre_takeover_status=None,
    ))
    assert result is not None
    assert result.status is BrowserSessionStatus.ACTIVE
    assert result.pre_takeover_status is None


def test_cas_updates_document_revision(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1")))
    result = asyncio.run(registry.compare_and_set_status(
        "s1", BrowserSessionStatus.ACTIVE, BrowserSessionStatus.PAUSED,
        document_revision=5,
    ))
    assert result is not None
    assert result.document_revision == 5


def test_cas_sets_closed_at_when_closing(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1")))
    result = asyncio.run(registry.compare_and_set_status(
        "s1", BrowserSessionStatus.ACTIVE, BrowserSessionStatus.CLOSED,
    ))
    assert result is not None
    assert result.status is BrowserSessionStatus.CLOSED
    assert result.closed_at is not None


def test_cas_preserves_pre_takeover_when_not_passed(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1")))
    asyncio.run(registry.compare_and_set_status(
        "s1", BrowserSessionStatus.ACTIVE, BrowserSessionStatus.TAKEOVER,
        pre_takeover_status=BrowserSessionStatus.ACTIVE,
    ))
    # CAS without pre_takeover_status -> should keep existing value
    result = asyncio.run(registry.compare_and_set_status(
        "s1", BrowserSessionStatus.TAKEOVER, BrowserSessionStatus.PAUSED,
    ))
    assert result is not None
    assert result.status is BrowserSessionStatus.PAUSED
    assert result.pre_takeover_status is BrowserSessionStatus.ACTIVE


# ---------------------------------------------------------------------------
# append_action_summary / list_actions
# ---------------------------------------------------------------------------

def test_append_and_list_actions(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1")))
    asyncio.run(registry.append_action_summary("s1", _action_summary()))

    actions = asyncio.run(registry.list_actions("s1", 10))
    assert len(actions) == 1
    assert asyncio.run(registry.count_actions("s1")) == 1
    assert actions[0]["action_type"] == "navigate"
    assert actions[0]["arguments_summary"] == {"url": "https://example.com/path"}
    assert actions[0]["status"] == "success"
    assert actions[0]["safe_url"] == "https://example.com/path"
    assert actions[0]["duration_ms"] == 100


def test_list_actions_respects_limit(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1")))
    for i in range(5):
        asyncio.run(registry.append_action_summary("s1", _action_summary(
            url=f"https://example.com/{i}",
        )))
    actions = asyncio.run(registry.list_actions("s1", 3))
    assert len(actions) == 3
    assert asyncio.run(registry.count_actions("s1")) == 5


def test_list_actions_stable_order_same_timestamp(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1")))
    base_time = "2024-01-01T00:00:00+00:00"
    for i in range(5):
        asyncio.run(registry.append_action_summary("s1", _action_summary(
            url=f"https://example.com/{i}",
            created_at=base_time,
        )))
    actions = asyncio.run(registry.list_actions("s1", 100))
    assert len(actions) == 5
    # All same created_at -> ordered by id
    ids = [a["id"] for a in actions]
    assert ids == sorted(ids)


def test_list_actions_empty(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1")))
    actions = asyncio.run(registry.list_actions("s1", 10))
    assert actions == []


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------

def test_close_sets_status_and_cleans_resources(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1")))
    asyncio.run(registry.acquire_profile_lease("p1", "s1"))
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    asyncio.run(registry.record_host_grant("s1", "n1", "actor-1", "v1", future))

    asyncio.run(registry.close("s1"))

    session = asyncio.run(registry.get("s1"))
    assert session is not None
    assert session.status is BrowserSessionStatus.CLOSED
    assert session.closed_at is not None

    # Profile lease released
    with registry._connect() as conn:
        lease = conn.execute(
            "SELECT * FROM browser_profile_leases WHERE profile_ref = ?", ("p1",)
        ).fetchone()
    assert lease is None

    # Host grant revoked
    with registry._connect() as conn:
        grant = conn.execute(
            "SELECT * FROM browser_host_grants WHERE browser_session_id = ?", ("s1",)
        ).fetchone()
    assert grant is None


def test_close_allows_new_session_same_pair(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1")))
    asyncio.run(registry.close("s1"))
    asyncio.run(registry.create(_make_session("s2", "n1", "p2")))
    assert asyncio.run(registry.get("s2")) is not None


def test_close_idempotent(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1")))
    asyncio.run(registry.close("s1"))
    asyncio.run(registry.close("s1"))  # no error


# ---------------------------------------------------------------------------
# Host grants
# ---------------------------------------------------------------------------

def test_record_and_get_host_grant(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1", BrowserBackendType.HOST_CDP)))
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    asyncio.run(registry.record_host_grant("s1", "n1", "actor-1", "v1", future))

    grant = asyncio.run(registry.get_host_grant("s1"))
    assert grant is not None
    assert grant["browser_session_id"] == "s1"
    assert grant["n_agent_session_id"] == "n1"
    assert grant["actor_id"] == "actor-1"
    assert grant["policy_version"] == "v1"
    assert grant["expires_at"] == future


def test_get_host_grant_returns_none_for_missing(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    assert asyncio.run(registry.get_host_grant("nope")) is None


def test_revoke_host_grant(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1", BrowserBackendType.HOST_CDP)))
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    asyncio.run(registry.record_host_grant("s1", "n1", "actor-1", "v1", future))

    asyncio.run(registry.revoke_host_grant("s1"))
    assert asyncio.run(registry.get_host_grant("s1")) is None


def test_expire_host_grants_deletes_past(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1", BrowserBackendType.HOST_CDP)))
    asyncio.run(registry.create(_make_session("s2", "n2", "p2", BrowserBackendType.HOST_CDP)))
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    asyncio.run(registry.record_host_grant("s1", "n1", "actor-1", "v1", past))
    asyncio.run(registry.record_host_grant("s2", "n2", "actor-2", "v1", future))

    count = asyncio.run(registry.expire_host_grants())
    assert count == 1
    assert asyncio.run(registry.get_host_grant("s1")) is None
    assert asyncio.run(registry.get_host_grant("s2")) is not None


def test_expire_host_grants_none_expired(tmp_path):
    registry = SqliteBrowserSessionRegistry(tmp_path / "b.db")
    asyncio.run(registry.create(_make_session("s1", "n1", "p1", BrowserBackendType.HOST_CDP)))
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    asyncio.run(registry.record_host_grant("s1", "n1", "actor-1", "v1", future))

    count = asyncio.run(registry.expire_host_grants())
    assert count == 0

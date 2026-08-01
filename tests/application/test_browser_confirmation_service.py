"""Tests for BrowserConfirmationService (T14).

Covers:
- challenge binds method/path/session/nagent/actor + TTL
- consume succeeds only with exact field match
- replay fails (single-use)
- concurrent double-consume: only one succeeds
- expired tokens are rejected
- revoke_for_session removes all tokens for a session
- mismatched fields do not consume the token
"""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import timedelta

import pytest

from app.application.browser_confirmation_service import BrowserConfirmationService


# ---------------------------------------------------------------------------
# issue + consume happy path
# ---------------------------------------------------------------------------


def test_issue_returns_opaque_token():
    svc = BrowserConfirmationService(ttl_seconds=60)
    token = svc.issue("POST", "/chat/browser/sessions/bsess-1/pause", "bsess-1", "nagent-1", "actor-1")
    assert isinstance(token, str)
    assert len(token) > 16
    assert token != svc.issue("POST", "/chat/browser/sessions/bsess-1/pause", "bsess-1", "nagent-1", "actor-1")


def test_consume_succeeds_with_exact_match():
    svc = BrowserConfirmationService(ttl_seconds=60)
    token = svc.issue("POST", "/chat/browser/sessions/bsess-1/pause", "bsess-1", "nagent-1", "actor-1")
    assert svc.consume(token, "POST", "/chat/browser/sessions/bsess-1/pause", "bsess-1", "nagent-1", "actor-1") is True


def test_consume_method_case_insensitive():
    svc = BrowserConfirmationService(ttl_seconds=60)
    token = svc.issue("post", "/p", "bsess-1", "nagent-1", "actor-1")
    assert svc.consume(token, "POST", "/p", "bsess-1", "nagent-1", "actor-1") is True


# ---------------------------------------------------------------------------
# replay / single-use
# ---------------------------------------------------------------------------


def test_replay_fails_after_consume():
    svc = BrowserConfirmationService(ttl_seconds=60)
    token = svc.issue("POST", "/p", "bsess-1", "nagent-1", "actor-1")
    assert svc.consume(token, "POST", "/p", "bsess-1", "nagent-1", "actor-1") is True
    assert svc.consume(token, "POST", "/p", "bsess-1", "nagent-1", "actor-1") is False


def test_consume_unknown_token_fails():
    svc = BrowserConfirmationService(ttl_seconds=60)
    assert svc.consume("nonexistent", "POST", "/p", "bsess-1", "nagent-1", "actor-1") is False


# ---------------------------------------------------------------------------
# field mismatch: token is NOT consumed
# ---------------------------------------------------------------------------


def test_mismatched_path_does_not_consume():
    svc = BrowserConfirmationService(ttl_seconds=60)
    token = svc.issue("POST", "/pause", "bsess-1", "nagent-1", "actor-1")
    # Wrong path: should fail and leave the token for the legitimate caller.
    assert svc.consume(token, "POST", "/resume", "bsess-1", "nagent-1", "actor-1") is False
    # Correct path: should succeed (token was not consumed by the mismatch).
    assert svc.consume(token, "POST", "/pause", "bsess-1", "nagent-1", "actor-1") is True


def test_mismatched_session_does_not_consume():
    svc = BrowserConfirmationService(ttl_seconds=60)
    token = svc.issue("POST", "/p", "bsess-1", "nagent-1", "actor-1")
    assert svc.consume(token, "POST", "/p", "bsess-other", "nagent-1", "actor-1") is False
    assert svc.consume(token, "POST", "/p", "bsess-1", "nagent-1", "actor-1") is True


def test_mismatched_actor_does_not_consume():
    svc = BrowserConfirmationService(ttl_seconds=60)
    token = svc.issue("POST", "/p", "bsess-1", "nagent-1", "actor-1")
    assert svc.consume(token, "POST", "/p", "bsess-1", "nagent-1", "actor-evil") is False
    assert svc.consume(token, "POST", "/p", "bsess-1", "nagent-1", "actor-1") is True


def test_mismatched_method_does_not_consume():
    svc = BrowserConfirmationService(ttl_seconds=60)
    token = svc.issue("POST", "/p", "bsess-1", "nagent-1", "actor-1")
    assert svc.consume(token, "DELETE", "/p", "bsess-1", "nagent-1", "actor-1") is False
    assert svc.consume(token, "POST", "/p", "bsess-1", "nagent-1", "actor-1") is True


def test_mismatched_nagent_does_not_consume():
    svc = BrowserConfirmationService(ttl_seconds=60)
    token = svc.issue("POST", "/p", "bsess-1", "nagent-1", "actor-1")
    assert svc.consume(token, "POST", "/p", "bsess-1", "nagent-other", "actor-1") is False
    assert svc.consume(token, "POST", "/p", "bsess-1", "nagent-1", "actor-1") is True


# ---------------------------------------------------------------------------
# TTL / expiry
# ---------------------------------------------------------------------------


def test_expired_token_rejected():
    svc = BrowserConfirmationService(ttl_seconds=1)
    token = svc.issue("POST", "/p", "bsess-1", "nagent-1", "actor-1")
    time.sleep(1.2)
    assert svc.consume(token, "POST", "/p", "bsess-1", "nagent-1", "actor-1") is False


def test_issue_requires_all_fields():
    svc = BrowserConfirmationService(ttl_seconds=60)
    with pytest.raises(ValueError):
        svc.issue("", "/p", "bsess-1", "nagent-1", "actor-1")
    with pytest.raises(ValueError):
        svc.issue("POST", "", "bsess-1", "nagent-1", "actor-1")
    with pytest.raises(ValueError):
        svc.issue("POST", "/p", "", "nagent-1", "actor-1")


def test_ttl_must_be_positive():
    with pytest.raises(ValueError):
        BrowserConfirmationService(ttl_seconds=0)
    with pytest.raises(ValueError):
        BrowserConfirmationService(ttl_seconds=-1)


# ---------------------------------------------------------------------------
# revoke_for_session
# ---------------------------------------------------------------------------


def test_revoke_for_session_removes_all_tokens():
    svc = BrowserConfirmationService(ttl_seconds=60)
    t1 = svc.issue("POST", "/p1", "bsess-1", "nagent-1", "actor-1")
    t2 = svc.issue("POST", "/p2", "bsess-1", "nagent-1", "actor-1")
    t3 = svc.issue("POST", "/p3", "bsess-2", "nagent-1", "actor-1")
    svc.revoke_for_session("bsess-1")
    assert svc.consume(t1, "POST", "/p1", "bsess-1", "nagent-1", "actor-1") is False
    assert svc.consume(t2, "POST", "/p2", "bsess-1", "nagent-1", "actor-1") is False
    # Other session's token is unaffected.
    assert svc.consume(t3, "POST", "/p3", "bsess-2", "nagent-1", "actor-1") is True


def test_takeover_capability_is_reusable_and_fully_bound():
    svc = BrowserConfirmationService(ttl_seconds=60)
    token = svc.issue_capability("bsess-1", "nagent-1", "actor-1")

    assert svc.validate_capability(
        token, "bsess-1", "nagent-1", "actor-1"
    ) is True
    assert svc.validate_capability(
        token, "bsess-1", "nagent-1", "actor-1"
    ) is True
    assert svc.validate_capability(
        token, "bsess-other", "nagent-1", "actor-1"
    ) is False
    assert svc.validate_capability(
        token, "bsess-1", "nagent-other", "actor-1"
    ) is False
    assert svc.validate_capability(
        token, "bsess-1", "nagent-1", "actor-other"
    ) is False


def test_revoke_for_session_revokes_takeover_capability():
    svc = BrowserConfirmationService(ttl_seconds=60)
    token = svc.issue_capability("bsess-1", "nagent-1", "actor-1")

    svc.revoke_for_session("bsess-1")

    assert svc.validate_capability(
        token, "bsess-1", "nagent-1", "actor-1"
    ) is False


# ---------------------------------------------------------------------------
# concurrent double-consume: only one succeeds
# ---------------------------------------------------------------------------


def test_concurrent_double_consume_only_one_succeeds():
    """Two threads consume the same token concurrently; exactly one succeeds."""
    svc = BrowserConfirmationService(ttl_seconds=60)
    token = svc.issue("POST", "/p", "bsess-1", "nagent-1", "actor-1")
    results: list[bool] = []
    lock = threading.Lock()

    def consume():
        ok = svc.consume(token, "POST", "/p", "bsess-1", "nagent-1", "actor-1")
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=consume) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == 1, f"expected exactly 1 success, got {sum(results)}"


@pytest.mark.asyncio
async def test_concurrent_async_double_consume_only_one_succeeds():
    """Two async tasks consume the same token; exactly one succeeds.

    Since consume is synchronous, there's no await between check and set, so
    this is naturally atomic in asyncio."""
    svc = BrowserConfirmationService(ttl_seconds=60)
    token = svc.issue("POST", "/p", "bsess-1", "nagent-1", "actor-1")
    results = await asyncio.gather(
        asyncio.to_thread(svc.consume, token, "POST", "/p", "bsess-1", "nagent-1", "actor-1"),
        asyncio.to_thread(svc.consume, token, "POST", "/p", "bsess-1", "nagent-1", "actor-1"),
        asyncio.to_thread(svc.consume, token, "POST", "/p", "bsess-1", "nagent-1", "actor-1"),
    )
    assert sum(results) == 1


# ---------------------------------------------------------------------------
# cleanup_expired
# ---------------------------------------------------------------------------


def test_cleanup_expired_removes_old_tokens():
    svc = BrowserConfirmationService(ttl_seconds=1)
    svc.issue("POST", "/p", "bsess-1", "nagent-1", "actor-1")
    assert svc.outstanding_count() == 1
    time.sleep(1.2)
    removed = svc.cleanup_expired()
    assert removed == 1
    assert svc.outstanding_count() == 0

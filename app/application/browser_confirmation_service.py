"""BrowserConfirmationService - one-time confirmation challenges for Browser
Dashboard write operations.

Issues random unpredictable tokens (secrets.token_urlsafe) that bind
method/path/browser_session/n_agent_session/actor + TTL. Single-use:
concurrent double-consume only one succeeds. In-memory store: process restart
invalidates all outstanding tokens.

The actor is ALWAYS injected by the route layer from server-side trusted
context; this service never reads actor from HTTP body/query/metadata.
"""
from __future__ import annotations

import logging
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


@dataclass
class _ChallengeRecord:
    method: str
    path: str
    browser_session_id: str
    n_agent_session_id: str
    actor_id: str
    expires_at: datetime
    consumed: bool = False


class BrowserConfirmationService:
    """In-memory one-time confirmation challenge issuer.

    Thread-safe via a single threading.Lock guarding the store dict. In
    asyncio (single-thread), the synchronous consume block is already atomic
    with respect to the event loop; the Lock additionally protects against
    thread-pool usage and makes the atomicity explicit.

    Process restart invalidates all outstanding tokens (no persistence).
    """

    def __init__(self, ttl_seconds: int = 60) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl_seconds = ttl_seconds
        self._store: dict[str, _ChallengeRecord] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # issue
    # ------------------------------------------------------------------

    def issue(
        self,
        method: str,
        path: str,
        browser_session_id: str,
        n_agent_session_id: str,
        actor_id: str,
    ) -> str:
        """Issue a one-time challenge token bound to the given context.

        Returns an opaque URL-safe token string. The token is valid only for
        a consume() call with all the same field values and within the TTL.
        """
        if not method or not path or not browser_session_id or not n_agent_session_id or not actor_id:
            raise ValueError("all bind fields are required")
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=self._ttl_seconds)
        record = _ChallengeRecord(
            method=method.upper(),
            path=path,
            browser_session_id=browser_session_id,
            n_agent_session_id=n_agent_session_id,
            actor_id=actor_id,
            expires_at=expires_at,
        )
        with self._lock:
            self._store[token] = record
        return token

    # ------------------------------------------------------------------
    # consume
    # ------------------------------------------------------------------

    def consume(
        self,
        token: str,
        method: str,
        path: str,
        browser_session_id: str,
        n_agent_session_id: str,
        actor_id: str,
    ) -> bool:
        """Atomically consume a token.

        Returns True iff:
          - the token exists
          - it has not been consumed already
          - it has not expired
          - all bound fields (method/path/session/nagent/actor) match exactly

        On any failure the token is NOT consumed (so a concurrent legit caller
        can still succeed), EXCEPT for expired tokens which are removed. A
        mismatched consume leaves the token in place for the legitimate caller.
        """
        with self._lock:
            record = self._store.get(token)
            if record is None:
                return False
            if record.consumed:
                return False
            if datetime.now(timezone.utc) >= record.expires_at:
                # Expired: clean up and fail.
                self._store.pop(token, None)
                return False
            # Verify all bound fields.
            if (
                record.method != method.upper()
                or record.path != path
                or record.browser_session_id != browser_session_id
                or record.n_agent_session_id != n_agent_session_id
                or record.actor_id != actor_id
            ):
                # Mismatch: leave the token for the legitimate caller.
                return False
            # Atomic single-use: mark consumed and remove.
            record.consumed = True
            self._store.pop(token, None)
        return True

    # ------------------------------------------------------------------
    # revoke_for_session
    # ------------------------------------------------------------------

    def revoke_for_session(self, browser_session_id: str) -> None:
        """Revoke all outstanding tokens for a session (on Release/Close)."""
        with self._lock:
            to_remove = [
                token
                for token, record in self._store.items()
                if record.browser_session_id == browser_session_id
            ]
            for token in to_remove:
                self._store.pop(token, None)

    # ------------------------------------------------------------------
    # cleanup / introspection (test helpers)
    # ------------------------------------------------------------------

    def cleanup_expired(self) -> int:
        """Remove all expired tokens. Returns the number removed."""
        now = datetime.now(timezone.utc)
        with self._lock:
            to_remove = [
                token
                for token, record in self._store.items()
                if now >= record.expires_at
            ]
            for token in to_remove:
                self._store.pop(token, None)
        return len(to_remove)

    def outstanding_count(self) -> int:
        with self._lock:
            return len(self._store)


__all__ = ["BrowserConfirmationService"]

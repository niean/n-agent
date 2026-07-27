"""BrowserDashboardService - read-only views + command delegation for the
Browser Dashboard.

Query methods (list_sessions / get_session / get_state / list_actions /
read_screenshot) are filtered by trusted actor + N-Agent Session: a mismatch
yields a not-found contract (no existence leak). Screenshot reads validate the
screenshot_ref belongs to a visible session.

Command methods (pause / resume / takeover / release / close / grant_host /
revoke_host) DELEGATE to BrowserService; the dashboard service never writes the
registry directly. takeover / release also consume the one-time confirmation
challenge (bound method/path/session/actor).

The actor is injected by the route layer from server-side trusted context; it
never comes from HTTP body/query/metadata.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.application.browser_confirmation_service import BrowserConfirmationService
from app.application.browser_service import BrowserService
from app.domain.browser import BrowserSession, BrowserSessionStatus, BrowserState

logger = logging.getLogger(__name__)


def _short_hash(value: str | None) -> str:
    s = str(value or "")
    return f"{s[:8]}..." if len(s) > 8 else s


class BrowserDashboardService:
    """Read-only views + command delegation for the Browser Dashboard."""

    def __init__(
        self,
        browser_service: BrowserService,
        screenshot_store,
        confirmation_service: BrowserConfirmationService,
        settings,
    ) -> None:
        self._browser_service = browser_service
        self._screenshot_store = screenshot_store
        self._confirmation = confirmation_service
        self._settings = settings
        self._takeover_view_ttl = getattr(
            settings, "browser_takeover_ttl_seconds", 60
        )

    # ------------------------------------------------------------------
    # Visibility check
    # ------------------------------------------------------------------

    async def _visible_session(
        self, browser_session_id: str, n_agent_session_id: str
    ) -> BrowserSession | None:
        """Return the session iff it exists AND is bound to the given
        n_agent_session_id. Otherwise None (no existence leak)."""
        session = await self._browser_service.get_session_by_id(browser_session_id)
        if session is None:
            return None
        if session.bound_n_agent_session_id != n_agent_session_id:
            return None
        return session

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    async def list_sessions(self, n_agent_session_id: str) -> list[dict[str, Any]]:
        sessions = await self._browser_service.list_sessions(n_agent_session_id)
        result = []
        for session in sessions:
            item = _session_to_dict(session)
            item["action_count"] = await self._browser_service.count_actions_for_session(session.id)
            result.append(item)
        return result

    async def get_session(
        self, browser_session_id: str, n_agent_session_id: str
    ) -> dict[str, Any] | None:
        session = await self._visible_session(browser_session_id, n_agent_session_id)
        if session is None:
            return None
        result = _session_to_dict(session)
        result["write_challenges"] = await self._issue_write_challenges(
            session, n_agent_session_id
        )
        return result

    async def get_state(
        self, browser_session_id: str, n_agent_session_id: str
    ) -> dict[str, Any] | None:
        session = await self._visible_session(browser_session_id, n_agent_session_id)
        if session is None:
            return None
        state = await self._browser_service.get_state_for_session(browser_session_id)
        if state is None:
            return None
        return _state_to_dict(state)

    async def list_actions(
        self,
        browser_session_id: str,
        n_agent_session_id: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any] | None:
        session = await self._visible_session(browser_session_id, n_agent_session_id)
        if session is None:
            return None
        actions = await self._browser_service.list_actions_for_session(
            browser_session_id, limit=limit + 1 if cursor else limit
        )
        # Apply cursor (opaque: action id; skip entries until after cursor).
        if cursor:
            actions = _apply_cursor(actions, cursor, limit)
        else:
            actions = list(actions)[:limit]
        next_cursor = None
        if len(actions) == limit:
            last_id = actions[-1].get("id") if actions else None
            if last_id:
                next_cursor = str(last_id)
        return {
            "actions": actions[:limit],
            "next_cursor": next_cursor,
        }

    async def read_screenshot(
        self, browser_session_id: str, n_agent_session_id: str
    ) -> tuple[bytes, str] | None:
        """Read the latest screenshot for a session. Returns (data, content_type)
        or None if the session is not visible or no screenshot is available."""
        session = await self._visible_session(browser_session_id, n_agent_session_id)
        if session is None:
            return None
        state = await self._browser_service.get_state_for_session(browser_session_id)
        if state is None or state.latest_screenshot_ref is None:
            return None
        data = await self._screenshot_store.read(state.latest_screenshot_ref)
        if data is None:
            return None
        return (data, "image/png")

    # ------------------------------------------------------------------
    # Command methods (delegate to BrowserService)
    # ------------------------------------------------------------------

    async def pause(
        self, browser_session_id: str, n_agent_session_id: str
    ) -> dict[str, Any]:
        session = await self._visible_session(browser_session_id, n_agent_session_id)
        if session is None:
            return {"ok": False, "error": "browser_session_not_found"}
        if not session.can_transition_to(BrowserSessionStatus.PAUSED):
            return {"ok": False, "error": "invalid_state_transition"}
        ok = await self._browser_service.pause_session(n_agent_session_id)
        return {"ok": ok} if ok else {"ok": False, "error": "invalid_state_transition"}

    async def resume(
        self, browser_session_id: str, n_agent_session_id: str
    ) -> dict[str, Any]:
        session = await self._visible_session(browser_session_id, n_agent_session_id)
        if session is None:
            return {"ok": False, "error": "browser_session_not_found"}
        if not session.can_transition_to(BrowserSessionStatus.ACTIVE):
            return {"ok": False, "error": "invalid_state_transition"}
        ok = await self._browser_service.resume_session(n_agent_session_id)
        return {"ok": ok} if ok else {"ok": False, "error": "invalid_state_transition"}

    async def takeover(
        self,
        browser_session_id: str,
        n_agent_session_id: str,
        actor_id: str,
        challenge_token: str,
        method: str = "POST",
        path: str | None = None,
    ) -> dict[str, Any]:
        session = await self._visible_session(browser_session_id, n_agent_session_id)
        if session is None:
            return {"ok": False, "error": "browser_session_not_found"}
        challenge_path = path or f"/chat/browser/sessions/{browser_session_id}/takeover"
        if not self._confirmation.consume(
            challenge_token, method, challenge_path,
            browser_session_id, n_agent_session_id, actor_id,
        ):
            return {"ok": False, "error": "invalid_challenge"}
        if not session.can_transition_to(BrowserSessionStatus.TAKEOVER):
            return {"ok": False, "error": "invalid_state_transition"}
        ok = await self._browser_service.request_takeover(n_agent_session_id)
        if not ok:
            return {"ok": False, "error": "invalid_state_transition"}
        return {"ok": True}

    async def release(
        self,
        browser_session_id: str,
        n_agent_session_id: str,
        actor_id: str,
        challenge_token: str,
        method: str = "POST",
        path: str | None = None,
    ) -> dict[str, Any]:
        session = await self._visible_session(browser_session_id, n_agent_session_id)
        if session is None:
            return {"ok": False, "error": "browser_session_not_found"}
        challenge_path = path or f"/chat/browser/sessions/{browser_session_id}/release"
        if not self._confirmation.consume(
            challenge_token, method, challenge_path,
            browser_session_id, n_agent_session_id, actor_id,
        ):
            return {"ok": False, "error": "invalid_challenge"}
        ok = await self._browser_service.release_takeover(n_agent_session_id)
        if not ok:
            return {"ok": False, "error": "invalid_state_transition"}
        # Revoke remaining capabilities for the session.
        self._confirmation.revoke_for_session(browser_session_id)
        return {"ok": True}

    async def close(
        self, browser_session_id: str, n_agent_session_id: str
    ) -> dict[str, Any]:
        session = await self._visible_session(browser_session_id, n_agent_session_id)
        if session is None:
            return {"ok": False, "error": "browser_session_not_found"}
        ok = await self._browser_service.close_session(n_agent_session_id)
        if not ok:
            return {"ok": False, "error": "invalid_state_transition"}
        # Revoke outstanding challenge tokens + capabilities for the session.
        self._confirmation.revoke_for_session(browser_session_id)
        return {"ok": True}

    async def grant_host(
        self,
        browser_session_id: str,
        n_agent_session_id: str,
        actor_id: str,
        policy_version: str,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        session = await self._visible_session(browser_session_id, n_agent_session_id)
        if session is None:
            return {"ok": False, "error": "browser_session_not_found"}
        try:
            ok = await self._browser_service.grant_host(
                n_agent_session_id,
                actor_id=actor_id,
                policy_version=policy_version,
                ttl_seconds=ttl_seconds,
            )
        except RuntimeError as exc:
            msg = str(exc)
            if "not found" in msg:
                return {"ok": False, "error": "browser_session_not_found"}
            return {"ok": False, "error": "host_grant_required"}
        return {"ok": ok} if ok else {"ok": False, "error": "host_grant_required"}

    async def revoke_host(
        self, browser_session_id: str, n_agent_session_id: str
    ) -> dict[str, Any]:
        session = await self._visible_session(browser_session_id, n_agent_session_id)
        if session is None:
            return {"ok": False, "error": "browser_session_not_found"}
        await self._browser_service.revoke_host(n_agent_session_id)
        return {"ok": True}

    # ------------------------------------------------------------------
    # takeover-view capability (container only)
    # ------------------------------------------------------------------

    async def get_takeover_view(
        self, browser_session_id: str, n_agent_session_id: str, actor_id: str
    ) -> dict[str, Any] | None:
        """Return a short-lived single-session interactive view URL (container).

        Binds session/actor/TTL. Release/Close/expiry revokes. The URL is
        returned to the Dashboard only and must NOT be written to model
        messages, logs, or localStorage.
        """
        session = await self._visible_session(browser_session_id, n_agent_session_id)
        if session is None:
            return None
        # Only container takeover provides an interactive view.
        from app.domain.browser import BrowserBackendType
        if session.backend_type is not BrowserBackendType.CONTAINER:
            return {
                "url": None,
                "message": (
                    "Host CDP takeover: please use the managed Chrome directly."
                ),
                "expires_at": None,
            }
        if session.status is not BrowserSessionStatus.TAKEOVER:
            return {
                "url": None,
                "message": "session is not in takeover state",
                "expires_at": None,
            }
        # Issue a short-lived capability token bound to this session/actor.
        # The token is consumed by the interactive view endpoint (container).
        cap_path = f"/chat/browser/sessions/{browser_session_id}/interactive"
        cap_token = self._confirmation.issue(
            method="GET",
            path=cap_path,
            browser_session_id=browser_session_id,
            n_agent_session_id=n_agent_session_id,
            actor_id=actor_id,
        )
        # Build a capability URL. The container endpoint serves the
        # interactive view; the capability token authorizes a single session.
        container_endpoint = getattr(self._settings, "browser_container_endpoint", "")
        url = (
            f"{container_endpoint}/vnc/websockify"
            f"?session={browser_session_id}&cap={cap_token}"
        )
        from datetime import timedelta
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=self._takeover_view_ttl
        )
        return {
            "url": url,
            "expires_at": expires_at.isoformat(),
            "message": None,
        }

    # ------------------------------------------------------------------
    # Write challenge issuance (for GET session response)
    # ------------------------------------------------------------------

    async def _issue_write_challenges(
        self, session: BrowserSession, n_agent_session_id: str
    ) -> dict[str, str]:
        """Issue one-time challenge tokens for write operations that are valid
        for the current session status. Actor is not known at issue time, so
        challenges are bound to a sentinel actor that must be re-issued with
        the actual actor at write time.

        Actually, for security the challenge must bind the actor. So we only
        issue challenges when the actor is known. This method is called from
        get_session() where the actor is not yet known. Instead, the route
        layer issues challenges after authenticating the actor. We return an
        empty dict here; the route layer fills in challenges via
        confirmation_service.issue().
        """
        return {}


def _apply_cursor(actions: list[dict[str, Any]], cursor: str, limit: int) -> list[dict[str, Any]]:
    """Skip actions until the cursor id is found, then return the rest up to limit."""
    for i, action in enumerate(actions):
        if str(action.get("id", "")) == cursor:
            return actions[i + 1 : i + 1 + limit]
    # Cursor not found: return from the beginning.
    return actions[:limit]


def _session_to_dict(session: BrowserSession) -> dict[str, Any]:
    return {
        "id": session.id,
        "n_agent_session_id": session.bound_n_agent_session_id,
        "backend_type": session.backend_type.value,
        "status": session.status.value,
        "profile_ref": _short_hash(session.profile_ref),
        "document_revision": session.document_revision,
        "pre_takeover_status": (
            session.pre_takeover_status.value
            if session.pre_takeover_status is not None
            else None
        ),
        "created_at": (
            session.created_at.isoformat() if session.created_at else None
        ),
        "updated_at": (
            session.updated_at.isoformat() if session.updated_at else None
        ),
        "closed_at": session.closed_at.isoformat() if session.closed_at else None,
    }


def _state_to_dict(state: BrowserState) -> dict[str, Any]:
    return {
        "safe_url": state.safe_url,
        "title": state.title,
        "status": state.status.value,
        "document_revision": state.document_revision,
        "latest_screenshot_ref": state.latest_screenshot_ref,
        "last_action_at": (
            state.last_action_at.isoformat() if state.last_action_at else None
        ),
    }


__all__ = ["BrowserDashboardService"]

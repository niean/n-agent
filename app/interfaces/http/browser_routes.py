"""Browser Dashboard HTTP routes - 12 endpoints.

Mounted into create_dashboard_router via register_browser_routes(router,
dashboard_service, confirmation_service, actor_resolver, settings).

Security:
- Write endpoints require: same-origin, trusted actor context (actor_resolver),
  and one-time confirmation challenge (method/path/session/actor bound, short
  TTL, single consume). Challenge replay fails.
- For takeover/release the Dashboard service consumes the challenge (so the
  token is bound to the command semantics); for other write endpoints the
  route consumes the challenge.
- host-grant endpoints: only when settings.browser_trusted_dev is True; else 404.
- takeover-view: returns short-lived single-session interactive view URL
  (container); binds session/actor/TTL; Release/Close/expiry revokes; URL not
  written to model messages/logs/localStorage.
- screenshot response: Cache-Control: no-store.
- errors: map to stable codes; NEVER leak backend exception, paths, token,
  page text, or URL query in responses.

Interfaces never access SQLite/CDP/Playwright directly; only via
BrowserDashboardService/BrowserService.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse, Response

from app.application.browser_confirmation_service import BrowserConfirmationService
from app.application.browser_dashboard_service import BrowserDashboardService

logger = logging.getLogger(__name__)

ActorResolver = Callable[[Request], str | None]

# Write operations whose challenge is consumed by the Dashboard service
# (takeover/release) rather than the route. These endpoints pass the token to
# the service which performs the consume.
_SERVICE_CONSUMED_OPS = frozenset({"takeover", "release"})


def _error(status: int, code: str, message: str = "") -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message or code}},
    )


def _allowed_origins(settings) -> set[str]:
    base_url = getattr(settings, "dashboard_base_url", "") or ""
    origins: set[str] = set()
    if base_url:
        origins.add(base_url.rstrip("/"))
    origins.update(
        {
            "http://localhost:8201",
            "http://127.0.0.1:8201",
        }
    )
    return origins


def _check_same_origin(request: Request, settings) -> bool:
    """Verify the request is same-origin. Allows non-browser clients (no
    Origin header) for CLI/server-side tooling; rejects cross-origin browser
    requests that would carry cookies."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    allowed = _allowed_origins(settings)
    if origin in allowed:
        return True
    host = request.headers.get("host", "")
    if host and any(host in a.split("//", 1)[-1] for a in allowed if "://" in a):
        return True
    return False


def register_browser_routes(
    router: APIRouter,
    dashboard_service: BrowserDashboardService,
    confirmation_service: BrowserConfirmationService,
    actor_resolver: ActorResolver,
    settings,
) -> None:
    """Register the 12 Browser Dashboard endpoints on the given router."""

    def _auth_write(request: Request) -> tuple[str | None, JSONResponse | None]:
        if not _check_same_origin(request, settings):
            return None, _error(403, "browser_cross_origin_forbidden")
        actor = actor_resolver(request)
        if not actor:
            return None, _error(403, "browser_actor_required")
        return actor, None

    def _require_challenge_header(request: Request) -> tuple[str, JSONResponse | None]:
        token = request.headers.get("x-browser-challenge", "")
        if not token:
            return "", _error(403, "browser_challenge_required")
        return token, None

    def _route_consume_challenge(
        request: Request,
        actor: str,
        browser_session_id: str,
        n_agent_session_id: str,
        method: str,
        path_template: str,
    ) -> JSONResponse | None:
        token, err = _require_challenge_header(request)
        if err:
            return err
        path = path_template.format(id=browser_session_id)
        if not confirmation_service.consume(
            token,
            method,
            path,
            browser_session_id,
            n_agent_session_id,
            actor,
        ):
            return _error(403, "invalid_challenge")
        return None

    # ------------------------------------------------------------------
    # GET /chat/browser/sessions
    # ------------------------------------------------------------------

    @router.get("/chat/browser/sessions")
    async def list_sessions(
        request: Request,
        n_agent_session_id: str = Query(..., description="N-Agent session ID"),
    ):
        actor = actor_resolver(request)
        if not actor:
            return _error(403, "browser_actor_required")
        sessions = await dashboard_service.list_sessions(n_agent_session_id)
        return {"sessions": sessions}

    # ------------------------------------------------------------------
    # GET /chat/browser/sessions/{id}
    # ------------------------------------------------------------------

    @router.get("/chat/browser/sessions/{browser_session_id}")
    async def get_session(
        browser_session_id: str,
        request: Request,
        n_agent_session_id: str = Query(...),
    ):
        actor = actor_resolver(request)
        if not actor:
            return _error(403, "browser_actor_required")
        session_dict = await dashboard_service.get_session(
            browser_session_id, n_agent_session_id
        )
        if session_dict is None:
            return _error(404, "browser_session_not_found")
        # Issue write challenges for the authenticated actor.
        challenges = _issue_challenges_for_session(
            confirmation_service,
            browser_session_id,
            n_agent_session_id,
            actor,
            session_dict.get("status", ""),
            settings,
        )
        session_dict["write_challenges"] = challenges
        return session_dict

    # ------------------------------------------------------------------
    # GET /chat/browser/sessions/{id}/actions
    # ------------------------------------------------------------------

    @router.get("/chat/browser/sessions/{browser_session_id}/actions")
    async def list_actions(
        browser_session_id: str,
        request: Request,
        n_agent_session_id: str = Query(...),
        limit: int = Query(50, ge=1, le=200),
        cursor: str | None = Query(None),
    ):
        actor = actor_resolver(request)
        if not actor:
            return _error(403, "browser_actor_required")
        result = await dashboard_service.list_actions(
            browser_session_id, n_agent_session_id, limit=limit, cursor=cursor
        )
        if result is None:
            return _error(404, "browser_session_not_found")
        return result

    # ------------------------------------------------------------------
    # GET /chat/browser/sessions/{id}/screenshot
    # ------------------------------------------------------------------

    @router.get("/chat/browser/sessions/{browser_session_id}/screenshot")
    async def get_screenshot(
        browser_session_id: str,
        request: Request,
        n_agent_session_id: str = Query(...),
    ):
        actor = actor_resolver(request)
        if not actor:
            return _error(403, "browser_actor_required")
        result = await dashboard_service.read_screenshot(
            browser_session_id, n_agent_session_id
        )
        if result is None:
            return _error(404, "screenshot_unavailable")
        data, content_type = result
        return Response(
            content=data,
            media_type=content_type,
            headers={"Cache-Control": "no-store"},
        )

    # ------------------------------------------------------------------
    # POST /chat/browser/sessions/{id}/pause
    # ------------------------------------------------------------------

    @router.post("/chat/browser/sessions/{browser_session_id}/pause")
    async def pause_session(
        browser_session_id: str,
        request: Request,
        n_agent_session_id: str = Query(...),
    ):
        actor, err = _auth_write(request)
        if err:
            return err
        cerr = _route_consume_challenge(
            request, actor, browser_session_id, n_agent_session_id,
            "POST", "/chat/browser/sessions/{id}/pause",
        )
        if cerr:
            return cerr
        result = await dashboard_service.pause(browser_session_id, n_agent_session_id)
        return _command_response(result)

    # ------------------------------------------------------------------
    # POST /chat/browser/sessions/{id}/resume
    # ------------------------------------------------------------------

    @router.post("/chat/browser/sessions/{browser_session_id}/resume")
    async def resume_session(
        browser_session_id: str,
        request: Request,
        n_agent_session_id: str = Query(...),
    ):
        actor, err = _auth_write(request)
        if err:
            return err
        cerr = _route_consume_challenge(
            request, actor, browser_session_id, n_agent_session_id,
            "POST", "/chat/browser/sessions/{id}/resume",
        )
        if cerr:
            return cerr
        result = await dashboard_service.resume(browser_session_id, n_agent_session_id)
        return _command_response(result)

    # ------------------------------------------------------------------
    # POST /chat/browser/sessions/{id}/takeover
    # ------------------------------------------------------------------

    @router.post("/chat/browser/sessions/{browser_session_id}/takeover")
    async def takeover_session(
        browser_session_id: str,
        request: Request,
        n_agent_session_id: str = Query(...),
    ):
        actor, err = _auth_write(request)
        if err:
            return err
        token, cerr = _require_challenge_header(request)
        if cerr:
            return cerr
        # Service consumes the challenge (bound to takeover semantics).
        result = await dashboard_service.takeover(
            browser_session_id, n_agent_session_id, actor, token
        )
        return _command_response(result)

    # ------------------------------------------------------------------
    # POST /chat/browser/sessions/{id}/release
    # ------------------------------------------------------------------

    @router.post("/chat/browser/sessions/{browser_session_id}/release")
    async def release_session(
        browser_session_id: str,
        request: Request,
        n_agent_session_id: str = Query(...),
    ):
        actor, err = _auth_write(request)
        if err:
            return err
        token, cerr = _require_challenge_header(request)
        if cerr:
            return cerr
        # Service consumes the challenge (bound to release semantics).
        result = await dashboard_service.release(
            browser_session_id, n_agent_session_id, actor, token
        )
        return _command_response(result)

    # ------------------------------------------------------------------
    # POST /chat/browser/sessions/{id}/close
    # ------------------------------------------------------------------

    @router.post("/chat/browser/sessions/{browser_session_id}/close")
    async def close_session(
        browser_session_id: str,
        request: Request,
        n_agent_session_id: str = Query(...),
    ):
        actor, err = _auth_write(request)
        if err:
            return err
        cerr = _route_consume_challenge(
            request, actor, browser_session_id, n_agent_session_id,
            "POST", "/chat/browser/sessions/{id}/close",
        )
        if cerr:
            return cerr
        result = await dashboard_service.close(browser_session_id, n_agent_session_id)
        return _command_response(result)

    # ------------------------------------------------------------------
    # POST /chat/browser/sessions/{id}/host-grant (trusted-dev only)
    # ------------------------------------------------------------------

    if getattr(settings, "browser_trusted_dev", False):

        @router.post("/chat/browser/sessions/{browser_session_id}/host-grant")
        async def grant_host(
            browser_session_id: str,
            request: Request,
            n_agent_session_id: str = Query(...),
            payload: dict = Body(default_factory=dict),
        ):
            actor, err = _auth_write(request)
            if err:
                return err
            cerr = _route_consume_challenge(
                request, actor, browser_session_id, n_agent_session_id,
                "POST", "/chat/browser/sessions/{id}/host-grant",
            )
            if cerr:
                return cerr
            policy_version = str(payload.get("policy_version", "system-v1"))
            ttl_seconds = int(
                payload.get(
                    "ttl_seconds",
                    getattr(settings, "browser_host_grant_ttl_seconds", 300),
                )
            )
            result = await dashboard_service.grant_host(
                browser_session_id, n_agent_session_id, actor,
                policy_version, ttl_seconds,
            )
            return _command_response(result)

        @router.delete("/chat/browser/sessions/{browser_session_id}/host-grant")
        async def revoke_host(
            browser_session_id: str,
            request: Request,
            n_agent_session_id: str = Query(...),
        ):
            actor, err = _auth_write(request)
            if err:
                return err
            cerr = _route_consume_challenge(
                request, actor, browser_session_id, n_agent_session_id,
                "DELETE", "/chat/browser/sessions/{id}/host-grant",
            )
            if cerr:
                return cerr
            result = await dashboard_service.revoke_host(
                browser_session_id, n_agent_session_id
            )
            return _command_response(result)

    # ------------------------------------------------------------------
    # GET /chat/browser/sessions/{id}/takeover-view (container only)
    # ------------------------------------------------------------------

    @router.get("/chat/browser/sessions/{browser_session_id}/takeover-view")
    async def takeover_view(
        browser_session_id: str,
        request: Request,
        n_agent_session_id: str = Query(...),
    ):
        actor = actor_resolver(request)
        if not actor:
            return _error(403, "browser_actor_required")
        result = await dashboard_service.get_takeover_view(
            browser_session_id, n_agent_session_id, actor
        )
        if result is None:
            return _error(404, "browser_session_not_found")
        return JSONResponse(
            content=result,
            headers={"Cache-Control": "no-store"},
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _issue_challenges_for_session(
    confirmation: BrowserConfirmationService,
    browser_session_id: str,
    n_agent_session_id: str,
    actor: str,
    status: str,
    settings,
) -> dict[str, str]:
    """Issue one-time challenge tokens for write operations valid in the given
    session status. The actor is bound at issue time so tokens cannot be
    replayed by a different actor."""
    challenges: dict[str, str] = {}
    valid_ops = _valid_write_ops(status, getattr(settings, "browser_trusted_dev", False))
    for op in valid_ops:
        method, path_suffix = _op_method_path(op)
        path = f"/chat/browser/sessions/{browser_session_id}/{path_suffix}"
        token = confirmation.issue(
            method=method,
            path=path,
            browser_session_id=browser_session_id,
            n_agent_session_id=n_agent_session_id,
            actor_id=actor,
        )
        challenges[op] = token
    return challenges


def _valid_write_ops(status: str, trusted_dev: bool) -> list[str]:
    ops: list[str] = []
    if status == "pending_authorization":
        ops.append("close")
        if trusted_dev:
            ops.append("host_grant")
    elif status == "active":
        ops.extend(["pause", "takeover", "close"])
        if trusted_dev:
            ops.extend(["host_grant", "revoke_host"])
    elif status == "paused":
        ops.extend(["resume", "takeover", "close"])
    elif status == "takeover":
        ops.extend(["release", "close"])
    elif status == "degraded":
        ops.append("close")
    return ops


def _op_method_path(op: str) -> tuple[str, str]:
    mapping = {
        "pause": ("POST", "pause"),
        "resume": ("POST", "resume"),
        "takeover": ("POST", "takeover"),
        "release": ("POST", "release"),
        "close": ("POST", "close"),
        "host_grant": ("POST", "host-grant"),
        "revoke_host": ("DELETE", "host-grant"),
    }
    return mapping.get(op, ("POST", op))


def _command_response(result: dict[str, Any]) -> JSONResponse:
    if result.get("ok"):
        return JSONResponse(content=result)
    error = result.get("error", "browser_error")
    status_code = _error_status_code(error)
    return _error(status_code, error)


def _error_status_code(code: str) -> int:
    if code in ("browser_session_not_found", "screenshot_unavailable"):
        return 404
    if code in (
        "invalid_challenge",
        "browser_actor_required",
        "browser_cross_origin_forbidden",
        "host_grant_required",
        "host_grant_expired",
        "browser_disabled",
    ):
        return 403
    if code in (
        "invalid_state_transition",
        "browser_session_conflict",
        "takeover_in_progress",
    ):
        return 409
    return 400

"""HTTP routes for the Delegation subdomain (T13).

Route registration follows the ``register_*_routes`` pattern. Routes call
only Application services / the registry port; they never import SQLite or
app.infrastructure.

Security model:
  - The trusted actor is a server-side constant (``_DELEGATION_ACTOR``),
    NEVER derived from header, body, or query param.
  - ``scope_authorizer`` is a server-injected callable that decides whether
    the actor may view a given delegation. Client-supplied ``scope_id`` is
    only a filter, never an authorization fact.
  - Detail/events/cancel routes authorize BEFORE the existence check, and
    both not-found and unauthorized return 404 so existence is not leaked.
  - Cancel returns 202 (async cancel requested); it never claims the
    delegation is already stopped.

Error envelope (unified with task_routes):
    {"error": {"code": <str>, "message": <str>}}
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from app.domain.delegation import Delegation, DelegationStatus, MutationOutcome

# Server-side actor constant. NEVER derived from client input.
_DELEGATION_ACTOR = "dashboard"

# Whitelisted list filter keys (client cannot inject arbitrary filters).
_ALLOWED_LIST_FILTERS = {"scope_id", "status"}


ScopeAuthorizer = Callable[[Delegation], bool]


def _default_authorizer(_delegation: Delegation) -> bool:
    """Dashboard admin actor can see all delegations by default."""
    return True


def register_delegation_routes(
    app: FastAPI,
    *,
    delegation_service: Any | None,
    registry: Any,
    scope_authorizer: ScopeAuthorizer | None = None,
) -> None:
    """Register delegation HTTP routes on ``app``.

    ``delegation_service`` is optional (cancel/list do not require it; only
    the synchronous delegate path would, which is not exposed over HTTP --
    delegation is invoked via the tool executor). ``registry`` provides
    list/get/events/cancel. ``scope_authorizer`` gates per-delegation
    visibility (server-injected).
    """
    authorize = scope_authorizer or _default_authorizer

    @app.get("/chat/delegations")
    async def list_delegations(
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        scope_id: str | None = Query(default=None),
        status: str | None = Query(default=None),
    ):
        delegations = await registry.list_delegations(
            limit=limit, offset=offset, scope_id=scope_id, status=status,
        )
        # Re-authorize every row: client-supplied scope_id is a filter, not
        # an auth fact. Unauthorized rows are silently excluded.
        visible = [d for d in delegations if authorize(d)]
        return {"items": [_project_delegation(d) for d in visible]}

    @app.get("/chat/delegations/{delegation_id}")
    async def show_delegation(delegation_id: str):
        delegation = await registry.get(delegation_id)
        if delegation is None or not authorize(delegation):
            # 404 for both not-found and unauthorized (no existence leak).
            return _not_found()
        members = await registry.list_members(delegation_id)
        return _project_delegation_detail(delegation, members)

    @app.get("/chat/delegations/{delegation_id}/events")
    async def list_events(
        delegation_id: str,
        since: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ):
        delegation = await registry.get(delegation_id)
        if delegation is None or not authorize(delegation):
            return _not_found()
        events = await registry.list_events(delegation_id, since=since, limit=limit)
        return {
            "items": [
                {
                    "id": e.id,
                    "kind": e.kind,
                    "member_ordinal": e.member_ordinal,
                    "created_at": e.created_at,
                    "payload": dict(e.payload),
                }
                for e in events
            ]
        }

    @app.post("/chat/delegations/{delegation_id}/cancel")
    async def cancel_delegation(delegation_id: str):
        delegation = await registry.get(delegation_id)
        if delegation is None or not authorize(delegation):
            return _not_found()
        if delegation.is_terminal:
            return _error("delegation_already_terminal", 409)
        await registry.request_cancel(delegation_id, "user_cancel")
        # 202: async cancel requested. The delegation transitions to
        # CANCELLING and reaches CANCELLED on the next tick; we do not
        # claim it is already stopped.
        return JSONResponse(
            status_code=202,
            content={
                "delegation_id": delegation_id,
                "status": DelegationStatus.CANCELLING.value,
            },
        )


# ---------------------------------------------------------------------------
# Projections (parent-safe)
# ---------------------------------------------------------------------------


def _enum_value(x: Any) -> Any:
    """Return ``.value`` for enums, else the value itself (robust to
    direct-construction dataclasses that bypass ``.new()`` coercion)."""
    return x.value if hasattr(x, "value") else x


def _project_delegation(d: Delegation) -> dict[str, Any]:
    return {
        "id": d.id,
        "delegation_key": d.delegation_key,
        "status": _enum_value(d.status),
        "parent_source": d.parent.source,
        "parent_scope_id": d.parent.scope_id,
        "join_policy": _enum_value(d.join_policy),
        "aggregation": _enum_value(d.aggregation),
        "deadline_at": d.deadline_at,
        "partial": _enum_value(d.status) in (
            DelegationStatus.CANCELLED.value, DelegationStatus.EXPIRED.value,
        ),
        "created_at": d.created_at,
        "updated_at": d.updated_at,
    }


def _project_delegation_detail(d: Delegation, members: tuple[Any, ...]) -> dict[str, Any]:
    base = _project_delegation(d)
    base["members"] = [
        {
            "ordinal": m.ordinal,
            "role": _enum_value(m.role),
            "title": m.title,
            "status": _enum_value(m.status),
            # Internal session/lease fields are NOT projected.
        }
        for m in members
    ]
    return base


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------


def _not_found() -> JSONResponse:
    return _error("delegation_not_found", 404)


def _error(code: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code}},
    )

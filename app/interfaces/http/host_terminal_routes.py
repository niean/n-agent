"""Host terminal Dashboard routes - 3 read-only endpoints.

Mounted into create_dashboard_router via register_host_terminal_routes(router, service).
No write/refresh/delete endpoints; the page is pure read-only. Routes are always
registered so disabled / misconfigured / policy-load-failed instances surface a
stable 200 unavailable contract instead of a 404.
"""

from __future__ import annotations

from fastapi import APIRouter, Query


def register_host_terminal_routes(router: APIRouter, service) -> None:
    @router.get("/chat/host/status")
    async def host_status():
        return await service.get_status()

    @router.get("/chat/host/policy")
    async def host_policy():
        return await service.get_policy()

    @router.get("/chat/host/history")
    async def host_history(
        session_id: str | None = None,
        limit: int = Query(50, ge=1, le=100),
    ):
        return await service.list_history(session_id, limit)

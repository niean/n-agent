"""Host terminal Dashboard views and automatic script grant revocation.

Mounted into create_dashboard_router via register_host_terminal_routes(router, service).
Routes are always
registered so disabled / misconfigured / policy-load-failed instances surface a
stable 200 unavailable contract instead of a 404.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Path, Query
from fastapi.responses import JSONResponse


def register_host_terminal_routes(router: APIRouter, service) -> None:
    @router.get("/chat/host/script-grants")
    async def script_grants():
        try:
            return await service.list_script_grants()
        except Exception:
            raise HTTPException(503, "host_script_grants_unavailable") from None

    @router.post("/chat/host/script-grants/{revision}/revoke")
    async def revoke_script_grant(revision: int = Path(..., ge=1)):
        try:
            result = await service.revoke_script_grant(revision)
        except ValueError as exc:
            code = str(exc)
            if code == "host_script_grants_stale_revision":
                raise HTTPException(409, code) from None
            if code == "host_script_grants_disabled":
                raise HTTPException(503, code) from None
            raise HTTPException(503, "host_script_grants_unavailable") from None
        except Exception:
            raise HTTPException(503, "host_script_grants_unavailable") from None
        return JSONResponse(result, status_code=200 if result["success"] else 202)

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

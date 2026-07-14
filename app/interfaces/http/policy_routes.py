from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.application.policy_dashboard_service import PolicyDashboardService

logger = logging.getLogger(__name__)


def register_policy_routes(router: APIRouter, service: PolicyDashboardService) -> None:
    """Register the read-only Policy Dashboard API.

    Only ``GET /chat/policies`` is exposed. Success and failure both return a
    ``JSONResponse`` with ``Cache-Control: no-store`` so browsers never reuse a
    process-local Settings snapshot. Any profile/projection/serialization error
    is logged server-side and mapped to a fixed ``policy_load_failed`` 500 that
    never leaks the original exception text, config values or env info.
    """

    @router.get("/chat/policies")
    async def list_policies():
        try:
            data = service.list_policies()
        except Exception:
            logger.exception("policy profile could not be loaded")
            return JSONResponse(
                status_code=500,
                content={"error": {"code": "policy_load_failed",
                                   "message": "Policy profile could not be loaded"}},
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(content=data, headers={"Cache-Control": "no-store"})

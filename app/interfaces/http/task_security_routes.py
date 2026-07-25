from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.application.task_security_dashboard_service import TaskSecurityDashboardService

logger = logging.getLogger(__name__)


def register_task_security_routes(router: APIRouter, service: TaskSecurityDashboardService) -> None:
    """Register the read-only Task Security Dashboard API.

    Only ``GET /chat/tasks/security`` is exposed. Success and failure both return
    a ``JSONResponse`` with ``Cache-Control: no-store`` so browsers never reuse a
    process-local Settings snapshot. The service call and the success
    ``JSONResponse`` construction share the same ``try`` so projection,
    normalization and JSON serialization failures all map to the same fixed 500.
    Any error is logged server-side and mapped to a fixed
    ``task_security_load_failed`` 500 that never leaks the original exception
    text, config values, env info or paths.

    Must be registered BEFORE ``/chat/tasks/{task_id}`` so the literal path is
    not captured as a task id.
    """

    @router.get("/chat/tasks/security")
    async def list_task_security():
        try:
            data = service.list_task_security()
            return JSONResponse(content=data, headers={"Cache-Control": "no-store"})
        except Exception:
            logger.exception("task security profile could not be loaded")
            return JSONResponse(
                status_code=500,
                content={"error": {"code": "task_security_load_failed",
                                   "message": "Task security profile could not be loaded"}},
                headers={"Cache-Control": "no-store"},
            )

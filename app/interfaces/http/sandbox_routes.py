"""Sandbox Dashboard routes — 6 endpoints.

Mounted into create_dashboard_router via register_sandbox_routes(router, service).
Paths match spec-260630 line 486 and the frontend management-api.js helper names.
"""

from __future__ import annotations

from fastapi import APIRouter


def register_sandbox_routes(router: APIRouter, service) -> None:
    @router.get("/chat/sandbox/config")
    async def sandbox_config():
        return await service.get_config()

    @router.get("/chat/sandbox/active")
    async def sandbox_active():
        return await service.list_active_sandboxes()

    @router.get("/chat/sandbox/released")
    async def sandbox_released():
        return await service.list_released_sandboxes()

    @router.get("/chat/sandbox/execute-code-history")
    async def sandbox_history(session_id: str | None = None, limit: int = 50):
        return await service.list_execute_code_history(session_id, limit)

    @router.delete("/chat/sandbox/execute-code-history/{tool_call_id}")
    async def sandbox_history_delete(tool_call_id: str):
        return await service.delete_execute_code_history(tool_call_id)

    @router.post("/chat/sandbox/active/{session_id}/release")
    async def sandbox_release(session_id: str):
        return await service.release_sandbox(session_id)

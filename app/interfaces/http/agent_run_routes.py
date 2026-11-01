"""Execution history and explicit cancellation, alongside Session APIs."""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.application.agent_run_service import AgentRunService


def create_agent_run_router(service: AgentRunService) -> APIRouter:
    router = APIRouter()

    def public_run(row: dict) -> dict:
        return {key: value for key, value in row.items() if key not in {"owner_token", "process_id", "lease_expires_at"}}

    def error_response(status: int, code: str, message: str) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})

    def find_run(session_id: str, run_id: str) -> dict | None:
        row = service.get(run_id)
        if row is None or row["session_id"] != session_id:
            return None
        return row

    @router.get("/chat/sessions/{session_id}/runs")
    async def list_runs(session_id: str):
        return [public_run(row) for row in service.list_for_session(session_id)]

    @router.get("/chat/sessions/{session_id}/runs/{run_id}")
    async def get_run(session_id: str, run_id: str):
        found = find_run(session_id, run_id)
        if found is None:
            return error_response(404, "agent_run_not_found", "Agent run not found")
        row = public_run(found)
        row["trace_links"] = [{**link, "availability": "unknown"} for link in service.trace_links("agent_run", run_id)]
        return row

    @router.post("/chat/sessions/{session_id}/runs/{run_id}/cancel")
    async def cancel_run(session_id: str, run_id: str):
        if find_run(session_id, run_id) is None:
            return error_response(404, "agent_run_not_found", "Agent run not found")
        if not service.cancel(run_id):
            return error_response(409, "agent_run_not_locally_cancellable", "Agent run is not locally cancellable")
        return {"run_id": run_id, "cancel_requested": True}

    return router

import httpx
import pytest
from fastapi import FastAPI

from app.interfaces.http.agent_run_routes import create_agent_run_router


class Runs:
    row = {"id": "run", "session_id": "chat", "status": "running", "owner_token": "private", "process_id": "private", "lease_expires_at": 42}

    def get(self, run_id):
        return self.row if run_id == "run" else None

    def list_for_session(self, session_id):
        return [self.row] if session_id == "chat" else []

    def trace_links(self, *args):
        return [{"trace_id": "a" * 32}]

    def cancel(self, run_id):
        return run_id == "run"


@pytest.mark.asyncio
async def test_run_api_hides_ownership_and_checks_session_scope():
    app = FastAPI()
    app.include_router(create_agent_run_router(Runs()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/chat/sessions/chat/runs/run")
        assert response.status_code == 200
        assert "private" not in response.text
        assert "lease_expires_at" not in response.json()
        assert response.json()["trace_links"][0]["availability"] == "unknown"
        assert (await client.post("/chat/sessions/other/runs/run/cancel")).status_code == 404
        assert (await client.post("/chat/sessions/chat/runs/run/cancel")).json()["cancel_requested"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("GET", "/chat/sessions/chat/runs/missing"),
    ("GET", "/chat/sessions/other/runs/run"),
    ("POST", "/chat/sessions/other/runs/run/cancel"),
])
async def test_unknown_or_out_of_scope_run_uses_uniform_error(method, path):
    app = FastAPI()
    app.include_router(create_agent_run_router(Runs()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, path)
    assert response.status_code == 404
    assert response.json() == {"error": {
        "code": "agent_run_not_found", "message": "Agent run not found",
    }}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "completed"])
async def test_uncancellable_run_uses_uniform_conflict_error(status):
    class UncancellableRuns(Runs):
        row = {**Runs.row, "status": status}

        def cancel(self, run_id):
            return False

    app = FastAPI()
    app.include_router(create_agent_run_router(UncancellableRuns()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/chat/sessions/chat/runs/run/cancel")
    assert response.status_code == 409
    assert response.json() == {"error": {
        "code": "agent_run_not_locally_cancellable",
        "message": "Agent run is not locally cancellable",
    }}

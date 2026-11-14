import importlib.util

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_execution_routes_are_available_and_do_not_change_registration_api():
    assert importlib.util.find_spec("app.interfaces.http.evaluation_execution_routes"), "execution routes missing"
    from app.interfaces.http.evaluation_execution_routes import register_evaluation_execution_routes

    class Service:
        async def capabilities(self):
            return {"ready": True}

        async def submit(self, body):
            return {"execution_id": "one", "phase": "queued"}

        async def list(self, **kwargs):
            return {"items": [], "next_cursor": None}

        async def get(self, execution_id):
            return {"execution_id": execution_id}

    app = FastAPI()
    register_evaluation_execution_routes(app, service=Service())
    client = TestClient(app)
    for path in ("/capabilities", "", "/one"):
        response = client.get("/chat/evaluations/executions"+path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
    assert client.post("/chat/evaluations/executions", json={}).status_code == 202
    assert client.post("/chat/evaluations/experiments", json={}).status_code == 404
    assert client.get("/chat/evaluations/executions?limit=bad").status_code == 422


def test_execution_errors_are_controlled():
    assert importlib.util.find_spec("app.interfaces.http.evaluation_execution_routes"), "execution routes missing"
    from app.interfaces.http.evaluation_execution_routes import register_evaluation_execution_routes
    from app.domain.evaluation.execution import ExecutionNotReadyError, ExecutionCapacityError
    from app.domain.evaluation.errors import EvaluationConflictError
    from app.domain.evaluation.models import EvaluationValidationError
    error = ExecutionNotReadyError("judge_not_ready")

    class Service:
        async def submit(self, body):
            raise error

    app = FastAPI()
    register_evaluation_execution_routes(app, service=Service())
    client = TestClient(app)
    for exc, status in ((error,503),(ExecutionCapacityError(),503),(EvaluationConflictError(),409),(EvaluationValidationError(),422)):
        error = exc
        response = client.post("/chat/evaluations/executions", json={})
        assert response.status_code == status
        assert response.headers["cache-control"] == "no-store"

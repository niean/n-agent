"""T5：预检路由合同（只读、无副作用、blocked 语义、未接线 503）。"""
from __future__ import annotations

import importlib.util

from fastapi import FastAPI
from fastapi.testclient import TestClient


class FakePreflight:
    def __init__(self, report=None, error=None):
        self.calls = []
        self._report = report or {"runnable": True, "blocked": []}
        self._error = error

    async def preflight(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._report


def _client(preflight):
    from app.interfaces.http.evaluation_execution_routes import (
        register_evaluation_execution_routes,
    )

    class Service:
        async def capabilities(self):
            return {"ready": True}

    app = FastAPI()
    register_evaluation_execution_routes(app, service=Service(), preflight=preflight)
    return TestClient(app)


def _body(**overrides):
    body = {
        "dataset_id": "agent-e2e-scenarios",
        "dataset_version": "1",
        "dataset_hash": "c" * 64,
        "evaluator_set": [
            {"definition_id": "agent.scenario_quality",
             "definition_version": "experiment-v1"},
        ],
        "replicates": 1,
    }
    body.update(overrides)
    return body


def test_preflight_route_readonly_returns_report():
    fake = FakePreflight(report={
        "runnable": True, "blocked": [], "cases": [],
        "selected_case_ids": ["case-00"], "item_count": 1,
        "max_replicates": 100, "judge_calls": 1,
        "total_budget_seconds": 1800.0,
    })
    client = _client(fake)
    response = client.post("/chat/evaluations/executions/preflight", json=_body())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["runnable"] is True
    assert payload["item_count"] == 1
    assert payload["total_budget_seconds"] == 1800.0
    # 只读：请求体原样转发，无写入语义
    assert fake.calls[0]["dataset_id"] == "agent-e2e-scenarios"


def test_preflight_route_blocked_report_passthrough():
    fake = FakePreflight(report={
        "runnable": False,
        "blocked": ["场景通道未接入执行适配: cli"],
        "cases": [],
        "selected_case_ids": [],
        "item_count": 0,
        "max_replicates": 0,
        "judge_calls": 0,
        "total_budget_seconds": 1800.0,
    })
    client = _client(fake)
    response = client.post("/chat/evaluations/executions/preflight", json=_body())
    assert response.status_code == 200
    assert response.json()["runnable"] is False
    assert "cli" in response.json()["blocked"][0]


def test_preflight_route_unwired_returns_503():
    from app.interfaces.http.evaluation_execution_routes import (
        register_evaluation_execution_routes,
    )

    class Service:
        async def capabilities(self):
            return {"ready": True}

    app = FastAPI()
    register_evaluation_execution_routes(app, service=Service(), preflight=None)
    client = TestClient(app)
    response = client.post("/chat/evaluations/executions/preflight", json=_body())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "preflight_unavailable"


def test_preflight_route_validation_error_422():
    from app.domain.evaluation.models import EvaluationValidationError

    client = _client(FakePreflight(error=EvaluationValidationError("bad")))
    response = client.post("/chat/evaluations/executions/preflight", json=_body())
    assert response.status_code == 422


def test_preflight_route_not_found_404():
    from app.domain.evaluation.errors import EvaluationNotFoundError

    client = _client(FakePreflight(error=EvaluationNotFoundError("missing")))
    response = client.post("/chat/evaluations/executions/preflight", json=_body())
    assert response.status_code == 404

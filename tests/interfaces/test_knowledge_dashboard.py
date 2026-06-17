from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.knowledge_service import KnowledgeBaseCreateInput, KnowledgeBaseUpdateInput, KnowledgeProbeInput
from app.domain.knowledge import (
    DuplicateKnowledgeBaseError,
    KnowledgeBase,
    KnowledgeBaseNotFoundError,
    KnowledgeBaseType,
    KnowledgeBaseValidationError,
    KnowledgeProbeError,
    KnowledgeProbeStatus,
)
from app.domain.tool import ToolDefinition
from app.interfaces.http.dashboard import create_dashboard_router


def _base(kb_id: str = "kb-1", *, api_key_present: bool = False) -> KnowledgeBase:
    now = datetime.now(timezone.utc)
    return KnowledgeBase(
        id=kb_id,
        name="Docs",
        description="Project docs",
        base_type=KnowledgeBaseType.N_KB,
        base_url="https://kb.example.com",
        dataset_id="dataset-1",
        api_key_present=api_key_present,
        enabled=True,
        default_top_k=5,
        default_min_score=0.2,
        last_probe_status=KnowledgeProbeStatus.UNKNOWN,
        last_probe_error=None,
        last_probed_at=None,
        created_at=now,
        updated_at=now,
    )


class FakeKnowledgeService:
    def __init__(self):
        self.items = {"kb-1": _base("kb-1")}
        self.created_inputs: list[KnowledgeBaseCreateInput] = []
        self.updated_inputs: list[tuple[str, KnowledgeBaseUpdateInput]] = []
        self.deleted: list[str] = []
        self.unsaved_probes: list[KnowledgeProbeInput] = []
        self.saved_probes: list[str] = []
        self.error: Exception | None = None

    async def list_bases(self):
        return list(self.items.values())

    async def get_base(self, kb_id: str):
        if self.error:
            raise self.error
        if kb_id not in self.items:
            raise KnowledgeBaseNotFoundError(kb_id)
        return self.items[kb_id]

    async def create_base(self, payload: KnowledgeBaseCreateInput):
        if self.error:
            raise self.error
        self.created_inputs.append(payload)
        base = _base(payload.id, api_key_present=bool(payload.api_key))
        self.items[base.id] = base
        return base

    async def update_base(self, kb_id: str, payload: KnowledgeBaseUpdateInput):
        if self.error:
            raise self.error
        if kb_id not in self.items:
            raise KnowledgeBaseNotFoundError(kb_id)
        self.updated_inputs.append((kb_id, payload))
        base = self.items[kb_id]
        updated = KnowledgeBase(
            **{
                **base.__dict__,
                "name": payload.name if payload.name is not None else base.name,
                "api_key_present": base.api_key_present if payload.api_key is None else bool(payload.api_key),
                "enabled": payload.enabled if payload.enabled is not None else base.enabled,
            }
        )
        self.items[kb_id] = updated
        return updated

    async def delete_base(self, kb_id: str):
        if self.error:
            raise self.error
        if kb_id not in self.items:
            raise KnowledgeBaseNotFoundError(kb_id)
        self.deleted.append(kb_id)
        self.items.pop(kb_id)

    async def probe_unsaved(self, payload: KnowledgeProbeInput):
        if self.error:
            raise self.error
        self.unsaved_probes.append(payload)

    async def probe_base(self, kb_id: str):
        if self.error:
            raise self.error
        if kb_id not in self.items:
            raise KnowledgeBaseNotFoundError(kb_id)
        self.saved_probes.append(kb_id)

    async def knowledge_tool_definition(self):
        ids = ",".join(sorted(self.items))
        return ToolDefinition(name="search_knowledge", description=f"enabled: {ids}", input_schema={"type": "object"})


class FakeToolService:
    def __init__(self):
        self.definitions = {"search_knowledge": ToolDefinition(name="search_knowledge", description="", input_schema={"type": "object"})}

    def list_definitions(self):
        return list(self.definitions.values())


class FakeSessionService:
    pass


class FakeModelService:
    default_model = "model"

    async def list_models(self):
        return []


def _client(service: FakeKnowledgeService, tool_service: FakeToolService | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_dashboard_router(
            FakeSessionService(),
            tool_service or FakeToolService(),
            FakeModelService(),
            lambda: {},
            knowledge_service=service,
        )
    )
    return TestClient(app)


def test_knowledge_bases_crud_routes_mask_api_key():
    service = FakeKnowledgeService()
    client = _client(service)

    listed = client.get("/chat/knowledge/bases")
    detail = client.get("/chat/knowledge/bases/kb-1")
    created = client.post(
        "/chat/knowledge/bases",
        json={
            "id": "kb-2",
            "name": "Docs 2",
            "description": "Docs 2",
            "base_type": "ragflow",
            "base_url": "https://rag.example.com",
            "dataset_id": "dataset-2",
            "api_key": "secret",
            "enabled": True,
            "default_top_k": 3,
            "default_min_score": 0.5,
        },
    )
    patched = client.patch("/chat/knowledge/bases/kb-2", json={"name": "Renamed"})
    deleted = client.delete("/chat/knowledge/bases/kb-2")

    assert listed.status_code == 200
    assert listed.json()[0]["id"] == "kb-1"
    assert detail.json()["id"] == "kb-1"
    assert created.status_code == 200
    assert created.json()["api_key_present"] is True
    assert "api_key" not in created.json()
    assert service.created_inputs[-1].base_type is KnowledgeBaseType.RAGFLOW
    assert patched.json()["name"] == "Renamed"
    assert service.updated_inputs[-1][1].api_key is None
    assert deleted.status_code == 204


def test_knowledge_update_preserves_empty_and_new_api_key_semantics():
    service = FakeKnowledgeService()
    client = _client(service)

    client.patch("/chat/knowledge/bases/kb-1", json={"api_key": ""})
    client.patch("/chat/knowledge/bases/kb-1", json={"api_key": "new"})

    assert service.updated_inputs[0][1].api_key == ""
    assert service.updated_inputs[1][1].api_key == "new"


def test_knowledge_tool_refresh_endpoint_updates_tool_definition():
    service = FakeKnowledgeService()
    tool_service = FakeToolService()
    client = _client(service, tool_service)

    response = client.post("/chat/knowledge/tools/refresh")

    assert response.status_code == 200
    assert response.json()["description"] == "enabled: kb-1"
    assert tool_service.definitions["search_knowledge"].description == "enabled: kb-1"


def test_knowledge_crud_routes_refresh_tool_definition():
    service = FakeKnowledgeService()
    tool_service = FakeToolService()
    client = _client(service, tool_service)

    created = client.post(
        "/chat/knowledge/bases",
        json={"id": "kb-2", "name": "Docs", "description": "Docs", "base_type": "n_kb", "base_url": "https://kb.example.com", "dataset_id": "dataset-1"},
    )
    assert created.status_code == 200
    assert "kb-2" in tool_service.definitions["search_knowledge"].description

    patched = client.patch("/chat/knowledge/bases/kb-2", json={"enabled": False})
    assert patched.status_code == 200
    assert "kb-2" in tool_service.definitions["search_knowledge"].description

    deleted = client.delete("/chat/knowledge/bases/kb-2")
    assert deleted.status_code == 204
    assert tool_service.definitions["search_knowledge"].description == "enabled: kb-1"


def test_knowledge_probe_routes_call_unsaved_and_saved_probe():
    service = FakeKnowledgeService()
    client = _client(service)

    unsaved = client.post(
        "/chat/knowledge/bases/probe",
        json={
            "name": "Probe",
            "description": "Probe",
            "base_type": "n_kb",
            "base_url": "https://kb.example.com",
            "dataset_id": "dataset-1",
            "api_key": "secret",
        },
    )
    saved = client.post("/chat/knowledge/bases/kb-1/probe")

    assert unsaved.status_code == 200
    assert saved.status_code == 200
    assert service.unsaved_probes[-1].api_key == "secret"
    assert service.saved_probes == ["kb-1"]


def test_knowledge_errors_are_mapped():
    cases = [
        (KnowledgeBaseNotFoundError("missing"), 404, "knowledge_base_not_found"),
        (KnowledgeBaseValidationError("bad"), 422, "knowledge_base_invalid"),
        (DuplicateKnowledgeBaseError("dup"), 409, "knowledge_base_duplicate"),
        (KnowledgeProbeError("probe"), 502, "knowledge_probe_failed"),
    ]

    for exc, status, code in cases:
        service = FakeKnowledgeService()
        service.error = exc
        response = _client(service).post(
            "/chat/knowledge/bases",
            json={"id": "kb-2", "name": "Docs", "description": "Docs", "base_type": "n_kb", "base_url": "https://kb.example.com", "dataset_id": "dataset-1"},
        )
        assert response.status_code == status
        assert response.json()["error"]["code"] == code

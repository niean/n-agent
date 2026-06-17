from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.application.knowledge_service import (
    KnowledgeBaseCreateInput,
    KnowledgeBaseUpdateInput,
    KnowledgeProbeInput,
    KnowledgeService,
    KnowledgeToolExecutor,
)
from app.domain.knowledge import (
    DuplicateKnowledgeBaseError,
    KnowledgeBackendSearchRequest,
    KnowledgeBase,
    KnowledgeBaseNotFoundError,
    KnowledgeBaseSecret,
    KnowledgeBaseType,
    KnowledgeBaseValidationError,
    KnowledgeProbeError,
    KnowledgeProbeStatus,
    KnowledgeSearchError,
    KnowledgeSearchResult,
    KnowledgeSnippet,
)
from app.domain.tool import ToolCallRequest, ToolResultStatus, ToolSourceType


def _base(
    *,
    kb_id: str = "kb-1",
    name: str = "Docs",
    description: str = "Engineering docs",
    base_type: KnowledgeBaseType = KnowledgeBaseType.N_KB,
    enabled: bool = True,
    default_top_k: int | None = 5,
    default_min_score: float | None = 0.2,
) -> KnowledgeBase:
    now = datetime.now(timezone.utc)
    return KnowledgeBase(
        id=kb_id,
        name=name,
        description=description,
        base_type=base_type,
        base_url="https://kb.example.com",
        dataset_id="dataset-1",
        api_key_present=False,
        enabled=enabled,
        default_top_k=default_top_k,
        default_min_score=default_min_score,
        last_probe_status=KnowledgeProbeStatus.UNKNOWN,
        last_probe_error=None,
        last_probed_at=None,
        created_at=now,
        updated_at=now,
    )


class FakeRegistry:
    def __init__(self):
        self.items: dict[str, KnowledgeBase] = {}
        self.secrets: dict[str, str | None] = {}
        self.secret_reads: list[str] = []
        self.probe_updates: list[tuple[str, KnowledgeProbeStatus, str | None]] = []

    async def list_bases(self) -> list[KnowledgeBase]:
        return list(self.items.values())

    async def get_base(self, kb_id: str) -> KnowledgeBase | None:
        return self.items.get(kb_id)

    async def create_base(self, base: KnowledgeBase, api_key: str | None = None) -> KnowledgeBase:
        if base.id in self.items or any(item.name == base.name for item in self.items.values()):
            raise DuplicateKnowledgeBaseError(base.id)
        created = KnowledgeBase(**{**base.__dict__, "api_key_present": bool(api_key)})
        self.items[created.id] = created
        self.secrets[created.id] = api_key or None
        return created

    async def update_base(self, kb_id: str, **kwargs) -> KnowledgeBase:
        if kb_id not in self.items:
            raise KnowledgeBaseNotFoundError(kb_id)
        clear_api_key = kwargs.pop("clear_api_key", False)
        api_key = kwargs.pop("api_key", None)
        clear_default_top_k = kwargs.pop("clear_default_top_k", False)
        clear_default_min_score = kwargs.pop("clear_default_min_score", False)
        if clear_api_key:
            self.secrets[kb_id] = None
        elif api_key is not None:
            self.secrets[kb_id] = api_key
        existing = self.items[kb_id]
        merged = {**existing.__dict__}
        for key, value in kwargs.items():
            if value is not None:
                merged[key] = value
        if clear_default_top_k:
            merged["default_top_k"] = None
        if clear_default_min_score:
            merged["default_min_score"] = None
        merged["api_key_present"] = bool(self.secrets.get(kb_id))
        merged["updated_at"] = datetime.now(timezone.utc)
        updated = KnowledgeBase(**merged)
        self.items[kb_id] = updated
        return updated

    async def delete_base(self, kb_id: str) -> None:
        if kb_id not in self.items:
            raise KnowledgeBaseNotFoundError(kb_id)
        self.items.pop(kb_id)
        self.secrets.pop(kb_id, None)

    async def get_secret(self, kb_id: str) -> str | None:
        if kb_id not in self.items:
            raise KnowledgeBaseNotFoundError(kb_id)
        self.secret_reads.append(kb_id)
        return self.secrets.get(kb_id)

    async def update_probe_status(
        self,
        kb_id: str,
        status: KnowledgeProbeStatus,
        error: str | None = None,
        probed_at=None,
    ) -> None:
        if kb_id not in self.items:
            raise KnowledgeBaseNotFoundError(kb_id)
        self.probe_updates.append((kb_id, status, error))
        self.items[kb_id] = KnowledgeBase(
            **{
                **self.items[kb_id].__dict__,
                "last_probe_status": status,
                "last_probe_error": error,
                "last_probed_at": probed_at or datetime.now(timezone.utc),
            }
        )


class FakeRetriever:
    def __init__(self):
        self.searches: list[tuple[KnowledgeBase, KnowledgeBackendSearchRequest, KnowledgeBaseSecret | None]] = []
        self.probes: list[tuple[KnowledgeBase, KnowledgeBaseSecret | None]] = []
        self.fail_probe = False
        self.fail_search = False

    async def probe(self, base: KnowledgeBase, secret: KnowledgeBaseSecret | None = None) -> None:
        self.probes.append((base, secret))
        if self.fail_probe:
            raise KnowledgeProbeError("probe failed")

    async def search(
        self,
        base: KnowledgeBase,
        request: KnowledgeBackendSearchRequest,
        secret: KnowledgeBaseSecret | None = None,
    ) -> KnowledgeSearchResult:
        self.searches.append((base, request, secret))
        if self.fail_search:
            raise KnowledgeSearchError("search failed")
        return KnowledgeSearchResult(
            kb_id=base.id,
            kb_name=base.name,
            base_type=base.base_type,
            query=request.query,
            results=[KnowledgeSnippet(id="chunk-1", title="Title", content="Content", score=0.9, source="doc")],
        )


class FakeFactory:
    def __init__(self):
        self.retrievers = {
            KnowledgeBaseType.N_KB: FakeRetriever(),
            KnowledgeBaseType.RAGFLOW: FakeRetriever(),
        }

    def get(self, base_type: KnowledgeBaseType) -> FakeRetriever:
        return self.retrievers[base_type]


def _service():
    registry = FakeRegistry()
    factory = FakeFactory()
    return KnowledgeService(registry, factory), registry, factory


@pytest.mark.asyncio
async def test_create_base_validates_slug_and_persists_without_echoing_key():
    service, registry, _ = _service()

    created = await service.create_base(
        KnowledgeBaseCreateInput(
            id="engineering-docs_1",
            name="Engineering Docs",
            description="Internal engineering docs",
            base_type=KnowledgeBaseType.N_KB,
            base_url="https://kb.example.com/",
            dataset_id="dataset-1",
            api_key="secret",
        )
    )

    assert created.id == "engineering-docs_1"
    assert created.api_key_present is True
    assert registry.secrets[created.id] == "secret"
    assert not hasattr(created, "api_key")


@pytest.mark.asyncio
async def test_create_base_rejects_invalid_slug():
    service, _, _ = _service()

    with pytest.raises(KnowledgeBaseValidationError):
        await service.create_base(
            KnowledgeBaseCreateInput(
                id="Bad ID",
                name="Docs",
                description="Docs",
                base_type=KnowledgeBaseType.N_KB,
                base_url="https://kb.example.com",
                dataset_id="dataset-1",
            )
        )


@pytest.mark.asyncio
async def test_update_base_cannot_change_id_and_supports_key_three_states():
    service, registry, _ = _service()
    created = await service.create_base(
        KnowledgeBaseCreateInput(
            id="kb-1",
            name="Docs",
            description="Docs",
            base_type=KnowledgeBaseType.N_KB,
            base_url="https://kb.example.com",
            dataset_id="dataset-1",
            api_key="orig",
        )
    )

    kept = await service.update_base(created.id, KnowledgeBaseUpdateInput(name="Docs 2", api_key=None))
    assert kept.name == "Docs 2"
    assert registry.secrets[created.id] == "orig"

    replaced = await service.update_base(created.id, KnowledgeBaseUpdateInput(api_key="new"))
    assert replaced.api_key_present is True
    assert registry.secrets[created.id] == "new"

    cleared = await service.update_base(created.id, KnowledgeBaseUpdateInput(api_key=""))
    assert cleared.api_key_present is False
    assert registry.secrets[created.id] is None

    assert "id" not in KnowledgeBaseUpdateInput.__dataclass_fields__


@pytest.mark.asyncio
async def test_knowledge_tool_definition_requires_kb_id_and_describes_enabled_bases_only():
    service, registry, _ = _service()
    registry.items["enabled"] = _base(kb_id="enabled", name="Enabled", description="Use for enabled docs")
    registry.items["disabled"] = _base(kb_id="disabled", name="Disabled", description="Hidden", enabled=False)

    definition = await service.knowledge_tool_definition()

    assert definition.name == "search_knowledge"
    assert definition.source_type is ToolSourceType.KNOWLEDGE
    assert definition.toolset == "knowledge"
    assert definition.input_schema["required"] == ["kb_id", "query"]
    assert "most relevant enabled knowledge base first" in definition.description
    assert "Do not query multiple knowledge bases initially" in definition.description
    assert "If the first search returns no useful results" in definition.description
    assert "Stop searching once enough relevant evidence is found" in definition.description
    assert "enabled" in definition.description
    assert "Use for enabled docs" in definition.description
    assert "disabled" not in definition.description
    assert definition.enabled is True


@pytest.mark.asyncio
async def test_knowledge_tool_definition_disabled_when_no_enabled_bases():
    service, registry, _ = _service()
    registry.items["disabled"] = _base(kb_id="disabled", enabled=False)

    definition = await service.knowledge_tool_definition()

    assert definition.enabled is False


@pytest.mark.asyncio
async def test_search_requires_kb_id_enabled_base_and_reads_secret_only_for_search():
    service, registry, factory = _service()
    registry.items["kb-1"] = _base(kb_id="kb-1", default_top_k=7, default_min_score=0.4)
    registry.secrets["kb-1"] = "secret"

    bases = await service.list_bases()
    result = await service.search(kb_id="kb-1", query="python")

    retriever = factory.retrievers[KnowledgeBaseType.N_KB]
    assert bases == [registry.items["kb-1"]]
    assert registry.secret_reads == ["kb-1"]
    assert result.kb_id == "kb-1"
    assert retriever.searches[0][1] == KnowledgeBackendSearchRequest(query="python", top_k=7, min_score=0.4)
    assert retriever.searches[0][2] == KnowledgeBaseSecret(kb_id="kb-1", api_key="secret")


@pytest.mark.asyncio
async def test_search_rejects_missing_or_disabled_base():
    service, registry, _ = _service()
    registry.items["disabled"] = _base(kb_id="disabled", enabled=False)

    with pytest.raises(KnowledgeBaseValidationError):
        await service.search(kb_id="", query="q")
    with pytest.raises(KnowledgeBaseNotFoundError):
        await service.search(kb_id="missing", query="q")
    with pytest.raises(KnowledgeSearchError):
        await service.search(kb_id="disabled", query="q")


@pytest.mark.asyncio
async def test_probe_unsaved_and_saved_use_secret_and_update_status():
    service, registry, factory = _service()
    registry.items["kb-1"] = _base(kb_id="kb-1", base_type=KnowledgeBaseType.RAGFLOW)
    registry.secrets["kb-1"] = "saved"

    await service.probe_unsaved(
        KnowledgeProbeInput(
            name="Unsaved",
            description="Unsaved",
            base_type=KnowledgeBaseType.N_KB,
            base_url="https://kb.example.com",
            dataset_id="dataset-1",
            api_key="unsaved",
        )
    )
    await service.probe_base("kb-1")

    assert factory.retrievers[KnowledgeBaseType.N_KB].probes[0][1] == KnowledgeBaseSecret(kb_id="", api_key="unsaved")
    assert factory.retrievers[KnowledgeBaseType.RAGFLOW].probes[0][1] == KnowledgeBaseSecret(kb_id="kb-1", api_key="saved")
    assert registry.probe_updates[-1][1] is KnowledgeProbeStatus.SUCCESS


@pytest.mark.asyncio
async def test_probe_base_records_failure_status():
    service, registry, factory = _service()
    registry.items["kb-1"] = _base(kb_id="kb-1")
    factory.retrievers[KnowledgeBaseType.N_KB].fail_probe = True

    with pytest.raises(KnowledgeProbeError):
        await service.probe_base("kb-1")

    assert registry.probe_updates[-1][1] is KnowledgeProbeStatus.FAILED
    assert "probe failed" in (registry.probe_updates[-1][2] or "")


@pytest.mark.asyncio
async def test_tool_executor_success_and_validation_errors():
    service, registry, _ = _service()
    registry.items["kb-1"] = _base(kb_id="kb-1")
    executor = KnowledgeToolExecutor(service)

    result = await executor.execute(
        ToolCallRequest(id="call-1", name="search_knowledge", arguments={"kb_id": "kb-1", "query": "q", "top_k": 99, "min_score": -1})
    )
    missing = await executor.execute(ToolCallRequest(id="call-2", name="search_knowledge", arguments={"query": "q"}))
    unknown = await executor.execute(ToolCallRequest(id="call-3", name="other", arguments={}))

    assert result.status is ToolResultStatus.SUCCESS
    assert result.content["kb_id"] == "kb-1"
    assert result.content["backend_type"] == "n_kb"
    assert result.content["results"][0]["content"] == "Content"
    assert missing.status is ToolResultStatus.ERROR
    assert "kb_id" in missing.content["error"]
    assert unknown.status is ToolResultStatus.ERROR

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.domain.knowledge import (
    KnowledgeBackendSearchRequest,
    KnowledgeBase,
    KnowledgeBaseNotFoundError,
    KnowledgeBaseRegistry,
    KnowledgeBaseSecret,
    KnowledgeBaseType,
    KnowledgeBaseValidationError,
    KnowledgeProbeError,
    KnowledgeProbeStatus,
    KnowledgeRetrieverFactory,
    KnowledgeSearchError,
    KnowledgeSearchResult,
    KnowledgeSnippet,
    validate_kb_id,
)
from app.domain.tool import (
    ToolCallRequest,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutor,
    ToolResult,
    ToolResultStatus,
    ToolSourceType,
)


@dataclass(frozen=True)
class KnowledgeBaseCreateInput:
    id: str
    name: str
    description: str
    base_type: KnowledgeBaseType
    base_url: str
    dataset_id: str
    api_key: str | None = None
    enabled: bool = True
    default_top_k: int | None = None
    default_min_score: float | None = None


@dataclass(frozen=True)
class KnowledgeBaseUpdateInput:
    name: str | None = None
    description: str | None = None
    base_type: KnowledgeBaseType | None = None
    base_url: str | None = None
    dataset_id: str | None = None
    api_key: str | None = None
    enabled: bool | None = None
    default_top_k: int | None = None
    default_min_score: float | None = None
    clear_default_top_k: bool = False
    clear_default_min_score: bool = False


@dataclass(frozen=True)
class KnowledgeProbeInput:
    name: str
    description: str
    base_type: KnowledgeBaseType
    base_url: str
    dataset_id: str
    api_key: str | None = None
    default_top_k: int | None = None
    default_min_score: float | None = None


class KnowledgeService:
    def __init__(self, registry: KnowledgeBaseRegistry, retriever_factory: KnowledgeRetrieverFactory):
        self.registry = registry
        self.retriever_factory = retriever_factory

    async def list_bases(self) -> list[KnowledgeBase]:
        return await self.registry.list_bases()

    async def get_base(self, kb_id: str) -> KnowledgeBase:
        base = await self.registry.get_base(kb_id)
        if base is None:
            raise KnowledgeBaseNotFoundError(kb_id)
        return base

    async def create_base(self, payload: KnowledgeBaseCreateInput) -> KnowledgeBase:
        kb_id = validate_kb_id(payload.id.strip())
        self._validate(payload.name, payload.base_url, payload.default_top_k, payload.default_min_score)
        now = datetime.now(timezone.utc)
        base = KnowledgeBase(
            id=kb_id,
            name=payload.name.strip(),
            description=payload.description.strip(),
            base_type=payload.base_type,
            base_url=payload.base_url.strip(),
            dataset_id=payload.dataset_id.strip(),
            api_key_present=False,
            enabled=payload.enabled,
            default_top_k=payload.default_top_k,
            default_min_score=payload.default_min_score,
            last_probe_status=KnowledgeProbeStatus.UNKNOWN,
            last_probe_error=None,
            last_probed_at=None,
            created_at=now,
            updated_at=now,
        )
        return await self.registry.create_base(base, payload.api_key)

    async def update_base(self, kb_id: str, patch: KnowledgeBaseUpdateInput) -> KnowledgeBase:
        existing = await self.get_base(kb_id)
        name = patch.name if patch.name is not None else existing.name
        base_url = patch.base_url if patch.base_url is not None else existing.base_url
        default_top_k = None if patch.clear_default_top_k else (
            patch.default_top_k if patch.default_top_k is not None else existing.default_top_k
        )
        default_min_score = None if patch.clear_default_min_score else (
            patch.default_min_score if patch.default_min_score is not None else existing.default_min_score
        )
        self._validate(name, base_url, default_top_k, default_min_score)
        clear_key = patch.api_key == ""
        api_key = patch.api_key if patch.api_key not in (None, "") else None
        return await self.registry.update_base(
            kb_id,
            name=patch.name.strip() if patch.name is not None else None,
            description=patch.description.strip() if patch.description is not None else None,
            base_type=patch.base_type,
            base_url=patch.base_url.strip() if patch.base_url is not None else None,
            dataset_id=patch.dataset_id.strip() if patch.dataset_id is not None else None,
            enabled=patch.enabled,
            default_top_k=patch.default_top_k,
            default_min_score=patch.default_min_score,
            clear_default_top_k=patch.clear_default_top_k,
            clear_default_min_score=patch.clear_default_min_score,
            api_key=api_key,
            clear_api_key=clear_key,
        )

    async def delete_base(self, kb_id: str) -> None:
        await self.get_base(kb_id)
        await self.registry.delete_base(kb_id)

    async def probe_unsaved(self, payload: KnowledgeProbeInput) -> None:
        self._validate(payload.name, payload.base_url, payload.default_top_k, payload.default_min_score)
        now = datetime.now(timezone.utc)
        base = KnowledgeBase(
            id="",
            name=payload.name.strip(),
            description=payload.description.strip(),
            base_type=payload.base_type,
            base_url=payload.base_url.strip(),
            dataset_id=payload.dataset_id.strip(),
            api_key_present=bool(payload.api_key),
            enabled=True,
            default_top_k=payload.default_top_k,
            default_min_score=payload.default_min_score,
            last_probe_status=KnowledgeProbeStatus.UNKNOWN,
            last_probe_error=None,
            last_probed_at=None,
            created_at=now,
            updated_at=now,
        )
        await self.retriever_factory.get(base.base_type).probe(base, _secret(base.id, payload.api_key))

    async def probe_base(self, kb_id: str) -> None:
        base = await self.get_base(kb_id)
        secret = _secret(kb_id, await self.registry.get_secret(kb_id))
        try:
            await self.retriever_factory.get(base.base_type).probe(base, secret)
        except KnowledgeProbeError as exc:
            await self.registry.update_probe_status(kb_id, KnowledgeProbeStatus.FAILED, str(exc))
            raise
        await self.registry.update_probe_status(kb_id, KnowledgeProbeStatus.SUCCESS, None)

    async def search(
        self,
        *,
        kb_id: str,
        query: str,
        top_k: int | None = None,
        min_score: float | None = None,
    ) -> KnowledgeSearchResult:
        if not kb_id:
            raise KnowledgeBaseValidationError("kb_id is required")
        if not query or not query.strip():
            raise KnowledgeBaseValidationError("query is required")
        base = await self.get_base(kb_id)
        if not base.enabled:
            raise KnowledgeSearchError("knowledge base is disabled")
        secret = _secret(kb_id, await self.registry.get_secret(kb_id))
        request = KnowledgeBackendSearchRequest(
            query=query.strip(),
            top_k=top_k if top_k is not None else base.default_top_k,
            min_score=min_score if min_score is not None else base.default_min_score,
        )
        return await self.retriever_factory.get(base.base_type).search(base, request, secret)

    async def knowledge_tool_definition(self) -> ToolDefinition:
        bases = [base for base in await self.registry.list_bases() if base.enabled]
        description = (
            "Search one configured knowledge base by kb_id. "
            "Choose the most relevant enabled knowledge base first based on each KB description. "
            "Do not query multiple knowledge bases initially. "
            "If the first search returns no useful results, try another relevant knowledge base. "
            "Stop searching once enough relevant evidence is found."
        )
        if bases:
            entries = "; ".join(
                f"{base.id} ({base.name}, {base.base_type.value}): {base.description}" for base in bases
            )
            description = f"{description} Enabled knowledge bases: {entries}."
        return ToolDefinition(
            name="search_knowledge",
            description=description,
            input_schema={
                "type": "object",
                "properties": {
                    "kb_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "query": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 50},
                    "min_score": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["kb_id", "query"],
                "additionalProperties": False,
            },
            enabled=bool(bases),
            source_type=ToolSourceType.KNOWLEDGE,
            toolset="knowledge",
        )

    @staticmethod
    def _validate(name: str, base_url: str, default_top_k: int | None, default_min_score: float | None) -> None:
        if not (name and name.strip()):
            raise KnowledgeBaseValidationError("name is required")
        if not (base_url and base_url.strip()):
            raise KnowledgeBaseValidationError("base_url is required")
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise KnowledgeBaseValidationError("base_url must start with http:// or https://")
        if default_top_k is not None and not (1 <= default_top_k <= 50):
            raise KnowledgeBaseValidationError("default_top_k must be between 1 and 50")
        if default_min_score is not None and not (0 <= default_min_score <= 1):
            raise KnowledgeBaseValidationError("default_min_score must be between 0 and 1")


class KnowledgeToolExecutor(ToolExecutor):
    def __init__(self, service: KnowledgeService):
        self.service = service

    async def execute(
        self,
        request: ToolCallRequest,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        if request.name != "search_knowledge":
            return ToolResult(request.id, request.name, ToolResultStatus.ERROR, {"error": "unsupported tool"})
        try:
            kb_id = _required_argument(request.arguments, "kb_id")
            query = _required_argument(request.arguments, "query")
            result = await self.service.search(
                kb_id=kb_id,
                query=query,
                top_k=_clamp_int(request.arguments.get("top_k"), 1, 50),
                min_score=_clamp_float(request.arguments.get("min_score"), 0, 1),
            )
        except (KnowledgeBaseValidationError, KnowledgeBaseNotFoundError, KnowledgeSearchError, ValueError) as exc:
            return ToolResult(request.id, request.name, ToolResultStatus.ERROR, {"error": str(exc)})
        return ToolResult(request.id, request.name, ToolResultStatus.SUCCESS, _result_to_dict(result))


def _secret(kb_id: str, api_key: str | None) -> KnowledgeBaseSecret | None:
    return KnowledgeBaseSecret(kb_id=kb_id, api_key=api_key) if api_key else None


def _required_argument(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value.strip()


def _clamp_int(value: Any, minimum: int, maximum: int) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError("top_k must be an integer")
    return min(max(value, minimum), maximum)


def _clamp_float(value: Any, minimum: float, maximum: float) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)):
        raise ValueError("min_score must be a number")
    return min(max(float(value), minimum), maximum)


def _result_to_dict(result: KnowledgeSearchResult) -> dict[str, Any]:
    return {
        "kb_id": result.kb_id,
        "kb_name": result.kb_name,
        "backend_type": result.base_type.value,
        "query": result.query,
        "results": [_snippet_to_dict(snippet) for snippet in result.results],
    }


def _snippet_to_dict(snippet: KnowledgeSnippet) -> dict[str, Any]:
    return {
        "id": snippet.id,
        "title": snippet.title,
        "content": snippet.content,
        "score": snippet.score,
        "source": snippet.source,
        "metadata": snippet.metadata,
    }

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.domain.knowledge import (
    KnowledgeBackendSearchRequest,
    KnowledgeBase,
    KnowledgeBaseSecret,
    KnowledgeBaseType,
    KnowledgeProbeError,
    KnowledgeRetriever,
    KnowledgeSearchError,
    KnowledgeSearchResult,
    KnowledgeSnippet,
)


@dataclass(frozen=True)
class HttpKnowledgeRetrieverConfig:
    base_url: str | None = None
    timeout_seconds: float = 10
    transport: httpx.AsyncBaseTransport | None = None


class NkbKnowledgeRetriever(KnowledgeRetriever):
    def __init__(self, config: HttpKnowledgeRetrieverConfig | None = None):
        self.config = config or HttpKnowledgeRetrieverConfig()

    async def probe(self, base: KnowledgeBase, secret: KnowledgeBaseSecret | None = None) -> None:
        try:
            await self.search(
                base,
                KnowledgeBackendSearchRequest(query="test", top_k=1, min_score=0),
                secret,
            )
        except KnowledgeSearchError as exc:
            raise KnowledgeProbeError(_safe_message("Knowledge probe failed", exc, secret)) from exc

    async def search(
        self,
        base: KnowledgeBase,
        request: KnowledgeBackendSearchRequest,
        secret: KnowledgeBaseSecret | None = None,
    ) -> KnowledgeSearchResult:
        url = f"{_base_url(self.config, base)}/retrieval/search"
        filters: dict[str, Any] = {}
        if base.dataset_id:
            filters["dataset_id"] = base.dataset_id
        payload = {
            "query": request.query,
            "top_k": request.top_k,
            "min_score": request.min_score,
            "filters": filters,
        }

        data = await _post_json(url, payload, self.config, secret=secret, backend_name="N-KB")
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise KnowledgeSearchError("N-KB search failed: malformed results")

        try:
            snippets = [_map_nkb_result(item) for item in results]
        except (TypeError, ValueError) as exc:
            raise KnowledgeSearchError(_safe_message("N-KB search failed", exc, secret)) from exc

        return KnowledgeSearchResult(
            kb_id=base.id,
            kb_name=base.name,
            base_type=base.base_type,
            query=request.query,
            results=snippets,
        )


class RagflowKnowledgeRetriever(KnowledgeRetriever):
    def __init__(self, config: HttpKnowledgeRetrieverConfig | None = None):
        self.config = config or HttpKnowledgeRetrieverConfig()

    async def probe(self, base: KnowledgeBase, secret: KnowledgeBaseSecret | None = None) -> None:
        try:
            await self.search(
                base,
                KnowledgeBackendSearchRequest(query="test", top_k=1, min_score=0),
                secret,
            )
        except KnowledgeSearchError as exc:
            raise KnowledgeProbeError(_safe_message("Knowledge probe failed", exc, secret)) from exc

    async def search(
        self,
        base: KnowledgeBase,
        request: KnowledgeBackendSearchRequest,
        secret: KnowledgeBaseSecret | None = None,
    ) -> KnowledgeSearchResult:
        url = f"{_base_url(self.config, base)}/api/v1/retrieval"
        headers = {}
        if secret and secret.api_key:
            headers["Authorization"] = f"Bearer {secret.api_key}"
        payload = {
            "question": request.query,
            "dataset_ids": [base.dataset_id] if base.dataset_id else [],
            "page": 1,
            "page_size": request.top_k,
            "similarity_threshold": request.min_score,
        }

        data = await _post_json(
            url,
            payload,
            self.config,
            headers=headers,
            secret=secret,
            backend_name="Ragflow",
        )
        code = data.get("code") if isinstance(data, dict) else None
        if code != 0:
            message = data.get("message") if isinstance(data, dict) else None
            detail = f"code={code}"
            if isinstance(message, str) and message:
                detail = f"{detail}, message={message}"
            raise KnowledgeSearchError(_safe_text(f"Ragflow search failed: {detail}", secret))

        chunks = data.get("data", {}).get("chunks") if isinstance(data.get("data"), dict) else None
        if not isinstance(chunks, list):
            raise KnowledgeSearchError("Ragflow search failed: malformed chunks")

        try:
            snippets = [_map_ragflow_chunk(item, base.id) for item in chunks]
        except (TypeError, ValueError) as exc:
            raise KnowledgeSearchError(_safe_message("Ragflow search failed", exc, secret)) from exc

        return KnowledgeSearchResult(
            kb_id=base.id,
            kb_name=base.name,
            base_type=base.base_type,
            query=request.query,
            results=snippets,
        )


class KnowledgeHttpRetrieverFactory:
    def __init__(self, config: HttpKnowledgeRetrieverConfig | None = None):
        self.config = config or HttpKnowledgeRetrieverConfig()
        self._retrievers: dict[KnowledgeBaseType, KnowledgeRetriever] = {
            KnowledgeBaseType.N_KB: NkbKnowledgeRetriever(self.config),
            KnowledgeBaseType.RAGFLOW: RagflowKnowledgeRetriever(self.config),
        }

    def get(self, base_type: KnowledgeBaseType) -> KnowledgeRetriever:
        try:
            return self._retrievers[base_type]
        except KeyError as exc:
            raise ValueError(f"unsupported knowledge base type: {base_type}") from exc


async def _post_json(
    url: str,
    payload: dict[str, Any],
    config: HttpKnowledgeRetrieverConfig,
    *,
    headers: dict[str, str] | None = None,
    secret: KnowledgeBaseSecret | None,
    backend_name: str,
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=config.timeout_seconds, transport=config.transport) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise KnowledgeSearchError(_safe_message("Knowledge search request failed", exc, secret)) from exc

    if response.status_code < 200 or response.status_code >= 300:
        detail = _safe_text(response.text, secret)
        if detail:
            raise KnowledgeSearchError(f"{backend_name} search failed: HTTP {response.status_code}: {detail}")
        raise KnowledgeSearchError(f"{backend_name} search failed: HTTP {response.status_code}")

    try:
        data = response.json()
    except ValueError as exc:
        raise KnowledgeSearchError(_safe_message(f"{backend_name} search failed: malformed JSON", exc, secret)) from exc
    if not isinstance(data, dict):
        raise KnowledgeSearchError(f"{backend_name} search failed: malformed response")
    return data


def _base_url(config: HttpKnowledgeRetrieverConfig, base: KnowledgeBase) -> str:
    return (config.base_url or base.base_url).rstrip("/")


def _map_nkb_result(item: Any) -> KnowledgeSnippet:
    if not isinstance(item, dict):
        raise ValueError("result must be an object")
    metadata = _metadata_from_item(item)
    source = _coerce_source(item.get("source")) or _optional_str(item.get("document_id"))
    title = _optional_str(item.get("title")) or _metadata_title(item.get("metadata"))
    return KnowledgeSnippet(
        id=_optional_str(item.get("id")) or _optional_str(item.get("chunk_id")) or _optional_str(item.get("document_id")),
        title=title,
        content=_required_str(item.get("content", item.get("snippet", item.get("text"))), "content"),
        score=_optional_float(item.get("score")),
        source=source,
        metadata=metadata,
    )


def _map_ragflow_chunk(item: Any, kb_id: str) -> KnowledgeSnippet:
    if not isinstance(item, dict):
        raise ValueError("chunk must be an object")
    metadata = {
        key: value
        for key, value in item.items()
        if key not in {"id", "content", "similarity", "document_keyword", "document_id"}
    }
    metadata["kb_id"] = kb_id
    return KnowledgeSnippet(
        id=_optional_str(item.get("id")),
        title=_optional_str(item.get("document_keyword")),
        content=_required_str(item.get("content"), "content"),
        score=_optional_float(item.get("similarity")),
        source=_optional_str(item.get("document_id")),
        metadata=metadata,
    )


def _metadata_from_item(item: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    raw_metadata = item.get("metadata")
    if isinstance(raw_metadata, dict):
        metadata.update(raw_metadata)
    for key, value in item.items():
        if key not in {"id", "chunk_id", "document_id", "title", "content", "snippet", "text", "score", "source", "metadata"}:
            metadata[key] = value
    return metadata


def _required_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _metadata_title(value: Any) -> str | None:
    if isinstance(value, dict):
        return _optional_str(value.get("title"))
    return None


def _coerce_source(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _optional_str(value.get("uri")) or _optional_str(value.get("id"))
    return None


def _safe_message(prefix: str, exc: BaseException, secret: KnowledgeBaseSecret | None) -> str:
    message = str(exc)
    if message:
        return _safe_text(f"{prefix}: {message}", secret)
    return prefix


def _safe_text(text: str, secret: KnowledgeBaseSecret | None) -> str:
    if secret and secret.api_key:
        return text.replace(secret.api_key, "[redacted]")
    return text

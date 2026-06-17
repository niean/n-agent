from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from app.domain.knowledge import (
    KnowledgeBackendSearchRequest,
    KnowledgeBase,
    KnowledgeBaseSecret,
    KnowledgeBaseType,
    KnowledgeProbeError,
    KnowledgeProbeStatus,
    KnowledgeSearchError,
)
from app.infrastructure.knowledge.http_adapters import (
    HttpKnowledgeRetrieverConfig,
    KnowledgeHttpRetrieverFactory,
    NkbKnowledgeRetriever,
    RagflowKnowledgeRetriever,
)


def make_base(
    *,
    base_type: KnowledgeBaseType = KnowledgeBaseType.N_KB,
    base_url: str = "http://kb.test/",
    dataset_id: str = "dataset-1",
) -> KnowledgeBase:
    now = datetime.now(timezone.utc)
    return KnowledgeBase(
        id="kb-1",
        name="Knowledge Base",
        description="Test knowledge base",
        base_type=base_type,
        base_url=base_url,
        dataset_id=dataset_id,
        api_key_present=True,
        enabled=True,
        default_top_k=5,
        default_min_score=0.2,
        last_probe_status=KnowledgeProbeStatus.UNKNOWN,
        last_probe_error=None,
        last_probed_at=None,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_nkb_posts_search_request_and_maps_results():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "chunk-1",
                        "title": "Python",
                        "content": "Python content",
                        "score": 0.91,
                        "source": "doc-1",
                        "metadata": {"section": "intro"},
                    }
                ]
            },
        )

    retriever = NkbKnowledgeRetriever(
        HttpKnowledgeRetrieverConfig(transport=httpx.MockTransport(handler))
    )

    result = await retriever.search(
        make_base(),
        KnowledgeBackendSearchRequest(query="python", top_k=3, min_score=0.7),
    )

    assert requests[0].method == "POST"
    assert str(requests[0].url) == "http://kb.test/retrieval/search"
    assert json.loads(requests[0].content) == {
        "query": "python",
        "top_k": 3,
        "min_score": 0.7,
        "filters": {"dataset_id": "dataset-1"},
    }
    assert result.kb_id == "kb-1"
    assert result.kb_name == "Knowledge Base"
    assert result.base_type is KnowledgeBaseType.N_KB
    assert result.query == "python"
    assert len(result.results) == 1
    snippet = result.results[0]
    assert snippet.id == "chunk-1"
    assert snippet.title == "Python"
    assert snippet.content == "Python content"
    assert snippet.score == 0.91
    assert snippet.source == "doc-1"
    assert snippet.metadata["section"] == "intro"


@pytest.mark.asyncio
async def test_nkb_omits_empty_dataset_filter():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"results": []})

    retriever = NkbKnowledgeRetriever(
        HttpKnowledgeRetrieverConfig(transport=httpx.MockTransport(handler))
    )

    await retriever.search(
        make_base(dataset_id=""),
        KnowledgeBackendSearchRequest(query="python", top_k=2, min_score=0.4),
    )

    assert json.loads(requests[0].content)["filters"] == {}


@pytest.mark.asyncio
async def test_ragflow_posts_retrieval_request_with_auth_and_maps_chunks():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "chunks": [
                        {
                            "id": "chunk-1",
                            "content": "Ragflow content",
                            "similarity": 0.82,
                            "document_keyword": "Ragflow title",
                            "document_id": "doc-1",
                            "positions": [[1, 2]],
                            "term_similarity": 0.3,
                            "vector_similarity": 0.8,
                            "important_keywords": ["ragflow"],
                        }
                    ]
                },
            },
        )

    retriever = RagflowKnowledgeRetriever(
        HttpKnowledgeRetrieverConfig(transport=httpx.MockTransport(handler))
    )

    result = await retriever.search(
        make_base(base_type=KnowledgeBaseType.RAGFLOW),
        KnowledgeBackendSearchRequest(query="rag", top_k=4, min_score=0.6),
        KnowledgeBaseSecret(kb_id="kb-1", api_key="secret"),
    )

    assert requests[0].method == "POST"
    assert str(requests[0].url) == "http://kb.test/api/v1/retrieval"
    assert requests[0].headers["Authorization"] == "Bearer secret"
    assert json.loads(requests[0].content) == {
        "question": "rag",
        "dataset_ids": ["dataset-1"],
        "page": 1,
        "page_size": 4,
        "similarity_threshold": 0.6,
    }
    snippet = result.results[0]
    assert snippet.id == "chunk-1"
    assert snippet.title == "Ragflow title"
    assert snippet.content == "Ragflow content"
    assert snippet.score == 0.82
    assert snippet.source == "doc-1"
    assert snippet.metadata["kb_id"] == "kb-1"
    assert snippet.metadata["positions"] == [[1, 2]]
    assert snippet.metadata["term_similarity"] == 0.3
    assert snippet.metadata["vector_similarity"] == 0.8
    assert snippet.metadata["important_keywords"] == ["ragflow"]


@pytest.mark.asyncio
async def test_ragflow_error_code_raises_safe_search_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 100, "message": "bad secret"})

    retriever = RagflowKnowledgeRetriever(
        HttpKnowledgeRetrieverConfig(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(KnowledgeSearchError) as exc_info:
        await retriever.search(
            make_base(base_type=KnowledgeBaseType.RAGFLOW),
            KnowledgeBackendSearchRequest(query="rag"),
            KnowledgeBaseSecret(kb_id="kb-1", api_key="secret"),
        )

    assert "secret" not in str(exc_info.value)
    assert "Ragflow search failed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_ragflow_malformed_success_payload_raises_search_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"chunks": "bad"}})

    retriever = RagflowKnowledgeRetriever(
        HttpKnowledgeRetrieverConfig(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(KnowledgeSearchError):
        await retriever.search(
            make_base(base_type=KnowledgeBaseType.RAGFLOW),
            KnowledgeBackendSearchRequest(query="rag"),
            KnowledgeBaseSecret(kb_id="kb-1", api_key="secret"),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="server error secret"),
        httpx.Response(200, json={"results": "bad"}),
    ],
)
async def test_nkb_non_2xx_or_malformed_results_raises_safe_search_error(response: httpx.Response):
    async def handler(request: httpx.Request) -> httpx.Response:
        return response

    retriever = NkbKnowledgeRetriever(
        HttpKnowledgeRetrieverConfig(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(KnowledgeSearchError) as exc_info:
        await retriever.search(
            make_base(),
            KnowledgeBackendSearchRequest(query="python"),
            KnowledgeBaseSecret(kb_id="kb-1", api_key="secret"),
        )

    assert "secret" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_transport_error_raises_safe_search_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout secret")

    retriever = NkbKnowledgeRetriever(
        HttpKnowledgeRetrieverConfig(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(KnowledgeSearchError) as exc_info:
        await retriever.search(
            make_base(),
            KnowledgeBackendSearchRequest(query="python"),
            KnowledgeBaseSecret(kb_id="kb-1", api_key="secret"),
        )

    assert "secret" not in str(exc_info.value)
    assert "Knowledge search request failed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_probe_raises_safe_probe_error_for_backend_failures():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid secret")

    retriever = RagflowKnowledgeRetriever(
        HttpKnowledgeRetrieverConfig(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(KnowledgeProbeError) as exc_info:
        await retriever.probe(
            make_base(base_type=KnowledgeBaseType.RAGFLOW),
            KnowledgeBaseSecret(kb_id="kb-1", api_key="secret"),
        )

    assert "secret" not in str(exc_info.value)
    assert "Knowledge probe failed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_probe_returns_none_on_success():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    retriever = NkbKnowledgeRetriever(
        HttpKnowledgeRetrieverConfig(transport=httpx.MockTransport(handler))
    )

    assert await retriever.probe(make_base()) is None


def test_factory_maps_supported_knowledge_base_types():
    factory = KnowledgeHttpRetrieverFactory(HttpKnowledgeRetrieverConfig())

    assert isinstance(factory.get(KnowledgeBaseType.N_KB), NkbKnowledgeRetriever)
    assert isinstance(factory.get(KnowledgeBaseType.RAGFLOW), RagflowKnowledgeRetriever)

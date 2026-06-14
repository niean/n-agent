import json

import httpx
import pytest

from app.domain.tool import ToolCallRequest, ToolResultStatus
from app.infrastructure.tools.kb import KnowledgeSearchClient, KnowledgeToolExecutor


SUCCESS_RESPONSE = {
    "query": "python",
    "results": [
        {
            "document_id": "doc-1",
            "chunk_id": "chunk-1",
            "score": 0.9,
            "snippet": "Python snippet",
            "source": {"kind": "text", "uri": "kb://doc-1"},
            "tags": {"site": "N-KB"},
            "metadata": {"title": "Python"},
        }
    ],
}


@pytest.mark.asyncio
async def test_kb_client_posts_search_request_and_normalizes_base_url():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=SUCCESS_RESPONSE)

    client = KnowledgeSearchClient("http://kb.test/", timeout_seconds=10, transport=httpx.MockTransport(handler))

    response = await client.search("python", top_k=3, min_score=0.7)

    assert response == SUCCESS_RESPONSE
    assert requests[0].method == "POST"
    assert str(requests[0].url) == "http://kb.test/retrieval/search"
    assert requests[0].content
    assert json.loads(requests[0].content) == {
        "query": "python",
        "top_k": 3,
        "min_score": 0.7,
        "filters": {},
    }


@pytest.mark.asyncio
async def test_kb_executor_maps_success_response_to_tool_result():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=SUCCESS_RESPONSE)

    client = KnowledgeSearchClient("http://kb.test", timeout_seconds=10, transport=httpx.MockTransport(handler))
    executor = KnowledgeToolExecutor(client, enabled=True, default_top_k=5, default_min_score=0.5)

    result = await executor.execute(ToolCallRequest(id="call-1", name="search_knowledge", arguments={"query": "python"}))

    assert result.status == ToolResultStatus.SUCCESS
    assert result.content == {"site": "N-KB", "query": "python", "results": SUCCESS_RESPONSE["results"]}


@pytest.mark.asyncio
async def test_kb_executor_non_2xx_returns_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    client = KnowledgeSearchClient("http://kb.test", timeout_seconds=10, transport=httpx.MockTransport(handler))
    executor = KnowledgeToolExecutor(client, enabled=True, default_top_k=5, default_min_score=0.5)

    result = await executor.execute(ToolCallRequest(id="call-1", name="search_knowledge", arguments={"query": "python"}))

    assert result.status == ToolResultStatus.ERROR
    assert result.content == {"error": "knowledge search failed"}


@pytest.mark.asyncio
async def test_kb_executor_unexpected_json_returns_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"query": "python", "results": "not-a-list"})

    client = KnowledgeSearchClient("http://kb.test", timeout_seconds=10, transport=httpx.MockTransport(handler))
    executor = KnowledgeToolExecutor(client, enabled=True, default_top_k=5, default_min_score=0.5)

    result = await executor.execute(ToolCallRequest(id="call-1", name="search_knowledge", arguments={"query": "python"}))

    assert result.status == ToolResultStatus.ERROR
    assert result.content == {"error": "knowledge search failed"}


@pytest.mark.asyncio
async def test_kb_executor_disabled_returns_permission_denied():
    executor = KnowledgeToolExecutor(None, enabled=False, default_top_k=5, default_min_score=0.5)

    result = await executor.execute(ToolCallRequest(id="call-1", name="search_knowledge", arguments={"query": "python"}))

    assert result.status == ToolResultStatus.PERMISSION_DENIED
    assert result.content == {"error": "permission_denied"}


@pytest.mark.asyncio
async def test_kb_executor_clamps_arguments_before_request():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=SUCCESS_RESPONSE)

    client = KnowledgeSearchClient("http://kb.test", timeout_seconds=10, transport=httpx.MockTransport(handler))
    executor = KnowledgeToolExecutor(client, enabled=True, default_top_k=5, default_min_score=0.5)

    result = await executor.execute(
        ToolCallRequest(
            id="call-1",
            name="search_knowledge",
            arguments={"query": " python ", "top_k": 999, "min_score": -1},
        )
    )

    assert result.status == ToolResultStatus.SUCCESS
    assert json.loads(requests[0].content) == {
        "query": "python",
        "top_k": 50,
        "min_score": 0,
        "filters": {},
    }

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.domain.tool import ToolCallRequest, ToolResult, ToolResultStatus

logger = logging.getLogger(__name__)


class KnowledgeSearchClient:
    def __init__(self, base_url: str, timeout_seconds: float, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def search(self, query: str, top_k: int, min_score: float) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
            response = await client.post(
                f"{self.base_url}/retrieval/search",
                json={
                    "query": query,
                    "top_k": top_k,
                    "min_score": min_score,
                    "filters": {},
                },
            )
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict):
            raise ValueError("invalid knowledge search response")
        return data


class KnowledgeToolExecutor:
    def __init__(
        self,
        client: KnowledgeSearchClient | None,
        enabled: bool,
        default_top_k: int,
        default_min_score: float,
    ):
        self.client = client
        self.enabled = enabled
        self.default_top_k = default_top_k
        self.default_min_score = default_min_score

    async def execute(self, request: ToolCallRequest) -> ToolResult:
        start = time.monotonic()
        if request.name != "search_knowledge":
            return self._result(request, ToolResultStatus.ERROR, {"error": "tool not found"}, start)
        if not self.enabled or self.client is None:
            return self._result(request, ToolResultStatus.PERMISSION_DENIED, {"error": "permission_denied"}, start)

        try:
            query = str(request.arguments.get("query", "")).strip()
            if not query:
                return self._result(request, ToolResultStatus.ERROR, {"error": "knowledge search failed"}, start)
            top_k = self._clamp_int(request.arguments.get("top_k", self.default_top_k), 1, 50, self.default_top_k)
            min_score = self._clamp_float(
                request.arguments.get("min_score", self.default_min_score), 0, 1, self.default_min_score
            )
            data = await self.client.search(query, top_k=top_k, min_score=min_score)
            results = data.get("results")
            if not isinstance(results, list):
                raise ValueError("invalid knowledge search results")
            response_query = data.get("query")
            if not isinstance(response_query, str):
                response_query = query
            return self._result(
                request,
                ToolResultStatus.SUCCESS,
                {"site": "N-KB", "query": response_query, "results": results},
                start,
            )
        except Exception as exc:
            logger.warning(
                "search_knowledge failed: %s: %s (base_url=%s)",
                type(exc).__name__,
                exc,
                getattr(self.client, "base_url", ""),
            )
            return self._result(request, ToolResultStatus.ERROR, {"error": "knowledge search failed"}, start)

    @staticmethod
    def _clamp_int(value: Any, minimum: int, maximum: int, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    @staticmethod
    def _clamp_float(value: Any, minimum: float, maximum: float, default: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    @staticmethod
    def _result(
        request: ToolCallRequest,
        status: ToolResultStatus,
        content: Any,
        start: float,
    ) -> ToolResult:
        return ToolResult(
            tool_call_id=request.id,
            tool_name=request.name,
            status=status,
            content=content,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

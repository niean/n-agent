# app/infrastructure/memory/external/mem0.py
from __future__ import annotations
import json
import logging
from typing import Any

from app.domain.memory_provider import ExternalMemoryProvider

logger = logging.getLogger(__name__)

_PROFILE_SCHEMA = {
    "name": "mem0_profile",
    "description": "Retrieve all stored memories about the user. Use at conversation start.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}
_SEARCH_SCHEMA = {
    "name": "mem0_search",
    "description": "Search memories by meaning. Returns ranked facts.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "rerank": {"type": "boolean"},
            "top_k": {"type": "integer"},
        },
        "required": ["query"],
    },
}
_CONCLUDE_SCHEMA = {
    "name": "mem0_conclude",
    "description": "Store a durable fact about the user.",
    "parameters": {
        "type": "object",
        "properties": {"conclusion": {"type": "string"}},
        "required": ["conclusion"],
    },
}


class Mem0Adapter(ExternalMemoryProvider):
    def __init__(self, *, http_client, config: dict[str, Any]) -> None:
        self._http = http_client
        self._config = config
        self._api_key = str(config.get("api_key", ""))
        self._base_url = str(config.get("base_url") or "https://api.mem0.ai/v3").rstrip("/")
        self._user_id = str(config.get("user_id", "n-agent-user"))
        self._agent_id = str(config.get("agent_id", "n-agent"))
        self._rerank = bool(config.get("rerank", True))

    @classmethod
    def factory(cls, *, http_client, config: dict[str, Any], secret: str | None) -> "Mem0Adapter":
        merged = dict(config)
        if secret:
            merged["api_key"] = secret
        return cls(http_client=http_client, config=merged)

    @property
    def name(self) -> str:
        return "mem0"

    def is_available(self) -> bool:
        return bool(self._api_key)

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        # 配置已在 __init__ 缓存，无网络调用
        return None

    def system_prompt_block(self) -> str:
        if not self.is_available():
            return ""
        return (
            "# Mem0 Memory\n"
            f"Active. User: {self._user_id}.\n"
            "Use mem0_search to find memories, mem0_conclude to store facts, "
            "mem0_profile for a full overview."
        )

    def prefetch(self, query: str, *, session_id: str) -> str:
        return ""

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str) -> None:
        if not self.is_available():
            return
        body = {
            "messages": [
                {"role": "user", "content": user_content},
                {"role": "assistant", "content": assistant_content},
            ],
            "user_id": self._user_id,
            "agent_id": self._agent_id,
            "metadata": {"session_id": session_id},
        }
        try:
            self._http.post(
                f"{self._base_url}/memories/add/",
                json=body,
                headers=self._auth_headers(),
            )
        except Exception as exc:
            logger.warning("Mem0 sync_turn failed: %s", exc)

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        if not self.is_available():
            return []
        return [_PROFILE_SCHEMA, _SEARCH_SCHEMA, _CONCLUDE_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        if not self.is_available():
            return json.dumps({"success": False, "error": "mem0 not configured"})
        try:
            if tool_name == "mem0_profile":
                resp = self._http.post(
                    f"{self._base_url}/memories/?page=1&page_size=50",
                    json={"filters": {"user_id": self._user_id}},
                    headers=self._auth_headers(),
                )
                results = resp.get("results", []) if isinstance(resp, dict) else []
                return json.dumps({"success": True, "results": results, "count": len(results)})
            if tool_name == "mem0_search":
                query = args.get("query", "")
                if not query:
                    return json.dumps({"success": False, "error": "query required"})
                body = {
                    "query": query,
                    "filters": {"user_id": self._user_id},
                    "rerank": bool(args.get("rerank", self._rerank)),
                    "top_k": int(args.get("top_k", 10)),
                }
                resp = self._http.post(
                    f"{self._base_url}/memories/search/",
                    json=body, headers=self._auth_headers(),
                )
                results = resp.get("results", []) if isinstance(resp, dict) else []
                return json.dumps({"success": True, "results": results, "count": len(results)})
            if tool_name == "mem0_conclude":
                conclusion = args.get("conclusion", "")
                if not conclusion:
                    return json.dumps({"success": False, "error": "conclusion required"})
                body = {
                    "messages": [{"role": "user", "content": conclusion}],
                    "user_id": self._user_id, "agent_id": self._agent_id,
                }
                resp = self._http.post(
                    f"{self._base_url}/memories/add/", json=body, headers=self._auth_headers(),
                )
                status = resp.get("status", "PENDING") if isinstance(resp, dict) else "PENDING"
                event_id = resp.get("event_id") if isinstance(resp, dict) else None
                return json.dumps({"success": True, "status": status, "event_id": event_id})
            return json.dumps({"success": False, "error": f"unknown tool {tool_name}"})
        except Exception as exc:
            logger.warning("Mem0 tool %s failed: %s", tool_name, exc)
            return json.dumps({"success": False, "error": type(exc).__name__})

    def shutdown(self) -> None:
        return None

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self._api_key}"}

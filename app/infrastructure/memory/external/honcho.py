# app/infrastructure/memory/external/honcho.py
from __future__ import annotations
import json
import logging
from typing import Any

from app.domain.memory_provider import ExternalMemoryProvider

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.honcho.dev"

_PROFILE_SCHEMA = {
    "name": "honcho_profile",
    "description": "Retrieve or update a peer card from Honcho. Omit card to read.",
    "parameters": {
        "type": "object",
        "properties": {
            "peer": {"type": "string"},
            "card": {"type": "array", "items": {"type": "string"}},
            "target": {"type": "string"},
        },
        "required": [],
    },
}
_SEARCH_SCHEMA = {
    "name": "honcho_search",
    "description": "Semantic search over Honcho's stored context about a peer.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
            "peer": {"type": "string"},
        },
        "required": ["query"],
    },
}
_REASONING_SCHEMA = {
    "name": "honcho_reasoning",
    "description": "Ask Honcho a natural language question and get a synthesized answer.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "reasoning_level": {"type": "string", "enum": ["minimal", "low", "medium", "high", "max"]},
            "peer": {"type": "string"},
        },
        "required": ["query"],
    },
}
_CONTEXT_SCHEMA = {
    "name": "honcho_context",
    "description": "Retrieve full session context from Honcho.",
    "parameters": {
        "type": "object",
        "properties": {
            "peer": {"type": "string"},
        },
        "required": [],
    },
}
_CONCLUDE_SCHEMA = {
    "name": "honcho_conclude",
    "description": "Write, query, or delete a conclusion about a peer.",
    "parameters": {
        "type": "object",
        "properties": {
            "conclusion": {"type": "string"},
            "query": {"type": "string"},
            "delete_id": {"type": "string"},
            "peer": {"type": "string"},
        },
        "required": [],
    },
}

_ALL_SCHEMAS = [_PROFILE_SCHEMA, _SEARCH_SCHEMA, _REASONING_SCHEMA, _CONTEXT_SCHEMA, _CONCLUDE_SCHEMA]


class HonchoAdapter(ExternalMemoryProvider):
    """Honcho v3 REST API 适配器。

    所有端点对齐官方 v3 路径前缀 /v3/workspaces/{workspace_id}/...，需要：
    - workspace_id（v3 workspace 标识，必填）
    - api_key（Bearer token，必填）
    - user_id（用户 peer_id，默认 n-agent-user）
    - ai_peer_id（助手 peer_id，默认 n-agent）
    - session_strategy（per-session 或 persistent）
    - recall_mode（hybrid / context / tools）
    """

    def __init__(self, *, http_client, config: dict[str, Any]) -> None:
        self._http = http_client
        self._config = config
        self._base_url = str(config.get("base_url", _DEFAULT_BASE_URL)).rstrip("/")
        self._api_key = str(config.get("api_key", ""))
        self._workspace_id = str(config.get("workspace_id", ""))
        self._user_id = str(config.get("user_id", "n-agent-user"))
        self._ai_peer_id = str(config.get("ai_peer_id", "n-agent"))
        self._session_strategy = str(config.get("session_strategy", "per-session"))
        self._recall_mode = str(config.get("recall_mode", "hybrid"))
        self._session_id = ""
        self._ensured = False  # workspace+peers 是否已 ensure（同进程幂等）

    @classmethod
    def factory(cls, *, http_client, config: dict[str, Any], secret: str | None) -> "HonchoAdapter":
        merged = dict(config)
        if secret:
            merged["api_key"] = secret
        return cls(http_client=http_client, config=merged)

    @property
    def name(self) -> str:
        return "honcho"

    def is_available(self) -> bool:
        return bool(self._api_key and self._workspace_id and self._base_url)

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = session_id
        self._ensured = False

    def _session_key(self, session_id: str) -> str:
        if self._session_strategy == "persistent":
            return self._user_id
        return session_id or self._session_id

    def _ensure_workspace(self) -> None:
        if self._ensured:
            return
        try:
            self._http.post(
                f"{self._base_url}/v3/workspaces",
                json={"id": self._workspace_id, "name": self._workspace_id},
                headers=self._auth_headers(),
            )
        except Exception as exc:
            logger.debug("Honcho ensure workspace failed (may already exist): %s", exc)
        self._ensured = True

    def _ensure_peers(self) -> None:
        for peer_id in (self._user_id, self._ai_peer_id):
            try:
                self._http.post(
                    f"{self._base_url}/v3/workspaces/{self._workspace_id}/peers",
                    json={"id": peer_id},
                    headers=self._auth_headers(),
                )
            except Exception as exc:
                logger.debug("Honcho ensure peer %s failed: %s", peer_id, exc)

    def _ensure_session(self, session_id: str) -> None:
        self._ensure_workspace()
        self._ensure_peers()
        try:
            self._http.post(
                f"{self._base_url}/v3/workspaces/{self._workspace_id}/sessions",
                json={
                    "id": session_id,
                    "peers": {self._user_id: {}, self._ai_peer_id: {}},
                },
                headers=self._auth_headers(),
            )
        except Exception as exc:
            logger.debug("Honcho ensure session failed (may already exist): %s", exc)

    def system_prompt_block(self) -> str:
        if not self.is_available():
            return ""
        if self._recall_mode == "tools":
            return (
                "## Honcho Memory\n"
                "Active (tools-only mode). Use honcho_profile / honcho_search / "
                "honcho_reasoning / honcho_context / honcho_conclude tools."
            )
        if self._recall_mode == "context":
            return (
                "## Honcho Memory\n"
                "Active (context-injection mode). Relevant context auto-injected."
            )
        return (
            "## Honcho Memory\n"
            "Active (hybrid mode). Context auto-injected AND tools available."
        )

    def prefetch(self, query: str, *, session_id: str) -> str:
        if not self.is_available() or self._recall_mode == "tools":
            return ""
        key = self._session_key(session_id)
        try:
            self._ensure_session(key)
            ctx = self._http.get(
                f"{self._base_url}/v3/workspaces/{self._workspace_id}/sessions/{key}/context",
                headers=self._auth_headers(),
                query={"summary": "true", "peer_target": self._user_id},
            )
        except Exception as exc:
            logger.warning("Honcho prefetch failed: %s", exc)
            return ""
        if not isinstance(ctx, dict):
            return ""
        parts = []
        if ctx.get("summary"):
            parts.append(f"## Summary\n{ctx['summary']}")
        if ctx.get("peer_representation"):
            parts.append(f"## Representation\n{ctx['peer_representation']}")
        if ctx.get("peer_card"):
            parts.append(f"## Card\n{ctx['peer_card']}")
        msgs = ctx.get("messages") or []
        if msgs:
            lines = [f"- {m.get('peer_id', '?')}: {m.get('content', '')}" for m in msgs if isinstance(m, dict)]
            parts.append("## Recent Messages\n" + "\n".join(lines))
        return "\n\n".join(parts)

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str) -> None:
        if not self.is_available():
            return
        key = self._session_key(session_id)
        try:
            self._ensure_session(key)
            self._http.post(
                f"{self._base_url}/v3/workspaces/{self._workspace_id}/sessions/{key}/messages",
                json={"messages": [
                    {"content": user_content, "peer_id": self._user_id},
                    {"content": assistant_content, "peer_id": self._ai_peer_id},
                ]},
                headers=self._auth_headers(),
            )
        except Exception as exc:
            logger.warning("Honcho sync_turn failed: %s", exc)

    def on_session_end(self, session_id: str) -> None:
        return None

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        if not self.is_available() or self._recall_mode == "context":
            return []
        return list(_ALL_SCHEMAS)

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        if not self.is_available():
            return json.dumps({"success": False, "error": "honcho not configured"})
        try:
            self._ensure_workspace()
            self._ensure_peers()
            peer_id = str(args.get("peer", self._user_id))
            if tool_name == "honcho_profile":
                return self._handle_profile(peer_id, args)
            if tool_name == "honcho_search":
                return self._handle_search(peer_id, args)
            if tool_name == "honcho_reasoning":
                return self._handle_reasoning(peer_id, args)
            if tool_name == "honcho_context":
                return self._handle_context(kwargs.get("session_id", self._session_id))
            if tool_name == "honcho_conclude":
                return self._handle_conclude(peer_id, args, kwargs.get("session_id", self._session_id))
            return json.dumps({"success": False, "error": f"unknown tool {tool_name}"})
        except Exception as exc:
            logger.warning("Honcho tool %s failed: %s", tool_name, exc)
            return json.dumps({"success": False, "error": type(exc).__name__})

    def _handle_profile(self, peer_id: str, args: dict[str, Any]) -> str:
        url = f"{self._base_url}/v3/workspaces/{self._workspace_id}/peers/{peer_id}/card"
        query = {"target": args["target"]} if args.get("target") else None
        card = args.get("card")
        if card is not None:
            resp = self._http.put(url, json={"peer_card": card}, headers=self._auth_headers(), query=query)
            return json.dumps({"success": True, "card": (resp or {}).get("peer_card", card) if isinstance(resp, dict) else card})
        resp = self._http.get(url, headers=self._auth_headers(), query=query)
        return json.dumps({"success": True, "card": (resp or {}).get("peer_card", []) if isinstance(resp, dict) else []})

    def _handle_search(self, peer_id: str, args: dict[str, Any]) -> str:
        query = args.get("query", "")
        if not query:
            return json.dumps({"success": False, "error": "query required"})
        url = f"{self._base_url}/v3/workspaces/{self._workspace_id}/peers/{peer_id}/search"
        resp = self._http.post(url, json={
            "query": query, "filters": None, "limit": int(args.get("limit", 10)),
        }, headers=self._auth_headers())
        return json.dumps({"success": True, "results": resp if isinstance(resp, list) else []})

    def _handle_reasoning(self, peer_id: str, args: dict[str, Any]) -> str:
        query = args.get("query", "")
        if not query:
            return json.dumps({"success": False, "error": "query required"})
        url = f"{self._base_url}/v3/workspaces/{self._workspace_id}/peers/{peer_id}/chat"
        body: dict[str, Any] = {"query": query, "stream": False}
        if "reasoning_level" in args:
            body["reasoning_level"] = args["reasoning_level"]
        resp = self._http.post(url, json=body, headers=self._auth_headers())
        content = (resp or {}).get("content") if isinstance(resp, dict) else None
        return json.dumps({"success": True, "result": content or ""})

    def _handle_context(self, session_id: str) -> str:
        key = self._session_key(session_id)
        self._ensure_session(key)
        url = f"{self._base_url}/v3/workspaces/{self._workspace_id}/sessions/{key}/context"
        resp = self._http.get(url, headers=self._auth_headers(), query={"summary": "true"})
        return json.dumps({"success": True, "result": resp if isinstance(resp, dict) else {}})

    def _handle_conclude(self, peer_id: str, args: dict[str, Any], session_id: str) -> str:
        key = self._session_key(session_id)
        self._ensure_session(key)
        delete_id = args.get("delete_id")
        if delete_id:
            self._http.delete(
                f"{self._base_url}/v3/workspaces/{self._workspace_id}/conclusions/{delete_id}",
                headers=self._auth_headers(),
            )
            return json.dumps({"success": True, "status": "deleted"})
        conclusion = args.get("conclusion")
        if conclusion:
            self._http.post(
                f"{self._base_url}/v3/workspaces/{self._workspace_id}/conclusions",
                json={"conclusions": [{
                    "content": conclusion,
                    "observer_id": peer_id,
                    "observed_id": peer_id,
                    "session_id": key,
                }]},
                headers=self._auth_headers(),
            )
            return json.dumps({"success": True, "status": "stored"})
        q = args.get("query")
        if q:
            resp = self._http.post(
                f"{self._base_url}/v3/workspaces/{self._workspace_id}/conclusions/query",
                json={"query": q},
                headers=self._auth_headers(),
            )
            return json.dumps({"success": True, "results": resp if isinstance(resp, list) else []})
        return json.dumps({"success": False, "error": "conclusion, query, or delete_id required"})

    def probe(self) -> str:
        """轻量联网验证：GET session context，不 ensure_session（避免副作用写入）。
        2xx（含 404 视为 auth 通过）→ success；401/网络异常 → failed。"""
        if not self.is_available():
            return json.dumps({"success": False, "error": "not configured"})
        try:
            self._http.get(
                f"{self._base_url}/v3/workspaces/{self._workspace_id}/sessions/probe/context",
                headers=self._auth_headers(),
                query={"summary": "true"},
            )
            return json.dumps({"success": True})
        except Exception as exc:
            return json.dumps({"success": False, "error": str(exc) or type(exc).__name__})

    def shutdown(self) -> None:
        return None

    def _auth_headers(self) -> dict[str, str]:
        h = {}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

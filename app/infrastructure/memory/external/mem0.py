# app/infrastructure/memory/external/mem0.py
from __future__ import annotations
import json
import logging
import threading
from typing import Any

from app.domain.memory_provider import ExternalMemoryProvider

logger = logging.getLogger(__name__)

_PREFETCH_WAIT_SECS = 1.5
_PREFETCH_TOP_K = 10

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
        # Prefetch state: 后台线程搜索 + 缓存结果，prefetch 短暂等待热结果
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread: threading.Thread | None = None
        self._prefetch_query = ""
        self._prefetch_result = ""
        self._prefetch_done = False

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
            "Persistent memory of this user from past conversations is available. "
            "Relevant facts are auto-recalled and injected into your context per turn; "
            "use the memory tools only when the recalled context is insufficient."
        )

    def prefetch(self, query: str, *, session_id: str) -> str:
        """自动预取：启动后台线程搜索 mem0，LLM 调用前等待最多 _PREFETCH_WAIT_SECS。

        双轨制：自动预取注入 context + 工具调用（mem0_search）兜底。
        预取失败或超时返回空，LLM 可主动调 mem0_search 补救。
        """
        if not self.is_available() or not query:
            return ""
        cached = self._consume_cached(query)
        if cached is not None:
            return cached
        self._start_prefetch(query)
        with self._prefetch_lock:
            thread = self._prefetch_thread if self._prefetch_query == query else None
        if thread is not None:
            thread.join(timeout=_PREFETCH_WAIT_SECS)
        cached = self._consume_cached(query)
        return cached or ""

    def _start_prefetch(self, query: str) -> None:
        """启动后台搜索线程；同 query 已有缓存或线程在跑则复用。"""
        with self._prefetch_lock:
            if self._prefetch_query == query:
                if self._prefetch_done:
                    return
                if self._prefetch_thread is not None and self._prefetch_thread.is_alive():
                    return
            self._prefetch_query = query
            self._prefetch_result = ""
            self._prefetch_done = False
            thread = threading.Thread(
                target=self._run_prefetch, args=(query,),
                daemon=True, name="mem0-prefetch",
            )
            self._prefetch_thread = thread
        thread.start()

    def _run_prefetch(self, query: str) -> None:
        body = ""
        try:
            body = self._search_backend(query)
        except Exception as exc:
            logger.debug("Mem0 prefetch failed: %s", exc)
        with self._prefetch_lock:
            if self._prefetch_query == query:
                self._prefetch_result = body
                self._prefetch_done = True

    def _search_backend(self, query: str) -> str:
        """同步搜索 mem0 API，返回格式化的记忆文本。无结果返回空。"""
        body = {
            "query": query,
            "filters": {"user_id": self._user_id},
            "rerank": self._rerank,
            "top_k": _PREFETCH_TOP_K,
        }
        resp = self._http.post(
            f"{self._base_url}/memories/search/",
            json=body, headers=self._auth_headers(),
        )
        results = resp.get("results", []) if isinstance(resp, dict) else []
        lines = [r.get("memory", "") for r in results if r.get("memory")]
        if not lines:
            return ""
        return "## Mem0 Memory\n" + "\n".join(f"- {l}" for l in lines)

    def _consume_cached(self, query: str) -> str | None:
        """消费缓存：query 匹配且已完成时返回结果（可能为空字符串）；否则返回 None。"""
        with self._prefetch_lock:
            if self._prefetch_query == query and self._prefetch_done:
                result = self._prefetch_result
                self._prefetch_result = ""
                self._prefetch_done = False
                return result
            return None

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

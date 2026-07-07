"""SandboxDashboardService — read-only + control views for the Sandbox Dashboard.

Delegates everything to SandboxManager / MemoryStore /
SandboxExecutionHistoryRegistry. Does not read internal dicts; goes through
the published Domain interfaces so the in-memory vs future SQLite swap is
transparent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class SandboxDashboardService:
    def __init__(
        self,
        sandbox_manager,
        memory_store,
        settings,
        history_registry=None,
    ) -> None:
        self.sandbox_manager = sandbox_manager
        self.memory_store = memory_store
        self.settings = settings
        self.history_registry = history_registry

    async def get_config(self) -> dict[str, Any]:
        sandbox_enabled = bool(getattr(self.settings, "sandbox_enabled", False))
        active_type = getattr(self.settings, "sandbox_type", "docker")
        common = {
            "timeout_seconds": getattr(self.settings, "sandbox_timeout_seconds", 300),
            "max_tool_calls": getattr(self.settings, "sandbox_max_tool_calls", 50),
            "callback_tools": list(getattr(self.settings, "sandbox_callback_tools", []) or []),
        }
        # Both local and docker rows always shown; `enabled` reflects which is active.
        # idle is docker-only (local has no container to reap).
        rows = [
            {
                "sandbox_type": "docker",
                "enabled": sandbox_enabled and active_type == "docker",
                "idle_seconds": getattr(self.settings, "sandbox_idle_seconds", 900),
                **common,
            },
            {
                "sandbox_type": "local",
                "enabled": sandbox_enabled and active_type == "local",
                "idle_seconds": None,
                **common,
            },
        ]
        return {"sandbox_enabled": sandbox_enabled, "sandbox_type": active_type, "rows": rows}

    async def list_active_sandboxes(self) -> list[dict[str, Any]]:
        if self.sandbox_manager is None:
            return []
        return [_active_to_dict(info) for info in self.sandbox_manager.list_active()]

    async def list_released_sandboxes(self) -> list[dict[str, Any]]:
        if self.sandbox_manager is None:
            return []
        return [_released_to_dict(info) for info in self.sandbox_manager.list_released()]

    async def delete_released_sandbox(self, entry_id: str) -> dict[str, Any]:
        if self.sandbox_manager is None:
            return {"ok": False, "error": "sandbox not enabled"}
        if not entry_id:
            return {"ok": False, "error": "entry_id required"}
        try:
            ok = self.sandbox_manager.delete_released(entry_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": ok}

    async def list_execute_code_history(self, session_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        merged: list[Any] = []
        seen: set[str] = set()
        if self.history_registry is not None:
            try:
                for item in self.history_registry.list_recent(session_id=session_id, limit=limit):
                    merged.append(item)
                    seen.add(item.id)
            except Exception:
                merged = []
                seen = set()
        if self.memory_store is None and merged:
            return [_history_to_dict(item) for item in merged[:limit]]
        if self.memory_store is None:
            return []
        try:
            if session_id:
                tool_calls = await self.memory_store.list_tool_calls(session_id)
                history = [tc for tc in tool_calls if getattr(tc, "tool_name", "") == "execute_code"]
            else:
                history = await self.memory_store.list_recent_tool_calls(tool_name="execute_code", limit=limit)
        except Exception:
            history = []
        for tc in history:
            if getattr(tc, "id", None) in seen:
                continue
            merged.append(tc)
            if getattr(tc, "id", None):
                seen.add(tc.id)
        merged.sort(key=lambda item: getattr(item, "created_at", datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
        return [_history_to_dict(item) for item in merged[:limit]]

    async def delete_execute_code_history(self, tool_call_id: str) -> dict[str, Any]:
        ok = False
        errors: list[str] = []
        if self.history_registry is not None:
            try:
                ok = self.history_registry.delete(tool_call_id) or ok
            except Exception as exc:
                errors.append(str(exc))
        try:
            if self.memory_store is not None:
                ok = await self.memory_store.delete_tool_call(tool_call_id) or ok
        except Exception as exc:
            errors.append(str(exc))
        if errors and not ok:
            return {"ok": False, "error": "; ".join(errors)}
        return {"ok": ok}

    async def release_sandbox(self, session_id: str) -> dict[str, Any]:
        if self.sandbox_manager is None:
            return {"ok": False, "error": "sandbox not enabled"}
        await self.sandbox_manager.force_release(session_id)
        return {"ok": True}


def _active_to_dict(info) -> dict[str, Any]:
    return {
        "session_id": info.session_id,
        "sandbox_type": info.sandbox_type,
        "scratch_root": str(info.scratch_root),
        "created_at": info.created_at.isoformat(),
        "last_used_at": info.last_used_at.isoformat(),
        "idle_seconds": info.idle_seconds,
        "container_status": info.container_status,
        "sandbox_id": info.sandbox_id,
    }


def _released_to_dict(info) -> dict[str, Any]:
    return {
        "id": info.id,
        "session_id": info.session_id,
        "sandbox_type": info.sandbox_type,
        "sandbox_id": info.sandbox_id,
        "created_at": info.created_at.isoformat(),
        "released_at": info.released_at.isoformat(),
        "reason": info.reason,
    }


def _history_to_dict(tc) -> dict[str, Any]:
    arguments = getattr(tc, "arguments", None)
    if arguments is None:
        arguments = {
            "code": getattr(tc, "code", ""),
            "code_hash": getattr(tc, "code_hash", ""),
        }
    # tool_name 优先从 result.tool_name 提取（terminal 写入时显式加了），
    # fallback 到顶层 tool_name（memory_store 路径的 ToolCall），
    # 再 fallback 到 execution_type（history_registry 路径），
    # 最后 "execute_code"。这样旧数据（execution_type=execute_code 但
    # result.tool_name=terminal）也能正确识别为 terminal。
    result = getattr(tc, "result", None)
    tool_name = None
    if isinstance(result, dict):
        tool_name = result.get("tool_name")
    if not tool_name:
        tool_name = getattr(tc, "tool_name", None)
    if not tool_name:
        tool_name = getattr(tc, "execution_type", None) or "execute_code"
    execution_type = getattr(tc, "execution_type", None) or tool_name
    return {
        "id": tc.id,
        "session_id": tc.session_id,
        "tool_name": tool_name,
        "execution_type": execution_type,
        "arguments": arguments,
        "result": getattr(tc, "result", None),
        "status": tc.status,
        "duration_ms": tc.duration_ms,
        "created_at": tc.created_at.isoformat() if hasattr(tc, "created_at") and tc.created_at else None,
    }

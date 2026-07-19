"""Read-only Dashboard views for the host_terminal executor.

Aggregates the policy loader, tool executor health and tool_call history into
stable, desensitized view objects. No write operations.

The service only reads published attributes of its dependencies:
- HostTerminalPolicySnapshotProvider.snapshot / last_error_code
- HostTerminalToolExecutor.last_health_code
- MemoryStore.list_tool_calls / list_recent_tool_calls

It never touches executor private fields, never creates an executor registry,
and never changes the authorization model. Memory-store query errors propagate
so the route layer surfaces them to the history panel instead of masking them
as an empty list.
"""
from __future__ import annotations

from typing import Any

from app.domain.host_terminal_policy import (
    HostCommandRule,
    HostExactArgRule,
    HostOneOfArgRule,
    HostSkillScriptRule,
)


_ALLOWED_ARGUMENT_KEYS = frozenset(
    {"target_type", "command", "skill", "script", "args", "timeout"}
)


def _short_hash(value: str | None) -> str:
    s = str(value or "")
    return f"{s[:8]}…" if len(s) > 8 else s


def _arg_rule_to_str(rule: object) -> str:
    if isinstance(rule, HostExactArgRule):
        return rule.value
    if isinstance(rule, HostOneOfArgRule):
        return "|".join(rule.values)
    return "-"


def _command_rule_to_dict(rule: HostCommandRule) -> dict[str, Any]:
    return {
        "rule_id": rule.rule_id,
        "executable": rule.executable,
        "positional_args": [_arg_rule_to_str(a) for a in rule.positional_args],
    }


def _skill_rule_to_dict(rule: HostSkillScriptRule) -> dict[str, Any]:
    return {
        "rule_id": rule.rule_id,
        "skill_name": rule.skill_name,
        "script_relative_path": rule.script_relative_path,
        "sha256": _short_hash(rule.sha256),
        "positional_args": [_arg_rule_to_str(a) for a in rule.positional_args],
    }


def _derive_target(arguments: Any) -> tuple[str, str]:
    if not isinstance(arguments, dict):
        return ("-", "-")
    kind = arguments.get("target_type")
    if kind == "command":
        cmd = arguments.get("command")
        return ("command", cmd.rsplit("/", 1)[-1]) if isinstance(cmd, str) and cmd else ("command", "-")
    if kind == "skill_script":
        skill = arguments.get("skill")
        script = arguments.get("script")
        if isinstance(skill, str) and isinstance(script, str) and skill and script:
            return ("skill_script", f"{skill}/{script}")
        return ("skill_script", "-")
    return ("-", "-")


def _sanitize_arguments(arguments: Any) -> dict[str, Any]:
    """Return only validated, non-sensitive argument fields; {} when malformed.

    Strips any key outside the host_terminal request schema so persistence
    artifacts never leak to the Dashboard. ``stdout``/``stderr``/``signed_url``
    are response fields and are never present in request arguments, but the
    whitelist still guards against future drift.
    """
    if not isinstance(arguments, dict):
        return {}
    return {k: arguments[k] for k in _ALLOWED_ARGUMENT_KEYS if k in arguments}


def _result_summary(result: Any) -> str:
    """Desensitized result summary; never exposes stdout/stderr/signed_url/exceptions."""
    if not isinstance(result, dict):
        return "-"
    # tool_call results are persisted as a wrapper
    # {tool_call_id, name, status, content, duration_ms}; the actual
    # ToolResult.content lives under "content". Fall back to the flat shape
    # {success/signed_url/error} for callers that pass content directly.
    content = result.get("content")
    if not isinstance(content, dict):
        content = result
    if "signed_url" in content:
        return "photo uploaded"
    if content.get("success") is True:
        return "success"
    err = content.get("error")
    return f"error: {err}" if isinstance(err, str) and err else "-"


def _history_to_dict(call: Any) -> dict[str, Any]:
    target_type, target = _derive_target(getattr(call, "arguments", None))
    created_at = getattr(call, "created_at", None)
    if hasattr(created_at, "isoformat"):
        created_at_str: str | None = created_at.isoformat()
    else:
        created_at_str = created_at if isinstance(created_at, str) else None
    return {
        "id": getattr(call, "id", None),
        "session_id": getattr(call, "session_id", None),
        "target_type": target_type,
        "target": target,
        "status": getattr(call, "status", None),
        "duration_ms": getattr(call, "duration_ms", None),
        "created_at": created_at_str,
        "arguments": _sanitize_arguments(getattr(call, "arguments", None)),
        "result_summary": _result_summary(getattr(call, "result", None)),
    }


def _sort_key(call: Any) -> Any:
    created_at = getattr(call, "created_at", None)
    # datetime is sortable; strings/None degrade to empty so they sort last
    # without raising. Malformed records never break the list render.
    if hasattr(created_at, "isoformat"):
        return created_at
    return datetime_min_sentinel()


def datetime_min_sentinel():
    # Local import to avoid a module-level datetime dependency in sort hot path;
    # returned for non-datetime created_at so they sort to the tail.
    from datetime import datetime, timezone

    return datetime.min.replace(tzinfo=timezone.utc)


class HostTerminalDashboardService:
    def __init__(self, policy_loader, tool_executor, memory_store, unavailable_reason):
        self._policy_loader = policy_loader
        self._tool_executor = tool_executor
        self._memory_store = memory_store
        self._unavailable_reason = unavailable_reason

    @property
    def _snapshot(self):
        return self._policy_loader.snapshot if self._policy_loader is not None else None

    async def get_status(self) -> dict[str, Any]:
        snapshot = self._snapshot
        last_error = (
            self._policy_loader.last_error_code if self._policy_loader is not None else None
        )
        if self._tool_executor is None or snapshot is None:
            return {
                "enabled": False,
                "health_code": self._unavailable_reason,
                "policy_version": None,
                "policy_loaded_at": None,
                "policy_content_digest": None,
                "policy_last_error": last_error,
                "limits_summary": None,
            }
        limits = snapshot.limits
        return {
            "enabled": True,
            "health_code": self._tool_executor.last_health_code,
            "policy_version": snapshot.version,
            "policy_loaded_at": snapshot.loaded_at.isoformat() if snapshot.loaded_at else None,
            "policy_content_digest": _short_hash(snapshot.content_digest),
            "policy_last_error": last_error,
            "limits_summary": {
                "default_timeout_seconds": limits.default_timeout_seconds,
                "max_timeout_seconds": limits.max_timeout_seconds,
                "max_stdout_bytes": limits.max_stdout_bytes,
                "max_stderr_bytes": limits.max_stderr_bytes,
                "max_concurrency": limits.max_concurrency,
            },
        }

    async def get_policy(self) -> dict[str, Any]:
        snapshot = self._snapshot
        last_error = (
            self._policy_loader.last_error_code if self._policy_loader is not None else None
        )
        if snapshot is None:
            return {
                "enabled": False,
                "schema_version": None,
                "version": None,
                "content_digest": None,
                "loaded_at": None,
                "limits": None,
                "command_rules": [],
                "skill_script_rules": [],
                "policy_last_error": last_error,
            }
        limits = snapshot.limits
        return {
            "enabled": True,
            "schema_version": snapshot.schema_version,
            "version": snapshot.version,
            "content_digest": _short_hash(snapshot.content_digest),
            "loaded_at": snapshot.loaded_at.isoformat() if snapshot.loaded_at else None,
            "limits": {
                "default_timeout_seconds": limits.default_timeout_seconds,
                "max_timeout_seconds": limits.max_timeout_seconds,
                "max_stdout_bytes": limits.max_stdout_bytes,
                "max_stderr_bytes": limits.max_stderr_bytes,
                "max_args": limits.max_args,
                "max_arg_length": limits.max_arg_length,
                "max_total_args_length": limits.max_total_args_length,
                "max_concurrency": limits.max_concurrency,
            },
            "command_rules": [_command_rule_to_dict(r) for r in snapshot.command_rules],
            "skill_script_rules": [_skill_rule_to_dict(r) for r in snapshot.skill_script_rules],
            "policy_last_error": last_error,
        }

    async def list_history(
        self, session_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        if self._memory_store is None:
            return []
        if session_id:
            calls = await self._memory_store.list_tool_calls(session_id)
            calls = [c for c in calls if getattr(c, "tool_name", "") == "host_terminal"]
        else:
            calls = await self._memory_store.list_recent_tool_calls(
                tool_name="host_terminal", limit=limit
            )
        # list_tool_calls returns ascending; normalize both paths to descending
        # by created_at so the UI shows newest first, then apply limit.
        ordered = sorted(calls, key=_sort_key, reverse=True)
        return [_history_to_dict(c) for c in ordered[:limit]]

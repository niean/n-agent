"""TaskSecurityDashboardService -- read-only projection of task-subsystem
security policies, scalar configs and code-level static guarantees.

Application layer: holds only ``Settings`` (the same instance passed by the
assembler). Projects a dashboard view on every call -- no caching, no IO, no
dependency on TaskService/TaskRunService/Registry, so ``task_enabled=False``
still constructs, registers and returns 200 (``task_enabled`` shows as
``false``).

Mirrors ``PolicyDashboardService``'s metadata-in-Application pattern: Chinese
display strings and presentation metadata live here, not in Domain. Domain
(TaskPolicy et al.) stays pure.

Deep immutability: metadata uses frozen dataclasses + tuples so that sector /
source_files / config / source cannot be mutated in place -- not just the
outer tuple.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.config import Settings


class TaskSecurityDashboardError(Exception):
    """Raised when the task security profile cannot be projected to a dashboard view."""


# Settings attrs allowed for display. Explicit allowlist: "attribute exists on
# Settings" alone is NOT sufficient -- sensitive attrs (provider_api_key,
# workspace_root, task_attachments_root, ...) must never be exposed even if
# they exist. Exactly the 12 task_* scalar fields from the spec Data Model.
_SETTINGS_ALLOWLIST: frozenset[str] = frozenset({
    "task_enabled",
    "task_max_concurrency",
    "task_lease_seconds",
    "task_heartbeat_timeout_seconds",
    "task_max_runtime_seconds",
    "task_dispatch_interval_seconds",
    "task_shutdown_grace_seconds",
    "task_failure_limit",
    "task_goal_max_turns",
    "task_planning_max_children",
    "task_attachment_max_bytes",
    "task_attachment_task_max_bytes",
})


@dataclass(frozen=True)
class _ConfigSpec:
    key: str
    label: str
    source: tuple  # ("settings", attr) | ("static", value) | ("resolved", attr)
    editable: bool = False  # C-class True; A/B/planning-unwired False


@dataclass(frozen=True)
class _SectorSpec:
    key: str
    name: str
    display_name: str
    dimension: str
    execution_point: str
    source_files: tuple  # tuple[str, ...]
    config: tuple  # tuple[_ConfigSpec, ...]


_TASK_SECURITY_METADATA: tuple[_SectorSpec, ...] = (
    _SectorSpec(
        key="task_policy",
        name="TaskPolicy",
        display_name="任务策略",
        dimension="状态转换、原子 claim、按任务重试断路",
        execution_point="TaskService / TaskRunService",
        source_files=(
            "app/domain/task_policy.py",
            "app/domain/task.py",
            "app/application/task_run_service.py",
        ),
        config=(
            _ConfigSpec("state_count", "状态数量", ("static", 7)),
            _ConfigSpec("claim_transition", "合法 claim 状态对", ("static", "queued -> running")),
            _ConfigSpec("retry_breaker", "断路条件", ("static", "consecutive_failures > task.max_retries")),
        ),
    ),
    _SectorSpec(
        key="task_execution",
        name="TaskExecution",
        display_name="执行管控",
        dimension="启用、并发、租约、心跳、运行时长、调度、关闭",
        execution_point="TaskRunner / TaskRunService",
        source_files=(
            "app/application/task_runner.py",
            "app/application/task_run_service.py",
            "app/config.py",
        ),
        config=(
            # B class (env-only, read-only display).
            _ConfigSpec("task_enabled", "任务子系统启用", ("settings", "task_enabled"), editable=False),
            # C class (Dashboard-editable, hot-reload; read resolved env+DB).
            _ConfigSpec("task_max_concurrency", "最大并发", ("resolved", "task_max_concurrency"), editable=True),
            _ConfigSpec("task_lease_seconds", "租约（秒）", ("resolved", "task_lease_seconds"), editable=True),
            _ConfigSpec("task_heartbeat_timeout_seconds", "心跳超时（秒）", ("resolved", "task_heartbeat_timeout_seconds"), editable=True),
            _ConfigSpec("task_max_runtime_seconds", "最大运行时长（秒）", ("resolved", "task_max_runtime_seconds"), editable=True),
            # B class (env-only).
            _ConfigSpec("task_dispatch_interval_seconds", "调度间隔（秒）", ("settings", "task_dispatch_interval_seconds"), editable=False),
            _ConfigSpec("task_shutdown_grace_seconds", "关闭宽限（秒）", ("settings", "task_shutdown_grace_seconds"), editable=False),
            # C class: task_failure_limit is now wired as Task.max_retries default
            # (create_task max_retries=None resolves to this).
            _ConfigSpec("task_failure_limit", "Task.max_retries 默认值", ("resolved", "task_failure_limit"), editable=True),
        ),
    ),
    _SectorSpec(
        key="task_planning",
        name="TaskPlanning",
        display_name="规划与附件",
        dimension="目标轮次、子任务上限、附件配额",
        execution_point="TaskAgentExecutor",
        source_files=(
            "app/application/task_agent_executor.py",
            "app/config.py",
        ),
        config=(
            _ConfigSpec("task_goal_max_turns", "目标最大轮次", ("resolved", "task_goal_max_turns"), editable=True),
            # planning 子系统已移除，task_planning_max_children 无消费方；只读展示，不可编辑。
            _ConfigSpec("task_planning_max_children", "规划最大子任务（未接入：planning 已移除）", ("settings", "task_planning_max_children"), editable=False),
            _ConfigSpec("task_attachment_max_bytes", "单附件上限（MB）", ("resolved", "task_attachment_max_bytes"), editable=True),
            _ConfigSpec("task_attachment_task_max_bytes", "任务附件总上限（MB）", ("resolved", "task_attachment_task_max_bytes"), editable=True),
        ),
    ),
    _SectorSpec(
        key="worker_security",
        name="WorkerSecurity",
        display_name="Worker 安全",
        dimension="工具剥离、Judge 只读、无人值守入口、worker token",
        execution_point="TaskAgentExecutor",
        source_files=(
            "app/application/task_agent_executor.py",
            "app/domain/task.py",
            "app/infrastructure/registry/sqlite_task_registry.py",
        ),
        config=(
            _ConfigSpec("approval_tools_stripped", "剥离审批工具（防递归自审批）", ("static", True)),
            _ConfigSpec("judge_permitted_tools", "Judge 只读工具", ("static", "task_show")),
            _ConfigSpec("worker_token_generated_per_claim", "每次 claim 生成不透明 token", ("static", True)),
            _ConfigSpec("ingress_source", "入口来源", ("static", "task")),
            _ConfigSpec("execution_mode", "执行模式", ("static", "unattended")),
        ),
    ),
    _SectorSpec(
        key="approval_security",
        name="ApprovalSecurity",
        display_name="审批安全",
        dimension="会话隔离、存在性不泄露、note 校验",
        execution_point="UserTaskToolExecutor",
        source_files=(
            "app/infrastructure/tools/user_task_management.py",
            "app/interfaces/http/task_routes.py",
        ),
        config=(
            _ConfigSpec("tool_session_isolation", "自然语言审批会话隔离", ("static", True)),
            _ConfigSpec("tool_not_found_normalization", "不泄露任务存在性", ("static", True)),
            _ConfigSpec("revise_note_required", "修订必填 note", ("static", True)),
            # C class (Dashboard-editable, hot-reload).
            _ConfigSpec("note_max_codepoints", "note 最大长度（code point）", ("resolved", "note_max_codepoints"), editable=True),
            _ConfigSpec("unknown_fields_rejected", "拒绝未知字段", ("static", True)),
        ),
    ),
)

_REQUIRED_SECTOR_FIELDS = (
    "key", "name", "display_name", "dimension", "execution_point", "source_files", "config",
)
_EXPECTED_SECTOR_KEYS = (
    "task_policy", "task_execution", "task_planning", "worker_security", "approval_security",
)


class TaskSecurityDashboardService:
    """Projects task-subsystem security policies and configs into a read-only view.

    Holds ``Settings`` (for A/B-class static + env values) and a
    ``TaskConfigService`` (for C-class resolved env+DB values). C-class reads
    go through ``get_resolved()`` so the dashboard shows the effective value
    (DB override or env). ``task_enabled=False`` still constructs/returns 200
    (config service has no task-runtime dependency).
    """

    def __init__(self, settings: Settings, task_config_service: Any = None) -> None:
        self._settings = settings
        self._task_config_service = task_config_service

    async def list_task_security(self) -> dict[str, object]:
        self._validate_metadata()
        # C-class resolved config (env + DB overrides). Falls back to an
        # env-derived TaskConfig when no config service is wired (legacy/tests);
        # the fallback reads Settings so env values still display.
        if self._task_config_service is not None:
            resolved = await self._task_config_service.get_resolved()
            resolved_config = resolved.config
        else:
            resolved_config = _resolved_from_settings(self._settings)
        sectors = [self._sector_view(meta, resolved_config) for meta in _TASK_SECURITY_METADATA]
        return {"profile_version": "task-security-v1", "policies": sectors}

    def _validate_metadata(self) -> None:
        if not isinstance(_TASK_SECURITY_METADATA, tuple):
            raise TaskSecurityDashboardError("metadata must be a tuple")
        if len(_TASK_SECURITY_METADATA) != 5:
            raise TaskSecurityDashboardError("task security metadata must contain exactly 5 sectors")
        keys = tuple(m.key for m in _TASK_SECURITY_METADATA)
        if keys != _EXPECTED_SECTOR_KEYS:
            raise TaskSecurityDashboardError("task security sector keys/order mismatch")
        for meta in _TASK_SECURITY_METADATA:
            self._validate_sector(meta)

    def _validate_sector(self, meta: _SectorSpec) -> None:
        for fld in _REQUIRED_SECTOR_FIELDS:
            if not hasattr(meta, fld):
                raise TaskSecurityDashboardError(f"sector missing field {fld}")
        for fld in ("name", "display_name", "dimension", "execution_point"):
            val = getattr(meta, fld)
            if not isinstance(val, str) or not val:
                raise TaskSecurityDashboardError(f"sector {meta.key} field {fld} must be non-empty string")
        if not isinstance(meta.key, str) or not meta.key:
            raise TaskSecurityDashboardError("sector key must be non-empty string")
        files = meta.source_files
        if not isinstance(files, tuple) or not files:
            raise TaskSecurityDashboardError(f"sector {meta.key} source_files must be non-empty tuple")
        seen_files: set[str] = set()
        for f in files:
            if not isinstance(f, str) or not f:
                raise TaskSecurityDashboardError(f"sector {meta.key} source_file must be non-empty string")
            if f.startswith("/"):
                raise TaskSecurityDashboardError(f"sector {meta.key} source_file must be relative path: {f}")
            if f in seen_files:
                raise TaskSecurityDashboardError(f"sector {meta.key} duplicate source_file {f}")
            seen_files.add(f)
        cfg = meta.config
        if not isinstance(cfg, tuple) or not cfg:
            raise TaskSecurityDashboardError(f"sector {meta.key} config must be non-empty tuple")
        seen_cfg: set[str] = set()
        for c in cfg:
            if not isinstance(c, _ConfigSpec):
                raise TaskSecurityDashboardError(f"sector {meta.key} config item must be _ConfigSpec")
            if not isinstance(c.key, str) or not c.key:
                raise TaskSecurityDashboardError(f"sector {meta.key} config key must be non-empty string")
            if not isinstance(c.label, str) or not c.label:
                raise TaskSecurityDashboardError(f"sector {meta.key} config label must be non-empty string")
            if c.key in seen_cfg:
                raise TaskSecurityDashboardError(f"sector {meta.key} duplicate config key {c.key}")
            seen_cfg.add(c.key)
            src = c.source
            if not isinstance(src, tuple) or len(src) != 2:
                raise TaskSecurityDashboardError(f"sector {meta.key} config {c.key} source must be 2-tuple")
            kind, payload = src
            if kind == "settings":
                if not isinstance(payload, str) or not payload:
                    raise TaskSecurityDashboardError("settings source attr must be non-empty string")
                # Allowlist first: existence alone is NOT sufficient.
                if payload not in _SETTINGS_ALLOWLIST:
                    raise TaskSecurityDashboardError(
                        f"settings attr {payload} not in display allowlist")
                if not hasattr(self._settings, payload):
                    raise TaskSecurityDashboardError(f"settings attr {payload} not found")
            elif kind == "static":
                _validate_static_value(payload)
            elif kind == "resolved":
                # C-class: attr must be a TaskConfig field.
                from app.domain.task_config import TASK_CONFIG_FIELDS
                if not isinstance(payload, str) or not payload:
                    raise TaskSecurityDashboardError("resolved source attr must be non-empty string")
                if payload not in TASK_CONFIG_FIELDS:
                    raise TaskSecurityDashboardError(f"resolved attr {payload} not a TaskConfig field")
            else:
                raise TaskSecurityDashboardError(f"unknown source kind {kind!r}")

    def _sector_view(self, meta: _SectorSpec, resolved_config: Any) -> dict[str, Any]:
        config_items: list[dict[str, Any]] = []
        for c in meta.config:
            kind, payload = c.source
            if kind == "settings":
                value = _normalize_value(getattr(self._settings, payload))
            elif kind == "resolved":
                value = _normalize_value(getattr(resolved_config, payload))
            else:
                value = _normalize_value(payload)
            config_items.append({"key": c.key, "label": c.label, "value": value, "editable": c.editable})
        return {
            "key": meta.key,
            "name": meta.name,
            "display_name": meta.display_name,
            "dimension": meta.dimension,
            "execution_point": meta.execution_point,
            "source_files": list(meta.source_files),
            "config": config_items,
        }


def _resolved_from_settings(settings: Settings) -> Any:
    """Build a TaskConfig from env Settings (fallback when no config service)."""
    from app.domain.task_config import TaskConfig
    return TaskConfig(
        task_max_concurrency=settings.task_max_concurrency,
        task_lease_seconds=settings.task_lease_seconds,
        task_heartbeat_timeout_seconds=settings.task_heartbeat_timeout_seconds,
        task_max_runtime_seconds=settings.task_max_runtime_seconds,
        task_goal_max_turns=settings.task_goal_max_turns,
        task_attachment_max_bytes=settings.task_attachment_max_bytes,
        task_attachment_task_max_bytes=settings.task_attachment_task_max_bytes,
        task_failure_limit=settings.task_failure_limit,
        note_max_codepoints=settings.task_note_max_codepoints,
    )


def _validate_static_value(value: Any) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TaskSecurityDashboardError("non-finite static float")
        return
    if isinstance(value, str):
        return
    if value is None:
        return
    raise TaskSecurityDashboardError(f"unsupported static value type: {type(value).__name__}")


def _normalize_value(value: Any) -> Any:
    # bool must be checked before int: bool is a subclass of int.
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TaskSecurityDashboardError("non-finite float config value")
        return value
    if isinstance(value, str):
        return value
    if value is None:
        return None
    raise TaskSecurityDashboardError(f"unsupported config value type: {type(value).__name__}")

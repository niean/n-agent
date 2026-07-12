from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import defaultdict
from typing import Any, Callable

from app.domain.memory_provider import ExternalMemoryProvider
from app.domain.tool import RiskLevel, ToolDefinition, ToolSourceType

logger = logging.getLogger(__name__)

_PROVIDER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_BUILTIN_PROVIDER_NAME = "builtin"
_MULTI_PROJECT_PROVIDER_NAME = "multi-project"

# Slot model: builtin / multi-project coexist with at most one external-query
# provider (mem0 / honcho / holographic / etc.). The external-query slot is
# the only one governed by at-most-one and swap_external_query_provider.
_BUILTIN_SLOT = "builtin"
_MULTI_PROJECT_SLOT = "multi-project"
_EXTERNAL_QUERY_SLOT = "external-query"
_SLOT_NAMES = {_BUILTIN_SLOT, _MULTI_PROJECT_SLOT, _EXTERNAL_QUERY_SLOT}

# Circuit breaker defaults — per-provider consecutive failure threshold and cooldown.
# Mirrors Hermes mem0 provider tuning: after N consecutive failures, skip the provider
# for COOLDOWN_SECS before retrying once.
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_SECS = 120.0


class _ProviderCircuitBreaker:
    """Per-provider circuit breaker tracking consecutive failures.

    A provider enters cooldown after `threshold` consecutive failures. While in
    cooldown, calls are skipped. Once the cooldown elapses, the breaker resets
    and allows one retry; a successful call resets the failure count, a failed
    call re-trips the breaker.

    Lifecycle hooks (session_switch / session_end / on_delegation / shutdown)
    are intentionally NOT routed through the breaker — those are one-shot events
    where skipping would lose semantic state, and they already have try/except
    isolation.
    """

    def __init__(
        self,
        threshold: int = _BREAKER_THRESHOLD,
        cooldown_secs: float = _BREAKER_COOLDOWN_SECS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._threshold = threshold
        self._cooldown_secs = cooldown_secs
        self._clock = clock or time.monotonic
        self._failures: dict[str, int] = defaultdict(int)
        self._open_until: dict[str, float] = defaultdict(float)

    def is_open(self, provider_name: str) -> bool:
        if self._failures[provider_name] < self._threshold:
            return False
        if self._clock() >= self._open_until[provider_name]:
            # Cooldown expired — reset and allow a single retry.
            self._failures[provider_name] = 0
            self._open_until[provider_name] = 0.0
            return False
        return True

    def record_success(self, provider_name: str) -> None:
        self._failures[provider_name] = 0
        self._open_until[provider_name] = 0.0

    def record_failure(self, provider_name: str) -> None:
        self._failures[provider_name] += 1
        if self._failures[provider_name] >= self._threshold:
            self._open_until[provider_name] = self._clock() + self._cooldown_secs
            logger.warning(
                "Memory provider %s circuit breaker tripped after %d consecutive "
                "failures. Pausing calls for %.0fs.",
                provider_name,
                self._failures[provider_name],
                self._cooldown_secs,
            )


class ExternalMemoryManager:
    """编排多个记忆提供者。

    注册约束：builtin 通常第一个注册；至多一个外部（非 builtin）提供者。
    第二个外部提供者会被拒绝并记录 warning，防止 schema 膨胀与工具名冲突
    （对齐 Hermes memory_manager 的 at-most-one-external 约束）。
    工具名采用 first-wins，避免路由漂移。

    Special case: MultiProjectMemory exposes each subdirectory as a separate selectable option.
    """

    def __init__(
        self,
        *,
        breaker_threshold: int = _BREAKER_THRESHOLD,
        breaker_cooldown_secs: float = _BREAKER_COOLDOWN_SECS,
        breaker_clock: Callable[[], float] | None = None,
    ) -> None:
        self._providers: list[ExternalMemoryProvider] = []
        self._tool_to_provider: dict[str, ExternalMemoryProvider] = {}
        self._tool_schemas_by_provider: list[
            tuple[ExternalMemoryProvider, list[dict[str, Any]]]
        ] = []
        self._has_external = False
        self._enabled_providers: set[str] | None = None
        self._breaker = _ProviderCircuitBreaker(
            threshold=breaker_threshold,
            cooldown_secs=breaker_cooldown_secs,
            clock=breaker_clock,
        )
        # Slot model: at most one external-query provider at a time.
        # builtin / multi-project coexist alongside it.
        self._external_query_provider: ExternalMemoryProvider | None = None
        self._tool_surface_callbacks: list[Callable[[], None]] = []
        self._swap_lock = threading.Lock()

    def add_provider(self, provider: ExternalMemoryProvider) -> None:
        """注册提供者。

        Slot 模型：
        - builtin slot：provider 名为 "builtin"，始终接受。
        - multi-project slot：provider 名为 "multi-project"，始终接受。
        - external-query slot：其余 provider 名，至多一个。第二个会被拒绝并记录
          warning，防止 schema 膨胀与工具名冲突。

        provider 名称必须只包含字母、数字、连字符和下划线。
        工具 schema 在注册时缓存，路由表与暴露给 LLM 的工具定义保持一致。
        """
        provider_name = self._safe_provider_name(provider)
        if not self._is_valid_provider_name(provider_name):
            logger.warning(
                "Skipping memory provider with invalid name %r; names may only contain "
                "alphanumeric characters, hyphens, and underscores",
                provider_name,
            )
            return

        slot = self._classify_slot(provider_name)
        if slot == _EXTERNAL_QUERY_SLOT:
            if self._external_query_provider is not None:
                existing = self._safe_provider_name(self._external_query_provider)
                logger.warning(
                    "Rejected memory provider %r — external-query provider %r is "
                    "already registered. Only one external-query memory provider is "
                    "allowed at a time; use swap_external_query_provider to replace it.",
                    provider_name,
                    existing,
                )
                return
            self._external_query_provider = provider
            self._has_external = True
        elif slot == _MULTI_PROJECT_SLOT:
            # Compat: _has_external tracks any non-builtin provider.
            self._has_external = True

        self._providers.append(provider)
        self._register_provider_schemas(provider)

    def _register_provider_schemas(self, provider: ExternalMemoryProvider) -> None:
        """注册 provider 的工具 schema 到路由表与缓存。

        抽取自 add_provider，swap_external_query_provider 复用此逻辑。
        工具名 first-wins：与已注册 provider 的同名工具冲突时跳过。
        """
        provider_name = self._safe_provider_name(provider)
        accepted_schemas: list[dict[str, Any]] = []
        try:
            tool_schemas = provider.get_tool_schemas()
        except Exception as exc:
            logger.warning(
                "Memory provider %s failed to get tool schemas: %s",
                provider_name,
                self._format_exception(exc),
            )
            tool_schemas = []

        for schema in tool_schemas:
            valid_schema = self._validate_tool_schema(provider_name, schema)
            if valid_schema is None:
                continue
            tool_name = valid_schema["name"]
            existing_provider = self._tool_to_provider.get(tool_name)
            if existing_provider is not None:
                logger.warning(
                    "Skipping duplicate memory tool %s from provider %s; already "
                    "registered by provider %s",
                    tool_name,
                    provider_name,
                    self._safe_provider_name(existing_provider),
                )
                continue
            self._tool_to_provider[tool_name] = provider
            accepted_schemas.append(valid_schema)

        self._tool_schemas_by_provider.append((provider, accepted_schemas))

    def swap_external_query_provider(
        self,
        new_provider: ExternalMemoryProvider | None,
    ) -> dict[str, bool]:
        """原子替换 external-query slot 的 provider。

        - 不调用 new_provider.initialize()：遵循 add_provider 模式，由调用方
          （service.activate 或 main.py startup）负责 initialize。避免
          HolographicAdapter 等 adapter 被 double-initialize 导致 SQLite 连接泄漏。
        - 回调在 _swap_lock 锁内同步执行：保证 activate 返回时工具面已一致。
        - 回调异常不阻塞 swap：捕获异常、记录 warning、返回
          tool_surface_refresh_failed=True。
        - 持锁期间 external-query slot 工具调用返回 provider_swapping。

        返回 dict：{"swapped": bool, "tool_surface_refresh_failed": bool}。
        """
        with self._swap_lock:
            old = self._external_query_provider
            if old is not None:
                # 移除旧 provider 的工具 schema 与路由表项
                self._tool_schemas_by_provider = [
                    (p, s) for p, s in self._tool_schemas_by_provider if p is not old
                ]
                for tname in list(self._tool_to_provider):
                    if self._tool_to_provider[tname] is old:
                        del self._tool_to_provider[tname]
                self._providers = [p for p in self._providers if p is not old]
                try:
                    old.shutdown()
                except Exception as exc:
                    logger.warning(
                        "old external-query provider %s shutdown failed: %s",
                        self._safe_provider_name(old),
                        self._format_exception(exc),
                    )
            if new_provider is None:
                self._external_query_provider = None
            else:
                self._external_query_provider = new_provider
                self._providers.append(new_provider)
                self._register_provider_schemas(new_provider)
                self._has_external = True
            # 回调在锁内同步执行
            refresh_failed = self._fire_tool_surface_callbacks()
        return {"swapped": True, "tool_surface_refresh_failed": refresh_failed}

    def register_tool_surface_callback(
        self, callback: Callable[[], None],
    ) -> None:
        """注册工具面变更回调。swap_external_query_provider 完成后会同步调用。"""
        self._tool_surface_callbacks.append(callback)

    def _fire_tool_surface_callbacks(self) -> bool:
        """触发所有工具面回调。返回 True 表示至少一个回调失败。"""
        failed = False
        for cb in self._tool_surface_callbacks:
            try:
                cb()
            except Exception as exc:
                failed = True
                logger.warning("tool surface callback failed: %s", exc)
        return failed

    def get_active_external_query_provider_name(self) -> str | None:
        """返回当前 external-query slot 的 provider 名（无 IO，读内存）。"""
        if self._external_query_provider is None:
            return None
        return self._safe_provider_name(self._external_query_provider)

    def resolve_provider_slot(self, name: str) -> str | None:
        """解析 provider 名到 slot（基于当前已装载 provider，无 IO）。

        供锁定 profile 时持久化 slot 映射，以便后续 provider 被删除后前端仍能
        按原 slot 分组展示历史会话的勾选状态。
        """
        if name == _BUILTIN_PROVIDER_NAME:
            return _BUILTIN_SLOT
        for provider in self._providers:
            safe_name = self._safe_provider_name(provider)
            if safe_name == _MULTI_PROJECT_PROVIDER_NAME and hasattr(provider, "list_projects"):
                if name in provider.list_projects():  # type: ignore[attr-defined]
                    return _MULTI_PROJECT_SLOT
            elif safe_name == name:
                return self._classify_slot(safe_name)
        return None

    @staticmethod
    def _classify_slot(provider_name: str) -> str:
        """根据 provider 名分类 slot。"""
        if provider_name == _BUILTIN_PROVIDER_NAME:
            return _BUILTIN_SLOT
        if provider_name == _MULTI_PROJECT_PROVIDER_NAME:
            return _MULTI_PROJECT_SLOT
        return _EXTERNAL_QUERY_SLOT

    def set_global_enabled(self, provider_names: list[str]) -> None:
        """Set globally enabled providers."""
        self._enabled_providers = set(provider_names)

    def list_providers(self) -> list[dict]:
        """Return all registered providers with global enabled status and slot.

        For MultiProjectMemory, expand each project as a separate entry.
        """
        result: list[dict] = []
        for provider in self._providers:
            name = self._safe_provider_name(provider)
            slot = self._classify_slot(name)
            # Special case: MultiProjectMemory expands projects
            if name == "multi-project" and hasattr(provider, "list_projects"):
                for project_name in provider.list_projects():  # type: ignore[attr-defined]
                    enabled_global = self._is_enabled(project_name, enabled_override=None)
                    result.append({
                        "name": project_name,
                        "enabled_global": enabled_global,
                        "slot": slot,
                    })
            else:
                enabled_global = self._is_enabled(name, enabled_override=None)
                item = {
                    "name": name,
                    "enabled_global": enabled_global,
                    "slot": slot,
                }
                if slot == _EXTERNAL_QUERY_SLOT:
                    item["active"] = True
                result.append(item)
        return result

    def _is_enabled(self, provider_name: str, enabled_override: list[str] | None) -> bool:
        """Check if provider is enabled given global config and optional session override."""
        if enabled_override is not None:
            return provider_name in enabled_override
        if self._enabled_providers is None:
            # Default: only enable builtin, not project memories
            return provider_name == "builtin"
        return provider_name in self._enabled_providers

    def _enabled_multi_project_names(
        self,
        provider: ExternalMemoryProvider,
        enabled_override: list[str] | None,
    ) -> list[str]:
        if not hasattr(provider, "list_projects"):
            return []
        available_projects = set(provider.list_projects())  # type: ignore[attr-defined]
        if enabled_override is not None:
            candidates = enabled_override
        elif self._enabled_providers is not None:
            candidates = list(self._enabled_providers)
        else:
            candidates = []
        return [name for name in candidates if name in available_projects]

    def build_system_prompt(self, enabled_override: list[str] | None = None) -> str:
        """收集所有提供者的静态提示块，拼接到 system prompt.

        enabled_override: session-level override from request options; None means use global.
        Note: build_system_prompt is called during prepare_context and may receive
        the session override from run_options.

        For MultiProjectMemory, sets the enabled projects before building.
        """
        blocks: list[str] = []
        for provider in self._providers:
            provider_name = self._safe_provider_name(provider)
            # Special case: MultiProjectMemory needs to know which projects are enabled
            if provider_name == "multi-project" and hasattr(provider, "set_enabled_projects"):
                # Collect enabled project names from enabled_override/global
                mp = provider  # type: ignore
                enabled_projects = self._enabled_multi_project_names(provider, enabled_override)
                mp.set_enabled_projects(enabled_projects)
                if not enabled_projects:
                    continue
            elif not self._is_enabled(provider_name, enabled_override):
                continue
            if self._breaker.is_open(provider_name):
                logger.info(
                    "Memory provider %s in cooldown, skip system_prompt_block",
                    provider_name,
                )
                continue
            try:
                block = provider.system_prompt_block()
            except Exception as exc:
                logger.warning(
                    "Memory provider %s failed to build system prompt block: %s",
                    provider_name,
                    self._format_exception(exc),
                )
                self._breaker.record_failure(provider_name)
                continue
            self._breaker.record_success(provider_name)
            if block:
                blocks.append(block)
        return "\n\n".join(blocks)

    def prefetch_all(
        self,
        query: str,
        *,
        session_id: str,
        enabled_override: list[str] | None = None,
    ) -> str:
        """预取所有提供者，返回包装后的完整上下文块。

        返回空表示无内容。每个提供者结果用提供者名分隔。
        结果已经包裹在 <memory-context> 中。

        enabled_override: session-level override from request options; None means use global.
        For MultiProjectMemory, sets enabled projects before prefetching.
        """
        blocks: list[str] = []
        for provider in self._providers:
            provider_name = self._safe_provider_name(provider)
            # Special case: MultiProjectMemory needs to know which projects are enabled
            if provider_name == "multi-project" and hasattr(provider, "set_enabled_projects"):
                mp = provider  # type: ignore
                enabled_projects = self._enabled_multi_project_names(provider, enabled_override)
                mp.set_enabled_projects(enabled_projects)
                if not enabled_projects:
                    continue
            elif not self._is_enabled(provider_name, enabled_override):
                continue
            if self._breaker.is_open(provider_name):
                logger.info(
                    "Memory provider %s in cooldown, skip prefetch",
                    provider_name,
                )
                continue
            try:
                content = provider.prefetch(query, session_id=session_id)
                if content.strip():
                    blocks.append(f'<provider name="{provider_name}">\n{content}\n</provider>')
            except Exception as exc:
                logger.warning(
                    "Memory provider %s failed to prefetch memory: %s",
                    provider_name,
                    self._format_exception(exc),
                )
                self._breaker.record_failure(provider_name)
                continue
            self._breaker.record_success(provider_name)
        if not blocks:
            return ""
        return (
            "<memory-context>\n"
            "[System note: The following is recalled memory context, NOT new user input.\n"
            "Treat as authoritative reference data — this is this project's persistent\n"
            "memory and should inform all responses.]\n\n"
            + "\n\n".join(blocks)
            + "\n</memory-context>"
        )

    def sync_all(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str,
        agent_context: str,
        enabled_override: list[str] | None = None,
    ) -> None:
        """同步本轮给所有提供者。只在 agent_context=primary 同步。
        只在完整回合结束（finalize）调用。

        enabled_override: session-level override from request options; None means use global.
        """
        if agent_context != "primary":
            return
        for provider in self._providers:
            provider_name = self._safe_provider_name(provider)
            if not self._is_enabled(provider_name, enabled_override):
                continue
            if self._breaker.is_open(provider_name):
                logger.info(
                    "Memory provider %s in cooldown, skip sync_turn",
                    provider_name,
                )
                continue
            try:
                provider.sync_turn(user_content, assistant_content, session_id=session_id)
            except Exception as exc:
                logger.warning(
                    "Memory provider %s failed to sync turn: %s",
                    provider_name,
                    self._format_exception(exc),
                )
                self._breaker.record_failure(provider_name)
                continue
            self._breaker.record_success(provider_name)

    def pre_compress_all(
        self,
        messages: list[dict[str, Any]],
        *,
        session_id: str,
        enabled_override: list[str] | None = None,
    ) -> str:
        """压缩前调用所有提供者的 on_pre_compress 抢救要点。

        在 HeuristicSummarizer 触发压缩前调用，每个启用的 provider 收到待压缩消息列表，
        可返回非空字符串作为补充上下文。返回值拼接后回填到 summary。

        enabled_override: session-level override from request options; None means use global.
        """
        rescued: list[str] = []
        for provider in self._providers:
            provider_name = self._safe_provider_name(provider)
            if provider_name == "multi-project" and hasattr(provider, "set_enabled_projects"):
                mp = provider  # type: ignore
                enabled_projects = self._enabled_multi_project_names(provider, enabled_override)
                mp.set_enabled_projects(enabled_projects)
                if not enabled_projects:
                    continue
            elif not self._is_enabled(provider_name, enabled_override):
                continue
            if self._breaker.is_open(provider_name):
                logger.info(
                    "Memory provider %s in cooldown, skip on_pre_compress",
                    provider_name,
                )
                continue
            try:
                content = provider.on_pre_compress(messages)
            except Exception as exc:
                logger.warning(
                    "Memory provider %s failed on pre-compress: %s",
                    provider_name,
                    self._format_exception(exc),
                )
                self._breaker.record_failure(provider_name)
                continue
            self._breaker.record_success(provider_name)
            if content and content.strip():
                rescued.append(content.strip())
        return "\n\n".join(rescued)

    def get_tool_definitions(self) -> list[ToolDefinition]:
        """返回注册时缓存的 provider 工具定义。

        风险等级 / source_type：
        - 所有 provider 工具 → RiskLevel.SAFE + source_type=ToolSourceType.AGENT
        """
        definitions: list[ToolDefinition] = []
        for _provider, schemas in self._tool_schemas_by_provider:
            for schema in schemas:
                definitions.append(
                    ToolDefinition(
                        name=schema["name"],
                        description=schema.get("description", ""),
                        input_schema=schema["parameters"],
                        risk_level=RiskLevel.SAFE,
                        source_type=ToolSourceType.AGENT,
                        toolset="memory",
                        enabled=True,
                    )
                )
        return definitions

    def handle_tool_call(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        agent_context: str,
        session_id: str,
        enabled_override: list[str] | None = None,
    ) -> str:
        """路由工具调用到正确提供者，返回 JSON 结果。

        enabled_override: session-level override from request options; None means use global.
        """
        provider = self._tool_to_provider.get(tool_name)
        if provider is None:
            return json.dumps({"success": False, "error": "tool not found"})
        provider_name = self._safe_provider_name(provider)
        slot = self._classify_slot(provider_name)
        # external-query slot 工具在 swap 进行中时拒绝调用
        if slot == _EXTERNAL_QUERY_SLOT and self._swap_lock.locked():
            return json.dumps({"success": False, "error": "provider_swapping"})
        if not self._is_enabled(provider_name, enabled_override):
            return json.dumps({"success": False, "error": "provider not enabled"})
        if self._breaker.is_open(provider_name):
            logger.info(
                "Memory provider %s in cooldown, skip tool %s",
                provider_name,
                tool_name,
            )
            return json.dumps({
                "success": False,
                "error": "provider in cooldown",
            })
        try:
            result = provider.handle_tool_call(
                tool_name, args, agent_context=agent_context, session_id=session_id
            )
        except Exception as exc:
            logger.warning(
                "Memory provider %s failed to handle tool %s: %s",
                provider_name,
                tool_name,
                self._format_exception(exc),
            )
            self._breaker.record_failure(provider_name)
            return json.dumps({"success": False, "error": type(exc).__name__})
        self._breaker.record_success(provider_name)
        return result

    def has_tool(self, tool_name: str) -> bool:
        """检查是否有此工具。"""
        return tool_name in self._tool_to_provider

    def on_session_switch(self, new_session_id: str, **kwargs: Any) -> None:
        """通知所有提供者会话切换。"""
        for provider in self._providers:
            provider_name = self._safe_provider_name(provider)
            try:
                provider.on_session_switch(new_session_id, **kwargs)
            except Exception as exc:
                logger.warning(
                    "Memory provider %s failed to handle session switch: %s",
                    provider_name,
                    self._format_exception(exc),
                )
                continue

    def on_session_end(self, session_id: str) -> None:
        """通知所有提供者会话结束。

        在会话被删除前调用，每个启用的 provider 可做会话级清理、摘要落盘。
        单个 provider 失败不阻塞其他 provider。
        """
        for provider in self._providers:
            provider_name = self._safe_provider_name(provider)
            try:
                provider.on_session_end(session_id)
            except Exception as exc:
                logger.warning(
                    "Memory provider %s failed to handle session end: %s",
                    provider_name,
                    self._format_exception(exc),
                )
                continue

    def on_delegation(
        self,
        task: str,
        result: str,
        *,
        child_session_id: str = "",
        **kwargs: Any,
    ) -> None:
        """通知所有提供者子 Agent 完成。

        子 Agent 完成任务后在父会话调用，把子任务 prompt 与结果交给父会话的
        provider。子 Agent 自身不持有 provider 会话（skip_memory=True）。
        单个 provider 失败不阻塞其他 provider。

        事件源待定：依赖 N-Agent 多 Agent 编排落地，当前为接口占位。
        """
        for provider in self._providers:
            provider_name = self._safe_provider_name(provider)
            try:
                provider.on_delegation(
                    task,
                    result,
                    child_session_id=child_session_id,
                    **kwargs,
                )
            except Exception as exc:
                logger.warning(
                    "Memory provider %s failed to handle delegation: %s",
                    provider_name,
                    self._format_exception(exc),
                )
                continue

    def shutdown_all(self) -> None:
        """关闭所有提供者。"""
        for provider in self._providers:
            provider_name = self._safe_provider_name(provider)
            try:
                provider.shutdown()
            except Exception as exc:
                logger.warning(
                    "Memory provider %s failed to shutdown: %s",
                    provider_name,
                    self._format_exception(exc),
                )
                continue

    @staticmethod
    def _safe_provider_name(provider: ExternalMemoryProvider) -> str:
        try:
            name = provider.name
        except Exception as exc:
            logger.warning(
                "Unable to read memory provider name: %s",
                ExternalMemoryManager._format_exception(exc),
            )
            return "<unknown>"
        if isinstance(name, str):
            return name
        return repr(name)

    @staticmethod
    def _is_valid_provider_name(provider_name: str) -> bool:
        return bool(_PROVIDER_NAME_RE.fullmatch(provider_name))

    @staticmethod
    def _validate_tool_schema(
        provider_name: str, schema: Any
    ) -> dict[str, Any] | None:
        if not isinstance(schema, dict):
            logger.warning(
                "Skipping invalid memory tool schema from provider %s: schema must be "
                "an object",
                provider_name,
            )
            return None

        tool_name = schema.get("name")
        if not isinstance(tool_name, str) or not tool_name:
            logger.warning(
                "Skipping invalid memory tool schema from provider %s: missing tool name",
                provider_name,
            )
            return None

        parameters = schema.get("parameters", {})
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            logger.warning(
                "Skipping invalid memory tool schema %s from provider %s: parameters "
                "must be an object schema",
                tool_name,
                provider_name,
            )
            return None

        return {**schema, "name": tool_name, "parameters": parameters}

    @staticmethod
    def _format_exception(exc: Exception) -> str:
        return f"{type(exc).__name__}: {exc}"

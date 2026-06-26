from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.domain.memory_provider import ExternalMemoryProvider
from app.domain.tool import RiskLevel, ToolDefinition, ToolSourceType

logger = logging.getLogger(__name__)

_PROVIDER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_BUILTIN_PROVIDER_NAME = "builtin"


class ExternalMemoryManager:
    """编排多个记忆提供者。

    注册约定：builtin 通常第一个注册，外置提供者通常最多一个。
    这是约定而非强制限制；管理器只跟踪是否已有外置提供者，
    不阻止后续 provider 注册。工具名采用 first-wins，避免路由漂移。

    Special case: MultiProjectMemory exposes each subdirectory as a separate selectable option.
    """

    def __init__(self):
        self._providers: list[ExternalMemoryProvider] = []
        self._tool_to_provider: dict[str, ExternalMemoryProvider] = {}
        self._tool_schemas_by_provider: list[
            tuple[ExternalMemoryProvider, list[dict[str, Any]]]
        ] = []
        self._has_external = False
        self._enabled_providers: set[str] | None = None

    def add_provider(self, provider: ExternalMemoryProvider) -> None:
        """注册提供者。

        builtin-first / at-most-one-external 是注册约定，不在此强制。
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

        if provider_name != _BUILTIN_PROVIDER_NAME:
            self._has_external = True

        self._providers.append(provider)
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

    def set_global_enabled(self, provider_names: list[str]) -> None:
        """Set globally enabled providers."""
        self._enabled_providers = set(provider_names)

    def list_providers(self) -> list[dict]:
        """Return all registered providers with global enabled status.

        For MultiProjectMemory, expand each project as a separate entry.
        """
        result: list[dict] = []
        for provider in self._providers:
            name = self._safe_provider_name(provider)
            # Special case: MultiProjectMemory expands projects
            if name == "multi-project" and hasattr(provider, "list_projects"):
                for project_name in provider.list_projects():  # type: ignore[attr-defined]
                    enabled_global = self._is_enabled(project_name, enabled_override=None)
                    result.append({"name": project_name, "enabled_global": enabled_global})
            else:
                enabled_global = self._is_enabled(name, enabled_override=None)
                result.append({"name": name, "enabled_global": enabled_global})
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
        Note: build_system_prompt is called from load_context before request options
        are available, so it typically won't get a session override. Refreshing the page
        after global config change picks up the new configuration.

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
            try:
                block = provider.system_prompt_block()
            except Exception as exc:
                logger.warning(
                    "Memory provider %s failed to build system prompt block: %s",
                    provider_name,
                    self._format_exception(exc),
                )
                continue
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
                continue
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
            try:
                provider.sync_turn(user_content, assistant_content, session_id=session_id)
            except Exception as exc:
                logger.warning(
                    "Memory provider %s failed to sync turn: %s",
                    provider_name,
                    self._format_exception(exc),
                )
                continue

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
        if not self._is_enabled(provider_name, enabled_override):
            return json.dumps({"success": False, "error": "provider not enabled"})
        try:
            return provider.handle_tool_call(
                tool_name, args, agent_context=agent_context, session_id=session_id
            )
        except Exception as exc:
            logger.warning(
                "Memory provider %s failed to handle tool %s: %s",
                provider_name,
                tool_name,
                self._format_exception(exc),
            )
            return json.dumps({"success": False, "error": type(exc).__name__})

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

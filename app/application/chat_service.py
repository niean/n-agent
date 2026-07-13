from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol
from uuid import uuid4

from app.application.agent_graph import AgentGraphRunner
from app.application.events import ChatEvent, ChatEventType
from app.application.session_service import SessionService
from app.domain.agent import AgentState
from app.domain.memory import MemoryStore
from app.domain.session import ConversationMessage, SessionSource
from app.domain.tool import (
    ApprovalDecider,
    ConfirmToolGrant,
    RiskLevel,
    ToolExecutionContext,
)
from app.utils.content_utils import extract_text, normalize_content


class ActiveExternalMemoryReader(Protocol):
    def get_active_provider_names(self) -> list[str]: ...


@dataclass(frozen=True)
class ChatCompletionInput:
    model: str
    messages: list[dict[str, Any]]
    stream: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    trusted_metadata: dict[str, Any] = field(default_factory=dict)
    approval_decider: ApprovalDecider | None = None
    allowed_confirm_tools_override: dict[str, ConfirmToolGrant] | None = None


@dataclass(frozen=True)
class ChatCompletionResult:
    session_id: str
    model: str
    message: dict[str, Any]
    finish_reason: str = "stop"
    usage: dict[str, Any] = field(default_factory=dict)


class ChatCompletionService:
    def __init__(
        self,
        memory_store: MemoryStore,
        graph_runner: AgentGraphRunner,
        session_service: SessionService,
        external_memory_reader: "ActiveExternalMemoryReader | None" = None,
        slot_resolver: "Callable[[str], str | None] | None" = None,
    ):
        self.memory_store = memory_store
        self.graph_runner = graph_runner
        self.session_service = session_service
        self._external_memory_reader = external_memory_reader
        self._slot_resolver = slot_resolver

    async def compress_session(self, session_id: str) -> dict[str, Any]:
        """Force compress a session's context without LLM call.

 Delegates to AgentGraphRunner.compress_session. Used by slash command
 `/compress` (Gateway + Dashboard paths) and conversational trigger.
 """
        return await self.graph_runner.compress_session(session_id)

    async def complete(self, request: ChatCompletionInput) -> ChatCompletionResult | AsyncIterator[ChatEvent]:
        session_id = request.session_id or request.metadata.get("session_id") or f"api-{uuid4()}"
        await self.session_service.create_session(session_id, source=SessionSource.API.value)
        session = await self.memory_store.get_session(session_id)
        existing_messages = await self.memory_store.list_messages(session_id)
        has_override = "external_memory_enabled" in request.options
        requested_memory = self._normalize_external_memory_enabled(request.options.get("external_memory_enabled"))
        if session is not None and session.external_memory_enabled is not None:
            locked_external_memory = session.external_memory_enabled
        elif existing_messages:
            locked_external_memory = []
        elif has_override:
            locked_external_memory = requested_memory
        else:
            locked_external_memory = []

        locked_external_memory = await self.memory_store.lock_session_external_memory(
            session_id, locked_external_memory, slots=self._build_slot_map(locked_external_memory),
        )
        normalized_messages: list[dict[str, Any]] = []
        for message in request.messages:
            new_message = dict(message)
            if new_message.get("role") == "user":
                new_message["content"] = normalize_content(new_message.get("content", ""))
            normalized_messages.append(new_message)
        first_user_message = next(
            (extract_text(message.get("content", ""))
             for message in normalized_messages if message.get("role") == "user"),
            "",
        )
        # Detect /compress slash command: compress directly without saving user message or calling LLM
        if _is_compress_slash(first_user_message):
            status = await self.compress_session(session_id)
            content = _format_compress_status(status)
            if request.stream:
                return _compress_status_stream(content)
            return ChatCompletionResult(
                session_id=session_id,
                model=request.model,
                message={"role": "assistant", "content": content},
                finish_reason="stop",
            )
        for message in normalized_messages:
            if message.get("role") == "user":
                await self.memory_store.append_message(
                    session_id,
                    ConversationMessage(role="user", content=message.get("content", "")),
                )
        await self.session_service.ensure_title(session_id, str(first_user_message))
        state = AgentState(session_id=session_id, input_messages=normalized_messages)
        options = dict(request.options)
        if _detect_conversational_compress(first_user_message):
            options["force_compress"] = True
        options["external_memory_enabled"] = locked_external_memory
        mode = options.get("execution_context_mode") or "realtime"
        if mode == "unattended":
            options["tool_exposure_policy"] = "safe_only"

        # If caller didn't set agent_context, derive from execution_context_mode
        if "agent_context" not in request.trusted_metadata:
            if mode == "realtime":
                # Interactive realtime conversation -> primary allows writes
                request.trusted_metadata["agent_context"] = "primary"
            else:
                # unattended/cron/subagent -> non-primary prohibits writes
                request.trusted_metadata["agent_context"] = "unattended"

        mcp_ctx = _mcp_tool_execution_context(str(first_user_message)) if mode == "realtime" else ToolExecutionContext()
        permitted = self._compute_permitted_managed_tools(mode, request.trusted_metadata)
        ctx = ToolExecutionContext(
            allowed_confirm_tools=dict(mcp_ctx.allowed_confirm_tools),
            session_id=session_id,
            metadata=dict(request.metadata),
            trusted_metadata=dict(request.trusted_metadata),
            execution_context_mode=mode,
            permitted_managed_tools=permitted,
            enabled_override=locked_external_memory,
        )
        if request.approval_decider is not None:
            ctx = dataclasses.replace(ctx, approval_decider=request.approval_decider)
        if request.allowed_confirm_tools_override:
            allowed_override, permitted_override = self._normalize_confirm_tool_grants(
                request.allowed_confirm_tools_override
            )
            ctx = dataclasses.replace(
                ctx,
                allowed_confirm_tools={
                    **ctx.allowed_confirm_tools,
                    **allowed_override,
                },
                permitted_managed_tools={
                    *ctx.permitted_managed_tools,
                    *permitted_override,
                },
            )
        options["tool_execution_context"] = ctx
        if request.stream:
            return self.graph_runner.stream_events(state, request.model, options)
        final_state = await self.graph_runner.run(state, request.model, options)
        if final_state.error:
            return ChatCompletionResult(
                session_id=session_id,
                model=request.model,
                message={"role": "assistant", "content": final_state.error},
                finish_reason="error",
            )
        return ChatCompletionResult(
            session_id=session_id,
            model=request.model,
            message=final_state.final_message or {"role": "assistant", "content": ""},
            finish_reason=final_state.finish_reason or "stop",
        )

    @staticmethod
    def _compute_permitted_managed_tools(mode: str, trusted_metadata: dict[str, Any]) -> set[str]:
        if mode != "realtime":
            return set()
        gateway_platform = trusted_metadata.get("gateway.platform")
        from app.domain.platform import Platform

        if gateway_platform == Platform.FEISHU.value:
            return {"manage_schedule"}
        return set()

    def _normalize_confirm_tool_grants(
        self,
        grants: Any,
    ) -> tuple[dict[str, ConfirmToolGrant], set[str]]:
        allowed: dict[str, ConfirmToolGrant] = {}
        permitted: set[str] = set()
        if type(grants) is not dict:
            return allowed, permitted
        tool_service = getattr(self.graph_runner, "tool_service", None)
        if tool_service is None:
            return allowed, permitted

        for tool_name, grant in grants.items():
            if not isinstance(tool_name, str) or not tool_name.strip():
                continue
            definition = tool_service.get_definition(tool_name)
            if (
                definition is None
                or not definition.enabled
                or definition.risk_level is not RiskLevel.CONFIRM
            ):
                continue
            if definition.managed:
                if isinstance(grant, str) and grant == "session":
                    permitted.add(tool_name)
                continue
            if isinstance(grant, str) and grant == "session":
                allowed[tool_name] = "session"
            elif type(grant) is dict and all(
                isinstance(key, str)
                for key in grant
            ):
                allowed[tool_name] = dict(grant)
        return allowed, permitted

    def _active_external_memory_names(self) -> set[str]:
        if self._external_memory_reader is None:
            return set()
        try:
            return {str(name).strip() for name in self._external_memory_reader.get_active_provider_names() if str(name).strip()}
        except Exception:
            return set()

    def _build_slot_map(self, names: list[str]) -> dict[str, str] | None:
        """构建 provider name -> slot 映射，供历史会话忠实分组展示。

        仅在 slot_resolver 可用时构建；resolver 无法解析的 name 不计入（前端
        会回退到 phantom 兜底）。
        """
        if self._slot_resolver is None:
            return None
        slots: dict[str, str] = {}
        for name in names:
            slot = self._slot_resolver(name)
            if slot:
                slots[name] = slot
        return slots or None

    def _normalize_external_memory_enabled(self, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            return []
        names: list[str] = []
        for item in value:
            name = str(item).strip()
            if name and name not in names:
                names.append(name)
        active_external_names = self._active_external_memory_names()
        external_query = [name for name in names if name in active_external_names]
        projects = [name for name in names if name != "builtin" and name not in active_external_names][:1]
        enabled: list[str] = []
        if "builtin" in names:
            enabled.append("builtin")
        enabled.extend(projects)
        enabled.extend(external_query)
        return enabled


def _mcp_tool_execution_context(user_message: str) -> ToolExecutionContext:
    text = user_message.strip()
    lowered = text.lower()
    allowed: dict[str, dict[str, object]] = {}
    url = _extract_url(text)
    transport_type = _extract_transport_type(lowered)
    if url and any(word in lowered for word in ("mcp", "站点", "site")):
        if any(word in lowered for word in ("探测", "probe", "检测")):
            expected: dict[str, object] = {"url": url}
            if transport_type:
                expected["transport_type"] = transport_type
            allowed["mcp_site_probe"] = expected
        if any(word in lowered for word in ("添加", "新增", "add", "保存")):
            expected = {"url": url}
            if transport_type:
                expected["transport_type"] = transport_type
            allowed["mcp_site_add"] = expected
    if any(word in lowered for word in ("刷新", "refresh")) and any(word in lowered for word in ("mcp", "站点", "site")):
        refresh_target = _extract_key_value(text, "site_id") or _extract_key_value(text, "name")
        if refresh_target:
            key = "site_id" if "site_id" in text else "name"
            allowed["mcp_site_refresh"] = {key: refresh_target}
    return ToolExecutionContext(allowed_confirm_tools=allowed)


def _extract_url(text: str) -> str | None:
    for token in text.replace("，", " ").replace("。", " ").split():
        if token.startswith(("http://", "https://")):
            return token.rstrip(",.;，。")
    return None


def _extract_key_value(text: str, key: str) -> str | None:
    for token in text.replace("，", " ").replace("。", " ").split():
        if token.startswith(f"{key}="):
            return token.split("=", 1)[1].strip().rstrip(",.;，。")
        if token.startswith(f"{key}:"):
            return token.split(":", 1)[1].strip().rstrip(",.;，。")
    return None


def _extract_transport_type(text: str) -> str | None:
    if "streamable_http" in text or "streamable http" in text:
        return "streamable_http"
    if "sse" in text:
        return "sse"
    return None


def _is_compress_slash(text: str) -> bool:
    stripped = text.strip()
    return stripped == "/compress" or stripped.startswith("/compress ")


def _detect_conversational_compress(text: str) -> bool:
    lowered = text.lower()
    has_cn = "压缩" in text and "上下文" in text
    has_en = "compress" in lowered and "context" in lowered
    return has_cn or has_en


def _format_compress_status(status: dict[str, Any]) -> str:
    if status.get("compressed"):
        return "已压缩上下文"
    reason = str(status.get("reason") or "")
    if reason == "context_engine_unavailable":
        return "上下文压缩未启用"
    if reason == "no_change":
        return "上下文无需压缩（消息过少或已在保护范围内）"
    if reason:
        return f"压缩未执行: {reason}"
    return "压缩未执行"


async def _compress_status_stream(content: str) -> AsyncIterator[ChatEvent]:
    yield ChatEvent(ChatEventType.MESSAGE_START)
    yield ChatEvent(ChatEventType.CONTENT_DELTA, content=content)
    yield ChatEvent(ChatEventType.MESSAGE_DONE, finish_reason="stop")
    yield ChatEvent(ChatEventType.DONE)

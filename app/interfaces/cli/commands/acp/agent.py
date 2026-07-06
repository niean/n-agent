"""NAgentACPAgent -- ACP Agent binding N-Agent runtime to the ACP JSON-RPC server.

T12 integration point: ties together T7-T11 (path mapping, auth, event bridge,
session bridge, permission bridge) and the ApplicationServices.gateway_service
for user prompt execution. The ACP SDK's ``acp.run_agent`` drives the stdio
JSON-RPC loop and dispatches requests to this agent's coroutine methods.

Design invariants:
- ``prompt`` is serialized per-session via ``_session_locks``; concurrent
  prompts on the same session return ``PromptResponse(stop_reason="refusal")``
  after emitting a busy agent update.
- ``prompt`` rejects unknown sessions or non-acp sessions with refusal -- it
  never implicitly creates an api session via ChatCompletionService.
- ``set_session_model`` and ``set_session_mode`` only mutate ACP metadata;
  they do NOT swap providers or invoke ProviderService.
- ``cancel`` delegates to ``chat_service.graph_runner.interrupt(session_id)``
  (T5) which cancels the running stream task.
- ``session/prompt`` is routed through GatewayService so ACP user messages
  share the same interaction path as Feishu IM and CLI/TUI. ACP protocol
  lifecycle, session bridge, event bridge, and permission bridge stay here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import uuid4

from acp.interfaces import Agent, Client
from acp.meta import PROTOCOL_VERSION
from acp.schema import (
    AgentCapabilities,
    AuthenticateResponse,
    CloseSessionResponse,
    ForkSessionResponse,
    ImageContentBlock,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    ModelInfo,
    NewSessionResponse,
    PromptCapabilities,
    PromptResponse,
    ResumeSessionResponse,
    SessionCapabilities,
    SessionForkCapabilities,
    SessionInfo,
    SessionListCapabilities,
    SessionMode,
    SessionModeState,
    SessionModelState,
    SessionResumeCapabilities,
    SetSessionConfigOptionResponse,
    SetSessionModeResponse,
    SetSessionModelResponse,
    TextContentBlock,
)

from app.application.events import ChatEventType
from app.config import Settings
from app.domain.gateway import GatewaySessionKey, InteractionMessage
from app.interfaces.cli.commands.acp.auth import (
    ProviderSnapshot,
    authenticate as auth_authenticate,
    build_auth_methods,
)
from app.interfaces.cli.commands.acp.event_bridge import ACPEventBridge
from app.interfaces.cli.commands.acp.path_mapping import map_cwd
from app.interfaces.cli.commands.acp.permission_bridge import ACPPermissionBridge
from app.interfaces.cli.commands.acp.session_bridge import ACPSessionBridge, new_acp_session_id

logger = logging.getLogger(__name__)

_VALID_MODES = {"default", "safe_only"}


class NAgentACPAgent(Agent):
    """ACP Agent implementation backed by N-Agent ApplicationServices."""

    def __init__(self, services: Any, settings: Settings) -> None:
        # ``services`` is an ApplicationServices dataclass; typed as Any to
        # avoid importing the frozen dataclass here (avoids a circular import
        # path through app.main).
        self.services = services
        self.settings = settings
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._conn: Client | None = None
        self._session_bridge = ACPSessionBridge(
            services.session_service,
            services.memory_store,
        )
        self._auth_methods: list[Any] = []
        self._permission_bridge: ACPPermissionBridge | None = None

    # ---- Connection lifecycle ---------------------------------------------

    def on_connect(self, conn: Client) -> None:
        # SYNC per SDK contract; store conn for later use by prompt/cancel.
        self._conn = conn
        self._permission_bridge = ACPPermissionBridge(
            conn,
            metadata_updater=self._persist_session_permission,
        )

    def _persist_session_permission(
        self,
        session_id: str,
        tool_name: str,
        scope: str,
    ) -> Any:
        """Best-effort persist allow_session into ACP metadata.

        Bridge calls this on allow_session outcome; subsequent prompts read
        ``allowed_confirm_tools`` from metadata and skip the bridge entirely.
        """
        async def _apply() -> None:
            session = await self.services.memory_store.get_session(session_id)
            if session is None or session.source != "acp":
                return
            metadata = dict(session.acp_metadata or {})
            allowed = dict(metadata.get("allowed_confirm_tools") or {})
            allowed[tool_name] = scope
            metadata["allowed_confirm_tools"] = allowed
            await self.services.memory_store.update_session_acp_metadata(session_id, metadata)

        return _apply()

    # ---- Initialize / authenticate ----------------------------------------

    async def initialize(
        self,
        protocol_version: int | None = None,
        client_capabilities: Any | None = None,
        client_info: Any | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        snapshot = self._provider_snapshot()
        self._auth_methods = build_auth_methods(snapshot)
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_info=Implementation(
                name="n-agent",
                title="N-Agent",
                version=self._agent_version(),
            ),
            agent_capabilities=AgentCapabilities(
                load_session=True,
                prompt_capabilities=PromptCapabilities(image=True),
                session_capabilities=SessionCapabilities(
                    fork=SessionForkCapabilities(),
                    list=SessionListCapabilities(),
                    resume=SessionResumeCapabilities(),
                ),
            ),
            auth_methods=self._auth_methods,
        )

    async def authenticate(
        self,
        method_id: str,
        **kwargs: Any,
    ) -> AuthenticateResponse | None:
        return await auth_authenticate(method_id, self._auth_methods)

    def _provider_snapshot(self) -> ProviderSnapshot:
        holder = self.services.provider_holder
        config = holder.current_config
        if config is None:
            return ProviderSnapshot(name=None, has_api_key=False)
        return ProviderSnapshot(
            name=config.name,
            has_api_key=bool(config.api_key_present),
        )

    def _agent_version(self) -> str:
        try:
            from app import __version__  # type: ignore[import-not-found]
            return __version__
        except Exception:
            return "0.1.0"

    # ---- Session lifecycle -------------------------------------------------

    async def new_session(
        self,
        cwd: str,
        mcp_servers: list | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        mapped = map_cwd(cwd, self.settings)
        if mapped is None:
            # Unmappable cwd: refuse session creation. ACP SDK treats a
            # raised exception as a JSON-RPC error, which the client surfaces.
            raise ValueError(f"cannot map host cwd to container: {cwd!r}")
        session_id = new_acp_session_id()
        await self._session_bridge.create(
            session_id,
            cwd=mapped,
            host_cwd=cwd,
            mode="default",
        )
        self._session_locks[session_id] = asyncio.Lock()
        return NewSessionResponse(
            session_id=session_id,
            models=self._build_model_state(),
            modes=self._build_mode_state(),
        )

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse | None:
        session = await self._session_bridge.load(session_id)
        if session is None:
            return None
        self._session_locks.setdefault(session_id, asyncio.Lock())
        await self._replay_history(session_id)
        return LoadSessionResponse(
            models=self._build_model_state(),
            modes=self._build_mode_state(),
        )

    async def resume_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list | None = None,
        **kwargs: Any,
    ) -> ResumeSessionResponse:
        mapped = map_cwd(cwd, self.settings)
        if mapped is None:
            raise ValueError(f"cannot map host cwd to container: {cwd!r}")
        session = await self._session_bridge.resume(session_id, cwd=mapped, host_cwd=cwd)
        if session is None:
            # Resume creates a new acp session if missing; if explicit rejection
            # is desired, callers should use load_session.
            session_id = new_acp_session_id()
            await self._session_bridge.create(
                session_id, cwd=mapped, host_cwd=cwd, mode="default",
            )
        self._session_locks.setdefault(session_id, asyncio.Lock())
        await self._replay_history(session_id)
        return ResumeSessionResponse(
            models=self._build_model_state(),
            modes=self._build_mode_state(),
        )

    async def fork_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list | None = None,
        **kwargs: Any,
    ) -> ForkSessionResponse:
        forked_id = await self._session_bridge.fork(session_id)
        if forked_id is None:
            raise ValueError(f"cannot fork session {session_id!r} (missing or non-acp)")
        self._session_locks.setdefault(forked_id, asyncio.Lock())
        return ForkSessionResponse(
            session_id=forked_id,
            models=self._build_model_state(),
            modes=self._build_mode_state(),
        )

    async def list_sessions(
        self,
        cursor: str | None = None,
        cwd: str | None = None,
        **kwargs: Any,
    ) -> ListSessionsResponse:
        sessions, next_cursor = await self._session_bridge.list(cwd=cwd, cursor=cursor)
        infos = [
            SessionInfo(
                cwd=s.acp_metadata.get("cwd", "") if s.acp_metadata else "",
                sessionId=s.id,
                title=s.title or "",
                updatedAt=s.updated_at.isoformat() if s.updated_at else None,
            )
            for s in sessions
        ]
        return ListSessionsResponse(sessions=infos, next_cursor=next_cursor)

    async def close_session(
        self,
        session_id: str,
        **kwargs: Any,
    ) -> CloseSessionResponse | None:
        def _cleanup(sid: str) -> None:
            self._session_locks.pop(sid, None)

        await self._session_bridge.close(session_id, cleanup_callback=_cleanup)
        return CloseSessionResponse()

    # ---- Session config (mode/model/options) ------------------------------

    async def set_session_mode(
        self,
        mode_id: str,
        session_id: str,
        **kwargs: Any,
    ) -> SetSessionModeResponse | None:
        if mode_id not in _VALID_MODES:
            return None
        await self._update_metadata(session_id, {"mode": mode_id})
        return SetSessionModeResponse()

    async def set_session_model(
        self,
        model_id: str,
        session_id: str,
        **kwargs: Any,
    ) -> SetSessionModelResponse | None:
        # S 7: only update metadata; do NOT call ProviderService.activate/swap.
        await self._update_metadata(session_id, {"model": model_id})
        return SetSessionModelResponse()

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: str | bool,
        **kwargs: Any,
    ) -> SetSessionConfigOptionResponse | None:
        session = await self.services.memory_store.get_session(session_id)
        if session is None or session.source != "acp":
            return None
        metadata = dict(session.acp_metadata or {})
        options = dict(metadata.get("config_options") or {})
        options[config_id] = value
        metadata["config_options"] = options
        await self.services.memory_store.update_session_acp_metadata(session_id, metadata)
        # S 9: do not advertise typed config surface; return empty list.
        return SetSessionConfigOptionResponse(config_options=[])

    async def _update_metadata(self, session_id: str, patch: dict[str, Any]) -> None:
        session = await self.services.memory_store.get_session(session_id)
        if session is None or session.source != "acp":
            return
        metadata = dict(session.acp_metadata or {})
        metadata.update(patch)
        await self.services.memory_store.update_session_acp_metadata(session_id, metadata)

    # ---- Prompt (core) -----------------------------------------------------

    async def prompt(
        self,
        prompt: list[Any],
        session_id: str,
        message_id: str | None = None,
        **kwargs: Any,
    ) -> PromptResponse:
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            await self._emit_busy(session_id)
            return PromptResponse(stop_reason="refusal")

        async with lock:
            session = await self._session_bridge.load(session_id)
            if session is None:
                return PromptResponse(stop_reason="refusal")

            metadata = session.acp_metadata or {}
            mapped_cwd = metadata.get("cwd", "")
            mode = metadata.get("mode", "default")
            model = metadata.get("model") or self.services.provider_holder.current_model or "default"
            allowed_confirm = dict(metadata.get("allowed_confirm_tools") or {})

            text, images = self._content_from_prompt(prompt)
            if not text and not images:
                return PromptResponse(stop_reason="end_turn")

            if self._conn is None:
                return PromptResponse(stop_reason="refusal")

            event_bridge = ACPEventBridge(self._conn, session_id)
            # Do NOT emit_user_message here: VsCode ACP clients optimistically
            # render the prompt text on send, so a server-side UserMessageChunk
            # duplicates it ("123" -> "123123"). UserMessageChunk is only needed
            # in replay_history (session/load), where the client has no local
            # copy and must reconstruct the transcript from server updates.

            options: dict[str, Any] = {
                "execution_context_mode": "realtime",
                "tool_exposure_policy": "safe_only" if mode == "safe_only" else "all",
            }
            session_key = GatewaySessionKey("acp", session_id, display_name=session_id)
            await self.services.gateway_registry.set_active_session(session_key, session_id)
            interaction = InteractionMessage(
                id=message_id or f"acp-{uuid4()}",
                session_key=session_key,
                text=text,
                images=images,
                metadata={
                    "actor_id": f"acp:{session_id}",
                    "message_id": message_id or "",
                    "acp.cwd": mapped_cwd,
                },
            )

            stop_reason = "end_turn"
            try:
                stream = self.services.gateway_service.handle_message_stream(
                    interaction,
                    model_override=model,
                    options_override=options,
                    trusted_metadata_override={
                        "acp.cwd": mapped_cwd,
                        "agent_context": "primary",
                    },
                    approval_decider=self._permission_bridge,
                    allowed_confirm_tools_override=allowed_confirm,
                )
                async for event in stream:
                    if event.type is ChatEventType.ERROR:
                        if event.finish_reason == "cancelled":
                            stop_reason = "cancelled"
                        else:
                            stop_reason = "refusal"
                    await event_bridge.emit_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("prompt failed for session %s", session_id)
                await event_bridge.emit_event(
                    _make_error_event(str(exc))
                )
                stop_reason = "refusal"

            return PromptResponse(stop_reason=stop_reason)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        self.services.chat_service.graph_runner.interrupt(session_id)

    # ---- Helpers -----------------------------------------------------------

    async def _emit_busy(self, session_id: str) -> None:
        if self._conn is None:
            return
        bridge = ACPEventBridge(self._conn, session_id)
        await bridge.emit_event(_make_error_event("session busy; another prompt is in flight"))

    async def _replay_history(self, session_id: str) -> None:
        if self._conn is None:
            return
        messages = await self.services.memory_store.list_messages(session_id)
        tool_calls = await self.services.memory_store.list_tool_calls(session_id)
        bridge = ACPEventBridge(self._conn, session_id)
        await bridge.replay_history(messages, tool_calls)

    @staticmethod
    def _content_from_prompt(prompt: list[Any]) -> tuple[str, list[str]]:
        """Extract text and image data URLs from an ACP prompt block list.

        Returns (text, images) where images is a list of data URLs
        (``data:{mime_type};base64,{data}``). ImageContentBlock carries
        base64 data per ACP SDK contract; http(s) URLs are not represented
        as image blocks and are skipped here.
        """
        text_parts: list[str] = []
        images: list[str] = []
        for block in prompt:
            if isinstance(block, TextContentBlock) and block.text:
                text_parts.append(block.text)
            elif isinstance(block, ImageContentBlock) and block.data:
                mime = block.mime_type or "image/png"
                images.append(f"data:{mime};base64,{block.data}")
        return "\n".join(text_parts).strip(), images

    def _build_model_state(self) -> SessionModelState:
        holder = self.services.provider_holder
        current = holder.current_model
        available: list[ModelInfo] = []
        config = holder.current_config
        if config is not None:
            available.append(
                ModelInfo(
                    model_id=config.model,
                    name=config.name or config.model,
                    description=None,
                )
            )
        return SessionModelState(available_models=available, current_model_id=current)

    def _build_mode_state(self) -> SessionModeState:
        return SessionModeState(
            available_modes=[
                SessionMode(id="default", name="Default", description="All tools exposed."),
                SessionMode(
                    id="safe_only",
                    name="Safe only",
                    description="Only safe tools exposed; CONFIRM tools hidden.",
                ),
            ],
            current_mode_id="default",
        )


def _make_error_event(message: str):
    from app.application.events import ChatEvent
    return ChatEvent(ChatEventType.ERROR, error=message)

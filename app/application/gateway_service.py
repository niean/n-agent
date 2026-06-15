from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from app.application.chat_service import ChatCompletionInput, ChatCompletionResult, ChatCompletionService
from app.application.model_service import ModelService
from app.application.session_service import SessionService
from app.application.tool_service import ToolService
from app.domain.gateway import (
    GatewayOutboundMessage,
    GatewaySessionRegistry,
    InteractionMessage,
    InteractionResponse,
)

HealthProvider = Callable[[], dict[str, Any]]


class GatewayCommandService:
    def __init__(
        self,
        registry: GatewaySessionRegistry,
        session_service: SessionService,
        tool_service: ToolService,
        model_service: ModelService,
        health_provider: HealthProvider,
    ):
        self.registry = registry
        self.session_service = session_service
        self.tool_service = tool_service
        self.model_service = model_service
        self.health_provider = health_provider

    async def handle(self, event: InteractionMessage, session_id: str) -> InteractionResponse | None:
        text = event.text.strip()
        if not text.startswith("/"):
            return None
        command, _, arg = text.partition(" ")
        if command == "/help":
            return _response(session_id, "可用命令: /help, /new, /switch <session_id>, /sessions, /tools, /models, /status")
        if command == "/new":
            new_session_id = f"gateway-{uuid4()}"
            await self.session_service.create_session(new_session_id, source=event.session_key.source_type.value)
            await self.registry.create_session_link(event.session_key, new_session_id)
            return _response(new_session_id, f"已创建新会话 {new_session_id}")
        if command == "/switch":
            target = arg.strip()
            if not target:
                return _response(session_id, "用法: /switch <session_id>")
            link = await self.registry.set_active_session(event.session_key, target)
            return _response(link.session_id, f"已切换到会话 {link.session_id}")
        if command == "/sessions":
            links = await self.registry.list_session_links(event.session_key)
            if not links:
                return _response(session_id, "暂无会话")
            return _response(session_id, "\n".join(link.session_id for link in links))
        if command == "/tools":
            definitions = self.tool_service.list_definitions()
            content = "\n".join(definition.name for definition in definitions) or "暂无工具"
            return _response(session_id, content)
        if command == "/models":
            models = await self.model_service.list_models()
            content = "\n".join(model.id for model in models) or "暂无模型"
            return _response(session_id, content)
        if command == "/status":
            return _response(session_id, str(self.health_provider()))
        return _response(session_id, "未知命令，输入 /help 查看可用命令")


class GatewayService:
    def __init__(
        self,
        registry: GatewaySessionRegistry,
        chat_service: ChatCompletionService,
        session_service: SessionService,
        tool_service: ToolService,
        model_service: ModelService,
        health_provider: HealthProvider,
    ):
        self.registry = registry
        self.chat_service = chat_service
        self.session_service = session_service
        self.command_service = GatewayCommandService(
            registry,
            session_service,
            tool_service,
            model_service,
            health_provider,
        )

    async def handle_message(self, event: InteractionMessage) -> InteractionResponse:
        message_id = str(event.metadata.get("message_id", ""))
        processed = await self.registry.mark_event_processed(event.session_key.source_type, event.id, message_id)
        if not processed:
            return InteractionResponse(session_id="", messages=[], metadata={"duplicate": True})

        session_id = await self._resolve_session_id(event)
        command_response = await self.command_service.handle(event, session_id)
        if command_response is not None:
            return command_response

        result = await self.chat_service.complete(
            ChatCompletionInput(
                model="N-Agent",
                messages=[{"role": "user", "content": event.text}],
                stream=False,
                metadata={
                    "gateway": {
                        "source_type": event.session_key.source_type.value,
                        "source_id": event.session_key.source_id,
                        "thread_id": event.session_key.thread_id,
                    }
                },
                session_id=session_id,
            )
        )
        assert isinstance(result, ChatCompletionResult)
        return _response(result.session_id, str(result.message.get("content", "")))

    async def _resolve_session_id(self, event: InteractionMessage) -> str:
        link = await self.registry.get_active_session(event.session_key)
        if link is not None:
            return link.session_id
        session_id = f"gateway-{uuid4()}"
        await self.session_service.create_session(session_id, source=event.session_key.source_type.value)
        await self.registry.create_session_link(event.session_key, session_id)
        return session_id


def _response(session_id: str, content: str, metadata: dict[str, Any] | None = None) -> InteractionResponse:
    return InteractionResponse(
        session_id=session_id,
        messages=[GatewayOutboundMessage(content=content)],
        metadata=metadata or {},
    )

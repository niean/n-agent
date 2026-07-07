from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.application.chat_service import ChatCompletionInput, ChatCompletionResult, ChatCompletionService
from app.application.events import ChatEvent, ChatEventType
from app.application.model_service import ModelService
from app.application.schedule_service import ScheduledTaskCreateInput, ScheduleService
from app.application.session_service import SessionService
from app.application.tool_service import ToolService
from app.domain.gateway import (
    GatewayConfirmationAction,
    GatewayConfirmationChoice,
    GatewayConfirmationRequest,
    GatewayHomeTarget,
    GatewayOutboundMessage,
    GatewaySessionKey,
    GatewaySessionRegistry,
    InteractionMessage,
    InteractionResponse,
)
from app.domain.platform import Platform
from app.domain.session import SessionSource

HealthProvider = Callable[[], dict[str, Any]]


class GatewayCommandService:
    def __init__(
        self,
        registry: GatewaySessionRegistry,
        session_service: SessionService,
        tool_service: ToolService,
        model_service: ModelService,
        health_provider: HealthProvider,
        schedule_service: ScheduleService | None = None,
    ):
        self.registry = registry
        self.session_service = session_service
        self.tool_service = tool_service
        self.model_service = model_service
        self.health_provider = health_provider
        self.schedule_service = schedule_service
        self.pending_confirmations: dict[str, GatewayConfirmationRequest] = {}
        self.trusted_actors: set[tuple[str, str, str, str]] = set()
        self.confirmation_ttl = timedelta(minutes=15)

    async def handle_destructive_preflight(self, event: InteractionMessage) -> InteractionResponse | None:
        parsed = self._parse_destructive_command(event.text)
        if parsed is None:
            return None
        action, args = parsed
        active = await self.registry.get_active_session(event.session_key)
        if action is not GatewayConfirmationAction.NEW and active is None:
            return _response("", "没有当前会话，请先发送普通消息或使用 /new 创建会话")
        session_id = active.session_id if active is not None else ""
        actor_value = event.metadata.get("actor_id")
        if actor_value is None and action is GatewayConfirmationAction.NEW:
            return await self._execute_new(event)
        actor_id = str(actor_value if actor_value is not None else event.session_key.display_name or event.session_key.platform_session_id)
        if self._trust_key(event.session_key, actor_id) in self.trusted_actors:
            return await self._execute(action, event, session_id, args, _build_trusted_metadata(event))
        now = datetime.now(timezone.utc)
        self._cleanup_expired(now)
        confirmation = GatewayConfirmationRequest(
            id=f"confirm-{uuid4()}",
            session_key=event.session_key,
            actor_id=actor_id,
            session_id=session_id,
            target_session_id=session_id,
            action=action,
            command=event.text.strip(),
            args=args,
            created_at=now,
            expires_at=now + self.confirmation_ttl,
            trusted_metadata=_build_trusted_metadata(event),
        )
        self.pending_confirmations[confirmation.id] = confirmation
        return _response(
            session_id,
            f"请确认执行 {confirmation.command}",
            {"confirmation": _confirmation_metadata(confirmation)},
        )

    async def handle(self, event: InteractionMessage, session_id: str) -> InteractionResponse | None:
        text = event.text.strip()
        if not text.startswith("/"):
            return None
        command, _, arg = text.partition(" ")
        if command == "/help":
            return _response(session_id, "可用命令: /help, /new, /rename <title>, /delete, /switch <session_id>, /sessions, /tools, /models, /status, /sethome, /schedule help")
        if command == "/sethome":
            if event.session_key.platform is None:
                return _response(session_id, "当前入口不支持 home chat")
            target = _home_target_from_event(event)
            await self.registry.set_home_target(target)
            return _response(session_id, f"已设置 {target.platform.value} home chat: {target.receive_id}")
        if command == "/rename" and not arg.strip():
            return _response(session_id, "用法: /rename <title>")
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
            return _response(session_id, json.dumps(self.health_provider(), ensure_ascii=False))
        if command == "/schedule":
            return await self._handle_schedule(arg, event, session_id)
        return _response(session_id, "未知命令，输入 /help 查看可用命令")

    async def handle_confirmation(
        self,
        session_key: GatewaySessionKey,
        actor_id: str,
        confirmation_id: str,
        choice: GatewayConfirmationChoice,
    ) -> InteractionResponse:
        now = datetime.now(timezone.utc)
        self._cleanup_expired(now)
        confirmation = self.pending_confirmations.get(confirmation_id)
        if confirmation is None or confirmation.expires_at <= now:
            self.pending_confirmations.pop(confirmation_id, None)
            return _response("", "确认已失效")
        if confirmation.session_key.conversation_parts != session_key.conversation_parts:
            return _response(confirmation.session_id, "确认已失效")
        if confirmation.actor_id != actor_id:
            return _response(confirmation.session_id, "只有命令发起者可以确认")
        self.pending_confirmations.pop(confirmation_id, None)
        if choice is GatewayConfirmationChoice.CANCEL:
            return _response(confirmation.session_id, "已取消")
        if choice is GatewayConfirmationChoice.TRUST_SESSION:
            self.trusted_actors.add(self._trust_key(session_key, actor_id))
        return await self._execute(
            confirmation.action,
            InteractionMessage("", session_key, confirmation.command),
            confirmation.session_id,
            confirmation.args,
            confirmation.trusted_metadata,
        )

    def discard_confirmation(self, confirmation_id: str) -> None:
        self.pending_confirmations.pop(confirmation_id, None)

    async def _handle_schedule(self, arg: str, event: InteractionMessage, session_id: str) -> InteractionResponse:
        if self.schedule_service is None:
            return _response(session_id, "任务服务未启用")
        action, _, rest = arg.strip().partition(" ")
        if action in ("", "help"):
            return _response(session_id, "用法: /schedule add <cron> <prompt> | list | pause <id> | resume <id> | run <id> | remove <id>")
        if action == "add":
            parsed = _parse_schedule_add(rest)
            if parsed is None:
                return _response(session_id, "用法: /schedule add <cron> <prompt>")
            cron_expression, prompt = parsed
            origin = await self._schedule_origin(event)
            task = await self.schedule_service.create(
                ScheduledTaskCreateInput(
                    name=prompt[:40] or "Scheduled Task",
                    prompt=prompt,
                    cron_expression=cron_expression,
                    delivery_target="origin",
                    origin=origin,
                )
            )
            return _response(session_id, f"已创建任务 {task.id}")
        if action == "list":
            tasks = await self.schedule_service.list()
            content = "\n".join(f"{task.id} {task.name} {task.next_run_at.isoformat()}" for task in tasks) or "暂无任务"
            return _response(session_id, content)
        if action == "pause":
            await self.schedule_service.pause(rest.strip())
            return _response(session_id, "已暂停")
        if action == "resume":
            await self.schedule_service.resume(rest.strip())
            return _response(session_id, "已恢复")
        if action == "run":
            result = await self.schedule_service.run_now(rest.strip())
            return _response(session_id, str(result))
        if action == "remove" and not rest.strip():
            return _response(session_id, "用法: /schedule remove <id>")
        return _response(session_id, "未知 /schedule 命令")

    async def _schedule_origin(self, event: InteractionMessage) -> dict[str, Any]:
        platform = event.session_key.platform
        if platform is Platform.FEISHU:
            target = await self.registry.get_home_target(platform)
            if target is None:
                target = await self.registry.set_home_target(_home_target_from_event(event))
            return {
                "platform": target.platform.value,
                "target": "home",
            }
        origin = dict(event.metadata)
        if platform is not None:
            origin["platform"] = platform.value
        else:
            origin["source"] = event.session_key.source_value
        origin.setdefault("thread_id", event.session_key.thread_id)
        return origin

    def _parse_destructive_command(self, text: str) -> tuple[GatewayConfirmationAction, dict[str, Any]] | None:
        command, _, arg = text.strip().partition(" ")
        if command == "/new":
            return GatewayConfirmationAction.NEW, {}
        if command == "/rename":
            title = arg.strip()
            if not title:
                return None
            return GatewayConfirmationAction.RENAME, {"title": title}
        if command == "/delete":
            return GatewayConfirmationAction.DELETE, {}
        if command == "/schedule":
            action, _, rest = arg.strip().partition(" ")
            if action == "remove" and rest.strip():
                return GatewayConfirmationAction.SCHEDULE_REMOVE, {"task_id": rest.strip()}
        return None

    async def _execute(
        self,
        action: GatewayConfirmationAction,
        event: InteractionMessage,
        session_id: str,
        args: dict[str, Any],
        trusted_metadata: dict[str, Any] | None = None,
    ) -> InteractionResponse:
        if action is GatewayConfirmationAction.NEW:
            return await self._execute_new(event)
        if action is GatewayConfirmationAction.RENAME:
            await self.session_service.rename_session(session_id, str(args["title"]))
            return _response(session_id, f"已重命名为 {args['title']}")
        if action is GatewayConfirmationAction.DELETE:
            await self.registry.delete_session_link(session_id)
            await self.session_service.delete_session(session_id)
            return await self._execute_new(event)
        if action is GatewayConfirmationAction.SCHEDULE_REMOVE:
            return await self._execute_schedule_remove(session_id, args, trusted_metadata or {})
        raise ValueError(f"unsupported confirmation action: {action}")

    async def _execute_schedule_remove(
        self,
        session_id: str,
        args: dict[str, Any],
        trusted_metadata: dict[str, Any],
    ) -> InteractionResponse:
        if self.schedule_service is None:
            return _response(session_id, "任务服务未启用")
        task_id = str(args.get("task_id") or "")
        expected_platform = str(trusted_metadata.get("platform") or trusted_metadata.get("gateway.platform") or "")
        expected_receive_id = str(trusted_metadata.get("receive_id") or "")
        expected_type = str(trusted_metadata.get("receive_id_type") or "")
        expected_thread = str(trusted_metadata.get("thread_id") or "")
        try:
            task = await self.schedule_service.get(task_id)
        except Exception:
            return _response(session_id, "任务不存在")
        origin = task.origin or {}
        if (
            str(origin.get("platform") or "") != expected_platform
            or str(origin.get("receive_id") or "") != expected_receive_id
            or str(origin.get("receive_id_type") or "") != expected_type
            or str(origin.get("thread_id") or "") != expected_thread
        ):
            return _response(session_id, "任务不存在")
        await self.schedule_service.delete(task_id)
        return _response(session_id, "已删除")

    async def _execute_new(self, event: InteractionMessage) -> InteractionResponse:
        prefix, source = _session_id_prefix_and_source(event.session_key)
        new_session_id = f"{prefix}-{uuid4()}"
        await self.session_service.create_session(new_session_id, source=source)
        await self.registry.create_session_link(event.session_key, new_session_id)
        return _response(new_session_id, f"已创建新会话 {new_session_id}")

    def _trust_key(self, session_key: GatewaySessionKey, actor_id: str) -> tuple[str, str, str, str]:
        return (*session_key.conversation_parts, actor_id)

    def _cleanup_expired(self, now: datetime) -> None:
        expired = [key for key, value in self.pending_confirmations.items() if value.expires_at <= now]
        for key in expired:
            self.pending_confirmations.pop(key, None)


def _session_id_prefix_and_source(key: GatewaySessionKey) -> tuple[str, str]:
    if key.source_value == SessionSource.ACP.value:
        return (SessionSource.ACP.value, SessionSource.ACP.value)
    platform = key.platform
    if platform is None:
        return (SessionSource.CLI.value, SessionSource.CLI.value)
    return (platform.value, platform.value)


class GatewayService:
    def __init__(
        self,
        registry: GatewaySessionRegistry,
        chat_service: ChatCompletionService,
        session_service: SessionService,
        tool_service: ToolService,
        model_service: ModelService,
        health_provider: HealthProvider,
        schedule_service: ScheduleService | None = None,
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
            schedule_service,
        )

    async def handle_message(self, event: InteractionMessage) -> InteractionResponse:
        message_id = str(event.metadata.get("message_id", ""))
        processed = await self.registry.mark_event_processed(event.session_key.source_value, event.id, message_id)
        if not processed:
            return InteractionResponse(session_id="", messages=[], metadata={"duplicate": True})

        slash_with_images = event.text.strip().startswith("/") and bool(event.images)
        if slash_with_images:
            return _response("", "slash 命令不支持附带图片")

        preflight = await self.command_service.handle_destructive_preflight(event)
        if preflight is not None:
            return preflight

        session_id = await self._resolve_session_id(event)
        command_response = await self.command_service.handle(event, session_id)
        if command_response is not None:
            return command_response

        result = await self.chat_service.complete(
            ChatCompletionInput(
                model=self.command_service.model_service.default_model,
                messages=[{"role": "user", "content": _content_from_interaction(event)}],
                stream=False,
                metadata={
                    "gateway": _gateway_metadata(event),
                },
                trusted_metadata=_build_trusted_metadata(event),
                session_id=session_id,
            )
        )
        assert isinstance(result, ChatCompletionResult)
        return _response(result.session_id, str(result.message.get("content", "")))

    async def handle_confirmation(
        self,
        session_key: GatewaySessionKey,
        actor_id: str,
        confirmation_id: str,
        choice: GatewayConfirmationChoice,
    ) -> InteractionResponse:
        return await self.command_service.handle_confirmation(session_key, actor_id, confirmation_id, choice)

    async def handle_message_stream(
        self,
        event: InteractionMessage,
        *,
        model_override: str | None = None,
        options_override: dict[str, Any] | None = None,
        trusted_metadata_override: dict[str, Any] | None = None,
        approval_decider: Any | None = None,
        allowed_confirm_tools_override: dict[str, Any] | None = None,
    ) -> AsyncIterator[ChatEvent]:
        """流式版本：复用幂等、destructive preflight、session 解析、Slash 分流。"""
        message_id = str(event.metadata.get("message_id", ""))
        processed = await self.registry.mark_event_processed(
            event.session_key.source_value, event.id, message_id,
        )
        if not processed:
            yield ChatEvent(ChatEventType.DONE, metadata={"duplicate": True})
            return

        slash_with_images = event.text.strip().startswith("/") and bool(event.images)
        if slash_with_images:
            yield ChatEvent(
                ChatEventType.MESSAGE_DONE,
                content="slash 命令不支持附带图片",
                finish_reason="stop",
            )
            yield ChatEvent(ChatEventType.DONE)
            return

        preflight = await self.command_service.handle_destructive_preflight(event)
        if preflight is not None:
            outbound_meta = preflight.messages[0].metadata if preflight.messages else {}
            yield ChatEvent(
                ChatEventType.MESSAGE_DONE,
                content=_first_message_content(preflight),
                finish_reason="confirmation_required",
                metadata=dict(outbound_meta),
            )
            yield ChatEvent(ChatEventType.DONE)
            return

        session_id = await self._resolve_session_id(event)
        command_response = await self.command_service.handle(event, session_id)
        if command_response is not None:
            for msg in command_response.messages:
                yield ChatEvent(
                    ChatEventType.MESSAGE_DONE,
                    content=msg.content,
                    finish_reason="stop",
                    metadata=dict(msg.metadata or {}),
                )
            yield ChatEvent(ChatEventType.DONE)
            return

        trusted_metadata = _build_trusted_metadata(event)
        if trusted_metadata_override:
            trusted_metadata.update(trusted_metadata_override)
        stream = await self.chat_service.complete(
            ChatCompletionInput(
                model=model_override or self.command_service.model_service.default_model,
                messages=[{"role": "user", "content": _content_from_interaction(event)}],
                stream=True,
                metadata={
                    "gateway": _gateway_metadata(event),
                },
                trusted_metadata=trusted_metadata,
                session_id=session_id,
                options=dict(options_override or {}),
                approval_decider=approval_decider,
                allowed_confirm_tools_override=allowed_confirm_tools_override,
            )
        )
        if isinstance(stream, ChatCompletionResult):
            yield ChatEvent(
                ChatEventType.ERROR,
                error="chat service did not return stream iterator",
            )
            yield ChatEvent(ChatEventType.DONE)
            return
        async for evt in stream:
            yield evt

    def discard_confirmation(self, confirmation_id: str) -> None:
        self.command_service.discard_confirmation(confirmation_id)

    async def _resolve_session_id(self, event: InteractionMessage) -> str:
        link = await self.registry.get_active_session(event.session_key)
        if link is not None:
            return link.session_id
        prefix, source = _session_id_prefix_and_source(event.session_key)
        session_id = f"{prefix}-{uuid4()}"
        await self.session_service.create_session(session_id, source=source)
        await self.registry.create_session_link(event.session_key, session_id)
        return session_id


def _parse_schedule_add(rest: str) -> tuple[str, str] | None:
    if " -- " in rest:
        cron_expression, prompt = rest.split(" -- ", 1)
        return (cron_expression.strip(), prompt.strip()) if cron_expression.strip() and prompt.strip() else None
    parts = rest.split()
    if len(parts) < 6:
        return None
    return " ".join(parts[:5]), " ".join(parts[5:])


def _home_target_from_event(event: InteractionMessage) -> GatewayHomeTarget:
    md = dict(event.metadata)
    platform = event.session_key.platform
    if platform is None:
        raise ValueError(f"source does not support home target: {event.session_key.source_value}")
    return GatewayHomeTarget(
        platform=platform,
        receive_id=str(md.get("receive_id") or event.session_key.platform_session_id),
        receive_id_type=str(md.get("receive_id_type") or "chat_id"),
        thread_id=str(md.get("thread_id") or event.session_key.thread_id or ""),
        display_name=event.session_key.display_name,
    )


def _build_trusted_metadata(event: InteractionMessage) -> dict[str, Any]:
    md = dict(event.metadata)
    trusted = {
        "gateway.source": event.session_key.source_value,
        "gateway.platform_session_id": event.session_key.platform_session_id,
        "thread_id": str(md.get("thread_id") or event.session_key.thread_id or ""),
        "actor_id": str(md.get("actor_id") or ""),
        "receive_id": str(md.get("receive_id") or ""),
        "receive_id_type": str(md.get("receive_id_type") or ""),
    }
    platform = event.session_key.platform
    if platform is not None:
        trusted["gateway.platform"] = platform.value
        trusted["platform"] = platform.value
    return trusted


def _gateway_metadata(event: InteractionMessage) -> dict[str, str]:
    metadata = {
        "source": event.session_key.source_value,
        "platform_session_id": event.session_key.platform_session_id,
        "thread_id": event.session_key.thread_id,
    }
    platform = event.session_key.platform
    if platform is not None:
        metadata["platform"] = platform.value
    return metadata


def _confirmation_metadata(confirmation: GatewayConfirmationRequest) -> dict[str, Any]:
    meta = {
        "id": confirmation.id,
        "action": confirmation.action.value,
        "command": confirmation.command,
        "expires_at": confirmation.expires_at.isoformat(),
        "platform_session_id": confirmation.session_key.platform_session_id,
        "thread_id": confirmation.session_key.thread_id,
    }
    return meta



def _response(session_id: str, content: str, metadata: dict[str, Any] | None = None) -> InteractionResponse:
    return InteractionResponse(
        session_id=session_id,
        messages=[GatewayOutboundMessage(content=content, metadata=metadata or {})],
    )


def _first_message_content(response: InteractionResponse) -> str:
    return response.messages[0].content if response.messages else ""


def _content_from_interaction(event: InteractionMessage) -> str | list[dict[str, Any]]:
    if not event.images:
        return event.text
    parts: list[dict[str, Any]] = []
    if event.text:
        parts.append({"type": "text", "text": event.text})
    for url in event.images:
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts

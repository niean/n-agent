from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from inspect import isawaitable
from typing import Any, Optional

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph

from app.application.context_service import ContextService
from app.application.events import ChatEvent, ChatEventType
from app.application.external_memory_manager import ExternalMemoryManager
from app.application.tool_service import (
    ToolExecutionEvaluation,
    ToolNotFoundError,
    ToolService,
)
from app.domain.agent import AgentState, RunStatus
from app.domain.context import ContextEngine
from app.domain.memory import MemoryStore, Summarizer
from app.domain.policy import PolicyOutcome
from app.domain.provider import LLMEventType, LLMProvider, LLMResult, resolve_model
from app.domain.session import ConversationMessage, TaskState, ToolCall
from app.domain.tool import (
    ApprovalDecision,
    ApprovalRequest,
    ToolCallRequest,
    ToolExecutionContext,
    ToolResult,
    ToolResultStatus,
)
from app.utils.content_utils import extract_text, has_image_part
from app.utils.memory_scrubber import scrub_memory_context


logger = logging.getLogger(__name__)

# Internal control keys in options that are NOT generation params and must
# not be recorded as part of the Provider Request. Mirrors the filter in
# OpenAICompatibleProvider._INTERNAL_OPTION_KEYS; duplicated here to keep
# the application layer free of infrastructure imports.
_INTERNAL_OPTION_KEYS = {
    "tool_execution_context",
    "tool_exposure_policy",
    "execution_context_mode",
    "external_memory_enabled",
    "stream_event_sink",
}


class AgentGraphRunner:
    def __init__(
        self,
        llm_provider: LLMProvider,
        tool_service: ToolService,
        memory_store: MemoryStore,
        summarizer: Summarizer,
        iteration_limit: int = 10,
        external_memory_manager: ExternalMemoryManager | None = None,
        vision_capability: Optional[Callable[[], bool]] = None,
        context_engine: ContextEngine | None = None,
        context_service: ContextService | None = None,
        usage_service: Any = None,
        skill_service: Any = None,
    ):
        self.llm_provider = llm_provider
        self.tool_service = tool_service
        self.memory_store = memory_store
        self.summarizer = summarizer
        self.iteration_limit = iteration_limit
        self.external_memory_manager = external_memory_manager
        self.vision_capability = vision_capability
        self.usage_service = usage_service
        self.skill_service = skill_service
        self.context_service = context_service or ContextService(
            memory_store,
            tool_service=tool_service,
            external_memory_manager=external_memory_manager,
            context_engine=context_engine,
            usage_service=usage_service,
            skill_service=skill_service,
            is_cancelled=self.is_cancelled,
        )
        self.graph = self._build_graph()
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}

    def register_run(self, session_id: str, task: asyncio.Task) -> None:
        self._running_tasks[session_id] = task
        self._cancel_events[session_id] = asyncio.Event()

    def interrupt(self, session_id: str) -> bool:
        if session_id not in self._running_tasks:
            return False
        event = self._cancel_events.get(session_id)
        if event is not None:
            event.set()
        task = self._running_tasks.get(session_id)
        if task is not None and not task.done():
            task.cancel()
        return True

    def is_cancelled(self, session_id: str) -> bool:
        event = self._cancel_events.get(session_id)
        return event is not None and event.is_set()

    def clear_run(self, session_id: str) -> None:
        self._running_tasks.pop(session_id, None)
        self._cancel_events.pop(session_id, None)

    def _build_graph(self):
        graph = StateGraph(AgentState)
        graph.add_node("prepare_context", self.prepare_context)
        graph.add_node("call_llm", self.call_llm)
        graph.add_node("execute_tools", self.execute_tools)
        graph.add_node("update_memory", self.update_memory)
        graph.add_node("finalize", self.finalize)
        graph.set_entry_point("prepare_context")
        graph.add_edge("prepare_context", "call_llm")
        graph.add_conditional_edges(
            "call_llm",
            self._after_llm,
            {"tools": "execute_tools", "memory": "update_memory", "finalize": "finalize"},
        )
        graph.add_edge("execute_tools", "update_memory")
        graph.add_conditional_edges(
            "update_memory",
            self._after_memory,
            {"continue": "call_llm", "finalize": "finalize"},
        )
        graph.add_edge("finalize", END)
        return graph.compile()

    async def run(self, state: AgentState, model: str, options: dict[str, Any] | None = None) -> AgentState:
        state.run_options = dict(options or {})
        result = await self.graph.ainvoke(state, {"configurable": {"model": model, "options": state.run_options}})
        return AgentState(**result) if isinstance(result, dict) else result

    async def compress_session(self, session_id: str) -> dict[str, Any]:
        """Force compress a session's context without LLM call.

 Used by slash command `/compress` and conversational trigger. Loads existing
 messages, forces compression via run_options, persists summary, returns status.
 Does NOT call LLM, does NOT save user message, does NOT run the full graph.
 """
        if self.context_service.context_engine is None:
            return {"compressed": False, "reason": "context_engine_unavailable"}
        state = AgentState(session_id=session_id, input_messages=[])
        state.run_options = {"force_compress": True}
        state = await self.context_service.build_context_state(state)
        state.run_options = {"force_compress": True}
        before_count = len(state.working_messages)
        before_summary = state.summary
        state = await self.context_service.compress_prepared_context(state)
        after_count = len(state.working_messages)
        after_summary = state.summary
        if after_count < before_count or after_summary != before_summary:
            return {"compressed": True, "reason": None}
        return {"compressed": False, "reason": "no_change"}

    def _split_content_for_streaming(self, content: str) -> list[str]:
        """Split content into small chunks for streaming.
        Matches existing streaming pattern used in other code paths.
        """
        chunks: list[str] = []
        line_len = 0
        current = []
        for line in content.splitlines(keepends=True):
            current.append(line)
            line_len += len(line)
            if line_len >= 20:
                chunks.append("".join(current))
                current = []
                line_len = 0
        if current:
            chunks.append("".join(current))
        return chunks

    async def stream_events(self, state: AgentState, model: str, options: dict[str, Any] | None = None) -> AsyncIterator[ChatEvent]:
        from app.utils.memory_scrubber import StreamingContextScrubber
        yield ChatEvent(ChatEventType.MESSAGE_START)
        scrubber = StreamingContextScrubber()
        stream_options = dict(options or {})
        tool_event_queue: asyncio.Queue[ChatEvent] = asyncio.Queue()

        async def emit_tool_event(event: ChatEvent) -> None:
            await tool_event_queue.put(event)

        stream_options["stream_event_sink"] = emit_tool_event
        run_task = asyncio.create_task(self.run(state, model, stream_options))
        self.register_run(state.session_id, run_task)
        result = None
        try:
            while not run_task.done():
                try:
                    yield await asyncio.wait_for(tool_event_queue.get(), timeout=0.05)
                except asyncio.TimeoutError:
                    continue
            result = await run_task
            while not tool_event_queue.empty():
                yield tool_event_queue.get_nowait()
        except asyncio.CancelledError:
            yield ChatEvent(ChatEventType.ERROR, error="cancelled", finish_reason="cancelled")
            result = None
        finally:
            if not run_task.done():
                run_task.cancel()
                with suppress(asyncio.CancelledError):
                    await run_task
            self.clear_run(state.session_id)
        if result is not None:
            if result.error:
                yield ChatEvent(ChatEventType.ERROR, error=result.error, finish_reason="error")
            elif result.final_message:
                content = str(result.final_message.get("content") or "")
                if content:
                    # scrub each chunk in streaming
                    for chunk in self._split_content_for_streaming(content):
                        scrubbed = scrubber.feed(chunk)
                        if scrubbed:
                            yield ChatEvent(ChatEventType.CONTENT_DELTA, content=scrubbed)
                    scrubbed_final = scrubber.flush()
                    if scrubbed_final:
                        yield ChatEvent(ChatEventType.CONTENT_DELTA, content=scrubbed_final)
                yield ChatEvent(ChatEventType.MESSAGE_DONE, finish_reason=result.finish_reason or "stop")
        yield ChatEvent(ChatEventType.DONE)

    async def prepare_context(self, state: AgentState) -> AgentState:
        return await self.context_service.prepare_context(state)

    def _resolve_usage_meta(self, model: str) -> tuple[str, str | None, str | None, str | None]:
        """Derive (provider_kind, provider_name, real_model, requested_model) for usage recording.

        ActiveProviderHolder exposes current_config (ProviderConfig with
        provider_type, model). The runtime `model` arg may be a placeholder
        id (e.g. "N-Agent") that the provider resolves to its configured model
        internally; for admin-facing usage stats we want the real model name,
        while preserving the originally requested name separately.

        For raw providers without current_config (OpenAICompatibleProvider /
        AnthropicProvider used directly, or test fakes), fall back to
        default_model attr or the local `model` arg.
        """
        requested_model = model or None
        config = getattr(self.llm_provider, "current_config", None)
        if config is not None:
            provider_type = getattr(config, "provider_type", None)
            provider_kind = "anthropic" if provider_type == "anthropic" else "openai"
            config_model = getattr(config, "model", None) or ""
            real_model = resolve_model(model, config_model) or None
            return provider_kind, provider_type, real_model, requested_model
        default_model = getattr(self.llm_provider, "default_model", None) or ""
        real_model = resolve_model(model, default_model) or None
        return "openai", None, real_model, requested_model

    def _resolve_trigger_type(self, state: AgentState) -> str:
        """Classify what triggered this LLM call: 'tool' for continuation after
        tool execution, 'user' for the first call in a turn."""
        msgs = state.working_messages or []
        for msg in reversed(msgs):
            role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
            if role == "tool":
                return "tool"
            if role == "user":
                return "user"
        return "user"

    async def call_llm(self, state: AgentState, config: Optional[RunnableConfig] = None) -> AgentState:
        if self.is_cancelled(state.session_id):
            raise asyncio.CancelledError()
        if state.iteration_count >= self.iteration_limit:
            state.error = "iteration limit reached"
            state.finish_reason = "length"
            return state
        configurable = (config or {}).get("configurable", {})
        model = configurable.get("model", "")
        options = configurable.get("options") or state.run_options
        call_start = time.monotonic()
        try:
            # ----- vision preflight: 不支持 vision 时返回友好消息，不调用 provider -----
            if (
                state.working_messages
                and self.vision_capability is not None
                and not self.vision_capability()
            ):
                last_msg = state.working_messages[-1]
                if last_msg.get("role") == "user" and has_image_part(last_msg.get("content")):
                    state.iteration_count += 1
                    state.final_message = {
                        "role": "assistant",
                        "content": "当前模型不支持图片输入，请切换到支持 vision 的模型后再试。",
                    }
                    state.finish_reason = "stop"
                    state.pending_tool_calls = []
                    return state

            provider_context = self.context_service.build_provider_context(state, options)
            working_messages_for_call = provider_context.messages
            tools = provider_context.tools

            result = await self.llm_provider.chat(
                working_messages_for_call,
                tools,
                False,
                model,
                options,
            )
            if not isinstance(result, LLMResult):
                state.error = "streaming provider result is not supported inside graph"
                state.finish_reason = "error"
                return state
            # capture request JSON before working_messages is mutated below
            request_json_cache = json.dumps(working_messages_for_call, default=str, ensure_ascii=False)
            tools_json_cache = json.dumps(tools, default=str, ensure_ascii=False) if tools else None
            gen_params = {k: v for k, v in options.items() if k not in _INTERNAL_OPTION_KEYS} if isinstance(options, dict) else {}
            gen_params_json_cache = json.dumps(gen_params, default=str, ensure_ascii=False) if gen_params else None
            state.iteration_count += 1
            state.final_message = result.message

            # ----- 新增：立即清理 final_message 内容，防止持久化脏数据 -----
            # 如果模型回显 <memory-context>，这里清理后再 append 到 state.working_messages
            # 保证 SQLite/summary 永远不会收到脏内容
            if self.external_memory_manager and state.final_message:
                content = state.final_message.get("content", "")
                if isinstance(content, str):
                    state.final_message["content"] = scrub_memory_context(content)
            # ----- 结束新增 -----

            state.finish_reason = result.finish_reason
            state.pending_tool_calls = result.message.get("tool_calls") or []
            if state.pending_tool_calls:
                state.assistant_tool_messages.append(result.message)
            state.working_messages.append(result.message)

            # ----- 应用日志：完整打印 LLM 调用的输入、输出 -----
            # 与 usage recording 解耦：即使 usage_service 为 None 或 result.usage
            # 为空，也输出输入/输出日志，便于排查 LLM 行为。复用已计算的 json cache
            # 避免重复序列化。response 在 scrub_memory_context 之后打印，保证日志
            # 内容与持久化一致。
            response_json_cache = json.dumps(result.message, default=str, ensure_ascii=False)
            logger.info(
                "LLM request: session=%s model=%s request=%s",
                state.session_id, model, request_json_cache,
            )
            logger.info(
                "LLM response: session=%s model=%s response=%s",
                state.session_id, model, response_json_cache,
            )

            # ----- usage recording (T6) -----
            # Record only when usage_service is wired AND provider returned a
            # non-empty usage dict. provider_kind/provider/model are derived
            # from llm_provider.current_config when available (ActiveProviderHolder);
            # otherwise we fall back to the local `model` param and "openai"
            # provider_kind (covers raw OpenAICompatibleProvider / test fakes).
            if self.usage_service is not None and result.usage:
                latency_ms = int((time.monotonic() - call_start) * 1000)
                provider_kind, provider_name, real_model, requested_model = self._resolve_usage_meta(model)
                trigger_type = self._resolve_trigger_type(state)
                try:
                    await self.usage_service.record_call(
                        session_id=state.session_id,
                        model=real_model,
                        provider=provider_name,
                        raw_usage=result.usage,
                        latency_ms=latency_ms,
                        provider_kind=provider_kind,
                        requested_model=requested_model,
                        trigger_type=trigger_type,
                        request_messages=request_json_cache,
                        response_message=response_json_cache,
                        tools=tools_json_cache,
                        generation_params=gen_params_json_cache,
                    )
                except Exception:
                    logger.exception(
                        "usage recording failed for session=%s", state.session_id,
                    )
            # ----- end usage recording -----
        except Exception as exc:
            state.error = str(exc)
            state.finish_reason = "error"
        return state

    async def execute_tools(self, state: AgentState, config: Optional[RunnableConfig] = None) -> AgentState:
        if self.is_cancelled(state.session_id):
            raise asyncio.CancelledError()
        state.tool_results = []
        context = None
        options = None
        if config:
            options = (config.get("configurable", {}) or {}).get("options")
        if not options:
            options = state.run_options
        raw_context = (options or {}).get("tool_execution_context")
        if isinstance(raw_context, ToolExecutionContext):
            context = raw_context
        for tool_call in state.pending_tool_calls:
            function = tool_call.get("function", {})
            arguments = function.get("arguments", {})
            invalid_arguments = False
            try:
                parsed_arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
            except json.JSONDecodeError:
                parsed_arguments = {}
                invalid_arguments = True
            if not isinstance(parsed_arguments, dict):
                parsed_arguments = {}
                invalid_arguments = True
            tool_id = tool_call.get("id", "")
            tool_name = function.get("name", "")

            # 工具执行前 - pending 事件
            state.stream_tool_events.append(ChatEvent(
                ChatEventType.TOOL_CALL_DELTA,
                tool_call={
                    "id": tool_id,
                    "name": tool_name,
                    "arguments": parsed_arguments,
                    "status": "pending",
                },
            ))
            await self._emit_stream_tool_event(state.stream_tool_events[-1], options)

            if invalid_arguments:
                result = ToolResult(
                    tool_call_id=tool_id,
                    tool_name=tool_name,
                    status=ToolResultStatus.ERROR,
                    content={"error": "invalid arguments"},
                )
            else:
                request = ToolCallRequest(
                    id=tool_id,
                    name=tool_name,
                    arguments=parsed_arguments,
                )
                try:
                    evaluation = self.tool_service.evaluate_execution(request, context)
                except ToolNotFoundError:
                    result = ToolResult(
                        tool_call_id=tool_id,
                        tool_name=tool_name,
                        status=ToolResultStatus.ERROR,
                        content={"error": "tool not found"},
                    )
                else:
                    decision = evaluation.decision
                    if decision.outcome is PolicyOutcome.ALLOW:
                        result = await self.tool_service.execute(
                            request,
                            context,
                            evaluation=evaluation,
                        )
                    elif decision.outcome is PolicyOutcome.DENY:
                        result = self._permission_denied_result(
                            request,
                            decision.reason,
                        )
                    else:
                        result = await self._request_tool_approval(
                            request,
                            state.session_id,
                            context,
                            evaluation,
                        )

            # 工具执行后 - success/error 事件
            tool_status = "success" if result.status == ToolResultStatus.SUCCESS else "error"
            state.stream_tool_events.append(ChatEvent(
                ChatEventType.TOOL_CALL_DELTA,
                tool_call={
                    "id": tool_id,
                    "name": result.tool_name,
                    "arguments": parsed_arguments,
                    "status": tool_status,
                    "duration_ms": result.duration_ms,
                },
            ))
            await self._emit_stream_tool_event(state.stream_tool_events[-1], options)

            result_payload = {
                "tool_call_id": result.tool_call_id,
                "name": result.tool_name,
                "status": result.status.value,
                "content": result.content,
                "duration_ms": result.duration_ms,
            }
            state.tool_results.append(result_payload)
            state.working_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result.tool_call_id,
                    "name": result.tool_name,
                    "content": json.dumps(result_payload),
                }
            )
            await self.memory_store.save_tool_call(
                ToolCall(
                    id=result.tool_call_id,
                    session_id=state.session_id,
                    tool_name=result.tool_name,
                    arguments=parsed_arguments,
                    result=result_payload,
                    status=result.status.value,
                    duration_ms=result.duration_ms,
                )
            )
        state.pending_tool_calls = []
        state.final_message = None
        return state

    @staticmethod
    def _permission_denied_result(
        request: ToolCallRequest,
        reason: str,
    ) -> ToolResult:
        return ToolResult(
            tool_call_id=request.id,
            tool_name=request.name,
            status=ToolResultStatus.PERMISSION_DENIED,
            content={"error": "permission_denied", "reason": reason},
        )

    async def _request_tool_approval(
        self,
        request: ToolCallRequest,
        state_session_id: str,
        context: ToolExecutionContext | None,
        evaluation: ToolExecutionEvaluation,
    ) -> ToolResult:
        decider = context.approval_decider if context is not None else None
        if decider is None:
            return self._permission_denied_result(request, "approval_required")

        approval = evaluation.approval
        approval_request = ApprovalRequest(
            session_id=context.session_id or state_session_id,
            tool_call_id=request.id,
            tool_name=approval.name,
            arguments=dict(request.arguments),
            description=approval.description,
            risk_level=approval.risk_level,
        )
        try:
            raw_decision = decider(approval_request)
            if isawaitable(raw_decision):
                raw_decision = await raw_decision
        except Exception:
            return self._permission_denied_result(request, "approval_failed")

        if not isinstance(raw_decision, ApprovalDecision):
            return self._permission_denied_result(
                request,
                "invalid_approval_decision",
            )
        if not raw_decision.allowed:
            return self._permission_denied_result(
                request,
                raw_decision.reason or "approval_denied",
            )
        if raw_decision.scope not in {"once", "session"}:
            return self._permission_denied_result(request, "invalid_approval_scope")

        try:
            authorized_context = self.tool_service.authorize_once(
                request,
                context,
                evaluation=evaluation,
            )
        except ValueError:
            return self._permission_denied_result(request, "authorization_failed")
        return await self.tool_service.execute(
            request,
            authorized_context,
            evaluation=evaluation,
        )

    async def _emit_stream_tool_event(self, event: ChatEvent, options: dict[str, Any] | None) -> None:
        sink = (options or {}).get("stream_event_sink")
        if not callable(sink):
            return
        result = sink(event)
        if isawaitable(result):
            await result
        await asyncio.sleep(0.001)

    async def update_memory(self, state: AgentState) -> AgentState:
        if self.is_cancelled(state.session_id):
            raise asyncio.CancelledError()
        assistant_messages = [*state.assistant_tool_messages]
        if state.final_message:
            assistant_messages.append(state.final_message)
        for assistant_message in assistant_messages:
            content = assistant_message.get("content", "")
            tool_calls = assistant_message.get("tool_calls") or []
            if tool_calls:
                content = {"content": content, "tool_calls": tool_calls}
            await self.memory_store.append_message(
                state.session_id,
                ConversationMessage(role="assistant", content=content),
            )
        state.assistant_tool_messages = []
        for result in state.tool_results:
            await self.memory_store.append_message(
                state.session_id,
                ConversationMessage(
                    role="tool",
                    content=json.dumps(result),
                    tool_call_id=result.get("tool_call_id"),
                    name=result.get("name"),
                ),
            )
        state.tool_results = []
        await self.memory_store.save_task_state(
            TaskState(
                session_id=state.session_id,
                status="failed" if state.error else "running",
                iteration_count=state.iteration_count,
                last_error=state.error,
            )
        )
        return state

    def _extract_user_content(self, input_messages: list[dict[str, Any]]) -> str:
        """Extract concatenated user content from input_messages."""
        contents: list[str] = []
        for msg in input_messages:
            if msg.get("role") == "user":
                text = extract_text(msg.get("content", ""))
                if text:
                    contents.append(text)
                elif has_image_part(msg.get("content")):
                    contents.append("[用户发送了图片]")
        return "\n".join(contents)

    def _extract_assistant_content(self, final_message: dict[str, Any]) -> str:
        """Extract assistant content from final_message."""
        return extract_text(final_message.get("content", ""))

    async def finalize(self, state: AgentState) -> AgentState:
        if not state.error and not state.final_message and state.iteration_count >= self.iteration_limit:
            state.error = "iteration limit reached"
            state.finish_reason = "length"
        state.run_status = RunStatus.FAILED if state.error else RunStatus.COMPLETED
        if state.error and not state.final_message:
            state.final_message = {"role": "assistant", "content": _error_message_for_user(state)}
            # error message also needs cleaning if it contains any tags
            content = state.final_message.get("content", "")
            if isinstance(content, str):
                state.final_message["content"] = scrub_memory_context(content)
            await self.memory_store.append_message(
                state.session_id,
                ConversationMessage(role="assistant", content=state.final_message["content"]),
            )

        # ----- 新增：外部记忆同步 -----
        # call_llm 已经清理过 final_message，这里同步的是干净内容
        if self.external_memory_manager and state.final_message:
            user_content = self._extract_user_content(state.input_messages)
            assistant_content = self._extract_assistant_content(state.final_message)
            agent_context = "unattended"  # fail-closed default
            enabled_override = None
            tool_ctx = state.run_options.get("tool_execution_context")
            if tool_ctx is not None and isinstance(tool_ctx, ToolExecutionContext):
                agent_context = tool_ctx.trusted_metadata.get("agent_context", "unattended")
                enabled_override = tool_ctx.enabled_override
            self.external_memory_manager.sync_all(
                user_content, assistant_content,
                session_id=state.session_id,
                agent_context=agent_context,
                enabled_override=enabled_override,
            )
        # ----- 结束新增 -----

        await self.memory_store.save_task_state(
            TaskState(
                session_id=state.session_id,
                status=state.run_status.value,
                iteration_count=state.iteration_count,
                last_error=state.error,
            )
        )
        return state

    def _after_llm(self, state: AgentState) -> str:
        if state.error:
            return "finalize"
        if state.pending_tool_calls:
            return "tools"
        return "memory"

    def _after_memory(self, state: AgentState) -> str:
        if state.error or state.final_message:
            return "finalize"
        if state.iteration_count >= self.iteration_limit:
            return "finalize"
        return "continue"


def llm_events_to_chat_events(events: AsyncIterator) -> AsyncIterator[ChatEvent]:
    async def convert() -> AsyncIterator[ChatEvent]:
        async for event in events:
            if event.type is LLMEventType.CONTENT_DELTA:
                yield ChatEvent(ChatEventType.CONTENT_DELTA, content=event.content)
            elif event.type is LLMEventType.TOOL_CALL_DELTA:
                yield ChatEvent(ChatEventType.TOOL_CALL_DELTA, tool_call=event.tool_call)
            elif event.type is LLMEventType.MESSAGE_DONE:
                yield ChatEvent(ChatEventType.MESSAGE_DONE, finish_reason=event.finish_reason)
            elif event.type is LLMEventType.ERROR:
                yield ChatEvent(ChatEventType.ERROR, error=event.error, finish_reason="error")
    return convert()


def _error_message_for_user(state: AgentState) -> str:
    if state.error == "iteration limit reached":
        content = _latest_tool_result_summary(state.working_messages)
        if content:
            return f"已达到工具调用上限，模型没有生成最终回答。最近一次工具调用已返回以下结果：\n\n{content}"
        return "已达到工具调用上限，模型没有生成最终回答。请查看工具调用调试信息，或缩小问题后重试。"
    return state.error or "error"


def _latest_tool_result_summary(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "tool":
            continue
        raw = message.get("content", "")
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            return str(raw)[:1200]
        content = payload.get("content") if isinstance(payload, dict) else None
        if isinstance(content, dict) and isinstance(content.get("results"), list):
            snippets = []
            for index, item in enumerate(content["results"][:3], start=1):
                title = item.get("title") or item.get("source") or item.get("id") or f"结果 {index}"
                text = str(item.get("content") or item.get("snippet") or "").strip()
                if len(text) > 600:
                    text = text[:600].rstrip() + "..."
                snippets.append(f"{index}. {title}\n{text}")
            return "\n\n".join(snippets)
        return json.dumps(content if content is not None else payload, ensure_ascii=False, indent=2)[:1200]
    return ""

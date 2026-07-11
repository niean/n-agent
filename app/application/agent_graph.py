from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from inspect import isawaitable
from typing import Any, Optional

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph

from app.application.events import ChatEvent, ChatEventType
from app.application.external_memory_manager import ExternalMemoryManager
from app.application.prompt_builder import build_system_prompt
from app.application.tool_service import ToolService
from app.domain.agent import AgentState, RunStatus
from app.domain.context import CONTEXT_SUMMARY_PREFIX, ContextEngine
from app.domain.memory import MemoryStore, Summarizer
from app.domain.provider import LLMEventType, LLMProvider, LLMResult, resolve_model
from app.domain.session import ConversationMessage, Summary, TaskState, ToolCall
from app.domain.tool import (
    ApprovalRequest,
    RiskLevel,
    ToolCallRequest,
    ToolExecutionContext,
    ToolResult,
    ToolResultStatus,
)
from app.utils.content_utils import extract_text, has_image_part, prepend_text_part
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
        self.context_engine = context_engine
        self.usage_service = usage_service
        self.skill_service = skill_service
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
        graph.add_node("load_context", self.load_context)
        graph.add_node("compress_context", self.compress_context)
        graph.add_node("call_llm", self.call_llm)
        graph.add_node("execute_tools", self.execute_tools)
        graph.add_node("update_memory", self.update_memory)
        graph.add_node("finalize", self.finalize)
        graph.set_entry_point("load_context")
        graph.add_edge("load_context", "compress_context")
        graph.add_edge("compress_context", "call_llm")
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
        if self.context_engine is None:
            return {"compressed": False, "reason": "context_engine_unavailable"}
        state = AgentState(session_id=session_id, input_messages=[])
        state.run_options = {"force_compress": True}
        state = await self.load_context(state)
        state.run_options = {"force_compress": True}
        before_count = len(state.working_messages)
        before_summary = state.summary
        state = await self.compress_context(state)
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

    async def load_context(self, state: AgentState) -> AgentState:
        if self.is_cancelled(state.session_id):
            raise asyncio.CancelledError()
        messages = await self.memory_store.list_messages(state.session_id)
        summary = await self.memory_store.get_summary(state.session_id)
        enabled_override = state.run_options.get("external_memory_enabled")
        # 过滤掉已被摘要吸收的原始消息（is_summarized=1），避免 middle + summary 冗余。
        unsummarized = [m for m in messages if not m.is_summarized]
        context_messages = _filter_to_latest_summary(unsummarized)
        # Deduplicate: callers (ChatCompletionService) persist user messages to
        # memory_store before invoking the graph, so the same messages may
        # appear both in context_messages (loaded from history) and in
        # state.input_messages. Trim trailing context_messages that match
        # input_messages to avoid sending duplicates to the LLM.
        context_messages = _dedupe_trailing(context_messages, state.input_messages)
        # 缓存 context_message_ids，供 compress_context 映射 middle 索引 -> 消息 id
        state.context_message_ids = [m.id for m in context_messages]
        skills_index: str | None = None
        if self.skill_service is not None:
            try:
                skills_index = await self.skill_service.build_skills_index() or None
            except Exception:
                logger.warning("build_skills_index failed", exc_info=True)
        state.working_messages = [
            {"role": "system", "content": build_system_prompt(self.external_memory_manager, enabled_override, skills_index)},
            *[_message_to_provider(message) for message in context_messages],
            *state.input_messages,
        ]
        state.summary = summary.summary if summary else ""
        state.run_status = RunStatus.RUNNING
        return state

    async def compress_context(self, state: AgentState) -> AgentState:
        if self.is_cancelled(state.session_id):
            raise asyncio.CancelledError()
        if self.context_engine is None:
            return state
        # Separate leading system messages from non-system messages
        leading_system = []
        idx = 0
        while idx < len(state.working_messages) and state.working_messages[idx].get("role") == "system":
            leading_system.append(state.working_messages[idx])
            idx += 1
        non_system = state.working_messages[idx:]
        force = bool(state.run_options.get("force_compress", False))
        if not self.context_engine.should_compress(non_system, force=force):
            return state
        # External memory pre_compress_all (only when actually compressing)
        rescued_context = ""
        if self.external_memory_manager:
            enabled_override = state.run_options.get("external_memory_enabled")
            rescued_context = self.external_memory_manager.pre_compress_all(
                non_system,
                session_id=state.session_id,
                enabled_override=enabled_override,
            )
        result = await self.context_engine.compress(
            non_system, existing_summary=state.summary, force=force,
        )
        if not result.compressed:
            return state

        # b. 先从 result.messages 里识别摘要消息（恰好 1 条），再更新 state
        # spec Error Handling #7 要求 summary count != 1 时保持 state.summary 不变
        summary_dicts = [
            m for m in result.messages
            if m.get("role") == "user"
            and isinstance(m.get("content"), str)
            and m["content"].startswith(CONTEXT_SUMMARY_PREFIX)
        ]
        if len(summary_dicts) != 1:
            logger.error(
                "compress_context: expected exactly 1 summary message in result.messages, got %d",
                len(summary_dicts),
            )
            return state

        # a. 计算 next_summary（不立即写入 state；replace 失败时保持 state 不变）
        if rescued_context:
            next_summary = f"{rescued_context}\n\n{result.summary}".strip()
        else:
            next_summary = result.summary

        # c. 构造 ConversationMessage(is_summary=True)
        summary_dict = summary_dicts[0]
        summary_message = ConversationMessage(
            role="user",
            content=summary_dict["content"],
            is_summary=True,
        )

        # d. append_summary_message（仅 INSERT，保留所有摘要记录供 Dashboard 渲染）
        try:
            returned_message = await self.memory_store.append_summary_message(
                state.session_id, summary_message,
            )
        except Exception as exc:
            logger.error("compress_context: append_summary_message failed: %s", exc)
            return state

        # d.2 mark_messages_summarized：把 middle 段（被摘要吸收的原始消息）标记为
        # is_summarized=1，下一次 load_context 时过滤掉，避免 middle + summary 冗余。
        # result.summarized_message_indices 是相对于 compress 输入（non_system）的索引；
        # non_system = context_messages + input_messages，前 len(context_message_ids) 个
        # 是历史消息，后面是本轮新增。middle 只可能在历史消息范围内。
        if result.summarized_message_indices:
            ctx_ids = state.context_message_ids
            ctx_len = len(ctx_ids)
            middle_ids = [
                ctx_ids[i] for i in result.summarized_message_indices
                if 0 <= i < ctx_len
            ]
            if middle_ids:
                try:
                    await self.memory_store.mark_messages_summarized(state.session_id, middle_ids)
                except Exception as exc:
                    logger.warning(
                        "compress_context: mark_messages_summarized failed: %s", exc,
                    )

        # e. save_summary（source_message_id 关联新摘要消息 id；单行表，仅存最新摘要）
        if next_summary:
            try:
                await self.memory_store.save_summary(
                    Summary(
                        session_id=state.session_id,
                        summary=next_summary,
                        source_message_id=returned_message.id,
                    )
                )
            except Exception as exc:
                # 降级：messages 表已更新，summaries 表滞后一轮，不回滚
                logger.warning(
                    "compress_context: save_summary failed, dashboard may lag one round: %s",
                    exc,
                )

        # f. 更新 state（replace 成功后才写入）
        state.summary = next_summary
        state.working_messages = leading_system + result.messages

        # ----- compression usage recording (T6) -----
        # Only record when both before/after token counts are available
        # (ContextCompressionResult.original_tokens / compressed_tokens may be None).
        if (
            self.usage_service is not None
            and result.original_tokens is not None
            and result.compressed_tokens is not None
        ):
            try:
                await self.usage_service.record_compression(
                    session_id=state.session_id,
                    before_tokens=result.original_tokens,
                    after_tokens=result.compressed_tokens,
                )
            except Exception:
                logger.exception(
                    "compression recording failed for session=%s", state.session_id,
                )
        # ----- end compression usage recording -----
        return state

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
            tools = self.tool_service.list_openai_tools(
                RiskLevel.SAFE if options.get("tool_exposure_policy") == "safe_only" else None,
                context=options.get("tool_execution_context") if isinstance(options, dict) else None,
            )

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

            # ----- 外部记忆动态预取注入（临时构造 api_messages，不修改 state）-----
            if self.external_memory_manager and state.working_messages:
                last_idx = len(state.working_messages) - 1
                last_msg = state.working_messages[last_idx]
                api_messages = state.working_messages.copy()
                enabled_override: list[str] | None = state.run_options.get("external_memory_enabled")
                if last_msg.get("role") == "user":
                    query_text = extract_text(last_msg["content"])
                    memory_context = self.external_memory_manager.prefetch_all(
                        query_text,
                        session_id=state.session_id,
                        enabled_override=enabled_override,
                    )
                    if memory_context:
                        last_msg_copy = last_msg.copy()
                        last_msg_copy["content"] = prepend_text_part(last_msg["content"], memory_context + "\n\n")
                        api_messages[last_idx] = last_msg_copy
                working_messages_for_call = api_messages
            else:
                working_messages_for_call = state.working_messages
            # ----- 结束外部记忆动态预取注入 -----

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
                    response_json = json.dumps(result.message, default=str, ensure_ascii=False)
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
                        response_message=response_json,
                        tools=tools_json_cache,
                        generation_params=gen_params_json_cache,
                    )
                    logger.info(
                        "API call model=%s provider=%s in=%s out=%s total=%s latency=%dms",
                        real_model, provider_name,
                        result.usage.get("prompt_tokens", result.usage.get("input_tokens", 0)),
                        result.usage.get("completion_tokens", result.usage.get("output_tokens", 0)),
                        result.usage.get("total_tokens", 0),
                        latency_ms,
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
            arguments = function.get("arguments") or "{}"
            try:
                parsed_arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
            except json.JSONDecodeError:
                parsed_arguments = {}
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

            request = ToolCallRequest(
                id=tool_id,
                name=tool_name,
                arguments=parsed_arguments,
            )

            # Approval gate: ask the decider BEFORE calling ToolService.execute.
            # Only CONFIRM-level tools with a configured decider trigger this path.
            # allow_once: create a per-iteration copy via dataclasses.replace so
            # ToolService.execute permits this single call. The original context
            # stays unchanged for the next tool_call (S 6). allow_session: the
            # ACP agent is responsible for persisting the permission into context
            # before constructing this runner; the runner does NOT write metadata
            # here (S 7).
            result: ToolResult | None = None
            effective_context = context
            definition = self.tool_service.get_definition(tool_name)
            if (
                definition is not None
                and definition.risk_level is RiskLevel.CONFIRM
                and effective_context is not None
                and effective_context.approval_decider is not None
            ):
                approval_request = ApprovalRequest(
                    session_id=effective_context.session_id or state.session_id,
                    tool_call_id=tool_id,
                    tool_name=tool_name,
                    arguments=parsed_arguments,
                    description=definition.description,
                    risk_level=definition.risk_level,
                )
                raw_decision = effective_context.approval_decider(approval_request)
                if isawaitable(raw_decision):
                    raw_decision = await raw_decision
                decision = raw_decision

                if not decision.allowed:
                    result = ToolResult(
                        tool_call_id=tool_id,
                        tool_name=tool_name,
                        status=ToolResultStatus.PERMISSION_DENIED,
                        content={"error": "permission_denied", "reason": decision.reason},
                    )
                elif decision.scope == "once":
                    if definition.managed:
                        new_permitted = set(effective_context.permitted_managed_tools)
                        new_permitted.add(tool_name)
                        effective_context = dataclasses.replace(
                            effective_context,
                            permitted_managed_tools=new_permitted,
                        )
                    else:
                        # For non-managed CONFIRM tools, _is_confirm_allowed
                        # iterates expected.items() and checks
                        # request.arguments[key] == value. An empty dict means
                        # no keys to check -> allowed for any arguments.
                        new_allowed = dict(effective_context.allowed_confirm_tools)
                        new_allowed[tool_name] = {}
                        effective_context = dataclasses.replace(
                            effective_context,
                            allowed_confirm_tools=new_allowed,
                        )
                # scope == "session": context is already set up by the ACP
                # permission bridge/agent; no-op here.

            if result is None:
                result = await self.tool_service.execute(request, effective_context)

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
                    arguments=request.arguments,
                    result=result_payload,
                    status=result.status.value,
                    duration_ms=result.duration_ms,
                )
            )
        state.pending_tool_calls = []
        state.final_message = None
        return state

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


def _message_to_provider(message: ConversationMessage) -> dict[str, Any]:
    content = message.content
    tool_calls = None
    if message.role == "assistant" and isinstance(content, dict) and "tool_calls" in content:
        tool_calls = content.get("tool_calls") or []
        content = content.get("content", "")
    if message.role == "tool" and not isinstance(content, str):
        content = json.dumps(content)
    data = {"role": message.role, "content": content}
    if tool_calls:
        data["tool_calls"] = tool_calls
    if message.tool_call_id:
        data["tool_call_id"] = message.tool_call_id
    if message.name:
        data["name"] = message.name
    return data


def _filter_to_latest_summary(messages: list[ConversationMessage]) -> list[ConversationMessage]:
    """保留全部非摘要消息 + 仅最新一条摘要；旧摘要从上下文剔除。

    前置条件：调用方已过滤 is_summarized=1 的消息（middle 段已被标记）。
    本函数只处理摘要消息：保留最新一条 summary，丢弃旧 summary。
    非摘要消息（head + tail + new_msgs）全部保留。
    """
    latest_summary_idx = -1
    for idx in range(len(messages) - 1, -1, -1):
        if messages[idx].is_summary:
            latest_summary_idx = idx
            break
    if latest_summary_idx == -1:
        return list(messages)
    return [
        m for idx, m in enumerate(messages)
        if not m.is_summary or idx == latest_summary_idx
    ]


def _msg_role_content_key(msg: Any) -> tuple[str, str]:
    """Normalize a message to a (role, content_text) key for dedup comparison."""
    role = msg.get("role", "") if isinstance(msg, dict) else getattr(msg, "role", "")
    content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
    if isinstance(content, list):
        content = json.dumps(content, default=str, ensure_ascii=False)
    elif not isinstance(content, str):
        content = str(content or "")
    return (role or "", content)


def _dedupe_trailing(history: list, inputs: list) -> list:
    """Drop trailing history entries that already appear in inputs.

    Callers persist user messages to memory_store before invoking the graph,
    so the same messages may show up in both history and inputs. Trim the
    trailing history that matches inputs so the LLM doesn't see duplicates.
    """
    if not inputs:
        return history
    n = len(inputs)
    if len(history) < n:
        return history
    tail = history[-n:]
    if all(_msg_role_content_key(t) == _msg_role_content_key(i) for t, i in zip(tail, inputs)):
        return history[:-n]
    return history


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

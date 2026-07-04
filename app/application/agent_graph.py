from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
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
from app.domain.memory import MemoryStore, Summarizer
from app.domain.provider import LLMEventType, LLMProvider, LLMResult
from app.domain.session import ConversationMessage, Summary, TaskState, ToolCall
from app.domain.tool import RiskLevel, ToolCallRequest, ToolExecutionContext, ToolResultStatus
from app.utils.memory_scrubber import scrub_memory_context


class AgentGraphRunner:
    def __init__(
        self,
        llm_provider: LLMProvider,
        tool_service: ToolService,
        memory_store: MemoryStore,
        summarizer: Summarizer,
        iteration_limit: int = 10,
        external_memory_manager: ExternalMemoryManager | None = None,
    ):
        self.llm_provider = llm_provider
        self.tool_service = tool_service
        self.memory_store = memory_store
        self.summarizer = summarizer
        self.iteration_limit = iteration_limit
        self.external_memory_manager = external_memory_manager
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(AgentState)
        graph.add_node("load_context", self.load_context)
        graph.add_node("call_llm", self.call_llm)
        graph.add_node("execute_tools", self.execute_tools)
        graph.add_node("update_memory", self.update_memory)
        graph.add_node("finalize", self.finalize)
        graph.set_entry_point("load_context")
        graph.add_edge("load_context", "call_llm")
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
        try:
            while not run_task.done():
                try:
                    yield await asyncio.wait_for(tool_event_queue.get(), timeout=0.05)
                except asyncio.TimeoutError:
                    continue
            result = await run_task
            while not tool_event_queue.empty():
                yield tool_event_queue.get_nowait()
        finally:
            if not run_task.done():
                run_task.cancel()
                with suppress(asyncio.CancelledError):
                    await run_task
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
        messages = await self.memory_store.list_messages(state.session_id)
        summary = await self.memory_store.get_summary(state.session_id)
        enabled_override = state.run_options.get("external_memory_enabled")
        state.working_messages = [
            {"role": "system", "content": build_system_prompt(self.external_memory_manager, enabled_override)},
            *[_message_to_provider(message) for message in messages],
            *state.input_messages,
        ]
        state.summary = summary.summary if summary else ""
        state.run_status = RunStatus.RUNNING
        return state

    async def call_llm(self, state: AgentState, config: Optional[RunnableConfig] = None) -> AgentState:
        if state.iteration_count >= self.iteration_limit:
            state.error = "iteration limit reached"
            state.finish_reason = "length"
            return state
        configurable = (config or {}).get("configurable", {})
        model = configurable.get("model", "")
        options = configurable.get("options") or state.run_options
        try:
            tools = self.tool_service.list_openai_tools(
                RiskLevel.SAFE if options.get("tool_exposure_policy") == "safe_only" else None,
                context=options.get("tool_execution_context") if isinstance(options, dict) else None,
            )

            # ----- 新增：外部记忆动态预取注入（临时构造 api_messages，不修改 state）-----
            if self.external_memory_manager and len(state.working_messages) > 0:
                last_idx = len(state.working_messages) - 1
                last_msg = state.working_messages[last_idx]
                api_messages = state.working_messages.copy()
                enabled_override: list[str] | None = state.run_options.get("external_memory_enabled")
                if last_msg["role"] == "user":
                    memory_context = self.external_memory_manager.prefetch_all(
                        str(last_msg["content"]),
                        session_id=state.session_id,
                        enabled_override=enabled_override,
                    )
                    if memory_context:
                        last_msg_copy = last_msg.copy()
                        new_content = memory_context + "\n\n" + last_msg["content"]
                        last_msg_copy["content"] = new_content
                        api_messages[last_idx] = last_msg_copy
                working_messages_for_call = api_messages
            else:
                working_messages_for_call = state.working_messages
            # ----- 结束新增 -----

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
        except Exception as exc:
            state.error = str(exc)
            state.finish_reason = "error"
        return state

    async def execute_tools(self, state: AgentState, config: Optional[RunnableConfig] = None) -> AgentState:
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
            result = await self.tool_service.execute(request, context)

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
        summary_messages = [message for message in state.working_messages if message.get("role") != "system"]
        rescued_context = ""
        if self.external_memory_manager:
            enabled_override = state.run_options.get("external_memory_enabled")
            rescued_context = self.external_memory_manager.pre_compress_all(
                summary_messages,
                session_id=state.session_id,
                enabled_override=enabled_override,
            )
        summary = await self.summarizer.summarize(summary_messages, state.summary)
        if rescued_context:
            summary = f"{rescued_context}\n\n{summary}".strip() if summary else rescued_context
        if summary:
            await self.memory_store.save_summary(Summary(session_id=state.session_id, summary=summary))
        return state

    def _extract_user_content(self, input_messages: list[dict[str, Any]]) -> str:
        """Extract concatenated user content from input_messages."""
        contents: list[str] = []
        for msg in input_messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    contents.append(content)
        return "\n".join(contents)

    def _extract_assistant_content(self, final_message: dict[str, Any]) -> str:
        """Extract assistant content from final_message."""
        content = final_message.get("content", "")
        if isinstance(content, str):
            return content
        return str(content)

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

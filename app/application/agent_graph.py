from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from langgraph.graph import END, StateGraph

from app.application.events import ChatEvent, ChatEventType
from app.application.prompt_builder import build_system_prompt
from app.application.tool_service import ToolService
from app.domain.agent import AgentState, RunStatus
from app.domain.memory import MemoryStore, Summarizer
from app.domain.provider import LLMEventType, LLMProvider, LLMResult
from app.domain.session import ConversationMessage, Summary, TaskState, ToolCall
from app.domain.tool import RiskLevel, ToolCallRequest, ToolExecutionContext


class AgentGraphRunner:
    def __init__(
        self,
        llm_provider: LLMProvider,
        tool_service: ToolService,
        memory_store: MemoryStore,
        summarizer: Summarizer,
        iteration_limit: int = 5,
    ):
        self.llm_provider = llm_provider
        self.tool_service = tool_service
        self.memory_store = memory_store
        self.summarizer = summarizer
        self.iteration_limit = iteration_limit
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

    async def stream_events(self, state: AgentState, model: str, options: dict[str, Any] | None = None) -> AsyncIterator[ChatEvent]:
        yield ChatEvent(ChatEventType.MESSAGE_START)
        result = await self.run(state, model, options)
        if result.error:
            yield ChatEvent(ChatEventType.ERROR, error=result.error, finish_reason="error")
        elif result.final_message:
            content = str(result.final_message.get("content") or "")
            if content:
                yield ChatEvent(ChatEventType.CONTENT_DELTA, content=content)
            yield ChatEvent(ChatEventType.MESSAGE_DONE, finish_reason=result.finish_reason or "stop")
        yield ChatEvent(ChatEventType.DONE)

    async def load_context(self, state: AgentState) -> AgentState:
        messages = await self.memory_store.list_messages(state.session_id)
        summary = await self.memory_store.get_summary(state.session_id)
        state.working_messages = [
            {"role": "system", "content": build_system_prompt()},
            *[_message_to_provider(message) for message in messages],
            *state.input_messages,
        ]
        state.summary = summary.summary if summary else ""
        state.run_status = RunStatus.RUNNING
        return state

    async def call_llm(self, state: AgentState, config: dict | None = None) -> AgentState:
        if state.iteration_count >= self.iteration_limit:
            state.error = "iteration limit reached"
            state.finish_reason = "length"
            return state
        configurable = (config or {}).get("configurable", {})
        model = configurable.get("model", "")
        options = configurable.get("options") or state.run_options
        try:
            tools = self.tool_service.list_openai_tools(
                RiskLevel.SAFE if options.get("tool_exposure_policy") == "safe_only" else None
            )
            result = await self.llm_provider.chat(
                state.working_messages,
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
            state.finish_reason = result.finish_reason
            state.pending_tool_calls = result.message.get("tool_calls") or []
            if state.pending_tool_calls:
                state.assistant_tool_messages.append(result.message)
            state.working_messages.append(result.message)
        except Exception as exc:
            state.error = str(exc)
            state.finish_reason = "error"
        return state

    async def execute_tools(self, state: AgentState, config: dict | None = None) -> AgentState:
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
            request = ToolCallRequest(
                id=tool_call.get("id", ""),
                name=function.get("name", ""),
                arguments=parsed_arguments,
            )
            result = await self.tool_service.execute(request, context)
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
        summary = await self.summarizer.summarize(summary_messages, state.summary)
        if summary:
            await self.memory_store.save_summary(Summary(session_id=state.session_id, summary=summary))
        return state

    async def finalize(self, state: AgentState) -> AgentState:
        if not state.error and not state.final_message and state.iteration_count >= self.iteration_limit:
            state.error = "iteration limit reached"
            state.finish_reason = "length"
        state.run_status = RunStatus.FAILED if state.error else RunStatus.COMPLETED
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

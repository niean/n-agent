from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from inspect import isawaitable
from typing import TYPE_CHECKING, Any, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.application.skill_evolution_service import SkillEvolutionService

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph

from app.application.budget_service import BudgetService
from app.application.browser_tool_audit import project_browser_tool_arguments
from app.application.context_service import ContextService
from app.application.events import ChatEvent, ChatEventType
from app.application.external_memory_manager import ExternalMemoryManager
from app.application.information_flow_service import InformationFlowService
from app.application.policy_snapshot import (
    BudgetPolicyConfig,
    InformationFlowPolicyConfig,
    RunPolicySnapshot,
)
from app.application.runtime_memory_service import RuntimeMemoryService
from app.application.tool_service import (
    ToolExecutionEvaluation,
    ToolNotFoundError,
    ToolService,
)
from app.domain.agent import AgentState, EndReason, RunStatus
from app.domain.budget import (
    BudgetActualUsage,
    BudgetReserveKind,
    BudgetReserveRequest,
)
from app.domain.context import ContextEngine
from app.domain.information_flow import ReleaseTarget, SecretCatalog
from app.domain.llm_policy import (
    LLMConfig,
    LLMPolicy,
    ModelRequirements,
    ProviderCapability,
    ProviderConstraints,
)
from app.domain.memory import MemoryStore, Summarizer
from app.domain.policy import ExecutionMode, PolicyOutcome
from app.domain.provider import LLMEventType, LLMProvider, LLMResult, ModelInfo, resolve_model
from app.domain.session import TaskState, ToolCall
from app.domain.tool import (
    ApprovalDecision,
    ApprovalRequest,
    RiskLevel,
    ToolCallRequest,
    ToolExecutionContext,
    ToolResult,
    ToolResultStatus,
)
from app.domain.turn_policy import (
    TurnDecision,
    TurnEvaluationInput,
    TurnNextStep,
    TurnPolicy,
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
    "_policy_snapshot",
    "force_compress",
    "max_iterations",
    "persist_messages",
    "dashboard_approval_event_queue",
    "activated_skills",
}

# Conversational artifact tools whose structured SUCCESS result materializes a
# ui.artifact card (spec line 205). Read-only tools (artifact_read/list/
# list_revisions/diff) are excluded -- they never write a card.
_ARTIFACT_WRITE_TOOLS = frozenset({
    "artifact_create",
    "artifact_update",
    "artifact_rollback",
    "artifact_publish",
})


# Mapping from Domain EndReason to OpenAI-compatible finish_reason strings.
# Used by finalize to set state.finish_reason from TurnPolicy's end_reason.
_END_REASON_TO_FINISH_REASON: dict[EndReason, str] = {
    EndReason.STOP: "stop",
    EndReason.ERROR: "error",
    EndReason.ITERATION_LIMIT: "length",
    EndReason.LENGTH: "length",
    EndReason.CANCELLED: "error",
    EndReason.DEADLINE: "length",
    EndReason.BUDGET_EXHAUSTED: "length",
    EndReason.TOOL_CALLS: "tool_calls",
}


@runtime_checkable
class HookDispatcherProtocol(Protocol):
    """Duck-typed dispatcher for plugin lifecycle hooks.

    PluginService implements this protocol. Kept as a Protocol to avoid
    importing PluginService into agent_graph.py (circular import / DDD).
    When None, all hook dispatch is skipped (backward-compatible).
    """

    async def invoke_hook(self, hook_name: str, **kwargs: Any) -> list[Any]:
        ...


class AgentGraphRunner:
    def __init__(
        self,
        llm_provider: LLMProvider,
        tool_service: ToolService,
        memory_store: MemoryStore,
        summarizer: Summarizer,
        iteration_limit: int = 10,
        turn_timeout_seconds: float = 900.0,
        external_memory_manager: ExternalMemoryManager | None = None,
        vision_capability: Optional[Callable[[], bool]] = None,
        context_engine: ContextEngine | None = None,
        context_service: ContextService | None = None,
        usage_service: Any = None,
        skill_service: Any = None,
        information_flow_service: InformationFlowService | None = None,
        runtime_memory_service: RuntimeMemoryService | None = None,
        budget_service: BudgetService | None = None,
        llm_policy: LLMPolicy | None = None,
        browser_guidance: str | None = None,
        artifact_guidance: str | None = None,
        llm_config: LLMConfig | None = None,
        evolution_service: SkillEvolutionService | None = None,
        nudge_interval: int = 10,
        curator_service: Any | None = None,
        hook_dispatcher: HookDispatcherProtocol | None = None,
        browser_dashboard_service: Any = None,
        browser_host_grant_ttl_seconds: int = 300,
    ):
        self.llm_provider = llm_provider
        self.tool_service = tool_service
        self.memory_store = memory_store
        self.summarizer = summarizer
        self.iteration_limit = iteration_limit
        self._turn_timeout_seconds = turn_timeout_seconds
        self._turn_policy = TurnPolicy()
        self._run_start_times: dict[str, float] = {}
        self.external_memory_manager = external_memory_manager
        self.vision_capability = vision_capability
        self.usage_service = usage_service
        self.skill_service = skill_service
        self._hook_dispatcher = hook_dispatcher
        self._browser_dashboard_service = browser_dashboard_service
        self._browser_host_grant_ttl_seconds = browser_host_grant_ttl_seconds
        self._information_flow_service = information_flow_service or InformationFlowService(
            InformationFlowPolicyConfig(),
            SecretCatalog(),
        )
        self._runtime_memory = runtime_memory_service or RuntimeMemoryService(
            memory_store,
            external_memory_manager=external_memory_manager,
        )
        self._budget_service = budget_service or BudgetService(BudgetPolicyConfig())
        self._llm_policy = llm_policy or LLMPolicy()
        self._llm_config = llm_config or LLMConfig()
        self._model_cache: list[ModelInfo] | None = None
        self.context_service = context_service or ContextService(
            memory_store,
            tool_service=tool_service,
            external_memory_manager=external_memory_manager,
            context_engine=context_engine,
            usage_service=usage_service,
            skill_service=skill_service,
            is_cancelled=self.is_cancelled,
            runtime_memory_service=self._runtime_memory,
            browser_guidance=browser_guidance,
            artifact_guidance=artifact_guidance,
        )
        self.graph = self._build_graph()
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self.evolution_service = evolution_service
        self.nudge_interval = nudge_interval
        self.curator_service = curator_service
        self._last_finalize_at: datetime | None = None

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

    async def _dispatch_hook(self, hook_name: str, **kwargs: Any) -> list[Any]:
        """Dispatch a lifecycle hook via the configured dispatcher.

        Returns the list of non-None results from invoke_hook, or [] when
        no dispatcher is configured (backward-compatible no-op).
        """
        if self._hook_dispatcher is None:
            return []
        try:
            return await self._hook_dispatcher.invoke_hook(hook_name, **kwargs)
        except Exception:
            logger.warning(
                "hook %s dispatch failed", hook_name, exc_info=True,
            )
            return []

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
        # Mirror persist_messages from options into the state field so that
        # update_memory / finalize / execute_tools can read it without re-checking
        # run_options. Caller may set either AgentState.persist_messages directly
        # (e.g. tests) or pass it via options (ChatCompletionService path).
        # Options wins to keep a single source of truth per run.
        opt_persist = state.run_options.get("persist_messages")
        if opt_persist is not None:
            state.persist_messages = bool(opt_persist)
        snapshot = self._policy_snapshot(state)
        self._budget_service.open(
            state.run_id,
            snapshot.budget if snapshot is not None else None,
        )
        self._run_start_times[state.run_id] = time.monotonic()
        # T10: on_turn_start -- at run() graph entry (also covers stream_events
        # which reuses this path, so no duplicate dispatch in stream_events).
        await self._dispatch_hook(
            "on_turn_start", session_id=state.session_id, metadata={},
        )
        finish_reason = "error"
        error: str | None = None
        try:
            cfg: dict[str, Any] = {
                "configurable": {"model": model, "options": state.run_options},
            }
            max_iter_opt = (
                state.run_options.get("max_iterations") if state.run_options else None
            )
            iter_limit = int(max_iter_opt) if max_iter_opt else self.iteration_limit
            # LangGraph recursion_limit: 每轮迭代约 2-3 节点（call_llm/execute_tools/
            # update_memory），consolidation fork 传 max_iterations=64 需高限；默认
            # iteration_limit 也给足余量，避免 GraphRecursionError 提前打断。
            cfg["recursion_limit"] = max(iter_limit * 3, 25)
            result = await self.graph.ainvoke(state, cfg)
            result = AgentState(**result) if isinstance(result, dict) else result
            finish_reason = result.finish_reason or "stop"
            error = result.error
            return result
        except asyncio.CancelledError:
            # Close Budget account on cancel (release unsettled reservations)
            error = error or "cancelled"
            await self._budget_service.close(state.run_id)
            raise
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            # T10: on_turn_end -- exactly once per turn (normal, provider-exception,
            # tool-exception, cancel, iteration-limit). Observer hook; callers
            # ignore the return value.
            await self._dispatch_hook(
                "on_turn_end",
                session_id=state.session_id,
                finish_reason=finish_reason,
                error=error,
            )
            self._run_start_times.pop(state.run_id, None)

    def _build_turn_input(self, state: AgentState) -> TurnEvaluationInput:
        """Build TurnEvaluationInput from AgentState + runtime facts.

        Supplies elapsed_seconds from the runner's monotonic clock (NOT
        from Domain). TurnPolicy receives facts as input, never calls
        time.now() itself.
        """
        start = self._run_start_times.get(state.run_id)
        elapsed = (time.monotonic() - start) if start else 0.0
        snapshot = self._policy_snapshot(state)
        turn_config = snapshot.turn if snapshot is not None else None
        return TurnEvaluationInput(
            final_message=state.final_message,
            error=state.error,
            pending_tool_calls=state.pending_tool_calls,
            iteration_count=state.iteration_count,
            cancelled=self.is_cancelled(state.session_id),
            elapsed_seconds=elapsed,
            turn_timeout_seconds=(
                turn_config.turn_timeout_seconds
                if turn_config is not None
                else self._turn_timeout_seconds
            ),
            iteration_limit=(
                turn_config.iteration_limit
                if turn_config is not None
                else (
                    int(state.run_options["max_iterations"])
                    if state.run_options.get("max_iterations")
                    else self.iteration_limit
                )
            ),
            budget_exhausted=state.budget_exhausted,
        )

    @staticmethod
    def _policy_snapshot(state: AgentState) -> RunPolicySnapshot | None:
        snapshot = state.run_options.get("_policy_snapshot")
        return snapshot if isinstance(snapshot, RunPolicySnapshot) else None

    def _evaluate_turn(self, state: AgentState) -> TurnDecision:
        """Run TurnPolicy on the current state. Single decision point."""
        return self._turn_policy.evaluate(self._build_turn_input(state))

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
        stream_guard = self._information_flow_service.create_stream_guard()
        stream_options = dict(options or {})
        tool_event_queue: asyncio.Queue[ChatEvent] = asyncio.Queue()

        async def emit_tool_event(event: ChatEvent) -> None:
            await tool_event_queue.put(event)

        stream_options["stream_event_sink"] = emit_tool_event
        # Pop the Dashboard approval event queue before run -- it must NEVER
        # reach run()/state.run_options/LLM provider/executor. Belt-and-suspenders:
        # also listed in _INTERNAL_OPTION_KEYS so gen_params filters it if it leaks.
        approval_queue = stream_options.pop("dashboard_approval_event_queue", None)
        run_task = asyncio.create_task(self.run(state, model, stream_options))
        self.register_run(state.session_id, run_task)
        result = None
        try:
            while not run_task.done():
                if approval_queue is not None:
                    # Fan-in: wait on tool_event_queue and approval_queue concurrently,
                    # yield whichever fires first. Approval events are yielded as-is
                    # (already ChatEvent(TOOL_APPROVAL_REQUIRED, metadata=...));
                    # they must NOT pass through scrubber/stream_guard/_redact_tool_event.
                    tool_task = asyncio.ensure_future(tool_event_queue.get())
                    approval_task = asyncio.ensure_future(approval_queue.get())
                    try:
                        done, _pending = await asyncio.wait(
                            [tool_task, approval_task],
                            timeout=0.05,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    finally:
                        # Cancel any still-pending tasks. This handles both the
                        # normal timeout case (neither queue had events) and
                        # GeneratorExit/CancelledError interrupting asyncio.wait.
                        for t in (tool_task, approval_task):
                            if not t.done():
                                t.cancel()
                                with suppress(asyncio.CancelledError):
                                    await t
                    for t in done:
                        if not t.cancelled():
                            yield t.result()
                else:
                    try:
                        yield await asyncio.wait_for(tool_event_queue.get(), timeout=0.05)
                    except asyncio.TimeoutError:
                        continue
            result = await run_task
            while not tool_event_queue.empty():
                yield tool_event_queue.get_nowait()
            if approval_queue is not None:
                while not approval_queue.empty():
                    yield approval_queue.get_nowait()
        except asyncio.CancelledError:
            yield ChatEvent(ChatEventType.ERROR, error="cancelled", finish_reason="cancelled")
            result = None
        finally:
            if not run_task.done():
                run_task.cancel()
                with suppress(asyncio.CancelledError):
                    await run_task
            # Close Budget account on any exit (cancel or normal).
            # finalize already closes on terminal; this is a safety net
            # for cancel/interrupt. Idempotent -- safe to call twice.
            await self._budget_service.close(state.run_id)
            self.clear_run(state.session_id)
        if result is not None:
            if result.error:
                # T9: Use result.finish_reason (set by TurnPolicy/finalize) for
                # consistent finish_reason between stream and non-stream paths.
                yield ChatEvent(ChatEventType.ERROR, error=result.error, finish_reason=result.finish_reason or "error")
                yield ChatEvent(ChatEventType.MESSAGE_DONE, finish_reason=result.finish_reason or "error")
            elif result.final_message:
                content = str(result.final_message.get("content") or "")
                if content:
                    # scrub each chunk: first memory-context tags, then secret values
                    for chunk in self._split_content_for_streaming(content):
                        scrubbed = scrubber.feed(chunk)
                        if scrubbed:
                            try:
                                safe = stream_guard.feed(scrubbed)
                                if safe:
                                    yield ChatEvent(ChatEventType.CONTENT_DELTA, content=safe)
                            except Exception:
                                yield ChatEvent(ChatEventType.ERROR, error="information_release_denied", finish_reason="error")
                                yield ChatEvent(ChatEventType.DONE)
                                return
                    scrubbed_final = scrubber.flush()
                    if scrubbed_final:
                        try:
                            safe = stream_guard.feed(scrubbed_final)
                            if safe:
                                yield ChatEvent(ChatEventType.CONTENT_DELTA, content=safe)
                        except Exception:
                            yield ChatEvent(ChatEventType.ERROR, error="information_release_denied", finish_reason="error")
                            yield ChatEvent(ChatEventType.DONE)
                            return
                    # flush stream guard lookbehind buffer
                    try:
                        guard_final = stream_guard.flush()
                        if guard_final:
                            yield ChatEvent(ChatEventType.CONTENT_DELTA, content=guard_final)
                    except Exception:
                        yield ChatEvent(ChatEventType.ERROR, error="information_release_denied", finish_reason="error")
                        yield ChatEvent(ChatEventType.DONE)
                        return
                yield ChatEvent(ChatEventType.MESSAGE_DONE, finish_reason=result.finish_reason or "stop")
        yield ChatEvent(ChatEventType.DONE)

    async def prepare_context(self, state: AgentState) -> AgentState:
        state = await self.context_service.build_context_state(state)
        # T10: on_pre_compress -- only when ContextEngine.should_compress is True
        # AND actually compressing. Check the ContextPlan from build_context_state;
        # compress_prepared_context re-evaluates the plan internally, but the
        # build plan is the best available signal at this point.
        if (
            self._hook_dispatcher is not None
            and self.context_service.context_engine is not None
            and state.context_plan is not None
            and state.context_plan.compression is not None
        ):
            non_system = [
                m for m in state.working_messages if m.get("role") != "system"
            ]
            estimated_tokens = sum(
                len(str(m.get("content", ""))) for m in non_system
            ) // 4
            await self._dispatch_hook(
                "on_pre_compress",
                session_id=state.session_id,
                messages=non_system,
                estimated_tokens=estimated_tokens,
                metadata={},
            )
        state = await self.context_service.compress_prepared_context(state)
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

    def _build_provider_capabilities(
        self,
        model_infos: list[ModelInfo] | None = None,
    ) -> tuple[ProviderCapability, ...]:
        """Build LLM-owned ProviderCapability snapshot from the active provider.

        Reads ``current_config`` (ProviderConfig) when available (ActiveProviderHolder).
        Falls back to ``default_model`` attr for raw providers (test doubles,
        OpenAICompatibleProvider used directly).

        ``supports_tools`` is projected from ``ModelInfo.supports_tools`` when
        a matching model is found in ``model_infos``; defaults to True when
        no model info is available.
        """
        config = getattr(self.llm_provider, "current_config", None)
        if config is not None:
            supports_vision = getattr(config, "supports_vision", False)
            if self.vision_capability is not None:
                supports_vision = self.vision_capability()
            model_id = getattr(config, "model", "")
            supports_tools = True
            if model_infos:
                for mi in model_infos:
                    if mi.id == model_id:
                        supports_tools = mi.supports_tools
                        break
            return (
                ProviderCapability(
                    provider_id=getattr(config, "id", "default"),
                    model_id=model_id,
                    supports_tools=supports_tools,
                    supports_vision=supports_vision,
                ),
            )
        # Fallback for raw providers (test doubles)
        supports_vision = False
        if self.vision_capability is not None:
            supports_vision = self.vision_capability()
        default_model = getattr(self.llm_provider, "default_model", None) or ""
        supports_tools = True
        if model_infos:
            for mi in model_infos:
                if mi.id == default_model:
                    supports_tools = mi.supports_tools
                    break
        return (
            ProviderCapability(
                provider_id="default",
                model_id=default_model,
                supports_tools=supports_tools,
                supports_vision=supports_vision,
            ),
        )

    def _build_model_requirements(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        state: AgentState,
    ) -> ModelRequirements:
        """Project ContextPlan + message content into LLM-owned ModelRequirements.

        - ``tools`` capability: tools list is non-empty.
        - ``vision`` capability: any user message has an image part.
        - ``context_window`` capability: token_need > 0 (from ContextPlan).
        - ``token_need``: from ContextPlan.token_allocation.total.
        """
        caps: set[str] = set()
        if tools:
            caps.add("tools")
        for msg in messages:
            if msg.get("role") == "user" and has_image_part(msg.get("content")):
                caps.add("vision")
                break
        token_need = 0
        plan = state.context_plan
        if plan is not None and plan.token_allocation is not None:
            token_need = plan.token_allocation.total
            if token_need > 0:
                caps.add("context_window")
        return ModelRequirements(
            capabilities=frozenset(caps),
            token_need=token_need,
        )

    @staticmethod
    def _extract_last_user_message(messages: list[dict[str, Any]]) -> str:
        """Extract text content from the last user message in a message list."""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                return extract_text(msg.get("content", ""))
        return ""

    @staticmethod
    def _inject_context_into_last_user_message(
        messages: list[dict[str, Any]], context: str,
    ) -> bool:
        """Inject merged pre_llm_call context into the last user message (ephemeral).

        String content -> append text with separator.
        Multimodal list content -> append a text part.
        Returns True if injected, False if no user message found.
        """
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    msg["content"] = content + "\n\n" + context
                    return True
                if isinstance(content, list):
                    msg["content"] = content + [{"type": "text", "text": context}]
                    return True
                # Unknown content type; keep searching for a user message
                # with string or list content.
        return False

    async def call_llm(self, state: AgentState, config: Optional[RunnableConfig] = None) -> AgentState:
        if self.is_cancelled(state.session_id):
            raise asyncio.CancelledError()
        configurable = (config or {}).get("configurable", {})
        model = configurable.get("model", "")
        options = configurable.get("options") or state.run_options
        call_start = time.monotonic()
        try:
            # 1. Build provider context (messages + tools)
            provider_context = self.context_service.build_provider_context(state, options)
            working_messages_for_call = provider_context.messages
            tools = provider_context.tools

            # Serialize request early -- reused for InformationFlow release,
            # payload logging, and usage retention.
            request_json_cache = json.dumps(working_messages_for_call, default=str, ensure_ascii=False)

            # 2. InformationFlow release to LLM_PROVIDER
            # If denied (e.g. secret content with redaction disabled), provider.chat
            # is NOT called (0 calls).  Return a stable error.
            llm_release = self._information_flow_service.release(
                request_json_cache, ReleaseTarget.LLM_PROVIDER, origin="llm_request",
            )
            if not llm_release.allowed:
                state.error = "information_release_denied"
                state.finish_reason = "error"
                return state

            # When the release applied a redaction transform, use the REDACTED
            # messages for provider.chat so secrets do not reach the provider.
            if llm_release.decision.transform == "redaction":
                working_messages_for_call = self._information_flow_service.redact_structured(
                    working_messages_for_call
                )
                # Re-serialize so logging and usage retention also use redacted content.
                request_json_cache = json.dumps(
                    working_messages_for_call, default=str, ensure_ascii=False,
                )

            # 3. ModelSelection via LLMPolicy
            # LLMPolicy checks placeholder resolution, tool/vision/context-window
            # capabilities, and ProviderConstraints (from InformationFlow decision).
            if self._model_cache is None:
                try:
                    self._model_cache = await self.llm_provider.list_models()
                except Exception:
                    self._model_cache = []
            provider_caps = self._build_provider_capabilities(self._model_cache)
            requirements = self._build_model_requirements(
                working_messages_for_call, tools, state,
            )
            active_provider_id = provider_caps[0].provider_id if provider_caps else ""
            constraints = ProviderConstraints(
                allowed_provider_ids=frozenset({active_provider_id}),
            )
            snapshot = self._policy_snapshot(state)
            llm_config = (
                LLMConfig(fallback_enabled=snapshot.llm.fallback_enabled)
                if snapshot is not None
                else self._llm_config
            )
            selection = self._llm_policy.evaluate(
                model, provider_caps, requirements, constraints, llm_config,
            )
            if selection.verdict is PolicyOutcome.DENY:
                # Vision unsupported -> friendly final reply (NOT 500)
                if selection.reason == "vision_capability_not_supported":
                    state.iteration_count += 1
                    state.final_message = {
                        "role": "assistant",
                        "content": "当前模型不支持图片输入，请切换到支持 vision 的模型后再试。",
                    }
                    state.finish_reason = "stop"
                    state.pending_tool_calls = []
                    return state
                # Other capability deny -> error
                state.error = selection.reason
                state.finish_reason = "error"
                return state

            selected_model = selection.model_id or model

            # 4. Budget reserve (fail-closed: if denied, provider.chat NOT called)
            # Use a rough chars/4 token estimate (matching the codebase's ~4
            # chars/token convention) so max_token_cost has rough enforcement
            # at reserve time.  The settle step adjusts to actual usage.
            estimated_tokens = len(request_json_cache) // 4
            reservation = await self._budget_service.reserve(
                state.run_id,
                BudgetReserveRequest(
                    kind=BudgetReserveKind.LLM_CALL,
                    estimated_tokens=estimated_tokens,
                ),
            )
            if reservation.outcome is PolicyOutcome.DENY:
                # T9: Set budget_exhausted flag; TurnPolicy will route to
                # finalize with BUDGET_EXHAUSTED. Finalize creates the
                # user-facing message.
                state.budget_exhausted = True
                state.pending_tool_calls = []
                return state

            # T10: pre_llm_call -- after working messages prepared (post-release,
            # post-redaction, post-model-selection, post-budget-reserve), before
            # provider.chat. Inject merged context into the provider CALL COPY's
            # last user message (EPHEMERAL: not written back to AgentState,
            # session, summary, or system prompt).
            pre_llm_results = await self._dispatch_hook(
                "pre_llm_call",
                session_id=state.session_id,
                model=selected_model,
                user_message=self._extract_last_user_message(working_messages_for_call),
                conversation_history=working_messages_for_call,
                iteration_count=state.iteration_count,
                metadata={},
            )
            if pre_llm_results and pre_llm_results[0]:
                merged_context = pre_llm_results[0]
                # Shallow-copy so injection does not mutate state.working_messages.
                working_messages_for_call = [dict(m) for m in working_messages_for_call]
                injected = self._inject_context_into_last_user_message(
                    working_messages_for_call, merged_context,
                )
                if not injected:
                    logger.warning(
                        "pre_llm_call context injection skipped: "
                        "no user message in working_messages for session=%s",
                        state.session_id,
                    )

            # 5. provider.chat (Provider Adapter = pure protocol conversion)
            try:
                result = await self.llm_provider.chat(
                    working_messages_for_call,
                    tools,
                    False,
                    selected_model,
                    options,
                )
            except Exception:
                # Release budget reservation on provider failure
                await self._budget_service.release(state.run_id, reservation)
                raise

            if not isinstance(result, LLMResult):
                await self._budget_service.release(state.run_id, reservation)
                state.error = "streaming provider result is not supported inside graph"
                state.finish_reason = "error"
                return state

            # 6. Budget settle (conservative: unknown usage keeps estimate)
            actual_token_cost = None
            if result.usage:
                total = result.usage.get("total_tokens")
                if isinstance(total, int):
                    actual_token_cost = total
            await self._budget_service.settle(
                state.run_id,
                reservation,
                BudgetActualUsage(token_cost=actual_token_cost),
            )

            # 7. Process result (T6/T7 preserved)
            tools_json_cache = json.dumps(tools, default=str, ensure_ascii=False) if tools else None
            gen_params = {k: v for k, v in options.items() if k not in _INTERNAL_OPTION_KEYS} if isinstance(options, dict) else {}
            gen_params_json_cache = json.dumps(gen_params, default=str, ensure_ascii=False) if gen_params else None
            state.iteration_count += 1
            state.final_message = result.message

            # ----- scrub memory context from final_message (T6) -----
            if self.external_memory_manager and state.final_message:
                content = state.final_message.get("content", "")
                if isinstance(content, str):
                    state.final_message["content"] = scrub_memory_context(content)

            state.finish_reason = result.finish_reason
            state.pending_tool_calls = result.message.get("tool_calls") or []
            if state.pending_tool_calls:
                state.assistant_tool_messages.append(result.message)
            state.working_messages.append(result.message)

            # T10: post_llm_call -- after provider success + protocol normalization
            # (state.final_message set, iteration_count incremented, working_messages
            # updated). Observer hook.
            await self._dispatch_hook(
                "post_llm_call",
                session_id=state.session_id,
                model=selected_model,
                assistant_content=extract_text(result.message.get("content", "")),
                tool_calls=result.message.get("tool_calls") or [],
                usage=result.usage or {},
                iteration_count=state.iteration_count,
            )

            # ----- T3: InformationFlow payload logging -----
            response_json_cache = json.dumps(result.message, default=str, ensure_ascii=False)
            if state.persist_messages:
                log_req = self._information_flow_service.release(
                    request_json_cache, ReleaseTarget.LLM_PAYLOAD_LOG, origin="llm_request",
                )
                log_resp = self._information_flow_service.release(
                    response_json_cache, ReleaseTarget.LLM_PAYLOAD_LOG, origin="llm_response",
                )
                if log_req.allowed and log_req.content is not None:
                    logger.info(
                        "LLM request: session=%s model=%s request=%s",
                        state.session_id, selected_model, log_req.content,
                    )
                if log_resp.allowed and log_resp.content is not None:
                    logger.info(
                        "LLM response: session=%s model=%s response=%s",
                        state.session_id, selected_model, log_resp.content,
                    )

            # ----- T3: usage recording -----
            if state.persist_messages and self.usage_service is not None and result.usage:
                latency_ms = int((time.monotonic() - call_start) * 1000)
                provider_kind, provider_name, real_model, requested_model = self._resolve_usage_meta(model)
                trigger_type = self._resolve_trigger_type(state)
                usage_req = self._information_flow_service.release(
                    request_json_cache, ReleaseTarget.USAGE_RETENTION, origin="llm_request",
                )
                usage_resp = self._information_flow_service.release(
                    response_json_cache, ReleaseTarget.USAGE_RETENTION, origin="llm_response",
                )
                usage_tools_result = None
                if tools_json_cache:
                    usage_tools_result = self._information_flow_service.release(
                        tools_json_cache, ReleaseTarget.USAGE_RETENTION, origin="llm_tools",
                    )
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
                        request_messages=usage_req.content if usage_req.allowed else None,
                        response_message=usage_resp.content if usage_resp.allowed else None,
                        tools=usage_tools_result.content if (usage_tools_result and usage_tools_result.allowed) else None,
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
        terminal_tool_called = False
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

            # T9/D039: Browser tool argument audit projection. Compute a safe
            # copy for persistence/display/stream/hooks. Original arguments
            # (parsed_arguments) are kept for ToolService.execute and
            # ToolPolicy.authorize_once. Projection failure is fail-closed:
            # the browser tool must NOT execute and must NOT persist raw args.
            projection_failed = False
            if tool_name.startswith("browser_"):
                try:
                    display_arguments = project_browser_tool_arguments(
                        tool_name, parsed_arguments,
                    )
                except Exception:
                    logger.warning(
                        "browser tool argument projection failed for %s",
                        tool_name, exc_info=True,
                    )
                    projection_failed = True
                    display_arguments = {}
            else:
                display_arguments = parsed_arguments

            # T10: pre_tool_call -- before ToolService evaluate_execution.
            # Observer hook (no block). Fires for every tool call including
            # invalid arguments (which skip evaluation but still produce a result).
            await self._dispatch_hook(
                "pre_tool_call",
                session_id=state.session_id,
                tool_call_id=tool_id,
                tool_name=tool_name,
                args=display_arguments,
                metadata={},
            )

            # 工具执行前 - pending 事件
            state.stream_tool_events.append(ChatEvent(
                ChatEventType.TOOL_CALL_DELTA,
                tool_call={
                    "id": tool_id,
                    "name": tool_name,
                    "arguments": display_arguments,
                    "status": "pending",
                },
            ))
            await self._emit_stream_tool_event(state.stream_tool_events[-1], options)

            if projection_failed:
                # T9/D039: fail-closed -- browser tool must NOT execute when
                # argument projection fails, and must NOT persist raw args.
                result = ToolResult(
                    tool_call_id=tool_id,
                    tool_name=tool_name,
                    status=ToolResultStatus.ERROR,
                    content={"error": "browser_argument_projection_failed"},
                )
            elif invalid_arguments:
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
                        # Browser host_grant_required signal: BrowserToolExecutor
                        # returns PERMISSION_DENIED with approval_kind=host_grant
                        # when a host_cdp session is pending. Route it to the
                        # Chat CONFIRM card flow (reusing _request_tool_approval).
                        if (
                            result.status is ToolResultStatus.PERMISSION_DENIED
                            and isinstance(result.content, dict)
                            and result.content.get("approval_kind") == "host_grant"
                        ):
                            result = await self._request_browser_host_grant_approval(
                                request,
                                state.session_id,
                                context,
                                result.content,
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
                    "arguments": display_arguments,
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

            # T10: post_tool_call -- after final ToolResult (success/denied/error/
            # timeout). Observer hook. result is a JSON-compatible snapshot;
            # args/metadata are shallow-copied by invoke_hook. Does not expose
            # ToolExecutor or trusted_metadata.
            await self._dispatch_hook(
                "post_tool_call",
                session_id=state.session_id,
                tool_call_id=tool_id,
                tool_name=result.tool_name,
                args=display_arguments,
                result=result_payload,
                duration_ms=result.duration_ms,
                metadata={},
            )

            # T10: transform_tool_result -- after post_tool_call, before tool
            # message encode + persist. Returns first valid value to replace
            # content (string or JSON-compatible dict/list/scalar).
            transform_results = await self._dispatch_hook(
                "transform_tool_result",
                session_id=state.session_id,
                tool_call_id=tool_id,
                tool_name=result.tool_name,
                args=display_arguments,
                result=result_payload,
                duration_ms=result.duration_ms,
                metadata={},
            )
            if transform_results and transform_results[0] is not None:
                result_payload["content"] = transform_results[0]

            state.tool_results.append(result_payload)
            state.working_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result.tool_call_id,
                    "name": result.tool_name,
                    "content": json.dumps(result_payload, ensure_ascii=False, default=str),
                }
            )
            if state.persist_messages:
                await self._runtime_memory.save_tool_call_if_allowed(
                    ToolCall(
                        id=result.tool_call_id,
                        session_id=state.session_id,
                        tool_name=result.tool_name,
                        arguments=display_arguments,
                        result=result_payload,
                        status=result.status.value,
                        duration_ms=result.duration_ms,
                    )
                )
            # T10: ui.artifact card -- persist a structured card for
            # conversational artifact write tools on SUCCESS only. Reads the
            # original result.content (never the transform_tool_result-mutated
            # payload). Failures, approval rejections, read-only tools and
            # missing-field results write NO card; a CAS conflict (success=
            # false) leaves the previous card untouched. Best-effort: a write
            # failure is logged and never breaks the tool flow.
            if (state.persist_messages
                    and result.status is ToolResultStatus.SUCCESS
                    and result.tool_name in _ARTIFACT_WRITE_TOOLS):
                await self._persist_artifact_card(state, result)
            if result.terminal:
                # Terminal tool semantics are decided by the server-side
                # executor. Stop after persisting this result; do not make a
                # further LLM call that can repeat the same terminal intent.
                terminal_tool_called = True
                break
        state.pending_tool_calls = []
        if terminal_tool_called:
            # TurnPolicy treats a final message without pending calls as STOP.
            # TaskAgentExecutor reads the authoritative task intent event for
            # its outcome and user-facing summary/error.
            state.final_message = {"role": "assistant", "content": ""}
            state.finish_reason = "stop"
        else:
            state.final_message = None
        return state

    async def _persist_artifact_card(
        self, state: AgentState, result: ToolResult,
    ) -> None:
        """Best-effort ui.artifact card for a successful artifact write tool.

        Extracts the structured success payload from ``result.content`` (a JSON
        string emitted by ArtifactToolExecutor) and persists a role=system,
        name=ui.artifact message whose card is exactly
        ``{artifact_id, revision_id, name, kind, revision_number,
        publish_sync_state}`` (spec line 205). Skips silently when the payload
        is not a dict, lacks success, or is missing the required artifact_id /
        revision_id. Never raises: a persistence failure is logged and the
        tool flow continues unaffected.
        """
        content = result.content
        if isinstance(content, str):
            try:
                payload = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                return
        else:
            payload = content
        if not isinstance(payload, dict) or not payload.get("success"):
            return
        artifact_id = payload.get("artifact_id")
        revision_id = payload.get("revision_id")
        if not artifact_id or not revision_id:
            return
        card = {
            "artifact_id": artifact_id,
            "revision_id": revision_id,
            "name": payload.get("name"),
            "kind": payload.get("kind"),
            "revision_number": payload.get("revision_number"),
            "publish_sync_state": payload.get("publish_sync_state"),
        }
        try:
            await self._runtime_memory.append_system_named_message(
                state.session_id,
                "ui.artifact",
                content=f"制品已更新: {payload.get('name') or artifact_id}",
                card=card,
            )
        except Exception:
            logger.warning(
                "ui.artifact card persist failed: tool=%s session=%s",
                result.tool_name, state.session_id, exc_info=True,
            )

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

    @staticmethod
    def _project_approval_arguments(
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Project browser tool arguments for approval display.

        Returns a new dict (never the original). For browser tools, sensitive
        arguments (typed text, URL query/fragment) are stripped/redacted.
        ApprovalDecider/authorize_once still bind the original arguments
        via ToolCallRequest; only the displayed ApprovalRequest copy is
        projected.
        """
        if tool_name.startswith("browser_"):
            try:
                return project_browser_tool_arguments(tool_name, arguments)
            except Exception:
                return {}
        return dict(arguments)

    @staticmethod
    def _project_tool_calls_for_persistence(
        tool_calls: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Project browser tool arguments in tool_calls for persistence.

        Creates a shallow copy of tool_calls with projected arguments for
        browser tools (strips typed text, URL query/fragment). Non-browser
        tool_calls are shallow-copied with original arguments. Does not
        mutate the input.
        """
        projected: list[dict[str, Any]] = []
        for tc in tool_calls:
            tc_copy = dict(tc)
            function = dict(tc_copy.get("function", {}))
            tool_name = function.get("name", "")
            raw_args = function.get("arguments", {})
            if tool_name.startswith("browser_"):
                parsed = raw_args
                if isinstance(raw_args, str):
                    try:
                        parsed = json.loads(raw_args)
                    except json.JSONDecodeError:
                        parsed = {}
                if not isinstance(parsed, dict):
                    parsed = {}
                try:
                    safe_args = project_browser_tool_arguments(tool_name, parsed)
                except Exception:
                    safe_args = {}
                function["arguments"] = json.dumps(
                    safe_args, ensure_ascii=False, default=str,
                )
            else:
                function["arguments"] = raw_args
            tc_copy["function"] = function
            projected.append(tc_copy)
        return projected

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
        # T9/D039: Project browser tool arguments for approval display.
        # ApprovalDecider/authorize_once still bind the original arguments
        # via ToolCallRequest; only the displayed ApprovalRequest copy is
        # projected.
        approval_request = ApprovalRequest(
            session_id=context.session_id or state_session_id,
            tool_call_id=request.id,
            tool_name=approval.name,
            arguments=self._project_approval_arguments(
                approval.name, request.arguments,
            ),
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

    async def _request_browser_host_grant_approval(
        self,
        request: ToolCallRequest,
        state_session_id: str,
        context: ToolExecutionContext | None,
        signal: dict[str, Any],
    ) -> ToolResult:
        """Route a browser host_grant_required signal to the Chat CONFIRM card.

        Reuses _request_tool_approval's decider flow with host-grant metadata
        so claim resolves the Future synchronously (no grant_host in claim).
        After decider returns allowed, execute grant_host in this async context;
        on success re-run the original tool call, on failure return
        PERMISSION_DENIED (host_grant_failed).
        """
        if self._browser_dashboard_service is None:
            return self._permission_denied_result(request, "host_grant_required")

        decider = context.approval_decider if context is not None else None
        if decider is None:
            return self._permission_denied_result(request, "approval_required")

        browser_session_id = signal.get("browser_session_id")
        if not browser_session_id:
            return self._permission_denied_result(request, "host_grant_required")

        approval_request = ApprovalRequest(
            session_id=context.session_id or state_session_id,
            tool_call_id=request.id,
            tool_name=request.name,
            arguments=self._project_approval_arguments(
                request.name, request.arguments,
            ),
            description="授予主机浏览器访问权限。批准后 Agent 可操作专用 Chrome 窗口。",
            risk_level=RiskLevel.CONFIRM,
            metadata={
                "approval_kind": "host_grant",
                "browser_session_id": browser_session_id,
            },
        )
        try:
            raw_decision = decider(approval_request)
            if isawaitable(raw_decision):
                raw_decision = await raw_decision
        except Exception:
            return self._permission_denied_result(request, "approval_failed")

        if not isinstance(raw_decision, ApprovalDecision):
            return self._permission_denied_result(request, "invalid_approval_decision")
        if not raw_decision.allowed:
            return self._permission_denied_result(
                request, raw_decision.reason or "approval_denied",
            )

        # decider allowed -> execute grant_host in async context.
        from app.domain.browser_policy import BROWSER_POLICY_VERSION
        actor_id = (
            context.trusted_metadata.get("actor_id")
            if context and context.trusted_metadata
            else None
        )
        try:
            grant_result = self._browser_dashboard_service.grant_host(
                browser_session_id,
                context.session_id or state_session_id,
                actor_id,
                BROWSER_POLICY_VERSION,
                self._browser_host_grant_ttl_seconds,
            )
            if isawaitable(grant_result):
                grant_result = await grant_result
        except Exception:
            return self._permission_denied_result(request, "host_grant_failed")

        # grant_host returns dict {ok, error} per BrowserDashboardService.
        ok = bool(grant_result.get("ok")) if isinstance(grant_result, dict) else False
        if not ok:
            return self._permission_denied_result(request, "host_grant_failed")

        # grant succeeded -> re-run the original tool call (session is now active).
        try:
            authorized_context = self.tool_service.authorize_once(
                request, context, evaluation=None,
            )
        except (ValueError, TypeError):
            authorized_context = context
        return await self.tool_service.execute(
            request, authorized_context,
        )

    async def _emit_stream_tool_event(self, event: ChatEvent, options: dict[str, Any] | None) -> None:
        sink = (options or {}).get("stream_event_sink")
        if not callable(sink):
            return
        # Structured redaction of tool call arguments before publishing
        event = self._redact_tool_event(event)
        result = sink(event)
        if isawaitable(result):
            await result
        await asyncio.sleep(0.001)

    def _redact_tool_event(self, event: ChatEvent) -> ChatEvent:
        """Apply structured redaction to tool call arguments in stream events."""
        if not event.tool_call:
            return event
        tool_call = dict(event.tool_call)
        if "arguments" in tool_call:
            tool_call["arguments"] = self._information_flow_service.redact_structured(
                tool_call["arguments"]
            )
        return ChatEvent(
            type=event.type,
            content=event.content,
            tool_call=tool_call,
            finish_reason=event.finish_reason,
            error=event.error,
            metadata=event.metadata,
        )

    async def update_memory(self, state: AgentState) -> AgentState:
        if self.is_cancelled(state.session_id):
            raise asyncio.CancelledError()
        assistant_messages = [*state.assistant_tool_messages]
        # T10: final_message is NOT persisted here; finalize persists it after
        # transform_llm_output. This ensures DB content matches client-visible
        # content (both use the transformed text). Previously, final_message
        # was appended here, which pre-empted the transform.
        persist = state.persist_messages
        assistant_source = state.message_source
        for assistant_message in assistant_messages:
            content = assistant_message.get("content", "")
            tool_calls = assistant_message.get("tool_calls") or []
            if tool_calls:
                # T9/D039: Project browser tool arguments in tool_calls for
                # persistence to prevent sensitive args (typed text, URL
                # query/fragment) from leaking into persisted assistant messages.
                projected_tool_calls = self._project_tool_calls_for_persistence(tool_calls)
                content = {"content": content, "tool_calls": projected_tool_calls}
            if persist:
                await self._runtime_memory.append_assistant_message(
                    state.session_id, content, source=assistant_source,
                )
        state.assistant_tool_messages = []
        for result in state.tool_results:
            if persist:
                await self._runtime_memory.append_tool_message(
                    state.session_id,
                    json.dumps(result, ensure_ascii=False, default=str),
                    tool_call_id=result.get("tool_call_id"),
                    name=result.get("name"),
                )
        state.tool_results = []
        if state.persist_messages:
            await self._runtime_memory.save_task_state_if_allowed(
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
        # T10: pre_finalize -- at finalize node entry, before persisting final
        # message. Observer hook.
        await self._dispatch_hook(
            "pre_finalize",
            session_id=state.session_id,
            content=extract_text(state.final_message.get("content", "")) if state.final_message else "",
            finish_reason=state.finish_reason or "",
            error=state.error,
            metadata={},
        )
        # T9: TurnPolicy confirms terminal + end_reason
        decision = self._evaluate_turn(state)
        if decision.terminal and decision.end_reason is not None:
            end_reason = decision.end_reason
            if end_reason == EndReason.ITERATION_LIMIT and not state.error:
                state.error = "iteration limit reached"
                state.finish_reason = _END_REASON_TO_FINISH_REASON[end_reason]
                # Clear final_message (LLM result with tool_calls) so the
                # error message is created for the user. The LLM result was
                # already saved by update_memory if we went through "memory".
                state.final_message = None
            elif end_reason == EndReason.BUDGET_EXHAUSTED and not state.error:
                state.finish_reason = _END_REASON_TO_FINISH_REASON[end_reason]
                if not state.final_message:
                    state.final_message = {
                        "role": "assistant",
                        "content": "已达到用量上限，请稍后重试或联系管理员。",
                    }
            elif end_reason == EndReason.DEADLINE and not state.error:
                state.error = "turn timeout exceeded"
                state.finish_reason = _END_REASON_TO_FINISH_REASON[end_reason]
                # Edge case: if deadline is hit after a successful LLM call
                # (LLM returned tool_calls) but before the next iteration,
                # routing goes directly to finalize (skipping update_memory).
                # The LLM response was appended to working_messages in
                # call_llm but is NOT persisted via update_memory -> response
                # lost from the store. This is an acceptable edge case for T9.
                # TODO: future graceful handling could route through memory
                # before finalize for DEADLINE (like ITERATION_LIMIT does).
                state.final_message = None
            elif end_reason == EndReason.CANCELLED:
                # Set state.error so run_status = FAILED (not COMPLETED) and
                # the error-message creation path produces a user-facing message.
                state.error = state.error or "cancelled"
                state.finish_reason = _END_REASON_TO_FINISH_REASON[end_reason]
            elif end_reason == EndReason.ERROR:
                state.finish_reason = state.finish_reason or "error"
            elif end_reason == EndReason.STOP:
                state.finish_reason = state.finish_reason or "stop"
        else:
            # Non-terminal at finalize (shouldn't happen): treat as stop
            if not state.finish_reason:
                state.finish_reason = "stop"

        state.run_status = RunStatus.FAILED if state.error else RunStatus.COMPLETED
        if state.error and not state.final_message:
            state.final_message = {"role": "assistant", "content": _error_message_for_user(state)}
            # error message also needs cleaning if it contains any tags
            content = state.final_message.get("content", "")
            if isinstance(content, str):
                state.final_message["content"] = scrub_memory_context(content)
            # T10: persist deferred to after transform_llm_output below.

        # T10: transform_llm_output -- after final assistant text determined,
        # BEFORE persist + send. Returns first non-empty string to replace
        # content. Applied to both normal and error final messages.
        if state.final_message:
            fm_content = state.final_message.get("content")
            if isinstance(fm_content, str) and fm_content:
                transform_results = await self._dispatch_hook(
                    "transform_llm_output",
                    session_id=state.session_id,
                    content=fm_content,
                    finish_reason=state.finish_reason or "",
                    metadata={},
                )
                if transform_results and transform_results[0]:
                    state.final_message["content"] = transform_results[0]

        # T10: Persist final_message after transform_llm_output. Previously,
        # the normal-path final_message was persisted in update_memory and the
        # error-path message was persisted in the error block above. Now both
        # are unified here so DB content matches client-visible text (both use
        # the post-transform content).
        if state.final_message and state.persist_messages:
            persist_content = state.final_message.get("content", "")
            persist_tool_calls = state.final_message.get("tool_calls") or []
            if persist_tool_calls:
                persist_content = {"content": persist_content, "tool_calls": persist_tool_calls}
            await self._runtime_memory.append_assistant_message(
                state.session_id, persist_content, source=state.message_source,
            )

        # ----- 外部记忆同步 -----
        # call_llm 已经清理过 final_message，这里同步的是干净内容
        if state.persist_messages and self.external_memory_manager and state.final_message:
            user_content = self._extract_user_content(state.input_messages)
            assistant_content = self._extract_assistant_content(state.final_message)
            agent_context = "unattended"  # fail-closed default
            enabled_override = None
            tool_ctx = state.run_options.get("tool_execution_context")
            if tool_ctx is not None and isinstance(tool_ctx, ToolExecutionContext):
                agent_context = tool_ctx.trusted_metadata.get("agent_context", "unattended")
                enabled_override = tool_ctx.enabled_override
            # Derive execution_mode from run options for policy evaluation
            mode_str = state.run_options.get("execution_context_mode") or "realtime"
            try:
                execution_mode = ExecutionMode(mode_str)
            except ValueError:
                execution_mode = ExecutionMode.REALTIME
            await self._runtime_memory.sync_external_if_allowed(
                user_content, assistant_content,
                session_id=state.session_id,
                agent_context=agent_context,
                execution_mode=execution_mode,
                enabled_override=enabled_override,
            )
        # ----- 结束新增 -----

        if state.persist_messages:
            await self._runtime_memory.save_task_state_if_allowed(
                TaskState(
                    session_id=state.session_id,
                    status=state.run_status.value,
                    iteration_count=state.iteration_count,
                    last_error=state.error,
                )
            )

        # T9: Close Budget account on terminal (release unsettled reservations)
        await self._budget_service.close(state.run_id)

        # T11: Post-finalize skill self-evolution nudge. Fire-and-forget;
        # _post_finalize_nudge guards on evolution_service is None and on
        # nudge_interval. maybe_trigger spawns a background asyncio task.
        # Wrapped so evolution never breaks the turn finalize path.
        if state.persist_messages and self.evolution_service is not None:
            try:
                await self._post_finalize_nudge(
                    state.session_id,
                    state.iteration_count,
                    state.working_messages,
                )
            except Exception:
                logger.warning(
                    "skill evolution nudge failed for session=%s",
                    state.session_id,
                    exc_info=True,
                )

        # Curator 周期维护：finalize 后若能证明空闲（距上次 finalize）则触发。
        # 首次 finalize（_last_finalize_at is None）无法计算 idle，不自动触发，
        # 避免 min_idle_hours 形同虚设。maybe_run_curator 内部 fire-and-forget
        # 且 never raises。CLI 手动 run 走 run_curator_review，不经此路径。
        if (
            state.persist_messages
            and self.curator_service is not None
            and self._last_finalize_at is not None
        ):
            idle = (
                datetime.now(timezone.utc) - self._last_finalize_at
            ).total_seconds()
            try:
                asyncio.create_task(
                    self.curator_service.maybe_run_curator(idle_for_seconds=idle)
                )
            except Exception:
                logger.warning(
                    "curator trigger failed for session=%s",
                    state.session_id,
                    exc_info=True,
                )
        if state.persist_messages:
            self._last_finalize_at = datetime.now(timezone.utc)

        return state

    async def _post_finalize_nudge(
        self,
        session_id: str,
        turn_count: int,
        recent_messages: list[dict[str, Any]],
    ) -> None:
        """Post-finalize hook for background skill self-evolution.

        Called after a turn finalizes. If evolution_service is configured and
        turn_count hits the nudge_interval, builds a digest from recent
        messages and triggers a background skill review. All exceptions are
        swallowed by maybe_trigger/run_background_review (fire-and-forget).
        """
        if self.evolution_service is None:
            return
        if turn_count % self.nudge_interval != 0:
            return
        parts: list[str] = []
        for msg in recent_messages:
            if not isinstance(msg, dict) or msg.get("role") == "system":
                continue
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                parts.append(content)
        digest = "\n".join(parts)
        if len(digest) > 2000:
            digest = digest[:2000]
        await self.evolution_service.maybe_trigger(session_id, turn_count, digest)

    def _after_llm(self, state: AgentState) -> str:
        """Route after call_llm using TurnPolicy.

        Maps TurnDecision to graph node names:
        - EXECUTE_TOOLS -> "tools"
        - terminal STOP (final_message, no tools) -> "memory" (save, then finalize)
        - terminal ITERATION_LIMIT -> "memory" (save intermediate, then finalize)
        - terminal ERROR/CANCELLED/DEADLINE/BUDGET_EXHAUSTED -> "finalize"
        - non-terminal continue -> "memory" (saves any assistant message)
        """
        decision = self._evaluate_turn(state)
        if decision.next_step is TurnNextStep.EXECUTE_TOOLS:
            return "tools"
        if decision.terminal:
            if decision.end_reason in (EndReason.STOP, EndReason.ITERATION_LIMIT):
                # Save final_message / intermediate via memory before finalize
                return "memory"
            # Error/cancel/deadline/budget: go directly to finalize
            return "finalize"
        return "memory"

    def _after_memory(self, state: AgentState) -> str:
        """Route after update_memory using TurnPolicy.

        Terminal decisions go to finalize; non-terminal continue to call_llm.
        """
        decision = self._evaluate_turn(state)
        if decision.terminal:
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
        return "已达到工具调用上限，模型没有生成最终回答。请查看工具调用，或缩小问题后重试。"
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

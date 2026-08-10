from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from collections.abc import Callable
from typing import Any

from app.application.external_memory_manager import ExternalMemoryManager
from app.application.policy_projections import (
    build_context_policy_request,
    project_messages,
    project_working_messages,
)
from app.application.prompt_builder import build_system_prompt
from app.application.runtime_memory_service import RuntimeMemoryService
from app.application.policy_snapshot import RunPolicySnapshot
from app.application.tool_service import ToolService
from app.domain.agent import AgentState, RunStatus
from app.domain.context import CONTEXT_SUMMARY_PREFIX, ContextEngine, ProviderContext
from app.domain.context_policy import ContextPlan, ContextPolicy, DefaultContextPolicy
from app.domain.memory import MemoryStore
from app.domain.session import ConversationMessage, Summary
from app.domain.tool_policy import ToolExposurePolicy
from app.utils.content_utils import extract_text, prepend_text_part


logger = logging.getLogger(__name__)


class ContextService:
    """Application service for preparing and adapting context for model calls.

    T7 refactor: ContextService is now a plan EXECUTOR.  ContextPolicy
    decides which messages to select, when/how to compress, where to inject
    external memory, and how to allocate tokens.  ContextService executes
    the ContextPlan; ContextEngine (ContextCompressor) only executes the
    CompressionPlan portion.
    """

    def __init__(
        self,
        memory_store: MemoryStore,
        *,
        tool_service: ToolService | None = None,
        external_memory_manager: ExternalMemoryManager | None = None,
        context_engine: ContextEngine | None = None,
        usage_service: Any = None,
        skill_service: Any = None,
        is_cancelled: Callable[[str], bool] | None = None,
        runtime_memory_service: RuntimeMemoryService | None = None,
        context_policy: ContextPolicy | None = None,
        browser_guidance: str | None = None,
        artifact_guidance: str | None = None,
    ):
        self.memory_store = memory_store
        self.tool_service = tool_service
        self.external_memory_manager = external_memory_manager
        self.context_engine = context_engine
        self.usage_service = usage_service
        self.skill_service = skill_service
        self._is_cancelled = is_cancelled or (lambda _session_id: False)
        self._runtime_memory = runtime_memory_service or RuntimeMemoryService(memory_store)
        self._context_policy = context_policy or DefaultContextPolicy()
        self._browser_guidance = browser_guidance
        self._artifact_guidance = artifact_guidance

    # ------------------------------------------------------------------
    # Engine config extraction (for ContextPolicy request)
    # ------------------------------------------------------------------

    def _get_engine_config(self, state: AgentState | None = None) -> dict[str, Any]:
        """Read config values from the context_engine (ContextCompressor).

        Uses getattr so FakeContextEngine (test doubles without these
        attributes) falls back to ContextPolicy defaults.
        """
        if state is not None:
            snapshot = state.run_options.get("_policy_snapshot")
            if isinstance(snapshot, RunPolicySnapshot):
                config = snapshot.context_config
                return {
                    "context_length": config.context_length,
                    "compression_threshold": config.compression_threshold,
                    "compression_target_ratio": config.compression_target_ratio,
                    "protect_first_n": config.protect_first_n,
                    "protect_last_n": config.protect_last_n,
                    "cooldown_seconds": config.cooldown_seconds,
                    "tail_budget_enabled": config.tail_budget_enabled,
                }
        engine = self.context_engine
        if engine is None:
            return {}
        return {
            "context_length": getattr(engine, "context_length", 32000),
            "compression_threshold": getattr(engine, "threshold_percent", 0.50),
            "compression_target_ratio": getattr(engine, "summary_target_ratio", 0.20),
            "protect_first_n": getattr(engine, "protect_first_n", 3),
            "protect_last_n": getattr(engine, "protect_last_n", 10),
            "cooldown_seconds": getattr(engine, "cooldown_seconds", 300),
            "tail_budget_enabled": getattr(engine, "tail_budget_enabled", False),
        }

    def _check_cooldown(self) -> bool:
        """Check if the context engine is currently in cooldown."""
        if self.context_engine is None:
            return False
        check = getattr(self.context_engine, "is_in_cooldown", None)
        if callable(check):
            return bool(check())
        return False

    def _create_plan_from_state(
        self,
        state: AgentState,
        non_system: list[dict[str, Any]] | None = None,
    ) -> ContextPlan:
        """Fallback: create a ContextPlan when build_context_state wasn't called.

        Projects working_messages to candidates and evaluates the policy.
        """
        if non_system is None:
            leading_count = 0
            for msg in state.working_messages:
                if msg.get("role") == "system":
                    leading_count += 1
                else:
                    break
            non_system = state.working_messages[leading_count:]

        candidates_msg = project_working_messages(non_system)
        from app.domain.context_policy import ContextCandidateSet

        candidates = ContextCandidateSet(messages=candidates_msg)
        config = self._get_engine_config(state)
        force = bool(state.run_options.get("force_compress", False))
        in_cooldown = self._check_cooldown()
        request = build_context_policy_request(
            candidates=candidates,
            force=force,
            in_cooldown=in_cooldown,
            existing_summary=state.summary,
            **config,
        )
        return self._context_policy.evaluate(request)

    # ------------------------------------------------------------------
    # Context preparation (plan execution)
    # ------------------------------------------------------------------

    async def prepare_context(self, state: AgentState) -> AgentState:
        """Prepare the context frame for the next LLM call."""
        state = await self.build_context_state(state)
        return await self.compress_prepared_context(state)

    async def build_context_state(self, state: AgentState) -> AgentState:
        if self._is_cancelled(state.session_id):
            raise asyncio.CancelledError()
        messages = await self._runtime_memory.read_session_messages(state.session_id)
        # 历史 role=system 消息（ui.task_command 命令记录 / ui.task_lifecycle 生命周期 /
        # ui.task_result 最终结果）是 UI 通知，排除出模型候选、压缩与外部记忆钩子；运行时
        # system prompt 仍由 build_system_prompt 构建并作为 working_messages[0]，不落盘。
        eligible_messages = [m for m in messages if m.role != "system"]
        summary = await self._runtime_memory.get_summary_if_allowed(state.session_id)
        enabled_override = state.run_options.get("external_memory_enabled")

        # -- T7: Evaluate ContextPolicy to produce ContextPlan --
        from app.domain.context_policy import ContextCandidateSet

        candidates = ContextCandidateSet(
            messages=project_messages(eligible_messages),
        )
        config = self._get_engine_config(state)
        force = bool(state.run_options.get("force_compress", False))
        in_cooldown = self._check_cooldown()
        request = build_context_policy_request(
            candidates=candidates,
            force=force,
            in_cooldown=in_cooldown,
            existing_summary=summary.summary if summary else "",
            **config,
        )
        plan = self._context_policy.evaluate(request)
        state.context_plan = plan

        # -- Execute message selection --
        # NOTE: plan.selected_message_ids is currently advisory. The executor
        # uses _build_latest_compressed_context + _sanitize_conversation_tool_pairs
        # + _dedupe_trailing, which implement equivalent selection logic.
        # There is a subtle edge-case divergence: the policy's _select_messages
        # drops ALL is_summarized non-summary messages when a latest summary
        # exists; the executor's _build_latest_compressed_context keeps
        # summarized messages that appear AFTER the latest summary when
        # first_summarized_idx == -1 (no summarized messages before the summary).
        # The executor's behavior is tested and correct for the current
        # incremental-compression flow. TODO: wire plan.selected_message_ids
        # directly in a future refactor for full consistency.
        context_messages = _sanitize_conversation_tool_pairs(
            _build_latest_compressed_context(eligible_messages)
        )
        context_messages = _dedupe_trailing(context_messages, state.input_messages)
        state.context_message_ids = [m.id for m in context_messages]

        skills_index: str | None = None
        if self.skill_service is not None:
            try:
                skills_index = await self.skill_service.build_skills_index() or None
            except Exception:
                logger.warning("build_skills_index failed", exc_info=True)

        state.working_messages = [
            {
                "role": "system",
                "content": build_system_prompt(
                    self.external_memory_manager,
                    enabled_override,
                    skills_index,
                    browser_guidance=self._browser_guidance,
                    artifact_guidance=self._artifact_guidance,
                ),
            },
            *[_message_to_provider(message) for message in context_messages],
            *state.input_messages,
        ]
        state.summary = summary.summary if summary else ""
        state.run_status = RunStatus.RUNNING
        return state

    async def compress_prepared_context(self, state: AgentState) -> AgentState:
        if self._is_cancelled(state.session_id):
            raise asyncio.CancelledError()
        if self.context_engine is None:
            return state

        leading_system = []
        idx = 0
        while idx < len(state.working_messages) and state.working_messages[idx].get("role") == "system":
            leading_system.append(state.working_messages[idx])
            idx += 1
        non_system = state.working_messages[idx:]

        # -- T7: Create/re-evaluate ContextPlan from full non_system context --
        # The plan from build_context_state was based on history messages only;
        # here we re-evaluate based on the full working_messages (which include
        # input_messages) so the compression decision considers the total token
        # count that will be sent to the model.
        plan = self._create_plan_from_state(state, non_system)
        state.context_plan = plan

        if plan.compression is None:
            return state

        force = plan.compression.force

        rescued_context = ""
        if state.persist_messages and self.external_memory_manager:
            enabled_override = state.run_options.get("external_memory_enabled")
            # pre_compress_all is exempt from RuntimeMemoryService gating: it does
            # NOT read from external memory stores. It passes the current
            # conversation messages to each provider's on_pre_compress hook,
            # which extracts rescue facts from already-policy-gated content
            # (prefetch was gated via read_external_if_allowed in
            # build_provider_messages). Re-gating here would be redundant.
            rescued_context = self.external_memory_manager.pre_compress_all(
                non_system,
                session_id=state.session_id,
                enabled_override=enabled_override,
            )

        result = await self.context_engine.compress(
            non_system,
            existing_summary=state.summary,
            force=force,
        )
        if not result.compressed:
            return state

        summary_dicts = [
            m for m in result.messages
            if m.get("role") == "user"
            and isinstance(m.get("content"), str)
            and m["content"].startswith(CONTEXT_SUMMARY_PREFIX)
        ]
        if len(summary_dicts) != 1:
            logger.error(
                "prepare_context: expected exactly 1 summary message in result.messages, got %d",
                len(summary_dicts),
            )
            return state

        next_summary = f"{rescued_context}\n\n{result.summary}".strip() if rescued_context else result.summary
        summary_dict = summary_dicts[0]
        if not state.persist_messages:
            state.summary = next_summary
            state.working_messages = leading_system + result.messages
            return state

        summary_message = ConversationMessage(
            role="user",
            content=summary_dict["content"],
            is_summary=True,
        )

        try:
            returned_message = await self._runtime_memory.append_summary_message_if_allowed(
                state.session_id,
                summary_message,
            )
        except Exception as exc:
            logger.error("prepare_context: append_summary_message failed: %s", exc)
            return state

        if result.summarized_message_indices:
            ctx_ids = state.context_message_ids
            ctx_len = len(ctx_ids)
            middle_ids = [
                ctx_ids[i] for i in result.summarized_message_indices
                if 0 <= i < ctx_len
            ]
            if middle_ids:
                try:
                    await self._runtime_memory.mark_messages_summarized_if_allowed(state.session_id, middle_ids)
                except Exception as exc:
                    logger.warning(
                        "prepare_context: mark_messages_summarized failed: %s",
                        exc,
                    )

        if next_summary:
            try:
                await self._runtime_memory.save_summary_if_allowed(
                    Summary(
                        session_id=state.session_id,
                        summary=next_summary,
                        source_message_id=returned_message.id,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "prepare_context: save_summary failed, dashboard may lag one round: %s",
                    exc,
                )

        state.summary = next_summary
        state.working_messages = leading_system + result.messages

        if (
            self.usage_service is not None
            and result.original_tokens is not None
            and result.compressed_tokens is not None
        ):
            try:
                before_messages_json = None
                after_messages_json = None
                try:
                    before_segment = [
                        non_system[i]
                        for i in result.summarized_message_indices
                        if 0 <= i < len(non_system)
                    ]
                    if before_segment:
                        before_messages_json = json.dumps(
                            before_segment,
                            default=str,
                            ensure_ascii=False,
                        )
                    if summary_dicts:
                        after_messages_json = json.dumps(
                            summary_dicts,
                            default=str,
                            ensure_ascii=False,
                        )
                except Exception:
                    logger.warning(
                        "prepare_context: serialize compression messages failed",
                        exc_info=True,
                    )
                await self.usage_service.record_compression(
                    session_id=state.session_id,
                    before_tokens=result.original_tokens,
                    after_tokens=result.compressed_tokens,
                    before_messages=before_messages_json,
                    after_messages=after_messages_json,
                )
            except Exception:
                logger.exception(
                    "compression recording failed for session=%s",
                    state.session_id,
                )
        return state

    def build_provider_messages(self, state: AgentState) -> list[dict[str, Any]]:
        """Build provider-visible messages, including temporary memory retrieval.

        T7: The ContextPlan's InjectionPlan is currently advisory. The executor
        implements equivalent injection logic here (inject into the last message
        if its role is "user"). Note a subtle difference: the plan's
        InjectionPlan targets the last non-summary user message found via
        reversed(candidate messages); this executor targets the last working
        message if it is "user" (which includes input_messages appended after
        history). TODO: wire plan.injection.target_message_id directly in a
        future refactor for full consistency.
        """
        if not self.external_memory_manager or not state.working_messages:
            return state.working_messages

        last_idx = len(state.working_messages) - 1
        last_msg = state.working_messages[last_idx]
        api_messages = state.working_messages.copy()
        enabled_override: list[str] | None = state.run_options.get("external_memory_enabled")
        if last_msg.get("role") == "user":
            query_text = extract_text(last_msg["content"])
            memory_context = self._runtime_memory.read_external_if_allowed(
                query_text,
                session_id=state.session_id,
                enabled_override=enabled_override,
            )
            if memory_context:
                last_msg_copy = last_msg.copy()
                last_msg_copy["content"] = prepend_text_part(
                    last_msg["content"],
                    memory_context + "\n\n",
                )
                api_messages[last_idx] = last_msg_copy
        return api_messages

    def build_provider_context(self, state: AgentState, options: dict[str, Any]) -> ProviderContext:
        """Build provider-visible message and tool context for one model call.

        T7: messages are generated per the ContextPlan (injection plan).
        tools consume ToolService-filtered results (already projected).
        """
        messages = self.build_provider_messages(state)
        tools: list[dict[str, Any]] = []
        if self.tool_service is not None:
            exposure_policy = (
                ToolExposurePolicy.SAFE_ONLY
                if options.get("tool_exposure_policy") == "safe_only"
                else ToolExposurePolicy.DEFAULT
            )
            tools = self.tool_service.list_openai_tools(
                exposure_policy,
                context=options.get("tool_execution_context") if isinstance(options, dict) else None,
            )
        return ProviderContext(messages=messages, tools=tools)


def _message_to_provider(message: ConversationMessage) -> dict[str, Any]:
    content = message.content
    tool_calls = None
    if message.role == "assistant" and isinstance(content, dict) and "tool_calls" in content:
        tool_calls = content.get("tool_calls") or []
        content = content.get("content", "")
    if message.role == "tool" and not isinstance(content, str):
        content = json.dumps(content, default=str, ensure_ascii=False)
    if message.role == "tool":
        data = {"role": message.role}
        if message.tool_call_id:
            data["tool_call_id"] = message.tool_call_id
        if message.name:
            data["name"] = message.name
        data["content"] = content
        return data

    data = {"role": message.role, "content": content}
    if tool_calls:
        data["tool_calls"] = tool_calls
    if message.tool_call_id:
        data["tool_call_id"] = message.tool_call_id
    if message.name:
        data["name"] = message.name
    return data


def _assistant_tool_calls(message: ConversationMessage) -> list[dict[str, Any]]:
    if message.role != "assistant" or not isinstance(message.content, dict):
        return []
    raw = message.content.get("tool_calls") or []
    return raw if isinstance(raw, list) else []


def _assistant_content_without_tool_calls(message: ConversationMessage) -> Any:
    if not isinstance(message.content, dict):
        return message.content
    return message.content.get("content", "")


def _sanitize_conversation_tool_pairs(messages: list[ConversationMessage]) -> list[ConversationMessage]:
    """Remove incomplete assistant/tool groups before sending history to a provider."""
    result: list[ConversationMessage] = []
    i = 0
    while i < len(messages):
        message = messages[i]
        tool_calls = _assistant_tool_calls(message)
        if tool_calls:
            ids = {tc.get("id") for tc in tool_calls if isinstance(tc, dict) and tc.get("id")}
            contiguous_tools: list[ConversationMessage] = []
            j = i + 1
            while j < len(messages) and messages[j].role == "tool":
                if messages[j].tool_call_id in ids:
                    contiguous_tools.append(messages[j])
                j += 1

            result_ids = {tool.tool_call_id for tool in contiguous_tools}
            kept_calls = [
                tc for tc in tool_calls
                if isinstance(tc, dict) and tc.get("id") in result_ids
            ]
            if kept_calls:
                content = _assistant_content_without_tool_calls(message)
                result.append(
                    dataclasses.replace(
                        message,
                        content={"content": content, "tool_calls": kept_calls},
                    )
                )
                result.extend(contiguous_tools)
            else:
                content = _assistant_content_without_tool_calls(message)
                if content:
                    result.append(dataclasses.replace(message, content=content))
            i = j
        elif message.role == "tool":
            i += 1
        else:
            result.append(message)
            i += 1
    return result


def _build_latest_compressed_context(messages: list[ConversationMessage]) -> list[ConversationMessage]:
    """Build provider context with latest summary between protected head and tail."""
    latest_summary_idx = -1
    for idx in range(len(messages) - 1, -1, -1):
        if messages[idx].is_summary:
            latest_summary_idx = idx
            break
    if latest_summary_idx == -1:
        return [m for m in messages if not m.is_summarized]

    latest_summary = messages[latest_summary_idx]
    first_summarized_idx = -1
    for idx, message in enumerate(messages[:latest_summary_idx]):
        if message.is_summarized and not message.is_summary:
            first_summarized_idx = idx
            break

    kept_non_summary = [
        m for m in messages
        if not m.is_summary and not m.is_summarized
    ]
    if first_summarized_idx == -1:
        return [
            m for idx, m in enumerate(messages)
            if not m.is_summary or idx == latest_summary_idx
        ]

    head = [
        m for m in messages[:first_summarized_idx]
        if not m.is_summary and not m.is_summarized
    ]
    head_ids = {m.id for m in head}
    tail = [m for m in kept_non_summary if m.id not in head_ids]
    return [*head, latest_summary, *tail]


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
    """Drop trailing history entries that already appear in inputs."""
    if not inputs:
        return history
    n = len(inputs)
    if len(history) < n:
        return history
    tail = history[-n:]
    if all(_msg_role_content_key(t) == _msg_role_content_key(i) for t, i in zip(tail, inputs)):
        return history[:-n]
    return history

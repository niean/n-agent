from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.runtime_memory_service import (
    MemoryAccessDeniedError,
    RuntimeMemoryService,
)
from app.domain.memory_policy import (
    MemoryAccessDecision,
    MemoryOperation,
    MemoryPolicy,
    MemoryPolicyRequest,
)
from app.domain.policy import ExecutionMode, PolicyAuditEvent, PolicyOutcome
from app.domain.session import ConversationMessage, ConversationSession, Summary, TaskState, ToolCall


# ---------------------------------------------------------------------------
# Spy MemoryStore -- counts every call
# ---------------------------------------------------------------------------


class SpyMemoryStore:
    """Minimal MemoryStore spy that records all call counts."""

    def __init__(self) -> None:
        self.create_session_calls = 0
        self.get_session_calls = 0
        self.list_messages_calls = 0
        self.append_message_calls = 0
        self.save_tool_call_calls = 0
        self.save_task_state_calls = 0
        self.get_task_state_calls = 0
        self.save_summary_calls = 0
        self.get_summary_calls = 0
        self.lock_session_external_memory_calls = 0
        self.append_summary_message_calls = 0
        self.mark_messages_summarized_calls = 0
        self.delete_summary_messages_calls = 0
        self.append_message_if_session_exists_calls = 0

        self._session: ConversationSession | None = None
        self._messages: list[ConversationMessage] = []
        self._summary: Summary | None = None
        # Controls append_message_if_session_exists: True -> append+return;
        # False -> return None (simulates a deleted-session race).
        self._session_exists = True

    # -- MemoryStore protocol methods (async) --
    async def create_session(self, session: ConversationSession) -> ConversationSession:
        self.create_session_calls += 1
        self._session = session
        return session

    async def get_session(self, session_id: str) -> ConversationSession | None:
        self.get_session_calls += 1
        return self._session

    async def list_messages(self, session_id: str) -> list[ConversationMessage]:
        self.list_messages_calls += 1
        return list(self._messages)

    async def append_message(self, session_id: str, message: ConversationMessage) -> ConversationMessage:
        self.append_message_calls += 1
        self._messages.append(message)
        return message

    async def append_message_if_session_exists(
        self, session_id: str, message: ConversationMessage,
    ) -> ConversationMessage | None:
        self.append_message_if_session_exists_calls += 1
        if not self._session_exists:
            return None
        self._messages.append(message)
        return message

    async def save_tool_call(self, tool_call: ToolCall) -> ToolCall:
        self.save_tool_call_calls += 1
        return tool_call

    async def save_task_state(self, task_state: TaskState) -> TaskState:
        self.save_task_state_calls += 1
        return task_state

    async def get_task_state(self, session_id: str) -> TaskState | None:
        self.get_task_state_calls += 1
        return None

    async def save_summary(self, summary: Summary) -> Summary:
        self.save_summary_calls += 1
        self._summary = summary
        return summary

    async def get_summary(self, session_id: str) -> Summary | None:
        self.get_summary_calls += 1
        return self._summary

    async def lock_session_external_memory(
        self, session_id: str, enabled: list[str], slots: dict[str, str] | None = None,
    ) -> list[str]:
        self.lock_session_external_memory_calls += 1
        return enabled

    async def append_summary_message(
        self, session_id: str, message: ConversationMessage,
    ) -> ConversationMessage:
        self.append_summary_message_calls += 1
        return message

    async def mark_messages_summarized(self, session_id: str, message_ids: list[str]) -> int:
        self.mark_messages_summarized_calls += 1
        return len(message_ids)

    async def delete_summary_messages(self, session_id: str) -> int:
        self.delete_summary_messages_calls += 1
        return 0

    # -- Methods not used in tests but required by protocol --
    async def list_sessions(self) -> list[ConversationSession]:
        return []

    async def update_session_title(self, session_id: str, title: str) -> None:
        pass

    async def delete_session(self, session_id: str) -> bool:
        return True

    async def list_tool_calls(self, session_id: str) -> list[ToolCall]:
        return []

    async def list_recent_tool_calls(self, tool_name: str | None = None, limit: int = 50) -> list[ToolCall]:
        return []

    async def delete_tool_call(self, tool_call_id: str) -> bool:
        return True

    async def update_session_acp_metadata(self, session_id: str, metadata: dict) -> None:
        pass

    async def list_sessions_by_source(self, source: str, cwd: str | None = None, cursor: str | None = None, limit: int = 50) -> tuple:
        return ([], None)

    async def clone_session(self, source_session_id: str, target_session_id: str) -> None:
        pass


class SpyExternalMemoryManager:
    """Minimal spy for ExternalMemoryManager external operations."""

    def __init__(self) -> None:
        self.sync_all_calls = 0
        self.handle_tool_call_calls = 0
        self.prefetch_all_calls = 0

    def sync_all(self, user_content, assistant_content, *, session_id, agent_context, enabled_override=None):
        self.sync_all_calls += 1

    def handle_tool_call(self, tool_name, args, *, agent_context, session_id, enabled_override=None):
        self.handle_tool_call_calls += 1
        return json.dumps({"success": True})

    def prefetch_all(self, query, *, session_id, enabled_override=None):
        self.prefetch_all_calls += 1
        return ""


# ---------------------------------------------------------------------------
# S4: RuntimeMemoryService boundary tests
# ---------------------------------------------------------------------------


class TestCreateSession:
    @pytest.mark.asyncio
    async def test_allow_calls_store(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy)
        session = ConversationSession(id="s1")
        await svc.create_session_if_allowed(session)
        assert spy.create_session_calls == 1


class TestGetSession:
    @pytest.mark.asyncio
    async def test_allow_calls_store(self):
        spy = SpyMemoryStore()
        spy._session = ConversationSession(id="s1")
        svc = RuntimeMemoryService(spy)
        result = await svc.get_session_if_allowed("s1")
        assert spy.get_session_calls == 1
        assert result is not None
        assert result.id == "s1"


class TestReadSessionMessages:
    @pytest.mark.asyncio
    async def test_current_session_allow(self):
        spy = SpyMemoryStore()
        spy._messages = [ConversationMessage(role="user", content="hi")]
        svc = RuntimeMemoryService(spy)
        msgs = await svc.read_session_messages("s1")
        assert spy.list_messages_calls == 1
        assert len(msgs) == 1

    @pytest.mark.asyncio
    async def test_cross_session_deny_zero_calls(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy)
        msgs = await svc.read_session_messages("s1", target_session_id="s2")
        assert spy.list_messages_calls == 0
        assert msgs == []


class TestAppendUserMessage:
    @pytest.mark.asyncio
    async def test_allow_calls_store(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy)
        await svc.append_user_message("s1", "hello")
        assert spy.append_message_calls == 1


class TestAppendAssistantMessage:
    @pytest.mark.asyncio
    async def test_allow_calls_store(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy)
        await svc.append_assistant_message("s1", "response")
        assert spy.append_message_calls == 1


class TestAppendToolMessage:
    @pytest.mark.asyncio
    async def test_allow_calls_store(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy)
        await svc.append_tool_message("s1", '{"result": 42}', tool_call_id="tc1", name="calc")
        assert spy.append_message_calls == 1


class TestSaveToolCall:
    @pytest.mark.asyncio
    async def test_allow_calls_store(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy)
        tc = ToolCall(id="tc1", session_id="s1", tool_name="calc", arguments={})
        await svc.save_tool_call_if_allowed(tc)
        assert spy.save_tool_call_calls == 1


class TestSaveTaskState:
    @pytest.mark.asyncio
    async def test_allow_calls_store(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy)
        ts = TaskState(session_id="s1", status="running")
        await svc.save_task_state_if_allowed(ts)
        assert spy.save_task_state_calls == 1


class TestSaveSummary:
    @pytest.mark.asyncio
    async def test_allow_calls_store(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy)
        s = Summary(session_id="s1", summary="test summary")
        await svc.save_summary_if_allowed(s)
        assert spy.save_summary_calls == 1


class TestGetSummary:
    @pytest.mark.asyncio
    async def test_allow_calls_store(self):
        spy = SpyMemoryStore()
        spy._summary = Summary(session_id="s1", summary="test")
        svc = RuntimeMemoryService(spy)
        result = await svc.get_summary_if_allowed("s1")
        assert spy.get_summary_calls == 1
        assert result is not None


class TestAppendSummaryMessage:
    @pytest.mark.asyncio
    async def test_allow_calls_store(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy)
        msg = ConversationMessage(role="user", content="summary", is_summary=True)
        await svc.append_summary_message_if_allowed("s1", msg)
        assert spy.append_summary_message_calls == 1


class TestMarkMessagesSummarized:
    @pytest.mark.asyncio
    async def test_allow_calls_store(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy)
        await svc.mark_messages_summarized_if_allowed("s1", ["m1", "m2"])
        assert spy.mark_messages_summarized_calls == 1


class TestLockProfile:
    @pytest.mark.asyncio
    async def test_allow_calls_store(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy)
        result = await svc.lock_profile("s1", ["builtin"])
        assert spy.lock_session_external_memory_calls == 1
        assert result == ["builtin"]


class TestSyncExternal:
    @pytest.mark.asyncio
    async def test_primary_realtime_allow(self):
        spy = SpyMemoryStore()
        ext = SpyExternalMemoryManager()
        svc = RuntimeMemoryService(spy, external_memory_manager=ext)
        await svc.sync_external_if_allowed(
            "user text", "assistant text",
            session_id="s1",
            agent_context="primary",
            execution_mode=ExecutionMode.REALTIME,
        )
        assert ext.sync_all_calls == 1

    @pytest.mark.asyncio
    async def test_non_primary_deny_zero_calls(self):
        spy = SpyMemoryStore()
        ext = SpyExternalMemoryManager()
        svc = RuntimeMemoryService(spy, external_memory_manager=ext)
        await svc.sync_external_if_allowed(
            "user text", "assistant text",
            session_id="s1",
            agent_context="unattended",
            execution_mode=ExecutionMode.REALTIME,
        )
        assert ext.sync_all_calls == 0

    @pytest.mark.asyncio
    async def test_unattended_deny_zero_calls(self):
        spy = SpyMemoryStore()
        ext = SpyExternalMemoryManager()
        svc = RuntimeMemoryService(spy, external_memory_manager=ext)
        await svc.sync_external_if_allowed(
            "user text", "assistant text",
            session_id="s1",
            agent_context="primary",
            execution_mode=ExecutionMode.UNATTENDED,
        )
        assert ext.sync_all_calls == 0


class TestHandleExternalToolCall:
    @pytest.mark.asyncio
    async def test_realtime_enabled_slot_allow(self):
        spy = SpyMemoryStore()
        ext = SpyExternalMemoryManager()
        svc = RuntimeMemoryService(
            spy, external_memory_manager=ext,
            enabled_slots=("builtin",),
        )
        result = svc.handle_external_tool_call_if_allowed(
            "external_memory", {"query": "test"},
            agent_context="primary",
            session_id="s1",
            execution_mode=ExecutionMode.REALTIME,
            provider_slot="builtin",
        )
        assert ext.handle_tool_call_calls == 1
        data = json.loads(result)
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_unattended_deny_zero_calls(self):
        spy = SpyMemoryStore()
        ext = SpyExternalMemoryManager()
        svc = RuntimeMemoryService(spy, external_memory_manager=ext)
        result = svc.handle_external_tool_call_if_allowed(
            "external_memory", {"query": "test"},
            agent_context="unattended",
            session_id="s1",
            execution_mode=ExecutionMode.UNATTENDED,
            provider_slot="builtin",
        )
        assert ext.handle_tool_call_calls == 0
        data = json.loads(result)
        assert data["success"] is False


class TestNoStoreAttribute:
    def test_store_not_exposed(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy)
        # RuntimeMemoryService must NOT expose a public `store` attribute
        assert not hasattr(svc, "store")
        assert not hasattr(svc, "memory_store")


class TestAuditRecording:
    @pytest.mark.asyncio
    async def test_allow_records_audit(self):
        spy = SpyMemoryStore()
        audit_sink = AsyncMock()
        svc = RuntimeMemoryService(spy, audit_sink=audit_sink)
        await svc.append_user_message("s1", "hello")
        assert audit_sink.record.called
        event: PolicyAuditEvent = audit_sink.record.call_args[0][0]
        assert event.outcome is PolicyOutcome.ALLOW
        assert event.policy == "memory-policy"

    @pytest.mark.asyncio
    async def test_deny_records_audit(self):
        spy = SpyMemoryStore()
        audit_sink = AsyncMock()
        svc = RuntimeMemoryService(spy, audit_sink=audit_sink)
        # Cross-session read -> DENY
        await svc.read_session_messages("s1", target_session_id="s2")
        assert audit_sink.record.called
        event: PolicyAuditEvent = audit_sink.record.call_args[0][0]
        assert event.outcome is PolicyOutcome.DENY

    @pytest.mark.asyncio
    async def test_no_audit_sink_no_error(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy)  # no audit_sink
        # Should not raise
        await svc.append_user_message("s1", "hello")
        assert spy.append_message_calls == 1


class TestBootstrapSequence:
    """Test the bootstrap descriptor -> snapshot -> create/lock/read/write order."""

    @pytest.mark.asyncio
    async def test_full_bootstrap_sequence(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy)

        # 1. Create session
        session = ConversationSession(id="s1")
        await svc.create_session_if_allowed(session)
        assert spy.create_session_calls == 1

        # 2. Lock profile
        await svc.lock_profile("s1", ["builtin"])
        assert spy.lock_session_external_memory_calls == 1

        # 3. Read existing messages
        msgs = await svc.read_session_messages("s1")
        assert spy.list_messages_calls == 1

        # 4. Append user message
        await svc.append_user_message("s1", "hello")
        assert spy.append_message_calls == 1

        # 5. Append assistant message
        await svc.append_assistant_message("s1", "response")
        assert spy.append_message_calls == 2

        # 6. Save task state
        await svc.save_task_state_if_allowed(TaskState(session_id="s1"))
        assert spy.save_task_state_calls == 1

        # 7. Save summary
        await svc.save_summary_if_allowed(Summary(session_id="s1", summary="sum"))
        assert spy.save_summary_calls == 1

    @pytest.mark.asyncio
    async def test_deny_on_cross_session_read_does_not_block_subsequent_ops(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy)

        # Cross-session read denied
        msgs = await svc.read_session_messages("s1", target_session_id="s2")
        assert msgs == []
        assert spy.list_messages_calls == 0

        # But current-session read still works
        msgs = await svc.read_session_messages("s1")
        assert spy.list_messages_calls == 1


# ---------------------------------------------------------------------------
# MemoryAccessDeniedError raise-path tests (Fix #2)
# ---------------------------------------------------------------------------


class DenyWriteMessagePolicy(MemoryPolicy):
    """Test stub that denies WRITE_MESSAGE operations."""

    def evaluate(self, request: MemoryPolicyRequest) -> MemoryAccessDecision:
        if request.operation is MemoryOperation.WRITE_MESSAGE:
            return MemoryAccessDecision(
                verdict=PolicyOutcome.DENY,
                reason="write denied by test stub",
            )
        return super().evaluate(request)


class DenyWriteSummaryPolicy(MemoryPolicy):
    """Test stub that denies WRITE_SUMMARY operations."""

    def evaluate(self, request: MemoryPolicyRequest) -> MemoryAccessDecision:
        if request.operation is MemoryOperation.WRITE_SUMMARY:
            return MemoryAccessDecision(
                verdict=PolicyOutcome.DENY,
                reason="summary denied by test stub",
            )
        return super().evaluate(request)


class TestMemoryAccessDeniedError:
    """Verify that denied writes raise MemoryAccessDeniedError with correct
    decision and operation attributes."""

    @pytest.mark.asyncio
    async def test_denied_user_message_raises(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy, memory_policy=DenyWriteMessagePolicy())
        with pytest.raises(MemoryAccessDeniedError) as exc_info:
            await svc.append_user_message("s1", "hello")
        assert spy.append_message_calls == 0
        err = exc_info.value
        assert err.operation is MemoryOperation.WRITE_MESSAGE
        assert err.decision.verdict is PolicyOutcome.DENY
        assert "write denied" in err.decision.reason

    @pytest.mark.asyncio
    async def test_denied_assistant_message_raises(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy, memory_policy=DenyWriteMessagePolicy())
        with pytest.raises(MemoryAccessDeniedError) as exc_info:
            await svc.append_assistant_message("s1", "response")
        assert spy.append_message_calls == 0
        err = exc_info.value
        assert err.operation is MemoryOperation.WRITE_MESSAGE
        assert err.decision.verdict is PolicyOutcome.DENY

    @pytest.mark.asyncio
    async def test_denied_tool_message_raises(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy, memory_policy=DenyWriteMessagePolicy())
        with pytest.raises(MemoryAccessDeniedError) as exc_info:
            await svc.append_tool_message("s1", '{"r": 1}', tool_call_id="tc1", name="calc")
        assert spy.append_message_calls == 0
        err = exc_info.value
        assert err.operation is MemoryOperation.WRITE_MESSAGE

    @pytest.mark.asyncio
    async def test_denied_summary_save_raises(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy, memory_policy=DenyWriteSummaryPolicy())
        with pytest.raises(MemoryAccessDeniedError) as exc_info:
            await svc.save_summary_if_allowed(Summary(session_id="s1", summary="sum"))
        assert spy.save_summary_calls == 0
        err = exc_info.value
        assert err.operation is MemoryOperation.WRITE_SUMMARY
        assert err.decision.verdict is PolicyOutcome.DENY
        assert "summary denied" in err.decision.reason

    @pytest.mark.asyncio
    async def test_denied_task_state_raises(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy, memory_policy=DenyWriteMessagePolicy())
        with pytest.raises(MemoryAccessDeniedError) as exc_info:
            await svc.save_task_state_if_allowed(TaskState(session_id="s1"))
        assert spy.save_task_state_calls == 0
        err = exc_info.value
        assert err.operation is MemoryOperation.WRITE_MESSAGE

    @pytest.mark.asyncio
    async def test_denied_tool_call_save_raises(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy, memory_policy=DenyWriteMessagePolicy())
        tc = ToolCall(id="tc1", session_id="s1", tool_name="calc", arguments={})
        with pytest.raises(MemoryAccessDeniedError) as exc_info:
            await svc.save_tool_call_if_allowed(tc)
        assert spy.save_tool_call_calls == 0
        err = exc_info.value
        assert err.operation is MemoryOperation.WRITE_MESSAGE

    @pytest.mark.asyncio
    async def test_denied_summary_message_append_raises(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy, memory_policy=DenyWriteSummaryPolicy())
        msg = ConversationMessage(role="user", content="summary", is_summary=True)
        with pytest.raises(MemoryAccessDeniedError) as exc_info:
            await svc.append_summary_message_if_allowed("s1", msg)
        assert spy.append_summary_message_calls == 0
        err = exc_info.value
        assert err.operation is MemoryOperation.WRITE_SUMMARY

    @pytest.mark.asyncio
    async def test_error_message_contains_operation_and_reason(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy, memory_policy=DenyWriteMessagePolicy())
        with pytest.raises(MemoryAccessDeniedError) as exc_info:
            await svc.append_user_message("s1", "hello")
        msg = str(exc_info.value)
        assert "write_message" in msg
        assert "write denied by test stub" in msg


class TestAppendUserMessageSource:
    async def test_threads_source_to_store(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy)
        msg = await svc.append_user_message("s1", "work task t1", source="task")
        assert msg.source == "task"
        assert spy._messages[0].source == "task"

    async def test_default_source_none(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy)
        msg = await svc.append_user_message("s1", "hi")
        assert msg.source is None

    async def test_source_is_keyword_only(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy)
        with pytest.raises(TypeError):
            await svc.append_user_message("s1", "hi", "task")

    async def test_assistant_message_has_no_source(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy)
        msg = await svc.append_assistant_message("s1", "ok")
        assert msg.source is None


class TestAppendSystemNamedMessage:
    """ui.artifact card path: role=system + name + card, session-gone safe."""

    @pytest.mark.asyncio
    async def test_allow_appends_system_named_with_card(self):
        spy = SpyMemoryStore()
        spy._session_exists = True
        svc = RuntimeMemoryService(spy)
        card = {
            "artifact_id": "a1", "revision_id": "r1", "name": "foo",
            "kind": "document", "revision_number": 2,
            "publish_sync_state": "unpublished",
        }
        msg = await svc.append_system_named_message(
            "s1", "ui.artifact", "制品已更新: foo", card=card,
        )
        assert msg is not None
        assert msg.role == "system"
        assert msg.name == "ui.artifact"
        assert msg.card == card
        # Uses append_message_if_session_exists, NOT the implicit-create path.
        assert spy.append_message_if_session_exists_calls == 1
        assert spy.append_message_calls == 0
        assert spy._messages[-1] is msg

    @pytest.mark.asyncio
    async def test_session_gone_returns_none_no_revive(self):
        spy = SpyMemoryStore()
        spy._session_exists = False
        svc = RuntimeMemoryService(spy)
        msg = await svc.append_system_named_message(
            "s1", "ui.artifact", "x", card={"artifact_id": "a1"},
        )
        assert msg is None
        assert spy.append_message_if_session_exists_calls == 1
        # A deleted-session race must NOT write an orphan or revive the session.
        assert spy._messages == []
        assert spy.append_message_calls == 0

    @pytest.mark.asyncio
    async def test_denied_raises_and_does_not_touch_store(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy, memory_policy=DenyWriteMessagePolicy())
        with pytest.raises(MemoryAccessDeniedError) as exc_info:
            await svc.append_system_named_message(
                "s1", "ui.artifact", "x", card={"artifact_id": "a1"},
            )
        assert spy.append_message_if_session_exists_calls == 0
        assert spy.append_message_calls == 0
        assert exc_info.value.operation is MemoryOperation.WRITE_MESSAGE

    @pytest.mark.asyncio
    async def test_default_card_none(self):
        spy = SpyMemoryStore()
        svc = RuntimeMemoryService(spy)
        msg = await svc.append_system_named_message("s1", "ui.artifact", "x")
        assert msg is not None
        assert msg.card is None

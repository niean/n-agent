import pytest
import asyncio
import sqlite3

from app.application.chat_service import ChatCompletionInput, ChatCompletionService
from app.application.session_service import SessionService
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.application.agent_run_service import AgentRunService
from app.application.agent_graph import AgentGraphRunner
from app.application.tool_service import ToolService, builtin_tool_definitions
from app.infrastructure.registry.sqlite_agent_run_registry import SQLiteAgentRunRegistry
from app.infrastructure.tools.builtin import build_builtin_tool_executor
from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
from app.domain.provider import LLMResult
from app.domain.agent_run import RunCancelledError


class UnavailableLedger:
    async def register(self, **kwargs):
        raise RuntimeError("ledger unavailable")


class NeverRunner:
    async def run(self, *args):
        pytest.fail("execution must not start without durable registration")


@pytest.mark.asyncio
async def test_failed_registration_does_not_save_user_message_or_execute(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    service = ChatCompletionService(
        store, NeverRunner(), SessionService(store),
        agent_run_service=UnavailableLedger(),
    )
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        await service.complete(ChatCompletionInput(
            model="test", session_id="chat", stream=False,
            messages=[{"role": "user", "content": "do work"}],
        ))
    assert await store.list_messages("chat") == []


class Provider:
    name = "test"

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def chat(self, *args, **kwargs):
        self.started.set()
        await self.release.wait()
        return LLMResult(message={"role": "assistant", "content": "done"}, finish_reason="stop")


def build_service(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    runs = AgentRunService(SQLiteAgentRunRegistry(store.path))
    provider = Provider()
    runner = AgentGraphRunner(
        provider, ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store, HeuristicSummarizer(), agent_run_service=runs,
    )
    chat = ChatCompletionService(store, runner, SessionService(store), agent_run_service=runs)
    return store, runs, provider, chat


@pytest.mark.asyncio
async def test_nonstream_execution_records_run_and_both_messages(tmp_path):
    store, runs, provider, chat = build_service(tmp_path)
    provider.release.set()
    try:
        result = await chat.complete(ChatCompletionInput(model="test", session_id="chat", stream=False, messages=[{"role": "user", "content": "hi"}]))
        assert runs.get(result.run_id)["status"] == "completed"
        messages = await store.list_messages("chat")
        assert [m.role for m in messages] == ["user", "assistant"]
        assert {m.run_id for m in messages} == {result.run_id}
        assert not chat.graph_runner.interrupt("chat")
    finally:
        await runs.close()


@pytest.mark.asyncio
async def test_session_stop_cancels_nonstream_foreground_execution(tmp_path):
    _, runs, provider, chat = build_service(tmp_path)
    task = asyncio.create_task(chat.complete(ChatCompletionInput(model="test", session_id="chat", stream=False, messages=[{"role": "user", "content": "hi"}])))
    try:
        await asyncio.wait_for(provider.started.wait(), 2)
        assert chat.graph_runner.interrupt("chat")
        with pytest.raises(RunCancelledError):
            await task
        assert runs.list_for_session("chat")[0]["status"] == "cancelled"
    finally:
        provider.release.set()
        await runs.close()


@pytest.mark.asyncio
async def test_stream_disconnect_keeps_execution_and_message_attribution(tmp_path):
    store, runs, provider, chat = build_service(tmp_path)
    stream = await chat.complete(ChatCompletionInput(model="test", session_id="chat", messages=[{"role": "user", "content": "hi"}]))
    await anext(stream)
    consumer = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(provider.started.wait(), 2)
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        assert runs.list_for_session("chat")[0]["status"] == "running"
        provider.release.set()
        await asyncio.wait_for(asyncio.gather(*chat.graph_runner._detached_run_waiters), 2)
        row = runs.list_for_session("chat")[0]
        assert row["status"] == "completed"
        assert {m.run_id for m in await store.list_messages("chat")} == {row["id"]}
    finally:
        provider.release.set()
        await stream.aclose()
        await runs.close()


@pytest.mark.asyncio
async def test_second_foreground_is_rejected_before_saving_its_message(tmp_path):
    from app.domain.agent_run import RunAdmissionConflict
    store, runs, provider, chat = build_service(tmp_path)
    request = ChatCompletionInput(model="test", session_id="chat", stream=False, messages=[{"role": "user", "content": "hi"}])
    first = asyncio.create_task(chat.complete(request))
    try:
        await asyncio.wait_for(provider.started.wait(), 2)
        with pytest.raises(RunAdmissionConflict):
            await chat.complete(request)
        assert len(await store.list_messages("chat")) == 1
        provider.release.set()
        await first
    finally:
        provider.release.set()
        await runs.close()


@pytest.mark.asyncio
async def test_terminal_write_failure_is_not_replaced_by_second_finish(tmp_path, monkeypatch):
    _, runs, provider, chat = build_service(tmp_path)
    provider.release.set()
    calls = []

    def unavailable(*args):
        calls.append(args)
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(runs.registry, "finish", unavailable)
    try:
        with pytest.raises(sqlite3.OperationalError):
            await chat.complete(ChatCompletionInput(model="test", session_id="chat", stream=False, messages=[{"role": "user", "content": "hi"}]))
        assert len(calls) == 1
        assert runs.list_for_session("chat")[0]["status"] == "running"
    finally:
        await runs.close()


@pytest.mark.asyncio
async def test_execution_error_is_preserved_when_failure_recording_also_fails(tmp_path, monkeypatch):
    _, runs, _, chat = build_service(tmp_path)

    async def execution_error(*args):
        raise RuntimeError("execution failed")

    def unavailable(*args):
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(chat.graph_runner, "_run", execution_error)
    monkeypatch.setattr(runs.registry, "finish", unavailable)
    try:
        with pytest.raises(RuntimeError, match="execution failed") as caught:
            await chat.complete(ChatCompletionInput(model="test", session_id="chat", stream=False, messages=[{"role": "user", "content": "hi"}]))
        assert any("OperationalError" in note for note in caught.value.__notes__)
    finally:
        await runs.close()


@pytest.mark.asyncio
async def test_stream_terminal_write_failure_emits_safe_error_and_done(tmp_path, monkeypatch):
    from app.application.events import ChatEventType
    _, runs, provider, chat = build_service(tmp_path)
    provider.release.set()

    def unavailable(*args):
        raise sqlite3.OperationalError("private database detail")

    monkeypatch.setattr(runs.registry, "finish", unavailable)
    try:
        stream = await chat.complete(ChatCompletionInput(model="test", session_id="chat", messages=[{"role": "user", "content": "hi"}]))
        events = [event async for event in stream]
        errors = [event for event in events if event.type == ChatEventType.ERROR]
        assert len(errors) == 1
        assert errors[0].error == "agent_run_execution_failed"
        assert events[-1].type == ChatEventType.DONE
        assert not any(event.type == ChatEventType.CONTENT_DELTA for event in events)
        assert runs.list_for_session("chat")[0]["status"] == "running"
    finally:
        await runs.close()


@pytest.mark.asyncio
async def test_preparation_failure_keeps_primary_error_when_finish_is_unavailable(tmp_path, monkeypatch):
    _, runs, _, chat = build_service(tmp_path)

    async def invalid_preparation(*args, **kwargs):
        raise ValueError("invalid preparation")

    def unavailable(*args):
        raise sqlite3.OperationalError("private database detail")

    monkeypatch.setattr(chat, "_complete", invalid_preparation)
    monkeypatch.setattr(runs.registry, "finish", unavailable)
    try:
        with pytest.raises(ValueError, match="invalid preparation") as caught:
            await chat.complete(ChatCompletionInput(model="test", session_id="chat", stream=False, messages=[{"role": "user", "content": "hi"}]))
        assert caught.value.__notes__ == ["agent_run_terminal_write_failed: OperationalError"]
        assert runs.list_for_session("chat")[0]["status"] == "pending"
    finally:
        await runs.close()


@pytest.mark.asyncio
async def test_process_shutdown_keeps_raw_cancellation_and_records_known_result(tmp_path):
    _, runs, provider, chat = build_service(tmp_path)
    task = asyncio.create_task(chat.complete(ChatCompletionInput(model="test", session_id="chat", stream=False, messages=[{"role": "user", "content": "hi"}])))
    await asyncio.wait_for(provider.started.wait(), 2)
    await runs.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runs.list_for_session("chat")[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_explicit_stream_cancel_preserves_error_and_done_protocol(tmp_path):
    from app.application.events import ChatEventType
    _, runs, provider, chat = build_service(tmp_path)
    stream = await chat.complete(ChatCompletionInput(model="test", session_id="chat", messages=[{"role": "user", "content": "hi"}]))

    async def consume():
        return [event async for event in stream]

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(provider.started.wait(), 2)
        run_id = runs.list_for_session("chat")[0]["id"]
        assert runs.cancel(run_id)
        events = await consumer
        assert events[-1].type == ChatEventType.DONE
        assert any(event.type == ChatEventType.ERROR and event.error == "cancelled" for event in events)
        assert runs.get(run_id)["status"] == "cancelled"
    finally:
        await runs.close()


@pytest.mark.asyncio
async def test_pending_stream_cancel_stays_cancelled_without_calling_provider(tmp_path):
    from app.application.events import ChatEventType
    _, runs, provider, chat = build_service(tmp_path)
    stream = await chat.complete(ChatCompletionInput(model="test", session_id="chat", messages=[{"role": "user", "content": "hi"}]))
    try:
        run_id = runs.list_for_session("chat")[0]["id"]
        assert runs.cancel(run_id)
        events = [event async for event in stream]
        assert not provider.started.is_set()
        assert events[-1].type == ChatEventType.DONE
        assert any(event.type == ChatEventType.ERROR and event.error == "cancelled" for event in events)
        assert runs.get(run_id)["status"] == "cancelled"
        assert runs.get(run_id)["end_reason"] == "cancelled_before_start"
    finally:
        await runs.close()

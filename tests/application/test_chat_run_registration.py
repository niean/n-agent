import pytest
import asyncio
import sqlite3

from app.application.chat_service import ChatCompletionInput, ChatCompletionService, _run_footer_stream
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
from app.application.events import ChatEvent, ChatEventType


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
    # 内部不变量钉住测试：公开行为路径已由 tests/interfaces/test_openai_compatible_api.py 与 tests/interfaces/test_dashboard.py 覆盖。
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


@pytest.mark.asyncio
async def test_run_footer_stream_message_done_carries_registered_run_id(tmp_path):
    store, runs, provider, chat = build_service(tmp_path)
    provider.release.set()
    try:
        stream = await chat.complete(ChatCompletionInput(model="test", session_id="chat", stream=True, messages=[{"role": "user", "content": "hi"}]))
        events = [event async for event in stream]
        done = [event for event in events if event.type == ChatEventType.MESSAGE_DONE]
        assert len(done) == 1
        row = runs.list_for_session("chat")[0]
        assert row["status"] == "completed"
        assert done[0].metadata["run_id"] == row["id"]
        assert {m.run_id for m in await store.list_messages("chat")} == {row["id"]}
        for event in events:
            if event.type not in (ChatEventType.MESSAGE_DONE, ChatEventType.ERROR):
                assert "run_id" not in event.metadata
        assert any(event.type == ChatEventType.CONTENT_DELTA for event in events)
        assert events[-1].type == ChatEventType.DONE
    finally:
        await runs.close()


@pytest.mark.asyncio
async def test_run_footer_provider_failure_error_carries_run_id(tmp_path, monkeypatch):
    _, runs, provider, chat = build_service(tmp_path)

    async def failing_chat(*args, **kwargs):
        provider.started.set()
        raise RuntimeError("provider boom")

    monkeypatch.setattr(provider, "chat", failing_chat)
    try:
        stream = await chat.complete(ChatCompletionInput(model="test", session_id="chat", stream=True, messages=[{"role": "user", "content": "hi"}]))
        events = [event async for event in stream]
        errors = [event for event in events if event.type == ChatEventType.ERROR]
        assert len(errors) == 1
        row = runs.list_for_session("chat")[0]
        assert row["status"] == "failed"
        assert errors[0].metadata["run_id"] == row["id"]
        done = [event for event in events if event.type == ChatEventType.MESSAGE_DONE]
        assert len(done) == 1
        assert done[0].metadata["run_id"] == row["id"]
        assert events[-1].type == ChatEventType.DONE
        assert "run_id" not in events[-1].metadata
    finally:
        await runs.close()


@pytest.mark.asyncio
async def test_run_footer_cancelled_error_carries_run_id(tmp_path):
    _, runs, provider, chat = build_service(tmp_path)
    stream = await chat.complete(ChatCompletionInput(model="test", session_id="chat", stream=True, messages=[{"role": "user", "content": "hi"}]))

    async def consume():
        return [event async for event in stream]

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(provider.started.wait(), 2)
        run_id = runs.list_for_session("chat")[0]["id"]
        assert runs.cancel(run_id)
        events = await consumer
        errors = [event for event in events if event.type == ChatEventType.ERROR]
        assert len(errors) == 1
        assert errors[0].error == "cancelled"
        assert errors[0].metadata["run_id"] == run_id
        # 取消路径无成功收尾：不产生 MESSAGE_DONE，DONE 不带身份
        assert not any(event.type == ChatEventType.MESSAGE_DONE for event in events)
        assert events[-1].type == ChatEventType.DONE
        assert "run_id" not in events[-1].metadata
        assert runs.get(run_id)["status"] == "cancelled"
    finally:
        provider.release.set()
        await runs.close()


@pytest.mark.asyncio
async def test_run_footer_compress_slash_adds_no_identity(tmp_path):
    _, runs, provider, chat = build_service(tmp_path)
    provider.release.set()
    try:
        stream = await chat.complete(ChatCompletionInput(model="test", session_id="chat", stream=True, messages=[{"role": "user", "content": "/compress"}]))
        events = [event async for event in stream]
        done = [event for event in events if event.type == ChatEventType.MESSAGE_DONE]
        assert len(done) == 1
        assert "run_id" not in done[0].metadata
        assert runs.list_for_session("chat") == []
    finally:
        await runs.close()


@pytest.mark.asyncio
async def test_run_footer_without_agent_run_service_adds_no_identity(tmp_path):
    store, runs, provider, chat = build_service(tmp_path)
    chat.agent_run_service = None
    chat.graph_runner.agent_run_service = None
    provider.release.set()
    try:
        stream = await chat.complete(ChatCompletionInput(model="test", session_id="chat", stream=True, messages=[{"role": "user", "content": "hi"}]))
        events = [event async for event in stream]
        done = [event for event in events if event.type == ChatEventType.MESSAGE_DONE]
        assert len(done) == 1
        assert "run_id" not in done[0].metadata
        assert runs.list_for_session("chat") == []
        assert any(m.role == "assistant" for m in await store.list_messages("chat"))
    finally:
        await runs.close()


@pytest.mark.asyncio
async def test_run_footer_interleaved_sessions_keep_own_identity(tmp_path):
    store, runs, provider, chat = build_service(tmp_path)
    try:
        stream_a = await chat.complete(ChatCompletionInput(model="test", session_id="chat-a", stream=True, messages=[{"role": "user", "content": "hi a"}]))
        stream_b = await chat.complete(ChatCompletionInput(model="test", session_id="chat-b", stream=True, messages=[{"role": "user", "content": "hi b"}]))
        # 延迟消费：两条流先登记并交错启动，再统一放开 provider
        assert (await anext(stream_a)).type == ChatEventType.MESSAGE_START
        assert (await anext(stream_b)).type == ChatEventType.MESSAGE_START

        async def collect(stream):
            return [event async for event in stream]

        tail_a = asyncio.create_task(collect(stream_a))
        tail_b = asyncio.create_task(collect(stream_b))
        await asyncio.wait_for(provider.started.wait(), 2)
        provider.release.set()
        events_a, events_b = await asyncio.gather(tail_a, tail_b)
        row_a = runs.list_for_session("chat-a")[0]
        row_b = runs.list_for_session("chat-b")[0]
        assert row_a["id"] != row_b["id"]
        done_a = next(event for event in events_a if event.type == ChatEventType.MESSAGE_DONE)
        done_b = next(event for event in events_b if event.type == ChatEventType.MESSAGE_DONE)
        assert done_a.metadata["run_id"] == row_a["id"]
        assert done_b.metadata["run_id"] == row_b["id"]
        assert {m.run_id for m in await store.list_messages("chat-a")} == {row_a["id"]}
        assert {m.run_id for m in await store.list_messages("chat-b")} == {row_b["id"]}
    finally:
        provider.release.set()
        await runs.close()


@pytest.mark.asyncio
async def test_run_footer_stream_disconnect_keeps_detach_and_attribution(tmp_path):
    # 内部不变量钉住测试：公开行为路径已由 tests/interfaces/test_openai_compatible_api.py 与 tests/interfaces/test_dashboard.py 覆盖。
    store, runs, provider, chat = build_service(tmp_path)
    stream = await chat.complete(ChatCompletionInput(model="test", session_id="chat", stream=True, messages=[{"role": "user", "content": "hi"}]))
    await anext(stream)
    consumer = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(provider.started.wait(), 2)
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        # 取消消费者时底层迭代器已同步关闭（detach waiter 立即出现）
        assert chat.graph_runner._detached_run_waiters
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
async def test_run_footer_wrapper_closes_underlying_iterator_on_close():
    # 内部不变量钉住测试：公开行为路径已由 tests/interfaces/test_openai_compatible_api.py 与 tests/interfaces/test_dashboard.py 覆盖。
    closed = asyncio.Event()

    async def underlying():
        try:
            yield ChatEvent(ChatEventType.MESSAGE_START)
            yield ChatEvent(ChatEventType.MESSAGE_DONE, finish_reason="stop")
        finally:
            closed.set()

    stream = _run_footer_stream(underlying(), "run-1")
    first = await anext(stream)
    assert first.type == ChatEventType.MESSAGE_START
    await stream.aclose()
    assert closed.is_set()


@pytest.mark.asyncio
async def test_run_footer_wrapper_overrides_conflicting_terminal_identity():
    # 内部不变量钉住测试：公开行为路径已由 tests/interfaces/test_openai_compatible_api.py 与 tests/interfaces/test_dashboard.py 覆盖。
    async def underlying():
        yield ChatEvent(ChatEventType.MESSAGE_DONE, finish_reason="stop", metadata={"run_id": "foreign-run"})
        yield ChatEvent(ChatEventType.MESSAGE_DONE, finish_reason="stop", metadata={"run_id": "run-1", "extra": "keep"})

    stream = _run_footer_stream(underlying(), "run-1")
    first, second = [event async for event in stream]
    # 冲突身份以 binding 身份覆盖，不得保留外来 run_id
    assert first.metadata == {"run_id": "run-1"}
    # 已一致时不重复替换，metadata 其余字段保持
    assert second.metadata == {"run_id": "run-1", "extra": "keep"}

"""Real SDK contracts for business entry spans and asynchronous ownership."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.application import telemetry_semantics as ts
from app.application.chat_service import ChatCompletionResult
from app.application.events import ChatEvent, ChatEventType
from app.application.skill_evolution_service import SkillEvolutionService
from app.domain.gateway import GatewaySessionKey, InteractionMessage
from tests.application.test_gateway_service import Harness
from tests.application.test_skill_curator_service import _make_service, _report, _settings
from tests.application.test_delegation_run_service import (
    FakeClock, _config, _make_request, _make_parent,
)
from app.application.delegation_run_service import DelegationRunService
from app.domain.delegation import DelegationMemberStatus, DelegationResult
from app.infrastructure.registry.sqlite_delegation_registry import SQLiteDelegationRegistry
from tests.application.test_task_run_service import (
    FakeRegistry as TaskRegistry, FakeDispatcher, FakeNotifier, _queued_task,
)
from app.application.task_run_service import TaskRunService
from app.application.task_agent_executor import TaskAgentResult
from app.domain.task import TaskRunOutcome
from app.domain.task_policy import TaskPolicy


@pytest.fixture
def sdk(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("business-entry-test")
    monkeypatch.setattr(ts, "get_tracer", lambda: tracer)
    yield tracer, exporter
    provider.shutdown()


def named(exporter, name):
    return next(s for s in exporter.get_finished_spans() if s.name == name)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["cli", "acp", "feishu"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_gateway_entry_contains_agent_and_stream_consumption(sdk, source, streaming):
    tracer, exporter = sdk
    harness = Harness()

    async def events():
        with ts.invoke_agent_span(source=source):
            yield ChatEvent(ChatEventType.MESSAGE_DONE, content="done")
            await asyncio.sleep(0)

    async def complete(request):
        if request.stream:
            return events()
        with ts.invoke_agent_span(source=source):
            return ChatCompletionResult(session_id=request.session_id, model="test", message={"content": "done"})

    harness.chat_service.complete = complete
    service = harness.service()
    event = InteractionMessage(id="event-1", session_key=GatewaySessionKey(source, "local"), text="hello")
    with tracer.start_as_current_span("upstream") as upstream:
        if streaming:
            async for _ in service.handle_message_stream(event):
                assert trace.get_current_span().get_span_context().trace_id == upstream.get_span_context().trace_id
        else:
            await service.handle_message(event)
    entry = named(exporter, "gateway.message")
    agent = named(exporter, "invoke_agent n-agent")
    assert entry.parent.span_id == upstream.get_span_context().span_id
    assert agent.parent.span_id == entry.context.span_id
    assert entry.attributes["nagent.source"] == source
    assert entry.end_time >= agent.end_time


@pytest.mark.asyncio
async def test_background_review_has_linked_root_and_restores_caller(sdk):
    tracer, exporter = sdk
    async def complete(request):
        with ts.invoke_agent_span(session_id=request.session_id):
            return ChatCompletionResult(session_id="session", model="test", message={"content": "done"})
    service = SkillEvolutionService(SimpleNamespace(complete=complete), MagicMock())
    with tracer.start_as_current_span("trigger") as trigger:
        await service.run_background_review("session", "digest")
        assert trace.get_current_span() is trigger
    root = named(exporter, "skill.review")
    assert root.parent is None
    assert root.context.trace_id != trigger.get_span_context().trace_id
    assert root.links[0].context == trigger.get_span_context()
    assert named(exporter, "invoke_agent n-agent").parent.span_id == root.context.span_id


@pytest.mark.asyncio
async def test_curator_keeps_review_inside_its_linked_root(sdk):
    tracer, exporter = sdk
    async def complete(request):
        with ts.invoke_agent_span(session_id=request.session_id):
            return ChatCompletionResult(session_id=request.session_id, model="test", message={"content": "done"})
    evolution = SkillEvolutionService(SimpleNamespace(complete=complete), MagicMock())
    service = _make_service(settings=_settings(skills_curator_consolidate=True), rows=[_report("old")], evolution_service=evolution)
    service.render_candidate_list = AsyncMock(return_value="candidate")
    service._finalize_run = AsyncMock()
    with tracer.start_as_current_span("trigger") as trigger:
        await service.run_curator_review(dry_run=True, consolidate=True)
    root = named(exporter, "curator.run")
    review = named(exporter, "skill.review")
    assert root.parent is None
    assert root.links[0].context == trigger.get_span_context()
    assert review.parent.span_id == root.context.span_id
    assert review.context.trace_id == root.context.trace_id


@pytest.mark.asyncio
async def test_delegation_carrier_survives_recovery_and_ignores_tick_parent(sdk, tmp_path):
    tracer, exporter = sdk
    registry = SQLiteDelegationRegistry(str(tmp_path / "delegation.db"), clock=FakeClock())
    delegations = []
    parents = []
    for index in range(2):
        with tracer.start_as_current_span(f"parent-{index}") as parent:
            parents.append(parent.get_span_context())
            request = replace(_make_request(parent=_make_parent(scope_id=f"scope-{index}"), delegation_key=f"key-{index}"),
                              trace_context=ts.capture_trace_context())
            delegations.append(await registry.create_or_reconnect(request))

    class Executor:
        async def execute(self, **kwargs):
            with ts.invoke_agent_span(session_id=kwargs["member"].execution_session_id):
                await asyncio.sleep(0)
            return DelegationResult(status=DelegationMemberStatus.SUCCEEDED)

    # A fresh registry/run service models process recovery: no in-memory parent cache.
    recovered = SQLiteDelegationRegistry(str(tmp_path / "delegation.db"), clock=FakeClock())
    service = DelegationRunService(recovered, Executor(), FakeClock(), _config())
    with tracer.start_as_current_span("unrelated-tick"):
        await service.tick()
    roots = [s for s in exporter.get_finished_spans() if s.name == "delegation.member"]
    assert len(roots) == 4
    for root in roots:
        assert root.attributes["nagent.source"] == "delegation"
        index = next(i for i, d in enumerate(delegations) if root.attributes["nagent.delegation.id"] == d.id)
        assert root.parent.span_id == parents[index].span_id
        assert root.context.trace_id == parents[index].trace_id
    agents = [s for s in exporter.get_finished_spans() if s.name == "invoke_agent n-agent"]
    assert {s.parent.span_id for s in agents} == {s.context.span_id for s in roots}


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "timeout", "crash"])
async def test_task_entry_covers_multiple_agents_and_finalization(sdk, outcome):
    _, exporter = sdk
    registry = TaskRegistry()
    task = _queued_task(id="trace-task", goal_mode=True)
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "claim", 900)

    class Executor:
        async def run_goal_loop(self, *args):
            for _ in range(3):  # worker / judge / next worker share one business run.
                with ts.invoke_agent_span(source="task"):
                    await asyncio.sleep(0)
            if outcome == "timeout":
                raise asyncio.TimeoutError()
            if outcome == "crash":
                raise RuntimeError("executor failed")
            return TaskAgentResult(status=TaskRunOutcome.COMPLETED, output="done")

    service = TaskRunService(registry=registry, dispatcher=FakeDispatcher(), executor=Executor(),
                             policy=TaskPolicy(), notifier=FakeNotifier())
    final_contexts = []
    original = service._finalize_run
    async def finalize(*args):
        final_contexts.append(trace.get_current_span().get_span_context())
        return await original(*args)
    service._finalize_run = finalize
    await service.run_claim(claim.task, claim.run.id, "claim")
    root = named(exporter, "task.run")
    assert root.parent is None
    assert final_contexts == [root.context]
    agents = [s for s in exporter.get_finished_spans() if s.name == "invoke_agent n-agent"]
    assert len(agents) == 3
    assert all(s.parent.span_id == root.context.span_id for s in agents)
    assert root.attributes["nagent.outcome"] == {"completed": "completed", "timeout": "timed_out", "crash": "crashed"}[outcome]


@pytest.mark.asyncio
async def test_gateway_closing_stream_ends_entry_and_inner_generator(sdk):
    _, exporter = sdk
    closed = []
    async def events():
        try:
            yield ChatEvent(ChatEventType.MESSAGE_DONE, content="partial")
            await asyncio.sleep(10)
        finally:
            closed.append(True)
    harness = Harness()
    harness.chat_service.complete = AsyncMock(return_value=events())
    stream = harness.service().handle_message_stream(
        InteractionMessage(id="close", session_key=GatewaySessionKey("cli", "local"), text="hi"))
    await anext(stream)
    await stream.aclose()
    entry = named(exporter, "gateway.message")
    assert entry.attributes["nagent.outcome"] == "cancelled"
    assert closed == [True]
    assert not trace.get_current_span().get_span_context().is_valid


@pytest.mark.asyncio
async def test_feishu_event_span_contains_reply_after_gateway(sdk):
    _, exporter = sdk
    from tests.interfaces.test_feishu_im_adapter import FakeFeishuClient, text_payload
    from app.interfaces.feishu_im_adapter import FeishuImAdapter
    harness = Harness()
    contexts = []
    client = FakeFeishuClient()
    async def reply(*args):
        contexts.append(trace.get_current_span().get_span_context())
    client.send_markdown_reply = reply
    await FeishuImAdapter(harness.service(), client).handle_event(text_payload())
    entry = named(exporter, "feishu.event")
    assert named(exporter, "gateway.message").parent.span_id == entry.context.span_id
    assert contexts == [entry.context]


@pytest.mark.parametrize("carrier", [{}, {"traceparent": "invalid"}, {"traceparent": "00-" + "0" * 32 + "-" + "0" * 16 + "-01"}])
def test_missing_or_invalid_carrier_never_inherits_tick(sdk, carrier):
    tracer, exporter = sdk
    with tracer.start_as_current_span("unrelated") as upstream:
        with ts.business_operation("delegation.member", source="delegation", parent_carrier=carrier):
            pass
        assert trace.get_current_span() is upstream
    assert named(exporter, "delegation.member").parent is None


def test_unsampled_remote_parent_keeps_context_without_new_sample(sdk):
    _, exporter = sdk
    carrier = {"traceparent": "00-1234567890abcdef1234567890abcdef-1234567890abcdef-00"}
    with ts.business_operation("delegation.member", source="delegation", parent_carrier=carrier):
        context = trace.get_current_span().get_span_context()
        assert context.trace_id == int("1234567890abcdef1234567890abcdef", 16)
        assert not context.trace_flags.sampled
    assert not exporter.get_finished_spans()


@pytest.mark.asyncio
async def test_background_cancellation_restores_business_scene_and_ends_root(sdk):
    tracer, exporter = sdk
    ready = asyncio.Event()
    async def complete(request):
        ready.set()
        await asyncio.Event().wait()
    service = SkillEvolutionService(SimpleNamespace(complete=complete), MagicMock())
    with tracer.start_as_current_span("trigger") as trigger:
        token = ts._execution_ids.set({"session_id": "parent-session", "source": "dashboard"})
        try:
            task = asyncio.create_task(service.run_background_review("child-session", "digest"))
            await ready.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert ts.execution_log_snapshot()["session_id"] == "parent-session"
            assert trace.get_current_span() is trigger
        finally:
            ts._execution_ids.reset(token)
    root = named(exporter, "skill.review")
    assert root.parent is None
    assert root.attributes["nagent.outcome"] == "cancelled"


def test_business_completion_logs_current_root_and_no_parent_scene(sdk, monkeypatch):
    tracer, exporter = sdk
    records = []
    monkeypatch.setattr(ts.logger, "info", lambda *args: records.append((trace.get_current_span().get_span_context(), ts.execution_log_snapshot())))
    with tracer.start_as_current_span("trigger"):
        token = ts._scene_var.set({"model": "parent-model", "tool": "parent-tool", "details": "parent-content"})
        try:
            with ts.business_operation("skill.review", source="curator", session_id="child", detached=True):
                assert ts.execution_log_snapshot() == {"operation": "skill.review", "source": "curator", "session_id": "child"}
        finally:
            ts._scene_var.reset(token)
    root = named(exporter, "skill.review")
    assert records[0][0] == root.context
    assert records[0][1]["operation"] == "skill.review"
    assert "parent" not in str(records[0][1])


def test_broken_tracer_does_not_break_business_or_restore_wrong_context(sdk, monkeypatch):
    tracer, _ = sdk
    def broken():
        raise RuntimeError("telemetry unavailable")
    with tracer.start_as_current_span("caller") as caller:
        monkeypatch.setattr(ts, "get_tracer", broken)
        with ts.business_operation("task.run", source="task", detached=True) as facts:
            facts["outcome"] = "completed"
        assert trace.get_current_span() is caller


@pytest.mark.asyncio
async def test_delegation_creation_persists_original_server_context_on_reconnect(sdk, tmp_path):
    tracer, _ = sdk
    from app.application.delegation_service import DelegationService
    from app.application.information_flow_service import InformationFlowService
    from app.application.policy_snapshot import InformationFlowPolicyConfig
    from app.domain.delegation_policy import DelegationPolicy
    from app.domain.information_flow import SecretCatalog
    from tests.application.test_delegation_service import _parent_capability, _child_spec
    clock = FakeClock()
    registry = SQLiteDelegationRegistry(str(tmp_path / "creation.db"), clock=clock)
    run_service = MagicMock()
    run_service.tick = AsyncMock()
    service = DelegationService(registry=registry, run_service=run_service, policy=DelegationPolicy(),
                                info_flow=InformationFlowService(InformationFlowPolicyConfig(), SecretCatalog()), clock=clock, config=_config())
    service._drive_to_terminal = AsyncMock()
    # Stop after creation; the missing result set is an expected application error.
    from app.application.delegation_service import DelegationError
    captured = []
    for index in range(2):
        with tracer.start_as_current_span(f"attempt-{index}"):
            captured.append(ts.capture_trace_context())
            with pytest.raises(DelegationError, match="produced no result set"):
                await service.delegate(parent_capability=_parent_capability(), delegation_key="same",
                                       children=[_child_spec()], join_policy="all_completed", aggregation="parent", timeout_seconds=60)
    delegations = await registry.list_for_trusted_scope("t1")
    assert len(delegations) == 1
    events = await registry.list_events(delegations[0].id)
    assert events[0].payload["trace_context"] == captured[0]
    assert events[0].payload["trace_context"] != captured[1]


@pytest.mark.asyncio
async def test_curator_consolidation_failure_marks_business_root_failed(sdk):
    _, exporter = sdk
    from app.application.skill_evolution_service import BackgroundReviewResult
    evolution = SimpleNamespace(run_background_review=AsyncMock(return_value=BackgroundReviewResult(error="failed")))
    service = _make_service(settings=_settings(skills_curator_consolidate=True), rows=[_report("old")], evolution_service=evolution)
    service.render_candidate_list = AsyncMock(return_value="candidate")
    service._finalize_run = AsyncMock()
    await service.run_curator_review(dry_run=True, consolidate=True)
    assert named(exporter, "curator.run").attributes["nagent.outcome"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_gateway_returned_error_marks_entry_failed(sdk, streaming):
    _, exporter = sdk
    async def events():
        yield ChatEvent(ChatEventType.ERROR, error="provider failed")
    harness = Harness()
    harness.chat_service.complete = AsyncMock(return_value=(events() if streaming else
        ChatCompletionResult(session_id="session", model="test", message={"content": "failed"}, finish_reason="error")))
    service = harness.service()
    event = InteractionMessage(id="fail", session_key=GatewaySessionKey("cli", "local"), text="hi")
    if streaming:
        async for _ in service.handle_message_stream(event):
            pass
    else:
        await service.handle_message(event)
    assert named(exporter, "gateway.message").attributes["nagent.outcome"] == "failed"


@pytest.mark.asyncio
async def test_curator_marks_provider_error_returned_by_real_review(sdk):
    _, exporter = sdk
    chat = SimpleNamespace(complete=AsyncMock(return_value=ChatCompletionResult(
        session_id="session", model="test", message={"content": "provider failed"}, finish_reason="error")))
    evolution = SkillEvolutionService(chat, MagicMock())
    service = _make_service(settings=_settings(skills_curator_consolidate=True), rows=[_report("old")], evolution_service=evolution)
    service.render_candidate_list = AsyncMock(return_value="candidate")
    service._finalize_run = AsyncMock()
    await service.run_curator_review(dry_run=True, consolidate=True)
    assert named(exporter, "skill.review").attributes["nagent.outcome"] == "failed"
    assert named(exporter, "curator.run").attributes["nagent.outcome"] == "failed"


@pytest.mark.asyncio
async def test_gateway_cancel_event_is_not_an_execution_failure(sdk):
    _, exporter = sdk
    async def events():
        yield ChatEvent(ChatEventType.ERROR, error="cancelled", finish_reason="cancelled")
    harness = Harness()
    harness.chat_service.complete = AsyncMock(return_value=events())
    service = harness.service()
    async for _ in service.handle_message_stream(
            InteractionMessage(id="cancel", session_key=GatewaySessionKey("cli", "local"), text="hi")):
        pass
    assert named(exporter, "gateway.message").attributes["nagent.outcome"] == "cancelled"

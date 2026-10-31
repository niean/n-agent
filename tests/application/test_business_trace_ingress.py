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
    from app.application import telemetry_details as td
    from app.infrastructure.observability.sanitizer import LogSanitizer
    monkeypatch.setattr(td, "_config", (LogSanitizer().sanitize, 2048))
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
    assert "hello" in entry.attributes["nagent.details.input_messages.content"]
    assert "done" in entry.attributes["nagent.details.output_messages.content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ingress_source,expected", [(None, "skill_review"), ("curator", "curator")])
async def test_background_review_has_linked_root_and_restores_caller(sdk, ingress_source, expected):
    tracer, exporter = sdk
    async def complete(request):
        assert request.trusted_metadata["gateway.source"] == expected
        with ts.invoke_agent_span(session_id=request.session_id, source=request.trusted_metadata["gateway.source"]):
            return ChatCompletionResult(session_id="session", model="test", message={"content": "done"})
    service = SkillEvolutionService(SimpleNamespace(complete=complete), MagicMock())
    with tracer.start_as_current_span("trigger") as trigger:
        await service.run_background_review("session", "digest", ingress_source=ingress_source)
        assert trace.get_current_span() is trigger
    root = named(exporter, "skill.review")
    assert root.parent is None
    assert root.attributes["nagent.source"] == expected
    assert named(exporter, "invoke_agent n-agent").attributes["nagent.source"] == expected
    assert root.context.trace_id != trigger.get_span_context().trace_id
    assert root.links[0].context == trigger.get_span_context()
    assert named(exporter, "invoke_agent n-agent").parent.span_id == root.context.span_id
    assert "digest" in root.attributes["nagent.details.input_messages.content"]
    assert "done" in root.attributes["nagent.details.output_messages.content"]


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
    assert "dry_run" in root.attributes["nagent.details.input_messages.content"]
    import json
    request_payload = json.loads(root.attributes["nagent.details.input_messages.content"])
    response_payload = json.loads(root.attributes["nagent.details.output_messages.content"])
    assert request_payload["candidates"][0]["name"] == "old"
    assert request_payload["consolidate"] is True
    assert response_payload["auto_transitions"]["checked"] == 1
    assert response_payload["llm"]["final"] == "done"
    assert response_payload["errors"] == []


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
        assert "instruction" in root.attributes["nagent.details.input_messages.content"]
        assert "succeeded" in root.attributes["nagent.details.output_messages.content"]
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
    assert task.title in root.attributes["nagent.details.input_messages.content"]
    if outcome == "completed":
        assert "done" in root.attributes["nagent.details.output_messages.content"]
    else:
        assert "execution" in root.attributes["nagent.details.error.content"]


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
    assert "partial" in entry.attributes["nagent.details.output_messages.content"]
    assert entry.attributes["nagent.details.output_messages.incomplete"] is True
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
    assert "nagent.details.input_messages.content" in entry.attributes
    assert "nagent.details.output_messages.content" in entry.attributes


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
    assert "failed" in named(exporter, "curator.run").attributes["nagent.details.error.content"]
    assert '"error": "failed"' in named(exporter, "curator.run").attributes["nagent.details.output_messages.content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["empty", "in-flight", "backup-failed", "orchestrator-failed"])
async def test_curator_return_paths_keep_actual_result_and_error_payload(sdk, path):
    import json
    service = _make_service(rows=[] if path == "empty" else [_report("old")],
                            backup_fail=path == "backup-failed")
    service._finalize_run = AsyncMock()
    service._in_flight = path == "in-flight"
    if path == "orchestrator-failed":
        service.skill_usage_store.list_curator_managed.side_effect = RuntimeError("catalog unavailable")
    result = await service.run_curator_review()
    root = named(sdk[1], "curator.run")
    payload = json.loads(root.attributes["nagent.details.output_messages.content"])
    assert payload["started_at"] == result.started_at
    assert payload["summary_so_far"] == result.summary_so_far
    assert payload["auto_transitions"]["checked"] == result.auto_transitions.checked
    if path in {"backup-failed", "orchestrator-failed"}:
        assert payload["errors"]
        assert "nagent.details.error.content" in root.attributes


@pytest.mark.asyncio
@pytest.mark.parametrize("large", [False, True])
async def test_curator_payload_is_sanitized_bounded_and_shared_with_completion_log(sdk, monkeypatch, large):
    from app.application.skill_evolution_service import BackgroundReviewResult
    logs = []
    monkeypatch.setattr(ts.logger, "info", lambda *args: logs.append(ts.execution_log_snapshot()))
    evolution = SimpleNamespace(run_background_review=AsyncMock(return_value=BackgroundReviewResult(
        final_text="整理结果" * (1500 if large else 1),
        tool_calls=[{"name": "skill_manage", "arguments": {"password": "SENTINEL-PRIVATE"}}],
    )))
    service = _make_service(rows=[_report("old")], evolution_service=evolution)
    service.render_candidate_list = AsyncMock(return_value="candidate")
    service._finalize_run = AsyncMock()
    await service.run_curator_review(dry_run=True, consolidate=True)
    root = named(sdk[1], "curator.run")
    log = next(item for item in logs if item["operation"] == "curator.run")
    assert root.attributes["nagent.details.output_messages.truncated"] is large
    if not large:
        assert "[REDACTED]" in root.attributes["nagent.details.output_messages.content"]
    assert "SENTINEL-PRIVATE" not in str(root.attributes) + str(log)
    for field, envelope in log["details"].items():
        assert len(envelope["content"].encode("utf-8")) <= 2048
        for key, value in envelope.items():
            assert root.attributes[f"nagent.details.{field}.{key}"] == value


@pytest.mark.asyncio
async def test_curator_payload_projection_failure_does_not_change_result(sdk, monkeypatch):
    import app.application.skill_curator_service as module
    def broken(**kwargs):
        raise RuntimeError("projection failed")
    service = _make_service(rows=[])
    service._finalize_run = AsyncMock()
    monkeypatch.setattr(module, "project_details", broken)
    result = await service.run_curator_review(dry_run=True)
    assert "no changes" in result.summary_so_far
    assert named(sdk[1], "curator.run").attributes["nagent.outcome"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("broken_projection", [False, True])
async def test_curator_cancel_preserves_partial_payload_and_original_cancellation(sdk, monkeypatch, broken_projection):
    import app.application.skill_curator_service as module
    service = _make_service(rows=[_report("old")])
    error = asyncio.CancelledError()
    service.apply_automatic_transitions = AsyncMock(side_effect=error)
    if broken_projection:
        def broken(**kwargs):
            raise RuntimeError("projection failed")
        monkeypatch.setattr(module, "project_details", broken)
    with pytest.raises(asyncio.CancelledError) as caught:
        await service.run_curator_review(dry_run=True)
    assert caught.value is error
    root = named(sdk[1], "curator.run")
    assert root.attributes["nagent.outcome"] == "cancelled"
    if not broken_projection:
        assert "old" in root.attributes["nagent.details.input_messages.content"]
        assert root.attributes["nagent.details.output_messages.incomplete"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["report", "cancel"])
async def test_curator_keeps_completed_transitions_when_later_step_fails(sdk, failure):
    import json
    service = _make_service(rows=[_report("old")])
    if failure == "report":
        service._finalize_run = AsyncMock(side_effect=RuntimeError("report unavailable"))
        result = await service.run_curator_review(dry_run=True)
        assert result.auto_transitions.checked == 0  # keep existing public behavior
    else:
        service._run_consolidation = AsyncMock(side_effect=asyncio.CancelledError())
        service.evolution_service = object()
        with pytest.raises(asyncio.CancelledError):
            await service.run_curator_review(dry_run=True, consolidate=True)
    payload = json.loads(named(sdk[1], "curator.run").attributes["nagent.details.output_messages.content"])
    assert payload["auto_transitions"]["checked"] == 1
    assert payload["auto_transitions"]["archived"] == 1


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


@pytest.mark.parametrize("operation", list(ts._BUSINESS_OPERATIONS) + ["schedule.execute"])
def test_entry_details_are_identical_in_span_and_completion_log(sdk, monkeypatch, operation):
    from app.application.telemetry_details import project_details
    _, exporter = sdk
    logs = []
    monkeypatch.setattr(ts.logger, "info", lambda *args: logs.append(
        (trace.get_current_span().get_span_context(), ts.execution_log_snapshot())))
    scope = ts.schedule_execution_span("task", "session", "claim") if operation == "schedule.execute" else ts.business_operation(operation)
    with scope as facts:
        facts["details"] = project_details(input_messages={"text": "request", "password": "SENTINEL-PRIVATE"},
            output_messages="结果" * 2048)
    span = named(exporter, operation)
    context, log = logs[0]
    assert context == span.context
    assert "SENTINEL-PRIVATE" not in str(span.attributes) + str(log)
    for name, envelope in log["details"].items():
        assert len(envelope["content"].encode("utf-8")) <= 2048
        for key, value in envelope.items():
            assert span.attributes[f"nagent.details.{name}.{key}"] == value
    assert log["details"]["output_messages"]["truncated"] is True


@pytest.mark.asyncio
async def test_gateway_stream_records_deltas_once_and_redacts_split_credentials(sdk):
    _, exporter = sdk
    async def events():
        yield ChatEvent(ChatEventType.CONTENT_DELTA, content='hello {"pass')
        yield ChatEvent(ChatEventType.CONTENT_DELTA, content='word":"SENTINEL-PRIVATE"}')
        yield ChatEvent(ChatEventType.MESSAGE_DONE, content='hello {"password":"SENTINEL-PRIVATE"}')
    harness = Harness()
    harness.chat_service.complete = AsyncMock(return_value=events())
    async for _ in harness.service().handle_message_stream(
        InteractionMessage(id="split", session_key=GatewaySessionKey("cli", "local"), text="hi")):
        pass
    content = named(exporter, "gateway.message").attributes["nagent.details.output_messages.content"]
    assert content.count("hello") == 1
    assert "SENTINEL-PRIVATE" not in content


def test_entry_exception_keeps_safe_error_and_business_exception(sdk):
    _, exporter = sdk
    error = RuntimeError('failed {"password":"SENTINEL-PRIVATE"}')
    with pytest.raises(RuntimeError) as caught:
        with ts.business_operation("task.run"):
            raise error
    assert caught.value is error
    content = named(exporter, "task.run").attributes["nagent.details.error.content"]
    assert "failed" in content
    assert "SENTINEL-PRIVATE" not in content


@pytest.mark.asyncio
async def test_gateway_closing_stream_survives_broken_partial_sanitizer(sdk, monkeypatch):
    from app.application import telemetry_details as td
    class BrokenSanitizer:
        def sanitize(self, text):
            return text
        def sanitize_partial(self, text):
            raise RuntimeError("sanitizer unavailable")
    monkeypatch.setattr(td, "_config", (BrokenSanitizer().sanitize, 2048))
    async def events():
        yield ChatEvent(ChatEventType.CONTENT_DELTA, content="partial")
    harness = Harness()
    harness.chat_service.complete = AsyncMock(return_value=events())
    stream = harness.service().handle_message_stream(
        InteractionMessage(id="broken", session_key=GatewaySessionKey("cli", "local"), text="hi"))
    await anext(stream)
    await stream.aclose()
    assert named(sdk[1], "gateway.message").attributes["nagent.outcome"] == "cancelled"


def test_entry_preserves_exception_with_broken_string_conversion(sdk):
    class BrokenError(RuntimeError):
        def __str__(self):
            raise ValueError("broken formatting")
    error = BrokenError()
    with pytest.raises(BrokenError) as caught:
        with ts.business_operation("task.run"):
            raise error
    assert caught.value is error


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["image", "card"])
async def test_feishu_nontext_entry_records_action_and_actual_response(sdk, kind):
    from tests.interfaces.test_feishu_im_adapter import FakeFeishuClient, FakeGatewayService, image_payload, card_payload
    from app.interfaces.feishu_im_adapter import FeishuImAdapter
    payload = image_payload() if kind == "image" else card_payload()
    await FeishuImAdapter(FakeGatewayService(), FakeFeishuClient(payload)).handle_event(payload)
    attributes = named(sdk[1], "feishu.event").attributes
    assert "nagent.details.output_messages.content" in attributes
    if kind == "card":
        assert "choice" in attributes["nagent.details.input_messages.content"]
        assert "confirmed" in attributes["nagent.details.output_messages.content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["image", "card"])
async def test_feishu_early_rejection_records_actual_response(sdk, kind):
    from tests.interfaces.test_feishu_im_adapter import FakeFeishuClient, FakeGatewayService, image_payload, card_payload
    from app.interfaces.feishu_im_adapter import FeishuImAdapter
    payload = image_payload() if kind == "image" else card_payload(choice="invalid")
    client = FakeFeishuClient(payload, download_error=RuntimeError("download failed"))
    await FeishuImAdapter(FakeGatewayService(), client).handle_event(payload)
    content = named(sdk[1], "feishu.event").attributes["nagent.details.output_messages.content"]
    assert client.sent[0][1] in content


@pytest.mark.asyncio
async def test_gateway_accumulator_failure_does_not_drop_business_event(sdk, monkeypatch):
    from app.application.telemetry_details import StreamDetailsAccumulator
    def broken(self, text):
        raise ValueError("invalid observation text")
    monkeypatch.setattr(StreamDetailsAccumulator, "append_text", broken)
    event = ChatEvent(ChatEventType.CONTENT_DELTA, content="reply")
    async def events():
        yield event
    harness = Harness()
    harness.chat_service.complete = AsyncMock(return_value=events())
    stream = harness.service().handle_message_stream(
        InteractionMessage(id="append", session_key=GatewaySessionKey("cli", "local"), text="hi"))
    assert [item async for item in stream] == [event]

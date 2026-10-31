"""Task attempts must restore their persisted trigger, never the dispatch tick."""
import pytest
from opentelemetry import trace

from tests.application.test_business_trace_ingress import sdk, named
from tests.application.test_task_run_service import FakeDispatcher, FakeNotifier
from app.application import telemetry_semantics as ts
from app.application.task_service import TaskService
from app.application.task_run_service import TaskRunService
from app.application.task_agent_executor import TaskAgentResult
from app.domain.task import Task, TaskRunOutcome, TaskStatus, RecoverRunCommand, TaskValidationError, TaskConflictError, BulkUpdateCommand, BulkUpdateItem
from app.domain.task_policy import TaskPolicy
from app.infrastructure.registry.sqlite_task_registry import SQLiteTaskRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.parametrize("source", ["conversation", "cli.request", "api.request", "manual.click", "schedule.execute"])
async def test_task_trigger_survives_sqlite_reopen_and_automatic_retry(sdk, tmp_path, recover, source):
    tracer, exporter = sdk
    path = str(tmp_path / "tasks.db")
    registry = SQLiteTaskRegistry(path)
    service = TaskService(registry=registry, policy=TaskPolicy())
    with tracer.start_as_current_span(source) as upstream:
        task = await service.create_task(title="trace persistence", max_retries=3)
    if recover:
        stale = await registry.claim_task(task.id, "stale", 900)
        registry = SQLiteTaskRegistry(path)
        await registry.recover_run(RecoverRunCommand(task.id, stale.run.id, "stale", TaskRunOutcome.FAILED))
    # Reconstruct the registry and service as a restarted worker would.
    registry = SQLiteTaskRegistry(path)
    task = await registry.get_task(task.id)
    assert task.trace_context["traceparent"].split("-")[1] == f"{upstream.get_span_context().trace_id:032x}"
    outcomes = iter([TaskRunOutcome.FAILED, TaskRunOutcome.COMPLETED])

    class Executor:
        async def run(self, *args):
            with ts.invoke_agent_span(source="task"):
                return TaskAgentResult(status=next(outcomes), output="done")

    runner = TaskRunService(registry=registry, dispatcher=FakeDispatcher(),
                            executor=Executor(), policy=TaskPolicy(), notifier=FakeNotifier())
    for i in range(2):
        claim = await registry.claim_task(task.id, f"lock-{i}", 900)
        with tracer.start_as_current_span("unrelated-dispatch-tick"):
            await runner.run_claim(claim.task, claim.run.id, f"lock-{i}")
    attempts = [s for s in exporter.get_finished_spans() if s.name == "task.run"]
    assert len(attempts) == 2
    assert len({s.context.span_id for s in attempts}) == 2
    assert all(s.context.trace_id == upstream.get_span_context().trace_id for s in attempts)
    parent_id = int(task.trace_context["traceparent"].split("-")[2], 16)
    assert all(s.parent.span_id == parent_id for s in attempts)
    assert all(s.attributes["nagent.task.trace_context"] == "inherited" for s in attempts)
    assert (await registry.get_task(task.id)).status == TaskStatus.SUCCEEDED


@pytest.mark.asyncio
@pytest.mark.parametrize("carrier,expected", [({}, "missing"), ({"traceparent": "bad"}, "invalid")])
async def test_task_without_context_is_marked_and_does_not_inherit_tick(sdk, tmp_path, carrier, expected):
    tracer, exporter = sdk
    registry = SQLiteTaskRegistry(str(tmp_path / "tasks.db"))
    task = await registry.create_task(Task(id="legacy", title="legacy", trace_context=carrier))
    claim = await registry.claim_task(task.id, "lock", 900)

    class Executor:
        async def run(self, *args):
            return TaskAgentResult(status=TaskRunOutcome.COMPLETED)

    runner = TaskRunService(registry=registry, dispatcher=FakeDispatcher(),
                            executor=Executor(), policy=TaskPolicy(), notifier=FakeNotifier())
    with tracer.start_as_current_span("tick") as tick:
        await runner.run_claim(claim.task, claim.run.id, "lock")
    span = named(exporter, "task.run")
    assert span.parent is None
    assert span.context.trace_id != tick.get_span_context().trace_id
    assert span.attributes["nagent.task.trace_context"] == expected


@pytest.mark.asyncio
async def test_unsampled_task_context_is_preserved(sdk, tmp_path):
    tracer, exporter = sdk
    registry = SQLiteTaskRegistry(str(tmp_path / "tasks.db"))
    parent = trace.SpanContext(0x1234, 0x5678, is_remote=True, trace_flags=trace.TraceFlags(0))
    with trace.use_span(trace.NonRecordingSpan(parent)):
        task = await TaskService(registry=registry, policy=TaskPolicy()).create_task(title="unsampled")
    assert task.trace_context["traceparent"].split("-")[1] == f"{parent.trace_id:032x}"
    assert task.trace_context["traceparent"].endswith("-00")
    contexts = []

    class Executor:
        async def run(self, *args):
            contexts.append(trace.get_current_span().get_span_context())
            return TaskAgentResult(status=TaskRunOutcome.COMPLETED)

    claim = await registry.claim_task(task.id, "lock", 900)
    runner = TaskRunService(registry=registry, dispatcher=FakeDispatcher(),
                            executor=Executor(), policy=TaskPolicy(), notifier=FakeNotifier())
    with tracer.start_as_current_span("unrelated-tick"):
        await runner.run_claim(claim.task, claim.run.id, "lock")
    assert contexts[0].trace_id == parent.trace_id
    assert not contexts[0].trace_flags.sampled
    assert not any(s.name == "task.run" for s in exporter.get_finished_spans())


@pytest.mark.asyncio
async def test_http_task_creation_captures_remote_api_trigger(sdk, tmp_path):
    import httpx
    from fastapi import FastAPI, APIRouter
    from app.interfaces.http.task_routes import register_task_routes
    _, exporter = sdk
    registry = SQLiteTaskRegistry(str(tmp_path / "tasks.db"))
    service = TaskService(registry=registry, policy=TaskPolicy())
    app = FastAPI()
    router = APIRouter()
    register_task_routes(router, task_service=service)
    app.include_router(router)
    carrier = "00-00000000000000000000000000001234-0000000000005678-01"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/chat/tasks", json={"title": "api"}, headers={"traceparent": carrier})
    assert response.status_code == 200
    tasks = await registry.list_tasks()
    task = tasks.items[0]
    assert task.trace_context["traceparent"].split("-")[1] == "00000000000000000000000000001234"
    entry = named(exporter, "task.submit")
    assert entry.parent.span_id == 0x5678


def test_cli_task_creation_owns_runtime_and_trigger(sdk, tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    import asyncio
    import app.main
    from app.interfaces.cli.commands import task as command
    _, exporter = sdk
    registry = SQLiteTaskRegistry(str(tmp_path / "tasks.db"))
    service = TaskService(registry=registry, policy=TaskPolicy())
    closed = []

    @contextmanager
    def runtime():
        try:
            yield SimpleNamespace(task_service=service)
        finally:
            closed.append(True)

    monkeypatch.setattr(app.main, "application_runtime", runtime)
    monkeypatch.setattr(command, "_load_task_service", lambda: service)
    assert command.run(SimpleNamespace(task_command="create", title="cli", json=True, form=False, yaml=False)) == 0
    task = asyncio.run(registry.list_tasks()).items[0]
    assert task.trace_context.get("traceparent")
    assert closed == [True]
    entry = named(exporter, "cli.request")
    assert entry.context.trace_id == int(task.trace_context["traceparent"].split("-")[1], 16)
    assert "cli" in entry.attributes["nagent.details.input_messages.content"]
    assert task.id in entry.attributes["nagent.details.output_messages.content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["trace_context", "trace_context_json"])
async def test_task_context_cannot_be_reassigned_through_edits(sdk, tmp_path, field):
    registry = SQLiteTaskRegistry(str(tmp_path / "tasks.db"))
    service = TaskService(registry=registry, policy=TaskPolicy())
    task = await service.create_task(title="immutable")
    with pytest.raises(TaskValidationError):
        await service.update_task(task.id, {field: {"traceparent": "forged"}}, task.version)
    with pytest.raises(TaskValidationError):
        await service.bulk_update(BulkUpdateCommand((BulkUpdateItem(task.id, {field: {}}, task.version),)))


@pytest.mark.asyncio
async def test_legacy_database_migrates_without_inventing_upstream(tmp_path):
    import sqlite3
    path = str(tmp_path / "tasks.db")
    registry = SQLiteTaskRegistry(path)
    await registry.create_task(Task(id="legacy", title="legacy"))
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE tasks DROP COLUMN trace_context_json")
    registry = SQLiteTaskRegistry(path)
    assert (await registry.get_task("legacy")).trace_context == {}
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE tasks SET trace_context_json = ?", ("broken-json",))
    carrier = (await registry.get_task("legacy")).trace_context
    assert ts.task_trace_context_status(carrier) == "invalid"


@pytest.mark.asyncio
async def test_duplicate_creation_does_not_replace_original_trigger(sdk, tmp_path):
    tracer, _ = sdk
    registry = SQLiteTaskRegistry(str(tmp_path / "tasks.db"))
    service = TaskService(registry=registry, policy=TaskPolicy())
    with tracer.start_as_current_span("first"):
        task = await service.create_task(title="idempotent", idempotency_key="same")
    with tracer.start_as_current_span("second"):
        with pytest.raises(TaskConflictError):
            await service.create_task(title="idempotent", idempotency_key="same")
    assert (await registry.get_task(task.id)).trace_context == task.trace_context

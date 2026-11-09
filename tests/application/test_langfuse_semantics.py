"""Business facts exported through the real OTel SDK."""
import asyncio
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from app.application import telemetry_semantics as ts
from app.application.telemetry_contract import project_execution_attributes, resolve_operation
from app.application.telemetry_details import configure_details, project_details
from tests.application.test_knowledge_service import _service, _base
from tests.application.test_task_agent_executor import _task, FakeChatService, FakeTaskRegistry, FakePromptBuilder
from app.application.task_agent_executor import TaskAgentExecutor
from app.application.chat_service import ChatCompletionResult

@pytest.fixture
def spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(ts.trace, 'get_tracer', provider.get_tracer)
    configure_details(lambda s: s.replace('SENSITIVE', '[REDACTED]'))
    yield exporter
    configure_details(None)
    provider.shutdown()

def test_contract_types_and_content_scope():
    assert resolve_operation('knowledge.search').object == 'knowledge'
    assert resolve_operation('task.evaluate').action == 'evaluate'
    for bad in (True, 0, -1, '1', 1.2):
        assert 'nagent.task.judge_attempt' not in project_execution_attributes(dict(operation='task.evaluate', judge_attempt=bad))
    assert project_execution_attributes(dict(operation='task.evaluate', achieved=False, judge_attempt=2))['nagent.task.achieved'] is False
    assert 'nagent.task.achieved' not in project_execution_attributes(dict(operation='task.evaluate', achieved='false'))

def test_new_content_only_on_corresponding_operation(spans):
    details = project_details(retrieval_query='SENSITIVE', retrieval_results=[], judge_verdict={'achieved': False}, judge_reason='SENSITIVE')
    for operation in ('knowledge.search', 'task.evaluate', 'task.run'):
        with ts.business_operation(operation) as facts:
            facts['details'] = details
    exported = {s.name: s.attributes for s in spans.get_finished_spans()}
    assert exported['knowledge.search']['nagent.content.retrieval_query'] == '[REDACTED]'
    assert 'nagent.content.judge_reason' not in exported['knowledge.search']
    assert exported['task.evaluate']['nagent.content.judge_reason'] == '[REDACTED]'
    assert not any(k.startswith('nagent.content.') for k in exported['task.run'])

@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [None, RuntimeError('SENSITIVE'), asyncio.CancelledError()])
async def test_knowledge_backend_scope(spans, failure):
    service, registry, factory = _service()
    registry.items['kb-1'] = _base()
    backend = factory.get(_base().base_type)
    original_search = backend.search
    async def search_with_http(*args):
        with ts.business_operation('http.client'):
            result = await original_search(*args)
            result.results[0].metadata['evidence'] = 'SENSITIVE'
            return result
    backend.search = search_with_http
    if failure is not None:
        async def fail(*args):
            raise failure
        factory.get(_base().base_type).search = fail
    with ts.business_operation('task.run', session_id='session-1'):
        if failure is None:
            await service.search(kb_id='kb-1', query='SENSITIVE')
        else:
            with pytest.raises(type(failure)):
                await service.search(kb_id='kb-1', query='SENSITIVE')
    exported = spans.get_finished_spans()
    child = next(span for span in exported if span.name == 'knowledge.search')
    parent = next(span for span in exported if span.name == 'task.run')
    if failure is None:
        import json
        http = next(span for span in exported if span.name == 'http.handle')
        assert http.parent.span_id == child.context.span_id
        results = json.loads(child.attributes['nagent.content.retrieval_results'])
        assert results['kb_id'] == 'kb-1'
        assert results['results'][0]['metadata']['evidence'] == '[REDACTED]'
        assert child.attributes['nagent.content.retrieval_results_original_bytes'] > 0
        assert child.attributes['nagent.content.retrieval_results_truncated'] is False
    assert child.name == 'knowledge.search'
    assert child.parent.span_id == parent.context.span_id
    assert child.attributes['gen_ai.conversation.id'] == 'session-1'
    assert child.attributes['nagent.knowledge.outcome'] == ('success' if failure is None else 'cancelled' if isinstance(failure, asyncio.CancelledError) else 'failure')
    assert 'SENSITIVE' not in str(child.attributes)

@pytest.mark.asyncio
@pytest.mark.parametrize('responses, outcomes', [(['{"achieved":false,"reason":"SENSITIVE"}'], ['success']), (['bad', '{"achieved":true,"reason":"ok"}'], ['failure', 'success']), (['bad', 'bad'], ['failure', 'failure']), ([RuntimeError('SENSITIVE')], ['failure'])])
async def test_judge_attempts_are_siblings(spans, responses, outcomes):
    chat = FakeChatService()
    async def complete(request):
        with ts.business_operation('invoke_agent'):
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return ChatCompletionResult(session_id=request.session_id, model='N-Agent', message={'content': response}, finish_reason='stop')
    chat.complete = complete
    executor = TaskAgentExecutor(chat, FakeTaskRegistry(), FakePromptBuilder())
    with ts.business_operation('task.run'):
        await executor._run_judge(_task(), 7, 'claim')
    exported = spans.get_finished_spans()
    judges = [s for s in exported if s.name == 'task.evaluate']
    root = exported[-1]
    assert len(judges) == len(outcomes)
    for attempt, (judge, outcome) in enumerate(zip(judges, outcomes), 1):
        assert judge.parent.span_id == root.context.span_id
        assert judge.attributes['nagent.task.judge_attempt'] == attempt
        assert judge.attributes['nagent.task.outcome'] == outcome
        assert judge.attributes['nagent.task.run_id'] == '7'
        assert 'SENSITIVE' not in str(judge.attributes)
    for agent in (s for s in exported if s.name == 'invoke_agent'):
        assert agent.parent.span_id in {s.context.span_id for s in judges}

@pytest.mark.asyncio
@pytest.mark.parametrize('query, enabled', [('', True), ('valid', False)])
async def test_knowledge_validation_does_not_start_backend_span(spans, query, enabled):
    service, registry, factory = _service()
    registry.items['kb-1'] = _base(enabled=enabled)
    with pytest.raises(Exception):
        await service.search(kb_id='kb-1', query=query)
    assert not spans.get_finished_spans()
    assert not factory.get(_base().base_type).searches

@pytest.mark.asyncio
async def test_judge_cancellation_propagates(spans):
    chat = FakeChatService()
    async def cancel(request):
        raise asyncio.CancelledError()
    chat.complete = cancel
    executor = TaskAgentExecutor(chat, FakeTaskRegistry(), FakePromptBuilder())
    with pytest.raises(asyncio.CancelledError):
        await executor._run_judge(_task(), 7, 'claim')
    judge, = spans.get_finished_spans()
    assert judge.attributes['nagent.task.outcome'] == 'cancelled'
    assert 'nagent.task.achieved' not in judge.attributes

def test_content_envelope_schema_and_transport_budget(spans):
    from app.application.telemetry_details import validate_details
    envelope = project_details(retrieval_query='SENSITIVE')['retrieval_query']
    assert set(envelope) == {'content', 'original_bytes', 'truncated'}
    assert not validate_details({'retrieval_query': {**envelope, 'original_bytes': True}})
    assert not validate_details({'judge_reason': {**envelope, 'untrusted': 'SENSITIVE'}})
    from app.application.telemetry_contract import CONTENT_FIELDS
    for operation in ('chat', 'execute_tool', 'task.run', 'task.evaluate', 'knowledge.search'):
        with ts.business_operation(operation) as facts:
            facts['details'] = project_details(**{key: 'safe' for key in CONTENT_FIELDS})
    for span in spans.get_finished_spans():
        fields = {key for key in span.attributes if key.startswith('nagent.content.') and key.rsplit('.', 1)[-1] in CONTENT_FIELDS}
        assert len(fields) <= 12
        if span.name in ('chat', 'execute_tool', 'task.run'):
            assert not any('retrieval_' in key or 'judge_' in key for key in fields)

@pytest.mark.asyncio
@pytest.mark.parametrize('cancelled', [True, False])
async def test_judge_domain_abort_preserves_terminal_semantics(spans, cancelled):
    from app.domain.agent_run import RunCancelledError, RunOwnershipLost
    from opentelemetry.trace import StatusCode
    error = RunCancelledError('cancelled') if cancelled else RunOwnershipLost('ownership lost')
    chat = FakeChatService()
    async def abort(request):
        raise error
    chat.complete = abort
    executor = TaskAgentExecutor(chat, FakeTaskRegistry(), FakePromptBuilder())
    with pytest.raises(type(error)):
        await executor._run_judge(_task(), 7, 'claim')
    judge, = spans.get_finished_spans()
    assert judge.attributes['nagent.task.outcome'] == ('cancelled' if cancelled else 'failure')
    assert judge.status.status_code == (StatusCode.UNSET if cancelled else StatusCode.ERROR)
    assert 'nagent.task.achieved' not in judge.attributes

@pytest.mark.parametrize('stream', [False, True])
def test_model_parameters_and_first_output(spans, stream):
    with ts.SemanticsTelemetry().begin_chat_attempt(model='model', stream=stream) as attempt:
        attempt.set_model_parameters(dict(temperature=.25, presence_penalty=-1, top_p=True, max_tokens=float('inf'), headers={'secret': 'SENSITIVE'}))
        attempt.append_output_text('')
        attempt.append_output_tool_delta(0)
        attempt.append_output_text('hello')
        attempt.append_output_text('world')
    attrs = spans.get_finished_spans()[0].attributes
    assert attrs['nagent.model.parameter.temperature'] == .25
    assert attrs['nagent.model.parameter.presence_penalty'] == -1
    assert 'nagent.model.parameter.top_p' not in attrs
    assert 'nagent.model.parameter.max_tokens' not in attrs
    assert ('nagent.model.completion_start_time' in attrs) is stream


def test_model_cost_uses_same_attempt_raw_and_independent_buckets(spans):
    telemetry = ts.SemanticsTelemetry()
    calls = []
    def estimate(raw, model, provider, buckets, bucket_states):
        calls.append((raw, model, provider, buckets, bucket_states))
        return {'amount_usd': .001, 'status': 'estimated', 'pricing_version': 'v1'}
    telemetry.set_cost_estimator(estimate)
    with telemetry.begin_chat_attempt(model='actual-model', stream=True, provider='openai') as attempt:
        attempt.set_raw_usage({'prompt_tokens': 10, 'prompt_tokens_details': {'cached_tokens': 2}, 'secret': 'SENSITIVE'})
        attempt.set_raw_usage({'completion_tokens': 4, 'prompt_tokens': None})
        attempt.set_usage(8, 4)
        attempt.set_cache_usage(cache_read_tokens=2, cache_write_tokens=0)
    attrs = spans.get_finished_spans()[0].attributes
    assert attrs['nagent.model.cost_usd'] == .001
    assert attrs['nagent.model.cost_status'] == 'estimated'
    assert calls == [({'prompt_tokens': 10, 'prompt_tokens_details': {'cached_tokens': 2}, 'completion_tokens': 4}, 'actual-model', 'openai', (8, 4, 2, 0), ('known', 'known', 'known', 'known'))]

@pytest.mark.parametrize('result', [None, {}, {'amount_usd': True, 'status': 'estimated', 'pricing_version': 'v1'}, {'amount_usd': float('nan'), 'status': 'estimated', 'pricing_version': 'v1'}, {'amount_usd': -1, 'status': 'estimated', 'pricing_version': 'v1'}, {'amount_usd': 0, 'status': 'unknown', 'pricing_version': 'v1'}, {'amount_usd': 0, 'status': 'estimated', 'pricing_version': ''}])
def test_invalid_cost_omitted(spans, result):
    telemetry = ts.SemanticsTelemetry()
    telemetry.set_cost_estimator(lambda *args: result)
    with telemetry.begin_chat_attempt(model='actual', stream=False) as attempt:
        attempt.set_usage(1, 2)
        attempt.set_cache_usage(cache_read_tokens=0, cache_write_tokens=0)
    assert 'nagent.model.cost_usd' not in spans.get_finished_spans()[0].attributes

@pytest.mark.parametrize('output', ['empty', 'failure', 'tool'])
def test_completion_start_requires_first_real_output(spans, output):
    with ts.SemanticsTelemetry().begin_chat_attempt(model='actual', stream=True) as attempt:
        attempt.append_output_text('')
        attempt.append_output_tool_delta(0, id='', name='', arguments='')
        if output == 'failure':
            attempt.set_error('RuntimeError')
        if output == 'tool':
            attempt.append_output_tool_delta(0, name='lookup')
            first = attempt._span.attributes['nagent.model.completion_start_time']
            attempt.append_output_text('later')
    attrs = spans.get_finished_spans()[0].attributes
    assert ('nagent.model.completion_start_time' in attrs) is (output == 'tool')
    if output == 'tool':
        from datetime import datetime
        assert datetime.fromisoformat(attrs['nagent.model.completion_start_time']).utcoffset().total_seconds() == 0
        assert attrs['nagent.model.completion_start_time'] == first


def test_unknown_usage_and_estimation_error_are_isolated(spans):
    telemetry = ts.SemanticsTelemetry()
    calls = []
    def fail(*args):
        calls.append(args)
        raise RuntimeError('pricing unavailable')
    telemetry.set_cost_estimator(fail)
    for known in (False, True):
        with telemetry.begin_chat_attempt(model='actual', stream=False) as attempt:
            attempt.set_usage(1, 2)
            attempt.set_cache_usage(cache_read_tokens=0 if known else None, cache_write_tokens=0)
    # T4：finalization 恒定收口成本（桶不完整也调用 estimator，由 raw 视图
    # 裁决 unknown 原因）；estimator 异常被隔离，不产出成本事实
    assert len(calls) == 2
    assert all('nagent.model.cost_usd' not in span.attributes for span in spans.get_finished_spans())
    assert all('nagent.model.cost_status' not in span.attributes for span in spans.get_finished_spans())

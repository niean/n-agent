"""Safe execution completion projections."""
import asyncio
import pytest
from app.application import telemetry_semantics as ts
from app.application import telemetry_details as td
from app.infrastructure.observability.sanitizer import LogSanitizer


@pytest.fixture(autouse=True)
def isolated_detail_configuration(monkeypatch):
    # Application assembly in other tests must not decide this file's mode.
    monkeypatch.setattr(td, '_config', (None, 2048))
    monkeypatch.setattr(ts, '_span_attr_sanitizer', None)


def test_completion_snapshot_contract(monkeypatch):
    snapshot = getattr(ts, 'execution_log_snapshot', None)
    assert callable(snapshot), 'execution snapshot must be available for logging injection'
    logs = []
    monkeypatch.setattr(ts.logger, 'info', lambda template: logs.append((template, snapshot())))
    tel = ts.SemanticsTelemetry()
    with tel.agent_run('session', 'cli', 'request', message_count=2):
        assert snapshot() == {'session_id': 'session', 'request_id': 'request',
                              'source': 'cli', 'operation': 'invoke_agent',
                              'message_count': 2}
        with tel.iteration(1, message_count=3):
            with tel.begin_chat_attempt(model='gpt-4o', stream=False) as attempt:
                attempt.set_usage(0, 0)
                attempt.set_finish_reasons(['PRIVATE-SENTINEL'])
        # after the chat attempt exits, the agent_run scene is restored
        assert snapshot()['operation'] == 'invoke_agent'
        assert snapshot()['request_id'] == 'request'
        tel.record_tool_denial('terminal', 'call')
    assert snapshot() == {}
    assert [record['operation'] for _, record in logs] == ['chat', 'execute_tool', 'invoke_agent']
    assert all(template == 'execution_completed' for template, _ in logs)
    assert logs[0][1]['message_count'] == 3
    assert logs[0][1]['finish_reason'] == 'other'
    assert logs[-1][1]['input_tokens'] == 0
    assert logs[-1][1]['usage_known'] is True
    assert 'PRIVATE-SENTINEL' not in str(logs)


@pytest.mark.asyncio
async def test_cleanup_and_concurrency(monkeypatch):
    snapshot = getattr(ts, 'execution_log_snapshot', None)
    assert callable(snapshot)
    monkeypatch.setattr(ts.logger, 'info', lambda _: (_ for _ in ()).throw(ValueError('logger')))
    async def run(identity):
        with pytest.raises(asyncio.CancelledError):
            with ts.SemanticsTelemetry().agent_run(identity, 'cli', identity):
                await asyncio.sleep(0)
                assert snapshot()['request_id'] == identity
                raise asyncio.CancelledError()
        assert snapshot() == {}
    await asyncio.gather(run('a'), run('b'))


@pytest.mark.parametrize('usage,expected', [([(0, 0)], (0, 0, True)), ([(2, 3), (None, 4)], (None, 7, False)), ([(None, None)], (None, None, False)), ([], (0, 0, True))])
def test_usage_knownness_and_span_lifecycle(monkeypatch, usage, expected):
    from tests.application.test_agent_graph_telemetry import ParentTrackingTracer
    tracer = ParentTrackingTracer()
    monkeypatch.setattr(ts, 'get_tracer', lambda: tracer)
    logs = []
    def capture(template):
        span = ts.trace.get_current_span()
        assert span.ended == 0
        record = ts.execution_log_snapshot()
        assert span.attributes[ts.NAGENT_SUMMARY] == record['summary']
        logs.append(record)
    monkeypatch.setattr(ts.logger, 'info', capture)
    tel = ts.SemanticsTelemetry()
    with tel.agent_run('session', 'cli', 'request'):
        for tokens in usage:
            with tel.begin_chat_attempt(model='gpt-4o', stream=True) as attempt:
                attempt.set_usage(*tokens)
        with tel.tool_execution('terminal', 'call', 'safe') as holder:
            holder.update(executed=True, policy_outcome='allow')
        tel.record_tool_denial('terminal', 'denied-call')
    root = logs[-1]
    assert (root.get('input_tokens'), root.get('output_tokens'), root['usage_known']) == expected
    assert root['inference_calls'] == len(usage)
    assert root['tool_calls'] == 1
    for span in tracer.spans:
        assert span.ended == 1
        assert span.attributes[ts.GEN_AI_CONVERSATION_ID] == 'session'
        assert span.attributes[ts.NAGENT_RUN_ID] == 'request'


@pytest.mark.parametrize('failure_type', [ValueError, asyncio.CancelledError])
@pytest.mark.parametrize('details_enabled', [False, True])
def test_business_failure_is_preserved_and_completed(monkeypatch, failure_type, details_enabled):
    from tests.application.test_agent_graph_telemetry import ParentTrackingTracer
    tracer = ParentTrackingTracer()
    monkeypatch.setattr(ts, 'get_tracer', lambda: tracer)
    logs = []
    monkeypatch.setattr(ts.logger, 'info', lambda _: logs.append(ts.execution_log_snapshot()))
    if details_enabled:
        sanitizer = LogSanitizer().sanitize
        monkeypatch.setattr(td, '_config', (sanitizer, 2048))
        monkeypatch.setattr(ts, '_span_attr_sanitizer', sanitizer)
    secret = 'fake-credential-123'
    message = f'ordinary failure api_key={secret}'
    failure = failure_type(message)
    tel = ts.SemanticsTelemetry()
    with pytest.raises(type(failure)) as caught:
        with tel.agent_run('s', 'cli', 'r'):
            with tel.begin_chat_attempt(model='unregistered-model', stream=False):
                raise failure
    assert caught.value is failure
    assert len(logs) == 2
    assert secret not in str(logs)
    assert logs[0]['model'] == 'other'
    error_detail = logs[0].get('details', {}).get('error')
    if details_enabled:
        assert error_detail['content'] == 'ordinary failure api_key=[REDACTED]'
        assert 'unregistered-model' not in str(logs)
    else:
        assert error_detail is None
        assert 'ordinary failure' not in str(logs)
    assert all(span.ended == 1 for span in tracer.spans)
    assert ts.execution_log_snapshot() == {}


def test_nested_context_restores_and_clear_detaches(monkeypatch):
    monkeypatch.setattr(ts.logger, 'info', lambda _: None)
    tel = ts.SemanticsTelemetry()
    with tel.agent_run('outer', 'cli', 'outer'):
        with tel.iteration(4, message_count=9):
            with tel.agent_run('inner', 'chat', 'inner'):
                assert ts.execution_log_snapshot()['request_id'] == 'inner'
            assert ts.execution_log_snapshot()['request_id'] == 'outer'
            ts.clear_run_context()
            assert ts.execution_log_snapshot() == {}
            assert ts.current_iteration() is None


@pytest.mark.asyncio
@pytest.mark.parametrize('end_reason,failed,expected', [
    ('budget_denied', False, 'denied'), ('deadline', True, 'error'),
    ('max_iterations', True, 'error'), ('error', True, 'error'),
    ('cancelled', True, 'cancelled'), ('completed', False, 'success'),
])
async def test_graph_returned_status_controls_completion(tmp_path, monkeypatch, end_reason, failed, expected):
    from tests.application.test_agent_graph_telemetry import _make_runner, _make_session, DirectProvider
    from app.domain.agent import AgentState, RunStatus
    from types import SimpleNamespace
    tel = ts.SemanticsTelemetry()
    runner, store = _make_runner(tmp_path, DirectProvider(), telemetry=tel)
    await _make_session(store, 'returned-state')
    state = AgentState(session_id='returned-state', input_messages=[])
    async def invoke(_state, _config):
        runner._run_end_reasons[state.run_id] = end_reason
        state.error = 'PRIVATE-SENTINEL' if failed else None
        state.run_status = RunStatus.FAILED if failed else RunStatus.COMPLETED
        return state
    runner.graph = SimpleNamespace(ainvoke=invoke)
    logs = []
    monkeypatch.setattr(ts.logger, 'info', lambda _: logs.append(ts.execution_log_snapshot()))
    await runner.run(state, 'gpt-4o')
    assert logs[-1]['outcome'] == expected
    assert logs[-1]['end_reason'] == end_reason
    assert 'PRIVATE-SENTINEL' not in str(logs)


def test_summaries_describe_operations(monkeypatch):
    logs = []
    monkeypatch.setattr(ts.logger, 'info', lambda _: logs.append(ts.execution_log_snapshot()))
    tel = ts.SemanticsTelemetry()
    with tel.agent_run('s', 'cli', 'r', message_count=2):
        with tel.begin_chat_attempt(model='gpt-4o', stream=False):
            pass
        tel.record_tool_denial('terminal')
    assert logs[0]['summary'].startswith('模型调用 gpt-4o')
    assert logs[1]['summary'].startswith('工具执行 terminal')
    assert logs[2]['summary'].startswith('Agent 执行')
    assert all(len(log['summary']) <= 512 for log in logs)

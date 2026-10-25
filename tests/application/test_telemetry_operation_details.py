"""Projected operation details stay identical across trace and completion logs."""
import asyncio
import pytest
from app.application import telemetry_details as td
from app.application import telemetry_semantics as ts
from app.infrastructure.observability.sanitizer import LogSanitizer
from tests.application.test_tool_service_telemetry import FakeTracer, RecordingExecutor, _service, _request

@pytest.fixture(autouse=True)
def details(monkeypatch):
    monkeypatch.setattr(td, '_config', (LogSanitizer().sanitize, 128))
    monkeypatch.setattr(ts, '_span_attr_sanitizer', LogSanitizer().sanitize)

@pytest.fixture
def capture(monkeypatch):
    tracer = FakeTracer()
    snapshots = []
    monkeypatch.setattr(ts, 'get_tracer', lambda: tracer)
    monkeypatch.setattr(ts.logger, 'info', lambda *a, **k: snapshots.append(dict(ts._completion.get())))
    return tracer, snapshots

def test_chat_details_match_snapshot(capture):
    tracer, snapshots = capture
    with ts.ChatAttempt(model='gpt-4o', stream=False) as attempt:
        attempt.set_input([{'role':'system','content':'rules'}, {'role':'user','content':'hello'}], tools=[])
        attempt.set_output({'role':'assistant','content':'answer'})
    details = snapshots[0]['details']
    assert 'rules' in details['system_prompt']['content']
    assert 'system' not in details['input_messages']['content']
    for key, value in td.flatten_details(details).items():
        assert tracer.spans[0].attributes[key] == value

@pytest.mark.parametrize('cancel', [False, True])
def test_stream_preserves_useful_partial_content(capture, cancel):
    attempt = ts.ChatAttempt(model='gpt-4o', stream=True)
    try:
        with attempt:
            attempt.append_output_text('useful partial reply' if cancel else 'x' * 1000)
            if cancel:
                raise asyncio.CancelledError()
    except asyncio.CancelledError:
        pass
    item = capture[1][0]['details']['output_messages']
    assert item['incomplete'] and item['truncated']
    assert ('useful partial reply' if cancel else 'x') in item['content']

def test_noop_hooks():
    with ts.NoopTelemetry().begin_chat_attempt(model='x', stream=True) as attempt:
        attempt.set_input([], system='x')
        attempt.set_output({})
        attempt.append_output_text('x')
        attempt.append_output_tool_delta(0, arguments='{}')
        attempt.set_error_detail('x')

@pytest.mark.asyncio
@pytest.mark.parametrize('behavior,name', [('success','calculator'), ('error_status','calculator'), ('success','disabled_tool'), ('raise','calculator'), ('cancel','calculator')])
async def test_tool_details(capture, behavior, name):
    service = _service(RecordingExecutor(behavior), ts.SemanticsTelemetry())
    try:
        result = await service.execute(_request(name))
    except (RuntimeError, asyncio.CancelledError):
        assert 'error' in capture[1][0]['details']
    else:
        assert capture[1][0]['details']['tool_result'] == td.project_details(tool_result=result.content)['tool_result']
    assert 'tool_arguments' in capture[1][0]['details']

def test_run_details_context_isolation(capture):
    telemetry = ts.SemanticsTelemetry()
    with telemetry.agent_run() as outer:
        outer.details.update(td.project_details(user_message='outer'))
        with telemetry.agent_run() as inner:
            inner.details.update(td.project_details(user_message='inner'))
    assert capture[1][0]['details']['user_message']['content'] == 'inner'
    assert capture[1][1]['details']['user_message']['content'] == 'outer'
    assert ts._completion.get() is None

@pytest.mark.asyncio
async def test_agent_captures_final_plugin_output(tmp_path, capture, monkeypatch):
    from tests.application.test_agent_graph_telemetry import DirectProvider, _make_runner, _make_session
    from app.domain.agent import AgentState
    runner, store = _make_runner(tmp_path, DirectProvider(), telemetry=ts.SemanticsTelemetry())
    await _make_session(store, 'details')
    dispatch = runner._dispatch_hook
    async def transformed(hook, **kwargs):
        if hook == 'transform_llm_output':
            return ['final plugin output']
        return await dispatch(hook, **kwargs)
    monkeypatch.setattr(runner, '_dispatch_hook', transformed)
    result = await runner.run(AgentState(session_id='details', input_messages=[{'role':'user','content':'question'}]), 'gpt-4o')
    details = capture[1][-1]['details']
    assert 'question' in details['user_message']['content']
    assert 'final plugin output' in details['assistant_message']['content']
    assert result.final_message['content'] == 'final plugin output'


def test_approval_denial_details(capture):
    from app.application.agent_graph import AgentGraphRunner
    from types import SimpleNamespace
    result = AgentGraphRunner._permission_denied_result(SimpleNamespace(_telemetry=ts.SemanticsTelemetry()), _request('calculator'), 'approval_required')
    assert capture[1][0]['details']['tool_result'] == td.project_details(tool_result=result.content)['tool_result']
    assert len(capture[0].spans) == 1


def test_chat_tool_delta_and_caught_error(capture):
    with ts.ChatAttempt(model='gpt-4o', stream=True) as attempt:
        attempt.set_input([], system='separate prompt')
        attempt.append_output_tool_delta(0, id='call', name='calculator', arguments='{"password":"secret"}')
    assert 'secret' not in capture[1][0]['details']['output_messages']['content']
    assert capture[1][0]['details']['system_prompt']['content'] == 'separate prompt'
    with ts.ChatAttempt(model='gpt-4o', stream=True) as attempt:
        attempt.append_output_text('partial')
        attempt.set_error('RuntimeError')
        attempt.set_error_detail('failed')
    assert capture[1][1]['details']['error']['content'] == 'failed'
    assert capture[1][1]['details']['output_messages']['incomplete']


def test_broken_error_string_preserves_completion_and_releases_stream(capture):
    class BrokenError(Exception):
        def __str__(self):
            raise ValueError('broken error text')
    attempt = ts.ChatAttempt(model='gpt-4o', stream=True)
    with pytest.raises(BrokenError):
        with attempt:
            attempt.append_output_text('private partial output')
            raise BrokenError()
    assert capture[1][0]['outcome'] == 'error'
    assert capture[1][0]['details']['output_messages']['incomplete']
    assert attempt._output_accumulator is None

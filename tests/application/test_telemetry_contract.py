from app.application import telemetry_semantics as ts
import pytest
import asyncio


@pytest.mark.parametrize('mode', ['redact', 'raise', 'invalid'])
def test_completion_event_uses_same_fail_closed_sanitized_attributes(monkeypatch, mode):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    secret = 'private-token'
    def sanitize(value):
        if secret in value:
            if mode == 'raise':
                raise ValueError('sanitizer unavailable')
            if mode == 'invalid':
                return None
            return value.replace(secret, '[REDACTED]')
        return value
    monkeypatch.setattr(ts, '_span_attr_sanitizer', sanitize)
    monkeypatch.setattr(ts.logger, 'info', lambda _: None)
    tracer = provider.get_tracer('sanitized-events')
    with tracer.start_as_current_span('chat') as span:
        ts._complete(span, 'chat', ts.time.monotonic(), model=secret,
                     session_id=secret, finish_reason=[secret, 'stop'], outcome='success')
    completed, = exporter.get_finished_spans()
    attrs = completed.attributes
    event_attrs = completed.events[-1].attributes
    assert secret not in str(dict(attrs))
    assert secret not in str(dict(event_attrs))
    assert dict(attrs) == dict(event_attrs)
    if mode == 'redact':
        assert attrs['gen_ai.conversation.id'] == '[REDACTED]'
        assert attrs['gen_ai.response.finish_reasons'] == ('[REDACTED]', 'stop')
        assert '[REDACTED]' in attrs['nagent.model.summary']
    else:
        assert 'gen_ai.conversation.id' not in attrs
        assert 'gen_ai.response.finish_reasons' not in attrs
        assert 'nagent.model.summary' not in attrs
    monkeypatch.setattr(ts, 'get_tracer', lambda: tracer)
    with ts.business_operation('task.run', session_id=secret):
        pass
    business = exporter.get_finished_spans()[-1]
    assert secret not in str(dict(business.attributes))
    assert secret not in str(dict(business.events[-1].attributes))
    assert dict(business.attributes) == dict(business.events[-1].attributes)
    provider.shutdown()


@pytest.mark.parametrize('scope', ['business', 'agent', 'model', 'tool', 'compression', 'schedule'])
@pytest.mark.parametrize('error,expected', [(GeneratorExit, 'cancelled'), (asyncio.CancelledError, 'cancelled'), (TimeoutError, 'timed_out'), (ValueError, 'failure')])
def test_all_scopes_real_sdk_exception_overrides_stale_success(monkeypatch, scope, error, expected):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer('all-exceptions')
    monkeypatch.setattr(ts, 'get_tracer', lambda: tracer)
    logs, measurements = [], []
    monkeypatch.setattr(ts.logger, 'info', lambda _: logs.append(ts.execution_log_snapshot()))
    monkeypatch.setattr(ts, '_record', lambda name, value, dimensions: measurements.append((name, dimensions)))
    tel = ts.SemanticsTelemetry()
    scopes = {
        'business': lambda: ts.business_operation('task.run'),
        'agent': lambda: tel.agent_run('session', 'cli', 'agent'),
        'model': lambda: tel.begin_chat_attempt(model='gpt-4o', stream=False),
        'tool': lambda: tel.tool_execution('terminal'),
        'compression': tel.compression,
        'schedule': lambda: ts.schedule_execution_span('task', 'session', 'claim'),
    }
    before = ts.execution_log_snapshot()
    failure = error()
    with tracer.start_as_current_span('parent') as parent:
        with pytest.raises(error) as caught:
            with scopes[scope]() as result:
                if isinstance(result, dict):
                    result.update(outcome='success', status='success')
                elif scope == 'agent':
                    result.outcome = 'success'
                    result.end_reason = 'completed'
                else:
                    result.set_finish_reasons('stop')
                raise failure
        assert caught.value is failure
        assert ts.trace.get_current_span() is parent
        assert ts.execution_log_snapshot() == before
    span = next(s for s in exporter.get_finished_spans() if s.name != 'parent')
    obj = {'business': 'task', 'compression': 'context', 'model': 'model'}.get(scope, scope)
    assert span.attributes[f'nagent.{obj}.outcome'] == expected
    assert span.events[-1].attributes[f'nagent.{obj}.outcome'] == expected
    assert logs[-1]['outcome'] == expected
    assert span.attributes['error.type'] == ts.map_error_type(error.__name__)
    assert span.events[-1].attributes['error.type'] == span.attributes['error.type']
    assert logs[-1]['error_type'] == span.attributes['error.type']
    assert expected in logs[-1]['summary']
    if scope == 'agent':
        reason = 'cancelled' if expected == 'cancelled' else 'deadline' if expected == 'timed_out' else 'error'
        assert span.attributes['nagent.agent.end_reason'] == reason
        assert logs[-1]['end_reason'] == reason
    assert (span.status.status_code is ts.StatusCode.ERROR) == (expected in {'failure', 'timed_out'})
    if scope == 'compression':
        assert measurements == [('nagent.context.compression_attempts', {'nagent.context.outcome': expected})]
    provider.shutdown()


@pytest.mark.parametrize('scope,error,expected', [('task', TimeoutError, 'timed_out'), ('tool', TimeoutError, 'timed_out'), ('tool', GeneratorExit, 'cancelled')])
def test_real_exception_outcomes(monkeypatch, scope, error, expected):
    logs = []
    monkeypatch.setattr(ts.logger, 'info', lambda _: logs.append(ts.execution_log_snapshot()))
    with pytest.raises(error):
        with (ts.business_operation('task.run') if scope == 'task' else ts.SemanticsTelemetry().tool_execution('terminal')):
            raise error()
    assert logs[-1]['outcome'] == expected


def test_tool_actual_timeout_overrides_stale_result(monkeypatch):
    logs = []
    monkeypatch.setattr(ts.logger, 'info', lambda _: logs.append(ts.execution_log_snapshot()))
    with pytest.raises(TimeoutError):
        with ts.SemanticsTelemetry().tool_execution('terminal') as result:
            result['outcome'] = 'success'
            raise TimeoutError()
    assert logs[-1]['outcome'] == 'timed_out'


def test_registry_declares_operation_boundaries():
    from app.application.telemetry_contract import resolve_operation, project_execution_attributes
    spec = resolve_operation('chat')
    assert 'nagent.model.message_count' in spec.local_attributes
    assert 'nagent.agent.run_id' in spec.shared_attributes
    assert 'timed_out' in spec.outcomes
    assert 'gen_ai.client.inference.duration' in spec.instruments
    attrs = project_execution_attributes({'operation': 'chat', 'nagent.tool.outcome': 'denied', 'nagent.agent.outcome': 'failure', 'nagent.agent.run_id': 'a', 'nagent.model.outcome': 'error'})
    assert 'nagent.tool.outcome' not in attrs
    assert 'nagent.agent.outcome' not in attrs
    assert attrs['nagent.agent.run_id'] == 'a'
    assert attrs['nagent.model.outcome'] == 'failure'


def test_denial_metric_exemplar_belongs_to_tool_span(monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.metrics._internal.exemplar import AlwaysOnExemplarFilter
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader], exemplar_filter=AlwaysOnExemplarFilter())
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(ts, 'get_tracer', lambda: provider.get_tracer('denial'))
    monkeypatch.setattr(ts, 'get_meter', lambda: meter_provider.get_meter('denial'))
    monkeypatch.setattr(ts, '_instrument_cache', {})
    with provider.get_tracer('parent').start_as_current_span('parent'):
        ts.SemanticsTelemetry().record_tool_denial('terminal')
    tool = next(s for s in exporter.get_finished_spans() if s.name.startswith('execute_tool'))
    metrics = [m for r in reader.get_metrics_data().resource_metrics for s in r.scope_metrics for m in s.metrics]
    metric, = [m for m in metrics if m.name == 'gen_ai.execute_tool.duration']
    exemplar, = metric.data.data_points[0].exemplars
    assert exemplar.span_id == tool.context.span_id
    assert exemplar.trace_id == tool.context.trace_id
    meter_provider.shutdown()
    provider.shutdown()


def test_contract_registry_and_projection():
    from app.application import telemetry_contract as c
    operation = c.resolve_operation('task.run')
    assert operation.object == 'task'
    assert c.completion_event(operation) == 'nagent.task.completed'
    attrs = c.project_execution_attributes({'operation': 'task.run', 'run_id': 'task-1', 'outcome': 'waiting_approval'})
    assert attrs['nagent.task.run_id'] == 'task-1'
    assert attrs['nagent.task.outcome'] == 'deferred'
    assert 'nagent.agent.run_id' not in attrs
    assert c.project_execution_attributes({'operation': 'invented.x'}) == {'nagent.observation.error': 'unknown_object'}


def test_outcomes():
    from app.application.telemetry_contract import normalize_outcome
    for source, expected in [('completed', 'success'), ('error', 'failure'), ('denied', 'denied'), ('cancelled', 'cancelled'), ('timed_out', 'timed_out'), ('skipped', 'skipped'), ('blocked', 'deferred'), (None, 'unknown'), ('aborted', 'failure')]:
        assert normalize_outcome(source) == expected


def test_content_allowlist_is_exact_and_fixed():
    from app.application import telemetry_contract as c
    assert len(c.CONTENT_FIELDS) == 16
    assert all(len(c.operation_content_fields(spec.object, spec.action)) <= 12 for spec in c.OPERATIONS.values())
    assert len(c.CONTENT_ATTRIBUTE_KEYS) == 80
    assert all(len(key.split('.')) == 3 for key in c.CONTENT_ATTRIBUTE_KEYS)
    assert 'nagent.content.input_messages_observed_bytes' in c.CONTENT_ATTRIBUTE_KEYS


def test_modality_is_only_a_usage_counter_dimension(monkeypatch):
    recorded = []
    monkeypatch.setattr(ts, '_record', lambda instrument, value, dimensions: recorded.append((instrument, dimensions)))
    ts.record_inference_tokens('gpt-4o', 'openai', 'chat', 3, 4, modality='text')
    for instrument, dimensions in recorded:
        assert set(dimensions) <= set(ts.INSTRUMENTS[instrument].allowed_dimensions)


def test_memory_provider_is_not_model_provider():
    from app.application.telemetry_contract import project_execution_attributes
    attrs = project_execution_attributes({'operation': 'memory.prompt builtin', 'provider': 'builtin'})
    assert attrs['nagent.memory.provider'] == 'builtin'
    assert 'gen_ai.provider.name' not in attrs


def test_model_cache_attributes_registered():
    from app.application import telemetry_contract as tc
    assert 'cache_read_tokens' in tc.OBJECT_ATTRIBUTES['model']
    assert 'cache_write_tokens' in tc.OBJECT_ATTRIBUTES['model']
    assert 'nagent.model.normalized_tokens' not in {f'nagent.model.{f}' for f in tc.OBJECT_ATTRIBUTES['model']}
    assert tc.ATTRIBUTE_TYPES['nagent.model.cache_read_tokens'] == 'integer'
    assert tc.ATTRIBUTE_TYPES['nagent.model.cache_write_tokens'] == 'integer'


def test_model_instruments_include_cache_and_normalized():
    from app.application import telemetry_contract as tc
    model_insts = tc._instruments['model']
    for name in ('nagent.model.cache_read_tokens', 'nagent.model.cache_write_tokens',
                 'nagent.model.normalized_tokens', 'nagent.model.cache_eligible_prompt_tokens',
                 'nagent.model.cache_eligible_read_tokens'):
        assert name in model_insts


def test_model_cost_attributes_registered():
    """T4：成本属性键登记进 OBJECT_ATTRIBUTES['model'] / 类型表，金额分项
    与 cost_usd 同款 number 守卫。"""
    from app.application import telemetry_contract as tc
    for field in ('cost_status', 'cost_reason', 'cost_currency', 'cost_decimal',
                  'cost_input_usd', 'cost_output_usd', 'cost_cache_read_usd',
                  'cost_cache_write_usd', 'price_id', 'billing_provider', 'billing_model'):
        assert f'nagent.model.{field}' in tc.EXECUTION_ATTRIBUTE_KEYS, field
        assert field in tc.OBJECT_ATTRIBUTES['model'], field
    assert tc.ATTRIBUTE_TYPES['nagent.model.cost_status'] == 'string'
    assert tc.ATTRIBUTE_TYPES['nagent.model.cost_reason'] == 'string'
    assert tc.ATTRIBUTE_TYPES['nagent.model.cost_decimal'] == 'string'
    assert tc.ATTRIBUTE_TYPES['nagent.model.price_id'] == 'string'
    assert tc.ATTRIBUTE_TYPES['nagent.model.billing_provider'] == 'string'
    assert tc.ATTRIBUTE_TYPES['nagent.model.billing_model'] == 'string'
    for field in ('cost_input_usd', 'cost_output_usd', 'cost_cache_read_usd', 'cost_cache_write_usd'):
        assert tc.ATTRIBUTE_TYPES[f'nagent.model.{field}'] == 'number', field


def test_model_instruments_include_cost_counters():
    from app.application import telemetry_contract as tc
    model_insts = tc._instruments['model']
    for name in ('nagent.model.cost', 'nagent.model.cost_components', 'nagent.model.cost_attempts'):
        assert name in model_insts


def test_cost_sentinel_attributes_never_pass_whitelist():
    """哨兵：伪造 langfuse.* 属性、未知 metadata、错误类型属性不能通过白名单；
    合法成本键保留且类型不符时丢弃。"""
    from app.application.telemetry_contract import project_execution_attributes
    attrs = project_execution_attributes({
        'operation': 'chat',
        'langfuse.observation.model.name': 'gpt-4o',
        'langfuse.metadata.injected': 'payload',
        'metadata': {'evil': 1},
        'nagent.model.free_text': 'injected',
        'nagent.model.cost_status': 'estimated',
        'nagent.model.cost_reason': 7,
        'nagent.model.cost_usd': '0.1',
        'nagent.model.cost_decimal': 0.5,
        'nagent.model.billing_provider': 3,
    })
    assert attrs['nagent.model.cost_status'] == 'estimated'
    for key in ('langfuse.observation.model.name', 'langfuse.metadata.injected',
                'metadata', 'nagent.model.free_text', 'nagent.model.cost_reason',
                'nagent.model.cost_usd', 'nagent.model.cost_decimal',
                'nagent.model.billing_provider'):
        assert key not in attrs


def test_unknown_operation_keeps_completion_log(monkeypatch):
    logs = []
    monkeypatch.setattr(ts.logger, 'info', lambda _: logs.append(ts.execution_log_snapshot()))
    with ts.business_operation('unregistered.operation'):
        pass
    assert ts.project_execution_attributes(logs[0]) == {'nagent.observation.error': 'unknown_object'}


def test_compression_cancelled_is_not_failure(monkeypatch):
    import asyncio
    import pytest
    logs = []
    monkeypatch.setattr(ts.logger, 'info', lambda _: logs.append(ts.execution_log_snapshot()))
    with pytest.raises(asyncio.CancelledError):
        with ts.SemanticsTelemetry().compression():
            raise asyncio.CancelledError()
    assert logs[0]['outcome'] == 'cancelled'


def test_real_sdk_nested_identity_and_completion(monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(ts, 'get_tracer', lambda: provider.get_tracer('identities'))
    tel = ts.SemanticsTelemetry()
    with ts.business_operation('task.run', run_id='task-run'):
        with tel.agent_run('conversation', 'task', 'agent-run', message_count=2):
            with tel.iteration(1, message_count=5):
                with tel.begin_chat_attempt(model='gpt-4o', stream=False) as attempt:
                    attempt.set_usage(None, 4)
            tel.record_tool_denial('terminal')
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert 'nagent.agent.run_id' not in spans['task.run'].attributes
    assert spans['task.run'].attributes['nagent.task.run_id'] == 'task-run'
    assert spans['chat gpt-4o'].attributes['nagent.agent.run_id'] == 'agent-run'
    assert spans['chat gpt-4o'].attributes['nagent.model.message_count'] == 5
    assert spans['chat gpt-4o'].attributes['nagent.model.usage_known'] is False
    assert spans['invoke_agent n-agent'].attributes['nagent.agent.message_count'] == 2
    assert spans['invoke_agent n-agent'].attributes['nagent.agent.usage_known'] is False
    assert spans['execute_tool terminal'].attributes['nagent.tool.executed'] is False
    assert 'nagent.agent.outcome' not in spans['execute_tool terminal'].attributes
    for span in spans.values():
        event = span.events[-1]
        assert event.name.startswith('nagent.')
        assert all(span.attributes[key] == value for key, value in event.attributes.items())
    provider.shutdown()


def test_real_sdk_object_completion_and_tool_operation(monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(ts, 'get_tracer', lambda: provider.get_tracer('contract'))
    with ts.SemanticsTelemetry().tool_execution('terminal') as result:
        result.update(executed=True, outcome='error')
    span, = exporter.get_finished_spans()
    assert span.attributes['gen_ai.operation.name'] == 'execute_tool'
    assert span.attributes['nagent.tool.outcome'] == 'failure'
    assert span.events[-1].name == 'nagent.tool.completed'
    assert all(not key.startswith('nagent.') or len(key.split('.')) == 3 for key in span.attributes)
    provider.shutdown()

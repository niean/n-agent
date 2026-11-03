"""Public projections use the Application contract across real SDK signals."""
import logging
import pytest

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.application import telemetry_semantics as ts
from app.application.telemetry_contract import CONTENT_ATTRIBUTE_KEYS, EXECUTION_ATTRIBUTE_KEYS, project_execution_attributes, completion_event
from app.infrastructure.observability import logging as obs


def test_real_sdk_completion_has_one_event_and_canonical_log(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(ts, 'get_tracer', lambda: provider.get_tracer('projection-test'))
    obs.configure_execution_context(ts.execution_log_snapshot, project_context=project_execution_attributes,
                                    completion_event=completion_event, execution_keys=EXECUTION_ATTRIBUTE_KEYS, content_keys=CONTENT_ATTRIBUTE_KEYS)
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
    logs = InMemoryLogRecordExporter()
    log_provider = LoggerProvider()
    log_provider.add_log_record_processor(SimpleLogRecordProcessor(logs))
    with pytest.warns(DeprecationWarning, match="LoggingHandler.*deprecated"):
        sdk_handler = LoggingHandler(logger_provider=log_provider)
    captured = []
    class Capture(logging.Handler):
        def emit(self, record):
            captured.append(record)
            sdk_handler.handle(record)
    bridge = obs.SafeLogBridge(otel_handler=Capture())
    logger = logging.getLogger(ts.__name__)
    previous = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(bridge)
    try:
        with ts.SemanticsTelemetry().agent_run(session_id='session-1', run_id='run-1', source='cli'):
            pass
        span = exporter.get_finished_spans()[0]
        assert len([e for e in span.events if e.name == 'nagent.agent.completed']) == 1
        assert len(captured) == 1
        record = captured[0]
        assert record.event == 'nagent.agent.completed'
        assert getattr(record, 'nagent.agent.outcome') == span.attributes['nagent.agent.outcome'] == 'success'
        assert getattr(record, 'gen_ai.conversation.id') == 'session-1'
        exported_log, = logs.get_finished_logs()
        sdk_log = exported_log.log_record
        assert sdk_log.trace_id == span.context.trace_id
        assert sdk_log.span_id == span.context.span_id
        for key, value in span.events[0].attributes.items():
            assert sdk_log.attributes[key] == span.attributes[key] == value
        assert not any(k in vars(record) for k in ('operation', 'outcome', 'summary', 'session_id', 'source'))
    finally:
        logger.removeHandler(bridge)
        logger.setLevel(previous)
        obs.configure_execution_context(None)
        provider.shutdown()
        log_provider.shutdown()


def test_model_completion_preserves_finish_reason_array(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(ts, 'get_tracer', lambda: provider.get_tracer('model-types'))
    with ts.ChatAttempt(model='gpt-4o-mini', stream=False) as attempt:
        attempt.set_finish_reasons('stop')
    span, = exporter.get_finished_spans()
    assert span.attributes['gen_ai.response.finish_reasons'] == ('stop',)
    assert span.events[-1].attributes['gen_ai.response.finish_reasons'] == ('stop',)
    provider.shutdown()



def test_uninjected_outlet_keeps_trusted_correlation_without_legacy_aliases():
    obs.configure_execution_context(lambda: {'operation': 'invoke_agent', 'summary': 'unsafe', 'source': 'api'})
    with obs.bound_log_session('server-owned'):
        record = logging.LogRecord('third.party', logging.WARNING, __file__, 1, 'diagnostic', (), None)
        record.source = 'spoofed'
        record._completion_event_emitted = True
        projected = obs.project_record(record)
    assert getattr(projected, 'gen_ai.conversation.id') == 'server-owned'
    assert not any(k in vars(projected) for k in ('summary', 'source', 'session_id', '_completion_event_emitted'))


@pytest.mark.asyncio
async def test_policy_sink_owns_all_canonical_audit_attributes(monkeypatch):
    from app.domain.policy import PolicyAuditEvent, PolicyDecisionKind, PolicyOutcome
    from app.infrastructure.policy.logging_sink import LoggingPolicyAuditSink
    monkeypatch.setattr(obs, '_sanitizer', obs.LogSanitizer())
    captured = []
    class Capture(logging.Handler):
        def emit(self, record):
            captured.append(obs.project_record(record))
    logger = logging.getLogger('app.infrastructure.policy.logging_sink')
    handler = Capture()
    monkeypatch.setattr(logger, 'level', logging.INFO)
    logger.addHandler(handler)
    from tests.infrastructure.observability._contract import configure
    configure(lambda: {'operation': 'invoke_agent', 'agent_run_id': 'agent-run', 'session_id': 'session'})
    try:
        await LoggingPolicyAuditSink().record(PolicyAuditEvent('tool', 'v1', PolicyDecisionKind.ADMISSION,
            'bounded reason', 'browser-session-not-agent-run', 'budget-ledger-not-conversation', 'system', PolicyOutcome.DENY, '2026-11-03T00:00:00Z'))
    finally:
        logger.removeHandler(handler)
    record, = captured
    assert record.event == 'nagent.policy.decided'
    expected = {'nagent.policy.name': 'tool', 'nagent.policy.version': 'v1',
        'nagent.policy.decision_kind': 'admission', 'nagent.policy.reason': 'bounded reason',
        'nagent.policy.outcome': 'deny', 'nagent.policy.scope': 'system',
        'nagent.policy.occurred_at': '2026-11-03T00:00:00Z',
        'nagent.agent.run_id': 'agent-run', 'gen_ai.conversation.id': 'session'}
    for key, value in expected.items():
        assert getattr(record, key) == value
    assert not any(key.startswith('policy.') for key in vars(record))


def test_collector_projection_matches_exact_application_schema():
    import re
    from pathlib import Path
    import yaml
    from app.application.telemetry_contract import CONTENT_ATTRIBUTE_KEYS
    config = yaml.safe_load((Path(__file__).resolve().parents[3] / 'docker/observability/otel-collector.yaml').read_text())
    statements = config['processors']['transform/loki_json']['log_statements'][0]['statements']
    nagent = [s for s in statements if 'service.name"] == "n-kb"' not in s]
    execution = {match[1] for statement in nagent
                 if (match := re.search(r'cache\["execution"\]\["([^"]+)"\]', statement))}
    standard_http = {'http.request.method', 'http.route', 'http.response.status_code',
                     'server.address', 'server.port', 'url.template'}
    assert execution == (EXECUTION_ATTRIBUTE_KEYS - CONTENT_ATTRIBUTE_KEYS) | standard_http
    content = {key for statement in nagent for key in re.findall(r'attributes\["(nagent\.content\.[^"]+)"\]', statement)}
    assert content == CONTENT_ATTRIBUTE_KEYS
    assert 'set(attributes, {}) where resource.attributes["service.name"] == "n-agent"' in statements
    assert not any('IsMatch' in s or 'keep_matching_keys' in s for s in nagent)


def test_budget_decision_log_uses_trusted_kind_and_canonical_event(monkeypatch):
    from app.main import _configure_execution_logging
    _configure_execution_logging()
    monkeypatch.setattr(ts, '_record', lambda *args: None)
    captured = []
    class Capture(logging.Handler):
        def emit(self, record):
            captured.append(obs.project_record(record))
    handler = Capture()
    logger = logging.getLogger(ts.__name__)
    logger.addHandler(handler)
    try:
        ts.SemanticsTelemetry().record_budget_denied('budget_exhausted')
    finally:
        logger.removeHandler(handler)
    record, = captured
    assert record.event == 'nagent.budget.denied'
    assert getattr(record, 'nagent.budget.kind') == 'budget_exhausted'
    assert not hasattr(record, 'kind')

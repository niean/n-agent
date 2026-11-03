"""Execution context survives both log outlets without trusting record extras."""
from tests.infrastructure.observability._contract import configure

import json
import logging
import sys

import pytest

from app.infrastructure.observability import logging as obs


@pytest.fixture(autouse=True)
def empty_context():
    if hasattr(obs, "configure_execution_context"):
        configure(None)
    yield
    if hasattr(obs, "configure_execution_context"):
        configure(None)


def record():
    rec = logging.LogRecord("app.test", logging.INFO, __file__, 1,
                            "untrusted %s", ("secret-content",), None)
    rec.session_id = "spoofed-session"
    rec.summary = "secret-summary"
    setattr(rec, "nagent.agent.summary", "spoofed-canonical-summary")
    return rec


def test_same_safe_fields_in_stderr_and_otlp_projection():
    assert callable(getattr(obs, "configure_execution_context", None))
    expected = {"session_id": "s1", "request_id": "r1", "source": "dashboard",
                "operation": "invoke_agent", "summary": "Agent 完成", "duration_ms": 12.5,
                "inference_calls": 2, "tool_calls": 1, "usage_known": False}
    configure(lambda expected=dict(expected): dict(expected, agent_source="dashboard", agent_run_id="r1", arbitrary="secret-extra"))
    from app.application.telemetry_contract import project_execution_attributes
    expected = project_execution_attributes(dict(expected, agent_source="dashboard", agent_run_id="r1"))
    original = record()
    payload = json.loads(obs.SafeEventJsonFormatter().format(original))
    projected = obs.project_record(original)
    for key, value in expected.items():
        assert payload[key] == value
        assert getattr(projected, key) == value
    assert "arbitrary" not in payload and not hasattr(projected, "arbitrary")
    assert original.summary == "secret-summary" and original.args == ("secret-content",)
    assert "secret" not in json.dumps(payload)
    assert projected.msg == obs.UNKNOWN_EVENT_CODE


def test_untrusted_extra_does_not_create_summary():
    payload = json.loads(obs.SafeEventJsonFormatter().format(record()))
    projected = obs.project_record(record())
    assert "summary" not in payload and not hasattr(projected, "summary")
    assert "nagent.agent.summary" not in payload and not hasattr(projected, "nagent.agent.summary")
    assert payload.get("gen_ai.conversation.id") != "spoofed-session"


def test_callback_failure_is_safe_and_wrong_types_are_rejected():
    assert callable(getattr(obs, "configure_execution_context", None))
    def broken():
        raise RuntimeError("secret")
    configure(broken)
    assert obs.project_record(record()).msg == obs.UNKNOWN_EVENT_CODE
    assert "secret" not in obs.SafeEventJsonFormatter().format(record())
    configure(lambda: {"operation": "invoke_agent", "summary": {"secret": "x"}, "duration_ms": float("nan"),
                       "inference_calls": True, "agent_run_id": ["secret"]})
    payload = json.loads(obs.SafeEventJsonFormatter().format(record()))
    projected = obs.project_record(record())
    keys = ("nagent.agent.summary", "nagent.agent.duration_ms", "nagent.agent.inference_calls", "nagent.agent.run_id")
    assert not any(key in payload or hasattr(projected, key) for key in keys)


def test_real_completed_event_matches_span_and_restores_context(monkeypatch):
    from app.application import telemetry_semantics as ts
    from tests.application.test_telemetry_semantics import FakeTracer
    assert callable(getattr(obs, "configure_execution_context", None))
    tracer = FakeTracer()
    monkeypatch.setattr(ts, "get_tracer", lambda: tracer)
    configure(ts.execution_log_snapshot)
    obs.register_safe_event(ts.EXECUTION_LOG_TEMPLATE, "execution_completed")
    records = []
    class Capture(logging.Handler):
        def emit(self, rec):
            records.append((json.loads(obs.SafeEventJsonFormatter().format(rec)), obs.project_record(rec)))
    logger = logging.getLogger(ts.__name__)
    handler = Capture()
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        with ts.SemanticsTelemetry().agent_run(session_id="s1", run_id="r1", source="dashboard"):
            pass
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    completed = [(p, r) for p, r in records if p["event"] == "nagent.agent.completed"]
    assert len(completed) == 1
    payload, projected = completed[0]
    span = next(s for s in tracer.spans if s.name.startswith("invoke_agent"))
    assert payload["nagent.agent.summary"] == span.attributes["nagent.agent.summary"] == getattr(projected, "nagent.agent.summary")
    assert payload["gen_ai.conversation.id"] == "s1" and payload["nagent.agent.run_id"] == "r1"
    assert ts.execution_log_snapshot() == {}


# ---------------------------------------------------------------------------
# T2 (spec-261027): 正文/异常恢复 + 固定脱敏 (LogSanitizer 注入)
# ---------------------------------------------------------------------------


@pytest.fixture
def ready_sanitizer():
    """Install a shared LogSanitizer with a known secret, restore afterwards."""
    sanitizer = obs.LogSanitizer()
    sanitizer.configure_secret_catalog({_BODY_SECRET})
    obs.prepare_sanitizer(sanitizer)
    yield sanitizer
    obs.prepare_sanitizer(None)


_BODY_SECRET = "E2EOTELVERIFY-7f3a-secret"


def _exc_record():
    """LogRecord with a real traceback and stack_info, body containing secret."""
    try:
        raise RuntimeError(f"boom {_BODY_SECRET}")
    except RuntimeError:
        exc_info = sys.exc_info()
    rec = logging.LogRecord(
        "app.probe", logging.ERROR, __file__, 1,
        "probe %s <end>", (_BODY_SECRET,), exc_info,
    )
    rec.stack_info = "  stack line: %s" % _BODY_SECRET
    obs.register_safe_event("probe %s <end>", "t_probe")
    return rec


def test_formatter_restores_sanitized_body_and_exception(ready_sanitizer):
    rec = _exc_record()
    payload = json.loads(obs.SafeEventJsonFormatter().format(rec))
    assert payload["event"] == "t_probe"
    assert payload["message"] == "probe [REDACTED] <end>"
    assert _BODY_SECRET not in json.dumps(payload, ensure_ascii=False)
    assert payload["exception"]["type"] == "RuntimeError"
    assert _BODY_SECRET not in payload["exception"]["message"]
    assert "[REDACTED]" in payload["exception"]["stacktrace"]
    assert "[REDACTED]" in payload["stack"]
    assert rec.args == (_BODY_SECRET,)  # original untouched


def test_project_record_drops_exc_and_carries_sanitized_text(ready_sanitizer):
    rec = _exc_record()
    projected = obs.project_record(rec)
    assert projected.msg == "probe [REDACTED] <end>"
    assert projected.event == "t_probe"
    assert projected.args == ()
    assert projected.exc_info is None
    assert projected.exc_text is None
    assert projected.stack_info is None
    # Flat dotted OTel log attributes (the SDK dumps vars(record)).
    assert getattr(projected, "exception.type") == "RuntimeError"
    assert "[REDACTED]" in getattr(projected, "exception.stacktrace")
    assert getattr(projected, "stack") == "  stack line: [REDACTED]"
    assert _BODY_SECRET not in repr(projected)
    assert rec.args == (_BODY_SECRET,) and rec.exc_info is not None


def test_uninitialized_sanitizer_no_restored_text():
    # No prepare_sanitizer: body/exception stay collapsed to the safe event.
    obs.prepare_sanitizer(None)
    rec = _exc_record()
    payload = json.loads(obs.SafeEventJsonFormatter().format(rec))
    assert "message" not in payload and "exception" not in payload
    assert _BODY_SECRET not in json.dumps(payload, ensure_ascii=False)
    obs.prepare_sanitizer(None)


def test_approved_details_are_identical_in_all_log_outlets(monkeypatch, ready_sanitizer):
    from app.application.telemetry_details import configure_details, project_details, validate_details, flatten_details
    configure_details(ready_sanitizer.sanitize)
    details = project_details(user_message='hello ' + _BODY_SECRET, tool_arguments={'password': 'hidden', 'ok': 'value'})
    calls = []
    def snapshot():
        calls.append(True)
        return {'summary': 'done', 'details': details}
    configure(snapshot, validate_details=validate_details, flatten_details=flatten_details)
    rec = record()
    rec.details = {'user_message': {'content': 'spoofed'}}
    payload = json.loads(obs.SafeEventJsonFormatter().format(rec))
    assert payload['details'] == details
    assert _BODY_SECRET not in json.dumps(payload) and 'hidden' not in json.dumps(payload)
    received = []
    class Sink(logging.Handler):
        def emit(self, record):
            received.append(record)
    class Span:
        def is_recording(self):
            return True
        def add_event(self, **kwargs):
            self.event = kwargs
    span = Span()
    monkeypatch.setattr(obs.trace, 'get_current_span', lambda: span)
    calls.clear()
    obs.SafeLogBridge(otel_handler=Sink()).emit(rec)
    assert len(calls) == 1
    for key, value in flatten_details(details).items():
        assert getattr(received[0], key) == value == span.event['attributes'][key]
    configure_details(None)


def test_untrusted_details_and_unwired_validator_fail_closed():
    rec = record()
    rec.details = {'user_message': {'content': 'secret'}}
    configure(lambda: {'details': rec.details})
    payload = json.loads(obs.SafeEventJsonFormatter().format(rec))
    assert 'details' not in payload
    assert not any(k.startswith('nagent.content.') for k in vars(obs.project_record(rec)))


@pytest.mark.parametrize('snapshot,key', [
    ({'summary': True}, 'nagent.agent.summary'),
    ({'summary': ['not a summary']}, 'nagent.agent.summary'),
    ({'duration_ms': True}, 'nagent.agent.duration_ms'),
    ({'duration_ms': '12'}, 'nagent.agent.duration_ms'),
    ({'inference_calls': True}, 'nagent.agent.inference_calls'),
    ({'inference_calls': 2.5}, 'nagent.agent.inference_calls'),
    ({'agent_run_id': ['not an ID']}, 'nagent.agent.run_id'),
    ({'agent_run_id': 12}, 'nagent.agent.run_id'),
    ({'usage_known': 1}, 'nagent.agent.usage_known'),
    ({'outcome': False}, 'nagent.agent.outcome'),
    ({'nagent.agent.outcome': False}, 'nagent.agent.outcome'),
    ({'session_id': ['not a conversation']}, 'gen_ai.conversation.id'),
])
def test_canonical_attribute_types_are_enforced_by_the_single_projector(snapshot, key):
    from app.application.telemetry_contract import project_execution_attributes
    facts = {'operation': 'invoke_agent', **snapshot}
    assert key not in project_execution_attributes(facts)
    configure(lambda: facts)
    assert key not in json.loads(obs.SafeEventJsonFormatter().format(record()))
    assert not hasattr(obs.project_record(record()), key)

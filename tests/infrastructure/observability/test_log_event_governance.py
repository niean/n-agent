"""Useful events survive both safe outlets; contextual placeholders do not."""
import json
import logging

import pytest

from app.infrastructure.observability import logging as obs


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.mark.parametrize("source", ["httpx", "Lark", "n_agent_plugins.opa_client"])
def test_context_alone_does_not_keep_unknown_info(source, monkeypatch):
    monkeypatch.setattr(obs, "_execution_context", lambda: {"operation": "chat", "model": "model"})
    target = Capture()
    bridge = obs.SafeLogBridge(otel_handler=target)
    record = logging.LogRecord(source, logging.INFO, __file__, 1, "unregistered %s", ("secret",), None,
                               func={"Lark": "_connect", "n_agent_plugins.opa_client": "register"}.get(source))
    bridge.handle(record)
    assert not target.records
    assert not obs.SafeLogFilter().filter(record)
    assert bridge.emit_failure_count == 0


@pytest.mark.parametrize("level", [logging.WARNING, logging.ERROR, logging.CRITICAL])
def test_unknown_diagnostics_keep_source_and_severity(level, monkeypatch):
    monkeypatch.setattr(obs, "_sanitizer", obs.LogSanitizer())
    target = Capture()
    record = logging.LogRecord("httpx", level, __file__, 1, "untrusted secret", (), None)
    obs.SafeLogBridge(otel_handler=target).handle(record)
    assert len(target.records) == 1
    assert target.records[0].name == "httpx"
    assert target.records[0].levelno == level
    assert target.records[0].msg == "untrusted secret"
    assert target.records[0].event == "diagnostic_log"
    assert obs.SafeLogFilter().filter(record)


@pytest.mark.asyncio
async def test_policy_decision_has_fixed_event_and_sanitized_structured_facts(monkeypatch):
    from app.domain.policy import PolicyAuditEvent, PolicyDecisionKind, PolicyOutcome
    from app.infrastructure.policy.logging_sink import LoggingPolicyAuditSink

    sanitizer = obs.LogSanitizer()
    sanitizer.configure_secret_catalog({"sentinel-private-value"})
    monkeypatch.setattr(obs, "_sanitizer", sanitizer)
    emitted = []

    class SafeCapture(logging.Handler):
        def emit(self, record):
            emitted.append((record.msg, json.loads(obs.SafeEventJsonFormatter().format(record)), obs.project_record(record)))

    logger = logging.getLogger("app.infrastructure.policy.logging_sink")
    monkeypatch.setattr(logger, "level", logging.INFO)
    handler = SafeCapture()
    logger.addHandler(handler)
    try:
        await LoggingPolicyAuditSink().record(PolicyAuditEvent(
            policy="tool", version="v1", decision_kind=PolicyDecisionKind.ADMISSION,
            reason="blocked sentinel-private-value", outcome=PolicyOutcome.DENY,
            run_id="r1", session_id="s1"))
    finally:
        logger.removeHandler(handler)
    assert len(emitted) == 1
    template, payload, projected = emitted[0]
    assert template == "policy_decision %s"
    assert payload["event"] == "policy_decision"
    assert payload["policy.name"] == projected.__dict__["policy.name"] == "tool"
    assert payload["policy.outcome"] == "deny"
    assert payload["policy.run_id"] == "r1"
    assert payload["policy.session_id"] == "s1"
    assert payload["policy.reason"] == "blocked [REDACTED]"
    assert "sentinel-private-value" not in json.dumps(payload)
    assert "sentinel-private-value" not in str(projected.__dict__)
    after = logging.LogRecord("app.other", logging.WARNING, __file__, 1, "other", (), None)
    assert "policy.name" not in json.loads(obs.SafeEventJsonFormatter().format(after))


def test_lifecycle_templates_are_registered():
    from app.main import _configure_logging
    _configure_logging()
    cases = {
        "TaskRunner started (interval=%ss)": "task_runner_started",
        "TaskRunner stopped": "task_runner_stopped",
        "artifact backfill ok processed=%s created=%s skipped=%s failed=%s": "artifact_backfill_completed",
        "artifact revision migration ok processed=%s migrated=%s skipped=%s failed=%s": "artifact_revision_migration_completed",
        "artifact backfill failed": "artifact_backfill_failed",
    }
    for template, event in cases.items():
        record = logging.LogRecord("app.main", logging.INFO, __file__, 1, template, (), None)
        assert obs.event_code_for(record) == event

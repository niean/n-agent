from __future__ import annotations

import json
import logging

import pytest

from app.application.policy_audit_service import PolicyAuditService
from app.domain.policy import (
    PolicyAuditEvent,
    PolicyAuditSink,
    PolicyDecisionKind,
    PolicyOutcome,
)
from app.infrastructure.policy.logging_sink import LoggingPolicyAuditSink


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[PolicyAuditEvent] = []

    async def record(self, event: PolicyAuditEvent) -> None:
        self.events.append(event)


class FailingSink:
    async def record(self, event: PolicyAuditEvent) -> None:
        raise RuntimeError("sink crashed")


# -- outcome rules --

def test_admission_event_carries_outcome():
    event = PolicyAuditEvent(
        policy="admission",
        version="system-v1",
        decision_kind=PolicyDecisionKind.ADMISSION,
        reason="allowed",
        run_id="run-1",
        session_id="s1",
        outcome=PolicyOutcome.ALLOW,
    )
    assert event.outcome is PolicyOutcome.ALLOW


def test_non_admission_events_have_none_outcome():
    for kind in (
        PolicyDecisionKind.PLAN,
        PolicyDecisionKind.SELECTION,
        PolicyDecisionKind.ALLOCATION,
    ):
        event = PolicyAuditEvent(
            policy="context",
            version="system-v1",
            decision_kind=kind,
            reason="evaluated",
            run_id="run-1",
            session_id="s1",
        )
        assert event.outcome is None


# -- PolicyAuditService delegation --

async def test_service_delegates_to_sink():
    sink = RecordingSink()
    service = PolicyAuditService(sink)

    event = PolicyAuditEvent(
        policy="tool",
        version="tool-v1",
        decision_kind=PolicyDecisionKind.ADMISSION,
        reason="safe_tool",
        run_id="run-1",
        session_id="s1",
        outcome=PolicyOutcome.ALLOW,
    )
    await service.record(event)

    assert sink.events == [event]


async def test_service_catches_sink_exception_and_logs_warning(caplog):
    service = PolicyAuditService(FailingSink())

    event = PolicyAuditEvent(
        policy="tool",
        version="tool-v1",
        decision_kind=PolicyDecisionKind.ADMISSION,
        reason="safe_tool",
        run_id="run-1",
        session_id="s1",
    )

    with caplog.at_level(logging.WARNING):
        await service.record(event)

    # Did not re-raise; warning was logged
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) >= 1
    assert "policy audit sink failed" in warning_records[0].getMessage()


# -- LoggingPolicyAuditSink --

async def test_logging_sink_outputs_only_allowed_fields(caplog):
    sink = LoggingPolicyAuditSink()

    event = PolicyAuditEvent(
        policy="tool",
        version="tool-v1",
        decision_kind=PolicyDecisionKind.ADMISSION,
        reason="safe_tool",
        run_id="run-1",
        session_id="s1",
        policy_scope="system",
        outcome=PolicyOutcome.ALLOW,
        occurred_at="2026-07-14T00:00:00Z",
    )

    with caplog.at_level(logging.INFO):
        await sink.record(event)

    log_records = [
        r for r in caplog.records
        if r.name == "app.infrastructure.policy.logging_sink"
    ]
    assert len(log_records) == 1

    payload = json.loads(log_records[0].message)

    expected_keys = {
        "policy", "version", "decision_kind", "reason", "outcome",
        "run_id", "session_id", "policy_scope", "occurred_at",
    }
    assert set(payload.keys()) == expected_keys

    assert payload["policy"] == "tool"
    assert payload["version"] == "tool-v1"
    assert payload["decision_kind"] == "admission"
    assert payload["reason"] == "safe_tool"
    assert payload["outcome"] == "allow"
    assert payload["run_id"] == "run-1"
    assert payload["session_id"] == "s1"
    assert payload["policy_scope"] == "system"
    assert payload["occurred_at"] == "2026-07-14T00:00:00Z"


async def test_logging_sink_does_not_leak_sensitive_fields(caplog):
    sink = LoggingPolicyAuditSink()

    event = PolicyAuditEvent(
        policy="tool",
        version="tool-v1",
        decision_kind=PolicyDecisionKind.ADMISSION,
        reason="safe_tool",
        run_id="run-1",
        session_id="s1",
        outcome=PolicyOutcome.ALLOW,
    )

    with caplog.at_level(logging.INFO):
        await sink.record(event)

    log_records = [
        r for r in caplog.records
        if r.name == "app.infrastructure.policy.logging_sink"
    ]
    assert len(log_records) == 1

    raw_line = log_records[0].message
    payload = json.loads(raw_line)

    forbidden_keys = (
        "prompt", "secret", "api_key", "tool_arguments",
        "trusted_claims", "password", "token", "credentials",
    )
    for key in forbidden_keys:
        assert key not in payload, f"forbidden key '{key}' found in sink output"
        assert key not in raw_line, f"forbidden key '{key}' found in raw log line"


async def test_logging_sink_serializes_none_outcome_for_non_admission(caplog):
    sink = LoggingPolicyAuditSink()

    event = PolicyAuditEvent(
        policy="context",
        version="system-v1",
        decision_kind=PolicyDecisionKind.PLAN,
        reason="compression_not_required",
        run_id="run-1",
        session_id="s1",
    )

    with caplog.at_level(logging.INFO):
        await sink.record(event)

    log_records = [
        r for r in caplog.records
        if r.name == "app.infrastructure.policy.logging_sink"
    ]
    assert len(log_records) == 1

    payload = json.loads(log_records[0].message)
    assert payload["outcome"] is None
    assert payload["decision_kind"] == "plan"


def test_logging_sink_conforms_to_policy_audit_sink_protocol():
    sink: PolicyAuditSink = LoggingPolicyAuditSink()
    assert hasattr(sink, "record")

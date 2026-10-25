"""Tests for OtelLinkConfigLoggingSink -- structured JSON audit logging."""
import json
import logging

import pytest

from app.domain.otel_link_config import (
    OtelLinkConfigAuditEvent,
    build_otel_link_field_state,
)
from app.infrastructure.policy.otel_link_config_logging_sink import (
    OtelLinkConfigLoggingSink,
)


def _make_event() -> OtelLinkConfigAuditEvent:
    before = build_otel_link_field_state(None, "")
    after = build_otel_link_field_state(
        "http://some-host:3000/d/x?token=SECRET",
        "http://some-host:3000/d/x?token=SECRET",
    )
    return OtelLinkConfigAuditEvent(
        actor="tester",
        updated_at="2026-10-27T00:00:00+00:00",
        old_version=1,
        new_version=2,
        changed_fields={"metric_base_url": (before, after)},
    )


def test_implements_audit_sink_port():
    # OtelLinkConfigAuditSink is a non-runtime_checkable Protocol; assert the
    # structural contract explicitly instead of isinstance.
    assert callable(OtelLinkConfigLoggingSink().record)


@pytest.mark.asyncio
async def test_record_emits_single_structured_json_log(caplog):
    sink = OtelLinkConfigLoggingSink()
    with caplog.at_level(logging.INFO):
        await sink.record(_make_event())

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.name == "app.infrastructure.policy.otel_link_config_logging_sink"
    payload = json.loads(record.getMessage())
    assert payload["event"] == "otel_link_config_changed"
    assert payload["actor"] == "tester"
    assert payload["updated_at"] == "2026-10-27T00:00:00+00:00"
    assert payload["old_version"] == 1
    assert payload["new_version"] == 2
    field = payload["changed_fields"]["metric_base_url"]
    assert field["before"] == {"override": "env", "url": ""}
    assert field["after"] == {"override": "custom", "url": "http://some-host:3000"}


@pytest.mark.asyncio
async def test_record_does_not_leak_secrets_or_paths(caplog):
    sink = OtelLinkConfigLoggingSink()
    with caplog.at_level(logging.INFO):
        await sink.record(_make_event())

    text = caplog.text
    assert "SECRET" not in text
    assert "/d/x" not in text
    assert "some-host:3000" in text

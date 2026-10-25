"""OtelLinkConfigLoggingSink -- best-effort structured JSON audit log for OTel link config changes.

Implements OtelLinkConfigAuditSink. Mirrors TaskConfigLoggingSink's pattern: async,
best-effort, logs a structured JSON line. changed_fields carry redacted
OtelLinkConfigFieldState projections (scheme://host[:port] only) built by the
Domain; the sink passes them through and never logs raw URLs.
Failure to log must not roll back a committed config (the service catches).
"""
from __future__ import annotations

import json
import logging

from app.domain.otel_link_config import (
    OtelLinkConfigAuditEvent,
    OtelLinkConfigAuditSink,
    OtelLinkConfigFieldState,
)

logger = logging.getLogger(__name__)


def _serialize_state(state: OtelLinkConfigFieldState) -> dict[str, str]:
    return {"override": state.override, "url": state.url}


class OtelLinkConfigLoggingSink(OtelLinkConfigAuditSink):
    """Logs OtelLinkConfigAuditEvent as a structured JSON line."""

    async def record(self, event: OtelLinkConfigAuditEvent) -> None:
        payload = {
            "event": "otel_link_config_changed",
            "actor": event.actor,
            "updated_at": event.updated_at,
            "old_version": event.old_version,
            "new_version": event.new_version,
            "changed_fields": {
                f: {"before": _serialize_state(before), "after": _serialize_state(after)}
                for f, (before, after) in event.changed_fields.items()
            },
        }
        logger.info(json.dumps(payload, ensure_ascii=False))

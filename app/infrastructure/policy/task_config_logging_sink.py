"""TaskConfigLoggingSink -- best-effort structured JSON audit log for config changes.

Implements TaskConfigAuditSink. Mirrors LoggingPolicyAuditSink's pattern: async,
best-effort, logs a structured JSON line with no secrets (C-class values only).
Failure to log must not roll back a committed config (the service catches).
"""
from __future__ import annotations

import json
import logging

from app.domain.task_config import TaskConfigAuditEvent, TaskConfigAuditSink

logger = logging.getLogger(__name__)


class TaskConfigLoggingSink(TaskConfigAuditSink):
    """Logs TaskConfigAuditEvent as a structured JSON line."""

    async def record(self, event: TaskConfigAuditEvent) -> None:
        payload = {
            "event": "task_config_changed",
            "actor": event.actor,
            "updated_at": event.updated_at,
            "old_version": event.old_version,
            "new_version": event.new_version,
            "changed_fields": {
                f: {"before": before, "after": after}
                for f, (before, after) in event.changed_fields.items()
            },
        }
        logger.info(json.dumps(payload, ensure_ascii=False))

# app/infrastructure/policy/logging_sink.py
from __future__ import annotations

import json
import logging

from app.domain.policy import PolicyAuditEvent, PolicyAuditSink
from app.infrastructure.observability.logging import policy_audit_context, register_safe_event

logger = logging.getLogger(__name__)
POLICY_DECISION_LOG_TEMPLATE = "policy_decision %s"


class LoggingPolicyAuditSink:
    """Infrastructure PolicyAuditSink that emits structured JSON log lines.

    Serializes ONLY PolicyAuditEvent fields.  PolicyAuditEvent has no fields
    for raw prompt, secret, tool arguments, or trusted claims by design --
    so the sink cannot leak them even if an attacker constructs an event.
    """

    async def record(self, event: PolicyAuditEvent) -> None:
        payload = {
            "policy": event.policy,
            "version": event.version,
            "decision_kind": event.decision_kind.value,
            "reason": event.reason,
            "outcome": event.outcome.value if event.outcome is not None else None,
            "run_id": event.run_id,
            "session_id": event.session_id,
            "policy_scope": event.policy_scope,
            "occurred_at": event.occurred_at,
        }
        register_safe_event(POLICY_DECISION_LOG_TEMPLATE, "policy_decision")
        with policy_audit_context(payload):
            logger.info(POLICY_DECISION_LOG_TEMPLATE, json.dumps(payload, ensure_ascii=False))

# app/application/policy_audit_service.py
from __future__ import annotations

import logging

from app.domain.policy import PolicyAuditEvent, PolicyAuditSink

logger = logging.getLogger(__name__)


class PolicyAuditService:
    """Application-level audit service that delegates to a PolicyAuditSink.

    Audit is a side-channel: if the sink raises, the service logs a warning
    and swallows the exception.  A sink failure MUST NOT change any business
    decision or propagate to the caller.
    """

    def __init__(self, sink: PolicyAuditSink) -> None:
        self._sink = sink

    async def record(self, event: PolicyAuditEvent) -> None:
        try:
            await self._sink.record(event)
        except Exception:
            logger.warning(
                "policy audit sink failed for policy=%s run_id=%s session_id=%s",
                event.policy,
                event.run_id,
                event.session_id,
                exc_info=True,
            )

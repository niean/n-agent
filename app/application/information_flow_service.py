"""Information flow application service.

Holds the InformationFlowPolicy + config + secret catalog and provides:
- ``release``: non-stream release with transform applied.
- ``redact_structured``: structured (field-by-field) redaction for tool events.
- ``create_stream_guard``: incremental stream transform guard.

The service is the single facade through which all T3-scoped paths
(LLM payload logging, Usage retention, stream events, non-stream reply)
route content before it reaches logs, Usage records, SSE chunks, or clients.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.application.policy_snapshot import InformationFlowPolicyConfig
from app.domain.information_flow import (
    Classification,
    InformationAsset,
    InformationFlowConfig,
    InformationFlowError,
    InformationReleaseDecision,
    ReleaseTarget,
    SecretCatalog,
    redact_secret_values,
    redact_structured,
)
from app.domain.information_flow_policy import InformationFlowPolicy
from app.domain.policy import PolicyAuditEvent, PolicyDecisionKind, PolicyOutcome

if TYPE_CHECKING:
    from app.application.policy_audit_service import PolicyAuditService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReleaseResult:
    """Application-level result of a release request.

    Attributes:
        allowed: whether the release was permitted.
        content: sanitized content if allowed, None if denied.
        error: stable error code if denied, None if allowed.
        decision: the domain decision for audit.
    """

    allowed: bool
    content: str | None
    error: str | None
    decision: InformationReleaseDecision


class InformationFlowService:
    """Application facade for information flow governance.

    Construct from an ``InformationFlowPolicyConfig`` (T2 snapshot) and a
    ``SecretCatalog``, or via ``from_settings`` for T3 wiring where a
    snapshot is not yet available at the call site.
    """

    def __init__(
        self,
        config: InformationFlowPolicyConfig,
        secrets: SecretCatalog,
        audit_service: "PolicyAuditService | None" = None,
    ) -> None:
        self._config = config
        self._secrets = secrets
        self._audit_service = audit_service
        self._policy = InformationFlowPolicy(
            config=InformationFlowConfig(
                log_llm_payloads=config.log_llm_payloads,
                store_usage_payloads=config.store_usage_payloads,
                redact_secrets=config.redact_secrets,
            ),
            secrets=secrets,
        )

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        audit_service: "PolicyAuditService | None" = None,
    ) -> InformationFlowService:
        """Construct from a Settings instance (T3 wiring convenience)."""
        config = InformationFlowPolicyConfig(
            log_llm_payloads=getattr(settings, "information_log_llm_payloads", False),
            store_usage_payloads=getattr(settings, "information_store_usage_payloads", True),
            redact_secrets=getattr(settings, "information_redact_secrets", True),
        )
        secret_values: set[str] = set()
        api_key = getattr(settings, "provider_api_key", "")
        if api_key:
            secret_values.add(api_key)
        return cls(config, SecretCatalog(secret_values=frozenset(secret_values)), audit_service=audit_service)

    @property
    def secrets(self) -> SecretCatalog:
        return self._secrets

    @property
    def config(self) -> InformationFlowPolicyConfig:
        return self._config

    def release(
        self,
        content: str,
        target: ReleaseTarget,
        *,
        classification: Classification = Classification.INTERNAL,
        origin: str = "unknown",
        labels: frozenset[str] = frozenset(),
        run_id: str = "",
        session_id: str = "",
    ) -> ReleaseResult:
        """Evaluate release for *content* to *target* and apply transform if needed.

        On ALLOW with transform, applies redaction and returns sanitized content.
        On DENY or transform failure, returns a stable error with content=None.
        """
        asset = InformationAsset(
            classification=classification,
            origin=origin,
            labels=labels,
            content=content,
        )
        decision = self._policy.evaluate(asset, target)
        self._audit_release(decision, target, origin, run_id, session_id)
        if decision.verdict is PolicyOutcome.ALLOW:
            if decision.transform == "redaction":
                try:
                    sanitized = redact_secret_values(content, self._secrets)
                    return ReleaseResult(
                        allowed=True,
                        content=sanitized,
                        error=None,
                        decision=decision,
                    )
                except Exception:
                    logger.warning(
                        "information_flow transform failed for target=%s origin=%s",
                        target.value,
                        origin,
                    )
                    return ReleaseResult(
                        allowed=False,
                        content=None,
                        error=InformationFlowError.STABLE_CODE,
                        decision=decision,
                    )
            return ReleaseResult(
                allowed=True,
                content=content,
                error=None,
                decision=decision,
            )
        return ReleaseResult(
            allowed=False,
            content=None,
            error=InformationFlowError.STABLE_CODE,
            decision=decision,
        )

    def _audit_release(
        self,
        decision: InformationReleaseDecision,
        target: ReleaseTarget,
        origin: str,
        run_id: str,
        session_id: str,
    ) -> None:
        """Fire-and-forget audit for a release decision.

        ``release`` is sync but always called from async contexts.  We use
        ``asyncio.create_task`` to schedule the async audit recording without
        blocking the caller.  If no event loop is running, the audit is
        skipped (best-effort).
        """
        if self._audit_service is None:
            return
        event = PolicyAuditEvent(
            policy="information-flow-policy",
            version="system-v1",
            decision_kind=PolicyDecisionKind.ADMISSION,
            reason=f"target={target.value} origin={origin} verdict={decision.verdict.value if decision.verdict else 'none'}",
            run_id=run_id,
            session_id=session_id,
            outcome=decision.verdict,
        )
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._audit_service.record(event))
        except RuntimeError:
            # No running event loop -- skip audit (best-effort)
            pass

    def redact_structured(self, data: Any) -> Any:
        """Apply structured (field-by-field) redaction to tool event data.

        This is a direct redaction -- it does not go through the Policy
        decision flow because tool event arguments are part of an already-
        permitted stream.  Used by ``_emit_stream_tool_event``.
        """
        if not self._config.redact_secrets:
            return data
        try:
            return redact_structured(data, self._secrets)
        except Exception:
            logger.warning("structured redaction failed, returning redacted placeholder")
            return "[REDACTED]"

    def create_stream_guard(self) -> InformationFlowStreamGuard:
        """Create a per-run stream guard for incremental secret redaction."""
        return InformationFlowStreamGuard(self._secrets, self._config)


class InformationFlowStreamGuard:
    """Incremental stream transform guard for streaming content.

    Keeps a lookbehind buffer of ``max_secret_length - 1`` characters to
    prevent a secret value split across two chunks from leaking.  Rules
    needing full-message context would buffer the whole message, but the
    current implementation only does exact-value redaction which is
    inherently incremental.

    On transform exception: stops yielding and raises ``InformationFlowError``
    so the caller can emit a stable error event.  The original text is never
    yielded because ``feed`` only emits *after* successful redaction.
    """

    def __init__(
        self,
        secrets: SecretCatalog,
        config: InformationFlowPolicyConfig,
    ) -> None:
        self._secrets = secrets
        self._config = config
        self._lookbehind_size = max(secrets.max_secret_length - 1, 0)
        self._buffer: str = ""

    def feed(self, chunk: str) -> str:
        """Feed a chunk and return the safe-to-emit portion.

        The chunk is appended to the internal buffer, all known secret
        values are redacted from the buffer, and everything except the
        last ``lookbehind_size`` characters is returned.  The held-back
        tail ensures that a secret spanning two chunks is caught.
        """
        self._buffer += chunk
        if self._config.redact_secrets:
            self._buffer = redact_secret_values(self._buffer, self._secrets)
        if len(self._buffer) > self._lookbehind_size:
            emit_size = len(self._buffer) - self._lookbehind_size
            result = self._buffer[:emit_size]
            self._buffer = self._buffer[emit_size:]
            return result
        return ""

    def flush(self) -> str:
        """Return any remaining buffer content (already redacted by last feed)."""
        result = self._buffer
        self._buffer = ""
        return result

    async def transform(self, chunks: Iterable[str]) -> AsyncIterator[str]:
        """Async stream transform: yields redacted content chunks.

        On any exception, raises ``InformationFlowError`` without yielding
        unredacted content.
        """
        try:
            for chunk in chunks:
                emitted = self.feed(chunk)
                if emitted:
                    yield emitted
            final = self.flush()
            if final:
                yield final
        except Exception:
            raise InformationFlowError()

    def transform_structured(self, data: Any) -> Any:
        """Field-by-field redaction for structured tool event data."""
        if not self._config.redact_secrets:
            return data
        try:
            return redact_structured(data, self._secrets)
        except Exception:
            return "[REDACTED]"

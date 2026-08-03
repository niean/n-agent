"""Information flow policy -- domain pure decision logic.

Decides whether an InformationAsset may be released to a ReleaseTarget,
based only on labels, target, config and provided secret values.
No IO, no LLM inference.
"""
from __future__ import annotations

from app.domain.information_flow import (
    Classification,
    InformationAsset,
    InformationFlowConfig,
    InformationReleaseDecision,
    ReleaseTarget,
    SecretCatalog,
)
from app.domain.policy import PolicyOutcome


class InformationFlowPolicy:
    """Domain policy that decides information release verdicts.

    The policy is constructed with an immutable config and secret catalog.
    ``evaluate`` is pure: it inspects the asset's labels, classification,
    content (for exact secret matching), the target boundary, and the config
    to produce an ``InformationReleaseDecision``.
    """

    def __init__(self, config: InformationFlowConfig, secrets: SecretCatalog) -> None:
        self._config = config
        self._secrets = secrets

    def evaluate(
        self,
        asset: InformationAsset,
        target: ReleaseTarget,
    ) -> InformationReleaseDecision:
        contains_secret = self._contains_secret_value(asset.content)
        has_secret_label = "secret" in asset.labels
        needs_redaction = contains_secret or has_secret_label

        # --- Target-specific gates ----------------------------------------

        if target is ReleaseTarget.LLM_PAYLOAD_LOG:
            # Default DENY; only ALLOW when config explicitly enables.
            if not self._config.log_llm_payloads:
                return InformationReleaseDecision(
                    verdict=PolicyOutcome.DENY,
                    transform=None,
                    allowed_fields=frozenset(),
                    retention="none",
                    audit_level="summary",
                    reason="llm_payload_log_disabled_by_config",
                )
            # When enabled, still redact secrets.
            if needs_redaction:
                if self._config.redact_secrets:
                    return InformationReleaseDecision(
                        verdict=PolicyOutcome.ALLOW,
                        transform="redaction",
                        allowed_fields=frozenset(),
                        retention="sanitized",
                        audit_level="summary",
                        reason="llm_payload_log_secret_redacted",
                    )
                return InformationReleaseDecision(
                    verdict=PolicyOutcome.DENY,
                    transform=None,
                    allowed_fields=frozenset(),
                    retention="none",
                    audit_level="summary",
                    reason="secret_no_transform_available",
                )
            return InformationReleaseDecision(
                verdict=PolicyOutcome.ALLOW,
                transform=None,
                allowed_fields=frozenset(),
                retention="raw",
                audit_level="full",
                reason="llm_payload_log_enabled_no_secrets",
            )

        if target is ReleaseTarget.USAGE_RETENTION:
            # Only sanitized payloads are retained.
            if not self._config.store_usage_payloads:
                return InformationReleaseDecision(
                    verdict=PolicyOutcome.DENY,
                    transform=None,
                    allowed_fields=frozenset(),
                    retention="none",
                    audit_level="summary",
                    reason="usage_storage_disabled_by_config",
                )
            if needs_redaction:
                if self._config.redact_secrets:
                    return InformationReleaseDecision(
                        verdict=PolicyOutcome.ALLOW,
                        transform="redaction",
                        allowed_fields=frozenset(),
                        retention="sanitized",
                        audit_level="summary",
                        reason="usage_retention_secret_redacted",
                    )
                return InformationReleaseDecision(
                    verdict=PolicyOutcome.DENY,
                    transform=None,
                    allowed_fields=frozenset(),
                    retention="none",
                    audit_level="summary",
                    reason="usage_retention_secret_denied",
                )
            return InformationReleaseDecision(
                verdict=PolicyOutcome.ALLOW,
                transform=None,
                allowed_fields=frozenset(),
                retention="sanitized",
                audit_level="summary",
                reason="usage_retention_no_secrets",
            )

        # --- PUBLIC_ARTIFACT (strictest boundary) -----------------------
        #
        # Public artifact release is for TEXT content only (binary publish
        # goes through ArtifactPolicy's classification gate).  SECRET and
        # SENSITIVE classifications are never released here, regardless of
        # content.  Known-secret text is released only through redaction.
        # This branch MUST NOT fall through to the generic default-allow.
        if target is ReleaseTarget.PUBLIC_ARTIFACT:
            if asset.classification in (Classification.SECRET, Classification.SENSITIVE):
                return InformationReleaseDecision(
                    verdict=PolicyOutcome.DENY,
                    transform=None,
                    allowed_fields=frozenset(),
                    retention="none",
                    audit_level="summary",
                    reason=f"public_artifact_{asset.classification.value}_classification_denied",
                )
            if needs_redaction:
                if self._config.redact_secrets:
                    return InformationReleaseDecision(
                        verdict=PolicyOutcome.ALLOW,
                        transform="redaction",
                        allowed_fields=frozenset(),
                        retention="sanitized",
                        audit_level="summary",
                        reason="public_artifact_secret_redacted",
                    )
                return InformationReleaseDecision(
                    verdict=PolicyOutcome.DENY,
                    transform=None,
                    allowed_fields=frozenset(),
                    retention="none",
                    audit_level="summary",
                    reason="public_artifact_secret_no_transform_available",
                )
            return InformationReleaseDecision(
                verdict=PolicyOutcome.ALLOW,
                transform=None,
                allowed_fields=frozenset(),
                retention="raw",
                audit_level="summary",
                reason=f"public_artifact_{asset.classification.value}_no_secrets",
            )

        # --- Generic targets (LLM_PROVIDER, CLIENT_RESPONSE, etc.) -------

        if needs_redaction:
            if self._config.redact_secrets:
                return InformationReleaseDecision(
                    verdict=PolicyOutcome.ALLOW,
                    transform="redaction",
                    allowed_fields=frozenset(),
                    retention="sanitized",
                    audit_level="summary",
                    reason="secret_detected_redaction_applied",
                )
            # Secret label or secret value present but no transform available.
            return InformationReleaseDecision(
                verdict=PolicyOutcome.DENY,
                transform=None,
                allowed_fields=frozenset(),
                retention="none",
                audit_level="summary",
                reason="secret_no_transform_available",
            )

        # No secrets detected -- allow without transform.
        if asset.classification == Classification.INTERNAL:
            return InformationReleaseDecision(
                verdict=PolicyOutcome.ALLOW,
                transform=None,
                allowed_fields=frozenset(),
                retention="raw",
                audit_level="summary",
                reason="internal_no_secrets",
            )

        return InformationReleaseDecision(
            verdict=PolicyOutcome.ALLOW,
            transform=None,
            allowed_fields=frozenset(),
            retention="raw",
            audit_level="summary",
            reason="default_allow_no_secrets",
        )

    # ------------------------------------------------------------------

    def _contains_secret_value(self, content: str) -> bool:
        """Check whether *content* contains any exact configured secret value."""
        return any(s and s in content for s in self._secrets.secret_values)

"""Information flow domain types.

Pure domain types for data classification, release targets, secret catalogs,
and release decisions. No IO, no LLM inference, no pydantic, no infrastructure.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.domain.policy import PolicyOutcome


class Classification(str, Enum):
    """Data classification levels (stable string values)."""

    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"
    SECRET = "secret"


class ReleaseTarget(str, Enum):
    """Boundary targets for information release."""

    LLM_PROVIDER = "llm_provider"
    TOOL_MCP_PLUGIN = "tool_mcp_plugin"
    EXTERNAL_MEMORY = "external_memory"
    GATEWAY = "gateway"
    OBSERVATION_LOG = "observation_log"
    USAGE_RETENTION = "usage_retention"
    LLM_PAYLOAD_LOG = "llm_payload_log"
    CLIENT_RESPONSE = "client_response"


# Standard credential field names whose values should be redacted in
# structured (dict) data.  These are lowercase; matching is case-insensitive.
DEFAULT_CREDENTIAL_FIELD_NAMES: frozenset[str] = frozenset({
    "api_key",
    "password",
    "secret",
    "token",
    "authorization",
})


@dataclass(frozen=True)
class SecretCatalog:
    """Immutable catalog of known secret values and credential field names.

    ``secret_values`` holds exact secret strings (e.g. API keys) to be
    redacted from content.  ``credential_field_names`` holds field names
    whose *values* should be redacted in structured data.
    """

    secret_values: frozenset[str] = frozenset()
    credential_field_names: frozenset[str] = DEFAULT_CREDENTIAL_FIELD_NAMES

    @property
    def max_secret_length(self) -> int:
        """Length of the longest configured secret value (0 if none)."""
        lengths = [len(s) for s in self.secret_values if s]
        return max(lengths) if lengths else 0

    @property
    def has_secrets(self) -> bool:
        return any(self.secret_values)


@dataclass(frozen=True)
class InformationFlowConfig:
    """Domain-level configuration for the information flow policy.

    Mirrors the application-level ``InformationFlowPolicyConfig`` but lives
    in Domain so the Policy never imports Application.
    """

    log_llm_payloads: bool = False
    store_usage_payloads: bool = True
    redact_secrets: bool = True


@dataclass(frozen=True)
class InformationAsset:
    """A unit of information being considered for release.

    Attributes:
        classification: data sensitivity level.
        origin: where the content came from (e.g. "user", "llm_response").
        labels: explicit tags on the content (e.g. ``frozenset({"secret"})``).
        content: the actual text content to evaluate/redact.
    """

    classification: Classification
    origin: str
    labels: frozenset[str]
    content: str


@dataclass(frozen=True)
class InformationReleaseDecision:
    """Domain decision for an information release request.

    Attributes:
        verdict: ALLOW or DENY.
        transform: transform kind to apply (e.g. "redaction") or None.
        allowed_fields: field names allowed in the release (empty = all).
        retention: retention level for the target ("raw", "sanitized", "none").
        audit_level: audit verbosity ("full", "summary", "none").
        reason: human-readable reason for the decision.
    """

    verdict: PolicyOutcome
    transform: str | None
    allowed_fields: frozenset[str]
    retention: str
    audit_level: str
    reason: str

    def __post_init__(self) -> None:
        if self.verdict not in (PolicyOutcome.ALLOW, PolicyOutcome.DENY):
            raise ValueError(
                f"InformationReleaseDecision verdict must be ALLOW or DENY, got {self.verdict}"
            )
        if not self.reason:
            raise ValueError("InformationReleaseDecision reason must not be empty")


class InformationFlowError(Exception):
    """Stable error raised when an information flow transform fails.

    The error message is always the stable code ``information_release_denied``
    and never contains the original content.
    """

    STABLE_CODE = "information_release_denied"

    def __init__(self, code: str = STABLE_CODE) -> None:
        self.code = code
        super().__init__(code)


# ---------------------------------------------------------------------------
# Redaction helpers (pure functions, no IO)
# ---------------------------------------------------------------------------


def redact_secret_values(text: str, secrets: SecretCatalog) -> str:
    """Replace all known secret values in *text* with ``[REDACTED]``.

    Secrets are replaced longest-first to avoid partial matches when one
    secret is a substring of another.
    """
    if not secrets.secret_values:
        return text
    result = text
    for secret in sorted(secrets.secret_values, key=len, reverse=True):
        if secret and secret in result:
            result = result.replace(secret, "[REDACTED]")
    return result


def redact_structured(data: Any, secrets: SecretCatalog) -> Any:
    """Recursively redact secrets in structured data (dict/list/str).

    - Dict keys matching ``credential_field_names`` (case-insensitive) have
      their values replaced with ``[REDACTED]``.
    - String values have exact secret values replaced.
    - Nested dicts and lists are recursed.
    """
    if isinstance(data, dict):
        result: dict[Any, Any] = {}
        lowered_names = {name.lower() for name in secrets.credential_field_names}
        for key, value in data.items():
            if isinstance(key, str) and key.lower() in lowered_names:
                result[key] = "[REDACTED]"
            elif isinstance(value, (dict, list)):
                result[key] = redact_structured(value, secrets)
            elif isinstance(value, str):
                result[key] = redact_secret_values(value, secrets)
            else:
                result[key] = value
        return result
    if isinstance(data, list):
        return [redact_structured(item, secrets) for item in data]
    if isinstance(data, str):
        return redact_secret_values(data, secrets)
    return data

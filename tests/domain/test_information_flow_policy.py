"""S1: Classification and release decision table tests for InformationFlowPolicy.

These tests verify the domain policy decision table:
- INTERNAL asset -> LLM_PROVIDER: ALLOW (no transform)
- Exact configured secret value -> redaction transform
- Explicit secret label + no available transform -> DENY
- Usage retention target -> only sanitized payload
- Application LLM payload log target -> default DENY
"""
from __future__ import annotations

import pytest

from app.domain.information_flow import (
    Classification,
    InformationAsset,
    InformationFlowConfig,
    ReleaseTarget,
    SecretCatalog,
)
from app.domain.information_flow_policy import InformationFlowPolicy
from app.domain.policy import PolicyOutcome


def _make_policy(
    *,
    log_llm_payloads: bool = False,
    store_usage_payloads: bool = True,
    redact_secrets: bool = True,
    secret_values: frozenset[str] | None = None,
) -> InformationFlowPolicy:
    return InformationFlowPolicy(
        config=InformationFlowConfig(
            log_llm_payloads=log_llm_payloads,
            store_usage_payloads=store_usage_payloads,
            redact_secrets=redact_secrets,
        ),
        secrets=SecretCatalog(
            secret_values=secret_values or frozenset(),
        ),
    )


class TestDecisionTable:
    """S1 decision table cases."""

    def test_internal_asset_to_provider_allow_no_transform(self):
        """Case 1: INTERNAL asset -> currently configured Provider: ALLOW."""
        policy = _make_policy()
        asset = InformationAsset(
            classification=Classification.INTERNAL,
            origin="user",
            labels=frozenset(),
            content="hello world",
        )
        decision = policy.evaluate(asset, ReleaseTarget.LLM_PROVIDER)
        assert decision.verdict is PolicyOutcome.ALLOW
        assert decision.transform is None
        assert decision.retention == "raw"

    def test_exact_configured_secret_value_triggers_redaction(self):
        """Case 2: asset containing an exact configured secret value -> transform redaction."""
        policy = _make_policy(secret_values=frozenset({"sk-secret123"}))
        asset = InformationAsset(
            classification=Classification.INTERNAL,
            origin="user",
            labels=frozenset(),
            content="the key is sk-secret123 and that is it",
        )
        decision = policy.evaluate(asset, ReleaseTarget.LLM_PROVIDER)
        assert decision.verdict is PolicyOutcome.ALLOW
        assert decision.transform == "redaction"
        assert decision.retention == "sanitized"

    def test_explicit_secret_label_no_transform_denies(self):
        """Case 3: explicit secret label + no available transform -> DENY."""
        policy = _make_policy(redact_secrets=False)
        asset = InformationAsset(
            classification=Classification.SECRET,
            origin="user",
            labels=frozenset({"secret"}),
            content="some sensitive content",
        )
        decision = policy.evaluate(asset, ReleaseTarget.LLM_PROVIDER)
        assert decision.verdict is PolicyOutcome.DENY
        assert decision.transform is None
        assert decision.retention == "none"

    def test_explicit_secret_label_with_transform_allows_redaction(self):
        """Case 3 complement: secret label + redact_secrets=True -> ALLOW with redaction."""
        policy = _make_policy(redact_secrets=True)
        asset = InformationAsset(
            classification=Classification.SECRET,
            origin="user",
            labels=frozenset({"secret"}),
            content="some sensitive content",
        )
        decision = policy.evaluate(asset, ReleaseTarget.LLM_PROVIDER)
        assert decision.verdict is PolicyOutcome.ALLOW
        assert decision.transform == "redaction"

    def test_usage_retention_only_sanitized_payload(self):
        """Case 4: usage retention target -> only sanitized payload retained."""
        policy = _make_policy(secret_values=frozenset({"sk-secret123"}))
        asset = InformationAsset(
            classification=Classification.INTERNAL,
            origin="llm_response",
            labels=frozenset(),
            content='{"role":"assistant","content":"key=sk-secret123"}',
        )
        decision = policy.evaluate(asset, ReleaseTarget.USAGE_RETENTION)
        assert decision.verdict is PolicyOutcome.ALLOW
        assert decision.transform == "redaction"
        assert decision.retention == "sanitized"

    def test_usage_retention_no_secrets_still_sanitized_retention(self):
        """Case 4 complement: usage retention with no secrets -> ALLOW, retention=sanitized."""
        policy = _make_policy()
        asset = InformationAsset(
            classification=Classification.INTERNAL,
            origin="llm_response",
            labels=frozenset(),
            content="hello",
        )
        decision = policy.evaluate(asset, ReleaseTarget.USAGE_RETENTION)
        assert decision.verdict is PolicyOutcome.ALLOW
        assert decision.transform is None
        assert decision.retention == "sanitized"

    def test_usage_retention_disabled_denies(self):
        """Case 4 edge: store_usage_payloads=False -> DENY."""
        policy = _make_policy(store_usage_payloads=False)
        asset = InformationAsset(
            classification=Classification.INTERNAL,
            origin="llm_response",
            labels=frozenset(),
            content="hello",
        )
        decision = policy.evaluate(asset, ReleaseTarget.USAGE_RETENTION)
        assert decision.verdict is PolicyOutcome.DENY
        assert decision.retention == "none"

    def test_llm_payload_log_default_denies(self):
        """Case 5: application LLM payload log -> default DENY (log_llm_payloads=False)."""
        policy = _make_policy(log_llm_payloads=False)
        asset = InformationAsset(
            classification=Classification.INTERNAL,
            origin="llm_request",
            labels=frozenset(),
            content="some request content",
        )
        decision = policy.evaluate(asset, ReleaseTarget.LLM_PAYLOAD_LOG)
        assert decision.verdict is PolicyOutcome.DENY
        assert decision.retention == "none"

    def test_llm_payload_log_enabled_allows_without_secrets(self):
        """Case 5 complement: log_llm_payloads=True + no secrets -> ALLOW."""
        policy = _make_policy(log_llm_payloads=True)
        asset = InformationAsset(
            classification=Classification.INTERNAL,
            origin="llm_request",
            labels=frozenset(),
            content="some request content",
        )
        decision = policy.evaluate(asset, ReleaseTarget.LLM_PAYLOAD_LOG)
        assert decision.verdict is PolicyOutcome.ALLOW
        assert decision.transform is None
        assert decision.retention == "raw"

    def test_llm_payload_log_enabled_with_secret_redacts(self):
        """Case 5 complement: log_llm_payloads=True + secret -> ALLOW with redaction."""
        policy = _make_policy(
            log_llm_payloads=True,
            secret_values=frozenset({"sk-secret123"}),
        )
        asset = InformationAsset(
            classification=Classification.INTERNAL,
            origin="llm_request",
            labels=frozenset(),
            content="key=sk-secret123",
        )
        decision = policy.evaluate(asset, ReleaseTarget.LLM_PAYLOAD_LOG)
        assert decision.verdict is PolicyOutcome.ALLOW
        assert decision.transform == "redaction"
        assert decision.retention == "sanitized"


class TestSecretCatalog:
    def test_max_secret_length(self):
        catalog = SecretCatalog(secret_values=frozenset({"abc", "abcdef", "x"}))
        assert catalog.max_secret_length == 6

    def test_max_secret_length_empty(self):
        catalog = SecretCatalog()
        assert catalog.max_secret_length == 0

    def test_has_secrets(self):
        assert SecretCatalog(secret_values=frozenset({"abc"})).has_secrets
        assert not SecretCatalog().has_secrets

    def test_empty_string_secret_ignored(self):
        catalog = SecretCatalog(secret_values=frozenset({""}))
        assert catalog.max_secret_length == 0
        assert not catalog.has_secrets

    def test_default_credential_field_names(self):
        catalog = SecretCatalog()
        assert "api_key" in catalog.credential_field_names
        assert "password" in catalog.credential_field_names
        assert "token" in catalog.credential_field_names

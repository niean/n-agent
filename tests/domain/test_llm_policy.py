"""Tests for LLMPolicy domain module.

Decision table tests covering:
- placeholder model -> active default resolution
- tool/vision/context-window capability requirements
- fallback disabled -> empty chain
- ProviderConstraints excluding providers
"""

from __future__ import annotations

import pytest

from app.domain.llm_policy import (
    LLMConfig,
    LLMPolicy,
    ModelRequirements,
    ModelSelection,
    ProviderCapability,
    ProviderConstraints,
)
from app.domain.policy import PolicyOutcome


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def policy() -> LLMPolicy:
    return LLMPolicy()


@pytest.fixture
def standard_cap() -> ProviderCapability:
    return ProviderCapability(
        provider_id="p1",
        model_id="gpt-4",
        supports_tools=True,
        supports_vision=True,
        context_window=128_000,
    )


@pytest.fixture
def no_constraints() -> ProviderConstraints:
    return ProviderConstraints(allowed_provider_ids=frozenset({"p1"}))


@pytest.fixture
def no_requirements() -> ModelRequirements:
    return ModelRequirements(capabilities=frozenset(), token_need=0)


# ---------------------------------------------------------------------------
# Placeholder model resolution
# ---------------------------------------------------------------------------

class TestPlaceholderResolution:
    def test_empty_model_resolves_to_active_default(self, policy, standard_cap, no_constraints, no_requirements):
        result = policy.evaluate(
            "", (standard_cap,), no_requirements, no_constraints, LLMConfig(),
        )
        assert result.verdict is PolicyOutcome.ALLOW
        assert result.model_id == "gpt-4"

    def test_n_agent_placeholder_resolves_to_active_default(self, policy, standard_cap, no_constraints, no_requirements):
        result = policy.evaluate(
            "n-agent", (standard_cap,), no_requirements, no_constraints, LLMConfig(),
        )
        assert result.verdict is PolicyOutcome.ALLOW
        assert result.model_id == "gpt-4"

    def test_n_agent_uppercase_placeholder_resolves(self, policy, standard_cap, no_constraints, no_requirements):
        result = policy.evaluate(
            "N-Agent", (standard_cap,), no_requirements, no_constraints, LLMConfig(),
        )
        assert result.verdict is PolicyOutcome.ALLOW
        assert result.model_id == "gpt-4"

    def test_non_placeholder_model_passes_through(self, policy, standard_cap, no_constraints, no_requirements):
        result = policy.evaluate(
            "claude-3-opus", (standard_cap,), no_requirements, no_constraints, LLMConfig(),
        )
        assert result.verdict is PolicyOutcome.ALLOW
        assert result.model_id == "claude-3-opus"


# ---------------------------------------------------------------------------
# Capability requirements
# ---------------------------------------------------------------------------

class TestCapabilityRequirements:
    def test_tools_required_and_supported_allows(self, policy, standard_cap, no_constraints):
        req = ModelRequirements(capabilities=frozenset({"tools"}), token_need=0)
        result = policy.evaluate("gpt-4", (standard_cap,), req, no_constraints, LLMConfig())
        assert result.verdict is PolicyOutcome.ALLOW

    def test_tools_required_but_not_supported_denies(self, policy, no_constraints):
        cap = ProviderCapability(
            provider_id="p1", model_id="gpt-4", supports_tools=False,
        )
        req = ModelRequirements(capabilities=frozenset({"tools"}), token_need=0)
        result = policy.evaluate("gpt-4", (cap,), req, no_constraints, LLMConfig())
        assert result.verdict is PolicyOutcome.DENY
        assert "tool" in result.reason.lower()

    def test_vision_required_and_supported_allows(self, policy, standard_cap, no_constraints):
        req = ModelRequirements(capabilities=frozenset({"vision"}), token_need=0)
        result = policy.evaluate("gpt-4", (standard_cap,), req, no_constraints, LLMConfig())
        assert result.verdict is PolicyOutcome.ALLOW

    def test_vision_required_but_not_supported_denies(self, policy, no_constraints):
        cap = ProviderCapability(
            provider_id="p1", model_id="gpt-4", supports_vision=False,
        )
        req = ModelRequirements(capabilities=frozenset({"vision"}), token_need=0)
        result = policy.evaluate("gpt-4", (cap,), req, no_constraints, LLMConfig())
        assert result.verdict is PolicyOutcome.DENY
        assert "vision" in result.reason.lower()

    def test_context_window_sufficient_allows(self, policy, standard_cap, no_constraints):
        req = ModelRequirements(capabilities=frozenset({"context_window"}), token_need=4096)
        result = policy.evaluate("gpt-4", (standard_cap,), req, no_constraints, LLMConfig())
        assert result.verdict is PolicyOutcome.ALLOW

    def test_context_window_insufficient_denies(self, policy, no_constraints):
        cap = ProviderCapability(
            provider_id="p1", model_id="gpt-4", context_window=4096,
        )
        req = ModelRequirements(capabilities=frozenset({"context_window"}), token_need=8192)
        result = policy.evaluate("gpt-4", (cap,), req, no_constraints, LLMConfig())
        assert result.verdict is PolicyOutcome.DENY
        assert "context" in result.reason.lower()

    def test_context_window_zero_unknown_does_not_deny(self, policy, no_constraints):
        """context_window=0 means unknown -- should not deny."""
        cap = ProviderCapability(
            provider_id="p1", model_id="gpt-4", context_window=0,
        )
        req = ModelRequirements(capabilities=frozenset({"context_window"}), token_need=999999)
        result = policy.evaluate("gpt-4", (cap,), req, no_constraints, LLMConfig())
        assert result.verdict is PolicyOutcome.ALLOW

    def test_multiple_capabilities_all_met_allows(self, policy, standard_cap, no_constraints):
        req = ModelRequirements(
            capabilities=frozenset({"tools", "vision", "context_window"}),
            token_need=4096,
        )
        result = policy.evaluate("gpt-4", (standard_cap,), req, no_constraints, LLMConfig())
        assert result.verdict is PolicyOutcome.ALLOW

    def test_multiple_capabilities_one_missing_denies(self, policy, no_constraints):
        cap = ProviderCapability(
            provider_id="p1",
            model_id="gpt-4",
            supports_tools=True,
            supports_vision=False,
            context_window=128_000,
        )
        req = ModelRequirements(
            capabilities=frozenset({"tools", "vision"}),
            token_need=0,
        )
        result = policy.evaluate("gpt-4", (cap,), req, no_constraints, LLMConfig())
        assert result.verdict is PolicyOutcome.DENY
        assert "vision" in result.reason.lower()


# ---------------------------------------------------------------------------
# Fallback chain
# ---------------------------------------------------------------------------

class TestFallbackChain:
    def test_fallback_disabled_empty_chain(self, policy, standard_cap, no_constraints, no_requirements):
        result = policy.evaluate(
            "gpt-4", (standard_cap,), no_requirements, no_constraints,
            LLMConfig(fallback_enabled=False),
        )
        assert result.fallback_chain == ()

    def test_fallback_enabled_includes_other_providers(self, policy, no_constraints, no_requirements):
        cap1 = ProviderCapability(provider_id="p1", model_id="gpt-4")
        cap2 = ProviderCapability(provider_id="p2", model_id="claude-3")
        constraints = ProviderConstraints(allowed_provider_ids=frozenset({"p1", "p2"}))
        result = policy.evaluate(
            "gpt-4", (cap1, cap2), no_requirements, constraints,
            LLMConfig(fallback_enabled=True),
        )
        assert result.verdict is PolicyOutcome.ALLOW
        assert "p2" in result.fallback_chain

    def test_fallback_enabled_single_provider_empty_chain(self, policy, standard_cap, no_constraints, no_requirements):
        result = policy.evaluate(
            "gpt-4", (standard_cap,), no_requirements, no_constraints,
            LLMConfig(fallback_enabled=True),
        )
        assert result.fallback_chain == ()


# ---------------------------------------------------------------------------
# ProviderConstraints
# ---------------------------------------------------------------------------

class TestProviderConstraints:
    def test_provider_excluded_by_constraints_denies(self, policy, standard_cap, no_requirements):
        constraints = ProviderConstraints(allowed_provider_ids=frozenset({"p2"}))
        result = policy.evaluate(
            "gpt-4", (standard_cap,), no_requirements, constraints, LLMConfig(),
        )
        assert result.verdict is PolicyOutcome.DENY
        assert "constraint" in result.reason.lower() or "no_provider" in result.reason.lower()

    def test_empty_constraints_denies(self, policy, standard_cap, no_requirements):
        constraints = ProviderConstraints(allowed_provider_ids=frozenset())
        result = policy.evaluate(
            "gpt-4", (standard_cap,), no_requirements, constraints, LLMConfig(),
        )
        assert result.verdict is PolicyOutcome.DENY

    def test_multiple_providers_first_allowed_selected(self, policy, no_requirements):
        cap1 = ProviderCapability(provider_id="p1", model_id="gpt-4")
        cap2 = ProviderCapability(provider_id="p2", model_id="claude-3")
        constraints = ProviderConstraints(allowed_provider_ids=frozenset({"p1", "p2"}))
        result = policy.evaluate(
            "gpt-4", (cap1, cap2), no_requirements, constraints, LLMConfig(),
        )
        assert result.verdict is PolicyOutcome.ALLOW
        assert result.provider_id == "p1"

    def test_first_provider_excluded_second_selected(self, policy, no_requirements):
        cap1 = ProviderCapability(provider_id="p1", model_id="gpt-4")
        cap2 = ProviderCapability(provider_id="p2", model_id="claude-3")
        constraints = ProviderConstraints(allowed_provider_ids=frozenset({"p2"}))
        result = policy.evaluate(
            "gpt-4", (cap1, cap2), no_requirements, constraints, LLMConfig(),
        )
        assert result.verdict is PolicyOutcome.ALLOW
        assert result.provider_id == "p2"


# ---------------------------------------------------------------------------
# ModelSelection structure
# ---------------------------------------------------------------------------

class TestModelSelection:
    def test_allow_selection_has_provider_and_model(self, policy, standard_cap, no_constraints, no_requirements):
        result = policy.evaluate(
            "gpt-4", (standard_cap,), no_requirements, no_constraints, LLMConfig(),
        )
        assert result.provider_id == "p1"
        assert result.model_id == "gpt-4"
        assert result.verdict is PolicyOutcome.ALLOW
        assert result.reason

    def test_deny_selection_has_reason(self, policy, no_constraints, no_requirements):
        constraints = ProviderConstraints(allowed_provider_ids=frozenset())
        result = policy.evaluate(
            "gpt-4", (standard_cap := ProviderCapability("p1", "gpt-4"),),
            no_requirements, constraints, LLMConfig(),
        )
        assert result.verdict is PolicyOutcome.DENY
        assert result.reason

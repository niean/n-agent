"""LLM Policy -- Domain-level governance for provider/model selection.

This module is pure Domain: it imports only stdlib/typing/dataclasses and
``app.domain.provider`` + ``app.domain.policy``.  It does NOT import
ContextPolicy, InformationFlowPolicy, pydantic, or Infrastructure.

The Policy takes LLM-owned input types (``ModelRequirements``,
``ProviderConstraints``, ``ProviderCapability``) and returns a
``ModelSelection``.  The Application mapper projects ContextPlan and
InformationReleaseDecision into these LLM-owned types before calling
the policy.

Cross-domain rule: LLMPolicy receives ONLY LLM-owned input types.
``ModelRequirements`` carries capabilities + token need (projected from
ContextPlan.token_allocation by the Application mapper).
``ProviderConstraints`` carries allowed provider ids (projected from
InformationReleaseDecision by the Application mapper).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from app.domain.policy import PolicyOutcome
from app.domain.provider import PLACEHOLDER_MODEL_IDS, resolve_model


# ---------------------------------------------------------------------------
# LLM-owned input types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelRequirements:
    """LLM-owned: capabilities needed + token need.

    ``capabilities`` is a frozenset of strings like "tools", "vision",
    "context_window".  ``token_need`` is the estimated token budget
    required (from ContextPlan.token_allocation.total, projected by
    the Application mapper).
    """

    capabilities: frozenset[str]
    token_need: int = 0


@dataclass(frozen=True)
class ProviderConstraints:
    """LLM-owned: available provider ids from InformationFlow.

    If a provider id is not in ``allowed_provider_ids``, the policy
    will not select it.  An empty set means no provider can be selected
    (InformationFlow denied release to all providers).
    """

    allowed_provider_ids: frozenset[str]


@dataclass(frozen=True)
class ProviderCapability:
    """LLM-owned: capability snapshot of a provider/model.

    Projected by the Application mapper from ``ProviderConfig`` (which
    has ``supports_vision``, ``model``, ``id``) and optionally
    ``ModelInfo`` (which has ``supports_tools``).

    ``context_window`` of 0 means unknown -- the policy does not
    enforce context-window limits when the window is unknown.
    """

    provider_id: str
    model_id: str
    supports_tools: bool = True
    supports_vision: bool = False
    context_window: int = 0


# ---------------------------------------------------------------------------
# Domain config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMConfig:
    """Domain-level configuration for the LLM policy.

    Mirrors the application-level ``LLMPolicyConfig`` but lives in
    Domain so the Policy never imports Application.
    """

    fallback_enabled: bool = False


# ---------------------------------------------------------------------------
# Output type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSelection:
    """Output of ``LLMPolicy.evaluate``.

    - ``provider_id``: selected provider id.
    - ``model_id``: resolved model id (placeholder resolved to default).
    - ``capabilities``: capabilities that were matched.
    - ``generation_limits``: generation parameter limits (currently empty;
      future tasks may populate from policy config).
    - ``fallback_chain``: ordered provider ids for fallback (empty when
      fallback is disabled).
    - ``verdict``: ALLOW or DENY.
    - ``reason``: human-readable decision reason.
    """

    provider_id: str
    model_id: str
    capabilities: frozenset[str] = frozenset()
    generation_limits: Mapping[str, Any] = field(default_factory=dict)
    fallback_chain: tuple[str, ...] = ()
    verdict: PolicyOutcome = PolicyOutcome.ALLOW
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("ModelSelection reason must not be empty")


# ---------------------------------------------------------------------------
# LLMPolicy
# ---------------------------------------------------------------------------


class LLMPolicy:
    """Domain policy for provider/model selection.

    Decision table:
    1. Filter providers by ProviderConstraints.
    2. If no providers allowed -> DENY.
    3. Resolve placeholder model (empty / "n-agent" / "model") to the
       first allowed provider's model_id.
    4. Check capabilities:
       - tools required but provider lacks tools -> DENY.
       - vision required but provider lacks vision -> DENY.
       - context_window required and known window < token_need -> DENY.
    5. Build fallback_chain: if fallback_enabled, include remaining
       allowed provider ids; else empty.
    6. ALLOW with selected provider/model.
    """

    def evaluate(
        self,
        requested_model: str,
        provider_capabilities: tuple[ProviderCapability, ...],
        requirements: ModelRequirements,
        constraints: ProviderConstraints,
        config: LLMConfig,
    ) -> ModelSelection:
        # 1. Filter by constraints
        allowed_caps = tuple(
            c for c in provider_capabilities
            if c.provider_id in constraints.allowed_provider_ids
        )
        if not allowed_caps:
            return ModelSelection(
                provider_id="",
                model_id="",
                verdict=PolicyOutcome.DENY,
                reason="no_provider_allowed_by_constraints",
            )

        cap = allowed_caps[0]

        # 2. Resolve placeholder model
        resolved_model = resolve_model(requested_model, cap.model_id)

        # 3. Check capabilities
        deny_reason = self._check_capabilities(cap, requirements)
        if deny_reason is not None:
            return ModelSelection(
                provider_id=cap.provider_id,
                model_id=resolved_model,
                capabilities=requirements.capabilities,
                verdict=PolicyOutcome.DENY,
                reason=deny_reason,
            )

        # 4. Build fallback chain
        fallback_chain: tuple[str, ...] = ()
        if config.fallback_enabled:
            fallback_chain = tuple(c.provider_id for c in allowed_caps[1:])

        return ModelSelection(
            provider_id=cap.provider_id,
            model_id=resolved_model,
            capabilities=requirements.capabilities,
            generation_limits={},
            fallback_chain=fallback_chain,
            verdict=PolicyOutcome.ALLOW,
            reason="model_selected",
        )

    def _check_capabilities(
        self,
        cap: ProviderCapability,
        requirements: ModelRequirements,
    ) -> str | None:
        """Return a deny reason string if capabilities are insufficient, else None."""
        caps = requirements.capabilities

        if "tools" in caps and not cap.supports_tools:
            return "tool_capability_not_supported"

        if "vision" in caps and not cap.supports_vision:
            return "vision_capability_not_supported"

        if "context_window" in caps:
            if cap.context_window > 0 and requirements.token_need > cap.context_window:
                return (
                    f"context_window_exceeded:need={requirements.token_need},"
                    f"window={cap.context_window}"
                )

        return None

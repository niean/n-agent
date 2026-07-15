# app/application/policy_snapshot.py
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from app.application.session_bootstrap import SessionDescriptor
from app.config import Settings
from app.domain.policy import ExecutionMode, RunPolicyContext
from app.domain.tool import ToolSourceType

# ---------------------------------------------------------------------------
# Typed Policy configs (Application DTOs derived from Settings)
#
# These dataclasses are configuration *snapshots* -- immutable values that
# capture Settings at resolve-time.  They live in Application (not Domain)
# because they are transport-level config, not domain concepts.  Later Domain
# Policies (T6-T11) receive needed config *values* via their own Domain-defined
# Request/input types; the Application mapper unpacks values from these typed
# configs into the Domain Request.  Domain never imports these classes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnPolicyConfig:
    iteration_limit: int = 10
    turn_timeout_seconds: int = 900


@dataclass(frozen=True)
class ContextPolicyConfig:
    context_length: int = 32000
    compression_threshold: float = 0.50
    compression_target_ratio: float = 0.20
    protect_first_n: int = 3
    protect_last_n: int = 10
    cooldown_seconds: int = 300
    tail_budget_enabled: bool = False


@dataclass(frozen=True)
class LLMPolicyConfig:
    fallback_enabled: bool = False


@dataclass(frozen=True)
class ToolPolicyConfig:
    # ToolPolicy is self-contained in Domain (keeps ToolDefinition /
    # ToolExposurePolicy).  This config is a version holder only; no
    # Settings-derived fields are needed yet.  When tool-level config
    # becomes Settings-driven, add fields here and map them in the provider.
    version: str = "system-v1"


@dataclass(frozen=True)
class MemoryPolicyConfig:
    cross_session_read_enabled: bool = False
    unattended_write_enabled: bool = False


@dataclass(frozen=True)
class SandboxPolicyConfig:
    timeout_seconds: int = 300
    max_tool_calls: int = 50
    cpus: float = 1.0
    memory_mb: int = 512
    network_enabled: bool = False
    idle_seconds: int = 900
    workspace_readonly: bool = True


@dataclass(frozen=True)
class GatewayPolicyConfig:
    enabled: bool = True
    confirmation_ttl_seconds: int = 900
    require_actor_for_managed_actions: bool = True


@dataclass(frozen=True)
class SchedulePolicyConfig:
    tick_seconds: float = 30
    max_due_per_tick: int = 5
    missed_grace_seconds: int = 300
    lease_seconds: int = 900
    # Scheduled tasks run unattended -> safe_only tool exposure, which blocks
    # source_type=AGENT tools (prevents the scheduler from recursively invoking
    # agent tools); task-level granted_tools can still exempt specific SAFE
    # AGENT tools (e.g. host_terminal). Constant mirror of the enforcement in
    # ToolPolicy.can_expose, surfaced so the Schedule sector displays the tool
    # source_type constraint.
    unattended_blocked_source_type: str = ToolSourceType.AGENT.value


@dataclass(frozen=True)
class BudgetPolicyConfig:
    max_wall_seconds: int = 900
    max_llm_calls: int = 10
    max_tool_calls: int = 100
    max_token_cost: int | None = None
    max_usd_cost: Decimal | None = None
    max_sandbox_seconds: float | None = None
    max_sandbox_cpu_seconds: float | None = None
    max_sandbox_memory_mb_seconds: float | None = None
    max_sandbox_callback_calls: int | None = None


@dataclass(frozen=True)
class InformationFlowPolicyConfig:
    log_llm_payloads: bool = False
    store_usage_payloads: bool = True
    redact_secrets: bool = True


# ---------------------------------------------------------------------------
# Profile facts & resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyProfileFacts:
    """Minimal facts needed to resolve a policy profile."""

    source: str
    execution_mode: ExecutionMode
    descriptor_source: str


@dataclass(frozen=True)
class ResolvedPolicyProfile:
    """A resolved set of 10 typed Policy configs with a version tag."""

    version: str
    turn: TurnPolicyConfig
    context: ContextPolicyConfig
    llm: LLMPolicyConfig
    tool: ToolPolicyConfig
    memory: MemoryPolicyConfig
    sandbox: SandboxPolicyConfig
    gateway: GatewayPolicyConfig
    schedule: SchedulePolicyConfig
    budget: BudgetPolicyConfig
    information_flow: InformationFlowPolicyConfig


class PolicyProfileProvider(Protocol):
    def resolve(
        self, scope_ref: str, facts: PolicyProfileFacts,
    ) -> ResolvedPolicyProfile: ...


class SettingsPolicyProfileProvider:
    """Wraps a Settings instance and reads it at resolve() time.

    Returns the system typed profile with version "system-v1".
    The provider holds the Settings reference; the factory does not.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def resolve(
        self, scope_ref: str, facts: PolicyProfileFacts,
    ) -> ResolvedPolicyProfile:
        s = self._settings
        return ResolvedPolicyProfile(
            version="system-v1",
            turn=TurnPolicyConfig(
                iteration_limit=s.agent_iteration_limit,
                turn_timeout_seconds=s.agent_turn_timeout_seconds,
            ),
            context=ContextPolicyConfig(
                context_length=s.context_length,
                compression_threshold=s.context_compression_threshold,
                compression_target_ratio=s.context_compression_target_ratio,
                protect_first_n=s.context_compression_protect_first_n,
                protect_last_n=s.context_compression_protect_last_n,
                cooldown_seconds=s.context_compression_cooldown_seconds,
                tail_budget_enabled=s.context_compression_tail_budget_enabled,
            ),
            llm=LLMPolicyConfig(fallback_enabled=s.llm_fallback_enabled),
            tool=ToolPolicyConfig(),
            memory=MemoryPolicyConfig(
                cross_session_read_enabled=s.memory_cross_session_read_enabled,
                unattended_write_enabled=s.memory_unattended_write_enabled,
            ),
            sandbox=SandboxPolicyConfig(
                timeout_seconds=s.sandbox_timeout_seconds,
                max_tool_calls=s.sandbox_max_tool_calls,
                cpus=s.sandbox_docker_cpus,
                memory_mb=s.sandbox_docker_memory_mb,
                network_enabled=s.sandbox_docker_network,
                idle_seconds=s.sandbox_idle_seconds,
                workspace_readonly=True,
            ),
            gateway=GatewayPolicyConfig(
                enabled=s.gateway_enabled,
                confirmation_ttl_seconds=s.gateway_confirmation_ttl_seconds,
                require_actor_for_managed_actions=s.gateway_require_actor_for_managed_actions,
            ),
            schedule=SchedulePolicyConfig(
                tick_seconds=s.scheduler_tick_seconds,
                max_due_per_tick=s.scheduler_max_due_per_tick,
                missed_grace_seconds=s.scheduler_missed_grace_seconds,
                lease_seconds=s.scheduler_lease_seconds,
                unattended_blocked_source_type=ToolSourceType.AGENT.value,
            ),
            budget=BudgetPolicyConfig(
                max_wall_seconds=s.budget_max_wall_seconds,
                max_llm_calls=s.budget_max_llm_calls,
                max_tool_calls=s.budget_max_tool_calls,
                max_token_cost=s.budget_max_token_cost,
                max_usd_cost=s.budget_max_usd_cost,
                max_sandbox_seconds=s.budget_max_sandbox_seconds,
                max_sandbox_cpu_seconds=s.budget_max_sandbox_cpu_seconds,
                max_sandbox_memory_mb_seconds=s.budget_max_sandbox_memory_mb_seconds,
                max_sandbox_callback_calls=s.budget_max_sandbox_callback_calls,
            ),
            information_flow=InformationFlowPolicyConfig(
                log_llm_payloads=s.information_log_llm_payloads,
                store_usage_payloads=s.information_store_usage_payloads,
                redact_secrets=s.information_redact_secrets,
            ),
        )


# ---------------------------------------------------------------------------
# IngressFacts (Interfaces-verified entry facts)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IngressFacts:
    """Verified entry facts produced by Interfaces.

    ``trusted_claims`` contains server-issued claims ONLY -- never request-body
    metadata.  Untrusted metadata must not flow into this type.
    """

    run_id: str
    session_id: str
    source: str
    actor_id: str | None
    execution_mode: ExecutionMode
    trusted_claims: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "trusted_claims", MappingProxyType(dict(self.trusted_claims)),
        )


# ---------------------------------------------------------------------------
# RunPolicySnapshot (immutable per-run snapshot)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunPolicySnapshot:
    """Frozen per-run policy snapshot.

    Holds the common RunPolicyContext, profile version, and 10 typed Policy
    configs.  Does NOT hold Budget account, approval pending, clock, manager,
    store, or any mutable runtime state.  After creation, the snapshot is never
    re-read against mutable config; the next run builds a new snapshot.
    """

    run_context: RunPolicyContext
    profile_version: str
    turn: TurnPolicyConfig
    context_config: ContextPolicyConfig
    llm: LLMPolicyConfig
    tool: ToolPolicyConfig
    memory: MemoryPolicyConfig
    sandbox: SandboxPolicyConfig
    gateway: GatewayPolicyConfig
    schedule: SchedulePolicyConfig
    budget: BudgetPolicyConfig
    information_flow: InformationFlowPolicyConfig


# ---------------------------------------------------------------------------
# RunPolicySnapshotFactory
# ---------------------------------------------------------------------------


class RunPolicySnapshotFactory:
    """Parses ingress facts + descriptor + profile into an immutable snapshot.

    The factory does NOT make business decisions and does NOT hold a Settings
    reference.  It delegates profile resolution to the injected
    PolicyProfileProvider.
    """

    def __init__(self, profile_provider: PolicyProfileProvider) -> None:
        self._profile_provider = profile_provider

    def create(
        self,
        ingress: IngressFacts,
        descriptor: SessionDescriptor,
    ) -> RunPolicySnapshot:
        facts = PolicyProfileFacts(
            source=ingress.source,
            execution_mode=ingress.execution_mode,
            descriptor_source=descriptor.source,
        )
        scope_ref = "system"
        profile = self._profile_provider.resolve(scope_ref, facts)

        run_context = RunPolicyContext(
            run_id=ingress.run_id,
            session_id=ingress.session_id,
            source=ingress.source,
            actor_id=ingress.actor_id,
            execution_mode=ingress.execution_mode,
            trusted_claims=ingress.trusted_claims,
            policy_scope=scope_ref,
        )

        return RunPolicySnapshot(
            run_context=run_context,
            profile_version=profile.version,
            turn=profile.turn,
            context_config=profile.context,
            llm=profile.llm,
            tool=profile.tool,
            memory=profile.memory,
            sandbox=profile.sandbox,
            gateway=profile.gateway,
            schedule=profile.schedule,
            budget=profile.budget,
            information_flow=profile.information_flow,
        )

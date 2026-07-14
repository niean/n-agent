from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from app.application.policy_snapshot import (
    BudgetPolicyConfig,
    ContextPolicyConfig,
    GatewayPolicyConfig,
    IngressFacts,
    InformationFlowPolicyConfig,
    LLMPolicyConfig,
    MemoryPolicyConfig,
    PolicyProfileFacts,
    PolicyProfileProvider,
    ResolvedPolicyProfile,
    RunPolicySnapshot,
    RunPolicySnapshotFactory,
    SandboxPolicyConfig,
    SchedulePolicyConfig,
    SettingsPolicyProfileProvider,
    ToolPolicyConfig,
    TurnPolicyConfig,
)
from app.application.session_bootstrap import SessionDescriptor
from app.config import Settings
from app.domain.policy import ExecutionMode


class FakePolicyProfileProvider:
    """Returns a pre-built profile regardless of scope_ref.

    Used to prove the factory never reads Settings or modifies Domain Policies.
    """

    def __init__(
        self,
        profile: ResolvedPolicyProfile,
        scope_ref: str = "system",
    ) -> None:
        self._profile = profile
        self._scope_ref = scope_ref
        self.resolve_calls: list[tuple[str, PolicyProfileFacts]] = []

    def resolve(
        self, scope_ref: str, facts: PolicyProfileFacts,
    ) -> ResolvedPolicyProfile:
        self.resolve_calls.append((scope_ref, facts))
        return self._profile


def _system_profile() -> ResolvedPolicyProfile:
    return SettingsPolicyProfileProvider(Settings()).resolve(
        "system",
        PolicyProfileFacts(
            source="api",
            execution_mode=ExecutionMode.REALTIME,
            descriptor_source="api",
        ),
    )


def _ingress() -> IngressFacts:
    return IngressFacts(
        run_id="run-1",
        session_id="s1",
        source="api",
        actor_id="user-1",
        execution_mode=ExecutionMode.REALTIME,
        trusted_claims={"gateway.platform": "feishu"},
    )


def _descriptor() -> SessionDescriptor:
    return SessionDescriptor(
        id="s1", exists=True, source="api", external_memory_profile_ref=(),
    )


# -- S4: profile provider replaceable + snapshot immutability --

def test_snapshot_is_immutable_to_settings_changes():
    settings = Settings()
    system_profile = SettingsPolicyProfileProvider(settings).resolve(
        "system",
        PolicyProfileFacts(
            source="api",
            execution_mode=ExecutionMode.REALTIME,
            descriptor_source="api",
        ),
    )

    provider = FakePolicyProfileProvider(profile=system_profile)
    factory = RunPolicySnapshotFactory(provider)

    snapshot = factory.create(_ingress(), _descriptor())

    # Mutate Settings after snapshot creation
    settings.agent_iteration_limit = 3

    assert snapshot.turn.iteration_limit == 10
    assert snapshot.run_context.trusted_claims["gateway.platform"] == "feishu"


def test_snapshot_preserves_trusted_claims():
    provider = FakePolicyProfileProvider(profile=_system_profile())
    factory = RunPolicySnapshotFactory(provider)

    ingress = IngressFacts(
        run_id="run-1",
        session_id="s1",
        source="api",
        actor_id="user-1",
        execution_mode=ExecutionMode.REALTIME,
        trusted_claims={"gateway.platform": "feishu", "tenant": "t1"},
    )
    snapshot = factory.create(ingress, _descriptor())

    assert snapshot.run_context.trusted_claims["gateway.platform"] == "feishu"
    assert snapshot.run_context.trusted_claims["tenant"] == "t1"


def test_untrusted_metadata_does_not_leak_into_snapshot():
    provider = FakePolicyProfileProvider(profile=_system_profile())
    factory = RunPolicySnapshotFactory(provider)

    # Server-verified facts only
    ingress = IngressFacts(
        run_id="run-1",
        session_id="s1",
        source="api",
        actor_id="verified-user",
        execution_mode=ExecutionMode.REALTIME,
        trusted_claims={"gateway.platform": "feishu"},
    )

    # IngressFacts has no field for untrusted request-body metadata
    assert not hasattr(ingress, "metadata")
    assert not hasattr(ingress, "untrusted")

    snapshot = factory.create(ingress, _descriptor())

    # Snapshot reflects only server-verified facts
    assert snapshot.run_context.source == "api"
    assert snapshot.run_context.actor_id == "verified-user"
    assert snapshot.run_context.execution_mode == ExecutionMode.REALTIME
    assert dict(snapshot.run_context.trusted_claims) == {"gateway.platform": "feishu"}


def test_ingress_facts_trusted_claims_is_immutable():
    original: dict[str, Any] = {"role": "agent"}
    ingress = IngressFacts(
        run_id="run-1",
        session_id="s1",
        source="api",
        actor_id=None,
        execution_mode=ExecutionMode.REALTIME,
        trusted_claims=original,
    )
    # Mutating original dict does not affect IngressFacts
    original["role"] = "admin"
    assert ingress.trusted_claims["role"] == "agent"

    # Stored mapping is immutable
    with pytest.raises(TypeError):
        ingress.trusted_claims["role"] = "admin"  # type: ignore[index]


def test_factory_does_not_hold_settings():
    provider = FakePolicyProfileProvider(profile=_system_profile())
    factory = RunPolicySnapshotFactory(provider)

    assert not hasattr(factory, "_settings")
    assert not hasattr(factory, "settings")


def test_factory_works_with_non_system_profile():
    tenant_profile = ResolvedPolicyProfile(
        version="tenant-1-v1",
        turn=TurnPolicyConfig(iteration_limit=5, turn_timeout_seconds=600),
        context=ContextPolicyConfig(),
        llm=LLMPolicyConfig(),
        tool=ToolPolicyConfig(),
        memory=MemoryPolicyConfig(),
        sandbox=SandboxPolicyConfig(),
        gateway=GatewayPolicyConfig(),
        schedule=SchedulePolicyConfig(),
        budget=BudgetPolicyConfig(),
        information_flow=InformationFlowPolicyConfig(),
    )
    provider = FakePolicyProfileProvider(profile=tenant_profile, scope_ref="tenant-1")
    factory = RunPolicySnapshotFactory(provider)

    snapshot = factory.create(_ingress(), _descriptor())

    assert snapshot.profile_version == "tenant-1-v1"
    assert snapshot.turn.iteration_limit == 5
    assert snapshot.turn.turn_timeout_seconds == 600

    # Factory called resolve exactly once, without reading Settings
    assert len(provider.resolve_calls) == 1


def test_snapshot_has_all_ten_typed_configs():
    provider = FakePolicyProfileProvider(profile=_system_profile())
    factory = RunPolicySnapshotFactory(provider)

    snapshot = factory.create(_ingress(), _descriptor())

    assert isinstance(snapshot.turn, TurnPolicyConfig)
    assert isinstance(snapshot.context_config, ContextPolicyConfig)
    assert isinstance(snapshot.llm, LLMPolicyConfig)
    assert isinstance(snapshot.tool, ToolPolicyConfig)
    assert isinstance(snapshot.memory, MemoryPolicyConfig)
    assert isinstance(snapshot.sandbox, SandboxPolicyConfig)
    assert isinstance(snapshot.gateway, GatewayPolicyConfig)
    assert isinstance(snapshot.schedule, SchedulePolicyConfig)
    assert isinstance(snapshot.budget, BudgetPolicyConfig)
    assert isinstance(snapshot.information_flow, InformationFlowPolicyConfig)


def test_snapshot_profile_version_from_provider():
    provider = FakePolicyProfileProvider(profile=_system_profile())
    factory = RunPolicySnapshotFactory(provider)

    snapshot = factory.create(_ingress(), _descriptor())

    assert snapshot.profile_version == "system-v1"


def test_snapshot_run_context_built_from_ingress():
    provider = FakePolicyProfileProvider(profile=_system_profile())
    factory = RunPolicySnapshotFactory(provider)

    ingress = _ingress()
    snapshot = factory.create(ingress, _descriptor())

    ctx = snapshot.run_context
    assert ctx.run_id == "run-1"
    assert ctx.session_id == "s1"
    assert ctx.source == "api"
    assert ctx.actor_id == "user-1"
    assert ctx.execution_mode == ExecutionMode.REALTIME
    assert ctx.policy_scope == "system"


def test_snapshot_is_frozen():
    provider = FakePolicyProfileProvider(profile=_system_profile())
    factory = RunPolicySnapshotFactory(provider)

    snapshot = factory.create(_ingress(), _descriptor())

    with pytest.raises(FrozenInstanceError):
        snapshot.turn = TurnPolicyConfig()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.profile_version = "other"  # type: ignore[misc]


def test_typed_configs_are_frozen():
    config = TurnPolicyConfig()
    with pytest.raises(FrozenInstanceError):
        config.iteration_limit = 5  # type: ignore[misc]


def test_settings_profile_provider_reads_settings_at_resolve_time():
    settings = Settings()
    provider = SettingsPolicyProfileProvider(settings)

    profile = provider.resolve(
        "system",
        PolicyProfileFacts(
            source="api",
            execution_mode=ExecutionMode.REALTIME,
            descriptor_source="api",
        ),
    )

    assert profile.version == "system-v1"
    assert profile.turn.iteration_limit == 10
    assert profile.turn.turn_timeout_seconds == 900
    assert profile.context.context_length == 32000
    assert profile.context.compression_threshold == 0.50
    assert profile.context.compression_target_ratio == 0.20
    assert profile.context.protect_first_n == 3
    assert profile.context.protect_last_n == 10
    assert profile.context.cooldown_seconds == 300
    assert profile.context.tail_budget_enabled is False
    assert profile.llm.fallback_enabled is False
    assert profile.memory.cross_session_read_enabled is False
    assert profile.memory.unattended_write_enabled is False
    assert profile.sandbox.timeout_seconds == 300
    assert profile.sandbox.max_tool_calls == 50
    assert profile.sandbox.cpus == 1.0
    assert profile.sandbox.memory_mb == 512
    assert profile.sandbox.network_enabled is False
    assert profile.sandbox.idle_seconds == 900
    assert profile.sandbox.workspace_readonly is True
    assert profile.gateway.enabled is True
    assert profile.gateway.confirmation_ttl_seconds == 900
    assert profile.gateway.require_actor_for_managed_actions is True
    assert profile.schedule.tick_seconds == 30
    assert profile.schedule.max_due_per_tick == 5
    assert profile.schedule.missed_grace_seconds == 300
    assert profile.schedule.lease_seconds == 900
    assert profile.budget.max_wall_seconds == 900
    assert profile.budget.max_llm_calls == 10
    assert profile.budget.max_tool_calls == 100
    assert profile.budget.max_token_cost is None
    assert profile.budget.max_usd_cost is None
    assert profile.budget.max_sandbox_seconds is None
    assert profile.budget.max_sandbox_cpu_seconds is None
    assert profile.budget.max_sandbox_memory_mb_seconds is None
    assert profile.budget.max_sandbox_callback_calls is None
    assert profile.information_flow.log_llm_payloads is False
    assert profile.information_flow.store_usage_payloads is True
    assert profile.information_flow.redact_secrets is True


def test_policy_profile_provider_protocol_conformance():
    provider: PolicyProfileProvider = FakePolicyProfileProvider(profile=_system_profile())
    assert hasattr(provider, "resolve")

from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.application.policy_dashboard_service import (
    PolicyDashboardError,
    PolicyDashboardService,
    _POLICY_METADATA,
    _normalize_value,
)
from app.application.policy_snapshot import (
    BudgetPolicyConfig,
    ContextPolicyConfig,
    GatewayPolicyConfig,
    InformationFlowPolicyConfig,
    LLMPolicyConfig,
    MemoryPolicyConfig,
    ResolvedPolicyProfile,
    SandboxPolicyConfig,
    SchedulePolicyConfig,
    ToolPolicyConfig,
    TurnPolicyConfig,
)
from app.domain.policy import ExecutionMode


class FakeProvider:
    def __init__(self, profile):
        self._profile = profile
        self.calls: list = []

    def resolve(self, scope_ref, facts):
        self.calls.append((scope_ref, facts))
        return self._profile


def _profile() -> ResolvedPolicyProfile:
    return ResolvedPolicyProfile(
        version="test-v9",
        turn=TurnPolicyConfig(iteration_limit=10, turn_timeout_seconds=900),
        context=ContextPolicyConfig(),
        llm=LLMPolicyConfig(fallback_enabled=False),
        tool=ToolPolicyConfig(),
        memory=MemoryPolicyConfig(),
        sandbox=SandboxPolicyConfig(),
        gateway=GatewayPolicyConfig(),
        schedule=SchedulePolicyConfig(),
        budget=BudgetPolicyConfig(max_usd_cost=Decimal("0.5")),
        information_flow=InformationFlowPolicyConfig(),
    )


def service(profile=None) -> tuple[PolicyDashboardService, FakeProvider]:
    provider = FakeProvider(profile or _profile())
    return PolicyDashboardService(provider), provider


def test_top_level_shape_and_version_from_profile():
    svc, _ = service()
    data = svc.list_policies()
    assert set(data.keys()) == {"profile_version", "policies"}
    assert data["profile_version"] == "test-v9"
    assert len(data["policies"]) == 10


def test_fixed_key_order():
    svc, _ = service()
    data = svc.list_policies()
    keys = [p["key"] for p in data["policies"]]
    assert keys == [
        "turn", "context", "llm", "tool", "memory",
        "sandbox", "gateway", "schedule", "budget", "information_flow",
    ]
    assert len(set(keys)) == 10


def test_policy_view_field_contract():
    svc, _ = service()
    data = svc.list_policies()
    for p in data["policies"]:
        assert set(p.keys()) == {
            "key", "name", "display_name", "dimension", "execution_point", "domain_file", "config",
        }
        for c in p["config"]:
            assert set(c.keys()) == {"key", "label", "value"}


def test_turn_values_and_spec_metadata_alignment():
    svc, _ = service()
    data = svc.list_policies()
    turn = data["policies"][0]
    assert turn["display_name"] == "轮次策略"
    assert turn["dimension"] == "迭代上限、结束原因"
    assert turn["execution_point"] == "AgentGraph 路由"
    assert turn["domain_file"] == "turn_policy.py"
    assert [c["key"] for c in turn["config"]] == ["iteration_limit", "turn_timeout_seconds"]
    assert turn["config"][0] == {"key": "iteration_limit", "label": "迭代上限", "value": 10}
    # Tool keeps its own version field inside config
    tool = next(p for p in data["policies"] if p["key"] == "tool")
    assert tool["config"][0]["key"] == "version"
    # metadata entry count matches the 10 spec policies
    assert len(_POLICY_METADATA) == 10


def test_schedule_displays_tool_source_type_constraint():
    # Scheduled tasks run unattended -> safe_only tool exposure, which blocks
    # source_type=AGENT tools (preventing scheduler self-recursion). This
    # source_type constraint must be surfaced in the Schedule sector.
    svc, _ = service()
    data = svc.list_policies()
    schedule = next(p for p in data["policies"] if p["key"] == "schedule")
    cfg = {c["key"]: c for c in schedule["config"]}
    assert "unattended_blocked_source_type" in cfg
    assert cfg["unattended_blocked_source_type"]["value"] == "agent"
    assert cfg["unattended_blocked_source_type"]["label"] == "无人值守屏蔽工具来源"


def test_config_field_order_matches_dataclass_declaration():
    svc, _ = service()
    data = svc.list_policies()
    import dataclasses
    profile = _profile()
    for p in data["policies"]:
        meta = next(m for m in _POLICY_METADATA if m["key"] == p["key"])
        config_obj = getattr(profile, meta["attr"])
        expected_order = [f.name for f in dataclasses.fields(config_obj)]
        assert [c["key"] for c in p["config"]] == expected_order


def test_bool_preserved_and_not_degraded_to_int():
    svc, _ = service()
    data = svc.list_policies()
    llm = next(p for p in data["policies"] if p["key"] == "llm")
    cfg = {c["key"]: c["value"] for c in llm["config"]}
    assert cfg["fallback_enabled"] is False
    assert isinstance(cfg["fallback_enabled"], bool)


def test_decimal_non_scientific_string_and_null_preserved():
    svc, _ = service()
    data = svc.list_policies()
    budget = next(p for p in data["policies"] if p["key"] == "budget")
    cfg = {c["key"]: c["value"] for c in budget["config"]}
    assert cfg["max_usd_cost"] == "0.5"
    assert "E" not in str(cfg["max_usd_cost"]).lower()
    assert cfg["max_token_cost"] is None
    assert cfg["max_sandbox_seconds"] is None


def test_re_resolves_each_call_with_system_facts():
    svc, provider = service()
    svc.list_policies()
    svc.list_policies()
    assert len(provider.calls) == 2
    scope_ref, facts = provider.calls[0]
    assert scope_ref == "system"
    assert facts.source == "system"
    assert facts.execution_mode is ExecutionMode.REALTIME
    assert facts.descriptor_source == "system"


def test_input_profile_not_mutated():
    profile = _profile()
    svc, _ = service(profile)
    svc.list_policies()
    assert profile.version == "test-v9"
    assert profile.turn.iteration_limit == 10
    assert profile.turn.turn_timeout_seconds == 900


def test_constructor_only_takes_provider():
    # Service must not accept Settings/store/runtime services; only the provider.
    import inspect
    params = list(inspect.signature(PolicyDashboardService.__init__).parameters)
    assert params == ["self", "profile_provider"]


@pytest.mark.parametrize("bad", [
    float("nan"),
    float("inf"),
    float("-inf"),
])
def test_normalize_rejects_non_finite_float(bad):
    with pytest.raises(PolicyDashboardError):
        _normalize_value(bad)


def test_normalize_rejects_unsupported_types():
    with pytest.raises(PolicyDashboardError):
        _normalize_value([1, 2])
    with pytest.raises(PolicyDashboardError):
        _normalize_value({"a": 1})
    with pytest.raises(PolicyDashboardError):
        _normalize_value(object())


def test_normalize_decimal_uses_non_scientific_format():
    assert _normalize_value(Decimal("1E-7")) == "0.0000001"
    assert _normalize_value(Decimal("0.500")) == "0.500"
    assert _normalize_value(Decimal("1")) == "1"


def test_normalize_preserves_primitives():
    assert _normalize_value(True) is True
    assert _normalize_value(False) is False
    assert _normalize_value(3) == 3
    assert _normalize_value(3.5) == 3.5
    assert _normalize_value("x") == "x"
    assert _normalize_value(None) is None


def _patch_metadata(monkeypatch, new_meta):
    monkeypatch.setattr(
        "app.application.policy_dashboard_service._POLICY_METADATA", tuple(new_meta)
    )


def test_validation_rejects_wrong_count(monkeypatch):
    meta = list(_POLICY_METADATA)
    _patch_metadata(monkeypatch, meta[:9])
    svc, _ = service()
    with pytest.raises(PolicyDashboardError):
        svc.list_policies()


def test_validation_rejects_duplicate_key(monkeypatch):
    meta = [dict(m) for m in _POLICY_METADATA]
    meta[1] = dict(meta[1]); meta[1]["key"] = "turn"  # duplicate key, attr kept
    _patch_metadata(monkeypatch, meta)
    svc, _ = service()
    with pytest.raises(PolicyDashboardError):
        svc.list_policies()


def test_validation_rejects_attr_mismatch(monkeypatch):
    meta = [dict(m) for m in _POLICY_METADATA]
    meta[0] = dict(meta[0]); meta[0]["attr"] = "does_not_exist"
    _patch_metadata(monkeypatch, meta)
    svc, _ = service()
    with pytest.raises(PolicyDashboardError):
        svc.list_policies()


def test_validation_rejects_label_mismatch(monkeypatch):
    meta = [dict(m) for m in _POLICY_METADATA]
    meta[0] = dict(meta[0]); meta[0]["labels"] = {**meta[0]["labels"], "extra_field": "x"}
    _patch_metadata(monkeypatch, meta)
    svc, _ = service()
    with pytest.raises(PolicyDashboardError):
        svc.list_policies()


def test_validation_rejects_missing_label(monkeypatch):
    meta = [dict(m) for m in _POLICY_METADATA]
    labels = dict(meta[0]["labels"]); del labels["turn_timeout_seconds"]
    meta[0] = dict(meta[0]); meta[0]["labels"] = labels
    _patch_metadata(monkeypatch, meta)
    svc, _ = service()
    with pytest.raises(PolicyDashboardError):
        svc.list_policies()


def test_validation_rejects_wrong_meta_field_set(monkeypatch):
    meta = [dict(m) for m in _POLICY_METADATA]
    bad = dict(meta[0]); bad["unexpected"] = "x"
    meta[0] = bad
    _patch_metadata(monkeypatch, meta)
    svc, _ = service()
    with pytest.raises(PolicyDashboardError):
        svc.list_policies()

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_default_policy_values():
    s = Settings()
    # Turn
    assert s.agent_turn_timeout_seconds == 900
    # LLM
    assert s.llm_fallback_enabled is False
    # Memory
    assert s.memory_cross_session_read_enabled is False
    assert s.memory_unattended_write_enabled is False
    # Budget
    assert s.budget_max_wall_seconds == 900
    assert s.budget_max_llm_calls == 10
    assert s.budget_max_tool_calls == 100
    assert s.budget_max_token_cost is None
    assert s.budget_max_usd_cost is None
    # Budget -- Sandbox cumulative
    assert s.budget_max_sandbox_seconds is None
    assert s.budget_max_sandbox_cpu_seconds is None
    assert s.budget_max_sandbox_memory_mb_seconds is None
    assert s.budget_max_sandbox_callback_calls is None
    # Gateway
    assert s.gateway_confirmation_ttl_seconds == 900
    assert s.gateway_require_actor_for_managed_actions is True
    # InformationFlow
    assert s.information_log_llm_payloads is False
    assert s.information_store_usage_payloads is True
    assert s.information_redact_secrets is True


def test_gt_zero_fields_reject_zero_and_negative():
    for field_name in (
        "agent_turn_timeout_seconds",
        "budget_max_wall_seconds",
        "budget_max_llm_calls",
        "budget_max_tool_calls",
        "gateway_confirmation_ttl_seconds",
    ):
        with pytest.raises(ValidationError):
            Settings(**{field_name: 0})
        with pytest.raises(ValidationError):
            Settings(**{field_name: -1})


def test_budget_max_token_cost_accepts_none_and_non_negative():
    assert Settings(budget_max_token_cost=None).budget_max_token_cost is None
    assert Settings(budget_max_token_cost=0).budget_max_token_cost == 0
    assert Settings(budget_max_token_cost=100).budget_max_token_cost == 100
    with pytest.raises(ValidationError):
        Settings(budget_max_token_cost=-1)


def test_budget_max_usd_cost_accepts_none_and_non_negative():
    assert Settings(budget_max_usd_cost=None).budget_max_usd_cost is None
    assert Settings(budget_max_usd_cost=Decimal("1.50")).budget_max_usd_cost == Decimal("1.50")
    with pytest.raises(ValidationError):
        Settings(budget_max_usd_cost=Decimal("-0.01"))


def test_nullable_sandbox_cumulative_fields_accept_none_and_non_negative():
    fields: dict[str, float | int] = {
        "budget_max_sandbox_seconds": 10.5,
        "budget_max_sandbox_cpu_seconds": 100.0,
        "budget_max_sandbox_memory_mb_seconds": 5000.0,
        "budget_max_sandbox_callback_calls": 50,
    }
    for field_name, positive_val in fields.items():
        assert Settings(**{field_name: None}).model_dump()[field_name] is None
        assert Settings(**{field_name: positive_val}).model_dump()[field_name] == positive_val
        with pytest.raises(ValidationError):
            Settings(**{field_name: -1})


def test_boolean_fields_accept_true_and_false():
    assert Settings(llm_fallback_enabled=True).llm_fallback_enabled is True
    assert Settings(memory_cross_session_read_enabled=True).memory_cross_session_read_enabled is True
    assert Settings(memory_unattended_write_enabled=True).memory_unattended_write_enabled is True
    assert Settings(information_log_llm_payloads=True).information_log_llm_payloads is True
    assert Settings(information_store_usage_payloads=False).information_store_usage_payloads is False
    assert Settings(information_redact_secrets=False).information_redact_secrets is False
    assert Settings(gateway_require_actor_for_managed_actions=False).gateway_require_actor_for_managed_actions is False

# app/application/delegation_policy_config.py
"""DelegationPolicyConfig -- the 11th typed Policy config (Application Layer).

A frozen configuration *snapshot* capturing Delegation subsystem Settings at
resolve-time. Lives in Application (not Domain) because it is transport-level
config, not a domain concept. The Domain ``DelegationPolicy`` receives needed
config *values* via its own ``DelegationPolicyRequest``; the Application mapper
unpacks values from this typed config into the Domain Request. Domain never
imports this class.

Only scalar projections needed for execution are stored here.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DelegationPolicyConfig:
    """Immutable snapshot of Delegation subsystem configuration.

    Fields mirror the corresponding ``N_AGENT_DELEGATION_*`` Settings; the
    ``SettingsPolicyProfileProvider`` constructs this from Settings at
    resolve-time. All numeric fields are positive (validated at Settings
    construction via pydantic ``gt``/``ge``).
    """

    enabled: bool = False
    realtime_enabled: bool = False
    task_enabled: bool = False
    max_children: int = 8
    max_concurrency: int = 8
    max_concurrency_per_parent: int = 3
    max_runtime_seconds: int = 1800
    member_max_runtime_seconds: int = 900
    max_total_tokens: int = 100000
    max_tokens_per_child: int = 50000
    result_max_bytes: int = 65536
    structured_result_max_bytes: int = 32768
    event_payload_max_bytes: int = 32768
    member_max_retries: int = 1
    cancel_retry_max_attempts: int = 5
    cancel_retry_max_backoff_seconds: int = 60

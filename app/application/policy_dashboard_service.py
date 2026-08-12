from __future__ import annotations

import dataclasses
import math
from decimal import Decimal
from typing import Any

from app.application.policy_snapshot import (
    PolicyProfileFacts,
    PolicyProfileProvider,
    ResolvedPolicyProfile,
)
from app.domain.policy import ExecutionMode


class PolicyDashboardError(Exception):
    """Raised when the system policy profile cannot be projected to a dashboard view."""


# Static presentation metadata. Mirrors .harness/knowledge/02-architecture.md
# Policy Mesh table. Lives in Application (UI projection), not Domain: Domain
# stays pure and never carries Chinese display strings. Each entry declares the
# policy key, the ResolvedPolicyProfile attribute, the Policy class name, the
# Chinese display name, governance dimension, execution point, Domain file, and
# a label map (dataclass field name -> Chinese label) covering every field of
# the corresponding typed config.
_POLICY_METADATA: tuple[dict[str, Any], ...] = (
    {"key": "turn", "attr": "turn", "name": "TurnPolicy", "display_name": "轮次策略",
     "dimension": "迭代上限、结束原因", "execution_point": "AgentGraph 路由",
     "domain_file": "turn_policy.py",
     "labels": {"iteration_limit": "迭代上限", "turn_timeout_seconds": "轮次超时（秒）"}},
    {"key": "context", "attr": "context", "name": "ContextPolicy", "display_name": "上下文策略",
     "dimension": "上下文压缩阈值与保护段", "execution_point": "ContextService",
     "domain_file": "context_policy.py",
     "labels": {"context_length": "上下文长度", "compression_threshold": "压缩阈值",
                "compression_target_ratio": "压缩目标比", "protect_first_n": "保护头部",
                "protect_last_n": "保护尾部", "cooldown_seconds": "防抖冷却（秒）",
                "tail_budget_enabled": "尾部预算"}},
    {"key": "llm", "attr": "llm", "name": "LLMPolicy", "display_name": "LLM 策略",
     "dimension": "LLM fallback、vision preflight", "execution_point": "ModelService.call_llm",
     "domain_file": "llm_policy.py",
     "labels": {"fallback_enabled": "Fallback 启用"}},
    {"key": "tool", "attr": "tool", "name": "ToolPolicy", "display_name": "工具策略",
     "dimension": "工具定义校验、暴露、执行审批", "execution_point": "ToolService.execute",
     "domain_file": "tool_policy.py",
     "labels": {"version": "版本"}},
    {"key": "memory", "attr": "memory", "name": "MemoryPolicy", "display_name": "记忆策略",
     "dimension": "读写、跨会话、外部记忆门控", "execution_point": "RuntimeMemoryService",
     "domain_file": "memory_policy.py",
     "labels": {"cross_session_read_enabled": "跨会话读", "unattended_write_enabled": "无人值守写"}},
    {"key": "sandbox", "attr": "sandbox", "name": "SandboxPolicy", "display_name": "沙盒策略",
     "dimension": "CPU/内存/时间/callback 授权",
     "execution_point": "SandboxToolExecutor、TerminalToolExecutor",
     "domain_file": "sandbox_policy.py",
     "labels": {"timeout_seconds": "超时（秒）", "max_tool_calls": "最大工具调用", "cpus": "CPU",
                "memory_mb": "内存（MB）", "network_enabled": "网络",
                "idle_seconds": "空闲回收（秒）", "workspace_readonly": "工作区只读"}},
    {"key": "gateway", "attr": "gateway", "name": "GatewayPolicy", "display_name": "网关策略",
     "dimension": "出站消息目标与内容", "execution_point": "GatewayService",
     "domain_file": "gateway_policy.py",
     "labels": {"enabled": "启用", "confirmation_ttl_seconds": "确认TTL（秒）",
                "require_actor_for_managed_actions": "托管动作需 actor"}},
    {"key": "schedule", "attr": "schedule", "name": "SchedulePolicy", "display_name": "调度策略",
     "dimension": "cron 安全、claim 原子性、投递", "execution_point": "ScheduleRunService",
     "domain_file": "schedule_policy.py",
     "labels": {"tick_seconds": "轮询（秒）", "max_due_per_tick": "单轮最大到期",
                "missed_grace_seconds": "错过宽限（秒）", "lease_seconds": "租约（秒）",
                "unattended_blocked_source_type": "无人值守屏蔽工具来源"}},
    {"key": "budget", "attr": "budget", "name": "BudgetPolicy", "display_name": "预算策略",
     "dimension": "LLM/Tool/Sandbox 调用配额", "execution_point": "BudgetService",
     "domain_file": "budget_policy.py",
     "labels": {"max_wall_seconds": "最大墙钟（秒）", "max_llm_calls": "最大LLM调用",
                "max_tool_calls": "最大工具调用", "max_token_cost": "最大Token",
                "max_usd_cost": "最大USD", "max_sandbox_seconds": "沙盒秒",
                "max_sandbox_cpu_seconds": "沙盒CPU秒",
                "max_sandbox_memory_mb_seconds": "沙盒内存MB秒",
                "max_sandbox_callback_calls": "沙盒回调数"}},
    {"key": "information_flow", "attr": "information_flow",
     "name": "InformationFlowPolicy", "display_name": "信息流策略",
     "dimension": "密级、释放目标、脱敏", "execution_point": "InformationFlowService",
     "domain_file": "information_flow_policy.py",
     "labels": {"log_llm_payloads": "记录LLM载荷", "store_usage_payloads": "存储Usage载荷",
                "redact_secrets": "脱敏密钥"}},
    {"key": "delegation", "attr": "delegation",
     "name": "DelegationPolicy", "display_name": "委派策略",
     "dimension": "多 Agent 委派并发、深度、预算、取消恢复",
     "execution_point": "DelegationService、DelegationRunService",
     "domain_file": "delegation_policy.py",
     "labels": {"enabled": "启用", "realtime_enabled": "Realtime 启用",
                "task_enabled": "Task 启用", "max_children": "最大子 Agent",
                "max_concurrency": "全局并发", "max_concurrency_per_parent": "单父并发",
                "max_runtime_seconds": "最大运行（秒）",
                "member_max_runtime_seconds": "成员最大运行（秒）",
                "max_total_tokens": "总 Token 上限",
                "max_tokens_per_child": "单子 Token 上限",
                "result_max_bytes": "结果字节上限",
                "structured_result_max_bytes": "结构化结果字节上限",
                "event_payload_max_bytes": "事件载荷字节上限",
                "member_max_retries": "成员最大重试",
                "cancel_retry_max_attempts": "取消重试次数",
                "cancel_retry_max_backoff_seconds": "取消退避（秒）"}},
)

_REQUIRED_META_FIELDS = frozenset(
    {"key", "attr", "name", "display_name", "dimension", "execution_point", "domain_file", "labels"}
)


class PolicyDashboardService:
    """Projects the system policy profile into a read-only dashboard view.

    Holds only a ``PolicyProfileProvider`` (never Settings, MemoryStore or any
    runtime service). Resolves the profile on every call -- no caching -- to
    preserve ``SettingsPolicyProfileProvider``'s resolve-time read semantics.
    """

    def __init__(self, profile_provider: PolicyProfileProvider) -> None:
        self._provider = profile_provider

    def list_policies(self) -> dict[str, object]:
        facts = PolicyProfileFacts(
            source="system",
            execution_mode=ExecutionMode.REALTIME,
            descriptor_source="system",
        )
        profile = self._provider.resolve("system", facts)
        self._validate_metadata(profile)
        policies = [self._policy_view(profile, meta) for meta in _POLICY_METADATA]
        return {"profile_version": profile.version, "policies": policies}

    def _validate_metadata(self, profile: ResolvedPolicyProfile) -> None:
        if len(_POLICY_METADATA) != 11:
            raise PolicyDashboardError("policy metadata must contain exactly 11 entries")
        keys = [m["key"] for m in _POLICY_METADATA]
        attrs = [m["attr"] for m in _POLICY_METADATA]
        if len(set(keys)) != 11 or len(set(attrs)) != 11:
            raise PolicyDashboardError("policy metadata key/attr must be unique")
        for meta in _POLICY_METADATA:
            if set(meta.keys()) != _REQUIRED_META_FIELDS:
                raise PolicyDashboardError("policy metadata entry has wrong field set")
            if not isinstance(meta["labels"], dict) or not meta["labels"]:
                raise PolicyDashboardError("policy metadata labels must be a non-empty dict")
        profile_config_attrs = {
            f.name for f in dataclasses.fields(profile) if f.name != "version"
        }
        if set(attrs) != profile_config_attrs:
            raise PolicyDashboardError(
                "policy metadata attrs do not match ResolvedPolicyProfile config attrs"
            )

    def _policy_view(self, profile: ResolvedPolicyProfile, meta: dict[str, Any]) -> dict[str, Any]:
        config_obj = getattr(profile, meta["attr"])
        if not dataclasses.is_dataclass(config_obj):
            raise PolicyDashboardError(f"config for {meta['key']} is not a dataclass")
        labels: dict[str, str] = meta["labels"]
        field_names = [f.name for f in dataclasses.fields(config_obj)]
        if set(field_names) != set(labels):
            raise PolicyDashboardError(f"label set mismatch for {meta['key']}")
        config_items: list[dict[str, Any]] = []
        seen_cfg_keys: set[str] = set()
        for fname in field_names:
            if fname not in labels:
                raise PolicyDashboardError(f"missing label for {meta['key']}.{fname}")
            if fname in seen_cfg_keys:
                raise PolicyDashboardError(f"duplicate config key {meta['key']}.{fname}")
            seen_cfg_keys.add(fname)
            config_items.append({
                "key": fname,
                "label": labels[fname],
                "value": _normalize_value(getattr(config_obj, fname)),
            })
        return {
            "key": meta["key"],
            "name": meta["name"],
            "display_name": meta["display_name"],
            "dimension": meta["dimension"],
            "execution_point": meta["execution_point"],
            "domain_file": meta["domain_file"],
            "config": config_items,
        }


def _normalize_value(value: Any) -> Any:
    # bool must be checked before int: bool is a subclass of int.
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PolicyDashboardError("non-finite float config value")
        return value
    if isinstance(value, str):
        return value
    if value is None:
        return None
    raise PolicyDashboardError(f"unsupported config value type: {type(value).__name__}")

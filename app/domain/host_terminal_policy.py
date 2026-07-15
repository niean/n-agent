"""Fail-closed, IO-free policy for normalized host-terminal requests."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import TypeAlias

from app.domain.host_terminal import (
    HostCommandTarget,
    HostSkillScriptTarget,
    HostTerminalExecutionLimits,
    HostTerminalTarget,
    _require_canonical_executable,
    _require_lower_sha256,
    _require_relative_script_path,
    _require_skill_name,
    _require_text,
)
from app.domain.policy import PolicyOutcome


_SUPPORTED_SCHEMA_VERSION = 1
_SHELL_METACHARACTERS = re.compile(r"[\s#;&|<>`$(){}\[\]*?!~\\\"']")


def _valid_argument(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and "\x00" not in value
        and "\n" not in value
        and "\r" not in value
        and _SHELL_METACHARACTERS.search(value) is None
    )


def host_terminal_arguments_allowed(
    args: object, limits: "HostTerminalResourceLimits"
) -> bool:
    """Validate argv admission against one immutable Domain limit set."""
    if not isinstance(limits, HostTerminalResourceLimits):
        return False
    if not isinstance(args, (list, tuple)):
        return False
    return (
        len(args) <= limits.max_args
        and all(_valid_argument(arg) for arg in args)
        and all(len(arg) <= limits.max_arg_length for arg in args)
        and sum(len(arg) for arg in args) <= limits.max_total_args_length
    )


def _require_rule_argument(value: str) -> None:
    if not _valid_argument(value):
        raise ValueError("invalid_rule_argument")


@dataclass(frozen=True)
class HostExactArgRule:
    value: str

    def __post_init__(self) -> None:
        _require_rule_argument(self.value)

    def matches(self, value: str) -> bool:
        return value == self.value


@dataclass(frozen=True)
class HostOneOfArgRule:
    values: tuple[str, ...]

    def __post_init__(self) -> None:
        values = tuple(self.values)
        if not values or len(set(values)) != len(values):
            raise ValueError("invalid_one_of_argument_rule")
        for value in values:
            _require_rule_argument(value)
        object.__setattr__(self, "values", values)

    def matches(self, value: str) -> bool:
        return value in self.values


HostPositionalArgRule: TypeAlias = HostExactArgRule | HostOneOfArgRule


def _freeze_positional_rules(
    rules: tuple[HostPositionalArgRule, ...],
) -> tuple[HostPositionalArgRule, ...]:
    result = tuple(rules)
    if any(not isinstance(rule, (HostExactArgRule, HostOneOfArgRule)) for rule in result):
        raise ValueError("unknown_positional_argument_rule")
    return result


@dataclass(frozen=True)
class HostCommandRule:
    rule_id: str
    executable: str
    positional_args: tuple[HostPositionalArgRule, ...]

    def __post_init__(self) -> None:
        _require_text(self.rule_id, "rule_id")
        _require_canonical_executable(self.executable)
        object.__setattr__(
            self, "positional_args", _freeze_positional_rules(self.positional_args)
        )


@dataclass(frozen=True)
class HostSkillScriptRule:
    rule_id: str
    skill_name: str
    script_relative_path: str
    sha256: str
    positional_args: tuple[HostPositionalArgRule, ...]

    def __post_init__(self) -> None:
        _require_text(self.rule_id, "rule_id")
        _require_skill_name(self.skill_name)
        _require_relative_script_path(self.script_relative_path)
        _require_lower_sha256(self.sha256, "sha256")
        object.__setattr__(
            self, "positional_args", _freeze_positional_rules(self.positional_args)
        )


HostTerminalRule: TypeAlias = HostCommandRule | HostSkillScriptRule


@dataclass(frozen=True)
class HostTerminalResourceLimits:
    default_timeout_seconds: int
    max_timeout_seconds: int
    max_stdout_bytes: int
    max_stderr_bytes: int
    max_args: int
    max_arg_length: int
    max_total_args_length: int
    max_concurrency: int

    def __post_init__(self) -> None:
        for name in (
            "default_timeout_seconds",
            "max_timeout_seconds",
            "max_stdout_bytes",
            "max_stderr_bytes",
            "max_arg_length",
            "max_total_args_length",
            "max_concurrency",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"invalid_{name}")
        if (
            isinstance(self.max_args, bool)
            or not isinstance(self.max_args, int)
            or self.max_args < 0
        ):
            raise ValueError("invalid_max_args")
        if self.default_timeout_seconds > self.max_timeout_seconds:
            raise ValueError("default_timeout_exceeds_maximum")


@dataclass(frozen=True)
class HostTerminalPolicySnapshot:
    schema_version: int
    version: str
    content_digest: str
    loaded_at: datetime
    limits: HostTerminalResourceLimits
    command_rules: tuple[HostCommandRule, ...] = ()
    skill_script_rules: tuple[HostSkillScriptRule, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != _SUPPORTED_SCHEMA_VERSION:
            raise ValueError("unsupported_schema_version")
        _require_text(self.version, "policy_version")
        _require_lower_sha256(self.content_digest, "content_digest")
        if not isinstance(self.loaded_at, datetime) or self.loaded_at.tzinfo is None:
            raise ValueError("invalid_loaded_at")
        if not isinstance(self.limits, HostTerminalResourceLimits):
            raise ValueError("invalid_resource_limits")

        command_rules = tuple(self.command_rules)
        skill_rules = tuple(self.skill_script_rules)
        if any(not isinstance(rule, HostCommandRule) for rule in command_rules):
            raise ValueError("unknown_command_rule")
        if any(not isinstance(rule, HostSkillScriptRule) for rule in skill_rules):
            raise ValueError("unknown_skill_script_rule")
        object.__setattr__(self, "command_rules", command_rules)
        object.__setattr__(self, "skill_script_rules", skill_rules)

        all_rules: tuple[HostTerminalRule, ...] = command_rules + skill_rules
        rule_ids = [rule.rule_id for rule in all_rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("duplicate_rule_id")

        target_keys = [self._target_key(rule) for rule in all_rules]
        if len(target_keys) != len(set(target_keys)):
            raise ValueError("duplicate_target")

        for rule in all_rules:
            if len(rule.positional_args) > self.limits.max_args:
                raise ValueError("rule_arguments_exceed_limits")
            values = (
                (arg.value,)
                if isinstance(arg, HostExactArgRule)
                else arg.values
                for arg in rule.positional_args
            )
            flattened = tuple(value for group in values for value in group)
            if any(len(value) > self.limits.max_arg_length for value in flattened):
                raise ValueError("rule_arguments_exceed_limits")
            maximum_total_length = sum(
                len(arg.value)
                if isinstance(arg, HostExactArgRule)
                else max(len(value) for value in arg.values)
                for arg in rule.positional_args
            )
            if maximum_total_length > self.limits.max_total_args_length:
                raise ValueError("rule_arguments_exceed_limits")

    @staticmethod
    def _target_key(rule: HostTerminalRule) -> tuple[object, ...]:
        args_key: tuple[object, ...] = tuple(
            ("exact", arg.value)
            if isinstance(arg, HostExactArgRule)
            else (
                ("exact", arg.values[0])
                if len(arg.values) == 1
                else ("one_of", tuple(sorted(arg.values)))
            )
            for arg in rule.positional_args
        )
        if isinstance(rule, HostCommandRule):
            return ("command", rule.executable, args_key)
        return (
            "skill_script",
            rule.skill_name,
            rule.script_relative_path,
            rule.sha256,
            args_key,
        )

    @property
    def rules(self) -> tuple[HostTerminalRule, ...]:
        return self.command_rules + self.skill_script_rules


@dataclass(frozen=True)
class HostTerminalPolicyRequest:
    """Normalized facts; path resolution and hashing happen before this boundary."""

    target: HostTerminalTarget
    requested_limits: HostTerminalExecutionLimits


@dataclass(frozen=True)
class HostTerminalPolicyDecision:
    outcome: PolicyOutcome
    reason: str
    rule_id: str | None
    policy_version: str
    effective_limits: HostTerminalExecutionLimits | None

    def __post_init__(self) -> None:
        if self.outcome not in (PolicyOutcome.ALLOW, PolicyOutcome.DENY):
            raise ValueError("invalid_host_policy_outcome")
        _require_text(self.reason, "policy_reason")

    @property
    def allowed(self) -> bool:
        return self.outcome is PolicyOutcome.ALLOW


class HostTerminalPolicy:
    """Exact allowlist policy. It performs no IO and never mutates its snapshot."""

    def __init__(self, snapshot: HostTerminalPolicySnapshot) -> None:
        self._snapshot = snapshot

    @property
    def snapshot(self) -> HostTerminalPolicySnapshot:
        return self._snapshot

    def evaluate(self, request: HostTerminalPolicyRequest) -> HostTerminalPolicyDecision:
        if not isinstance(request.target, (HostCommandTarget, HostSkillScriptTarget)):
            return self._deny("host_target_not_allowed")
        if not self._snapshot.rules:
            return self._deny("host_policy_empty")
        if not isinstance(request.requested_limits, HostTerminalExecutionLimits):
            return self._deny("host_resource_limits_exceeded")
        if not self._resources_allowed(request.requested_limits):
            return self._deny("host_resource_limits_exceeded")
        if not self._arguments_allowed(request.target.args):
            return self._deny("host_arguments_invalid")

        for rule in self._snapshot.rules:
            if self._matches(rule, request.target):
                return HostTerminalPolicyDecision(
                    outcome=PolicyOutcome.ALLOW,
                    reason="host_target_allowed",
                    rule_id=rule.rule_id,
                    policy_version=self._snapshot.version,
                    effective_limits=request.requested_limits,
                )
        return self._deny("host_target_not_allowed")

    def _resources_allowed(self, requested: HostTerminalExecutionLimits) -> bool:
        limits = self._snapshot.limits
        return (
            requested.timeout_seconds <= limits.max_timeout_seconds
            and requested.max_stdout_bytes <= limits.max_stdout_bytes
            and requested.max_stderr_bytes <= limits.max_stderr_bytes
            and requested.max_concurrency <= limits.max_concurrency
        )

    def _arguments_allowed(self, args: tuple[str, ...]) -> bool:
        return host_terminal_arguments_allowed(args, self._snapshot.limits)

    @staticmethod
    def _matches(rule: HostTerminalRule, target: HostTerminalTarget) -> bool:
        if isinstance(rule, HostCommandRule) and isinstance(target, HostCommandTarget):
            identity_matches = rule.executable == target.executable
        elif isinstance(rule, HostSkillScriptRule) and isinstance(
            target, HostSkillScriptTarget
        ):
            identity_matches = (
                rule.skill_name == target.skill_name
                and rule.script_relative_path == target.script_relative_path
                and rule.sha256 == target.sha256
            )
        else:
            return False
        return identity_matches and len(rule.positional_args) == len(target.args) and all(
            arg_rule.matches(value)
            for arg_rule, value in zip(rule.positional_args, target.args, strict=True)
        )

    def _deny(self, reason: str) -> HostTerminalPolicyDecision:
        return HostTerminalPolicyDecision(
            outcome=PolicyOutcome.DENY,
            reason=reason,
            rule_id=None,
            policy_version=self._snapshot.version,
            effective_limits=None,
        )

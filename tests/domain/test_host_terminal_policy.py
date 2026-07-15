from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from app.domain.host_terminal import (
    HostCommandTarget,
    HostSkillScriptTarget,
    HostTerminalBridgeRequest,
    HostTerminalBridgeResponse,
    HostTerminalExecutionLimits,
    HostTerminalStatus,
)
from app.domain.host_terminal_policy import (
    HostCommandRule,
    HostExactArgRule,
    HostOneOfArgRule,
    HostSkillScriptRule,
    HostTerminalPolicy,
    HostTerminalPolicyRequest,
    HostTerminalPolicySnapshot,
    HostTerminalResourceLimits,
    host_terminal_arguments_allowed,
)
from app.domain.policy import PolicyOutcome


SCRIPT_HASH = "a" * 64
CONTENT_DIGEST = "b" * 64


def _limits(**overrides: int) -> HostTerminalResourceLimits:
    values = {
        "default_timeout_seconds": 15,
        "max_timeout_seconds": 30,
        "max_stdout_bytes": 4096,
        "max_stderr_bytes": 2048,
        "max_args": 4,
        "max_arg_length": 32,
        "max_total_args_length": 64,
        "max_concurrency": 1,
    }
    values.update(overrides)
    return HostTerminalResourceLimits(**values)


def _requested(**overrides: int) -> HostTerminalExecutionLimits:
    values = {
        "timeout_seconds": 15,
        "max_stdout_bytes": 4096,
        "max_stderr_bytes": 2048,
        "max_concurrency": 1,
    }
    values.update(overrides)
    return HostTerminalExecutionLimits(**values)


def _snapshot(
    *,
    command_rules: tuple[HostCommandRule, ...] = (),
    skill_rules: tuple[HostSkillScriptRule, ...] = (),
    limits: HostTerminalResourceLimits | None = None,
) -> HostTerminalPolicySnapshot:
    return HostTerminalPolicySnapshot(
        schema_version=1,
        version="policy-v1",
        content_digest=CONTENT_DIGEST,
        loaded_at=datetime(2026, 7, 15, tzinfo=UTC),
        limits=limits or _limits(),
        command_rules=command_rules,
        skill_script_rules=skill_rules,
    )


def _request(target: object, **limit_overrides: int) -> HostTerminalPolicyRequest:
    return HostTerminalPolicyRequest(
        target=target,  # type: ignore[arg-type]
        requested_limits=_requested(**limit_overrides),
    )


def test_target_types_are_mutually_exclusive_value_objects() -> None:
    command = HostCommandTarget(executable="/usr/bin/true", args=())
    script = HostSkillScriptTarget(
        skill_name="photo-and-upload",
        script_relative_path="scripts/photo-upload.py",
        sha256=SCRIPT_HASH,
        args=(),
    )

    assert not hasattr(command, "skill_name")
    assert not hasattr(script, "executable")
    with pytest.raises(TypeError):
        HostCommandTarget(  # type: ignore[call-arg]
            executable="/usr/bin/true", args=(), skill_name="mixed"
        )


def test_empty_and_unknown_policy_default_deny() -> None:
    policy = HostTerminalPolicy(_snapshot())

    empty = policy.evaluate(_request(HostCommandTarget("/usr/bin/true", ())))
    unknown = policy.evaluate(_request(object()))

    assert empty.outcome is PolicyOutcome.DENY
    assert empty.reason == "host_policy_empty"
    assert unknown.outcome is PolicyOutcome.DENY
    assert unknown.reason == "host_target_not_allowed"


def test_command_requires_exact_executable_args_and_resource_bounds() -> None:
    rule = HostCommandRule(
        rule_id="diagnostic",
        executable="/usr/bin/printf",
        positional_args=(
            HostExactArgRule("--format"),
            HostOneOfArgRule(("short", "long")),
        ),
    )
    policy = HostTerminalPolicy(_snapshot(command_rules=(rule,)))

    allowed = policy.evaluate(
        _request(HostCommandTarget("/usr/bin/printf", ("--format", "short")))
    )
    wrong_executable = policy.evaluate(
        _request(HostCommandTarget("/bin/printf", ("--format", "short")))
    )
    wrong_arg = policy.evaluate(
        _request(HostCommandTarget("/usr/bin/printf", ("--format", "wide")))
    )
    extra_arg = policy.evaluate(
        _request(HostCommandTarget("/usr/bin/printf", ("--format", "short", "x")))
    )
    too_long = policy.evaluate(
        _request(
            HostCommandTarget("/usr/bin/printf", ("--format", "short")),
            timeout_seconds=31,
        )
    )

    assert allowed.outcome is PolicyOutcome.ALLOW
    assert allowed.reason == "host_target_allowed"
    assert allowed.rule_id == "diagnostic"
    assert allowed.policy_version == "policy-v1"
    assert allowed.effective_limits == _requested()
    assert wrong_executable.outcome is PolicyOutcome.DENY
    assert wrong_arg.outcome is PolicyOutcome.DENY
    assert extra_arg.outcome is PolicyOutcome.DENY
    assert too_long.reason == "host_resource_limits_exceeded"


def test_skill_rule_matches_exact_skill_path_hash_and_args() -> None:
    rule = HostSkillScriptRule(
        rule_id="photo",
        skill_name="photo-and-upload",
        script_relative_path="scripts/photo-upload.py",
        sha256=SCRIPT_HASH,
        positional_args=(),
    )
    policy = HostTerminalPolicy(_snapshot(skill_rules=(rule,)))
    target = HostSkillScriptTarget(
        "photo-and-upload", "scripts/photo-upload.py", SCRIPT_HASH, ()
    )

    assert policy.evaluate(_request(target)).outcome is PolicyOutcome.ALLOW
    for mismatch in (
        HostSkillScriptTarget("other", target.script_relative_path, SCRIPT_HASH, ()),
        HostSkillScriptTarget(target.skill_name, "scripts/other.py", SCRIPT_HASH, ()),
        HostSkillScriptTarget(target.skill_name, target.script_relative_path, "c" * 64, ()),
    ):
        assert policy.evaluate(_request(mismatch)).outcome is PolicyOutcome.DENY


@pytest.mark.parametrize("sha256", ["A" * 64, "a" * 63, "g" * 64, ""])
def test_skill_hash_must_be_lowercase_sha256(sha256: str) -> None:
    with pytest.raises(ValueError):
        HostSkillScriptTarget("skill", "scripts/run.py", sha256, ())


@pytest.mark.parametrize(
    "path", ["", "/scripts/run.py", "./run.py", "scripts/../run.py", "scripts\\run.py"]
)
def test_skill_path_must_be_normalized_posix_relative(path: str) -> None:
    with pytest.raises(ValueError):
        HostSkillScriptTarget("skill", path, SCRIPT_HASH, ())


@pytest.mark.parametrize("skill_name", [".", ".."])
def test_skill_target_rejects_dot_path_segment_names(skill_name: str) -> None:
    with pytest.raises(ValueError, match="invalid_skill_name"):
        HostSkillScriptTarget(skill_name, "scripts/run.py", SCRIPT_HASH, ())


@pytest.mark.parametrize("skill_name", [".", ".."])
def test_skill_rule_rejects_dot_path_segment_names(skill_name: str) -> None:
    with pytest.raises(ValueError, match="invalid_skill_name"):
        HostSkillScriptRule(
            "skill-rule", skill_name, "scripts/run.py", SCRIPT_HASH, ()
        )


def test_duplicate_rule_ids_or_targets_invalidate_snapshot() -> None:
    first = HostCommandRule("same", "/usr/bin/true", ())
    same_id = HostCommandRule("same", "/usr/bin/false", ())
    same_target = HostCommandRule("other", "/usr/bin/true", ())

    with pytest.raises(ValueError, match="duplicate_rule_id"):
        _snapshot(command_rules=(first, same_id))
    with pytest.raises(ValueError, match="duplicate_target"):
        _snapshot(command_rules=(first, same_target))


def test_duplicate_target_canonicalizes_one_of_value_order() -> None:
    first = HostCommandRule(
        "first", "/usr/bin/printf", (HostOneOfArgRule(("short", "long")),)
    )
    reordered = HostCommandRule(
        "second", "/usr/bin/printf", (HostOneOfArgRule(("long", "short")),)
    )

    with pytest.raises(ValueError, match="duplicate_target"):
        _snapshot(command_rules=(first, reordered))


def test_duplicate_target_treats_singleton_one_of_as_exact() -> None:
    exact = HostCommandRule(
        "exact", "/usr/bin/printf", (HostExactArgRule("short"),)
    )
    singleton = HostCommandRule(
        "singleton", "/usr/bin/printf", (HostOneOfArgRule(("short",)),)
    )

    with pytest.raises(ValueError, match="duplicate_target"):
        _snapshot(command_rules=(exact, singleton))


@pytest.mark.parametrize(
    "args",
    [
        ("bad\x00arg",),
        ("bad\narg",),
        ("bad;arg",),
        ("bad arg",),
        ("bad\targ",),
        ("bad#arg",),
        ("a" * 33,),
        ("a", "b", "c", "d", "e"),
        ("a" * 32, "b" * 32, "c"),
    ],
)
def test_invalid_or_oversized_args_are_denied(args: tuple[str, ...]) -> None:
    rule = HostCommandRule("args", "/usr/bin/printf", ())
    outcome = HostTerminalPolicy(_snapshot(command_rules=(rule,))).evaluate(
        _request(HostCommandTarget("/usr/bin/printf", args))
    )
    assert outcome.outcome is PolicyOutcome.DENY
    assert outcome.reason == "host_arguments_invalid"


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ((), True),
        (("safe-value",), True),
        (("bad value",), False),
        (("bad#value",), False),
        (("a" * 33,), False),
        (("a", "b", "c", "d", "e"), False),
        (("a" * 32, "b" * 32, "c"), False),
        (["safe-value"], True),
        ("safe-value", False),
        ((1,), False),
    ],
)
def test_argument_admission_helper_is_the_domain_source_of_truth(
    args: object, expected: bool
) -> None:
    assert host_terminal_arguments_allowed(args, _limits()) is expected


@pytest.mark.parametrize("value", ["bad arg", "bad\targ", "bad#arg"])
def test_policy_arg_rules_reject_shell_token_or_comment_characters(value: str) -> None:
    with pytest.raises(ValueError, match="invalid_rule_argument"):
        HostExactArgRule(value)
    with pytest.raises(ValueError, match="invalid_rule_argument"):
        HostOneOfArgRule(("safe", value))


def test_bridge_request_requires_typed_execution_limits() -> None:
    with pytest.raises(ValueError, match="invalid_execution_limits"):
        HostTerminalBridgeRequest(
            protocol_version="1",
            request_id="request-1",
            target=HostCommandTarget("/usr/bin/true", ()),
            n_agent_policy_version="policy-v1",
            n_agent_content_digest=CONTENT_DIGEST,
            limits=object(),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("duration_ms", [True, -1, 1.5])
def test_bridge_response_rejects_invalid_duration(duration_ms: object) -> None:
    with pytest.raises(ValueError, match="invalid_duration_ms"):
        HostTerminalBridgeResponse(
            protocol_version="1",
            request_id="request-1",
            status=HostTerminalStatus.SUCCESS,
            exit_code=0,
            stdout="",
            stderr="",
            duration_ms=duration_ms,  # type: ignore[arg-type]
            stdout_truncated=False,
            stderr_truncated=False,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"timeout_seconds": 31},
        {"max_stdout_bytes": 4097},
        {"max_stderr_bytes": 2049},
        {"max_concurrency": 2},
    ],
)
def test_requested_resource_bounds_are_enforced(overrides: dict[str, int]) -> None:
    rule = HostCommandRule("true", "/usr/bin/true", ())
    decision = HostTerminalPolicy(_snapshot(command_rules=(rule,))).evaluate(
        _request(HostCommandTarget("/usr/bin/true", ()), **overrides)
    )
    assert decision.outcome is PolicyOutcome.DENY
    assert decision.reason == "host_resource_limits_exceeded"


def test_snapshot_version_digest_and_nested_collections_are_immutable() -> None:
    mutable_args = [HostExactArgRule("ok")]
    rule = HostCommandRule("echo", "/bin/echo", mutable_args)  # type: ignore[arg-type]
    mutable_rules = [rule]
    snapshot = _snapshot(command_rules=mutable_rules)  # type: ignore[arg-type]
    mutable_args.append(HostExactArgRule("changed"))
    mutable_rules.clear()

    assert snapshot.schema_version == 1
    assert snapshot.version == "policy-v1"
    assert snapshot.content_digest == CONTENT_DIGEST
    assert len(snapshot.command_rules) == 1
    assert len(snapshot.command_rules[0].positional_args) == 1
    with pytest.raises(FrozenInstanceError):
        snapshot.version = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("version", ""),
        ("content_digest", "not-a-digest"),
    ],
)
def test_snapshot_rejects_unknown_version_or_invalid_identity(
    field: str, value: object
) -> None:
    values = {
        "schema_version": 1,
        "version": "policy-v1",
        "content_digest": CONTENT_DIGEST,
        "loaded_at": datetime(2026, 7, 15, tzinfo=UTC),
        "limits": _limits(),
    }
    values[field] = value
    with pytest.raises(ValueError):
        HostTerminalPolicySnapshot(**values)  # type: ignore[arg-type]

from dataclasses import replace, FrozenInstanceError
import pytest
import importlib


def grant_module():
    return importlib.import_module("app.domain.host_script_grant")


from tests.domain.test_host_terminal_policy import _snapshot, _request
from app.domain.host_terminal import HostSkillScriptTarget
from app.domain.host_terminal_policy import HostSkillScriptRule, HostTerminalPolicy


def grant(**kw):
    return grant_module().HostScriptGrant(
        **(
            dict(
                skill_name="camera",
                script_relative_path="scripts/run.py",
                sha256="a" * 64,
                revision=1,
                origin="foreground",
                updated_at="2026-11-06T00:00:00Z",
                state="active",
            )
            | kw
        )
    )


def test_validated_immutable_roundtrip():
    g = grant()
    assert grant_module().HostScriptGrant.from_dict(g.to_dict()) == g
    with pytest.raises(FrozenInstanceError):
        g.revision = 3
    for kw in [
        dict(revision=True),
        dict(revision=0),
        dict(origin="external"),
        dict(state="x"),
        dict(updated_at="yesterday"),
        dict(script_relative_path="../run.py"),
        dict(script_relative_path="run.sh"),
    ]:
        with pytest.raises(ValueError):
            grant(**kw)
    with pytest.raises(ValueError):
        grant_module().HostScriptGrant.from_dict(g.to_dict() | {"extra": 1})


def test_merge_replaces_manual_only_active_and_bounds_args():
    manual = HostSkillScriptRule("manual", "camera", "scripts/run.py", "b" * 64, ())
    base = _snapshot(skill_rules=(manual,))
    merged = grant_module().merge_script_grants(base, [grant()])
    assert merged.content_digest == base.content_digest
    assert len(merged.skill_script_rules) == 1
    assert merged.skill_script_rules[0].allow_any_args
    policy = HostTerminalPolicy(merged)
    assert policy.evaluate(
        _request(
            HostSkillScriptTarget(
                "camera", "scripts/run.py", "a" * 64, ("hello world", "--flag=$x")
            )
        )
    ).allowed
    assert not policy.evaluate(
        _request(
            HostSkillScriptTarget("camera", "scripts/run.py", "a" * 64, ("x" * 33,))
        )
    ).allowed
    assert grant_module().merge_script_grants(base, [grant(state="revoked")]) == base

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.domain.curator_policy import CuratorPolicy, CuratorPolicyRequest
from app.domain.policy import PolicyOutcome
from app.domain.skill import SkillSource


def _req(**overrides):
    base = dict(
        name="deploy-staging",
        source=SkillSource.AGENT,
        state="active",
        pinned=False,
        is_protected_seed=False,
        prune_seeds=False,
        action="transition",
    )
    base.update(overrides)
    return CuratorPolicyRequest(**base)


def test_agent_created_allow():
    policy = CuratorPolicy()
    assert policy.evaluate(_req()) == PolicyOutcome.ALLOW


def test_protected_seed_deny():
    policy = CuratorPolicy()
    assert policy.evaluate(_req(is_protected_seed=True)) == PolicyOutcome.DENY


def test_pinned_deny():
    policy = CuratorPolicy()
    assert policy.evaluate(_req(pinned=True)) == PolicyOutcome.DENY


def test_seed_without_prune_seeds_deny():
    policy = CuratorPolicy()
    assert (
        policy.evaluate(_req(source=SkillSource.SEED, prune_seeds=False))
        == PolicyOutcome.DENY
    )


def test_seed_with_prune_seeds_allow():
    policy = CuratorPolicy()
    assert (
        policy.evaluate(
            _req(source=SkillSource.SEED, prune_seeds=True, is_protected_seed=False)
        )
        == PolicyOutcome.ALLOW
    )


def test_protected_seed_deny_even_with_prune_seeds():
    policy = CuratorPolicy()
    assert (
        policy.evaluate(
            _req(source=SkillSource.SEED, prune_seeds=True, is_protected_seed=True)
        )
        == PolicyOutcome.DENY
    )


def test_archived_non_restore_deny():
    policy = CuratorPolicy()
    assert (
        policy.evaluate(_req(state="archived", action="transition"))
        == PolicyOutcome.DENY
    )


def test_archived_restore_allow():
    policy = CuratorPolicy()
    assert (
        policy.evaluate(_req(state="archived", action="restore"))
        == PolicyOutcome.ALLOW
    )


def test_stale_transition_allow():
    policy = CuratorPolicy()
    assert (
        policy.evaluate(_req(state="stale", action="transition"))
        == PolicyOutcome.ALLOW
    )


def test_evaluate_accepts_context_none():
    """evaluate 签名对齐 Policy Protocol（带 context=None）。"""
    policy = CuratorPolicy()
    assert policy.evaluate(_req(), context=None) == PolicyOutcome.ALLOW


def test_user_source_deny():
    """user-created skill 不做自动迁移（归属治理留给 skill CLI/Dashboard）。"""
    policy = CuratorPolicy()
    assert policy.evaluate(_req(source=SkillSource.USER)) == PolicyOutcome.DENY


def test_source_none_allow():
    policy = CuratorPolicy()
    assert policy.evaluate(_req(source=None)) == PolicyOutcome.ALLOW

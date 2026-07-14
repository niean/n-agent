"""Domain tests for SandboxPolicy deny/clamp decision table.

Tests the pure-domain decision logic:
- DENY: network requested but snapshot denies; out-of-bounds mount; writable
  workspace when config mandates readonly; unallowed backend; non-positive quota.
- CLAMP: timeout/CPU/memory/pids/output-bytes exceeding snapshot max -> clamp
  down to the max.
- CALLBACKS: requested ∩ registry-enabled ∩ snapshot allowlist.  Removed
  callbacks are audited by NAME ONLY (no arguments).
"""
from __future__ import annotations

import time

import pytest

from app.domain.policy import PolicyOutcome
from app.domain.sandbox_policy import (
    SandboxDomainConfig,
    SandboxExecutionGrant,
    SandboxMountAccess,
    SandboxMountSpec,
    SandboxPolicy,
    SandboxPolicyRequest,
    SandboxResourceSpec,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(**overrides) -> SandboxDomainConfig:
    defaults = dict(
        timeout_seconds=300,
        max_tool_calls=50,
        cpus=2.0,
        memory_mb=1024,
        network_enabled=False,
        idle_seconds=900,
        workspace_readonly=True,
        max_stdout_bytes=50000,
        max_stderr_bytes=10000,
        pids_limit=256,
        allowed_backends=frozenset({"docker", "local"}),
        allowed_callbacks=frozenset({"web_search", "read_file", "write_file"}),
    )
    defaults.update(overrides)
    return SandboxDomainConfig(**defaults)


def _resources(**overrides) -> SandboxResourceSpec:
    defaults = dict(
        timeout_seconds=60,
        cpus=1.0,
        memory_mb=512,
        pids=128,
        max_stdout_bytes=5000,
        max_stderr_bytes=1000,
    )
    defaults.update(overrides)
    return SandboxResourceSpec(**defaults)


def _request(
    *,
    operation: str = "execute_code",
    backend: str = "docker",
    network: bool = False,
    mounts: tuple[SandboxMountSpec, ...] = (
        SandboxMountSpec(target="/workspace", access=SandboxMountAccess.READONLY),
        SandboxMountSpec(target="/scratch", access=SandboxMountAccess.READWRITE),
    ),
    requested_callbacks: frozenset[str] = frozenset({"web_search", "read_file"}),
    registry_enabled_callbacks: frozenset[str] = frozenset({"web_search", "read_file", "write_file"}),
    resources: SandboxResourceSpec | None = None,
) -> SandboxPolicyRequest:
    return SandboxPolicyRequest(
        operation=operation,
        backend=backend,
        network=network,
        mounts=mounts,
        requested_callbacks=requested_callbacks,
        registry_enabled_callbacks=registry_enabled_callbacks,
        resources=resources or _resources(),
    )


# ---------------------------------------------------------------------------
# DENY cases
# ---------------------------------------------------------------------------


class TestDenyNetwork:
    def test_deny_when_network_requested_but_config_disables(self):
        cfg = _config(network_enabled=False)
        req = _request(network=True)
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.verdict is PolicyOutcome.DENY
        assert "network" in grant.reason

    def test_allow_when_network_not_requested_and_config_disables(self):
        cfg = _config(network_enabled=False)
        req = _request(network=False)
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.verdict is PolicyOutcome.ALLOW
        assert grant.network is False


class TestDenyOutOfBoundsMount:
    def test_deny_mount_outside_workspace_or_scratch(self):
        cfg = _config()
        req = _request(mounts=(
            SandboxMountSpec(target="/etc", access=SandboxMountAccess.READONLY),
        ))
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.verdict is PolicyOutcome.DENY
        assert "mount" in grant.reason.lower()

    def test_allow_workspace_and_scratch_mounts(self):
        cfg = _config()
        req = _request()
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.verdict is PolicyOutcome.ALLOW


class TestDenyWritableWorkspace:
    def test_deny_writable_workspace_when_readonly_config(self):
        cfg = _config(workspace_readonly=True)
        req = _request(mounts=(
            SandboxMountSpec(target="/workspace", access=SandboxMountAccess.READWRITE),
            SandboxMountSpec(target="/scratch", access=SandboxMountAccess.READWRITE),
        ))
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.verdict is PolicyOutcome.DENY
        assert "workspace" in grant.reason.lower()

    def test_allow_writable_workspace_when_not_readonly_config(self):
        cfg = _config(workspace_readonly=False)
        req = _request(mounts=(
            SandboxMountSpec(target="/workspace", access=SandboxMountAccess.READWRITE),
            SandboxMountSpec(target="/scratch", access=SandboxMountAccess.READWRITE),
        ))
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.verdict is PolicyOutcome.ALLOW


class TestDenyUnallowedBackend:
    def test_deny_backend_not_in_allowed_set(self):
        cfg = _config(allowed_backends=frozenset({"docker"}))
        req = _request(backend="local")
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.verdict is PolicyOutcome.DENY
        assert "backend" in grant.reason.lower()

    def test_allow_backend_in_allowed_set(self):
        cfg = _config(allowed_backends=frozenset({"docker", "local"}))
        req = _request(backend="local")
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.verdict is PolicyOutcome.ALLOW


class TestDenyNonPositiveQuota:
    @pytest.mark.parametrize("field", ["timeout_seconds", "cpus", "memory_mb", "pids"])
    def test_deny_zero_or_negative_quota(self, field):
        cfg = _config()
        kwargs = {field: 0}
        req = _request(resources=_resources(**kwargs))
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.verdict is PolicyOutcome.DENY
        assert "quota" in grant.reason.lower() or "non-positive" in grant.reason.lower() or field in grant.reason.lower()

    def test_deny_negative_timeout(self):
        cfg = _config()
        req = _request(resources=_resources(timeout_seconds=-1))
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.verdict is PolicyOutcome.DENY


# ---------------------------------------------------------------------------
# CLAMP cases
# ---------------------------------------------------------------------------


class TestClampTimeout:
    def test_clamp_timeout_to_config_max(self):
        cfg = _config(timeout_seconds=120)
        req = _request(resources=_resources(timeout_seconds=300))
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.verdict is PolicyOutcome.ALLOW
        assert grant.resources.timeout_seconds == 120

    def test_no_clamp_when_within_config(self):
        cfg = _config(timeout_seconds=300)
        req = _request(resources=_resources(timeout_seconds=60))
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.verdict is PolicyOutcome.ALLOW
        assert grant.resources.timeout_seconds == 60


class TestClampCpu:
    def test_clamp_cpu_to_config_max(self):
        cfg = _config(cpus=1.0)
        req = _request(resources=_resources(cpus=2.0))
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.verdict is PolicyOutcome.ALLOW
        assert grant.resources.cpus == 1.0

    def test_no_clamp_when_cpu_within_config(self):
        cfg = _config(cpus=2.0)
        req = _request(resources=_resources(cpus=1.0))
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.resources.cpus == 1.0


class TestClampMemory:
    def test_clamp_memory_to_config_max(self):
        cfg = _config(memory_mb=512)
        req = _request(resources=_resources(memory_mb=1024))
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.verdict is PolicyOutcome.ALLOW
        assert grant.resources.memory_mb == 512


class TestClampPids:
    def test_clamp_pids_to_config_max(self):
        cfg = _config(pids_limit=100)
        req = _request(resources=_resources(pids=256))
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.verdict is PolicyOutcome.ALLOW
        assert grant.resources.pids == 100


class TestClampOutputBytes:
    def test_clamp_stdout_bytes(self):
        cfg = _config(max_stdout_bytes=10000)
        req = _request(resources=_resources(max_stdout_bytes=50000))
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.verdict is PolicyOutcome.ALLOW
        assert grant.resources.max_stdout_bytes == 10000

    def test_clamp_stderr_bytes(self):
        cfg = _config(max_stderr_bytes=2000)
        req = _request(resources=_resources(max_stderr_bytes=10000))
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.verdict is PolicyOutcome.ALLOW
        assert grant.resources.max_stderr_bytes == 2000


# ---------------------------------------------------------------------------
# CALLBACKS intersection
# ---------------------------------------------------------------------------


class TestCallbacksIntersection:
    def test_callbacks_are_requested_intersect_registry_intersect_allowlist(self):
        cfg = _config(allowed_callbacks=frozenset({"web_search", "read_file"}))
        req = _request(
            requested_callbacks=frozenset({"web_search", "write_file", "nonexistent"}),
            registry_enabled_callbacks=frozenset({"web_search", "read_file", "write_file"}),
        )
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.verdict is PolicyOutcome.ALLOW
        # web_search is in all three sets; write_file is in requested+registry
        # but NOT in allowlist; nonexistent is not in registry
        assert grant.callbacks == frozenset({"web_search"})

    def test_removed_callbacks_audited_by_name_only(self):
        """Removed callbacks must be recorded as names, not arguments."""
        cfg = _config(allowed_callbacks=frozenset({"web_search"}))
        req = _request(
            requested_callbacks=frozenset({"web_search", "write_file", "read_file"}),
            registry_enabled_callbacks=frozenset({"web_search", "write_file", "read_file"}),
        )
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.verdict is PolicyOutcome.ALLOW
        assert grant.callbacks == frozenset({"web_search"})
        # Removed callbacks are names only -- frozenset of str, not dict
        assert isinstance(grant.removed_callbacks, frozenset)
        assert all(isinstance(c, str) for c in grant.removed_callbacks)
        assert "write_file" in grant.removed_callbacks
        assert "read_file" in grant.removed_callbacks

    def test_no_callbacks_requested_yields_empty_set(self):
        cfg = _config(allowed_callbacks=frozenset({"web_search"}))
        req = _request(
            requested_callbacks=frozenset(),
            registry_enabled_callbacks=frozenset({"web_search"}),
        )
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.verdict is PolicyOutcome.ALLOW
        assert grant.callbacks == frozenset()

    def test_empty_allowlist_deny_all_callbacks(self):
        cfg = _config(allowed_callbacks=frozenset())
        req = _request(
            requested_callbacks=frozenset({"web_search"}),
            registry_enabled_callbacks=frozenset({"web_search"}),
        )
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.verdict is PolicyOutcome.ALLOW
        assert grant.callbacks == frozenset()
        assert "web_search" in grant.removed_callbacks


# ---------------------------------------------------------------------------
# Grant structure
# ---------------------------------------------------------------------------


class TestGrantStructure:
    def test_grant_is_frozen_dataclass(self):
        cfg = _config()
        req = _request()
        grant = SandboxPolicy(cfg).evaluate(req)
        assert isinstance(grant, SandboxExecutionGrant)
        with pytest.raises(Exception):
            grant.backend = "local"  # type: ignore[misc]

    def test_grant_carries_determined_backend(self):
        cfg = _config()
        req = _request(backend="docker")
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.backend == "docker"

    def test_grant_carries_mounts(self):
        cfg = _config()
        mounts = (
            SandboxMountSpec(target="/workspace", access=SandboxMountAccess.READONLY),
            SandboxMountSpec(target="/scratch", access=SandboxMountAccess.READWRITE),
        )
        req = _request(mounts=mounts)
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.mounts == mounts

    def test_grant_carries_network(self):
        cfg = _config(network_enabled=False)
        req = _request(network=False)
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.network is False

    def test_grant_deadline_is_positive(self):
        cfg = _config(timeout_seconds=60)
        req = _request(resources=_resources(timeout_seconds=30))
        before = time.monotonic()
        grant = SandboxPolicy(cfg).evaluate(req)
        after = time.monotonic()
        assert grant.deadline >= before
        assert grant.deadline <= after + 31  # deadline = now + clamped_timeout

    def test_grant_reason_not_empty(self):
        cfg = _config()
        req = _request()
        grant = SandboxPolicy(cfg).evaluate(req)
        assert grant.reason

"""Sandbox policy -- domain pure decision logic.

Decides whether a sandbox execution request is ALLOWed or DENYed, and clamps
resource quotas to the snapshot-configured maximums.  The Policy produces a
``SandboxExecutionGrant`` that carries the determined backend, mounts, network
mode, callback allowlist, and resource limits.  Executors consume the grant;
they do NOT read Settings for permissions.

Decision table (first-stage defaults):
- DENY: request network but config ``network_enabled=False``; out-of-bounds
  mount (target not ``/workspace`` or ``/scratch``); writable workspace when
  ``workspace_readonly=True``; backend not in ``allowed_backends``; non-positive
  quota (timeout<=0, cpu<=0, memory<=0, pids<=0).
- CLAMP: timeout/CPU/memory/pids/output-bytes exceeding the config max ->
  clamp down to the max.
- CALLBACKS: ``requested ∩ registry-enabled ∩ config.allowed_callbacks``.
  Removed callbacks are audited by NAME ONLY (no arguments).

Pure Domain: stdlib + typing + dataclasses + enum + app.domain.policy only.
No Infrastructure, no pydantic, no asyncio.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from app.domain.policy import PolicyOutcome


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SandboxMountAccess(str, Enum):
    READONLY = "readonly"
    READWRITE = "readwrite"


# ---------------------------------------------------------------------------
# Config (Domain mirror of application-level SandboxPolicyConfig)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxDomainConfig:
    """Domain-level configuration for the sandbox policy.

    This is intentionally a SUPERSET of the application-level
    ``SandboxPolicyConfig`` (7 fields).  The extra fields --
    ``max_stdout_bytes``, ``max_stderr_bytes``, ``pids_limit``,
    ``allowed_backends``, ``allowed_callbacks`` -- are sourced directly
    from ``Settings`` at assembly time (in ``main.py``), NOT via the
    per-run ``SandboxPolicyConfig`` snapshot.  The snapshot config
    captures only the per-run immutable subset (timeout, max_tool_calls,
    cpus, memory_mb, network, idle, workspace_readonly); the extra fields
    here are infrastructure-derived constants that don't vary per run.

    There is no mapper from ``SandboxPolicyConfig`` to
    ``SandboxDomainConfig``; main.py builds ``SandboxDomainConfig``
    directly from ``Settings``.  Do NOT "fix" the asymmetry by removing
    fields or assuming a mapper exists -- the design is intentional.
    """

    timeout_seconds: int = 300
    max_tool_calls: int = 50
    cpus: float = 1.0
    memory_mb: int = 512
    network_enabled: bool = False
    idle_seconds: int = 900
    workspace_readonly: bool = True
    max_stdout_bytes: int = 50000
    max_stderr_bytes: int = 10000
    pids_limit: int = 256
    allowed_backends: frozenset[str] = frozenset({"docker", "local"})
    allowed_callbacks: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# Mount spec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxMountSpec:
    target: str
    access: SandboxMountAccess


# ---------------------------------------------------------------------------
# Resource spec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxResourceSpec:
    timeout_seconds: int
    cpus: float
    memory_mb: int
    pids: int
    max_stdout_bytes: int
    max_stderr_bytes: int


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxPolicyRequest:
    """Typed request to the SandboxPolicy.

    Carries the operation type, requested backend, network flag, mounts,
    callback sets (requested + registry-enabled), and resource specs.  The
    Application mapper projects ToolPolicy/Budget facts into this type before
    calling ``SandboxPolicy.evaluate``.
    """

    operation: str
    backend: str
    network: bool
    mounts: tuple[SandboxMountSpec, ...]
    requested_callbacks: frozenset[str]
    registry_enabled_callbacks: frozenset[str]
    resources: SandboxResourceSpec


# ---------------------------------------------------------------------------
# Grant (output)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxExecutionGrant:
    """The determined execution grant.

    Carries the DETERMINED backend/mounts/network/callbacks/resources/deadline.
    Executors consume the grant; they do NOT read Settings for permissions.
    ``removed_callbacks`` records callback tool names that were requested but
    denied (audited by name only, not arguments).
    """

    verdict: PolicyOutcome
    backend: str
    mounts: tuple[SandboxMountSpec, ...]
    network: bool
    callbacks: frozenset[str]
    resources: SandboxResourceSpec
    deadline: float
    reason: str
    removed_callbacks: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("sandbox execution grant reason must not be empty")

    @property
    def allowed(self) -> bool:
        return self.verdict is PolicyOutcome.ALLOW


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


_VALID_MOUNT_TARGETS = frozenset({"/workspace", "/scratch"})


class SandboxPolicy:
    """Domain policy that decides sandbox execution grants.

    Constructed with an immutable ``SandboxDomainConfig``.  ``evaluate`` is
    pure: it inspects the request and config to produce a grant.  The Policy
    never performs IO or modifies any account.
    """

    def __init__(self, config: SandboxDomainConfig) -> None:
        self._config = config

    def evaluate(self, request: SandboxPolicyRequest) -> SandboxExecutionGrant:
        cfg = self._config

        # -- DENY: unallowed backend --
        if request.backend not in cfg.allowed_backends:
            return self._deny(
                backend=request.backend,
                reason=f"backend_not_allowed:{request.backend}",
            )

        # -- DENY: non-positive quota --
        res = request.resources
        if res.timeout_seconds <= 0:
            return self._deny(
                backend=request.backend,
                reason=f"non_positive_quota:timeout_seconds={res.timeout_seconds}",
            )
        if res.cpus <= 0:
            return self._deny(
                backend=request.backend,
                reason=f"non_positive_quota:cpus={res.cpus}",
            )
        if res.memory_mb <= 0:
            return self._deny(
                backend=request.backend,
                reason=f"non_positive_quota:memory_mb={res.memory_mb}",
            )
        if res.pids <= 0:
            return self._deny(
                backend=request.backend,
                reason=f"non_positive_quota:pids={res.pids}",
            )

        # -- DENY: network requested but config disables --
        if request.network and not cfg.network_enabled:
            return self._deny(
                backend=request.backend,
                reason="network_requested_but_disabled",
            )

        # -- DENY: out-of-bounds mount --
        for m in request.mounts:
            if m.target not in _VALID_MOUNT_TARGETS:
                return self._deny(
                    backend=request.backend,
                    reason=f"out_of_bounds_mount:{m.target}",
                )

        # -- DENY: writable workspace when config mandates readonly --
        if cfg.workspace_readonly:
            for m in request.mounts:
                if m.target == "/workspace" and m.access is SandboxMountAccess.READWRITE:
                    return self._deny(
                        backend=request.backend,
                        reason="writable_workspace_denied",
                    )

        # -- CLAMP: resource quotas to config maximums --
        clamped_timeout = min(res.timeout_seconds, cfg.timeout_seconds)
        clamped_cpus = min(res.cpus, cfg.cpus)
        clamped_memory = min(res.memory_mb, cfg.memory_mb)
        clamped_pids = min(res.pids, cfg.pids_limit)
        clamped_stdout = min(res.max_stdout_bytes, cfg.max_stdout_bytes)
        clamped_stderr = min(res.max_stderr_bytes, cfg.max_stderr_bytes)
        clamped_resources = SandboxResourceSpec(
            timeout_seconds=clamped_timeout,
            cpus=clamped_cpus,
            memory_mb=clamped_memory,
            pids=clamped_pids,
            max_stdout_bytes=clamped_stdout,
            max_stderr_bytes=clamped_stderr,
        )

        # -- Network: only allow if config permits AND request asks --
        granted_network = request.network and cfg.network_enabled

        # -- CALLBACKS: requested ∩ registry-enabled ∩ config allowlist --
        granted_callbacks = (
            request.requested_callbacks
            & request.registry_enabled_callbacks
            & cfg.allowed_callbacks
        )
        removed = (
            (request.requested_callbacks & request.registry_enabled_callbacks)
            - granted_callbacks
        )

        # -- Deadline: monotonic + clamped timeout --
        deadline = time.monotonic() + clamped_timeout

        return SandboxExecutionGrant(
            verdict=PolicyOutcome.ALLOW,
            backend=request.backend,
            mounts=request.mounts,
            network=granted_network,
            callbacks=granted_callbacks,
            resources=clamped_resources,
            deadline=deadline,
            reason="sandbox_execution_allowed",
            removed_callbacks=removed,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _deny(self, backend: str, reason: str) -> SandboxExecutionGrant:
        return SandboxExecutionGrant(
            verdict=PolicyOutcome.DENY,
            backend=backend,
            mounts=(),
            network=False,
            callbacks=frozenset(),
            resources=SandboxResourceSpec(
                timeout_seconds=0,
                cpus=0.0,
                memory_mb=0,
                pids=0,
                max_stdout_bytes=0,
                max_stderr_bytes=0,
            ),
            deadline=0.0,
            reason=reason,
            removed_callbacks=frozenset(),
        )

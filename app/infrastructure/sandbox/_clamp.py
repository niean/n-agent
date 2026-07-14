"""Shared second-layer clamp helpers for sandbox infrastructure.

Defense-in-depth: Docker/Local receive a ``SandboxExecutionGrant`` (or
grant-derived params) from the manager.  These helpers clamp the per-call
request to the grant-derived limits -- they NEVER escalate (no increasing
timeout/quota beyond what the grant allows).
"""
from __future__ import annotations

from dataclasses import replace

from app.domain.sandbox import SandboxExecutionRequest


def clamp_execution_request(
    request: SandboxExecutionRequest,
    max_timeout: int | None = None,
    allowed_callbacks: frozenset[str] | None = None,
) -> SandboxExecutionRequest:
    """Clamp a SandboxExecutionRequest to grant-derived limits.

    - timeout: if ``max_timeout`` is set and request exceeds it, clamp DOWN.
    - callbacks: if ``allowed_callbacks`` is set, intersect request with it.

    Returns the original request unchanged when no clamping is needed
    (max_timeout is None or already within bounds, allowed_callbacks is
    None or already a subset).
    """
    if max_timeout is not None and request.timeout_seconds > max_timeout:
        request = replace(request, timeout_seconds=max_timeout)
    if allowed_callbacks is not None:
        clamped = request.enabled_callback_tools & allowed_callbacks
        if clamped != request.enabled_callback_tools:
            request = replace(request, enabled_callback_tools=clamped)
    return request


def clamp_timeout(
    timeout: int,
    max_timeout: int | None = None,
) -> int:
    """Clamp a timeout value to a grant-derived maximum.

    Returns ``min(timeout, max_timeout)`` when ``max_timeout`` is set;
    returns ``timeout`` unchanged when ``max_timeout`` is None.
    """
    if max_timeout is not None and timeout > max_timeout:
        return max_timeout
    return timeout

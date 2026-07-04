"""ACP auth helpers — advertise N-Agent auth methods and validate handshake.

This module is sync-only and does not touch the network. The ACP agent (T12)
constructs a :class:`ProviderSnapshot` from ``ProviderService`` at initialize
time and passes it here; ``build_auth_methods`` only reads the snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


TERMINAL_SETUP_AUTH_METHOD_ID = "n-agent-setup"


@dataclass(frozen=True)
class ProviderSnapshot:
    """Sync snapshot of active provider state for ACP auth building.

    The ACP agent constructs this at initialize time from ``ProviderService``.
    ``name`` is ``None`` when no active provider is configured.
    """

    name: str | None
    has_api_key: bool


def build_auth_methods(holder: ProviderSnapshot | None) -> list[Any]:
    """Build the ACP auth method list from a provider snapshot.

    Does NOT perform network or async calls — only reads the sync snapshot.
    Always advertises a terminal setup method; adds an agent-managed provider
    auth method when an active provider with credentials is available.
    """
    from acp.schema import AuthMethodAgent, TerminalAuthMethod

    methods: list[Any] = []

    if holder is not None and holder.name and holder.has_api_key:
        methods.append(
            AuthMethodAgent(
                id=holder.name,
                name=f"{holder.name} runtime credentials",
                description=(
                    "Authenticate N-Agent using the currently configured "
                    f"{holder.name} runtime credentials."
                ),
            )
        )

    methods.append(
        TerminalAuthMethod(
            id=TERMINAL_SETUP_AUTH_METHOD_ID,
            name="Configure N-Agent provider",
            description=(
                "Open N-Agent's interactive provider setup in a terminal. "
                "Use this when N-Agent has not been configured on this machine yet."
            ),
            type="terminal",
            args=["acp", "--setup"],
        )
    )
    return methods


async def authenticate(method_id: str, methods: list[Any]) -> Any | None:
    """Validate the chosen auth method against advertised methods.

    Returns an :class:`AuthenticateResponse` if ``method_id`` matches an
    advertised method, ``None`` otherwise. Does NOT perform real auth — the
    ACP SDK only needs acknowledgment that the chosen method is valid.
    """
    from acp.schema import AuthenticateResponse

    advertised_ids = {m.id for m in methods}
    if method_id not in advertised_ids:
        return None
    return AuthenticateResponse()

"""Tests for ACP auth helpers (T8)."""

from __future__ import annotations

import pytest

from app.interfaces.cli.commands.acp.auth import (
    ProviderSnapshot,
    authenticate,
    build_auth_methods,
)


def test_build_auth_methods_includes_provider_auth_when_credentials_present():
    holder = ProviderSnapshot(name="ollama", has_api_key=True)
    methods = build_auth_methods(holder)

    # Should have 2 methods: provider auth + terminal setup
    assert len(methods) == 2
    ids = {m.id for m in methods}
    assert "ollama" in ids
    assert "n-agent-setup" in ids


def test_build_auth_methods_only_terminal_when_no_provider():
    methods = build_auth_methods(None)

    assert len(methods) == 1
    assert methods[0].id == "n-agent-setup"
    assert methods[0].args == ["acp", "--setup"]


def test_build_auth_methods_only_terminal_when_provider_missing_api_key():
    holder = ProviderSnapshot(name="ollama", has_api_key=False)
    methods = build_auth_methods(holder)

    assert len(methods) == 1
    assert methods[0].id == "n-agent-setup"


def test_terminal_setup_method_args_include_acp_setup():
    methods = build_auth_methods(None)
    terminal = methods[0]
    assert terminal.type == "terminal"
    assert terminal.args == ["acp", "--setup"]


@pytest.mark.asyncio
async def test_authenticate_returns_response_for_known_method():
    methods = build_auth_methods(ProviderSnapshot(name="ollama", has_api_key=True))

    result = await authenticate("ollama", methods)

    assert result is not None  # AuthenticateResponse instance


@pytest.mark.asyncio
async def test_authenticate_returns_none_for_unknown_method():
    methods = build_auth_methods(ProviderSnapshot(name="ollama", has_api_key=True))

    result = await authenticate("unknown-method", methods)

    assert result is None


@pytest.mark.asyncio
async def test_authenticate_returns_none_when_no_methods_advertised():
    # Edge case: empty methods list
    result = await authenticate("anything", [])

    assert result is None

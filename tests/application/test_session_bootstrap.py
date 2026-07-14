from __future__ import annotations

import pytest

from app.application.session_bootstrap import (
    SessionBootstrapReader,
    SessionDescriptor,
    SessionScopeMismatchError,
)
from app.domain.session import ConversationSession


class FakeMemoryStore:
    """Minimal MemoryStore fake: only implements methods the reader/tests touch."""

    def __init__(self, session: ConversationSession | None = None) -> None:
        self._session = session
        self.get_session_calls: list[str] = []
        self.list_messages_calls: list[str] = []

    async def get_session(self, session_id: str) -> ConversationSession | None:
        self.get_session_calls.append(session_id)
        return self._session

    async def list_messages(self, session_id: str) -> list:  # noqa: ANN201
        self.list_messages_calls.append(session_id)
        return []


async def test_describe_returns_descriptor_for_existing_session():
    session = ConversationSession(
        id="s1", source="api", external_memory_enabled=["builtin"],
    )
    store = FakeMemoryStore(session=session)
    reader = SessionBootstrapReader(store)

    descriptor = await reader.describe("s1", "api")

    assert descriptor == SessionDescriptor(
        id="s1", exists=True, source="api", external_memory_profile_ref=("builtin",),
    )
    assert not hasattr(descriptor, "messages")


async def test_describe_returns_provisional_descriptor_when_session_not_found():
    store = FakeMemoryStore(session=None)
    reader = SessionBootstrapReader(store)

    descriptor = await reader.describe("s1", "api")

    assert descriptor == SessionDescriptor(
        id="s1", exists=False, source="api", external_memory_profile_ref=(),
    )
    assert not hasattr(descriptor, "messages")


async def test_describe_does_not_create_session_when_not_found():
    store = FakeMemoryStore(session=None)
    reader = SessionBootstrapReader(store)

    await reader.describe("missing", "api")

    # Only get_session was called; no create_session or content reads
    assert store.get_session_calls == ["missing"]
    assert store.list_messages_calls == []


async def test_source_mismatch_raises_before_content_read():
    session = ConversationSession(
        id="s1", source="dashboard", external_memory_enabled=["builtin"],
    )
    store = FakeMemoryStore(session=session)
    reader = SessionBootstrapReader(store)

    with pytest.raises(SessionScopeMismatchError) as exc_info:
        await reader.describe("s1", "api")

    err = exc_info.value
    assert err.session_id == "s1"
    assert err.expected_source == "api"
    assert err.actual_source == "dashboard"
    assert err.code == "api_session_scope_mismatch"

    # get_session was called, but list_messages was NOT
    assert store.get_session_calls == ["s1"]
    assert store.list_messages_calls == []


async def test_source_mismatch_error_code_reflects_expected_source():
    session = ConversationSession(
        id="s2", source="api", external_memory_enabled=None,
    )
    store = FakeMemoryStore(session=session)
    reader = SessionBootstrapReader(store)

    with pytest.raises(SessionScopeMismatchError) as exc_info:
        await reader.describe("s2", "dashboard")

    assert exc_info.value.code == "dashboard_session_scope_mismatch"


async def test_external_memory_profile_ref_derived_from_session():
    session = ConversationSession(
        id="s1", source="api", external_memory_enabled=["builtin", "feishu"],
    )
    store = FakeMemoryStore(session=session)
    reader = SessionBootstrapReader(store)

    descriptor = await reader.describe("s1", "api")

    assert descriptor.external_memory_profile_ref == ("builtin", "feishu")


async def test_external_memory_profile_ref_empty_when_none():
    session = ConversationSession(
        id="s1", source="api", external_memory_enabled=None,
    )
    store = FakeMemoryStore(session=session)
    reader = SessionBootstrapReader(store)

    descriptor = await reader.describe("s1", "api")

    assert descriptor.external_memory_profile_ref == ()


async def test_external_memory_profile_ref_empty_when_empty_list():
    session = ConversationSession(
        id="s1", source="api", external_memory_enabled=[],
    )
    store = FakeMemoryStore(session=session)
    reader = SessionBootstrapReader(store)

    descriptor = await reader.describe("s1", "api")

    assert descriptor.external_memory_profile_ref == ()


def test_session_descriptor_is_frozen():
    from dataclasses import FrozenInstanceError

    descriptor = SessionDescriptor(
        id="s1", exists=True, source="api", external_memory_profile_ref=(),
    )
    with pytest.raises(FrozenInstanceError):
        descriptor.id = "s2"  # type: ignore[misc]


def test_session_descriptor_has_no_content_fields():
    descriptor = SessionDescriptor(
        id="s1", exists=False, source="api", external_memory_profile_ref=(),
    )
    assert not hasattr(descriptor, "messages")
    assert not hasattr(descriptor, "content")
    assert not hasattr(descriptor, "title")

# app/application/session_bootstrap.py
from __future__ import annotations

from dataclasses import dataclass

from app.domain.memory import MemoryStore
from app.domain.session import ConversationSession


@dataclass(frozen=True)
class SessionDescriptor:
    """Minimal, content-free projection of a session for policy bootstrap.

    Carries only the fields needed to build a RunPolicySnapshot.
    MUST NOT expose messages, title, or any conversation content --
    the architecture boundary test guarantees this.
    """

    id: str
    exists: bool
    source: str
    external_memory_profile_ref: tuple[str, ...]


class SessionScopeMismatchError(Exception):
    """Raised when an existing session's source does not match the expected source.

    The error code follows the pattern ``{expected_source}_session_scope_mismatch``
    (e.g. ``api_session_scope_mismatch``, ``dashboard_session_scope_mismatch``).
    """

    def __init__(
        self,
        session_id: str,
        expected_source: str,
        actual_source: str,
    ) -> None:
        self.session_id = session_id
        self.expected_source = expected_source
        self.actual_source = actual_source
        self.code = f"{expected_source}_session_scope_mismatch"
        super().__init__(
            f"session scope mismatch: session={session_id} "
            f"expected={expected_source} actual={actual_source}"
        )


class SessionBootstrapReader:
    """Reads a minimal SessionDescriptor without touching session content.

    Only calls ``MemoryStore.get_session`` -- never ``list_messages``,
    ``append_message``, or any content-bearing method.  When the session
    does not exist, returns a provisional descriptor instead of creating one.
    """

    def __init__(self, memory_store: MemoryStore) -> None:
        self._memory_store = memory_store

    async def describe(
        self,
        session_id: str,
        expected_source: str,
    ) -> SessionDescriptor:
        session: ConversationSession | None = await self._memory_store.get_session(
            session_id,
        )
        if session is None:
            return SessionDescriptor(
                id=session_id,
                exists=False,
                source=expected_source,
                external_memory_profile_ref=(),
            )

        if session.source != expected_source:
            raise SessionScopeMismatchError(
                session_id=session_id,
                expected_source=expected_source,
                actual_source=session.source,
            )

        ext = session.external_memory_enabled
        ext_ref = tuple(ext) if ext else ()

        return SessionDescriptor(
            id=session_id,
            exists=True,
            source=session.source,
            external_memory_profile_ref=ext_ref,
        )

    async def describe_unchecked(
        self,
        session_id: str,
        provisional_source: str,
    ) -> SessionDescriptor:
        """Read bootstrap facts after the interface has validated ownership.

        Gateway and scheduler ingress do not always use the persisted session
        source as their policy source, so snapshot construction needs a
        content-free descriptor without repeating selector validation.
        """
        session: ConversationSession | None = await self._memory_store.get_session(
            session_id,
        )
        if session is None:
            return SessionDescriptor(
                id=session_id,
                exists=False,
                source=provisional_source,
                external_memory_profile_ref=(),
            )
        ext = session.external_memory_enabled
        return SessionDescriptor(
            id=session.id,
            exists=True,
            source=session.source,
            external_memory_profile_ref=tuple(ext) if ext else (),
        )

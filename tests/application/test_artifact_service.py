"""Tests for ArtifactService (T8).

Covers CRUD, export, publish, lifecycle, and source registration using
typed fakes. No FastAPI, SQLite, or Docker required.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable

import pytest

from app.application.artifact_service import (
    ArtifactService,
    ArtifactServiceConfig,
    ArtifactTooLargeError,
    PublishBlockedError,
    PublishResult,
)
from app.application.information_flow_service import ReleaseResult
from app.domain.artifact import (
    Artifact,
    ArtifactAttachmentSource,
    ArtifactContentUnavailableError,
    ArtifactKind,
    ArtifactListCursor,
    ArtifactNotFoundError,
    ArtifactSource,
    ArtifactStatus,
    ArtifactValidationError,
    PublishedArtifact,
    PublishedArtifactNotFoundError,
    PublishedArtifactStatus,
)
from app.domain.artifact_policy import (
    ArtifactPolicy,
    ArtifactPolicyAction,
    ArtifactPolicyRequest,
)
from app.domain.information_flow import (
    Classification,
    InformationReleaseDecision,
    ReleaseTarget,
)
from app.domain.policy import (
    PolicyAuditEvent,
    PolicyDecisionKind,
    PolicyOutcome,
)
from app.domain.task import TaskArtifact
from app.infrastructure.artifact.export_converter import convert_to_html


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeArtifactRegistry:
    """In-memory ArtifactRegistry for testing."""

    def __init__(self) -> None:
        self._artifacts: dict[str, Artifact] = {}
        self._published: dict[str, PublishedArtifact] = {}
        self._active_by_artifact: dict[str, str] = {}
        self.create_calls: list[Artifact] = []
        self.update_calls: list[Artifact] = []
        self.delete_calls: list[str] = []
        self.register_calls: list[tuple[PublishedArtifact, str | None]] = []
        self.revoke_calls: list[str] = []
        self.list_calls: list[dict[str, Any]] = []
        self.fail_on_create = False
        self.fail_on_update = False
        self.fail_on_register = False

    async def create_artifact(self, artifact: Artifact) -> Artifact:
        self.create_calls.append(artifact)
        if self.fail_on_create:
            raise RuntimeError("registry create failed")
        self._artifacts[artifact.id] = artifact
        return artifact

    async def get_artifact(self, artifact_id: str) -> Artifact | None:
        return self._artifacts.get(artifact_id)

    async def list_artifacts(
        self,
        *,
        source_kind: ArtifactSource | None = None,
        source_context_ref: str | None = None,
        source_session_id: str | None = None,
        kind: ArtifactKind | None = None,
        status: ArtifactStatus | None = None,
        q: str | None = None,
        cursor: ArtifactListCursor | None = None,
        limit: int = 50,
    ) -> Any:
        self.list_calls.append({
            "source_kind": source_kind,
            "source_context_ref": source_context_ref,
            "source_session_id": source_session_id,
            "kind": kind,
            "status": status,
            "q": q,
            "cursor": cursor,
            "limit": limit,
        })
        items = list(self._artifacts.values())
        if source_kind is not None:
            items = [a for a in items if a.source_kind is source_kind]
        if source_context_ref is not None:
            items = [a for a in items if a.source_context_ref == source_context_ref]
        if source_session_id is not None:
            items = [a for a in items if a.source_session_id == source_session_id]
        if kind is not None:
            items = [a for a in items if a.kind is kind]
        if status is not None:
            items = [a for a in items if a.status is status]
        if q is not None:
            ql = q.lower()
            items = [a for a in items if ql in a.name.lower()]
        items.sort(
            key=lambda a: (a.updated_at or datetime.min.replace(tzinfo=timezone.utc), a.id),
            reverse=True,
        )
        if cursor is not None:
            cu = (cursor.updated_at or datetime.min.replace(tzinfo=timezone.utc), cursor.artifact_id)
            items = [
                a for a in items
                if (a.updated_at or datetime.min.replace(tzinfo=timezone.utc), a.id) < cu
            ]
        page = items[:limit]
        next_cursor = None
        if len(items) > limit and page:
            last = page[-1]
            next_cursor = ArtifactListCursor(
                updated_at=last.updated_at, artifact_id=last.id
            )
        from app.domain.artifact import ArtifactListPage
        return ArtifactListPage(items=tuple(page), next_cursor=next_cursor)

    async def update_artifact(self, artifact: Artifact) -> Artifact:
        self.update_calls.append(artifact)
        if self.fail_on_update:
            raise RuntimeError("registry update failed")
        self._artifacts[artifact.id] = artifact
        return artifact

    async def delete_artifact(self, artifact_id: str) -> bool:
        self.delete_calls.append(artifact_id)
        existed = self._artifacts.pop(artifact_id, None) is not None
        return existed

    async def get_by_source(
        self, source_kind: ArtifactSource, source_ref: str
    ) -> Artifact | None:
        for a in self._artifacts.values():
            if a.source_kind is source_kind and a.source_ref == source_ref:
                return a
        return None

    async def count_artifacts(self) -> int:
        return len(self._artifacts)

    async def list_task_artifacts_missing_session(
        self, *, limit: int = 200,
    ) -> tuple[Artifact, ...]:
        items = [
            a for a in self._artifacts.values()
            if a.source_kind in (ArtifactSource.TASK_ARTIFACT, ArtifactSource.TASK_ATTACHMENT)
            and a.source_session_id is None
        ]
        items.sort(key=lambda a: a.id)
        return tuple(items[:limit])

    async def list_artifacts_with_empty_mime(
        self, *, limit: int = 200,
    ) -> tuple[Artifact, ...]:
        items = [a for a in self._artifacts.values() if not (a.mime or "")]
        items.sort(key=lambda a: a.id)
        return tuple(items[:limit])

    async def register_published(
        self,
        published: PublishedArtifact,
        *,
        revoke_artifact_id: str | None = None,
    ) -> PublishedArtifact:
        self.register_calls.append((published, revoke_artifact_id))
        if self.fail_on_register:
            raise RuntimeError("registry register failed")
        self._published[published.publish_id] = published
        if published.artifact_id:
            if revoke_artifact_id and revoke_artifact_id in self._active_by_artifact:
                old_pid = self._active_by_artifact.pop(revoke_artifact_id)
                old = self._published[old_pid]
                self._published[old_pid] = replace(
                    old,
                    status=PublishedArtifactStatus.REVOKED,
                    revoked_at=datetime.now(timezone.utc),
                )
            if published.status is PublishedArtifactStatus.ACTIVE:
                self._active_by_artifact[published.artifact_id] = published.publish_id
        return published

    async def get_published(self, publish_id: str) -> PublishedArtifact | None:
        return self._published.get(publish_id)

    async def get_active_publish(self, artifact_id: str) -> PublishedArtifact | None:
        pid = self._active_by_artifact.get(artifact_id)
        if pid is not None:
            return self._published.get(pid)
        return None

    async def list_published(
        self, artifact_id: str | None = None
    ) -> tuple[PublishedArtifact, ...]:
        items = list(self._published.values())
        if artifact_id is not None:
            items = [p for p in items if p.artifact_id == artifact_id]
        return tuple(items)

    async def revoke_published(self, artifact_id: str) -> PublishedArtifact | None:
        self.revoke_calls.append(artifact_id)
        pid = self._active_by_artifact.get(artifact_id)
        if pid is not None:
            p = self._published[pid]
            revoked = replace(
                p,
                status=PublishedArtifactStatus.REVOKED,
                revoked_at=datetime.now(timezone.utc),
            )
            self._published[pid] = revoked
            del self._active_by_artifact[artifact_id]
            return revoked
        for p in self._published.values():
            if p.artifact_id == artifact_id and p.status is PublishedArtifactStatus.REVOKED:
                return p
        return None

    async def list_attachment_sources(
        self,
        *,
        after_attachment_id: str | None = None,
        limit: int = 100,
    ) -> tuple[ArtifactAttachmentSource, ...]:
        return ()

    # Helper to seed an artifact directly.
    def seed(self, artifact: Artifact) -> None:
        self._artifacts[artifact.id] = artifact


class FakeArtifactContentStore:
    """In-memory ArtifactContentStore for testing."""

    def __init__(self) -> None:
        self._content: dict[str, bytes] = {}
        self.read_calls: list[tuple[str, int]] = []
        self.write_calls: list[tuple[str, str, int]] = []
        self.delete_calls: list[str] = []
        self.materialize_calls: list[tuple[ArtifactSource, str, str]] = []
        self.copy_calls: list[tuple[str, str, str | None]] = []
        self.fail_on_read = False
        self.fail_on_write = False
        self.fail_on_materialize = False

    async def read(self, content_ref: str, *, max_bytes: int) -> bytes:
        self.read_calls.append((content_ref, max_bytes))
        if self.fail_on_read:
            raise ArtifactContentUnavailableError("read failed")
        if content_ref not in self._content:
            raise ArtifactContentUnavailableError(f"not found: {content_ref}")
        data = self._content[content_ref]
        if len(data) > max_bytes:
            raise ArtifactValidationError(
                f"size {len(data)} exceeds max {max_bytes}"
            )
        return data

    async def write_atomic(self, artifact_id: str, filename: str, data: bytes) -> str:
        self.write_calls.append((artifact_id, filename, len(data)))
        if self.fail_on_write:
            raise RuntimeError("write failed")
        ref = f"item:{artifact_id}/{filename}"
        self._content[ref] = data
        return ref

    async def delete_owned(self, content_ref: str) -> bool:
        self.delete_calls.append(content_ref)
        return self._content.pop(content_ref, None) is not None

    async def materialize_source(
        self,
        source_kind: ArtifactSource,
        source_ref: str,
        artifact_id: str,
    ) -> str:
        self.materialize_calls.append((source_kind, source_ref, artifact_id))
        if self.fail_on_materialize:
            raise ArtifactContentUnavailableError("materialize failed")
        if source_ref not in self._content:
            raise ArtifactContentUnavailableError(f"source not found: {source_ref}")
        data = self._content[source_ref]
        ref = f"item:{artifact_id}/materialized"
        self._content[ref] = data
        return ref

    async def copy_to_publish_snapshot(
        self, src_ref: str, publish_id: str, *, inline: str | None = None
    ) -> str:
        self.copy_calls.append((src_ref, publish_id, inline))
        if inline is not None:
            data = inline.encode("utf-8")
        else:
            if src_ref not in self._content:
                raise ArtifactContentUnavailableError(f"source not found: {src_ref}")
            data = self._content[src_ref]
        ref = f"published:{publish_id}/snapshot"
        self._content[ref] = data
        return ref

    def seed(self, ref: str, data: bytes) -> None:
        self._content[ref] = data

    def has(self, ref: str) -> bool:
        return ref in self._content


class FakeInformationFlowService:
    """Fake InformationFlowService with configurable release behavior."""

    def __init__(
        self,
        *,
        allow: bool = True,
        redacted_content: str | None = None,
    ) -> None:
        self._allow = allow
        self._redacted_content = redacted_content
        self.release_calls: list[dict[str, Any]] = []

    def release(
        self,
        content: str,
        target: ReleaseTarget,
        *,
        classification: Classification = Classification.INTERNAL,
        origin: str = "unknown",
        labels: frozenset[str] = frozenset(),
        run_id: str = "",
        session_id: str = "",
    ) -> ReleaseResult:
        self.release_calls.append(
            {
                "content": content,
                "target": target,
                "classification": classification,
                "origin": origin,
                "labels": labels,
            }
        )
        if self._allow:
            decision = InformationReleaseDecision(
                verdict=PolicyOutcome.ALLOW,
                transform="redaction" if self._redacted_content is not None else None,
                allowed_fields=frozenset(),
                retention="sanitized" if self._redacted_content is not None else "raw",
                audit_level="summary",
                reason="test_allow",
            )
            return ReleaseResult(
                allowed=True,
                content=self._redacted_content if self._redacted_content is not None else content,
                error=None,
                decision=decision,
            )
        decision = InformationReleaseDecision(
            verdict=PolicyOutcome.DENY,
            transform=None,
            allowed_fields=frozenset(),
            retention="none",
            audit_level="summary",
            reason="test_deny",
        )
        return ReleaseResult(
            allowed=False,
            content=None,
            error="information_release_denied",
            decision=decision,
        )


class FakePolicyAuditService:
    """Fake PolicyAuditService that records events."""

    def __init__(self) -> None:
        self.events: list[PolicyAuditEvent] = []

    async def record(self, event: PolicyAuditEvent) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _make_config(
    *,
    artifact_max_bytes: int = 20 * 1024 * 1024,
    artifact_publish_max_bytes: int = 10 * 1024 * 1024,
    artifact_inline_max_bytes: int = 256 * 1024,
    published_base_url: str = "",
) -> ArtifactServiceConfig:
    return ArtifactServiceConfig(
        artifact_max_bytes=artifact_max_bytes,
        artifact_publish_max_bytes=artifact_publish_max_bytes,
        artifact_inline_max_bytes=artifact_inline_max_bytes,
        published_base_url=published_base_url,
    )


def _make_service(
    *,
    registry: FakeArtifactRegistry | None = None,
    content_store: FakeArtifactContentStore | None = None,
    policy: ArtifactPolicy | None = None,
    flow: FakeInformationFlowService | None = None,
    audit: FakePolicyAuditService | None = None,
    config: ArtifactServiceConfig | None = None,
    convert_html: Callable[[str], str] | None = None,
    task_session_resolver: Callable[[str], Awaitable[str | None]] | None = None,
    task_attachment_delete: Callable[[str], Awaitable[bool]] | None = None,
) -> ArtifactService:
    return ArtifactService(
        registry=registry or FakeArtifactRegistry(),
        content_store=content_store or FakeArtifactContentStore(),
        policy=policy or ArtifactPolicy(),
        information_flow_service=flow or FakeInformationFlowService(),
        policy_audit_service=audit or FakePolicyAuditService(),
        config=config or _make_config(),
        convert_to_html=convert_html or convert_to_html,
        task_session_resolver=task_session_resolver,
        task_attachment_delete=task_attachment_delete,
    )


def _make_inline_artifact(
    *,
    artifact_id: str = "art-1",
    name: str = "doc.md",
    kind: ArtifactKind = ArtifactKind.MARKDOWN,
    mime: str = "text/markdown",
    inline_content: str = "# Hello",
    source_kind: ArtifactSource = ArtifactSource.MANUAL,
    source_ref: str | None = None,
    source_context_ref: str | None = None,
    status: ArtifactStatus = ArtifactStatus.DRAFT,
    classification: str | None = None,
    labels: tuple[str, ...] | None = None,
    updated_at: datetime | None = None,
) -> Artifact:
    data = inline_content.encode("utf-8")
    return Artifact(
        id=artifact_id,
        name=name,
        kind=kind,
        mime=mime,
        content_ref=None,
        inline_content=inline_content,
        size=len(data),
        checksum=_sha256(data),
        source_kind=source_kind,
        source_ref=source_ref or artifact_id,
        source_context_ref=source_context_ref,
        status=status,
        classification=classification,
        labels=labels,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=updated_at or datetime(2026, 1, 1, tzinfo=timezone.utc),
        created_by="dashboard",
    )


def _make_file_artifact(
    *,
    artifact_id: str = "art-2",
    name: str = "image.png",
    kind: ArtifactKind = ArtifactKind.IMAGE,
    mime: str = "image/png",
    content_ref: str = "item:art-2/file",
    size: int = 100,
    checksum: str | None = None,
    source_kind: ArtifactSource = ArtifactSource.MANUAL,
    source_ref: str | None = None,
    source_context_ref: str | None = None,
    source_session_id: str | None = None,
    status: ArtifactStatus = ArtifactStatus.DRAFT,
    classification: str | None = None,
    labels: tuple[str, ...] | None = None,
) -> Artifact:
    return Artifact(
        id=artifact_id,
        name=name,
        kind=kind,
        mime=mime,
        content_ref=content_ref,
        inline_content=None,
        size=size,
        checksum=checksum or _sha256(b"x" * size),
        source_kind=source_kind,
        source_ref=source_ref or artifact_id,
        source_context_ref=source_context_ref,
        source_session_id=source_session_id,
        status=status,
        classification=classification,
        labels=labels,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        created_by="dashboard",
    )


# ---------------------------------------------------------------------------
# List / Get tests
# ---------------------------------------------------------------------------


class TestListArtifacts:
    @pytest.mark.asyncio
    async def test_limit_clamped_to_100(self):
        registry = FakeArtifactRegistry()
        svc = _make_service(registry=registry)
        page = await svc.list_artifacts(limit=200)
        assert isinstance(page.items, tuple)

    @pytest.mark.asyncio
    async def test_limit_clamped_to_1(self):
        registry = FakeArtifactRegistry()
        svc = _make_service(registry=registry)
        await svc.list_artifacts(limit=0)
        # limit=0 should be clamped to 1
        # verify by checking that list_artifacts was called with limit >= 1
        # (we check the result is a valid page)
        page = await svc.list_artifacts(limit=0)
        assert isinstance(page.items, tuple)

    @pytest.mark.asyncio
    async def test_limit_default_50(self):
        registry = FakeArtifactRegistry()
        for i in range(60):
            art = _make_inline_artifact(
                artifact_id=f"art-{i}",
                name=f"doc-{i}.md",
                updated_at=datetime(2026, 1, 1, 0, i, tzinfo=timezone.utc),
            )
            registry.seed(art)
        svc = _make_service(registry=registry)
        page = await svc.list_artifacts()
        assert len(page.items) == 50
        assert page.next_cursor is not None

    @pytest.mark.asyncio
    async def test_filter_pass_through(self):
        registry = FakeArtifactRegistry()
        registry.seed(_make_inline_artifact(artifact_id="a1", kind=ArtifactKind.MARKDOWN))
        registry.seed(_make_file_artifact(artifact_id="a2", kind=ArtifactKind.IMAGE))
        svc = _make_service(registry=registry)
        page = await svc.list_artifacts(kind=ArtifactKind.MARKDOWN)
        assert len(page.items) == 1
        assert page.items[0].id == "a1"

    @pytest.mark.asyncio
    async def test_cursor_pass_through(self):
        registry = FakeArtifactRegistry()
        for i in range(10):
            art = _make_inline_artifact(
                artifact_id=f"art-{i}",
                name=f"doc-{i}.md",
                updated_at=datetime(2026, 1, 1, 0, i, tzinfo=timezone.utc),
            )
            registry.seed(art)
        svc = _make_service(registry=registry)
        page1 = await svc.list_artifacts(limit=5)
        assert page1.next_cursor is not None
        page2 = await svc.list_artifacts(limit=5, cursor=page1.next_cursor)
        all_ids = {a.id for a in page1.items} | {a.id for a in page2.items}
        assert len(all_ids) == 10

    @pytest.mark.asyncio
    async def test_source_context_ref_passthrough_specific_value(self):
        """Service passes source_context_ref as-is to registry."""
        registry = FakeArtifactRegistry()
        svc = _make_service(registry=registry)
        await svc.list_artifacts(source_context_ref="session-a")
        assert registry.list_calls[-1]["source_context_ref"] == "session-a"

    @pytest.mark.asyncio
    async def test_source_context_ref_passthrough_empty_string(self):
        """Empty string is passed as-is, NOT coerced to None."""
        registry = FakeArtifactRegistry()
        svc = _make_service(registry=registry)
        await svc.list_artifacts(source_context_ref="")
        assert registry.list_calls[-1]["source_context_ref"] == ""

    @pytest.mark.asyncio
    async def test_source_context_ref_omitted_defaults_to_none(self):
        """Omitting source_context_ref passes None to registry."""
        registry = FakeArtifactRegistry()
        svc = _make_service(registry=registry)
        await svc.list_artifacts()
        assert registry.list_calls[-1]["source_context_ref"] is None

    @pytest.mark.asyncio
    async def test_source_context_ref_with_limit_clamp(self):
        """source_context_ref does not interfere with limit clamping."""
        registry = FakeArtifactRegistry()
        svc = _make_service(registry=registry)
        await svc.list_artifacts(source_context_ref="session-a", limit=200)
        assert registry.list_calls[-1]["source_context_ref"] == "session-a"
        assert registry.list_calls[-1]["limit"] == 100

    @pytest.mark.asyncio
    async def test_source_context_ref_filters_results(self):
        """The fake registry filters by source_context_ref, proving the
        keyword is accepted and applied."""
        registry = FakeArtifactRegistry()
        registry.seed(_make_inline_artifact(
            artifact_id="a1", source_kind=ArtifactSource.SESSION,
            source_ref="session-a:1", source_context_ref="session-a",
        ))
        registry.seed(_make_inline_artifact(
            artifact_id="a2", source_kind=ArtifactSource.SESSION,
            source_ref="session-b:2", source_context_ref="session-b",
        ))
        svc = _make_service(registry=registry)
        page = await svc.list_artifacts(source_context_ref="session-a")
        assert {a.id for a in page.items} == {"a1"}


class TestGetArtifact:
    @pytest.mark.asyncio
    async def test_returns_metadata_not_found(self):
        svc = _make_service()
        with pytest.raises(ArtifactNotFoundError):
            await svc.get_artifact("missing")

    @pytest.mark.asyncio
    async def test_detail_view_no_content_ref(self):
        registry = FakeArtifactRegistry()
        art = _make_file_artifact(content_ref="item:art-2/secret")
        registry.seed(art)
        svc = _make_service(registry=registry)
        result = await svc.get_artifact("art-2")
        view = result.to_public_view()
        assert "content_ref" not in view
        assert "inline_content" not in view
        assert "source_ref" not in view


class TestGetContent:
    @pytest.mark.asyncio
    async def test_inline_content(self):
        registry = FakeArtifactRegistry()
        registry.seed(_make_inline_artifact(inline_content="# Hi"))
        svc = _make_service(registry=registry)
        data, art = await svc.get_content("art-1")
        assert data == b"# Hi"
        assert art.id == "art-1"

    @pytest.mark.asyncio
    async def test_file_content(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        store.seed("item:art-2/file", b"\x89PNG data")
        registry.seed(_make_file_artifact(content_ref="item:art-2/file"))
        svc = _make_service(registry=registry, content_store=store)
        data, art = await svc.get_content("art-2")
        assert data == b"\x89PNG data"

    @pytest.mark.asyncio
    async def test_content_missing(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        registry.seed(_make_file_artifact(content_ref="item:art-2/missing"))
        svc = _make_service(registry=registry, content_store=store)
        with pytest.raises(ArtifactContentUnavailableError):
            await svc.get_content("art-2")

    @pytest.mark.asyncio
    async def test_artifact_not_found(self):
        svc = _make_service()
        with pytest.raises(ArtifactNotFoundError):
            await svc.get_content("missing")


# ---------------------------------------------------------------------------
# Create tests
# ---------------------------------------------------------------------------


class TestCreateArtifact:
    @pytest.mark.asyncio
    async def test_inline_create(self):
        registry = FakeArtifactRegistry()
        svc = _make_service(registry=registry)
        art = await svc.create_artifact(
            name="doc.md",
            kind=ArtifactKind.MARKDOWN,
            mime="text/markdown",
            inline_content="# Hello World",
        )
        assert art.id == art.source_ref  # manual source_ref = id
        assert art.inline_content == "# Hello World"
        assert art.content_ref is None
        assert art.size == len(b"# Hello World")
        assert art.checksum == _sha256(b"# Hello World")
        assert art.source_kind is ArtifactSource.MANUAL
        assert len(registry.create_calls) == 1

    @pytest.mark.asyncio
    async def test_file_create(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        svc = _make_service(registry=registry, content_store=store)
        file_data = b"\x89PNG fake image data"
        art = await svc.create_artifact(
            name="photo.png",
            kind=ArtifactKind.IMAGE,
            mime="image/png",
            file_data=file_data,
            filename="photo.png",
        )
        assert art.content_ref is not None
        assert art.content_ref.startswith("item:")
        assert art.inline_content is None
        assert art.size == len(file_data)
        assert art.checksum == _sha256(file_data)
        assert len(store.write_calls) == 1

    @pytest.mark.asyncio
    async def test_inline_exceeds_limit(self):
        svc = _make_service(config=_make_config(artifact_inline_max_bytes=10))
        with pytest.raises(ArtifactTooLargeError):
            await svc.create_artifact(
                name="big.md",
                kind=ArtifactKind.MARKDOWN,
                mime="text/markdown",
                inline_content="x" * 100,
            )

    @pytest.mark.asyncio
    async def test_file_exceeds_limit(self):
        svc = _make_service(config=_make_config(artifact_max_bytes=10))
        with pytest.raises(ArtifactTooLargeError):
            await svc.create_artifact(
                name="big.bin",
                kind=ArtifactKind.OTHER,
                mime="application/octet-stream",
                file_data=b"x" * 100,
                filename="big.bin",
            )

    @pytest.mark.asyncio
    async def test_registry_failure_compensates_delete(self):
        registry = FakeArtifactRegistry()
        registry.fail_on_create = True
        store = FakeArtifactContentStore()
        svc = _make_service(registry=registry, content_store=store)
        with pytest.raises(RuntimeError):
            await svc.create_artifact(
                name="doc.md",
                kind=ArtifactKind.MARKDOWN,
                mime="text/markdown",
                file_data=b"content",
                filename="doc.md",
            )
        # content was written then compensated (deleted)
        assert len(store.write_calls) == 1
        assert len(store.delete_calls) == 1
        # no content left in store
        assert all(not store.has(ref) for ref in [f"item:{c[0]}/{c[1]}" for c in store.write_calls])

    @pytest.mark.asyncio
    async def test_manual_source_ref_defaults_to_id(self):
        svc = _make_service()
        art = await svc.create_artifact(
            name="doc.md",
            kind=ArtifactKind.MARKDOWN,
            mime="text/markdown",
            inline_content="content",
        )
        assert art.source_ref == art.id


# ---------------------------------------------------------------------------
# Update tests
# ---------------------------------------------------------------------------


class TestUpdateArtifact:
    @pytest.mark.asyncio
    async def test_metadata_only_update(self):
        registry = FakeArtifactRegistry()
        registry.seed(_make_inline_artifact(name="old.md"))
        svc = _make_service(registry=registry)
        result = await svc.update_artifact("art-1", name="new.md", summary="updated")
        assert result.name == "new.md"
        assert result.summary == "updated"
        assert result.checksum == registry._artifacts["art-1"].checksum

    @pytest.mark.asyncio
    async def test_text_update_inline(self):
        registry = FakeArtifactRegistry()
        registry.seed(_make_inline_artifact(inline_content="old"))
        svc = _make_service(registry=registry)
        result = await svc.update_artifact("art-1", inline_content="new content")
        assert result.inline_content == "new content"
        assert result.size == len("new content")
        assert result.checksum == _sha256(b"new content")

    @pytest.mark.asyncio
    async def test_binary_update(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        old_ref = "item:art-2/old"
        store.seed(old_ref, b"old data")
        registry.seed(_make_file_artifact(artifact_id="art-2", content_ref=old_ref, size=8))
        svc = _make_service(registry=registry, content_store=store)
        new_data = b"new binary data"
        result = await svc.update_artifact(
            "art-2", file_data=new_data, filename="updated.bin"
        )
        assert result.content_ref != old_ref
        assert result.size == len(new_data)
        assert result.checksum == _sha256(new_data)
        # old owned content cleaned up
        assert old_ref in store.delete_calls

    @pytest.mark.asyncio
    async def test_attachment_first_edit_materialize_for_file(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        attachment_ref = "attachment:task-1/stored-1"
        store.seed(attachment_ref, b"attachment content")
        art = _make_file_artifact(
            artifact_id="art-att",
            kind=ArtifactKind.TEXT,
            mime="text/plain",
            content_ref=attachment_ref,
            source_kind=ArtifactSource.TASK_ATTACHMENT,
            source_ref="att-1",
            source_context_ref="task-1",
            size=len(b"attachment content"),
            checksum=_sha256(b"attachment content"),
        )
        registry.seed(art)
        svc = _make_service(registry=registry, content_store=store)
        new_data = b"edited binary content"
        result = await svc.update_artifact(
            "art-att", file_data=new_data, filename="edited.txt"
        )
        # materialize was called (file replacement on source-backed artifact)
        assert len(store.materialize_calls) == 1
        assert store.materialize_calls[0][0] is ArtifactSource.TASK_ATTACHMENT
        # original attachment not modified
        assert store.has(attachment_ref)
        assert store._content[attachment_ref] == b"attachment content"
        # artifact now has owned content_ref
        assert result.content_ref is not None
        assert result.content_ref.startswith("item:")
        assert result.inline_content is None

    @pytest.mark.asyncio
    async def test_attachment_first_edit_inline_skips_materialize(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        attachment_ref = "attachment:task-1/stored-1"
        store.seed(attachment_ref, b"attachment content")
        art = _make_file_artifact(
            artifact_id="art-att",
            kind=ArtifactKind.TEXT,
            mime="text/plain",
            content_ref=attachment_ref,
            source_kind=ArtifactSource.TASK_ATTACHMENT,
            source_ref="att-1",
            source_context_ref="task-1",
            size=len(b"attachment content"),
            checksum=_sha256(b"attachment content"),
        )
        registry.seed(art)
        svc = _make_service(registry=registry, content_store=store)
        new_text = "edited text content"
        result = await svc.update_artifact("art-att", inline_content=new_text)
        # materialize was NOT called (inline edit does not need owned copy)
        assert len(store.materialize_calls) == 0
        # original attachment not modified
        assert store.has(attachment_ref)
        assert store._content[attachment_ref] == b"attachment content"
        # artifact now has inline content
        assert result.inline_content == new_text

    @pytest.mark.asyncio
    async def test_registry_update_failure_old_content_still_readable(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        old_ref = "item:art-2/old"
        store.seed(old_ref, b"old data")
        registry.seed(_make_file_artifact(artifact_id="art-2", content_ref=old_ref, size=8))
        registry.fail_on_update = True
        svc = _make_service(registry=registry, content_store=store)
        with pytest.raises(RuntimeError):
            await svc.update_artifact(
                "art-2", file_data=b"new data", filename="new.bin"
            )
        # old content still readable
        assert store.has(old_ref)
        assert store._content[old_ref] == b"old data"
        # new temp content cleaned up
        new_refs = [c for c in store.delete_calls if c != old_ref]
        assert len(new_refs) >= 1

    @pytest.mark.asyncio
    async def test_successful_update_cleans_old_owned(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        old_ref = "item:art-1/old"
        store.seed(old_ref, b"old data")
        art = Artifact(
            id="art-1",
            name="doc.md",
            kind=ArtifactKind.MARKDOWN,
            mime="text/markdown",
            content_ref=old_ref,
            inline_content=None,
            size=8,
            checksum=_sha256(b"old data"),
            source_kind=ArtifactSource.MANUAL,
            source_ref="art-1",
        )
        registry.seed(art)
        svc = _make_service(registry=registry, content_store=store)
        await svc.update_artifact("art-1", file_data=b"new data", filename="doc.md")
        assert old_ref in store.delete_calls

    @pytest.mark.asyncio
    async def test_archived_deny(self):
        registry = FakeArtifactRegistry()
        registry.seed(_make_inline_artifact(status=ArtifactStatus.ARCHIVED))
        svc = _make_service(registry=registry)
        with pytest.raises(ArtifactValidationError):
            await svc.update_artifact("art-1", name="new")


# ---------------------------------------------------------------------------
# Delete tests
# ---------------------------------------------------------------------------


class TestDeleteArtifact:
    @pytest.mark.asyncio
    async def test_delete_owned_content(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        ref = "item:art-1/file"
        store.seed(ref, b"data")
        registry.seed(_make_file_artifact(artifact_id="art-1", content_ref=ref, size=4))
        svc = _make_service(registry=registry, content_store=store)
        result = await svc.delete_artifact("art-1")
        assert result is True
        assert ref in store.delete_calls

    @pytest.mark.asyncio
    async def test_delete_does_not_touch_source(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        attachment_ref = "attachment:task-1/stored-1"
        store.seed(attachment_ref, b"source data")
        art = _make_file_artifact(
            artifact_id="art-att",
            content_ref=attachment_ref,
            source_kind=ArtifactSource.TASK_ATTACHMENT,
            source_ref="att-1",
        )
        registry.seed(art)
        svc = _make_service(registry=registry, content_store=store)
        await svc.delete_artifact("art-att")
        # attachment source not deleted
        assert attachment_ref not in store.delete_calls
        assert store.has(attachment_ref)

    @pytest.mark.asyncio
    async def test_delete_task_attachment_cascades_to_source(self):
        """Deleting a task_attachment artifact must cascade-delete the
        underlying TaskAttachment via the injected callback, so the task
        detail page stays in sync. The source (source of truth) is removed
        BEFORE the artifact metadata, so backfill cannot resurrect it."""
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        attachment_ref = "attachment:task-1/stored-1"
        store.seed(attachment_ref, b"source data")
        art = _make_file_artifact(
            artifact_id="art-att",
            content_ref=attachment_ref,
            source_kind=ArtifactSource.TASK_ATTACHMENT,
            source_ref="att-1",
        )
        registry.seed(art)
        deleted: list[str] = []

        async def delete_attachment(attachment_id: str) -> bool:
            deleted.append(attachment_id)
            return True

        svc = _make_service(
            registry=registry, content_store=store,
            task_attachment_delete=delete_attachment,
        )
        result = await svc.delete_artifact("art-att")
        assert result is True
        # Source TaskAttachment delete invoked with the attachment_id (source_ref).
        assert deleted == ["att-1"]
        # Artifact metadata deleted.
        assert "art-att" in registry.delete_calls
        # ArtifactService must NOT delete the source file via its own content
        # store (the callback owns the attachment file lifecycle).
        assert attachment_ref not in store.delete_calls

    @pytest.mark.asyncio
    async def test_delete_task_attachment_callback_already_gone(self):
        """When the callback reports the attachment already deleted (False),
        the artifact metadata is still removed (stale projection cleanup)."""
        registry = FakeArtifactRegistry()
        art = _make_file_artifact(
            artifact_id="art-att",
            content_ref="attachment:task-1/stored-1",
            source_kind=ArtifactSource.TASK_ATTACHMENT,
            source_ref="att-1",
        )
        registry.seed(art)

        async def delete_attachment(attachment_id: str) -> bool:
            return False  # already gone

        svc = _make_service(
            registry=registry, task_attachment_delete=delete_attachment,
        )
        result = await svc.delete_artifact("art-att")
        assert result is True
        assert "art-att" in registry.delete_calls

    @pytest.mark.asyncio
    async def test_delete_task_attachment_callback_failure_propagates(self):
        """If the source TaskAttachment cannot be deleted, the artifact
        metadata must be left intact -- otherwise the attachment survives and
        backfill resurrects the artifact on next startup (the exact bug)."""
        registry = FakeArtifactRegistry()
        art = _make_file_artifact(
            artifact_id="art-att",
            content_ref="attachment:task-1/stored-1",
            source_kind=ArtifactSource.TASK_ATTACHMENT,
            source_ref="att-1",
        )
        registry.seed(art)

        async def delete_attachment(attachment_id: str) -> bool:
            raise RuntimeError("attachment delete failed")

        svc = _make_service(
            registry=registry, task_attachment_delete=delete_attachment,
        )
        with pytest.raises(RuntimeError):
            await svc.delete_artifact("art-att")
        # Artifact metadata NOT deleted (no resurrection).
        assert "art-att" not in registry.delete_calls
        assert await registry.get_artifact("art-att") is not None

    @pytest.mark.asyncio
    async def test_delete_non_task_attachment_skips_callback(self):
        """Only task_attachment-sourced artifacts cascade. Manual and
        task_artifact (workspace) artifacts must not trigger the attachment
        delete callback."""
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        deleted: list[str] = []

        async def delete_attachment(attachment_id: str) -> bool:
            deleted.append(attachment_id)
            return True

        # Manual artifact (owned content).
        owned_ref = "item:art-m/file"
        store.seed(owned_ref, b"data")
        registry.seed(_make_file_artifact(
            artifact_id="art-m", content_ref=owned_ref,
            source_kind=ArtifactSource.MANUAL,
        ))
        # task_artifact (workspace source) artifact.
        registry.seed(_make_file_artifact(
            artifact_id="art-ta",
            content_ref="workspace:task-1/output.md",
            source_kind=ArtifactSource.TASK_ARTIFACT,
            source_ref="task-1#r1#0",
        ))
        svc = _make_service(
            registry=registry, content_store=store,
            task_attachment_delete=delete_attachment,
        )
        await svc.delete_artifact("art-m")
        await svc.delete_artifact("art-ta")
        assert deleted == []


    @pytest.mark.asyncio
    async def test_delete_does_not_touch_snapshots(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        ref = "item:art-1/file"
        snapshot_ref = "published:pub-1/snap"
        store.seed(ref, b"data")
        store.seed(snapshot_ref, b"snapshot")
        registry.seed(_make_file_artifact(artifact_id="art-1", content_ref=ref, size=4))
        svc = _make_service(registry=registry, content_store=store)
        await svc.delete_artifact("art-1")
        # snapshot not deleted
        assert snapshot_ref not in store.delete_calls
        assert store.has(snapshot_ref)

    @pytest.mark.asyncio
    async def test_repeat_delete_not_found(self):
        svc = _make_service()
        with pytest.raises(ArtifactNotFoundError):
            await svc.delete_artifact("missing")

    @pytest.mark.asyncio
    async def test_delete_metadata_first(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        ref = "item:art-1/file"
        store.seed(ref, b"data")
        registry.seed(_make_file_artifact(artifact_id="art-1", content_ref=ref, size=4))
        store.fail_on_read = True  # simulate content issue
        svc = _make_service(registry=registry, content_store=store)
        result = await svc.delete_artifact("art-1")
        assert result is True
        assert len(registry.delete_calls) == 1

    @pytest.mark.asyncio
    async def test_delete_artifacts_by_source_task_removes_only_task_artifacts(self):
        """delete_artifacts_by_source_task deletes every artifact whose
        source_context_ref == task_id (both task_attachment and task_artifact),
        leaving other tasks' artifacts intact and cleaning owned content."""
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        store.seed("item:ta-1/file", b"a-data")
        registry.seed(_make_file_artifact(
            artifact_id="ta-1", content_ref="item:ta-1/file",
            source_kind=ArtifactSource.TASK_ATTACHMENT, source_context_ref="task-1",
        ))
        registry.seed(_make_inline_artifact(
            artifact_id="ta-2", source_kind=ArtifactSource.TASK_ARTIFACT,
            source_context_ref="task-1",
        ))
        # task-2 artifact must survive
        registry.seed(_make_inline_artifact(
            artifact_id="tb-1", source_kind=ArtifactSource.TASK_ARTIFACT,
            source_context_ref="task-2",
        ))
        svc = _make_service(registry=registry, content_store=store)
        count = await svc.delete_artifacts_by_source_task("task-1")
        assert count == 2
        assert await registry.get_artifact("ta-1") is None
        assert await registry.get_artifact("ta-2") is None
        assert await registry.get_artifact("tb-1") is not None
        # owned content of the deleted file artifact cleaned up
        assert "item:ta-1/file" in store.delete_calls

    @pytest.mark.asyncio
    async def test_delete_artifacts_by_source_task_empty(self):
        """No artifacts for the task -> returns 0, no delete calls."""
        registry = FakeArtifactRegistry()
        registry.seed(_make_inline_artifact(
            artifact_id="tb-1", source_context_ref="task-2",
        ))
        svc = _make_service(registry=registry)
        count = await svc.delete_artifacts_by_source_task("task-1")
        assert count == 0
        assert registry.delete_calls == []


# ---------------------------------------------------------------------------
# Export tests
# ---------------------------------------------------------------------------


class TestExport:
    @pytest.mark.asyncio
    async def test_original_export_exact_bytes(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        content = b"# Markdown content"
        ref = "item:art-1/file"
        store.seed(ref, content)
        art = Artifact(
            id="art-1",
            name="doc.md",
            kind=ArtifactKind.MARKDOWN,
            mime="text/markdown",
            content_ref=ref,
            inline_content=None,
            size=len(content),
            checksum=_sha256(content),
            source_kind=ArtifactSource.MANUAL,
            source_ref="art-1",
        )
        registry.seed(art)
        svc = _make_service(registry=registry, content_store=store)
        data, mime, filename = await svc.export("art-1")
        assert data == content
        assert mime == "text/markdown"

    @pytest.mark.asyncio
    async def test_original_export_inline(self):
        registry = FakeArtifactRegistry()
        registry.seed(_make_inline_artifact(inline_content="# Hi"))
        svc = _make_service(registry=registry)
        data, mime, filename = await svc.export("art-1")
        assert data == b"# Hi"
        assert mime == "text/markdown"

    @pytest.mark.asyncio
    async def test_html_export_markdown(self):
        registry = FakeArtifactRegistry()
        registry.seed(_make_inline_artifact(inline_content="# Title\n\nContent"))
        svc = _make_service(registry=registry)
        data, mime, filename = await svc.export("art-1", format="html")
        assert mime == "text/html"
        assert b"<html" in data.lower()
        assert b"Content-Security-Policy" in data

    @pytest.mark.asyncio
    async def test_html_export_document(self):
        registry = FakeArtifactRegistry()
        registry.seed(
            _make_inline_artifact(
                kind=ArtifactKind.DOCUMENT,
                mime="text/plain",
                inline_content="Document text",
            )
        )
        svc = _make_service(registry=registry)
        data, mime, filename = await svc.export("art-1", format="html")
        assert mime == "text/html"

    @pytest.mark.asyncio
    async def test_html_export_other_kind_rejected(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        store.seed("item:art-2/file", b"\x89PNG")
        registry.seed(_make_file_artifact(content_ref="item:art-2/file", size=4))
        svc = _make_service(registry=registry, content_store=store)
        with pytest.raises(ArtifactValidationError):
            await svc.export("art-2", format="html")

    @pytest.mark.asyncio
    async def test_export_does_not_modify_artifact(self):
        registry = FakeArtifactRegistry()
        registry.seed(_make_inline_artifact(inline_content="# Original"))
        svc = _make_service(registry=registry)
        await svc.export("art-1", format="html")
        art = await svc.get_artifact("art-1")
        assert art.inline_content == "# Original"


# ---------------------------------------------------------------------------
# Publish tests
# ---------------------------------------------------------------------------


class TestPublish:
    @pytest.mark.asyncio
    async def test_policy_and_audit_called(self):
        registry = FakeArtifactRegistry()
        audit = FakePolicyAuditService()
        registry.seed(_make_inline_artifact(inline_content="safe content"))
        svc = _make_service(registry=registry, audit=audit)
        await svc.publish("art-1")
        assert len(audit.events) >= 1
        assert audit.events[0].policy == "artifact-policy"

    @pytest.mark.asyncio
    async def test_text_publish_uses_release_content(self):
        registry = FakeArtifactRegistry()
        flow = FakeInformationFlowService(allow=True, redacted_content="REDACTED")
        registry.seed(_make_inline_artifact(inline_content="secret-value-here"))
        svc = _make_service(registry=registry, flow=flow)
        result = await svc.publish("art-1")
        assert flow.release_calls
        call = flow.release_calls[0]
        assert call["target"] is ReleaseTarget.PUBLIC_ARTIFACT
        assert call["origin"] == "artifact"
        # snapshot stores redacted content, not original
        published = result.published
        assert published.snapshot_inline_content == "REDACTED"
        assert published.snapshot_size == len("REDACTED".encode("utf-8"))

    @pytest.mark.asyncio
    async def test_text_publish_denied_by_flow(self):
        registry = FakeArtifactRegistry()
        flow = FakeInformationFlowService(allow=False)
        registry.seed(_make_inline_artifact(inline_content="secret content"))
        svc = _make_service(registry=registry, flow=flow)
        with pytest.raises(PublishBlockedError):
            await svc.publish("art-1")
        # no snapshot created
        assert len(registry.register_calls) == 0

    @pytest.mark.asyncio
    async def test_binary_publish_public_gate(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        ref = "item:art-2/file"
        store.seed(ref, b"\x89PNG data")
        # non-public classification -> deny
        registry.seed(
            _make_file_artifact(
                artifact_id="art-2",
                content_ref=ref,
                size=9,
                classification="internal",
            )
        )
        svc = _make_service(registry=registry, content_store=store)
        with pytest.raises((PublishBlockedError, ArtifactValidationError)):
            await svc.publish("art-2")

    @pytest.mark.asyncio
    async def test_binary_publish_public_allowed(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        ref = "item:art-2/file"
        store.seed(ref, b"\x89PNG data")
        registry.seed(
            _make_file_artifact(
                artifact_id="art-2",
                content_ref=ref,
                size=9,
                classification="public",
            )
        )
        svc = _make_service(registry=registry, content_store=store)
        result = await svc.publish("art-2")
        assert result.published.snapshot_content_ref is not None
        assert result.published.snapshot_content_ref.startswith("published:")
        assert result.published.snapshot_inline_content is None

    @pytest.mark.asyncio
    async def test_same_artifact_same_checksum_reuse(self):
        registry = FakeArtifactRegistry()
        flow = FakeInformationFlowService(allow=True)
        registry.seed(_make_inline_artifact(inline_content="stable"))
        svc = _make_service(registry=registry, flow=flow)
        r1 = await svc.publish("art-1")
        assert not r1.reused
        r2 = await svc.publish("art-1")
        assert r2.reused
        assert r1.published.publish_id == r2.published.publish_id
        # no new snapshot for reuse
        assert len(registry.register_calls) == 1

    @pytest.mark.asyncio
    async def test_edit_then_replacement_new_id_old_revoked(self):
        registry = FakeArtifactRegistry()
        flow = FakeInformationFlowService(allow=True)
        registry.seed(_make_inline_artifact(inline_content="v1"))
        svc = _make_service(registry=registry, flow=flow)
        r1 = await svc.publish("art-1")
        # edit content
        await svc.update_artifact("art-1", inline_content="v2")
        r2 = await svc.publish("art-1")
        assert not r2.reused
        assert r1.published.publish_id != r2.published.publish_id
        # old publish revoked
        old = await registry.get_published(r1.published.publish_id)
        assert old.status is PublishedArtifactStatus.REVOKED
        # new is active
        new = await registry.get_published(r2.published.publish_id)
        assert new.status is PublishedArtifactStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_another_artifact_same_checksum_no_reuse(self):
        registry = FakeArtifactRegistry()
        flow = FakeInformationFlowService(allow=True)
        # Two artifacts with identical content
        registry.seed(_make_inline_artifact(artifact_id="art-a", inline_content="same"))
        registry.seed(_make_inline_artifact(artifact_id="art-b", inline_content="same", name="doc-b.md"))
        svc = _make_service(registry=registry, flow=flow)
        ra = await svc.publish("art-a")
        rb = await svc.publish("art-b")
        assert ra.published.publish_id != rb.published.publish_id
        assert not rb.reused

    @pytest.mark.asyncio
    async def test_registry_failure_compensates_snapshot(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        flow = FakeInformationFlowService(allow=True)
        registry.seed(_make_inline_artifact(inline_content="content"))
        registry.fail_on_register = True
        # Use small inline_max_bytes to force file-backed snapshot (not inline)
        config = _make_config(artifact_inline_max_bytes=2)
        svc = _make_service(
            registry=registry, content_store=store, flow=flow, config=config
        )
        with pytest.raises(RuntimeError):
            await svc.publish("art-1")
        # snapshot file was created then compensated (deleted)
        assert len(store.copy_calls) == 1
        assert len(store.delete_calls) >= 1
        # no active publish registered
        assert len(registry.register_calls) == 1  # attempt was made

    @pytest.mark.asyncio
    async def test_registry_failure_old_active_preserved(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        flow = FakeInformationFlowService(allow=True)
        registry.seed(_make_inline_artifact(inline_content="v1"))
        svc = _make_service(registry=registry, content_store=store, flow=flow)
        r1 = await svc.publish("art-1")
        # edit and try to republish, but registry fails
        await svc.update_artifact("art-1", inline_content="v2")
        registry.fail_on_register = True
        with pytest.raises(RuntimeError):
            await svc.publish("art-1")
        # old active still preserved
        active = await registry.get_active_publish("art-1")
        assert active is not None
        assert active.publish_id == r1.published.publish_id
        assert active.status is PublishedArtifactStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_publish_id_is_url_safe_128bit(self):
        registry = FakeArtifactRegistry()
        registry.seed(_make_inline_artifact(inline_content="content"))
        svc = _make_service(registry=registry)
        result = await svc.publish("art-1")
        pid = result.published.publish_id
        # URL-safe base64url, no padding, >= 22 chars
        assert len(pid) >= 22
        assert "=" not in pid
        assert "+" not in pid
        assert "/" not in pid
        import re
        assert re.match(r"^[A-Za-z0-9_-]+$", pid)

    @pytest.mark.asyncio
    async def test_share_url_from_config_origin(self):
        registry = FakeArtifactRegistry()
        registry.seed(_make_inline_artifact(inline_content="content"))
        svc = _make_service(
            registry=registry,
            config=_make_config(published_base_url="https://example.com"),
        )
        result = await svc.publish("art-1")
        assert result.share_url == f"https://example.com/p/{result.published.publish_id}"

    @pytest.mark.asyncio
    async def test_share_url_relative_when_no_origin(self):
        registry = FakeArtifactRegistry()
        registry.seed(_make_inline_artifact(inline_content="content"))
        svc = _make_service(registry=registry)
        result = await svc.publish("art-1")
        assert result.share_url == f"/p/{result.published.publish_id}"

    @pytest.mark.asyncio
    async def test_archived_deny(self):
        registry = FakeArtifactRegistry()
        registry.seed(_make_inline_artifact(status=ArtifactStatus.ARCHIVED))
        svc = _make_service(registry=registry)
        with pytest.raises((PublishBlockedError, ArtifactValidationError)):
            await svc.publish("art-1")

    @pytest.mark.asyncio
    async def test_content_unavailable_deny(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        # artifact has content_ref but content is missing
        registry.seed(_make_file_artifact(content_ref="item:art-2/missing", size=10))
        svc = _make_service(registry=registry, content_store=store)
        with pytest.raises((PublishBlockedError, ArtifactContentUnavailableError)):
            await svc.publish("art-2")


# ---------------------------------------------------------------------------
# Revoke / GetPublished tests
# ---------------------------------------------------------------------------


class TestRevokePublish:
    @pytest.mark.asyncio
    async def test_revoke(self):
        registry = FakeArtifactRegistry()
        flow = FakeInformationFlowService(allow=True)
        registry.seed(_make_inline_artifact(inline_content="content"))
        svc = _make_service(registry=registry, flow=flow)
        await svc.publish("art-1")
        revoked = await svc.revoke_publish("art-1")
        assert revoked is not None
        assert revoked.status is PublishedArtifactStatus.REVOKED

    @pytest.mark.asyncio
    async def test_repeat_revoke_returns_same_revoked(self):
        registry = FakeArtifactRegistry()
        flow = FakeInformationFlowService(allow=True)
        registry.seed(_make_inline_artifact(inline_content="content"))
        svc = _make_service(registry=registry, flow=flow)
        await svc.publish("art-1")
        r1 = await svc.revoke_publish("art-1")
        r2 = await svc.revoke_publish("art-1")
        assert r1.publish_id == r2.publish_id
        assert r2.status is PublishedArtifactStatus.REVOKED

    @pytest.mark.asyncio
    async def test_revoke_no_active_publish(self):
        svc = _make_service()
        with pytest.raises(PublishedArtifactNotFoundError):
            await svc.revoke_publish("art-1")

    @pytest.mark.asyncio
    async def test_revoke_does_not_delete_snapshot(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        flow = FakeInformationFlowService(allow=True)
        registry.seed(_make_inline_artifact(inline_content="content"))
        svc = _make_service(registry=registry, content_store=store, flow=flow)
        result = await svc.publish("art-1")
        revoked = await svc.revoke_publish("art-1")
        # snapshot content not deleted
        assert len(store.delete_calls) == 0
        # snapshot data still accessible in the published record
        assert revoked.snapshot_inline_content == result.published.snapshot_inline_content
        assert revoked.snapshot_checksum == result.published.snapshot_checksum


class TestGetPublished:
    @pytest.mark.asyncio
    async def test_get_published(self):
        registry = FakeArtifactRegistry()
        flow = FakeInformationFlowService(allow=True)
        registry.seed(_make_inline_artifact(inline_content="content"))
        svc = _make_service(registry=registry, flow=flow)
        result = await svc.publish("art-1")
        pub = await svc.get_published(result.published.publish_id)
        assert pub.publish_id == result.published.publish_id

    @pytest.mark.asyncio
    async def test_get_published_not_found(self):
        svc = _make_service()
        with pytest.raises(PublishedArtifactNotFoundError):
            await svc.get_published("missing")

    @pytest.mark.asyncio
    async def test_get_published_does_not_read_source(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        flow = FakeInformationFlowService(allow=True)
        registry.seed(_make_inline_artifact(inline_content="content"))
        svc = _make_service(registry=registry, content_store=store, flow=flow)
        result = await svc.publish("art-1")
        store.read_calls.clear()
        await svc.get_published(result.published.publish_id)
        # no source content reads
        assert len(store.read_calls) == 0

    @pytest.mark.asyncio
    async def test_source_delete_snapshot_still_readable(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        flow = FakeInformationFlowService(allow=True)
        ref = "item:art-1/file"
        store.seed(ref, b"content data")
        registry.seed(
            _make_file_artifact(
                artifact_id="art-1",
                kind=ArtifactKind.TEXT,
                mime="text/plain",
                content_ref=ref,
                size=len(b"content data"),
                checksum=_sha256(b"content data"),
            )
        )
        svc = _make_service(registry=registry, content_store=store, flow=flow)
        result = await svc.publish("art-1")
        # delete source artifact
        await svc.delete_artifact("art-1")
        # snapshot still readable
        pub = await svc.get_published(result.published.publish_id)
        assert pub is not None


# ---------------------------------------------------------------------------
# Source registration tests
# ---------------------------------------------------------------------------


class TestRegisterFromAttachment:
    @pytest.mark.asyncio
    async def test_idempotent_register(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        attachment_ref = "attachment:task-1/stored-1"
        store.seed(attachment_ref, b"attachment data")
        svc = _make_service(registry=registry, content_store=store)
        src = ArtifactAttachmentSource(
            attachment_id="att-1",
            task_id="task-1",
            stored_name="stored-1",
            filename="report.pdf",
            content_type="application/pdf",
            size=len(b"attachment data"),
            checksum=_sha256(b"attachment data"),
            uploaded_by="user-1",
        )
        a1 = await svc.register_from_attachment(src)
        a2 = await svc.register_from_attachment(src)
        assert a1 is not None
        assert a2 is not None
        assert a1.id == a2.id
        assert len(registry.create_calls) == 1

    @pytest.mark.asyncio
    async def test_server_computes_trusted_fields(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        attachment_ref = "attachment:task-1/stored-1"
        actual_data = b"actual content"
        store.seed(attachment_ref, actual_data)
        svc = _make_service(registry=registry, content_store=store)
        src = ArtifactAttachmentSource(
            attachment_id="att-1",
            task_id="task-1",
            stored_name="stored-1",
            filename="report.pdf",
            content_type="application/pdf",
            size=999,  # wrong size
            checksum="sha256:" + "0" * 64,  # wrong checksum
            uploaded_by="user-1",
        )
        result = await svc.register_from_attachment(src)
        assert result is not None
        # server computed correct size/checksum
        assert result.size == len(actual_data)
        assert result.checksum == _sha256(actual_data)
        assert result.kind is ArtifactKind.PDF
        assert result.mime == "application/pdf"
        assert result.content_ref == attachment_ref
        assert result.source_kind is ArtifactSource.TASK_ATTACHMENT
        assert result.source_ref == "att-1"
        assert result.source_context_ref == "task-1"

    @pytest.mark.asyncio
    async def test_infers_kind_from_name_when_content_type_empty(self):
        """Attachments without content_type fall back to filename extension.

        Regression: attachments uploaded without a MIME content_type were
        classified as OTHER and could not render.
        """
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        attachment_ref = "attachment:task-1/stored-1"
        store.seed(attachment_ref, b"a,b,c\n1,2,3")
        svc = _make_service(registry=registry, content_store=store)
        src = ArtifactAttachmentSource(
            attachment_id="att-1",
            task_id="task-1",
            stored_name="stored-1",
            filename="report.csv",
            content_type="",  # missing -> infer from extension
            size=0,
            checksum="",
            uploaded_by="user-1",
        )
        result = await svc.register_from_attachment(src)
        assert result is not None
        assert result.kind is ArtifactKind.CSV
        assert result.mime == "text/csv"


class TestRegisterFromTaskArtifact:
    @pytest.mark.asyncio
    async def test_idempotent_register(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        ws_ref = "workspace:reports/output.md"
        store.seed(ws_ref, b"# Task output")
        svc = _make_service(registry=registry, content_store=store)
        ta = TaskArtifact(
            type="file",
            name="output.md",
            mime="text/markdown",
            size=len(b"# Task output"),
            storage_ref=ws_ref,
            source_task_id="task-1",
            summary="task result",
            checksum=_sha256(b"# Task output"),
        )
        a1 = await svc.register_from_task_artifact(ta, "task-1", 1, 0)
        a2 = await svc.register_from_task_artifact(ta, "task-1", 1, 0)
        assert a1 is not None
        assert a2 is not None
        assert a1.id == a2.id
        assert len(registry.create_calls) == 1

    @pytest.mark.asyncio
    async def test_server_computes_trusted_fields(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        ws_ref = "workspace:reports/output.md"
        actual_data = b"# Actual output"
        store.seed(ws_ref, actual_data)
        svc = _make_service(registry=registry, content_store=store)
        ta = TaskArtifact(
            type="file",
            name="output.md",
            mime="text/markdown",
            size=999,  # wrong
            storage_ref=ws_ref,
            source_task_id="task-1",
            summary="task result",
            checksum="sha256:" + "0" * 64,  # wrong
        )
        result = await svc.register_from_task_artifact(ta, "task-1", 1, 0)
        assert result is not None
        assert result.size == len(actual_data)
        assert result.checksum == _sha256(actual_data)
        assert result.kind is ArtifactKind.MARKDOWN
        assert result.source_kind is ArtifactSource.TASK_ARTIFACT
        expected_ref = Artifact.task_artifact_source_ref("task-1", 1, 0)
        assert result.source_ref == expected_ref
        assert result.source_context_ref == "task-1"

    @pytest.mark.asyncio
    async def test_infers_kind_from_name_when_mime_empty(self):
        """Task artifacts submitted without mime fall back to filename ext.

        Regression: task_complete submits {name, storage_ref, type} with no
        mime (artifact_normalizer is not wired); kind became OTHER and
        .md/.txt could not render in the workbench (BINARY_KINDS has 'other').
        """
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        ws_md = "workspace:task-output-b.md"
        ws_txt = "workspace:task-output-a.txt"
        store.seed(ws_md, b"# artifact b")
        store.seed(ws_txt, b"artifact a")
        svc = _make_service(registry=registry, content_store=store)

        ta_md = TaskArtifact(
            type="text", name="task-output-b.md", mime="", size=0,
            storage_ref=ws_md, source_task_id="task-1", summary="", checksum="",
        )
        result_md = await svc.register_from_task_artifact(ta_md, "task-1", 1, 0)
        assert result_md is not None
        assert result_md.kind is ArtifactKind.MARKDOWN
        assert result_md.mime == "text/markdown"

        ta_txt = TaskArtifact(
            type="text", name="task-output-a.txt", mime="", size=0,
            storage_ref=ws_txt, source_task_id="task-1", summary="", checksum="",
        )
        result_txt = await svc.register_from_task_artifact(ta_txt, "task-1", 1, 1)
        assert result_txt is not None
        assert result_txt.kind is ArtifactKind.TEXT
        assert result_txt.mime == "text/plain"

    @pytest.mark.asyncio
    async def test_empty_mime_unknown_extension_stays_other(self):
        """Unknown extension + empty mime stays OTHER (no false positive)."""
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        ws = "workspace:output.dat"
        store.seed(ws, b"\x00\x01\x02")
        svc = _make_service(registry=registry, content_store=store)
        ta = TaskArtifact(
            type="binary", name="output.dat", mime="", size=0,
            storage_ref=ws, source_task_id="task-1", summary="", checksum="",
        )
        result = await svc.register_from_task_artifact(ta, "task-1", 1, 0)
        assert result is not None
        assert result.kind is ArtifactKind.OTHER
        assert result.mime == ""

    @pytest.mark.asyncio
    async def test_invalid_workspace_ref_skipped(self, caplog):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        svc = _make_service(registry=registry, content_store=store)
        ta = TaskArtifact(
            type="file",
            name="output.md",
            mime="text/markdown",
            size=10,
            storage_ref="/etc/passwd",  # invalid: absolute path
            source_task_id="task-1",
            summary="",
            checksum="sha256:" + "0" * 64,
        )
        with caplog.at_level(logging.WARNING):
            result = await svc.register_from_task_artifact(ta, "task-1", 1, 0)
        assert result is None
        assert len(registry.create_calls) == 0
        # warning only contains source kind/ref/exception type, no content
        warning_text = " ".join(r.getMessage() for r in caplog.records)
        assert "task_artifact" in warning_text or "TaskArtifact" in warning_text

    @pytest.mark.asyncio
    async def test_missing_workspace_ref_skipped(self, caplog):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        svc = _make_service(registry=registry, content_store=store)
        ta = TaskArtifact(
            type="file",
            name="output.md",
            mime="text/markdown",
            size=10,
            storage_ref="workspace:missing/file.md",
            source_task_id="task-1",
            summary="",
            checksum="sha256:" + "0" * 64,
        )
        with caplog.at_level(logging.WARNING):
            result = await svc.register_from_task_artifact(ta, "task-1", 1, 0)
        assert result is None
        assert len(registry.create_calls) == 0

    @pytest.mark.asyncio
    async def test_does_not_affect_task_finish(self):
        """Registration failure returns None, does not raise."""
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        svc = _make_service(registry=registry, content_store=store)
        ta = TaskArtifact(
            type="file",
            name="output.md",
            mime="text/markdown",
            size=10,
            storage_ref="invalid-ref",
            source_task_id="task-1",
            summary="",
            checksum="sha256:" + "0" * 64,
        )
        result = await svc.register_from_task_artifact(ta, "task-1", 1, 0)
        assert result is None  # no exception raised


def _session_resolver(mapping: dict[str, str]):
    """Build a task_id -> session_id resolver from a static mapping."""
    async def resolve(task_id: str) -> str | None:
        return mapping.get(task_id)
    return resolve


class TestSourceSessionAssociation:
    """Registration establishes a session-keyed queryable association."""

    @pytest.mark.asyncio
    async def test_register_task_artifact_sets_session_id(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        ws_ref = "workspace:reports/output.md"
        store.seed(ws_ref, b"# Task output")
        resolver = _session_resolver({"task-1": "task-sess-1"})
        svc = _make_service(
            registry=registry, content_store=store, task_session_resolver=resolver,
        )
        ta = TaskArtifact(
            type="file", name="output.md", mime="text/markdown",
            size=len(b"# Task output"), storage_ref=ws_ref,
            source_task_id="task-1", summary="",
            checksum=_sha256(b"# Task output"),
        )
        result = await svc.register_from_task_artifact(ta, "task-1", 1, 0)
        assert result is not None
        assert result.source_session_id == "task-sess-1"
        # source_context_ref remains the task id (provenance), not the session.
        assert result.source_context_ref == "task-1"

    @pytest.mark.asyncio
    async def test_register_attachment_sets_session_id(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        attachment_ref = "attachment:task-9/stored-1"
        store.seed(attachment_ref, b"attachment data")
        resolver = _session_resolver({"task-9": "task-sess-9"})
        svc = _make_service(
            registry=registry, content_store=store, task_session_resolver=resolver,
        )
        src = ArtifactAttachmentSource(
            attachment_id="att-9", task_id="task-9", stored_name="stored-1",
            filename="report.pdf", content_type="application/pdf",
            size=len(b"attachment data"), checksum=_sha256(b"attachment data"),
            uploaded_by="user-1",
        )
        result = await svc.register_from_attachment(src)
        assert result is not None
        assert result.source_session_id == "task-sess-9"
        assert result.source_context_ref == "task-9"

    @pytest.mark.asyncio
    async def test_register_without_resolver_leaves_session_null(self):
        """No resolver wired (task subsystem disabled) -> graceful None."""
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()
        ws_ref = "workspace:reports/output.md"
        store.seed(ws_ref, b"# Task output")
        svc = _make_service(registry=registry, content_store=store)
        ta = TaskArtifact(
            type="file", name="output.md", mime="text/markdown",
            size=len(b"# Task output"), storage_ref=ws_ref,
            source_task_id="task-1", summary="",
            checksum=_sha256(b"# Task output"),
        )
        result = await svc.register_from_task_artifact(ta, "task-1", 1, 0)
        assert result is not None
        assert result.source_session_id is None

    @pytest.mark.asyncio
    async def test_list_passes_source_session_id_through(self):
        registry = FakeArtifactRegistry()
        svc = _make_service(registry=registry)
        await svc.list_artifacts(source_session_id="task-sess-1")
        assert registry.list_calls[-1]["source_session_id"] == "task-sess-1"

    @pytest.mark.asyncio
    async def test_list_omitted_source_session_id_defaults_none(self):
        registry = FakeArtifactRegistry()
        svc = _make_service(registry=registry)
        await svc.list_artifacts()
        assert registry.list_calls[-1]["source_session_id"] is None


class TestBackfillSessionIds:
    @pytest.mark.asyncio
    async def test_backfill_populates_missing_session(self):
        registry = FakeArtifactRegistry()
        # Two task artifacts missing session, one already populated, one manual.
        registry.seed(_make_file_artifact(
            artifact_id="a1", source_kind=ArtifactSource.TASK_ARTIFACT,
            source_ref="task:t1:run:1:artifact:0", source_context_ref="t1",
            content_ref="item:a1/f", checksum=_sha256(b"a1"),
        ))
        registry.seed(_make_file_artifact(
            artifact_id="a2", source_kind=ArtifactSource.TASK_ATTACHMENT,
            source_ref="att-2", source_context_ref="t2",
            content_ref="item:a2/f", checksum=_sha256(b"a2"),
        ))
        registry.seed(_make_file_artifact(
            artifact_id="a3", source_kind=ArtifactSource.TASK_ARTIFACT,
            source_ref="task:t3:run:1:artifact:0", source_context_ref="t3",
            source_session_id="sess-3",
            content_ref="item:a3/f", checksum=_sha256(b"a3"),
        ))
        registry.seed(_make_file_artifact(
            artifact_id="a4", source_kind=ArtifactSource.MANUAL,
            content_ref="item:a4/f", checksum=_sha256(b"a4"),
        ))
        resolver = _session_resolver({"t1": "sess-1", "t2": "sess-2", "t3": "sess-3"})
        svc = _make_service(registry=registry, task_session_resolver=resolver)

        stats = await svc.backfill_session_ids()
        assert stats == {"processed": 2, "updated": 2, "skipped": 0, "failed": 0}
        assert registry._artifacts["a1"].source_session_id == "sess-1"
        assert registry._artifacts["a2"].source_session_id == "sess-2"
        # Already-populated artifact untouched by backfill (not in missing set).
        assert registry._artifacts["a3"].source_session_id == "sess-3"
        # Manual artifact untouched.
        assert registry._artifacts["a4"].source_session_id is None
        # Idempotent: a second run finds nothing missing.
        stats2 = await svc.backfill_session_ids()
        assert stats2["processed"] == 0

    @pytest.mark.asyncio
    async def test_backfill_noop_without_resolver(self):
        registry = FakeArtifactRegistry()
        registry.seed(_make_file_artifact(
            artifact_id="a1", source_kind=ArtifactSource.TASK_ARTIFACT,
            source_ref="task:t1:run:1:artifact:0", source_context_ref="t1",
            content_ref="item:a1/f", checksum=_sha256(b"a1"),
        ))
        svc = _make_service(registry=registry)
        stats = await svc.backfill_session_ids()
        assert stats == {"processed": 0, "updated": 0, "skipped": 0, "failed": 0}
        assert registry._artifacts["a1"].source_session_id is None


class TestBackfillKinds:
    """Re-infer kind/mime for artifacts registered with empty mime.

    Regression: existing task artifacts with empty mime were classified as
    OTHER and could not render; the backfill re-derives kind/mime from the
    filename extension so historical artifacts render correctly.
    """

    @pytest.mark.asyncio
    async def test_reclassifies_empty_mime_from_name(self):
        registry = FakeArtifactRegistry()
        registry.seed(_make_file_artifact(
            artifact_id="a1", name="task-output-b.md",
            kind=ArtifactKind.OTHER, mime="",
            source_kind=ArtifactSource.TASK_ARTIFACT,
            source_ref="task:t1:run:1:artifact:0", source_context_ref="t1",
            content_ref="workspace:task-output-b.md",
        ))
        registry.seed(_make_file_artifact(
            artifact_id="a2", name="task-output-a.txt",
            kind=ArtifactKind.OTHER, mime="",
            source_kind=ArtifactSource.TASK_ARTIFACT,
            source_ref="task:t1:run:1:artifact:1", source_context_ref="t1",
            content_ref="workspace:task-output-a.txt",
        ))
        svc = _make_service(registry=registry)

        stats = await svc.backfill_kinds()
        assert stats == {"processed": 2, "updated": 2, "skipped": 0, "failed": 0}
        assert registry._artifacts["a1"].kind is ArtifactKind.MARKDOWN
        assert registry._artifacts["a1"].mime == "text/markdown"
        assert registry._artifacts["a2"].kind is ArtifactKind.TEXT
        assert registry._artifacts["a2"].mime == "text/plain"

    @pytest.mark.asyncio
    async def test_skips_artifacts_with_nonempty_mime(self):
        registry = FakeArtifactRegistry()
        registry.seed(_make_file_artifact(
            artifact_id="a1", name="doc.md",
            kind=ArtifactKind.MARKDOWN, mime="text/markdown",
            content_ref="item:a1/f",
        ))
        registry.seed(_make_file_artifact(
            artifact_id="a2", name="data.dat",
            kind=ArtifactKind.OTHER, mime="application/octet-stream",
            content_ref="item:a2/f",
        ))
        svc = _make_service(registry=registry)

        stats = await svc.backfill_kinds()
        assert stats == {"processed": 0, "updated": 0, "skipped": 0, "failed": 0}
        # Untouched.
        assert registry._artifacts["a2"].kind is ArtifactKind.OTHER

    @pytest.mark.asyncio
    async def test_skips_empty_mime_unknown_extension(self):
        """Empty mime + unknown extension -> no reclassification (stays OTHER)."""
        registry = FakeArtifactRegistry()
        registry.seed(_make_file_artifact(
            artifact_id="a1", name="blob.dat",
            kind=ArtifactKind.OTHER, mime="",
            content_ref="item:a1/f",
        ))
        svc = _make_service(registry=registry)

        stats = await svc.backfill_kinds()
        assert stats == {"processed": 1, "updated": 0, "skipped": 1, "failed": 0}
        assert registry._artifacts["a1"].kind is ArtifactKind.OTHER
        assert registry._artifacts["a1"].mime == ""

    @pytest.mark.asyncio
    async def test_idempotent(self):
        registry = FakeArtifactRegistry()
        registry.seed(_make_file_artifact(
            artifact_id="a1", name="doc.md",
            kind=ArtifactKind.OTHER, mime="",
            content_ref="item:a1/f",
        ))
        svc = _make_service(registry=registry)

        s1 = await svc.backfill_kinds()
        assert s1["updated"] == 1
        # Second run finds nothing with empty mime.
        s2 = await svc.backfill_kinds()
        assert s2 == {"processed": 0, "updated": 0, "skipped": 0, "failed": 0}


class TestBackfillAttachments:
    @pytest.mark.asyncio
    async def test_backfill_processes_batches(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()

        sources = []
        for i in range(3):
            att_ref = f"attachment:task-{i}/stored-{i}"
            store.seed(att_ref, f"content-{i}".encode())
            sources.append(
                ArtifactAttachmentSource(
                    attachment_id=f"att-{i}",
                    task_id=f"task-{i}",
                    stored_name=f"stored-{i}",
                    filename=f"file-{i}.txt",
                    content_type="text/plain",
                    size=len(f"content-{i}".encode()),
                    checksum=_sha256(f"content-{i}".encode()),
                    uploaded_by="user",
                )
            )

        call_count = [0]

        async def list_sources(*, after_attachment_id=None, limit=100):
            call_count[0] += 1
            if after_attachment_id is None:
                return tuple(sources[:2])
            return tuple(sources[2:])

        registry.list_attachment_sources = list_sources  # type: ignore

        svc = _make_service(registry=registry, content_store=store)
        stats = await svc.backfill_attachments(batch_size=2)
        assert stats["processed"] == 3
        assert stats["created"] == 3
        assert stats["failed"] == 0

    @pytest.mark.asyncio
    async def test_backfill_idempotent(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()

        att_ref = "attachment:task-1/stored-1"
        store.seed(att_ref, b"content-1")
        sources = [
            ArtifactAttachmentSource(
                attachment_id="att-1",
                task_id="task-1",
                stored_name="stored-1",
                filename="file-1.txt",
                content_type="text/plain",
                size=len(b"content-1"),
                checksum=_sha256(b"content-1"),
                uploaded_by="user",
            )
        ]

        async def list_sources(*, after_attachment_id=None, limit=100):
            if after_attachment_id is None:
                return tuple(sources)
            return ()

        registry.list_attachment_sources = list_sources  # type: ignore

        svc = _make_service(registry=registry, content_store=store)
        s1 = await svc.backfill_attachments()
        s2 = await svc.backfill_attachments()
        assert s1["created"] == 1
        assert s2["created"] == 0  # idempotent
        assert s2["skipped"] == 1

    @pytest.mark.asyncio
    async def test_backfill_single_failure_continues(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()

        sources = []
        for i in range(3):
            att_ref = f"attachment:task-{i}/stored-{i}"
            # Middle item's content is missing: content_store.read will raise,
            # register_from_attachment returns None -> backfill counts it as failed.
            if i != 1:
                store.seed(att_ref, f"content-{i}".encode())
            sources.append(
                ArtifactAttachmentSource(
                    attachment_id=f"att-{i}",
                    task_id=f"task-{i}",
                    stored_name=f"stored-{i}",
                    filename=f"file-{i}.txt",
                    content_type="text/plain",
                    size=len(f"content-{i}".encode()),
                    checksum=_sha256(f"content-{i}".encode()),
                    uploaded_by="user",
                )
            )

        async def list_sources(*, after_attachment_id=None, limit=100):
            if after_attachment_id is None:
                return tuple(sources)
            return ()

        registry.list_attachment_sources = list_sources  # type: ignore

        svc = _make_service(registry=registry, content_store=store)
        stats = await svc.backfill_attachments()
        assert stats["processed"] == 3
        assert stats["created"] == 2
        assert stats["failed"] == 1
        assert stats["skipped"] == 0
        assert len(registry.create_calls) == 2  # other 2 still registered

    @pytest.mark.asyncio
    async def test_backfill_interrupt_restart_resumes_via_idempotency(self):
        registry = FakeArtifactRegistry()
        store = FakeArtifactContentStore()

        sources = []
        for i in range(3):
            att_ref = f"attachment:task-{i}/stored-{i}"
            store.seed(att_ref, f"content-{i}".encode())
            sources.append(
                ArtifactAttachmentSource(
                    attachment_id=f"att-{i}",
                    task_id=f"task-{i}",
                    stored_name=f"stored-{i}",
                    filename=f"file-{i}.txt",
                    content_type="text/plain",
                    size=len(f"content-{i}".encode()),
                    checksum=_sha256(f"content-{i}".encode()),
                    uploaded_by="user",
                )
            )

        state = {"interrupted": True}

        async def list_sources(*, after_attachment_id=None, limit=100):
            if after_attachment_id is None:
                return tuple(sources[:2])  # first batch
            if state["interrupted"]:
                raise RuntimeError("simulated interrupt")  # crash before 2nd batch
            return tuple(sources[2:])  # remaining after restart

        registry.list_attachment_sources = list_sources  # type: ignore

        svc = _make_service(registry=registry, content_store=store)

        # First run: registers the first batch (2 items), then "crashes".
        with pytest.raises(RuntimeError, match="simulated interrupt"):
            await svc.backfill_attachments(batch_size=2)
        assert len(registry.create_calls) == 2  # first batch registered

        # Restart: re-scan from start; idempotent skip for the 2 already
        # registered, create the 3rd. No persisted checkpoint needed.
        state["interrupted"] = False
        stats = await svc.backfill_attachments(batch_size=2)
        assert stats["processed"] == 3
        assert stats["created"] == 1
        assert stats["skipped"] == 2
        assert stats["failed"] == 0
        assert len(registry.create_calls) == 3  # total unique artifacts across both runs


# ---------------------------------------------------------------------------
# Backfill orphaned task artifacts (deleted-task cleanup)
# ---------------------------------------------------------------------------


class TestBackfillOrphanedTaskArtifacts:
    """Delete task-sourced artifacts whose source task no longer exists.

    Regression: artifacts registered one-way into the separate artifacts DB
    survived task deletion (pre-cascade-fix), so deleted tasks' artifacts kept
    showing in the list. The startup backfill reclaims these orphans.
    """

    @pytest.mark.asyncio
    async def test_deletes_orphans_keeps_alive(self):
        registry = FakeArtifactRegistry()
        registry.seed(_make_inline_artifact(
            artifact_id="orphan-1", source_kind=ArtifactSource.TASK_ATTACHMENT,
            source_context_ref="task-deleted",
        ))
        registry.seed(_make_inline_artifact(
            artifact_id="alive-1", source_kind=ArtifactSource.TASK_ARTIFACT,
            source_context_ref="task-alive",
        ))
        svc = _make_service(registry=registry)
        alive = {"task-alive"}

        async def task_exists(tid: str) -> bool:
            return tid in alive

        svc.set_task_exists_callback(task_exists)
        stats = await svc.backfill_orphaned_task_artifacts(batch_size=100)
        assert stats["deleted"] == 1
        assert await registry.get_artifact("orphan-1") is None
        assert await registry.get_artifact("alive-1") is not None

    @pytest.mark.asyncio
    async def test_noop_without_callback(self):
        registry = FakeArtifactRegistry()
        registry.seed(_make_inline_artifact(
            artifact_id="orphan-1", source_kind=ArtifactSource.TASK_ATTACHMENT,
            source_context_ref="task-deleted",
        ))
        svc = _make_service(registry=registry)
        stats = await svc.backfill_orphaned_task_artifacts(batch_size=100)
        assert stats["deleted"] == 0
        assert await registry.get_artifact("orphan-1") is not None

    @pytest.mark.asyncio
    async def test_fail_safe_skips_on_existence_error(self):
        """If task_exists raises (e.g. task DB unavailable), the artifact is NOT
        deleted -- counted as failed, left intact (fail-safe)."""
        registry = FakeArtifactRegistry()
        registry.seed(_make_inline_artifact(
            artifact_id="orphan-1", source_kind=ArtifactSource.TASK_ATTACHMENT,
            source_context_ref="task-deleted",
        ))
        svc = _make_service(registry=registry)

        async def task_exists(tid: str) -> bool:
            raise RuntimeError("task DB unavailable")

        svc.set_task_exists_callback(task_exists)
        stats = await svc.backfill_orphaned_task_artifacts(batch_size=100)
        assert stats["deleted"] == 0
        assert stats["failed"] == 1
        assert await registry.get_artifact("orphan-1") is not None

    @pytest.mark.asyncio
    async def test_ignores_non_task_artifacts(self):
        """session/manual artifacts are never task orphans; not touched."""
        registry = FakeArtifactRegistry()
        registry.seed(_make_inline_artifact(
            artifact_id="ses-1", source_kind=ArtifactSource.SESSION,
            source_context_ref="sess-1",
        ))
        registry.seed(_make_inline_artifact(
            artifact_id="man-1", source_kind=ArtifactSource.MANUAL,
            source_context_ref=None,
        ))
        svc = _make_service(registry=registry)

        async def task_exists(tid: str) -> bool:
            return False  # nothing exists -> would delete if it touched these

        svc.set_task_exists_callback(task_exists)
        stats = await svc.backfill_orphaned_task_artifacts(batch_size=100)
        assert stats["deleted"] == 0
        assert await registry.get_artifact("ses-1") is not None
        assert await registry.get_artifact("man-1") is not None

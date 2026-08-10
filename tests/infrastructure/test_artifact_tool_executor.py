"""ArtifactToolExecutor 测试 (T8).

spec: spec-260809-artifact-agent-revision-export.md
覆盖 8 个 artifact_* 工具的: 可信 provenance 注入(不可由参数伪造)、会话作用域、
参数校验、text_patch 结构校验、read 文本分页/二进制元数据/首行超限、错误信封
{success,error:{code,message,retryable}}、retryable 仅 409/503、terminal=False、
未知异常映射 artifact_internal_error、不泄露 traceback/content_ref。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from app.application.artifact_service import (
    ArtifactTooLargeError,
    DiffResult,
    PublishBlockedError,
    PublishResult,
    UpdateRevisionResult,
)
from app.application.artifact_tools import (
    ARTIFACT_TOOL_CREATE,
    ARTIFACT_TOOL_DIFF,
    ARTIFACT_TOOL_LIST,
    ARTIFACT_TOOL_LIST_REVISIONS,
    ARTIFACT_TOOL_PUBLISH,
    ARTIFACT_TOOL_READ,
    ARTIFACT_TOOL_ROLLBACK,
    ARTIFACT_TOOL_UPDATE,
    artifact_tool_definitions,
)
from app.domain.artifact import (
    Artifact,
    ArtifactConflictError,
    ArtifactDiffTooLargeError,
    ArtifactDiffUnsupportedError,
    ArtifactExportUnsupportedError,
    ArtifactKind,
    ArtifactListCursor,
    ArtifactListPage,
    ArtifactMigrationIncompleteError,
    ArtifactNotFoundError,
    ArtifactReadTooLargeError,
    ArtifactRevision,
    ArtifactRevisionConflictError,
    ArtifactRevisionNotFoundError,
    ArtifactRevisionValidationError,
    ArtifactSource,
    ArtifactStatus,
    ArtifactValidationError,
    PublishedArtifact,
    PublishedArtifactStatus,
    RevisionListCursor,
    RevisionListPage,
)
from app.domain.tool import (
    ToolCallRequest,
    ToolExecutionContext,
    ToolResultStatus,
)
from app.infrastructure.tools.artifact_management import ArtifactToolExecutor

_CHKSUM = "sha256:" + "0" * 64


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _artifact(
    *, id="a_1", name="doc", kind=ArtifactKind.MARKDOWN, mime="text/markdown",
    inline="# hello\nworld\n", source_kind=ArtifactSource.SESSION,
    source_session_id="sess-1", current_revision_id="rev-1", status=ArtifactStatus.DRAFT,
) -> Artifact:
    if kind.value in ("image", "pdf", "other"):
        content_ref, inline_content, size = "item:binary", None, 10
        mime = mime or "application/octet-stream"
    else:
        content_ref, inline_content, size = None, inline, len(inline.encode("utf-8"))
    return Artifact(
        id=id, name=name, kind=kind, mime=mime, content_ref=content_ref,
        inline_content=inline_content, size=size, checksum=_CHKSUM,
        source_kind=source_kind, source_ref=id,
        source_session_id=source_session_id, status=status,
        created_at=_now(), updated_at=_now(), created_by="user-1",
        current_revision_id=current_revision_id,
    )


def _revision(
    *, id="rev-1", artifact_id="a_1", number=1, kind=ArtifactKind.MARKDOWN,
    mime="text/markdown", inline="# hello\nworld\n", parent=None,
    rollback_from=None,
) -> ArtifactRevision:
    size = len(inline.encode("utf-8"))
    return ArtifactRevision(
        id=id, artifact_id=artifact_id, revision_number=number,
        parent_revision_id=parent, rollback_from_revision_id=rollback_from,
        content_ref=None, inline_content=inline, size=size, checksum=_CHKSUM,
        mime=mime, kind=kind, created_at=_now(), created_by="user-1",
        source_session_id="sess-1",
    )


class FakeArtifactService:
    """满足 ArtifactToolServiceProtocol 的 async fake。"""

    def __init__(self):
        self.artifacts: dict[str, Artifact] = {}
        self.revisions: dict[str, ArtifactRevision] = {}  # revision_id -> rev
        self.contents: dict[str, bytes] = {}  # revision_id -> bytes
        self.create_calls: list[dict[str, Any]] = []
        self.create_return: Artifact | None = None
        self.raise_on_create: Exception | None = None
        self.list_calls: list[dict[str, Any]] = []
        self.list_return: ArtifactListPage = ArtifactListPage(items=())
        self.raise_on_list: Exception | None = None
        self.raise_on_get_artifact: Exception | None = None
        self.raise_on_get_revision_content: Exception | None = None
        self.update_calls: list[dict[str, Any]] = []
        self.update_return: tuple[ArtifactRevision, UpdateRevisionResult] | None = None
        self.raise_on_update: Exception | None = None
        self.list_revisions_return: RevisionListPage = RevisionListPage(items=())
        self.raise_on_list_revisions: Exception | None = None
        self.diff_calls: list[dict[str, Any]] = []
        self.diff_return = DiffResult(diff_text="", binary_changed=False)  # type: ignore[name-defined]
        self.raise_on_diff: Exception | None = None
        self.rollback_calls: list[dict[str, Any]] = []
        self.rollback_return: tuple[ArtifactRevision, UpdateRevisionResult] | None = None
        self.raise_on_rollback: Exception | None = None
        self.publish_calls: list[dict[str, Any]] = []
        self.publish_return: PublishResult | None = None
        self.raise_on_publish: Exception | None = None
        self.export_capabilities_return: tuple[str, ...] = ("original", "html")
        self.raise_on_export_capabilities: Exception | None = None
        self.active_publish: PublishedArtifact | None = None
        self.raise_on_get_active_publish: Exception | None = None
        self.raise_on_get_current_revision: Exception | None = None

    async def create_artifact(self, **kwargs):
        self.create_calls.append(kwargs)
        if self.raise_on_create:
            raise self.raise_on_create
        return self.create_return or _artifact()

    async def list_artifacts(self, **kwargs):
        self.list_calls.append(kwargs)
        if self.raise_on_list:
            raise self.raise_on_list
        return self.list_return

    async def get_artifact(self, artifact_id):
        if self.raise_on_get_artifact:
            raise self.raise_on_get_artifact
        art = self.artifacts.get(artifact_id)
        if art is None:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_id}")
        return art

    async def get_current_revision(self, artifact_id):
        if self.raise_on_get_current_revision:
            raise self.raise_on_get_current_revision
        aid = self.artifacts[artifact_id].current_revision_id if artifact_id in self.artifacts else "rev-1"
        return self.revisions.get(aid or "rev-1", _revision())

    async def get_revision_content(self, artifact_id, revision_id=None):
        if self.raise_on_get_revision_content:
            raise self.raise_on_get_revision_content
        rid = revision_id or (
            self.artifacts[artifact_id].current_revision_id if artifact_id in self.artifacts else "rev-1"
        )
        rev = self.revisions.get(rid or "rev-1")
        if rev is None:
            raise ArtifactRevisionNotFoundError(f"revision not found: {rid}")
        content = self.contents.get(rid or "rev-1", b"")
        return content, rev

    async def update_revision(self, artifact_id, **kwargs):
        self.update_calls.append({"artifact_id": artifact_id, **kwargs})
        if self.raise_on_update:
            raise self.raise_on_update
        return self.update_return or (_revision(id="rev-2", number=2, parent="rev-1"),
                                      UpdateRevisionResult(diff_summary="changed", content_unchanged=False, publish_sync_state="outdated"))

    async def list_revisions(self, artifact_id, **kwargs):
        if self.raise_on_list_revisions:
            raise self.raise_on_list_revisions
        return self.list_revisions_return

    async def diff_revisions(self, artifact_id, from_id, to_id, **kwargs):
        self.diff_calls.append({"artifact_id": artifact_id, "from_id": from_id, "to_id": to_id, **kwargs})
        if self.raise_on_diff:
            raise self.raise_on_diff
        return self.diff_return

    async def rollback(self, artifact_id, target_revision_id, **kwargs):
        self.rollback_calls.append({"artifact_id": artifact_id, "target": target_revision_id, **kwargs})
        if self.raise_on_rollback:
            raise self.raise_on_rollback
        return self.rollback_return or (_revision(id="rev-3", number=3, parent="rev-2", rollback_from="rev-2"),
                                        UpdateRevisionResult(diff_summary="rolled back", content_unchanged=False, publish_sync_state="outdated"))

    async def publish_revision(self, artifact_id, **kwargs):
        self.publish_calls.append({"artifact_id": artifact_id, **kwargs})
        if self.raise_on_publish:
            raise self.raise_on_publish
        return self.publish_return or PublishResult(
            published=PublishedArtifact(
                publish_id="pub-1", artifact_id=artifact_id, snapshot_name="doc",
                snapshot_kind=ArtifactKind.MARKDOWN, snapshot_mime="text/markdown",
                snapshot_content_ref=None, snapshot_inline_content="# hello",
                snapshot_size=7, snapshot_checksum=_CHKSUM, published_at=_now(),
                published_by="user-1", status=PublishedArtifactStatus.ACTIVE,
                published_revision_id="rev-1",
            ),
            share_url="/p/pub-1", reused=False,
        )

    async def export_capabilities(self, artifact_id, **kwargs):
        if self.raise_on_export_capabilities:
            raise self.raise_on_export_capabilities
        return self.export_capabilities_return

    async def get_active_publish(self, artifact_id):
        if self.raise_on_get_active_publish:
            raise self.raise_on_get_active_publish
        return self.active_publish


def _ctx(session_id="sess-1", actor_id="user-1", run_id="run-9"):
    return ToolExecutionContext(
        session_id=session_id,
        trusted_metadata={"actor_id": actor_id, "run_id": run_id},
    )


def _req(tool_name, **arguments):
    return ToolCallRequest(id="call-1", name=tool_name, arguments=arguments)


def _payload(res):
    return json.loads(res.content)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


class TestArtifactToolDefinitions:
    def test_eight_definitions_with_consistent_metadata(self):
        defs = {d.name: d for d in artifact_tool_definitions()}
        assert set(defs) == {
            ARTIFACT_TOOL_CREATE, ARTIFACT_TOOL_LIST, ARTIFACT_TOOL_READ,
            ARTIFACT_TOOL_UPDATE, ARTIFACT_TOOL_LIST_REVISIONS, ARTIFACT_TOOL_DIFF,
            ARTIFACT_TOOL_ROLLBACK, ARTIFACT_TOOL_PUBLISH,
        }
        for d in defs.values():
            assert d.source_type.value == "agent"
            assert d.toolset == "artifact"
            assert d.managed is False
            assert d.realtime_only is True  # hidden in SAFE_ONLY even when granted
            assert d.input_schema.get("additionalProperties") is False

    def test_risk_levels_match_spec(self):
        defs = {d.name: d for d in artifact_tool_definitions()}
        assert defs[ARTIFACT_TOOL_CREATE].risk_level.value == "confirm"
        assert defs[ARTIFACT_TOOL_UPDATE].risk_level.value == "confirm"
        assert defs[ARTIFACT_TOOL_ROLLBACK].risk_level.value == "confirm"
        assert defs[ARTIFACT_TOOL_PUBLISH].risk_level.value == "dangerous"
        for safe in (ARTIFACT_TOOL_LIST, ARTIFACT_TOOL_READ, ARTIFACT_TOOL_LIST_REVISIONS, ARTIFACT_TOOL_DIFF):
            assert defs[safe].risk_level.value == "safe"

    def test_no_provenance_params_in_schema(self):
        """session_id/run_id/actor_id/source_* must not be model-settable args."""
        forbidden = {"session_id", "run_id", "actor_id", "actor", "source_kind",
                     "source_ref", "source_session_id", "source_run_id", "created_by"}
        for d in artifact_tool_definitions():
            props = set(d.input_schema.get("properties", {}).keys())
            assert not (props & forbidden), f"{d.name} exposes forbidden props {props & forbidden}"


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


class TestCreate:
    @pytest.mark.asyncio
    async def test_success_provenance_from_context_not_args(self):
        svc = FakeArtifactService()
        svc.create_return = _artifact(id="a_new", current_revision_id="rev-1")
        svc.export_capabilities_return = ("original", "html", "docx")
        ex = ArtifactToolExecutor(svc)
        # forged provenance args must be ignored (not even in schema)
        res = await ex.execute(_req(ARTIFACT_TOOL_CREATE, name="My Doc", kind="markdown",
                                    inline_content="# hi", session_id="FORGED",
                                    run_id="FORGED", actor_id="FORGED"), _ctx())
        assert res.status == ToolResultStatus.SUCCESS
        assert res.terminal is False
        payload = _payload(res)
        assert payload["success"] is True
        assert payload["artifact_id"] == "a_new"
        assert payload["revision_id"] == "rev-1"
        assert payload["revision_number"] == 1
        assert payload["publish_sync_state"] == "unpublished"
        assert payload["capabilities"] == ["original", "html", "docx"]
        call = svc.create_calls[0]
        assert call["source_kind"] is ArtifactSource.SESSION
        assert call["source_session_id"] == "sess-1"
        assert call["source_run_id"] == "run-9"
        assert call["created_by"] == "user-1"

    @pytest.mark.asyncio
    async def test_missing_actor_run_collapses_to_empty(self):
        svc = FakeArtifactService()
        svc.create_return = _artifact()
        ex = ArtifactToolExecutor(svc)
        await ex.execute(_req(ARTIFACT_TOOL_CREATE, name="D", kind="text", inline_content="x"),
                         _ctx(actor_id="", run_id=""))
        call = svc.create_calls[0]
        assert call["source_run_id"] is None  # empty -> None
        assert call["created_by"] == ""

    @pytest.mark.asyncio
    async def test_session_missing_denied(self):
        ex = ArtifactToolExecutor(FakeArtifactService())
        res = await ex.execute(_req(ARTIFACT_TOOL_CREATE, name="D", kind="text", inline_content="x"),
                               ToolExecutionContext())
        assert res.status == ToolResultStatus.PERMISSION_DENIED
        payload = _payload(res)
        assert payload["success"] is False
        assert payload["error"]["code"] == "session_missing"

    @pytest.mark.asyncio
    async def test_name_required(self):
        ex = ArtifactToolExecutor(FakeArtifactService())
        res = await ex.execute(_req(ARTIFACT_TOOL_CREATE, kind="text", inline_content="x"), _ctx())
        payload = _payload(res)
        assert payload["error"]["code"] == "artifact_invalid"

    @pytest.mark.asyncio
    async def test_invalid_kind(self):
        ex = ArtifactToolExecutor(FakeArtifactService())
        res = await ex.execute(_req(ARTIFACT_TOOL_CREATE, name="D", kind="nope", inline_content="x"), _ctx())
        assert _payload(res)["error"]["code"] == "artifact_invalid"

    @pytest.mark.asyncio
    async def test_exactly_one_content_input(self):
        ex = ArtifactToolExecutor(FakeArtifactService())
        # neither
        r1 = await ex.execute(_req(ARTIFACT_TOOL_CREATE, name="D", kind="text"), _ctx())
        assert _payload(r1)["error"]["code"] == "artifact_invalid"
        # both
        r2 = await ex.execute(_req(ARTIFACT_TOOL_CREATE, name="D", kind="text",
                                   inline_content="x", workspace_ref="workspace:f"), _ctx())
        assert _payload(r2)["error"]["code"] == "artifact_invalid"

    @pytest.mark.asyncio
    async def test_workspace_ref_bad_scheme_rejected(self):
        ex = ArtifactToolExecutor(FakeArtifactService())
        res = await ex.execute(_req(ARTIFACT_TOOL_CREATE, name="D", kind="text",
                                    workspace_ref="item:evil"), _ctx())
        assert _payload(res)["error"]["code"] == "artifact_invalid"

    @pytest.mark.asyncio
    async def test_export_capabilities_failure_does_not_fail_create(self):
        svc = FakeArtifactService()
        svc.create_return = _artifact()
        svc.raise_on_export_capabilities = RuntimeError("boom")
        ex = ArtifactToolExecutor(svc)
        res = await ex.execute(_req(ARTIFACT_TOOL_CREATE, name="D", kind="text", inline_content="x"), _ctx())
        payload = _payload(res)
        assert payload["success"] is True
        assert payload["capabilities"] == []


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


class TestList:
    @pytest.mark.asyncio
    async def test_session_scoped(self):
        svc = FakeArtifactService()
        svc.list_return = ArtifactListPage(items=(_artifact(id="a1"), _artifact(id="a2")))
        ex = ArtifactToolExecutor(svc)
        res = await ex.execute(_req(ARTIFACT_TOOL_LIST), _ctx(session_id="sess-42"))
        assert _payload(res)["count"] == 2
        assert svc.list_calls[0]["source_session_id"] == "sess-42"

    @pytest.mark.asyncio
    async def test_cursor_round_trip(self):
        svc = FakeArtifactService()
        cursor = ArtifactListCursor(updated_at=_now(), artifact_id="a2")
        svc.list_return = ArtifactListPage(items=(), next_cursor=cursor)
        ex = ArtifactToolExecutor(svc)
        r1 = await ex.execute(_req(ARTIFACT_TOOL_LIST), _ctx())
        token = _payload(r1)["next_cursor"]
        assert token is not None
        await ex.execute(_req(ARTIFACT_TOOL_LIST, cursor=token), _ctx())
        decoded = svc.list_calls[1]["cursor"]
        assert isinstance(decoded, ArtifactListCursor)
        assert decoded.artifact_id == "a2"

    @pytest.mark.asyncio
    async def test_invalid_cursor(self):
        ex = ArtifactToolExecutor(FakeArtifactService())
        res = await ex.execute(_req(ARTIFACT_TOOL_LIST, cursor="not-json"), _ctx())
        assert _payload(res)["error"]["code"] == "artifact_invalid"

    @pytest.mark.asyncio
    async def test_no_content_ref_leaked(self):
        svc = FakeArtifactService()
        svc.list_return = ArtifactListPage(items=(_artifact(),))
        ex = ArtifactToolExecutor(svc)
        res = await ex.execute(_req(ARTIFACT_TOOL_LIST), _ctx())
        item = _payload(res)["items"][0]
        assert "content_ref" not in item
        assert "inline_content" not in item


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


class TestRead:
    @pytest.mark.asyncio
    async def test_text_pagination(self):
        svc = FakeArtifactService()
        svc.artifacts["a1"] = _artifact(id="a1", current_revision_id="r1")
        text = "line0\nline1\nline2\nline3\n"
        rev = _revision(id="r1", artifact_id="a1", inline=text)
        svc.revisions["r1"] = rev
        svc.contents["r1"] = text.encode("utf-8")
        ex = ArtifactToolExecutor(svc)
        res = await ex.execute(_req(ARTIFACT_TOOL_READ, artifact_id="a1", line_offset=1, line_limit=2), _ctx())
        payload = _payload(res)
        assert payload["content"] == "line1\nline2\n"
        assert payload["truncated"] is True
        assert payload["next_line_offset"] == 3
        assert payload["line_offset"] == 1
        assert payload["redacted"] is True
        assert payload["binary"] is False

    @pytest.mark.asyncio
    async def test_text_pagination_end_no_next(self):
        svc = FakeArtifactService()
        svc.artifacts["a1"] = _artifact(id="a1", current_revision_id="r1")
        text = "line0\nline1\n"
        svc.revisions["r1"] = _revision(id="r1", artifact_id="a1", inline=text)
        svc.contents["r1"] = text.encode("utf-8")
        ex = ArtifactToolExecutor(svc)
        res = await ex.execute(_req(ARTIFACT_TOOL_READ, artifact_id="a1"), _ctx())
        payload = _payload(res)
        assert payload["truncated"] is False
        assert payload["next_line_offset"] is None

    @pytest.mark.asyncio
    async def test_binary_returns_metadata_only(self):
        svc = FakeArtifactService()
        svc.artifacts["a1"] = _artifact(id="a1", current_revision_id="r1", kind=ArtifactKind.IMAGE)
        from app.domain.artifact import ArtifactRevision as AR
        rev = AR(
            id="r1", artifact_id="a1", revision_number=1, parent_revision_id=None,
            rollback_from_revision_id=None, content_ref="item:binary", inline_content=None,
            size=10, checksum=_CHKSUM, mime="image/png", kind=ArtifactKind.IMAGE,
            created_at=_now(), created_by="user-1", source_session_id="sess-1",
        )
        svc.revisions["r1"] = rev
        svc.contents["r1"] = b"\x89PNG\r\n"
        ex = ArtifactToolExecutor(svc)
        res = await ex.execute(_req(ARTIFACT_TOOL_READ, artifact_id="a1"), _ctx())
        payload = _payload(res)
        assert payload["binary"] is True
        assert payload["content"] is None
        assert payload["content_ref"] is None
        assert payload["download_url"] == "/chat/artifacts/a1/content"
        assert payload["size"] == 10

    @pytest.mark.asyncio
    async def test_binary_historical_revision_download_url(self):
        svc = FakeArtifactService()
        svc.artifacts["a1"] = _artifact(id="a1", current_revision_id="r1", kind=ArtifactKind.IMAGE)
        from app.domain.artifact import ArtifactRevision as AR
        rev = AR(
            id="r-old", artifact_id="a1", revision_number=1, parent_revision_id=None,
            rollback_from_revision_id=None, content_ref="item:binary", inline_content=None,
            size=10, checksum=_CHKSUM, mime="image/png", kind=ArtifactKind.IMAGE,
            created_at=_now(), created_by="user-1", source_session_id="sess-1",
        )
        svc.revisions["r-old"] = rev
        svc.contents["r-old"] = b"\x89PNG\r\n"
        ex = ArtifactToolExecutor(svc)
        res = await ex.execute(_req(ARTIFACT_TOOL_READ, artifact_id="a1", revision_id="r-old"), _ctx())
        payload = _payload(res)
        assert payload["download_url"] == "/chat/artifacts/a1/revisions/r-old/content"
        assert payload["revision_id"] == "r-old"

    @pytest.mark.asyncio
    async def test_first_line_too_large(self):
        svc = FakeArtifactService()
        svc.artifacts["a1"] = _artifact(id="a1", current_revision_id="r1")
        big = "x" * 200 + "\n"
        svc.revisions["r1"] = _revision(id="r1", artifact_id="a1", inline=big)
        svc.contents["r1"] = big.encode("utf-8")
        ex = ArtifactToolExecutor(svc, read_max_bytes=64)
        res = await ex.execute(_req(ARTIFACT_TOOL_READ, artifact_id="a1"), _ctx())
        payload = _payload(res)
        assert payload["error"]["code"] == "artifact_read_too_large"
        assert payload["error"]["retryable"] is False

    @pytest.mark.asyncio
    async def test_cross_artifact_revision_not_found(self):
        svc = FakeArtifactService()
        svc.artifacts["a1"] = _artifact(id="a1", current_revision_id="r1")
        svc.revisions["r1"] = _revision(id="r1", artifact_id="a1")
        svc.contents["r1"] = b"x\n"
        svc.raise_on_get_revision_content = ArtifactRevisionNotFoundError("cross")
        ex = ArtifactToolExecutor(svc)
        res = await ex.execute(_req(ARTIFACT_TOOL_READ, artifact_id="a1", revision_id="other-artifact-rev"), _ctx())
        payload = _payload(res)
        assert payload["error"]["code"] == "artifact_revision_not_found"

    @pytest.mark.asyncio
    async def test_session_scope_mismatch_no_leak(self):
        svc = FakeArtifactService()
        svc.artifacts["a1"] = _artifact(id="a1", source_session_id="other-session")
        ex = ArtifactToolExecutor(svc)
        res = await ex.execute(_req(ARTIFACT_TOOL_READ, artifact_id="a1"), _ctx(session_id="sess-1"))
        payload = _payload(res)
        assert payload["error"]["code"] == "artifact_not_found"
        # no leak of which case
        assert "other-session" not in json.dumps(payload)

    @pytest.mark.asyncio
    async def test_line_limit_bounds(self):
        ex = ArtifactToolExecutor(FakeArtifactService())
        r = await ex.execute(_req(ARTIFACT_TOOL_READ, artifact_id="a1", line_limit=0), _ctx())
        assert _payload(r)["error"]["code"] == "artifact_invalid"
        r2 = await ex.execute(_req(ARTIFACT_TOOL_READ, artifact_id="a1", line_limit=501), _ctx())
        assert _payload(r2)["error"]["code"] == "artifact_invalid"


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


class TestUpdate:
    @pytest.mark.asyncio
    async def test_success_returns_revision_meta(self):
        svc = FakeArtifactService()
        svc.artifacts["a1"] = _artifact(id="a1", current_revision_id="r1")
        rev = _revision(id="r2", artifact_id="a1", number=2, parent="r1")
        result = UpdateRevisionResult(diff_summary="c3", content_unchanged=False, publish_sync_state="outdated")
        svc.update_return = (rev, result)
        ex = ArtifactToolExecutor(svc)
        res = await ex.execute(_req(ARTIFACT_TOOL_UPDATE, artifact_id="a1",
                                    expected_revision_id="r1", content="# new"), _ctx())
        payload = _payload(res)
        assert payload["revision_id"] == "r2"
        assert payload["revision_number"] == 2
        assert payload["diff_summary"] == "c3"
        assert payload["content_unchanged"] is False
        assert payload["publish_sync_state"] == "outdated"
        assert svc.update_calls[0]["inline_content"] == "# new"

    @pytest.mark.asyncio
    async def test_cas_conflict_retryable(self):
        svc = FakeArtifactService()
        svc.raise_on_update = ArtifactRevisionConflictError("stale")
        ex = ArtifactToolExecutor(svc)
        res = await ex.execute(_req(ARTIFACT_TOOL_UPDATE, artifact_id="a1",
                                    expected_revision_id="r1", content="x"), _ctx())
        payload = _payload(res)
        assert payload["error"]["code"] == "artifact_revision_conflict"
        assert payload["error"]["retryable"] is True

    @pytest.mark.asyncio
    async def test_text_patch_structural_validation(self):
        ex = ArtifactToolExecutor(FakeArtifactService())
        # empty
        r = await ex.execute(_req(ARTIFACT_TOOL_UPDATE, artifact_id="a1", expected_revision_id="r",
                                  text_patch=[]), _ctx())
        assert _payload(r)["error"]["code"] == "artifact_revision_invalid"
        # bad mode
        r = await ex.execute(_req(ARTIFACT_TOOL_UPDATE, artifact_id="a1", expected_revision_id="r",
                                  text_patch=[{"search": "a", "replace": "b", "mode": "regex"}]), _ctx())
        assert _payload(r)["error"]["code"] == "artifact_revision_invalid"
        # unknown field
        r = await ex.execute(_req(ARTIFACT_TOOL_UPDATE, artifact_id="a1", expected_revision_id="r",
                                  text_patch=[{"search": "a", "replace": "b", "mode": "first", "extra": 1}]), _ctx())
        assert _payload(r)["error"]["code"] == "artifact_revision_invalid"
        # empty search
        r = await ex.execute(_req(ARTIFACT_TOOL_UPDATE, artifact_id="a1", expected_revision_id="r",
                                  text_patch=[{"search": "", "replace": "b", "mode": "first"}]), _ctx())
        assert _payload(r)["error"]["code"] == "artifact_revision_invalid"

    @pytest.mark.asyncio
    async def test_text_patch_too_many_ops(self):
        ex = ArtifactToolExecutor(FakeArtifactService())
        ops = [{"search": "a", "replace": "b", "mode": "first"} for _ in range(101)]
        r = await ex.execute(_req(ARTIFACT_TOOL_UPDATE, artifact_id="a1", expected_revision_id="r",
                                  text_patch=ops), _ctx())
        assert _payload(r)["error"]["code"] == "artifact_revision_invalid"

    @pytest.mark.asyncio
    async def test_exactly_one_content_input(self):
        ex = ArtifactToolExecutor(FakeArtifactService())
        # none
        r = await ex.execute(_req(ARTIFACT_TOOL_UPDATE, artifact_id="a1", expected_revision_id="r"), _ctx())
        assert _payload(r)["error"]["code"] == "artifact_invalid"
        # two
        r = await ex.execute(_req(ARTIFACT_TOOL_UPDATE, artifact_id="a1", expected_revision_id="r",
                                  content="x", text_patch=[{"search": "a", "replace": "b", "mode": "first"}]), _ctx())
        assert _payload(r)["error"]["code"] == "artifact_invalid"

    @pytest.mark.asyncio
    async def test_text_patch_cleaned_passed_to_service(self):
        svc = FakeArtifactService()
        svc.update_return = (_revision(id="r2", number=2), UpdateRevisionResult("s", False, "outdated"))
        ex = ArtifactToolExecutor(svc)
        await ex.execute(_req(ARTIFACT_TOOL_UPDATE, artifact_id="a1", expected_revision_id="r1",
                              text_patch=[{"search": "a", "replace": "b", "mode": "first"},
                                           {"search": "c", "replace": "d", "mode": "all"}]), _ctx())
        patch = svc.update_calls[0]["text_patch"]
        assert patch == [{"search": "a", "replace": "b", "mode": "first"},
                         {"search": "c", "replace": "d", "mode": "all"}]


# ---------------------------------------------------------------------------
# list_revisions / diff / rollback
# ---------------------------------------------------------------------------


class TestListRevisions:
    @pytest.mark.asyncio
    async def test_is_current_and_is_published(self):
        svc = FakeArtifactService()
        svc.artifacts["a1"] = _artifact(id="a1", current_revision_id="r2")
        r1 = _revision(id="r1", artifact_id="a1", number=1)
        r2 = _revision(id="r2", artifact_id="a1", number=2, parent="r1")
        svc.list_revisions_return = RevisionListPage(items=(r2, r1))
        svc.active_publish = PublishedArtifact(
            publish_id="pub", artifact_id="a1", snapshot_name="doc",
            snapshot_kind=ArtifactKind.MARKDOWN, snapshot_mime="text/markdown",
            snapshot_content_ref=None, snapshot_inline_content="x", snapshot_size=1,
            snapshot_checksum=_CHKSUM, published_at=_now(), published_by="u",
            status=PublishedArtifactStatus.ACTIVE, published_revision_id="r1",
        )
        ex = ArtifactToolExecutor(svc)
        res = await ex.execute(_req(ARTIFACT_TOOL_LIST_REVISIONS, artifact_id="a1"), _ctx())
        items = _payload(res)["items"]
        by_id = {it["id"]: it for it in items}
        assert by_id["r2"]["is_current"] is True
        assert by_id["r2"]["is_published"] is False
        assert by_id["r1"]["is_current"] is False
        assert by_id["r1"]["is_published"] is True

    @pytest.mark.asyncio
    async def test_bad_cursor_returns_revision_invalid(self):
        svc = FakeArtifactService()
        svc.artifacts["a1"] = _artifact(id="a1", current_revision_id="r1")
        ex = ArtifactToolExecutor(svc)
        res = await ex.execute(_req(ARTIFACT_TOOL_LIST_REVISIONS, artifact_id="a1", cursor="nope"), _ctx())
        assert _payload(res)["error"]["code"] == "artifact_revision_invalid"


class TestDiff:
    @pytest.mark.asyncio
    async def test_success_redacted(self):
        svc = FakeArtifactService()
        svc.artifacts["a1"] = _artifact(id="a1", current_revision_id="r1")
        svc.diff_return = DiffResult(diff_text="--- a\n+++ b\n", binary_changed=False)
        ex = ArtifactToolExecutor(svc)
        res = await ex.execute(_req(ARTIFACT_TOOL_DIFF, artifact_id="a1",
                                    from_revision_id="r1", to_revision_id="r2", context_lines=5), _ctx())
        payload = _payload(res)
        assert payload["diff_text"] == "--- a\n+++ b\n"
        assert payload["binary_changed"] is False
        assert payload["redacted"] is True
        assert svc.diff_calls[0]["context_lines"] == 5

    @pytest.mark.asyncio
    async def test_diff_too_large_not_retryable(self):
        svc = FakeArtifactService()
        svc.artifacts["a1"] = _artifact(id="a1", current_revision_id="r1")
        svc.raise_on_diff = ArtifactDiffTooLargeError("big")
        ex = ArtifactToolExecutor(svc)
        res = await ex.execute(_req(ARTIFACT_TOOL_DIFF, artifact_id="a1",
                                    from_revision_id="r1", to_revision_id="r2"), _ctx())
        payload = _payload(res)
        assert payload["error"]["code"] == "artifact_diff_too_large"
        assert payload["error"]["retryable"] is False

    @pytest.mark.asyncio
    async def test_diff_unsupported(self):
        svc = FakeArtifactService()
        svc.artifacts["a1"] = _artifact(id="a1", current_revision_id="r1")
        svc.raise_on_diff = ArtifactDiffUnsupportedError("mixed")
        ex = ArtifactToolExecutor(svc)
        res = await ex.execute(_req(ARTIFACT_TOOL_DIFF, artifact_id="a1",
                                    from_revision_id="r1", to_revision_id="r2"), _ctx())
        assert _payload(res)["error"]["code"] == "artifact_diff_unsupported"


class TestRollback:
    @pytest.mark.asyncio
    async def test_success(self):
        svc = FakeArtifactService()
        rev = _revision(id="r3", artifact_id="a1", number=3, parent="r2", rollback_from="r2")
        svc.rollback_return = (rev, UpdateRevisionResult("rb", False, "outdated"))
        ex = ArtifactToolExecutor(svc)
        res = await ex.execute(_req(ARTIFACT_TOOL_ROLLBACK, artifact_id="a1",
                                    target_revision_id="r1", expected_revision_id="r2"), _ctx())
        payload = _payload(res)
        assert payload["revision_id"] == "r3"
        assert payload["revision_number"] == 3
        assert svc.rollback_calls[0]["target"] == "r1"


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


class TestPublish:
    @pytest.mark.asyncio
    async def test_success(self):
        svc = FakeArtifactService()
        svc.artifacts["a1"] = _artifact(id="a1", current_revision_id="r1")
        svc.revisions["r1"] = _revision(id="r1", artifact_id="a1", number=1)
        ex = ArtifactToolExecutor(svc)
        res = await ex.execute(_req(ARTIFACT_TOOL_PUBLISH, artifact_id="a1",
                                    revision_id="r1", expected_current_revision_id="r1"), _ctx())
        payload = _payload(res)
        assert payload["publish_id"] == "pub-1"
        assert payload["published_revision_id"] == "rev-1"
        assert payload["share_url"] == "/p/pub-1"
        assert payload["reused"] is False
        assert payload["publish_sync_state"] == "current"
        assert payload["revision_number"] == 1

    @pytest.mark.asyncio
    async def test_publish_blocked(self):
        svc = FakeArtifactService()
        svc.raise_on_publish = PublishBlockedError("denied")
        ex = ArtifactToolExecutor(svc)
        res = await ex.execute(_req(ARTIFACT_TOOL_PUBLISH, artifact_id="a1",
                                    revision_id="r1", expected_current_revision_id="r1"), _ctx())
        payload = _payload(res)
        assert payload["error"]["code"] == "publish_blocked"
        assert payload["error"]["retryable"] is False


# ---------------------------------------------------------------------------
# Cross-cutting: error envelope, terminal, unknown tool, leak prevention
# ---------------------------------------------------------------------------


class TestErrorContract:
    @pytest.mark.asyncio
    async def test_error_envelope_shape(self):
        svc = FakeArtifactService()
        svc.raise_on_update = ArtifactRevisionConflictError("stale")
        ex = ArtifactToolExecutor(svc)
        res = await ex.execute(_req(ARTIFACT_TOOL_UPDATE, artifact_id="a1",
                                    expected_revision_id="r", content="x"), _ctx())
        payload = _payload(res)
        assert set(payload.keys()) == {"success", "error"}
        assert set(payload["error"].keys()) == {"code", "message", "retryable"}
        assert res.terminal is False

    @pytest.mark.asyncio
    async def test_unknown_exception_maps_to_internal_error_no_leak(self):
        svc = FakeArtifactService()
        svc.raise_on_update = RuntimeError("DB connection lost at /var/lib/x.db")
        ex = ArtifactToolExecutor(svc)
        res = await ex.execute(_req(ARTIFACT_TOOL_UPDATE, artifact_id="a1",
                                    expected_revision_id="r", content="x"), _ctx())
        payload = _payload(res)
        assert payload["error"]["code"] == "artifact_internal_error"
        assert payload["error"]["retryable"] is False
        body = json.dumps(payload)
        assert "/var/lib" not in body
        assert "RuntimeError" not in body
        assert "connection lost" not in body

    @pytest.mark.asyncio
    async def test_unknown_tool_name(self):
        ex = ArtifactToolExecutor(FakeArtifactService())
        res = await ex.execute(_req("artifact_noop", artifact_id="a1"), _ctx())
        assert _payload(res)["error"]["code"] == "artifact_invalid"

    @pytest.mark.asyncio
    async def test_migration_incomplete_retryable(self):
        svc = FakeArtifactService()
        svc.artifacts["a1"] = _artifact(id="a1", current_revision_id="r1")
        svc.raise_on_get_revision_content = ArtifactMigrationIncompleteError("mig")
        ex = ArtifactToolExecutor(svc)
        res = await ex.execute(_req(ARTIFACT_TOOL_READ, artifact_id="a1"), _ctx())
        payload = _payload(res)
        assert payload["error"]["code"] == "artifact_migration_incomplete"
        assert payload["error"]["retryable"] is True

    @pytest.mark.asyncio
    async def test_no_content_ref_in_read_error(self):
        svc = FakeArtifactService()
        svc.artifacts["a1"] = _artifact(id="a1", current_revision_id="r1")
        svc.raise_on_get_revision_content = RuntimeError("content_ref=item:secret/path")
        ex = ArtifactToolExecutor(svc)
        res = await ex.execute(_req(ARTIFACT_TOOL_READ, artifact_id="a1"), _ctx())
        body = json.dumps(_payload(res))
        assert "item:secret" not in body

    @pytest.mark.asyncio
    async def test_terminal_false_on_success_and_error(self):
        svc = FakeArtifactService()
        svc.create_return = _artifact()
        ex = ArtifactToolExecutor(svc)
        ok = await ex.execute(_req(ARTIFACT_TOOL_CREATE, name="D", kind="text", inline_content="x"), _ctx())
        err = await ex.execute(_req(ARTIFACT_TOOL_CREATE, kind="text", inline_content="x"), _ctx())
        assert ok.terminal is False
        assert err.terminal is False

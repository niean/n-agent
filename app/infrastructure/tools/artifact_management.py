"""T8: Artifact tool executor (Infrastructure Layer).

Dispatches the 8 ``artifact_*`` tools to ``ArtifactService``, session-bound.
Mirrors ``UserTaskToolExecutor``'s trusted-context discipline (模式十二):

  - source_type=AGENT conversational tools, realtime (DEFAULT) only; hidden in
    SAFE_ONLY even when granted (realtime_only=True on the definitions).
  - Provenance (session_id / run_id / actor_id) comes exclusively from
    ``ctx.session_id`` and ``ctx.trusted_metadata``; the tool schema accepts
    none of these as arguments, so forged provenance is impossible.
  - Never reads untrusted ``ctx.metadata``.
  - No traceback / class name / absolute path / content_ref leaks: every
    failure returns ``{success:false, error:{code, message, retryable}}`` with
    a stable scrubbed message; unknown exceptions map to
    ``artifact_internal_error``.

Error contract (spec lines 216-227): 409/503 -> retryable=true, else false.
Session scope (spec line 50/73): ``artifact_list`` filters by
``ctx.session_id``; the SAFE read tools (read / list_revisions / diff) run an
Artifact visibility check (``source_session_id == ctx.session_id`` when set,
else ``artifact_not_found`` without leaking which case). Write tools
(update / rollback / publish) rely on the service-enforced ArtifactPolicy
EDIT/PUBLISH admission (spec lines 70-72), not executor-side session scoping.
``terminal=False``: artifact operations are never a conversation terminal.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Protocol

from app.application.artifact_service import (
    ArtifactTooLargeError,
    PublishBlockedError,
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
)
from app.domain.artifact import (
    ArtifactConflictError,
    ArtifactContentUnavailableError,
    ArtifactDiffTooLargeError,
    ArtifactDiffUnsupportedError,
    ArtifactExportError,
    ArtifactExportTooLargeError,
    ArtifactExportUnsupportedError,
    ArtifactKind,
    ArtifactListCursor,
    ArtifactMigrationIncompleteError,
    ArtifactNotFoundError,
    ArtifactReadTooLargeError,
    ArtifactRevisionConflictError,
    ArtifactRevisionNotFoundError,
    ArtifactRevisionValidationError,
    ArtifactSource,
    ArtifactStatus,
    ArtifactValidationError,
    RevisionListCursor,
)
from app.domain.tool import (
    ToolCallRequest,
    ToolExecutionContext,
    ToolExecutor,
    ToolResult,
    ToolResultStatus,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_READ_MAX_BYTES = 64 * 1024
_DEFAULT_LINE_LIMIT = 200
_MAX_LINE_LIMIT = 500
_DEFAULT_LIST_LIMIT = 50
_MAX_LIST_LIMIT = 100
_DEFAULT_CONTEXT_LINES = 3
_MAX_CONTEXT_LINES = 20
_MAX_TEXT_PATCH_OPS = 100

# Binary kinds (image/pdf/other) return metadata only on read/diff.
_BINARY_KIND_VALUES = frozenset({"image", "pdf", "other"})

# Default MIME per kind -- mirrors app/interfaces/http/artifact_routes._default_mime.
_DEFAULT_MIME: dict[str, str] = {
    "markdown": "text/markdown",
    "html": "text/html",
    "code": "text/plain",
    "text": "text/plain",
    "json": "application/json",
    "csv": "text/csv",
    "data": "application/octet-stream",
    "document": "text/plain",
    "image": "image/png",
    "pdf": "application/pdf",
    "other": "application/octet-stream",
}

# Stable, scrubbed messages per error code (never str(exc)).
_ERROR_MESSAGES: dict[str, str] = {
    "artifact_not_found": "artifact not found",
    "artifact_revision_not_found": "revision not found for this artifact",
    "artifact_revision_conflict": "revision changed since expected_revision_id; re-read and retry",
    "artifact_migration_incomplete": "artifact revision migration is incomplete; retry shortly",
    "artifact_revision_invalid": "revision content or patch is invalid",
    "artifact_invalid": "artifact arguments are invalid",
    "artifact_read_too_large": "requested content slice exceeds the read limit",
    "artifact_diff_too_large": "diff output exceeds the size limit",
    "artifact_diff_unsupported": "diff is not supported for the given revision pair",
    "artifact_export_unsupported": "export format is not supported for this artifact",
    "artifact_export_too_large": "export output exceeds the size limit",
    "artifact_export_failed": "export failed",
    "artifact_content_unavailable": "artifact content is temporarily unavailable",
    "artifact_conflict": "artifact state conflict",
    "artifact_too_large": "artifact exceeds the size limit",
    "publish_blocked": "publish is blocked by policy",
    "artifact_internal_error": "internal error",
    "session_missing": "session context is required",
}

# Domain/application exception -> (code, http_status). Subclasses MUST precede
# parents (isinstance ordering). retryable = http_status in (409, 503).
_ERROR_MAP: tuple[tuple[type[Exception], str, int], ...] = (
    (ArtifactRevisionValidationError, "artifact_revision_invalid", 422),
    (ArtifactExportUnsupportedError, "artifact_export_unsupported", 422),
    (ArtifactExportTooLargeError, "artifact_export_too_large", 413),
    (ArtifactExportError, "artifact_export_failed", 500),
    (ArtifactValidationError, "artifact_invalid", 422),
    (ArtifactRevisionConflictError, "artifact_revision_conflict", 409),
    (ArtifactRevisionNotFoundError, "artifact_revision_not_found", 404),
    (ArtifactMigrationIncompleteError, "artifact_migration_incomplete", 503),
    (ArtifactReadTooLargeError, "artifact_read_too_large", 413),
    (ArtifactDiffTooLargeError, "artifact_diff_too_large", 413),
    (ArtifactDiffUnsupportedError, "artifact_diff_unsupported", 422),
    (ArtifactContentUnavailableError, "artifact_content_unavailable", 409),
    (ArtifactConflictError, "artifact_conflict", 409),
    (ArtifactTooLargeError, "artifact_too_large", 413),
    (PublishBlockedError, "publish_blocked", 422),
    (ArtifactNotFoundError, "artifact_not_found", 404),
)


class ArtifactToolServiceProtocol(Protocol):
    """The async ArtifactService subset the executor depends on.

    ``ArtifactService`` already implements these; tests substitute an async fake.
    """

    async def create_artifact(
        self, *, name: str, kind: ArtifactKind, mime: str,
        inline_content: str | None = None, workspace_ref: str | None = None,
        source_kind: ArtifactSource = ArtifactSource.MANUAL,
        source_session_id: str | None = None, source_run_id: str | None = None,
        summary: str = "", classification: str | None = None,
        labels: tuple[str, ...] | None = None, created_by: str = "",
    ) -> Any: ...

    async def list_artifacts(
        self, *, source_session_id: str | None = None,
        kind: ArtifactKind | None = None, status: Any = None,
        cursor: ArtifactListCursor | None = None, limit: int = 50,
    ) -> Any: ...

    async def get_artifact(self, artifact_id: str) -> Any: ...
    async def get_current_revision(self, artifact_id: str) -> Any: ...

    async def get_revision_content(
        self, artifact_id: str, revision_id: str | None = None,
    ) -> tuple[bytes, Any]: ...

    async def update_revision(
        self, artifact_id: str, *, expected_revision_id: str,
        inline_content: str | None = None, workspace_ref: str | None = None,
        text_patch: list[dict[str, object]] | None = None,
        change_summary: str = "", kind: ArtifactKind | None = None,
        mime: str | None = None,
    ) -> tuple[Any, Any]: ...

    async def list_revisions(
        self, artifact_id: str, *, cursor: RevisionListCursor | None = None,
        limit: int = 50,
    ) -> Any: ...

    async def diff_revisions(
        self, artifact_id: str, from_id: str, to_id: str, *,
        context_lines: int = 3,
    ) -> Any: ...

    async def rollback(
        self, artifact_id: str, target_revision_id: str, *,
        expected_revision_id: str, change_summary: str = "",
    ) -> tuple[Any, Any]: ...

    async def publish_revision(
        self, artifact_id: str, *, revision_id: str,
        expected_current_revision_id: str,
    ) -> Any: ...

    async def export_capabilities(
        self, artifact_id: str, *, revision_id: str | None = None,
    ) -> tuple[str, ...]: ...

    async def get_active_publish(self, artifact_id: str) -> Any: ...


class ArtifactToolExecutor(ToolExecutor):
    """Dispatch the 8 artifact_* tools to ArtifactService, session-bound.

    Trusted provenance only (session_id / run_id / actor_id from context);
    SAFE read tools enforce Artifact visibility; write tools rely on the
    service's ArtifactPolicy admission. ``terminal=False``.
    """

    def __init__(
        self,
        service: ArtifactToolServiceProtocol,
        *,
        read_max_bytes: int = _DEFAULT_READ_MAX_BYTES,
    ):
        self.service = service
        self._read_max_bytes = read_max_bytes

    async def execute(
        self,
        request: ToolCallRequest,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        start = time.monotonic()
        try:
            if context is None or not (context.session_id or "").strip():
                raise _ArtifactDenied("session_missing")
            if request.name == ARTIFACT_TOOL_CREATE:
                payload = await self._handle_create(request, context)
            elif request.name == ARTIFACT_TOOL_LIST:
                payload = await self._handle_list(request, context)
            elif request.name == ARTIFACT_TOOL_READ:
                payload = await self._handle_read(request, context)
            elif request.name == ARTIFACT_TOOL_UPDATE:
                payload = await self._handle_update(request, context)
            elif request.name == ARTIFACT_TOOL_LIST_REVISIONS:
                payload = await self._handle_list_revisions(request, context)
            elif request.name == ARTIFACT_TOOL_DIFF:
                payload = await self._handle_diff(request, context)
            elif request.name == ARTIFACT_TOOL_ROLLBACK:
                payload = await self._handle_rollback(request, context)
            elif request.name == ARTIFACT_TOOL_PUBLISH:
                payload = await self._handle_publish(request, context)
            else:
                raise _ArtifactInvalid("artifact_invalid", f"unknown tool: {request.name}")
            status = ToolResultStatus.SUCCESS
        except _ArtifactDenied as exc:
            payload = _error_payload(exc.code, _ERROR_MESSAGES.get(exc.code, "denied"), False)
            status = ToolResultStatus.PERMISSION_DENIED
        except _ArtifactInvalid as exc:
            payload = _error_payload(exc.code, exc.message or _ERROR_MESSAGES.get(exc.code, "invalid"), False)
            status = ToolResultStatus.ERROR
        except Exception as exc:  # defensive: no traceback/class/path leak
            code, http_status = _map_exception(exc)
            if code is None:
                code, http_status = "artifact_internal_error", 500
            retryable = http_status in (409, 503)
            payload = _error_payload(code, _ERROR_MESSAGES.get(code, "error"), retryable)
            status = ToolResultStatus.ERROR
        return ToolResult(
            tool_call_id=request.id,
            tool_name=request.name,
            status=status,
            content=json.dumps(payload, ensure_ascii=False, default=str),
            duration_ms=int((time.monotonic() - start) * 1000),
            terminal=False,
        )

    # ------------------------------------------------------------------
    # Provenance + visibility helpers
    # ------------------------------------------------------------------

    def _provenance(
        self, ctx: ToolExecutionContext,
    ) -> tuple[str, str, str]:
        """Return trusted (session_id, run_id, actor_id) from context.

        Missing run_id/actor collapse to "" (spec line 48). These never come
        from tool arguments.
        """
        tm = ctx.trusted_metadata or {}
        run_id = str(tm.get("run_id") or "")
        actor_id = str(tm.get("actor_id") or "")
        return ctx.session_id or "", run_id, actor_id

    async def _check_visibility(self, artifact_id: str, ctx: ToolExecutionContext) -> Any:
        """Artifact visibility check for SAFE read tools (spec line 73).

        Fetches the artifact and verifies ``source_session_id == ctx.session_id``
        when the artifact is session-bound. Cross-session / not-found both map
        to ``artifact_not_found`` without leaking which case occurred. Artifacts
        with ``source_session_id is None`` (manual/legacy) remain visible.

        Returns the Artifact for callers to reuse (current_revision_id, name).
        """
        art = await self.service.get_artifact(artifact_id)  # raises NotFound -> mapped
        sid = getattr(art, "source_session_id", None)
        if sid is not None and sid != ctx.session_id:
            raise ArtifactNotFoundError("artifact not found")  # no leak
        return art

    async def _safe_get_artifact(self, artifact_id: str) -> Any:
        """Best-effort artifact fetch for write-tool metadata (name/kind).

        The write has already committed; a post-write read failure must not turn
        a successful write into an error response. Returns None on failure
        (callers degrade name->"" via getattr).
        """
        try:
            return await self.service.get_artifact(artifact_id)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # artifact_create
    # ------------------------------------------------------------------

    async def _handle_create(
        self, request: ToolCallRequest, ctx: ToolExecutionContext,
    ) -> dict[str, Any]:
        args = request.arguments or {}
        name = args.get("name")
        if not isinstance(name, str) or not name.strip():
            raise _ArtifactInvalid("artifact_invalid", "name is required")
        name = name.strip()

        kind = _parse_kind(args.get("kind"))
        if kind is None:
            raise _ArtifactInvalid("artifact_invalid", "kind is required")

        inline_content = args.get("inline_content")
        workspace_ref = args.get("workspace_ref")
        if inline_content is not None and not isinstance(inline_content, str):
            raise _ArtifactInvalid("artifact_invalid", "inline_content must be a string")
        if workspace_ref is not None:
            if not isinstance(workspace_ref, str) or not workspace_ref.strip():
                raise _ArtifactInvalid("artifact_invalid", "workspace_ref must be a non-empty string")
            if not workspace_ref.startswith("workspace:"):
                raise _ArtifactInvalid("artifact_invalid", "workspace_ref must use the workspace: scheme")
        # exactly one content input
        if (inline_content is not None) == (workspace_ref is not None):
            raise _ArtifactInvalid(
                "artifact_invalid", "exactly one of inline_content or workspace_ref is required",
            )

        summary = args.get("summary", "")
        if not isinstance(summary, str):
            raise _ArtifactInvalid("artifact_invalid", "summary must be a string")
        classification = args.get("classification")
        if classification is not None and not isinstance(classification, str):
            raise _ArtifactInvalid("artifact_invalid", "classification must be a string")
        labels = _parse_labels(args.get("labels"))

        session_id, run_id, actor_id = self._provenance(ctx)
        mime = _DEFAULT_MIME.get(kind.value, "application/octet-stream")

        artifact = await self.service.create_artifact(
            name=name,
            kind=kind,
            mime=mime,
            inline_content=inline_content,
            workspace_ref=workspace_ref,
            source_kind=ArtifactSource.SESSION,
            source_session_id=session_id,
            source_run_id=run_id or None,
            summary=summary,
            classification=classification,
            labels=labels,
            created_by=actor_id,
        )
        artifact_id = getattr(artifact, "id", "")
        revision_id = getattr(artifact, "current_revision_id", None)
        # export capabilities for the initial revision (best-effort: create
        # already succeeded; degrade to [] rather than failing the response).
        try:
            capabilities = await self.service.export_capabilities(artifact_id)
        except Exception:
            capabilities = ()
        return {
            "success": True,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
            "revision_number": 1,  # create always materializes revision_number=1
            "name": getattr(artifact, "name", name),
            "kind": _enum_value(getattr(artifact, "kind", kind)),
            "mime": getattr(artifact, "mime", mime),
            "size": getattr(artifact, "size", 0),
            "checksum": getattr(artifact, "checksum", ""),
            "summary": getattr(artifact, "summary", summary),
            "publish_sync_state": "unpublished",  # new artifact, no active publish
            "capabilities": list(capabilities),
        }

    # ------------------------------------------------------------------
    # artifact_list
    # ------------------------------------------------------------------

    async def _handle_list(
        self, request: ToolCallRequest, ctx: ToolExecutionContext,
    ) -> dict[str, Any]:
        args = request.arguments or {}
        kind = _parse_kind(args.get("kind"))
        status = _parse_status(args.get("status"))
        cursor = _decode_list_cursor(args.get("cursor"))
        limit = _parse_limit(args.get("limit"), _DEFAULT_LIST_LIMIT, _MAX_LIST_LIMIT)
        session_id, _, _ = self._provenance(ctx)
        page = await self.service.list_artifacts(
            source_session_id=session_id,
            kind=kind,
            status=status,
            cursor=cursor,
            limit=limit,
        )
        items = [
            {
                "id": getattr(a, "id", ""),
                "name": getattr(a, "name", ""),
                "kind": _enum_value(getattr(a, "kind", None)),
                "status": _enum_value(getattr(a, "status", None)),
                "summary": getattr(a, "summary", ""),
                "size": getattr(a, "size", 0),
                "current_revision_id": getattr(a, "current_revision_id", None),
                "updated_at": _dt_to_iso(getattr(a, "updated_at", None)),
            }
            for a in getattr(page, "items", ()) or ()
        ]
        return {
            "success": True,
            "items": items,
            "count": len(items),
            "next_cursor": _encode_list_cursor(getattr(page, "next_cursor", None)),
        }

    # ------------------------------------------------------------------
    # artifact_read
    # ------------------------------------------------------------------

    async def _handle_read(
        self, request: ToolCallRequest, ctx: ToolExecutionContext,
    ) -> dict[str, Any]:
        args = request.arguments or {}
        artifact_id = _require_str(args, "artifact_id")
        revision_id = _optional_str(args, "revision_id")
        line_offset = _require_int(args, "line_offset", 0, min_val=0)
        line_limit = _require_int(
            args, "line_limit", _DEFAULT_LINE_LIMIT, min_val=1, max_val=_MAX_LINE_LIMIT,
        )

        await self._check_visibility(artifact_id, ctx)
        content_bytes, revision = await self.service.get_revision_content(
            artifact_id, revision_id,
        )
        rev_kind = getattr(revision, "kind", None)
        base = {
            "success": True,
            "artifact_id": artifact_id,
            "revision_id": getattr(revision, "id", ""),
            "revision_number": getattr(revision, "revision_number", 0),
            "kind": _enum_value(rev_kind),
            "mime": getattr(revision, "mime", ""),
            "size": getattr(revision, "size", 0),
            "checksum": getattr(revision, "checksum", ""),
            "redacted": True,  # cleaned by InformationFlow before model context
        }
        if _is_binary_kind(rev_kind):
            # binary: metadata + controlled download URL only; no bytes/content_ref.
            # When a historical revision_id is requested, point at the revision
            # content endpoint so the URL matches the metadata (spec line 198).
            if revision_id is not None:
                download_url = f"/chat/artifacts/{artifact_id}/revisions/{revision_id}/content"
            else:
                download_url = f"/chat/artifacts/{artifact_id}/content"
            return {
                **base,
                "binary": True,
                "content": None,
                "content_ref": None,
                "download_url": download_url,
            }
        # text: paginate by complete UTF-8 lines (splitlines keepends)
        try:
            text = content_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _ArtifactInvalid("artifact_internal_error") from exc
        lines = text.splitlines(keepends=True)
        total = len(lines)
        start = min(line_offset, total)
        selected: list[str] = []
        acc = 0
        idx = start
        cap = self._read_max_bytes
        while idx < total and len(selected) < line_limit:
            line = lines[idx]
            lb = len(line.encode("utf-8"))
            if not selected and lb > cap:
                # first line alone exceeds the limit -> no half line (413)
                raise ArtifactReadTooLargeError("first line exceeds read limit")
            if acc + lb > cap:
                break
            selected.append(line)
            acc += lb
            idx += 1
        consumed = start + len(selected)
        truncated = consumed < total
        return {
            **base,
            "binary": False,
            "content": "".join(selected),
            "line_offset": start,
            "line_limit": line_limit,
            "truncated": truncated,
            "next_line_offset": consumed if truncated else None,
        }

    # ------------------------------------------------------------------
    # artifact_update
    # ------------------------------------------------------------------

    async def _handle_update(
        self, request: ToolCallRequest, ctx: ToolExecutionContext,
    ) -> dict[str, Any]:
        args = request.arguments or {}
        artifact_id = _require_str(args, "artifact_id")
        expected_revision_id = _require_str(args, "expected_revision_id")
        content = args.get("content")
        workspace_ref = args.get("workspace_ref")
        text_patch_raw = args.get("text_patch")
        change_summary = args.get("change_summary", "")
        kind = _parse_kind(args.get("kind"))
        mime = args.get("mime")

        if content is not None and not isinstance(content, str):
            raise _ArtifactInvalid("artifact_invalid", "content must be a string")
        if workspace_ref is not None:
            if not isinstance(workspace_ref, str) or not workspace_ref.strip() or not workspace_ref.startswith("workspace:"):
                raise _ArtifactInvalid("artifact_invalid", "workspace_ref must use the workspace: scheme")
        if mime is not None and not isinstance(mime, str):
            raise _ArtifactInvalid("artifact_invalid", "mime must be a string")
        if not isinstance(change_summary, str):
            raise _ArtifactInvalid("artifact_invalid", "change_summary must be a string")

        text_patch = _validate_text_patch(text_patch_raw) if text_patch_raw is not None else None
        # exactly one content input
        inputs = [content is not None, workspace_ref is not None, text_patch is not None]
        if sum(inputs) != 1:
            raise _ArtifactInvalid(
                "artifact_invalid", "exactly one of content/workspace_ref/text_patch is required",
            )

        rev, result = await self.service.update_revision(
            artifact_id,
            expected_revision_id=expected_revision_id,
            inline_content=content,
            workspace_ref=workspace_ref,
            text_patch=text_patch,
            change_summary=change_summary,
            kind=kind,
            mime=mime,
        )
        art = await self._safe_get_artifact(artifact_id)
        return {
            "success": True,
            **_revision_meta(artifact_id, rev, art, getattr(result, "publish_sync_state", "")),
            "diff_summary": getattr(result, "diff_summary", ""),
            "content_unchanged": getattr(result, "content_unchanged", False),
        }

    # ------------------------------------------------------------------
    # artifact_list_revisions
    # ------------------------------------------------------------------

    async def _handle_list_revisions(
        self, request: ToolCallRequest, ctx: ToolExecutionContext,
    ) -> dict[str, Any]:
        args = request.arguments or {}
        artifact_id = _require_str(args, "artifact_id")
        cursor = _decode_revision_cursor(args.get("cursor"))
        limit = _parse_limit(args.get("limit"), _DEFAULT_LIST_LIMIT, _MAX_LIST_LIMIT)

        art = await self._check_visibility(artifact_id, ctx)
        current_revision_id = getattr(art, "current_revision_id", None)
        page = await self.service.list_revisions(
            artifact_id, cursor=cursor, limit=limit,
        )
        active = await self.service.get_active_publish(artifact_id)
        published_revision_id = getattr(active, "published_revision_id", None) if active else None
        items = []
        for rev in getattr(page, "items", ()) or ():
            rid = getattr(rev, "id", "")
            items.append({
                "id": rid,
                "revision_number": getattr(rev, "revision_number", 0),
                "checksum": getattr(rev, "checksum", ""),
                "size": getattr(rev, "size", 0),
                "change_summary": getattr(rev, "change_summary", ""),
                "created_by": getattr(rev, "created_by", ""),
                "created_at": _dt_to_iso(getattr(rev, "created_at", None)),
                "is_current": rid == current_revision_id,
                "is_published": bool(published_revision_id) and rid == published_revision_id,
            })
        return {
            "success": True,
            "artifact_id": artifact_id,
            "items": items,
            "count": len(items),
            "next_cursor": _encode_revision_cursor(getattr(page, "next_cursor", None)),
        }

    # ------------------------------------------------------------------
    # artifact_diff
    # ------------------------------------------------------------------

    async def _handle_diff(
        self, request: ToolCallRequest, ctx: ToolExecutionContext,
    ) -> dict[str, Any]:
        args = request.arguments or {}
        artifact_id = _require_str(args, "artifact_id")
        from_revision_id = _require_str(args, "from_revision_id")
        to_revision_id = _require_str(args, "to_revision_id")
        context_lines = _require_int(
            args, "context_lines", _DEFAULT_CONTEXT_LINES, min_val=0, max_val=_MAX_CONTEXT_LINES,
        )
        await self._check_visibility(artifact_id, ctx)
        result = await self.service.diff_revisions(
            artifact_id, from_revision_id, to_revision_id, context_lines=context_lines,
        )
        return {
            "success": True,
            "artifact_id": artifact_id,
            "from_revision_id": from_revision_id,
            "to_revision_id": to_revision_id,
            "context_lines": context_lines,
            "diff_text": getattr(result, "diff_text", ""),
            "binary_changed": getattr(result, "binary_changed", False),
            "redacted": True,  # cleaned by InformationFlow before model context
        }

    # ------------------------------------------------------------------
    # artifact_rollback
    # ------------------------------------------------------------------

    async def _handle_rollback(
        self, request: ToolCallRequest, ctx: ToolExecutionContext,
    ) -> dict[str, Any]:
        args = request.arguments or {}
        artifact_id = _require_str(args, "artifact_id")
        target_revision_id = _require_str(args, "target_revision_id")
        expected_revision_id = _require_str(args, "expected_revision_id")
        change_summary = args.get("change_summary", "")
        if not isinstance(change_summary, str):
            raise _ArtifactInvalid("artifact_invalid", "change_summary must be a string")
        rev, result = await self.service.rollback(
            artifact_id,
            target_revision_id,
            expected_revision_id=expected_revision_id,
            change_summary=change_summary,
        )
        art = await self._safe_get_artifact(artifact_id)
        return {
            "success": True,
            **_revision_meta(artifact_id, rev, art, getattr(result, "publish_sync_state", "")),
            "diff_summary": getattr(result, "diff_summary", ""),
            "content_unchanged": getattr(result, "content_unchanged", False),
        }

    # ------------------------------------------------------------------
    # artifact_publish
    # ------------------------------------------------------------------

    async def _handle_publish(
        self, request: ToolCallRequest, ctx: ToolExecutionContext,
    ) -> dict[str, Any]:
        args = request.arguments or {}
        artifact_id = _require_str(args, "artifact_id")
        revision_id = _require_str(args, "revision_id")
        expected_current_revision_id = _require_str(args, "expected_current_revision_id")
        result = await self.service.publish_revision(
            artifact_id,
            revision_id=revision_id,
            expected_current_revision_id=expected_current_revision_id,
        )
        published = getattr(result, "published", None)
        art = await self._safe_get_artifact(artifact_id)
        try:
            current = await self.service.get_current_revision(artifact_id)
            revision_number = getattr(current, "revision_number", 0)
        except Exception:
            revision_number = 0
        return {
            "success": True,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
            "revision_number": revision_number,
            "name": getattr(art, "name", ""),
            "kind": _enum_value(getattr(art, "kind", None)),
            "publish_sync_state": "current",  # active publish now binds the current revision
            "publish_id": getattr(published, "publish_id", ""),
            "published_revision_id": getattr(published, "published_revision_id", None),
            "share_url": getattr(result, "share_url", ""),
            "reused": getattr(result, "reused", False),
        }


# ---------------------------------------------------------------------------
# Internal exceptions
# ---------------------------------------------------------------------------


class _ArtifactDenied(Exception):
    """Access denied (maps to PERMISSION_DENIED)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _ArtifactInvalid(Exception):
    """Invalid tool call (maps to ERROR with a stable code)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(code)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _map_exception(exc: Exception) -> tuple[str | None, int]:
    """Return (code, http_status) for a known domain/app exception, else (None, 0)."""
    for exc_cls, code, http_status in _ERROR_MAP:
        if isinstance(exc, exc_cls):
            return code, http_status
    return None, 0


def _error_payload(code: str, message: str, retryable: bool) -> dict[str, Any]:
    return {"success": False, "error": {"code": code, "message": message, "retryable": retryable}}


def _revision_meta(
    artifact_id: str, revision: Any, art: Any, publish_sync_state: str,
) -> dict[str, Any]:
    """Build the unified write-tool metadata (spec line 55)."""
    return {
        "artifact_id": artifact_id,
        "revision_id": getattr(revision, "id", ""),
        "revision_number": getattr(revision, "revision_number", 0),
        "name": getattr(art, "name", ""),
        "kind": _enum_value(getattr(art, "kind", None)),
        "publish_sync_state": publish_sync_state,
    }


def _is_binary_kind(kind: Any) -> bool:
    return getattr(kind, "value", str(kind)) in _BINARY_KIND_VALUES


def _enum_value(val: Any) -> str | None:
    if val is None:
        return None
    return getattr(val, "value", str(val))


def _dt_to_iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if hasattr(dt, "isoformat"):
        if getattr(dt, "tzinfo", None) is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return str(dt)


def _parse_kind(value: Any) -> ArtifactKind | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _ArtifactInvalid("artifact_invalid", "kind must be a string")
    try:
        return ArtifactKind(value)
    except ValueError as exc:
        raise _ArtifactInvalid("artifact_invalid", f"invalid kind: {value}") from exc


def _parse_status(value: Any):
    if value is None:
        return None
    if not isinstance(value, str):
        raise _ArtifactInvalid("artifact_invalid", "status must be a string")
    try:
        return ArtifactStatus(value)
    except ValueError as exc:
        raise _ArtifactInvalid("artifact_invalid", f"invalid status: {value}") from exc


def _parse_labels(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(s, str) and s for s in value):
        raise _ArtifactInvalid("artifact_invalid", "labels must be an array of non-empty strings")
    seen: list[str] = []
    for s in value:
        if s not in seen:
            seen.append(s)
    return tuple(seen)


def _parse_limit(value: Any, default: int, max_val: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _ArtifactInvalid("artifact_invalid", "limit must be a positive integer")
    return min(value, max_val)


def _require_str(args: dict[str, Any], key: str) -> str:
    raw = args.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise _ArtifactInvalid("artifact_invalid", f"{key} is required")
    return raw.strip()


def _optional_str(args: dict[str, Any], key: str) -> str | None:
    raw = args.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise _ArtifactInvalid("artifact_invalid", f"{key} must be a non-empty string")
    return raw.strip()


def _require_int(
    args: dict[str, Any], key: str, default: int, *,
    min_val: int | None = None, max_val: int | None = None,
) -> int:
    raw = args.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise _ArtifactInvalid("artifact_invalid", f"{key} must be an integer")
    if min_val is not None and raw < min_val:
        raise _ArtifactInvalid("artifact_invalid", f"{key} must be >= {min_val}")
    if max_val is not None and raw > max_val:
        raise _ArtifactInvalid("artifact_invalid", f"{key} must be <= {max_val}")
    return raw


def _validate_text_patch(text_patch: Any) -> list[dict[str, object]]:
    """Validate a text_patch array (spec lines 60-64).

    Each op must be exactly ``{search: non-empty str, replace: str, mode:
    "first"|"all"}``; 1..100 ops; unknown fields / bad mode -> artifact_revision_invalid.
    Application (search match / atomicity) is validated by the service.
    """
    if not isinstance(text_patch, list) or not text_patch:
        raise _ArtifactInvalid("artifact_revision_invalid", "text_patch must be a non-empty array")
    if len(text_patch) > _MAX_TEXT_PATCH_OPS:
        raise _ArtifactInvalid("artifact_revision_invalid", "text_patch exceeds 100 operations")
    cleaned: list[dict[str, object]] = []
    for op in text_patch:
        if not isinstance(op, dict) or set(op.keys()) != {"search", "replace", "mode"}:
            raise _ArtifactInvalid(
                "artifact_revision_invalid", "text_patch op must have exactly search/replace/mode",
            )
        search = op["search"]
        replace = op["replace"]
        mode = op["mode"]
        if not isinstance(search, str) or not search:
            raise _ArtifactInvalid("artifact_revision_invalid", "search must be a non-empty string")
        if not isinstance(replace, str):
            raise _ArtifactInvalid("artifact_revision_invalid", "replace must be a string")
        if mode not in ("first", "all"):
            raise _ArtifactInvalid("artifact_revision_invalid", "mode must be 'first' or 'all'")
        cleaned.append({"search": search, "replace": replace, "mode": mode})
    return cleaned


# ---------------------------------------------------------------------------
# Cursor codec (JSON; mirrors HTTP layer format)
# ---------------------------------------------------------------------------


def _encode_list_cursor(cursor: Any) -> str | None:
    if cursor is None:
        return None
    return json.dumps({
        "updated_at": _dt_to_iso(getattr(cursor, "updated_at", None)),
        "artifact_id": getattr(cursor, "artifact_id", ""),
    })


def _decode_list_cursor(value: Any) -> ArtifactListCursor | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _ArtifactInvalid("artifact_invalid", "cursor must be a string")
    try:
        data = json.loads(value)
        updated_at = None
        if data.get("updated_at"):
            updated_at = datetime.fromisoformat(data["updated_at"])
        return ArtifactListCursor(updated_at=updated_at, artifact_id=data["artifact_id"])
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        raise _ArtifactInvalid("artifact_invalid", "invalid cursor") from exc


def _encode_revision_cursor(cursor: Any) -> str | None:
    if cursor is None:
        return None
    return json.dumps({
        "artifact_id": getattr(cursor, "artifact_id", ""),
        "revision_number": getattr(cursor, "revision_number", 0),
        "id": getattr(cursor, "id", ""),
    })


def _decode_revision_cursor(value: Any) -> RevisionListCursor | None:
    """Decode a revision-list cursor token.

    Format errors map to ``artifact_revision_invalid`` (spec line 124:
    Artifact 不匹配、格式错误时返回 artifact_revision_invalid); cross-artifact
    cursors are validated by the service (also artifact_revision_invalid).
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise _ArtifactInvalid("artifact_revision_invalid", "cursor must be a string")
    try:
        data = json.loads(value)
        return RevisionListCursor(
            artifact_id=data["artifact_id"],
            revision_number=int(data["revision_number"]),
            id=data["id"],
        )
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        raise _ArtifactInvalid("artifact_revision_invalid", "invalid cursor") from exc

"""T12: Dashboard management API for the Artifact workbench.

Registers /chat/artifacts* routes on a FastAPI APIRouter. Routes only do
request parsing, response construction, and exception mapping; all business
rules delegate to ArtifactService. The actor is server-fixed "dashboard"
and never read from body, query, or headers.

Error envelope (unified): {"error": {"code": <str>, "message": <str>}}
Error code -> HTTP status mapping:
    404 artifact_not_found / publish_not_found
    409 artifact_content_unavailable / artifact_conflict
    413 artifact_too_large
    415 unsupported_media_type
    422 artifact_invalid / publish_blocked
    500 artifact_internal_error (unexpected, no traceback/abs-path)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from app.application.artifact_service import (
    ArtifactTooLargeError,
    PublishBlockedError,
    PublishResult,
)
from app.domain.artifact import (
    Artifact,
    ArtifactConflictError,
    ArtifactContentUnavailableError,
    ArtifactKind,
    ArtifactListCursor,
    ArtifactNotFoundError,
    ArtifactSource,
    ArtifactStatus,
    ArtifactValidationError,
    PublishedArtifact,
    PublishedArtifactNotFoundError,
)

logger = logging.getLogger(__name__)

# Upload size limit (matches ArtifactServiceConfig.artifact_max_bytes default).
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024
# Chunk size for streaming reads.
_CHUNK_SIZE = 64 * 1024

# PATCH field whitelist: only these fields may be set by the client.
# id/source/source_kind/source_ref/created_by/checksum/size/status are
# NOT directly settable.
_PATCH_ALLOWED_FIELDS = frozenset({"name", "summary", "classification", "labels", "content"})


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_artifact_routes(router: APIRouter, artifact_service) -> None:
    """Register /chat/artifacts* routes on router.

    Only request parsing, upload size limits, and application error mapping.
    All business rules delegate to artifact_service (ArtifactService).
    """

    # ---- list ----
    @router.get("/chat/artifacts")
    async def list_artifacts(
        source_kind: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        q: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ):
        parsed_source_kind = _parse_enum(source_kind, ArtifactSource, "source_kind")
        if isinstance(parsed_source_kind, JSONResponse):
            return parsed_source_kind
        parsed_kind = _parse_enum(kind, ArtifactKind, "kind")
        if isinstance(parsed_kind, JSONResponse):
            return parsed_kind
        parsed_status = _parse_enum(status, ArtifactStatus, "status")
        if isinstance(parsed_status, JSONResponse):
            return parsed_status

        parsed_cursor = _parse_cursor(cursor)
        if isinstance(parsed_cursor, JSONResponse):
            return parsed_cursor

        try:
            page = await artifact_service.list_artifacts(
                source_kind=parsed_source_kind,
                kind=parsed_kind,
                status=parsed_status,
                q=q,
                cursor=parsed_cursor,
                limit=limit,
            )
        except Exception as exc:
            return _exception_to_response(exc)

        return {
            "items": [_artifact_to_dict(a) for a in page.items],
            "next_cursor": _cursor_to_dict(page.next_cursor),
        }

    # ---- create ----
    @router.post("/chat/artifacts")
    async def create_artifact(request: Request):
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type == "application/json":
            return await _create_from_json(request, artifact_service)
        if content_type == "multipart/form-data":
            return await _create_from_multipart(request, artifact_service)
        return _error_response("unsupported_media_type", "unsupported content type", 415)

    # ---- get detail ----
    @router.get("/chat/artifacts/{artifact_id}")
    async def get_artifact(artifact_id: str):
        try:
            art = await artifact_service.get_artifact(artifact_id)
        except Exception as exc:
            return _exception_to_response(exc)
        return _artifact_to_dict(art)

    # ---- get content ----
    @router.get("/chat/artifacts/{artifact_id}/content")
    async def get_content(artifact_id: str):
        try:
            data, art = await artifact_service.get_content(artifact_id)
        except Exception as exc:
            return _exception_to_response(exc)
        return _build_content_response(data, art.mime, art.name, art.kind)

    # ---- patch ----
    @router.patch("/chat/artifacts/{artifact_id}")
    async def update_artifact(artifact_id: str, request: Request):
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type == "application/json":
            return await _patch_from_json(artifact_id, request, artifact_service)
        if content_type == "multipart/form-data":
            return await _patch_from_multipart(artifact_id, request, artifact_service)
        return _error_response("unsupported_media_type", "unsupported content type", 415)

    # ---- delete ----
    @router.delete("/chat/artifacts/{artifact_id}")
    async def delete_artifact(artifact_id: str):
        try:
            await artifact_service.delete_artifact(artifact_id)
        except Exception as exc:
            return _exception_to_response(exc)
        return Response(status_code=204)

    # ---- export ----
    @router.get("/chat/artifacts/{artifact_id}/export")
    async def export_artifact(artifact_id: str, format: str = "original"):
        try:
            data, mime, filename = await artifact_service.export(artifact_id, format=format)
        except Exception as exc:
            return _exception_to_response(exc)
        return _build_content_response(data, mime, filename, None, force_attachment=True)

    # ---- publish ----
    @router.post("/chat/artifacts/{artifact_id}/publish")
    async def publish_artifact(artifact_id: str):
        try:
            result = await artifact_service.publish(artifact_id)
        except Exception as exc:
            return _exception_to_response(exc)
        return _publish_result_to_dict(result)

    # ---- get active publish ----
    @router.get("/chat/artifacts/{artifact_id}/publish")
    async def get_publish(artifact_id: str):
        try:
            published = await artifact_service.get_active_publish(artifact_id)
        except Exception as exc:
            return _exception_to_response(exc)
        if published is None:
            return {"status": "unpublished"}
        return _published_to_dict(published)

    # ---- revoke publish ----
    @router.delete("/chat/artifacts/{artifact_id}/publish")
    async def revoke_publish(artifact_id: str):
        try:
            revoked = await artifact_service.revoke_publish(artifact_id)
        except PublishedArtifactNotFoundError:
            return {"status": "unpublished"}
        except Exception as exc:
            return _exception_to_response(exc)
        return {
            "status": "revoked",
            "publish_id": revoked.publish_id,
            "revoked_at": _dt_to_iso(revoked.revoked_at),
        }


# ---------------------------------------------------------------------------
# Create helpers (JSON + multipart)
# ---------------------------------------------------------------------------


async def _create_from_json(request: Request, service) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        return _error_response("artifact_invalid", "invalid json body", 422)
    if not isinstance(payload, dict):
        return _error_response("artifact_invalid", "body must be a JSON object", 422)

    name = payload.get("name")
    kind_str = payload.get("kind")
    content = payload.get("content")
    if not name or not isinstance(name, str):
        return _error_response("artifact_invalid", "name is required", 422)
    if not kind_str or not isinstance(kind_str, str):
        return _error_response("artifact_invalid", "kind is required", 422)
    if content is None or not isinstance(content, str):
        return _error_response("artifact_invalid", "content (inline text) is required", 422)

    kind_enum = _parse_enum(kind_str, ArtifactKind, "kind")
    if isinstance(kind_enum, JSONResponse):
        return kind_enum

    mime = payload.get("mime") or _default_mime(kind_enum)
    source_kind_str = payload.get("source_kind")
    source_kind_enum = ArtifactSource.MANUAL
    if source_kind_str:
        source_kind_enum = _parse_enum(source_kind_str, ArtifactSource, "source_kind")
        if isinstance(source_kind_enum, JSONResponse):
            return source_kind_enum

    labels = payload.get("labels")
    if labels is not None and not isinstance(labels, list):
        return _error_response("artifact_invalid", "labels must be a list", 422)

    try:
        art = await service.create_artifact(
            name=name,
            kind=kind_enum,
            mime=mime,
            inline_content=content,
            source_kind=source_kind_enum,
            source_ref=payload.get("source_ref"),
            source_context_ref=payload.get("source_context_ref"),
            summary=payload.get("summary", ""),
            classification=payload.get("classification"),
            labels=tuple(labels) if labels else None,
            # actor is server-fixed; created_by is NOT read from body
        )
    except Exception as exc:
        return _exception_to_response(exc)
    return JSONResponse(status_code=201, content=_artifact_to_dict(art))


async def _create_from_multipart(request: Request, service) -> JSONResponse:
    try:
        form = await request.form()
    except Exception:
        return _error_response("artifact_invalid", "invalid multipart form", 422)

    upload_files: list[UploadFile] = []
    form_fields: dict[str, str] = {}
    for key, value in form.multi_items():
        if isinstance(value, UploadFile):
            upload_files.append(value)
        else:
            form_fields[key] = str(value)

    if len(upload_files) != 1:
        return _error_response("artifact_invalid", "exactly one file is required", 422)

    file = upload_files[0]
    name = form_fields.get("name")
    kind_str = form_fields.get("kind")
    if not name:
        return _error_response("artifact_invalid", "name is required", 422)
    if not kind_str:
        return _error_response("artifact_invalid", "kind is required", 422)

    kind_enum = _parse_enum(kind_str, ArtifactKind, "kind")
    if isinstance(kind_enum, JSONResponse):
        return kind_enum

    source_kind_str = form_fields.get("source_kind")
    source_kind_enum = ArtifactSource.MANUAL
    if source_kind_str:
        source_kind_enum = _parse_enum(source_kind_str, ArtifactSource, "source_kind")
        if isinstance(source_kind_enum, JSONResponse):
            return source_kind_enum

    labels_str = form_fields.get("labels")
    labels = None
    if labels_str:
        try:
            parsed = json.loads(labels_str)
            if isinstance(parsed, list):
                labels = tuple(str(x) for x in parsed)
        except (json.JSONDecodeError, TypeError):
            labels = tuple(s.strip() for s in labels_str.split(",") if s.strip())

    # Stream file in chunks with size limit (NO unbounded await file.read()).
    file_data = await _read_upload_bounded(file)
    if isinstance(file_data, JSONResponse):
        return file_data

    mime = form_fields.get("mime") or file.content_type or _default_mime(kind_enum)

    try:
        art = await service.create_artifact(
            name=name,
            kind=kind_enum,
            mime=mime,
            file_data=file_data,
            filename=file.filename or name,
            source_kind=source_kind_enum,
            source_ref=form_fields.get("source_ref"),
            source_context_ref=form_fields.get("source_context_ref"),
            summary=form_fields.get("summary", ""),
            classification=form_fields.get("classification") or None,
            labels=labels,
            # actor is server-fixed; created_by is NOT read from body
        )
    except Exception as exc:
        return _exception_to_response(exc)
    return JSONResponse(status_code=201, content=_artifact_to_dict(art))


# ---------------------------------------------------------------------------
# Patch helpers (JSON + multipart)
# ---------------------------------------------------------------------------


async def _patch_from_json(artifact_id: str, request: Request, service) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        return _error_response("artifact_invalid", "invalid json body", 422)
    if not isinstance(payload, dict):
        return _error_response("artifact_invalid", "body must be a JSON object", 422)

    forbidden = set(payload.keys()) - _PATCH_ALLOWED_FIELDS
    if forbidden:
        return _error_response(
            "artifact_invalid",
            f"fields not settable: {', '.join(sorted(forbidden))}",
            422,
        )

    kwargs = _extract_patch_kwargs(payload)
    try:
        art = await service.update_artifact(artifact_id, **kwargs)
    except Exception as exc:
        return _exception_to_response(exc)
    return JSONResponse(content=_artifact_to_dict(art))


async def _patch_from_multipart(artifact_id: str, request: Request, service) -> JSONResponse:
    try:
        form = await request.form()
    except Exception:
        return _error_response("artifact_invalid", "invalid multipart form", 422)

    upload_files: list[UploadFile] = []
    form_fields: dict[str, str] = {}
    for key, value in form.multi_items():
        if isinstance(value, UploadFile):
            upload_files.append(value)
        else:
            form_fields[key] = str(value)

    forbidden = set(form_fields.keys()) - _PATCH_ALLOWED_FIELDS
    if forbidden:
        return _error_response(
            "artifact_invalid",
            f"fields not settable: {', '.join(sorted(forbidden))}",
            422,
        )

    file_data = None
    filename = None
    if len(upload_files) == 1:
        file = upload_files[0]
        file_data = await _read_upload_bounded(file)
        if isinstance(file_data, JSONResponse):
            return file_data
        filename = file.filename
    elif len(upload_files) > 1:
        return _error_response("artifact_invalid", "at most one file is allowed", 422)

    kwargs = _extract_patch_kwargs(form_fields)
    if file_data is not None:
        kwargs["file_data"] = file_data
        kwargs["filename"] = filename

    try:
        art = await service.update_artifact(artifact_id, **kwargs)
    except Exception as exc:
        return _exception_to_response(exc)
    return JSONResponse(content=_artifact_to_dict(art))


def _extract_patch_kwargs(payload: dict) -> dict:
    """Extract allowed PATCH fields from a dict, mapping 'content' to inline_content."""
    kwargs: dict = {}
    if "name" in payload:
        kwargs["name"] = payload["name"]
    if "summary" in payload:
        kwargs["summary"] = payload["summary"]
    if "classification" in payload:
        kwargs["classification"] = payload["classification"]
    if "labels" in payload:
        labels = payload["labels"]
        if isinstance(labels, list):
            kwargs["labels"] = tuple(str(x) for x in labels)
        elif isinstance(labels, str):
            try:
                parsed = json.loads(labels)
                if isinstance(parsed, list):
                    kwargs["labels"] = tuple(str(x) for x in parsed)
                else:
                    kwargs["labels"] = tuple(s.strip() for s in labels.split(",") if s.strip())
            except (json.JSONDecodeError, TypeError):
                kwargs["labels"] = tuple(s.strip() for s in labels.split(",") if s.strip())
    if "content" in payload:
        kwargs["inline_content"] = payload["content"]
    return kwargs


# ---------------------------------------------------------------------------
# Streaming upload reader
# ---------------------------------------------------------------------------


async def _read_upload_bounded(file: UploadFile) -> bytes | JSONResponse:
    """Read upload file in chunks with size limit.

    Returns bytes on success, or JSONResponse (413) if size exceeded.
    NEVER calls unbounded await file.read().
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_UPLOAD_BYTES:
            return _error_response(
                "artifact_too_large",
                f"upload {total} exceeds max {_MAX_UPLOAD_BYTES} bytes",
                413,
            )
        chunks.append(chunk)
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# Content/export response builder
# ---------------------------------------------------------------------------


def _build_content_response(
    data: bytes,
    mime: str,
    filename: str,
    kind: ArtifactKind | None = None,
    *,
    force_attachment: bool = False,
) -> Response:
    """Build a content/export response with security headers."""
    content_type = _content_type_with_charset(mime)
    # Raw HTML must use attachment (not same-origin top-level HTML).
    is_html = (mime or "").lower().split(";")[0].strip() == "text/html"
    disposition = "attachment" if (force_attachment or is_html) else "inline"
    cd = _safe_content_disposition(filename, disposition)
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Content-Disposition": cd,
            "X-Content-Type-Options": "nosniff",
        },
    )


def _content_type_with_charset(mime: str) -> str:
    """Add charset=utf-8 for text mime types."""
    base = (mime or "application/octet-stream").split(";")[0].strip()
    if _is_text_mime(base):
        return f"{base}; charset=utf-8"
    return base


def _is_text_mime(mime: str) -> bool:
    mime = (mime or "").lower().split(";")[0].strip()
    return (
        mime.startswith("text/")
        or mime in ("application/json", "application/xml", "application/javascript")
    )


def _safe_content_disposition(filename: str, disposition: str = "attachment") -> str:
    """Build a safe Content-Disposition header with RFC 5987 UTF-8 filename."""
    raw = filename or "artifact"
    # Sanitize: remove path separators, quotes, control chars.
    safe = raw.replace("/", "_").replace("\\", "_").replace("\x00", "")
    safe = safe.replace('"', "").replace("\r", "").replace("\n", "")
    if not safe:
        safe = "artifact"
    quoted = quote(raw, safe="")
    return f'{disposition}; filename="{safe}"; filename*=UTF-8\'\'{quoted}'


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _artifact_to_dict(artifact: Artifact) -> dict:
    """Serialize an Artifact to a JSON-safe dict (no content_ref/inline_content)."""
    view = artifact.to_public_view()
    return {
        "id": view["id"],
        "name": view["name"],
        "kind": _enum_value(view["kind"]),
        "mime": view["mime"],
        "size": view["size"],
        "checksum": view["checksum"],
        "source_kind": _enum_value(view["source_kind"]),
        "source_context_ref": view["source_context_ref"],
        "summary": view["summary"],
        "classification": view["classification"],
        "labels": list(view["labels"]) if view["labels"] is not None else None,
        "status": _enum_value(view["status"]),
        "created_at": _dt_to_iso(view["created_at"]),
        "updated_at": _dt_to_iso(view["updated_at"]),
        "created_by": view["created_by"],
    }


def _published_to_dict(published: PublishedArtifact) -> dict:
    return {
        "status": _enum_value(published.status),
        "publish_id": published.publish_id,
        "artifact_id": published.artifact_id,
        "share_path": f"/p/{published.publish_id}",
        "snapshot_name": published.snapshot_name,
        "snapshot_kind": _enum_value(published.snapshot_kind),
        "snapshot_mime": published.snapshot_mime,
        "snapshot_size": published.snapshot_size,
        "snapshot_checksum": published.snapshot_checksum,
        "snapshot_summary": published.snapshot_summary,
        "published_at": _dt_to_iso(published.published_at),
        "published_by": published.published_by,
        "revoked_at": _dt_to_iso(published.revoked_at),
    }


def _publish_result_to_dict(result: PublishResult) -> dict:
    return {
        "publish_id": result.published.publish_id,
        "share_path": f"/p/{result.published.publish_id}",
        "share_url": result.share_url,
        "reused": result.reused,
    }


def _cursor_to_dict(cursor: ArtifactListCursor | None) -> dict | None:
    if cursor is None:
        return None
    return {
        "updated_at": _dt_to_iso(cursor.updated_at),
        "artifact_id": cursor.artifact_id,
    }


def _dt_to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _enum_value(val) -> str | None:
    if val is None:
        return None
    return val.value if hasattr(val, "value") else str(val)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_enum(value: str | None, enum_cls, field_name: str):
    """Parse a string into an enum value. Returns JSONResponse on error."""
    if value is None:
        return None
    try:
        return enum_cls(value)
    except ValueError:
        return _error_response(
            "artifact_invalid",
            f"invalid {field_name}: {value}",
            422,
        )


def _parse_cursor(cursor_str: str | None):
    """Parse a JSON-encoded cursor string. Returns JSONResponse on error."""
    if cursor_str is None:
        return None
    try:
        data = json.loads(cursor_str)
        updated_at = None
        if data.get("updated_at"):
            updated_at = datetime.fromisoformat(data["updated_at"])
        return ArtifactListCursor(
            updated_at=updated_at,
            artifact_id=data["artifact_id"],
        )
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return _error_response("artifact_invalid", "invalid cursor", 422)


def _default_mime(kind: ArtifactKind) -> str:
    """Default MIME for an ArtifactKind."""
    defaults = {
        ArtifactKind.MARKDOWN: "text/markdown",
        ArtifactKind.HTML: "text/html",
        ArtifactKind.CODE: "text/plain",
        ArtifactKind.TEXT: "text/plain",
        ArtifactKind.JSON: "application/json",
        ArtifactKind.CSV: "text/csv",
        ArtifactKind.DATA: "application/octet-stream",
        ArtifactKind.DOCUMENT: "text/plain",
        ArtifactKind.IMAGE: "image/png",
        ArtifactKind.PDF: "application/pdf",
        ArtifactKind.OTHER: "application/octet-stream",
    }
    return defaults.get(kind, "application/octet-stream")


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _error_response(code: str, message: str, status_code: int) -> JSONResponse:
    """Build the unified error envelope with scrubbed message."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": _scrub_message(message)}},
    )


def _exception_to_response(exc: Exception) -> JSONResponse:
    """Map application/domain exceptions to the unified error envelope."""
    if isinstance(exc, ArtifactNotFoundError):
        return _error_response("artifact_not_found", str(exc), 404)
    if isinstance(exc, ArtifactValidationError):
        return _error_response("artifact_invalid", str(exc), 422)
    if isinstance(exc, ArtifactTooLargeError):
        return _error_response("artifact_too_large", str(exc), 413)
    if isinstance(exc, ArtifactContentUnavailableError):
        return _error_response("artifact_content_unavailable", str(exc), 409)
    if isinstance(exc, ArtifactConflictError):
        return _error_response("artifact_conflict", str(exc), 409)
    if isinstance(exc, PublishBlockedError):
        return _error_response("publish_blocked", str(exc), 422)
    if isinstance(exc, PublishedArtifactNotFoundError):
        return _error_response("publish_not_found", str(exc), 404)
    # Unexpected exception -> stable 500 (no traceback/abs-path in response).
    logger.exception("unexpected error in artifact routes")
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "artifact_internal_error", "message": "internal error"}},
    )


def _scrub_message(message: str) -> str:
    """Best-effort scrub of internal details from error messages."""
    if not isinstance(message, str):
        return "internal error"
    for token in ("sqlite3.OperationalError:", "sqlite3.IntegrityError:"):
        if token in message:
            return "registry error"
    return message

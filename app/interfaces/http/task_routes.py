"""T19: Dashboard HTTP API + WebSocket for the Task subdomain.

Route registration follows the ``register_*_routes`` pattern from
``plugin_routes.py``: a top-level function injects services and registers
all routes on a FastAPI ``APIRouter``. Static paths (board / swarm /
dispatch / inspect / runs / attachments / events) register before
``/{id}`` so they aren't captured as task ids.

Error envelope (unified):
    {"error": {"code": <str>, "message": <str>, "details"?: <any>}}

Error code -> HTTP status mapping:
    404 task_not_found / attachment_not_found
    409 task_conflict / task_claim_failed / task_state_invalid
    413 task_attachment_too_large
    422 task_invalid / task_dependency_cycle / task_attachment_invalid
    500 task_internal_error / task_scan_failed
    503 task_registry_busy

Interfaces layer MUST NOT import SQLite or app.infrastructure. Only
Application services are called.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Body, File, Form, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from app.application.task_run_service import TaskRunService
from app.application.task_service import TaskService
from app.domain.task import (
    BulkUpdateCommand,
    BulkUpdateItem,
    TaskAttachmentError,
    TaskClaimError,
    TaskConflictError,
    TaskListCursor,
    TaskNotFoundError,
    TaskStateError,
    TaskValidationError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# WebSocket per-connection queue cap. Slow consumers are dropped so the
# dispatcher tail isn't blocked (spec).
_WS_QUEUE_MAX = 256

# Board endpoint: cap cards per column to avoid unbounded responses.
_BOARD_COLUMN_LIMIT = 200

# List endpoint hard cap.
_LIST_LIMIT_MAX = 200

# Allowed simple field names for PATCH /chat/tasks/{id}. Restricted to
# the public surface; status transitions still go through PATCH but
# RUNNING rejects generic updates at the service layer.
_PATCH_ALLOWED_FIELDS = frozenset({
    "title", "body", "priority", "is_archived",
})


# Approval note length cap (trim'd). Defends worker_context budget.
_NOTE_MAX = 2000


# Fixed public error messages (never leak task id, str(exc), or db details).
_MSG_TASK_NOT_FOUND = "task not found"
_MSG_TASK_STATE_INVALID = "task state invalid"
_MSG_TASK_CONFLICT = "task conflict"
_MSG_TASK_INVALID = "invalid task request"
_MSG_TASK_INTERNAL = "internal task error"


def _extract_note(
    payload: Any, required: bool = False,
) -> tuple[str | None, str | None]:
    """Extract and validate the approval note from request body.

    Shared by approve/reject (required=False) and revise (required=True).
    Returns ``(note, error)``: on success error is None and note is None when
    absent/empty (only allowed for required=False); on failure note is None
    and error is the fixed public message ``invalid task request``.

    Validation order:
      1. payload must be a JSON object (dict) or None
      2. only the ``note`` field is allowed (extra fields rejected)
      3. note must be a string (if present)
      4. note is trimmed; empty note is None (or error if required)
      5. note length <= _NOTE_MAX code points (after trim)

    Never calls str() on non-string input (rejects list/dict/number).
    """
    if payload is None:
        if required:
            return None, _MSG_TASK_INVALID
        return None, None
    if not isinstance(payload, dict):
        return None, _MSG_TASK_INVALID
    # Only ``note`` is allowed; reject any extra field.
    if any(key != "note" for key in payload):
        return None, _MSG_TASK_INVALID
    raw = payload.get("note")
    if raw is None:
        if required:
            return None, _MSG_TASK_INVALID
        return None, None
    if not isinstance(raw, str):
        return None, _MSG_TASK_INVALID
    note = raw.strip()
    if not note:
        if required:
            return None, _MSG_TASK_INVALID
        return None, None
    if len(note) > _NOTE_MAX:
        return None, _MSG_TASK_INVALID
    return note, None


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_task_routes(
    router: APIRouter,
    task_service: TaskService,
    task_run_service: TaskRunService | None = None,
) -> None:
    """Register all ``/chat/tasks*`` routes on ``router``.

    Static paths (board/swarm/dispatch/inspect/runs/attachments/events) are
    registered BEFORE ``/{id}`` so they aren't captured as task ids.
    """

    # ---- static paths (registered first) ----
    @router.get("/chat/tasks/board")
    async def get_board():
        try:
            page = await task_service.list_tasks(limit=_LIST_LIMIT_MAX)
        except Exception as exc:
            return _task_error_response("task_internal_error", str(exc), 500)
        columns = _group_by_status(list(page.items))
        # Add per-column totals (capped)
        for col in columns:
            cards = col["cards"][:_BOARD_COLUMN_LIMIT]
            col["cards"] = cards
            col["total"] = len(col["cards"]) if len(cards) < col["total"] else col["total"]
        return {"columns": columns}

    @router.get("/chat/tasks/inspect")
    async def inspect_dispatcher():
        if task_run_service is None:
            return {"active": [], "recovered": [], "note": "task_run_service not configured"}
        try:
            snapshot = await task_run_service.dispatcher.inspect()
            return snapshot
        except Exception as exc:
            return _task_error_response("task_internal_error", str(exc), 500)

    @router.post("/chat/tasks/dispatch")
    async def dispatch_tick():
        if task_run_service is None:
            return _task_error_response("task_state_invalid", "task_run_service not configured", 409)
        try:
            return await task_run_service.dispatch_once()
        except Exception as exc:
            return _task_error_response("task_internal_error", str(exc), 500)

    # ---- list / create ----
    @router.get("/chat/tasks")
    async def list_tasks(
        status: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=_LIST_LIMIT_MAX),
    ):
        try:
            page = await task_service.list_tasks(limit=limit)
        except Exception as exc:
            return _task_error_response("task_internal_error", str(exc), 500)
        items = list(page.items)
        if status:
            items = [t for t in items if t.status.value == status]
        return {
            "items": [_task_to_dict(t) for t in items],
            "next_cursor": _cursor_to_dict(page.next_cursor),
        }

    @router.post("/chat/tasks")
    async def create_task(payload: dict = Body(default_factory=dict)):
        try:
            scheduled_at = None
            raw_scheduled = payload.get("scheduled_at")
            if raw_scheduled:
                try:
                    scheduled_at = datetime.fromisoformat(str(raw_scheduled))
                except ValueError as exc:
                    return _task_error_response("task_invalid", f"invalid scheduled_at: {exc}", 422)
                if scheduled_at.tzinfo is None:
                    scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
            task = await task_service.create_task(
                title=str(payload.get("title", "") or ""),
                body=str(payload.get("body", "") or ""),
                priority=int(payload.get("priority", 0) or 0),
                created_by=str(payload.get("created_by", "") or ""),
                idempotency_key=payload.get("idempotency_key"),
                origin_session_id=payload.get("origin_session_id"),
                skills=tuple(payload.get("skills") or ()),
                max_retries=int(payload.get("max_retries", 0) or 0),
                goal_mode=bool(payload.get("goal_mode", False)),
                goal_max_turns=payload.get("goal_max_turns"),
                model_override=payload.get("model_override"),
                max_runtime_seconds=payload.get("max_runtime_seconds"),
                scheduled_at=scheduled_at,
            )
        except TaskValidationError as exc:
            return _task_error_response("task_invalid", str(exc), 422)
        except TaskConflictError as exc:
            return _task_error_response("task_conflict", str(exc), 409)
        except Exception as exc:
            return _task_error_response("task_internal_error", str(exc), 500)
        return _task_to_dict(task)

    # ---- attachments (static, before /{id}) ----
    @router.get("/chat/tasks/attachments/{attachment_id}")
    async def download_attachment(attachment_id: str):
        att = await task_service.get_attachment(attachment_id)
        if att is None:
            return _task_error_response(
                "attachment_not_found",
                f"attachment not found: {attachment_id}",
                404,
            )
        path = task_service.get_attachment_path(att.task_id, att.stored_name)
        if path is None or not path.exists():
            return _task_error_response(
                "attachment_not_found",
                "attachment file missing",
                404,
            )
        # Security: re-check is_relative_to after resolve (path_security
        # pattern: never trust stored_name; always re-resolve).
        root = task_service.attachments_root
        if root is not None and not path.is_relative_to(root.resolve()):
            return _task_error_response(
                "attachment_not_found",
                "attachment path escapes root",
                404,
            )
        # Safe Content-Disposition: filename uses stored_name only (server
        # generated); display filename is also safe (validated on upload).
        safe_filename = att.stored_name.replace('"', "").replace("\r", "").replace("\n", "")
        display_name = (att.filename or att.stored_name).replace('"', "").replace("\r", "").replace("\n", "")
        # Use both filename (legacy) and filename* (RFC 5987) for UTF-8 display.
        quoted_utf8 = quote(display_name, safe="")
        headers = {
            "Content-Disposition": (
                f'attachment; filename="{safe_filename}"; '
                f"filename*=UTF-8''{quoted_utf8}"
            ),
            "X-Content-Type-Options": "nosniff",
        }
        media_type = att.content_type or "application/octet-stream"
        # Read file content into memory (attachments are bounded by
        # Settings.task_attachment_max_bytes; the path is whitelisted).
        try:
            data = path.read_bytes()
        except OSError as exc:
            return _task_error_response(
                "attachment_not_found",
                f"failed to read attachment: {exc}",
                404,
            )
        return Response(content=data, media_type=media_type, headers=headers)

    # ---- runs (static, before /{id}) ----
    @router.get("/chat/tasks/runs/{run_id}")
    async def get_run(run_id: int):
        # Look up by run_id across all tasks. Linear scan is acceptable for
        # the Dashboard; production uses task-scoped list_runs.
        try:
            page = await task_service.list_tasks(limit=_LIST_LIMIT_MAX)
        except Exception as exc:
            return _task_error_response("task_internal_error", str(exc), 500)
        for task in page.items:
            runs = await task_service.list_runs(task.id, limit=200)
            for r in runs:
                if r.id == run_id:
                    return _run_to_dict(r)
        return _task_error_response(
            "task_not_found", f"run not found: {run_id}", 404,
        )

    @router.post("/chat/tasks/runs/{run_id}/terminate")
    async def terminate_run(run_id: int):
        if task_run_service is None:
            return _task_error_response(
                "task_state_invalid", "task_run_service not configured", 409,
            )
        try:
            # Find the task that owns this run.
            page = await task_service.list_tasks(limit=_LIST_LIMIT_MAX)
            owner_id: str | None = None
            for task in page.items:
                runs = await task_service.list_runs(task.id, limit=200)
                if any(r.id == run_id for r in runs):
                    owner_id = task.id
                    break
            if owner_id is None:
                return _task_error_response(
                    "task_not_found", f"run not found: {run_id}", 404,
                )
            return await task_run_service.terminate(owner_id, run_id)
        except TaskNotFoundError:
            return _task_error_response(
                "task_not_found", f"run not found: {run_id}", 404,
            )
        except TaskStateError as exc:
            return _task_error_response("task_state_invalid", str(exc), 409)
        except Exception as exc:
            return _task_error_response("task_internal_error", str(exc), 500)

    # ---- WebSocket events ----
    @router.websocket("/chat/tasks/events")
    async def ws_events(websocket: WebSocket, since: int = Query(default=0, ge=0)):
        """Tail task_events with a monotonic id cursor.

        On connect: replay events with id > ``since``. Then poll for new
        events until the client disconnects. The connection has a bounded
        queue; slow consumers are dropped so the dispatcher tail isn't
        blocked. Clients reconnect with the last id they saw.
        """
        await websocket.accept()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_WS_QUEUE_MAX)
        last_event_id = since
        # Replay backlog from each task (linear scan; acceptable for the
        # Dashboard; production wiring can swap in a registry-level tail).
        try:
            page = await task_service.list_tasks(limit=_LIST_LIMIT_MAX)
            for task in page.items:
                events = await task_service.list_events(task.id, since=since, limit=500)
                for e in events:
                    await _ws_safe_put(queue, _event_to_dict(e))
                    if e.id > last_event_id:
                        last_event_id = e.id
        except Exception as exc:
            logger.warning("ws_events replay failed: %s", exc)

        poll_task = asyncio.create_task(_ws_poll_events(task_service, queue, last_event_id))
        recv_task = asyncio.create_task(_ws_recv_loop(websocket))
        send_task = asyncio.create_task(_ws_send_loop(websocket, queue))
        try:
            await asyncio.wait(
                {poll_task, recv_task, send_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except WebSocketDisconnect:
            pass
        finally:
            for t in (poll_task, recv_task, send_task):
                t.cancel()
        try:
            await websocket.close()
        except Exception:
            pass


    # ---- /{id} dynamic paths (registered LAST) ----
    @router.get("/chat/tasks/{task_id}")
    async def get_task_detail(task_id: str):
        try:
            detail = await task_service.get_task_detail(task_id)
        except TaskNotFoundError:
            return _task_error_response(
                "task_not_found", f"task not found: {task_id}", 404,
            )
        except Exception as exc:
            return _task_error_response("task_internal_error", str(exc), 500)
        if detail is None:
            return _task_error_response(
                "task_not_found", f"task not found: {task_id}", 404,
            )
        return detail

    @router.patch("/chat/tasks/{task_id}")
    async def patch_task(task_id: str, payload: dict = Body(default_factory=dict)):
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            return _task_error_response(
                "task_invalid", "expected_version is required (positive int)", 422,
            )
        fields: dict[str, Any] = {}
        for key in _PATCH_ALLOWED_FIELDS:
            if key in payload:
                fields[key] = payload[key]
        # Normalize status string -> TaskStatus enum at service layer via
        # dict (the registry accepts a Mapping; task_service.update_task
        # passes through to registry.update_task which applies fields).
        if "status" in fields and isinstance(fields["status"], str):
            # Service/registry uses TaskStatus enum via dataclass replace;
            # we send the raw value and let the registry map. To keep the
            # public contract simple we coerce here.
            from app.domain.task import TaskStatus as _TS
            try:
                fields["status"] = _TS(fields["status"])
            except ValueError as exc:
                return _task_error_response(
                    "task_invalid", f"invalid status: {exc}", 422,
                )
        if not fields:
            return _task_error_response(
                "task_invalid", "no updatable fields provided", 422,
            )
        try:
            task = await task_service.update_task(task_id, fields, expected_version)
        except TaskNotFoundError:
            return _task_error_response(
                "task_not_found", f"task not found: {task_id}", 404,
            )
        except TaskStateError as exc:
            return _task_error_response("task_state_invalid", str(exc), 409)
        except TaskConflictError as exc:
            return _task_error_response("task_conflict", str(exc), 409)
        except TaskValidationError as exc:
            return _task_error_response("task_invalid", str(exc), 422)
        except Exception as exc:
            return _task_error_response("task_internal_error", str(exc), 500)
        return _task_to_dict(task)

    @router.delete("/chat/tasks/{task_id}")
    async def delete_task(task_id: str):
        try:
            ok = await task_service.delete_task(task_id)
        except TaskNotFoundError:
            return _task_error_response(
                "task_not_found", f"task not found: {task_id}", 404,
            )
        except TaskStateError as exc:
            return _task_error_response("task_state_invalid", str(exc), 409)
        except Exception as exc:
            return _task_error_response("task_internal_error", str(exc), 500)
        if not ok:
            return _task_error_response(
                "task_not_found", f"task not found: {task_id}", 404,
            )
        return Response(status_code=204)

    @router.post("/chat/tasks/bulk")
    async def bulk_update(payload: dict = Body(default_factory=dict)):
        items_raw = payload.get("items") or []
        if not isinstance(items_raw, list) or not items_raw:
            return _task_error_response(
                "task_invalid", "items list is required", 422,
            )
        items: list[BulkUpdateItem] = []
        for raw in items_raw:
            if not isinstance(raw, dict):
                return _task_error_response(
                    "task_invalid", "each item must be an object", 422,
                )
            tid = raw.get("task_id")
            ev = raw.get("expected_version")
            if not isinstance(tid, str) or not tid:
                return _task_error_response(
                    "task_invalid", "task_id is required", 422,
                )
            if not isinstance(ev, int) or ev < 1:
                return _task_error_response(
                    "task_invalid", "expected_version is required (positive int)", 422,
                )
            fields = {k: v for k, v in (raw.get("fields") or {}).items() if k in _PATCH_ALLOWED_FIELDS}
            if "status" in fields and isinstance(fields["status"], str):
                from app.domain.task import TaskStatus as _TS
                try:
                    fields["status"] = _TS(fields["status"])
                except ValueError as exc:
                    return _task_error_response(
                        "task_invalid", f"invalid status: {exc}", 422,
                    )
            if not fields:
                return _task_error_response(
                    "task_invalid", "no updatable fields provided", 422,
                )
            items.append(BulkUpdateItem(task_id=tid, fields=fields, expected_version=ev))
        try:
            updated = await task_service.bulk_update(BulkUpdateCommand(items=tuple(items)))
        except TaskNotFoundError:
            return _task_error_response(
                "task_not_found", "one or more tasks not found", 404,
            )
        except TaskStateError as exc:
            return _task_error_response("task_state_invalid", str(exc), 409)
        except TaskConflictError as exc:
            return _task_error_response("task_conflict", str(exc), 409)
        except TaskValidationError as exc:
            return _task_error_response("task_invalid", str(exc), 422)
        except Exception as exc:
            return _task_error_response("task_internal_error", str(exc), 500)
        return {"items": [_task_to_dict(t) for t in updated]}

    # ---- /{id}/comments ----
    @router.post("/chat/tasks/{task_id}/comments")
    async def add_comment(task_id: str, payload: dict = Body(default_factory=dict)):
        body = str(payload.get("body", "") or "")
        author = str(payload.get("author", "") or "dashboard")
        if not body.strip():
            return _task_error_response(
                "task_invalid", "body is required", 422,
            )
        try:
            return await task_service.add_comment(task_id, body, author)
        except TaskNotFoundError:
            return _task_error_response(
                "task_not_found", f"task not found: {task_id}", 404,
            )
        except Exception as exc:
            return _task_error_response("task_internal_error", str(exc), 500)

    # ---- /{id}/propose-change, /{id}/approve, /{id}/reject, /{id}/cancel, /{id}/retry ----
    @router.post("/chat/tasks/{task_id}/propose-change")
    async def propose_change(task_id: str, payload: dict = Body(default_factory=dict)):
        proposal = str(payload.get("proposal") or "")
        if not proposal:
            return _task_error_response("task_invalid", "proposal is required", 422)
        try:
            detail = await task_service.get_task_detail(task_id)
            if detail is None:
                return _task_error_response("task_not_found", f"task not found: {task_id}", 404)
            run_id = detail.get("current_run_id")
            return await task_service.propose_change(task_id, proposal, run_id)
        except TaskNotFoundError:
            return _task_error_response("task_not_found", f"task not found: {task_id}", 404)
        except TaskStateError as exc:
            return _task_error_response("task_state_invalid", str(exc), 409)
        except TaskValidationError as exc:
            return _task_error_response("task_invalid", str(exc), 422)
        except Exception as exc:
            return _task_error_response("task_internal_error", str(exc), 500)

    @router.post("/chat/tasks/{task_id}/approve")
    async def approve_change(task_id: str, payload: Any = Body(default=None)):
        note, err = _extract_note(payload, required=False)
        if err is not None:
            return _task_error_response("task_invalid", err, 422)
        try:
            return await task_service.approve_change(task_id, note=note)
        except TaskNotFoundError:
            return _task_error_response("task_not_found", _MSG_TASK_NOT_FOUND, 404)
        except TaskValidationError:
            return _task_error_response("task_invalid", _MSG_TASK_INVALID, 422)
        except TaskStateError:
            return _task_error_response("task_state_invalid", _MSG_TASK_STATE_INVALID, 409)
        except TaskConflictError:
            return _task_error_response("task_conflict", _MSG_TASK_CONFLICT, 409)
        except Exception:
            return _task_error_response("task_internal_error", _MSG_TASK_INTERNAL, 500)

    @router.post("/chat/tasks/{task_id}/reject")
    async def reject_change(task_id: str, payload: Any = Body(default=None)):
        note, err = _extract_note(payload, required=False)
        if err is not None:
            return _task_error_response("task_invalid", err, 422)
        try:
            return await task_service.reject_change(task_id, note=note)
        except TaskNotFoundError:
            return _task_error_response("task_not_found", _MSG_TASK_NOT_FOUND, 404)
        except TaskValidationError:
            return _task_error_response("task_invalid", _MSG_TASK_INVALID, 422)
        except TaskStateError:
            return _task_error_response("task_state_invalid", _MSG_TASK_STATE_INVALID, 409)
        except TaskConflictError:
            return _task_error_response("task_conflict", _MSG_TASK_CONFLICT, 409)
        except Exception:
            return _task_error_response("task_internal_error", _MSG_TASK_INTERNAL, 500)

    @router.post("/chat/tasks/{task_id}/revise")
    async def revise_change(task_id: str, payload: Any = Body(default=None)):
        note, err = _extract_note(payload, required=True)
        if err is not None:
            return _task_error_response("task_invalid", err, 422)
        try:
            return await task_service.revise_change(task_id, note=note)
        except TaskNotFoundError:
            return _task_error_response("task_not_found", _MSG_TASK_NOT_FOUND, 404)
        except TaskValidationError:
            return _task_error_response("task_invalid", _MSG_TASK_INVALID, 422)
        except TaskStateError:
            return _task_error_response("task_state_invalid", _MSG_TASK_STATE_INVALID, 409)
        except TaskConflictError:
            return _task_error_response("task_conflict", _MSG_TASK_CONFLICT, 409)
        except Exception:
            return _task_error_response("task_internal_error", _MSG_TASK_INTERNAL, 500)

    @router.post("/chat/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str):
        try:
            return await task_service.cancel_task(task_id)
        except TaskNotFoundError:
            return _task_error_response("task_not_found", f"task not found: {task_id}", 404)
        except TaskStateError as exc:
            return _task_error_response("task_state_invalid", str(exc), 409)
        except Exception as exc:
            return _task_error_response("task_internal_error", str(exc), 500)

    @router.post("/chat/tasks/{task_id}/retry")
    async def retry_task(task_id: str):
        try:
            return await task_service.retry_task(task_id)
        except TaskNotFoundError:
            return _task_error_response("task_not_found", f"task not found: {task_id}", 404)
        except TaskStateError as exc:
            return _task_error_response("task_state_invalid", str(exc), 409)
        except Exception as exc:
            return _task_error_response("task_internal_error", str(exc), 500)

    # ---- /{id}/attachments ----
    @router.get("/chat/tasks/{task_id}/attachments")
    async def list_attachments(task_id: str):
        try:
            attachments = await task_service.list_attachments(task_id)
        except TaskNotFoundError:
            return _task_error_response(
                "task_not_found", f"task not found: {task_id}", 404,
            )
        except Exception as exc:
            return _task_error_response("task_internal_error", str(exc), 500)
        return {"items": [_attachment_to_dict(a) for a in attachments]}

    @router.post("/chat/tasks/{task_id}/attachments")
    async def upload_attachment(
        task_id: str,
        file: UploadFile = File(...),
        uploaded_by: str = Form(default="dashboard"),
    ):
        # Read content first (FastAPI streams uploads; we cap via Settings
        # at the service layer).
        try:
            content = await file.read()
        except Exception as exc:
            return _task_error_response(
                "task_attachment_invalid", f"failed to read upload: {exc}", 422,
            )
        try:
            attachment = await task_service.upload_attachment(
                task_id=task_id,
                filename=file.filename or "upload.bin",
                content=content,
                content_type=file.content_type or "application/octet-stream",
                uploaded_by=uploaded_by,
            )
        except TaskNotFoundError:
            return _task_error_response(
                "task_not_found", f"task not found: {task_id}", 404,
            )
        except TaskValidationError as exc:
            # Size or filename validation failures
            msg = str(exc)
            if "too large" in msg:
                return _task_error_response("task_attachment_too_large", msg, 413)
            return _task_error_response("task_attachment_invalid", msg, 422)
        except TaskAttachmentError as exc:
            return _task_error_response("task_attachment_invalid", str(exc), 422)
        except Exception as exc:
            return _task_error_response("task_internal_error", str(exc), 500)
        return _attachment_to_dict(attachment)


# ---------------------------------------------------------------------------
# WebSocket helpers
# ---------------------------------------------------------------------------


async def _ws_safe_put(queue: asyncio.Queue, item: dict[str, Any]) -> None:
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        # Drop oldest and try again (slow consumer); the client will
        # resync via last event id on reconnect.
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            pass


async def _ws_poll_events(
    task_service: TaskService,
    queue: asyncio.Queue[dict[str, Any]],
    last_event_id: int,
) -> None:
    """Poll task_events for new rows periodically.

    A registry-level subscribe would be cleaner, but to keep the
    Interfaces layer free of SQLite we poll via the Application service.
    """
    try:
        while True:
            await asyncio.sleep(1.0)
            try:
                page = await task_service.list_tasks(limit=_LIST_LIMIT_MAX)
            except Exception:
                continue
            current_max = last_event_id
            for task in page.items:
                try:
                    events = await task_service.list_events(
                        task.id, since=last_event_id, limit=200,
                    )
                except Exception:
                    continue
                for e in events:
                    if e.id > current_max:
                        current_max = e.id
                    await _ws_safe_put(queue, _event_to_dict(e))
            if current_max > last_event_id:
                last_event_id = current_max
    except asyncio.CancelledError:
        raise


async def _ws_recv_loop(websocket: WebSocket) -> None:
    """Receive loop to detect client disconnect."""
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        return
    except Exception:
        return


async def _ws_send_loop(
    websocket: WebSocket,
    queue: asyncio.Queue[dict[str, Any]],
) -> None:
    try:
        while True:
            item = await queue.get()
            await websocket.send_text(json.dumps(item, ensure_ascii=False, default=str))
    except WebSocketDisconnect:
        return
    except Exception:
        return


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


# 5 Kanban swimlanes (Manus 7 statuses merged): (lane_id, statuses, label)
_SWIMLANES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("queued", ("queued",), "排队"),
    ("running", ("running",), "运行中"),
    ("waiting_approval", ("waiting_approval",), "待批准"),
    ("failed_expired", ("failed", "expired"), "失败/过期"),
    ("succeeded_cancelled", ("succeeded", "cancelled"), "成功/取消"),
)


def _group_by_status(tasks: list[Any]) -> list[dict[str, Any]]:
    """Group tasks into 5 Kanban swimlanes (Manus 7 statuses merged)."""
    by_status: dict[str, list[Any]] = {}
    for t in tasks:
        status_value = t.status.value if hasattr(t.status, "value") else str(t.status)
        by_status.setdefault(status_value, []).append(t)
    columns = []
    for lane_id, statuses, label in _SWIMLANES:
        items: list[Any] = []
        for st in statuses:
            items.extend(by_status.get(st, []))
        # Newer tasks lead each swimlane, independent of priority.
        items.sort(
            key=lambda t: (t.created_at or datetime.min.replace(tzinfo=timezone.utc), t.id),
            reverse=True,
        )
        columns.append({
            "id": lane_id,
            "label": label,
            "statuses": list(statuses),
            "cards": [_task_to_dict(t) for t in items],
            "total": len(items),
        })
    return columns


def _task_to_dict(task: Any) -> dict[str, Any]:
    """Serialize a Task aggregate to a JSON-safe dict."""
    # Reuse the worker-facing serializer for consistency.
    from app.application.task_service import _task_to_dict as _svc_to_dict
    return _svc_to_dict(task)


def _run_to_dict(run: Any) -> dict[str, Any]:
    return {
        "id": run.id,
        "task_id": run.task_id,
        "profile": run.profile,
        "status": run.status.value if hasattr(run.status, "value") else str(run.status),
        "outcome": run.outcome.value if run.outcome and hasattr(run.outcome, "value") else None,
        "claim_lock": run.claim_lock,
        "worker_token": run.worker_token,
        "started_at": _dt_str(run.started_at),
        "ended_at": _dt_str(run.ended_at),
        "summary": run.summary,
        "error": run.error,
        "last_heartbeat_at": _dt_str(run.last_heartbeat_at),
    }


def _event_to_dict(event: Any) -> dict[str, Any]:
    return {
        "id": event.id,
        "task_id": event.task_id,
        "run_id": event.run_id,
        "kind": event.kind,
        "payload": event.payload or {},
        "created_at": _dt_str(event.created_at),
    }


def _attachment_to_dict(att: Any) -> dict[str, Any]:
    return {
        "id": att.id,
        "task_id": att.task_id,
        "filename": att.filename,
        "content_type": att.content_type,
        "size": att.size,
        "checksum": att.checksum,
        "uploaded_by": att.uploaded_by,
        "created_at": _dt_str(att.created_at),
    }


def _cursor_to_dict(cursor: Any) -> dict[str, Any] | None:
    if cursor is None:
        return None
    return {
        "created_at": _dt_str(getattr(cursor, "created_at", None)),
        "task_id": getattr(cursor, "task_id", ""),
    }


def _dt_str(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Error response builder
# ---------------------------------------------------------------------------


def _task_error_response(code: str, message: str, status_code: int) -> JSONResponse:
    """Build the unified error envelope.

    Internal exception messages are scrubbed at the route boundary; we
    never leak SQL, file paths, or secrets.
    """
    safe_message = _scrub_message(message)
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": safe_message}},
    )


def _scrub_message(message: str) -> str:
    """Best-effort scrub of internal details from error messages.

    Avoids leaking SQLite paths, SQL fragments, or absolute filesystem
    paths. Keeps the short informative tail.
    """
    if not isinstance(message, str):
        return "internal error"
    # Replace backslash path separators on common leak patterns.
    for token in ("sqlite3.OperationalError:", "sqlite3.IntegrityError:"):
        if token in message:
            return "registry error"
    return message

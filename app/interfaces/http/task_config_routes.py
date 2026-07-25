"""Task config Dashboard API -- GET/PATCH /chat/tasks/security/config.

Read-only+write route for the 9 C-class task config fields. PATCH is CAS with
expected_version; body updated_by is rejected (actor from trusted context).
Registered before /chat/tasks/{task_id} to avoid catch-all capture.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from app.application.task_config_service import TaskConfigService
from app.domain.task_config import (
    TASK_CONFIG_FIELDS,
    TaskConfigConflictError,
    TaskConfigValidationError,
)

logger = logging.getLogger(__name__)

# Trusted actor when the Dashboard has no auth principal (current deployment).
_DEFAULT_ACTOR = "dashboard-local"


def _resolved_to_dto(resolved: Any) -> dict[str, Any]:
    return {
        "config": {
            f: getattr(resolved.config, f) for f in TASK_CONFIG_FIELDS
        },
        "version": resolved.version,
        "overridden_fields": list(resolved.overridden_fields),
        "updated_at": resolved.updated_at,
        "updated_by": resolved.updated_by,
    }


def register_task_config_routes(router: APIRouter, service: TaskConfigService) -> None:
    """Register GET/PATCH /chat/tasks/security/config."""

    @router.get("/chat/tasks/security/config")
    async def get_config():
        try:
            resolved = await service.get_resolved()
        except Exception:
            logger.exception("task config get failed")
            return JSONResponse(
                status_code=500,
                content={"error": {"code": "task_config_load_failed",
                                   "message": "Task config could not be loaded"}},
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(content=_resolved_to_dto(resolved), headers={"Cache-Control": "no-store"})

    @router.patch("/chat/tasks/security/config")
    async def patch_config(payload: Any = Body(default_factory=dict)):
        if not isinstance(payload, dict):
            return _err(422, "task_config_invalid", "body must be a JSON object")
        # body updated_by is rejected; actor is trusted context.
        if "updated_by" in payload:
            return _err(422, "task_config_invalid", "updated_by must not be provided")
        expected_version = payload.get("expected_version")
        # expected_version must be a non-negative int (not bool).
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
            return _err(422, "task_config_invalid", "expected_version must be a non-negative integer")
        # Build partial from whitelisted C-class fields only.
        partial: dict[str, int] = {}
        for f in TASK_CONFIG_FIELDS:
            if f in payload:
                v = payload[f]
                if isinstance(v, bool) or not isinstance(v, int):
                    return _err(422, "task_config_invalid", f"field {f} must be an integer")
                partial[f] = v
        if not partial:
            return _err(422, "task_config_invalid", "no configurable fields provided")
        try:
            resolved = await service.update(partial, expected_version, _DEFAULT_ACTOR)
        except TaskConfigValidationError as exc:
            return _err(422, "task_config_invalid", str(exc))
        except TaskConfigConflictError:
            return _err(409, "task_config_conflict", "Config version mismatch; reload and retry")
        except Exception:
            logger.exception("task config update failed")
            return _err(500, "task_config_load_failed", "Task config could not be saved")
        return JSONResponse(content=_resolved_to_dto(resolved), headers={"Cache-Control": "no-store"})


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
        headers={"Cache-Control": "no-store"},
    )

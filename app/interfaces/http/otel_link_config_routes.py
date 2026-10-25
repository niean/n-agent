"""OTel link config Dashboard API -- GET/PATCH /chat/observations/otel/links.

Read+write route for the 4 C-class OTel external-link base URLs. PATCH is
CAS with expected_version; body updated_by is rejected (actor from trusted
context). null clears a field's override (follow env); "" disables the link.
Malformed JSON is parsed locally via Request.json() so every failure shares
the uniform 422 error contract with Cache-Control: no-store. Error messages
carry safe field names and rules only -- submitted URLs are never echoed.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.application.otel_link_config_service import OtelLinkConfigService
from app.domain.otel_link_config import (
    OTEL_LINK_CONFIG_FIELDS,
    OtelLinkConfigConflictError,
    OtelLinkConfigStoreError,
    OtelLinkConfigValidationError,
)

logger = logging.getLogger(__name__)

# Trusted actor when the Dashboard has no auth principal (current deployment).
_DEFAULT_ACTOR = "dashboard-local"


def _resolved_to_dto(resolved: Any) -> dict[str, Any]:
    return {
        "config": {f: getattr(resolved.config, f) for f in OTEL_LINK_CONFIG_FIELDS},
        "version": resolved.version,
        "overridden_fields": list(resolved.overridden_fields),
        "updated_at": resolved.updated_at,
        "updated_by": resolved.updated_by,
    }


def register_otel_link_config_routes(router: APIRouter, service: OtelLinkConfigService) -> None:
    """Register GET/PATCH /chat/observations/otel/links."""

    @router.get("/chat/observations/otel/links")
    async def get_otel_link_config():
        try:
            resolved = await service.get_resolved()
        except OtelLinkConfigStoreError:
            logger.warning("otel link config get failed: store error")
            return _err(500, "otel_link_config_load_failed", "OTel link config could not be loaded")
        except Exception:
            logger.warning("otel link config get failed: unexpected error")
            return _err(500, "otel_link_config_load_failed", "OTel link config could not be loaded")
        return JSONResponse(
            content=_resolved_to_dto(resolved), headers={"Cache-Control": "no-store"}
        )

    @router.patch("/chat/observations/otel/links")
    async def patch_otel_link_config(request: Request):
        try:
            payload = await request.json()
        except Exception:
            return _err(422, "otel_link_config_invalid", "body must be valid JSON")
        if not isinstance(payload, dict):
            return _err(422, "otel_link_config_invalid", "body must be a JSON object")
        # body updated_by is rejected; actor is trusted context.
        if "updated_by" in payload:
            return _err(422, "otel_link_config_invalid", "updated_by must not be provided")
        expected_version = payload.get("expected_version")
        # expected_version must be present and a non-negative int (not bool).
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 0
        ):
            return _err(
                422, "otel_link_config_invalid", "expected_version must be a non-negative integer"
            )
        # Whitelist the 4 C-class fields; unknown keys get a fixed safe
        # message (the attacker-controlled key is never echoed). Values are
        # str ("" disables) or None (clears the override -> follow env).
        partial: dict[str, str | None] = {}
        for key, value in payload.items():
            if key == "expected_version":
                continue
            if key not in OTEL_LINK_CONFIG_FIELDS:
                return _err(422, "otel_link_config_invalid", "unknown field in patch")
            if value is not None and (isinstance(value, bool) or not isinstance(value, str)):
                return _err(422, "otel_link_config_invalid", f"field {key} must be str or null")
            partial[key] = value
        if not partial:
            return _err(422, "otel_link_config_invalid", "no configurable fields provided")
        try:
            resolved = await service.update(partial, expected_version, _DEFAULT_ACTOR)
        except OtelLinkConfigValidationError as exc:
            return _err(422, "otel_link_config_invalid", str(exc))
        except OtelLinkConfigConflictError:
            return _err(409, "otel_link_config_conflict", "Config version mismatch; reload and retry")
        except OtelLinkConfigStoreError:
            logger.warning("otel link config update failed: store error")
            return _err(500, "otel_link_config_load_failed", "OTel link config could not be saved")
        except Exception:
            logger.warning("otel link config update failed: unexpected error")
            return _err(500, "otel_link_config_load_failed", "OTel link config could not be saved")
        return JSONResponse(
            content=_resolved_to_dto(resolved), headers={"Cache-Control": "no-store"}
        )


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
        headers={"Cache-Control": "no-store"},
    )

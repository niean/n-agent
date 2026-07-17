"""Application orchestration for the narrowly allowlisted host terminal."""
from __future__ import annotations

import hashlib
import asyncio
import json
import re
from dataclasses import replace
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from app.application.policy_audit_service import PolicyAuditService
from app.application.host_terminal_capability import is_photo_capability_request
from app.application.skill_service import SkillService
from app.domain.host_terminal import (
    HostCommandTarget,
    HostSkillScriptTarget,
    HostTerminalBridgeClient,
    HostTerminalBridgeRequest,
    HostTerminalBridgeResponse,
    HostTerminalExecutionLimits,
    HostTerminalStatus,
)
from app.domain.host_terminal_policy import (
    HostSkillScriptRule,
    HostTerminalPolicy,
    HostTerminalPolicyRequest,
    HostTerminalPolicySnapshot,
    host_terminal_arguments_allowed,
)
from app.domain.policy import (
    PolicyAuditEvent,
    PolicyDecisionKind,
    PolicyOutcome,
)
from app.domain.skill import SkillNotFoundError, SkillValidationError
from app.domain.tool import (
    RiskLevel,
    ToolCallRequest,
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
    ToolResultStatus,
    ToolSourceType,
)


_PROTOCOL_VERSION = "1"
_CAPTURE_RE = re.compile(r"CAPTURED:([A-Za-z0-9._-]+):([1-9][0-9]*)")
_HTTP_RE = re.compile(r"UPLOAD_HTTP:(2[0-9]{2})")
_URL_RE = re.compile(r"URL:(https://[^\s\r\n]+)")
_BRIDGE_CODES = {
    "host_bridge_unavailable",
    "host_bridge_auth_failed",
    "host_bridge_busy",
    "host_policy_version_mismatch",
    "skill_script_hash_mismatch",
    "skill_script_path_denied",
    "skill_script_not_found",
    "host_execution_timeout",
    "host_bridge_invalid_response",
    "host_bridge_unhealthy",
    "host_target_not_allowed",
    "host_executable_denied",
    "host_execution_failed",
    "host_execution_cancelled",
    "host_process_start_failed",
    "host_bridge_internal_error",
    "host_bridge_invalid_request",
    "host_command_signing_failed",
    "host_command_snapshot_invalid",
}
_STAGE_ERROR_RE = re.compile(r"^ERROR:([a-z][a-z0-9_]{0,63})$", re.MULTILINE)


def _parse_stage_error(stderr: str) -> str | None:
    """Extract the desensitized stage code a Skill script writes to stderr.

    Skill scripts emit ``ERROR:<code>`` on failure where ``<code>`` is a stable,
    desensitized stage identifier. Only that code is surfaced; raw stderr never
    enters the model context.
    """
    if not isinstance(stderr, str):
        return None
    match = _STAGE_ERROR_RE.search(stderr)
    return match.group(1) if match is not None else None


class HostTerminalPolicySnapshotProvider(Protocol):
    @property
    def snapshot(self) -> HostTerminalPolicySnapshot | None: ...

    @property
    def last_error_code(self) -> str | None: ...

    def refresh(self) -> bool: ...


class ImagePersister(Protocol):
    """Persists photo-upload images so they remain renderable after the
    OSS signed URL expires (~1h). Returns a permanent serve URL, or ``None``
    to fall back to the original (expiring) URL."""

    async def persist(self, source_url: str, mime_hint: str | None = None) -> str | None: ...


def host_terminal_tool_definition(timeout_seconds: int = 120) -> ToolDefinition:
    common_properties: dict[str, Any] = {
        "args": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "timeout": {"type": "integer", "minimum": 1},
    }
    return ToolDefinition(
        name="host_terminal",
        description=(
            "Execute one administrator-allowlisted host command or linked Skill "
            "script. This is not a general shell and accepts argv only."
        ),
        input_schema={
            "type": "object",
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "target_type": {"const": "command"},
                        "command": {"type": "string", "minLength": 1},
                        **common_properties,
                    },
                    "required": ["target_type", "command", "args"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "target_type": {"const": "skill_script"},
                        "skill": {"type": "string", "minLength": 1},
                        "script": {"type": "string", "minLength": 1},
                        **common_properties,
                    },
                    "required": ["target_type", "skill", "script", "args"],
                    "additionalProperties": False,
                },
            ]
        },
        risk_level=RiskLevel.SAFE,
        source_type=ToolSourceType.AGENT,
        toolset="host",
        managed=False,
        timeout_seconds=timeout_seconds,
    )


class HostTerminalToolExecutor:
    def __init__(
        self,
        *,
        client: HostTerminalBridgeClient,
        skill_service: SkillService,
        policy_loader: HostTerminalPolicySnapshotProvider,
        tool_timeout_seconds: int = 120,
        bridge_timeout_seconds: int | None = None,
        max_stdout_bytes: int = 65536,
        max_stderr_bytes: int = 16384,
        max_concurrency: int = 1,
        audit_service: PolicyAuditService | None = None,
        image_persister: ImagePersister | None = None,
    ) -> None:
        self._client = client
        self._skill_service = skill_service
        self._policy_loader = policy_loader
        self._tool_timeout_seconds = tool_timeout_seconds
        self._bridge_timeout_seconds = bridge_timeout_seconds or tool_timeout_seconds
        self._max_stdout_bytes = max_stdout_bytes
        self._max_stderr_bytes = max_stderr_bytes
        self._max_concurrency = max_concurrency
        self._audit_service = audit_service
        self._image_persister = image_persister
        self.last_health_code = "host_bridge_not_checked"

    async def execute(
        self,
        request: ToolCallRequest,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        try:
            return await self._execute_once(request, context)
        except asyncio.CancelledError:
            audit_task = asyncio.create_task(
                self._audit(
                    request,
                    context,
                    self._policy_loader.snapshot,
                    self._target_type(request.arguments),
                    "unresolved",
                    None,
                    "host_execution_cancelled",
                    None,
                    0,
                    False,
                    False,
                )
            )
            try:
                await asyncio.shield(audit_task)
            except asyncio.CancelledError:
                pass
            raise

    async def _execute_once(
        self,
        request: ToolCallRequest,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        if request.name != "host_terminal":
            await self._audit(
                request,
                context,
                self._policy_loader.snapshot,
                self._target_type(request.arguments),
                "unsupported",
                None,
                "host_terminal_unsupported_route",
                None,
                0,
                False,
                False,
            )
            return self._error(request, "host_terminal_unsupported_route")

        # Refresh once, then pin this exact immutable object for the whole call.
        await asyncio.to_thread(self._policy_loader.refresh)
        snapshot = self._policy_loader.snapshot
        if snapshot is None or not snapshot.rules:
            await self._audit(
                request,
                context,
                snapshot,
                self._target_type(request.arguments),
                "unresolved",
                None,
                "host_policy_unavailable",
                None,
                0,
                False,
                False,
            )
            return self._error(request, "host_policy_unavailable")

        target_type = self._target_type(request.arguments)
        args, validation_error = self._validate_shape(request.arguments, snapshot)
        if validation_error is not None:
            await self._audit(
                request, context, snapshot, target_type, "invalid", None,
                validation_error, None, 0, False, False,
            )
            return self._error(request, validation_error)

        timeout = request.arguments.get(
            "timeout", snapshot.limits.default_timeout_seconds
        )
        requested_limits = HostTerminalExecutionLimits(
            timeout_seconds=timeout,
            max_stdout_bytes=min(self._max_stdout_bytes, snapshot.limits.max_stdout_bytes),
            max_stderr_bytes=min(self._max_stderr_bytes, snapshot.limits.max_stderr_bytes),
            max_concurrency=min(self._max_concurrency, snapshot.limits.max_concurrency),
        )

        if target_type == "command":
            try:
                target = HostCommandTarget(request.arguments["command"], tuple(args))
            except (TypeError, ValueError):
                await self._audit(
                    request,
                    context,
                    snapshot,
                    "command",
                    "invalid",
                    None,
                    "host_request_invalid",
                    None,
                    0,
                    False,
                    False,
                )
                return self._error(request, "host_request_invalid")
            target_label = target.executable.rsplit("/", 1)[-1]
        else:
            try:
                script = await self._skill_service.resolve_script_bytes(
                    request.arguments["skill"], request.arguments["script"]
                )
            except SkillNotFoundError:
                await self._audit(
                    request,
                    context,
                    snapshot,
                    "skill_script",
                    "unresolved_skill_script",
                    None,
                    "skill_script_not_found",
                    None,
                    0,
                    False,
                    False,
                )
                return self._error(request, "skill_script_not_found")
            except SkillValidationError:
                await self._audit(
                    request,
                    context,
                    snapshot,
                    "skill_script",
                    "unresolved_skill_script",
                    None,
                    "skill_script_path_denied",
                    None,
                    0,
                    False,
                    False,
                )
                return self._error(request, "skill_script_path_denied")
            try:
                target = HostSkillScriptTarget(
                    skill_name=script.skill_name,
                    script_relative_path=script.script_relative_path,
                    sha256=script.sha256,
                    args=tuple(args),
                )
            except (TypeError, ValueError):
                await self._audit(
                    request,
                    context,
                    snapshot,
                    "skill_script",
                    "unresolved_skill_script",
                    None,
                    "host_request_invalid",
                    None,
                    0,
                    False,
                    False,
                )
                return self._error(request, "host_request_invalid")
            target_label = f"{target.skill_name}/{target.script_relative_path}"

        decision = HostTerminalPolicy(snapshot).evaluate(
            HostTerminalPolicyRequest(target=target, requested_limits=requested_limits)
        )
        if not decision.allowed:
            internal_reason = decision.reason
            if isinstance(target, HostSkillScriptTarget) and self._is_hash_mismatch(
                snapshot, target
            ):
                internal_reason = "skill_script_hash_mismatch"
                result = self._error(request, internal_reason)
            else:
                result = ToolResult(
                    request.id,
                    request.name,
                    ToolResultStatus.PERMISSION_DENIED,
                    {"error": "host_target_not_allowed"},
                )
            await self._audit(
                request, context, snapshot, target_type,
                "unresolved_skill_script"
                if isinstance(target, HostSkillScriptTarget)
                else target_label,
                decision.rule_id,
                internal_reason, None, 0, False, False,
            )
            return result

        effective_timeout = min(
            requested_limits.timeout_seconds,
            self._tool_timeout_seconds,
            self._bridge_timeout_seconds,
            snapshot.limits.max_timeout_seconds,
        )
        limits = HostTerminalExecutionLimits(
            timeout_seconds=effective_timeout,
            max_stdout_bytes=requested_limits.max_stdout_bytes,
            max_stderr_bytes=requested_limits.max_stderr_bytes,
            max_concurrency=requested_limits.max_concurrency,
        )
        bridge_request = HostTerminalBridgeRequest(
            protocol_version=_PROTOCOL_VERSION,
            request_id=str(uuid4()),
            target=target,
            n_agent_policy_version=snapshot.version,
            n_agent_content_digest=snapshot.content_digest,
            limits=limits,
        )
        try:
            response = await self._client.execute(bridge_request)
        except Exception as exc:
            code = getattr(exc, "error_code", "host_bridge_unavailable")
            if code not in _BRIDGE_CODES:
                code = "host_bridge_unavailable"
            self.last_health_code = code
            await self._audit(
                request, context, snapshot, target_type, target_label, decision.rule_id,
                code, None, 0, code == "host_execution_timeout", False,
            )
            return self._mapped_error(request, code)

        result, reason = self._normalize_response(
            request, response, bridge_request.request_id
        )
        self.last_health_code = (
            "ok" if result.status is ToolResultStatus.SUCCESS else reason
        )
        await self._audit(
            request, context, snapshot, target_type, target_label, decision.rule_id,
            reason, response.exit_code, response.duration_ms,
            response.status is HostTerminalStatus.TIMEOUT,
            response.stdout_truncated or response.stderr_truncated,
        )
        return await self._persist_photo_image(result)

    async def _persist_photo_image(self, result: ToolResult) -> ToolResult:
        """Replace the expiring OSS signed_url in a photo success result with a
        permanent serve URL. No-op when no persister is wired, on non-photo
        results, or when persistence fails (falls back to the original URL)."""
        if self._image_persister is None or result.status is not ToolResultStatus.SUCCESS:
            return result
        content = result.content
        if not isinstance(content, dict):
            return result
        signed_url = content.get("signed_url")
        if not isinstance(signed_url, str) or not signed_url:
            return result
        try:
            permanent = await self._image_persister.persist(signed_url)
        except Exception:
            permanent = None
        if not permanent:
            return result
        return replace(result, content={**content, "signed_url": permanent})

    @staticmethod
    def _validate_shape(
        arguments: Any, snapshot: HostTerminalPolicySnapshot
    ) -> tuple[list[str], str | None]:
        if not isinstance(arguments, dict):
            return [], "host_request_invalid"
        kind = arguments.get("target_type")
        allowed = (
            {"target_type", "command", "args", "timeout"}
            if kind == "command"
            else {"target_type", "skill", "script", "args", "timeout"}
            if kind == "skill_script"
            else set()
        )
        required = (
            {"target_type", "command", "args"}
            if kind == "command"
            else {"target_type", "skill", "script", "args"}
            if kind == "skill_script"
            else set()
        )
        if not allowed or set(arguments) - allowed or not required <= set(arguments):
            return [], "host_request_invalid"
        identity_fields = ["command"] if kind == "command" else ["skill", "script"]
        if any(
            not isinstance(arguments.get(field), str)
            or not arguments[field]
            or "\x00" in arguments[field]
            or "\n" in arguments[field]
            or "\r" in arguments[field]
            for field in identity_fields
        ):
            return [], "host_request_invalid"
        args = arguments.get("args")
        if not host_terminal_arguments_allowed(args, snapshot.limits):
            return [], "host_arguments_invalid"
        timeout = arguments.get("timeout", snapshot.limits.default_timeout_seconds)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int)
            or timeout < 1
            or timeout > snapshot.limits.max_timeout_seconds
        ):
            return [], "host_resource_limits_exceeded"
        return args, None

    @staticmethod
    def _is_hash_mismatch(
        snapshot: HostTerminalPolicySnapshot, target: HostSkillScriptTarget
    ) -> bool:
        return any(
            isinstance(rule, HostSkillScriptRule)
            and rule.skill_name == target.skill_name
            and rule.script_relative_path == target.script_relative_path
            and rule.sha256 != target.sha256
            for rule in snapshot.skill_script_rules
        )

    def _normalize_response(
        self,
        request: ToolCallRequest,
        response: HostTerminalBridgeResponse,
        expected_request_id: str,
    ) -> tuple[ToolResult, str]:
        if (
            response.protocol_version != _PROTOCOL_VERSION
            or response.request_id != expected_request_id
        ):
            return self._error(request, "host_bridge_invalid_response"), "host_bridge_invalid_response"
        if response.status is HostTerminalStatus.TIMEOUT:
            return self._mapped_error(request, "host_execution_timeout"), "host_execution_timeout"
        if response.status is not HostTerminalStatus.SUCCESS:
            code = response.error_code or "host_bridge_invalid_response"
            if code not in _BRIDGE_CODES:
                code = "host_bridge_invalid_response"
            if code in {"host_target_not_allowed", "host_executable_denied"}:
                return (
                    ToolResult(
                        request.id,
                        request.name,
                        ToolResultStatus.PERMISSION_DENIED,
                        {"error": "host_target_not_allowed"},
                        duration_ms=response.duration_ms,
                    ),
                    code,
                )
            if code == "host_bridge_unhealthy":
                code = "host_bridge_unavailable"
            if code == "host_execution_failed" and self._target_type(request.arguments) == "skill_script":
                stage = _parse_stage_error(response.stderr)
                if stage is not None:
                    return (
                        ToolResult(
                            request.id,
                            request.name,
                            ToolResultStatus.ERROR,
                            {"error": "host_execution_failed", "stage": stage},
                            duration_ms=response.duration_ms,
                        ),
                        code,
                    )
            return self._mapped_error(request, code), code
        if (
            response.exit_code != 0
            or response.stdout_truncated
            or response.stderr_truncated
        ):
            return self._error(request, "host_bridge_invalid_response"), "host_bridge_invalid_response"
        if not is_photo_capability_request(request.arguments):
            return (
                ToolResult(
                    request.id,
                    request.name,
                    ToolResultStatus.SUCCESS,
                    {"success": True},
                    duration_ms=response.duration_ms,
                ),
                "host_execution_succeeded",
            )
        lines = response.stdout.splitlines()
        if len(lines) != 3:
            return self._error(request, "host_bridge_invalid_response"), "host_bridge_invalid_response"
        capture = _CAPTURE_RE.fullmatch(lines[0])
        upload = _HTTP_RE.fullmatch(lines[1])
        url_line = _URL_RE.fullmatch(lines[2])
        if not capture or not upload or not url_line:
            return self._error(request, "host_bridge_invalid_response"), "host_bridge_invalid_response"
        signed_url = url_line.group(1)
        parsed = urlsplit(signed_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            return self._error(request, "host_bridge_invalid_response"), "host_bridge_invalid_response"
        return (
            ToolResult(
                request.id,
                request.name,
                ToolResultStatus.SUCCESS,
                {
                    "capture_size": int(capture.group(2)),
                    "upload_http": int(upload.group(1)),
                    "signed_url": signed_url,
                },
                duration_ms=response.duration_ms,
            ),
            "host_execution_succeeded",
        )

    async def _audit(
        self,
        request: ToolCallRequest,
        context: ToolExecutionContext | None,
        snapshot: HostTerminalPolicySnapshot | None,
        target_type: str,
        target_label: str,
        rule_id: str | None,
        reason: str,
        exit_code: int | None,
        duration_ms: int,
        timed_out: bool,
        truncated: bool,
    ) -> None:
        if self._audit_service is None:
            return
        summary = hashlib.sha256(
            json.dumps(request.arguments, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        safe_reason = json.dumps(
            {
                "reason": reason,
                "tool_call": request.id,
                "target_type": target_type,
                "target": target_label,
                "request_sha256": summary,
                "rule": rule_id,
                "exit": exit_code,
                "duration_ms": duration_ms,
                "timeout": timed_out,
                "truncated": truncated,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        await self._audit_service.record(
            PolicyAuditEvent(
                policy="host_terminal",
                version=snapshot.version if snapshot is not None else "unavailable",
                decision_kind=PolicyDecisionKind.ADMISSION,
                reason=safe_reason,
                run_id=(context.run_id if context and context.run_id else "unknown"),
                session_id=(context.session_id if context and context.session_id else "unknown"),
                outcome=(
                    PolicyOutcome.ALLOW
                    if rule_id is not None
                    else PolicyOutcome.DENY
                ),
            )
        )

    @staticmethod
    def _target_type(arguments: Any) -> str:
        if isinstance(arguments, dict) and arguments.get("target_type") in {
            "command",
            "skill_script",
        }:
            return arguments["target_type"]
        return "invalid"

    @staticmethod
    def _skill_target_label(arguments: Any) -> str:
        if not isinstance(arguments, dict):
            return "invalid"
        skill = arguments.get("skill")
        script = arguments.get("script")
        if not isinstance(skill, str) or not isinstance(script, str):
            return "invalid"
        return f"{skill}/{script}"

    @staticmethod
    def _error(request: ToolCallRequest, code: str) -> ToolResult:
        return ToolResult(
            request.id, request.name, ToolResultStatus.ERROR, {"error": code}
        )

    @staticmethod
    def _mapped_error(request: ToolCallRequest, code: str) -> ToolResult:
        status = (
            ToolResultStatus.TIMEOUT
            if code == "host_execution_timeout"
            else ToolResultStatus.ERROR
        )
        return ToolResult(request.id, request.name, status, {"error": code})


__all__ = [
    "HostTerminalPolicySnapshotProvider",
    "HostTerminalToolExecutor",
    "host_terminal_tool_definition",
]

from datetime import datetime, timezone
from dataclasses import replace
import asyncio
import hashlib
import json
import threading
import time

import pytest

from app.application.host_terminal_tool_executor import (
    HostTerminalToolExecutor,
    host_terminal_tool_definition,
)
from app.application.tool_service import ToolService
from app.application.policy_audit_service import PolicyAuditService
from app.application.skill_service import SkillScriptBytes
from app.domain.host_terminal import HostTerminalBridgeResponse, HostTerminalStatus
from app.domain.host_terminal_policy import (
    HostCommandRule,
    HostSkillScriptRule,
    HostTerminalPolicySnapshot,
    HostTerminalResourceLimits,
    host_terminal_arguments_allowed,
)
from app.domain.skill import SkillNotFoundError, SkillValidationError
from app.domain.tool import (
    RiskLevel,
    ToolCallRequest,
    ToolExecutionContext,
    ToolResultStatus,
    ToolSourceType,
)


SCRIPT = b"print('photo')\n"
DIGEST = hashlib.sha256(SCRIPT).hexdigest()


def _snapshot(*, digest=DIGEST):
    return HostTerminalPolicySnapshot(
        schema_version=1,
        version="v1",
        content_digest="a" * 64,
        loaded_at=datetime.now(timezone.utc),
        limits=HostTerminalResourceLimits(120, 120, 8192, 8192, 2, 64, 128, 1),
        skill_script_rules=(
            HostSkillScriptRule(
                "photo", "photo-and-upload", "scripts/photo-upload.py", digest, ()
            ),
        ),
    )


def _command_snapshot():
    return HostTerminalPolicySnapshot(
        schema_version=1,
        version="v1",
        content_digest="a" * 64,
        loaded_at=datetime.now(timezone.utc),
        limits=HostTerminalResourceLimits(120, 120, 8192, 8192, 2, 64, 128, 1),
        command_rules=(HostCommandRule("echo", "/bin/echo", ()),),
    )


def _other_skill_snapshot():
    return HostTerminalPolicySnapshot(
        schema_version=1,
        version="v1",
        content_digest="a" * 64,
        loaded_at=datetime.now(timezone.utc),
        limits=HostTerminalResourceLimits(120, 120, 8192, 8192, 2, 64, 128, 1),
        skill_script_rules=(
            HostSkillScriptRule(
                "other", "other-skill", "scripts/other.py", DIGEST, ()
            ),
        ),
    )


class _Loader:
    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.refresh_count = 0

    @property
    def snapshot(self):
        return self._snapshot

    @property
    def last_error_code(self):
        return None

    def refresh(self):
        self.refresh_count += 1
        return True


class _Skills:
    async def resolve_script_bytes(self, skill, script):
        return SkillScriptBytes(skill, script, SCRIPT, DIGEST)


class _Client:
    def __init__(self, response=None):
        self.requests = []
        self.response = response

    async def execute(self, request):
        self.requests.append(request)
        return self.response or HostTerminalBridgeResponse(
            protocol_version="1",
            request_id=request.request_id,
            status=HostTerminalStatus.SUCCESS,
            exit_code=0,
            stdout="CAPTURED:photo.jpg:12\nUPLOAD_HTTP:200\nURL:https://example.invalid/photo.jpg?sig=test\n",
            stderr="",
            duration_ms=10,
            stdout_truncated=False,
            stderr_truncated=False,
        )


class _PinnedClient:
    """Returns a response pinned to each request's id, for success/error paths."""

    def __init__(self, response):
        self.requests = []
        self._response = response

    async def execute(self, request):
        self.requests.append(request)
        return replace(self._response, request_id=request.request_id)


class _FakePersister:
    def __init__(self, url=None, exc=None):
        self.calls = []
        self._url = url
        self._exc = exc

    async def persist(self, source_url, mime_hint=None):
        self.calls.append((source_url, mime_hint))
        if self._exc is not None:
            raise self._exc
        return self._url


def _executor(snapshot=None, client=None):
    loader = _Loader(snapshot or _snapshot())
    client = client or _Client()
    return HostTerminalToolExecutor(
        client=client, skill_service=_Skills(), policy_loader=loader
    ), client, loader


@pytest.mark.asyncio
async def test_definition_is_strict_agent_host_safe_oneof():
    definition = host_terminal_tool_definition()
    assert definition.source_type is ToolSourceType.AGENT
    assert definition.risk_level is RiskLevel.SAFE
    assert definition.toolset == "host" and definition.managed is False
    assert definition.input_schema["type"] == "object"
    assert len(definition.input_schema["oneOf"]) == 2
    assert all(branch["additionalProperties"] is False for branch in definition.input_schema["oneOf"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"target_type": "skill_script", "skill": "photo-and-upload", "script": "scripts/photo-upload.py", "args": [], "command": "/bin/echo"},
        {"target_type": "command", "command": "/bin/echo", "args": "hello"},
        {"target_type": "command", "command": "/bin/echo", "args": ["bad value"]},
        {"target_type": "skill_script", "skill": "photo-and-upload", "script": "scripts/photo-upload.py", "args": [], "timeout": 121},
    ],
)
async def test_invalid_inputs_never_call_bridge(arguments):
    executor, client, _ = _executor()
    result = await executor.execute(ToolCallRequest("c1", "host_terminal", arguments))
    assert result.status is ToolResultStatus.ERROR
    assert client.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        ["bad value"],
        ["bad#value"],
        ["a" * 65],
        ["a", "b", "c"],
        ["a" * 64, "b" * 64, "c"],
    ],
)
async def test_application_argument_admission_has_domain_parity(args):
    snapshot = _snapshot()
    assert host_terminal_arguments_allowed(args, snapshot.limits) is False
    executor, client, _ = _executor(snapshot)

    result = await executor.execute(ToolCallRequest("parity", "host_terminal", {
        "target_type": "command", "command": "/bin/echo", "args": args,
    }))

    assert result.content == {"error": "host_arguments_invalid"}
    assert client.requests == []


@pytest.mark.asyncio
async def test_hash_mismatch_denied_without_bridge_call():
    executor, client, _ = _executor(_snapshot(digest="b" * 64))
    result = await executor.execute(ToolCallRequest("c1", "host_terminal", {
        "target_type": "skill_script", "skill": "photo-and-upload",
        "script": "scripts/photo-upload.py", "args": [], "timeout": 120,
    }))
    assert result.content == {"error": "skill_script_hash_mismatch"}
    assert client.requests == []


@pytest.mark.asyncio
async def test_success_pins_policy_and_returns_only_structured_photo_result():
    executor, client, loader = _executor()
    result = await executor.execute(ToolCallRequest("c1", "host_terminal", {
        "target_type": "skill_script", "skill": "photo-and-upload",
        "script": "scripts/photo-upload.py", "args": [], "timeout": 120,
    }))
    assert result.status is ToolResultStatus.SUCCESS
    assert result.content == {
        "capture_size": 12,
        "upload_http": 200,
        "signed_url": "https://example.invalid/photo.jpg?sig=test",
    }
    assert loader.refresh_count == 1
    assert client.requests[0].n_agent_policy_version == "v1"
    assert client.requests[0].n_agent_content_digest == "a" * 64


@pytest.mark.asyncio
async def test_photo_success_replaces_signed_url_with_persistent_url():
    persister = _FakePersister(url="http://localhost:8201/chat/images/abc.jpg")
    executor = HostTerminalToolExecutor(
        client=_Client(), skill_service=_Skills(), policy_loader=_Loader(_snapshot()),
        image_persister=persister,
    )
    result = await executor.execute(ToolCallRequest("c1", "host_terminal", {
        "target_type": "skill_script", "skill": "photo-and-upload",
        "script": "scripts/photo-upload.py", "args": [], "timeout": 120,
    }))
    assert result.status is ToolResultStatus.SUCCESS
    assert result.content["signed_url"] == "http://localhost:8201/chat/images/abc.jpg"
    assert persister.calls and persister.calls[0][0] == "https://example.invalid/photo.jpg?sig=test"
    # capture_size / upload_http preserved
    assert result.content["capture_size"] == 12
    assert result.content["upload_http"] == 200


@pytest.mark.asyncio
async def test_photo_success_falls_back_to_signed_url_when_persist_returns_none():
    persister = _FakePersister(url=None)
    executor = HostTerminalToolExecutor(
        client=_Client(), skill_service=_Skills(), policy_loader=_Loader(_snapshot()),
        image_persister=persister,
    )
    result = await executor.execute(ToolCallRequest("c1", "host_terminal", {
        "target_type": "skill_script", "skill": "photo-and-upload",
        "script": "scripts/photo-upload.py", "args": [],
    }))
    assert result.status is ToolResultStatus.SUCCESS
    # falls back to the original (expiring) signed url
    assert result.content["signed_url"] == "https://example.invalid/photo.jpg?sig=test"


@pytest.mark.asyncio
async def test_photo_success_falls_back_when_persister_raises():
    persister = _FakePersister(exc=RuntimeError("boom"))
    executor = HostTerminalToolExecutor(
        client=_Client(), skill_service=_Skills(), policy_loader=_Loader(_snapshot()),
        image_persister=persister,
    )
    result = await executor.execute(ToolCallRequest("c1", "host_terminal", {
        "target_type": "skill_script", "skill": "photo-and-upload",
        "script": "scripts/photo-upload.py", "args": [],
    }))
    assert result.status is ToolResultStatus.SUCCESS
    assert result.content["signed_url"] == "https://example.invalid/photo.jpg?sig=test"


@pytest.mark.asyncio
async def test_policy_refresh_runs_off_event_loop_thread():
    loop_thread = threading.get_ident()

    class _ThreadCapturingLoader(_Loader):
        def __init__(self, snapshot):
            super().__init__(snapshot)
            self.refresh_thread = None

        def refresh(self):
            self.refresh_thread = threading.get_ident()
            time.sleep(0.02)
            return super().refresh()

    loader = _ThreadCapturingLoader(_snapshot())
    executor = HostTerminalToolExecutor(
        client=_Client(), skill_service=_Skills(), policy_loader=loader
    )
    result = await executor.execute(ToolCallRequest("off-thread", "host_terminal", {
        "target_type": "skill_script", "skill": "photo-and-upload",
        "script": "scripts/photo-upload.py", "args": [],
    }))

    assert result.status is ToolResultStatus.SUCCESS
    assert loader.refresh_thread is not None
    assert loader.refresh_thread != loop_thread


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "snapshot,arguments",
    [
        (
            _command_snapshot(),
            {"target_type": "command", "command": "/bin/echo", "args": []},
        ),
        (
            _other_skill_snapshot(),
            {
                "target_type": "skill_script",
                "skill": "other-skill",
                "script": "scripts/other.py",
                "args": [],
            },
        ),
    ],
)
async def test_non_photo_targets_never_turn_matching_output_into_capability(
    snapshot, arguments
):
    executor, _, _ = _executor(snapshot)
    result = await executor.execute(
        ToolCallRequest("c1", "host_terminal", arguments)
    )
    assert result.status is ToolResultStatus.SUCCESS
    assert result.content == {"success": True}
    assert "signed_url" not in result.content


@pytest.mark.asyncio
async def test_malformed_or_truncated_success_is_invalid_response():
    response = HostTerminalBridgeResponse(
        protocol_version="1", request_id="ignored", status=HostTerminalStatus.SUCCESS,
        exit_code=0, stdout="URL:https://example.invalid/x?sig=test\n", stderr="",
        duration_ms=1, stdout_truncated=True, stderr_truncated=False,
    )
    executor, _, _ = _executor(client=_Client(response))
    result = await executor.execute(ToolCallRequest("c1", "host_terminal", {
        "target_type": "skill_script", "skill": "photo-and-upload",
        "script": "scripts/photo-upload.py", "args": [],
    }))
    assert result.content == {"error": "host_bridge_invalid_response"}


@pytest.mark.asyncio
async def test_script_failure_surfaces_desensitized_stage_and_duration():
    response = HostTerminalBridgeResponse(
        protocol_version="1", request_id="ignored", status=HostTerminalStatus.ERROR,
        exit_code=1, stdout="", stderr="ERROR:sts_failed\n",
        duration_ms=1234, stdout_truncated=False, stderr_truncated=False,
        error_code="host_execution_failed",
    )
    executor, _, _ = _executor(client=_PinnedClient(response))
    result = await executor.execute(ToolCallRequest("c1", "host_terminal", {
        "target_type": "skill_script", "skill": "photo-and-upload",
        "script": "scripts/photo-upload.py", "args": [],
    }))
    assert result.status is ToolResultStatus.ERROR
    assert result.content == {"error": "host_execution_failed", "stage": "sts_failed"}
    assert result.duration_ms == 1234
    assert "ERROR:" not in str(result.content)


@pytest.mark.asyncio
async def test_script_failure_without_stage_code_keeps_opaque_error():
    response = HostTerminalBridgeResponse(
        protocol_version="1", request_id="ignored", status=HostTerminalStatus.ERROR,
        exit_code=1, stdout="", stderr="some unstructured output\n",
        duration_ms=10, stdout_truncated=False, stderr_truncated=False,
        error_code="host_execution_failed",
    )
    executor, _, _ = _executor(client=_PinnedClient(response))
    result = await executor.execute(ToolCallRequest("c1", "host_terminal", {
        "target_type": "skill_script", "skill": "photo-and-upload",
        "script": "scripts/photo-upload.py", "args": [],
    }))
    assert result.status is ToolResultStatus.ERROR
    assert result.content == {"error": "host_execution_failed"}


@pytest.mark.asyncio
async def test_signed_url_is_preserved_only_for_scoped_success_result():
    class _ResultExecutor:
        async def execute(self, request, context=None):
            from app.domain.tool import ToolResult
            return ToolResult(request.id, request.name, ToolResultStatus.SUCCESS, {
                "capture_size": 12,
                "upload_http": 200,
                "signed_url": "https://example.invalid/x?sig=secret-value",
            })

    class _Release:
        def release(self, content, *args, **kwargs):
            from types import SimpleNamespace
            return SimpleNamespace(allowed=True, content=content)

        def redact_structured(self, value):
            return {**value, "signed_url": "[REDACTED]"}

    service = ToolService(
        _ResultExecutor(),
        [host_terminal_tool_definition()],
        information_flow_service=_Release(),
    )
    result = await service.execute(ToolCallRequest("c1", "host_terminal", {
        "target_type": "skill_script", "skill": "photo-and-upload",
        "script": "scripts/photo-upload.py", "args": [],
    }))
    assert result.content["signed_url"] == "https://example.invalid/x?sig=secret-value"

    for arguments in (
        {"target_type": "command", "command": "/bin/echo", "args": []},
        {
            "target_type": "skill_script",
            "skill": "other-skill",
            "script": "scripts/other.py",
            "args": [],
        },
    ):
        denied_capability = await service.execute(
            ToolCallRequest("negative", "host_terminal", arguments)
        )
        assert denied_capability.content["signed_url"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_photo_target_does_not_restore_url_for_non_strict_result_shape():
    class _InvalidResultExecutor:
        async def execute(self, request, context=None):
            from app.domain.tool import ToolResult
            return ToolResult(request.id, request.name, ToolResultStatus.SUCCESS, {
                "capture_size": 0,
                "upload_http": 200,
                "signed_url": "https://example.invalid/x?sig=secret-value",
            })

    class _Release:
        def release(self, content, *args, **kwargs):
            from types import SimpleNamespace
            return SimpleNamespace(allowed=True, content=content)

        def redact_structured(self, value):
            return {**value, "signed_url": "[REDACTED]"}

    service = ToolService(
        _InvalidResultExecutor(),
        [host_terminal_tool_definition()],
        information_flow_service=_Release(),
    )
    result = await service.execute(ToolCallRequest("c1", "host_terminal", {
        "target_type": "skill_script", "skill": "photo-and-upload",
        "script": "scripts/photo-upload.py", "args": [],
    }))
    assert result.content["signed_url"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_policy_120_tool_60_request_90_preserves_scoped_url_after_capping():
    class _Release:
        def release(self, content, *args, **kwargs):
            from types import SimpleNamespace
            return SimpleNamespace(allowed=True, content=content)

        def redact_structured(self, value):
            return {**value, "signed_url": "[REDACTED]"}

    client = _Client()
    executor = HostTerminalToolExecutor(
        client=client,
        skill_service=_Skills(),
        policy_loader=_Loader(_snapshot()),
        tool_timeout_seconds=60,
    )
    service = ToolService(
        executor,
        [host_terminal_tool_definition(60)],
        information_flow_service=_Release(),
    )
    result = await service.execute(ToolCallRequest("c1", "host_terminal", {
        "target_type": "skill_script", "skill": "photo-and-upload",
        "script": "scripts/photo-upload.py", "args": [], "timeout": 90,
    }))
    assert client.requests[0].limits.timeout_seconds == 60
    assert result.content["signed_url"] == "https://example.invalid/photo.jpg?sig=test"


class _AuditSink:
    def __init__(self):
        self.events = []

    async def record(self, event):
        self.events.append(event)


class _FailingSkills:
    def __init__(self, error):
        self.error = error

    async def resolve_script_bytes(self, skill, script):
        raise self.error


async def _audited_executor(*, snapshot=None, skills=None, client=None):
    sink = _AuditSink()
    loader = _Loader(snapshot)
    executor = HostTerminalToolExecutor(
        client=client or _Client(),
        skill_service=skills or _Skills(),
        policy_loader=loader,
        audit_service=PolicyAuditService(sink),
    )
    return executor, sink


@pytest.mark.asyncio
async def test_every_early_host_failure_is_audited_without_sensitive_payloads():
    attempts = []

    executor, sink = await _audited_executor(snapshot=_snapshot())
    await executor.execute(ToolCallRequest(
        "unsupported", "other_route", {"args": ["TOPSECRET"], "token": "TOKENVALUE"}
    ))
    attempts.extend(sink.events)

    executor, sink = await _audited_executor(snapshot=None)
    await executor.execute(ToolCallRequest(
        "no-policy", "host_terminal", {"target_type": "command", "command": "/bin/echo", "args": ["TOPSECRET"]}
    ))
    assert sink.events[0].version == "unavailable"
    attempts.extend(sink.events)

    executor, sink = await _audited_executor(snapshot=_snapshot())
    await executor.execute(ToolCallRequest(
        "bad-target", "host_terminal", {"target_type": "command", "command": "relative", "args": ["TOPSECRET"]}
    ))
    attempts.extend(sink.events)

    for error, expected in (
        (SkillNotFoundError("missing"), "skill_script_not_found"),
        (SkillValidationError("denied"), "skill_script_path_denied"),
    ):
        executor, sink = await _audited_executor(
            snapshot=_snapshot(), skills=_FailingSkills(error)
        )
        await executor.execute(ToolCallRequest(
            expected, "host_terminal", {
                "target_type": "skill_script", "skill": "photo-and-upload",
                "script": "scripts/photo-upload.py", "args": [],
            }
        ))
        assert expected in sink.events[0].reason
        attempts.extend(sink.events)

    reasons = "\n".join(event.reason for event in attempts)
    for expected in (
        "host_terminal_unsupported_route",
        "host_policy_unavailable",
        "host_request_invalid",
        "skill_script_not_found",
        "skill_script_path_denied",
    ):
        assert expected in reasons
    for secret in ("TOPSECRET", "TOKENVALUE", "CAPTURED:", "URL:", "sig="):
        assert secret not in reasons


@pytest.mark.asyncio
async def test_unresolved_skill_audit_never_contains_raw_skill_or_script_identity():
    sensitive_skill = "private-customer-camera"
    sensitive_script = "scripts/private-account-upload.py"
    executor, sink = await _audited_executor(
        snapshot=_snapshot(), skills=_FailingSkills(SkillNotFoundError("missing"))
    )

    await executor.execute(ToolCallRequest(
        "unresolved-sensitive", "host_terminal", {
            "target_type": "skill_script",
            "skill": sensitive_skill,
            "script": sensitive_script,
            "args": [],
        }
    ))

    detail = json.loads(sink.events[0].reason)
    assert detail["target"] == "unresolved_skill_script"
    assert sensitive_skill not in sink.events[0].reason
    assert sensitive_script not in sink.events[0].reason

    executor, sink = await _audited_executor(snapshot=_snapshot(), skills=_Skills())
    await executor.execute(ToolCallRequest(
        "policy-mismatch-sensitive", "host_terminal", {
            "target_type": "skill_script",
            "skill": sensitive_skill,
            "script": sensitive_script,
            "args": [],
        }
    ))
    detail = json.loads(sink.events[0].reason)
    assert detail["target"] == "unresolved_skill_script"
    assert sensitive_skill not in sink.events[0].reason
    assert sensitive_script not in sink.events[0].reason


@pytest.mark.asyncio
async def test_cancellation_is_shield_audited_then_reraised_without_leakage():
    class _BlockingClient:
        async def execute(self, request):
            await asyncio.Event().wait()

    executor, sink = await _audited_executor(
        snapshot=_snapshot(), client=_BlockingClient()
    )
    task = asyncio.create_task(executor.execute(
        ToolCallRequest("cancel", "host_terminal", {
            "target_type": "skill_script", "skill": "photo-and-upload",
            "script": "scripts/photo-upload.py", "args": [],
        }),
        ToolExecutionContext(run_id="run-1", session_id="session-1"),
    ))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(sink.events) == 1
    event = sink.events[0]
    assert "host_execution_cancelled" in event.reason
    assert event.run_id == "run-1" and event.session_id == "session-1"
    assert "URL:" not in event.reason and "TOKEN" not in event.reason


@pytest.mark.asyncio
async def test_cancellation_during_policy_refresh_is_prompt_and_audited():
    class _SlowLoader(_Loader):
        def refresh(self):
            time.sleep(0.25)
            return super().refresh()

    sink = _AuditSink()
    loader = _SlowLoader(_snapshot())
    executor = HostTerminalToolExecutor(
        client=_Client(),
        skill_service=_Skills(),
        policy_loader=loader,
        audit_service=PolicyAuditService(sink),
    )
    task = asyncio.create_task(executor.execute(
        ToolCallRequest("cancel-refresh", "host_terminal", {
            "target_type": "skill_script", "skill": "photo-and-upload",
            "script": "scripts/photo-upload.py", "args": [],
        }),
        ToolExecutionContext(run_id="run-refresh", session_id="session-refresh"),
    ))
    await asyncio.sleep(0.01)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert time.monotonic() - started < 0.1
    assert len(sink.events) == 1
    detail = json.loads(sink.events[0].reason)
    assert detail["reason"] == "host_execution_cancelled"
    assert detail["tool_call"] == "cancel-refresh"


@pytest.mark.asyncio
async def test_cancelled_to_thread_refresh_can_publish_valid_last_good_snapshot_safely():
    started = threading.Event()
    release = threading.Event()
    replacement = replace(_snapshot(), version="v2")

    class _PublishingLoader(_Loader):
        def refresh(self):
            started.set()
            assert release.wait(timeout=2)
            self._snapshot = replacement
            return True

    loader = _PublishingLoader(_snapshot())
    executor = HostTerminalToolExecutor(
        client=_Client(),
        skill_service=_Skills(),
        policy_loader=loader,
    )
    task = asyncio.create_task(executor.execute(ToolCallRequest(
        "cancel-publish", "host_terminal", {
            "target_type": "command", "command": "/bin/echo", "args": ["hello"]
        }
    )))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    deadline = time.monotonic() + 2
    while loader.snapshot is not replacement and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert loader.snapshot is replacement


@pytest.mark.asyncio
async def test_audit_detail_is_compact_json_and_control_characters_cannot_inject_fields():
    sink = _AuditSink()
    executor = HostTerminalToolExecutor(
        client=_Client(),
        skill_service=_Skills(),
        policy_loader=_Loader(None),
        audit_service=PolicyAuditService(sink),
    )
    injected_id = 'call"\n,"rule":"injected'
    await executor.execute(ToolCallRequest(
        injected_id,
        "host_terminal",
        {"target_type": "command", "command": "/bin/echo", "args": []},
    ))

    encoded = sink.events[0].reason
    detail = json.loads(encoded)
    assert "\n" not in encoded and "\r" not in encoded
    assert detail["reason"] == "host_policy_unavailable"
    assert detail["tool_call"] == injected_id
    assert detail["rule"] is None
    assert set(detail) == {
        "reason", "tool_call", "target_type", "target", "request_sha256",
        "rule", "exit", "duration_ms", "timeout", "truncated",
    }
    for forbidden in ("args", "stdout", "stderr", "url"):
        assert forbidden not in detail

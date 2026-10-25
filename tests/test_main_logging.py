"""Verify main.py configures HTTP logging without polluting CLI output.

uvicorn 0.30+ 默认 LOGGING_CONFIG 不再为 root logger 配置 handler/level,
导致应用层 logger.info() 静默 (仅 uvicorn.access logger 单独配置仍可见)。
create_app() 调用 _configure_logging() 修复 HTTP 日志，同时模块 import 保持无副作用。

T6 (plan-261024-otel-observability) 扩展: 应用自有 stderr handler 使用安全
JSON formatter (白名单字段, 事件码代替消息正文), observability 启用且存在
LoggerProvider 时 root 恰好挂一个投影副本 OTel bridge。
"""
from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
from types import SimpleNamespace

from opentelemetry import trace as otel_trace


def test_importing_main_does_not_configure_cli_root_logger():
    """CLI imports app.main for service wiring without opting into INFO logs."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, logging; "
                "from app.interfaces.cli.main import _configure_cli_env; "
                "_configure_cli_env(); "
                "import app.main; "
                "root = logging.getLogger(); "
                "print(json.dumps({'handlers': len(root.handlers), 'level': root.level}))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    state = json.loads(result.stdout)
    assert state == {"handlers": 0, "level": logging.WARNING}
    assert result.stderr == ""


def _reset_root() -> tuple[list[logging.Handler], int]:
    """Snapshot root logger state and reset to unconfigured (simulating
    uvicorn 0.30+ default where root logger has no handler)."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    root.handlers.clear()
    root.setLevel(logging.NOTSET)
    return saved_handlers, saved_level


def _restore_root(saved_handlers: list[logging.Handler], saved_level: int) -> None:
    root = logging.getLogger()
    root.handlers = saved_handlers
    root.setLevel(saved_level)


def test_configure_logging_sets_root_handler_and_level():
    """_configure_logging should add handler and set INFO level on root logger
    when root logger is unconfigured (simulating uvicorn 0.30+ default)."""
    from app.main import _configure_logging
    saved_handlers, saved_level = _reset_root()
    try:
        _configure_logging()
        root = logging.getLogger()
        assert root.handlers, "root logger should have at least one handler"
        assert root.level <= logging.INFO, "root logger level should be INFO or lower"
    finally:
        _restore_root(saved_handlers, saved_level)


def test_configure_logging_idempotent_when_handlers_exist():
    """_configure_logging should not override existing handler configuration
    (e.g., when pytest or older uvicorn has already configured root logger)."""
    from app.main import _configure_logging
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        preexisting = logging.StreamHandler()
        root.handlers = [preexisting]
        root.setLevel(logging.DEBUG)
        _configure_logging()
        assert root.handlers == [preexisting], "existing handler should not be replaced"
        assert root.level == logging.DEBUG, "existing level should not be changed"
    finally:
        _restore_root(saved_handlers, saved_level)


def test_app_logger_info_visible_after_configure():
    """After _configure_logging, app logger.info() should reach a handler.

    Reproduces the production scenario: uvicorn 0.30+ leaves root logger
    unconfigured, main.py imports and calls _configure_logging(), then
    AgentGraphRunner.call_llm logs "API call ..." which must be visible.
    """
    from app.main import _configure_logging
    saved_handlers, saved_level = _reset_root()
    stream = io.StringIO()
    try:
        _configure_logging()
        capture = logging.StreamHandler(stream)
        root = logging.getLogger()
        root.addHandler(capture)
        app_logger = logging.getLogger("app.application.agent_graph")
        app_logger.info(
            "API call model=test provider=test in=1 out=1 total=2 latency=10ms"
        )
        assert "API call model=test" in stream.getvalue(), (
            "app logger.info() should be visible after _configure_logging"
        )
    finally:
        _restore_root(saved_handlers, saved_level)


# ---------------------------------------------------------------------------
# T6: JSON stderr formatter + safe projection + OTel log bridge
# ---------------------------------------------------------------------------

_JSON_FIELDS = (
    "timestamp",
    "severity",
    "logger",
    "event",
    "trace_id",
    "span_id",
    "session_id",
)


class _CapturingOtelHandler(logging.Handler):
    """Stands in for the SDK LoggingHandler; captures projected records."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _BoomOtelHandler(logging.Handler):
    """Always fails, to prove the bridge swallows handler failures."""

    def __init__(self):
        super().__init__()
        self.calls = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.calls += 1
        raise RuntimeError("otel handler exploded")


class _FakeSpanWithAttributes(otel_trace.NonRecordingSpan):
    """Minimal recording-span stand-in for current-span context reads.

    Subclasses NonRecordingSpan so get_current_span's isinstance check
    accepts it; only the trusted conversation attribute is exercised."""

    def __init__(self, span_context, attributes):
        super().__init__(span_context)
        self.attributes = attributes


def _reset_event_registry():
    from app.infrastructure.observability.logging import (
        _reset_event_registry_for_tests,
    )

    _reset_event_registry_for_tests()


def _json_stderr_handler(root: logging.Logger) -> logging.Handler:
    from app.infrastructure.observability.logging import SafeEventJsonFormatter

    matches = [
        h
        for h in root.handlers
        if isinstance(getattr(h, "formatter", None), SafeEventJsonFormatter)
    ]
    assert len(matches) == 1, "exactly one JSON stderr handler expected"
    return matches[0]


def _capture_json_line(root: logging.Logger) -> tuple[logging.Handler, io.StringIO]:
    handler = _json_stderr_handler(root)
    stream = io.StringIO()
    handler.stream = stream
    return handler, stream


def test_json_stderr_line_fields_without_active_span():
    from app.main import _configure_logging

    saved_handlers, saved_level = _reset_root()
    try:
        _configure_logging()
        root = logging.getLogger()
        _, stream = _capture_json_line(root)
        logging.getLogger("app.test.json").warning(
            "request token=SECRET-PROMPT password=%s", "SECRET-ARG"
        )
        payload = json.loads(stream.getvalue().strip())
        for key in _JSON_FIELDS:
            assert key in payload, f"missing JSON field: {key}"
        # No active span/session: correlation fields are null, never fabricated.
        assert payload["trace_id"] is None
        assert payload["span_id"] is None
        assert payload["session_id"] is None
        assert payload["severity"] == "WARNING"
        assert payload["logger"] == "app.test.json"
        # Free text never reaches the JSON line.
        assert "SECRET-PROMPT" not in stream.getvalue()
        assert "SECRET-ARG" not in stream.getvalue()
    finally:
        _restore_root(saved_handlers, saved_level)
        _reset_event_registry()


def test_json_trace_and_span_ids_from_active_span():
    from opentelemetry import context as otel_context
    from opentelemetry import trace as otel_trace

    from app.main import _configure_logging

    saved_handlers, saved_level = _reset_root()
    trace_id = 0x1234567890ABCDEF1234567890ABCDEF
    span_id = 0x248163264128256
    span_context = otel_trace.SpanContext(
        trace_id=trace_id, span_id=span_id, is_remote=False
    )
    try:
        _configure_logging()
        root = logging.getLogger()
        _, stream = _capture_json_line(root)
        current = otel_context.get_current()
        token = otel_context.attach(
            otel_trace.set_span_in_context(
                otel_trace.NonRecordingSpan(span_context), current
            )
        )
        try:
            logging.getLogger("app.test.span").warning("hello")
        finally:
            otel_context.detach(token)
        payload = json.loads(stream.getvalue().strip())
        assert payload["trace_id"] == format(trace_id, "032x")
        assert payload["span_id"] == format(span_id, "016x")
    finally:
        _restore_root(saved_handlers, saved_level)
        _reset_event_registry()


def test_json_unknown_event_outputs_only_fixed_code():
    from app.infrastructure.observability.logging import UNKNOWN_EVENT_CODE
    from app.main import _configure_logging

    saved_handlers, saved_level = _reset_root()
    try:
        _configure_logging()
        root = logging.getLogger()
        _, stream = _capture_json_line(root)
        logging.getLogger("app.test.unknown").warning(
            "free text UNKNOW-FREE-TEXT with args=%s", "UNKNOW-ARG"
        )
        payload = json.loads(stream.getvalue().strip())
        assert payload["event"] == "application_log"
        assert "UNKNOW-FREE-TEXT" in payload["message"]
        assert "UNKNOW-ARG" in payload["message"]
    finally:
        _restore_root(saved_handlers, saved_level)
        _reset_event_registry()


def test_owned_stderr_drops_contextual_unknown_info_but_keeps_warning(monkeypatch):
    from app.infrastructure.observability import logging as obs
    from app.main import _configure_logging

    monkeypatch.setattr(obs, "_execution_context", lambda: {"operation": "chat", "model": "test"})
    saved_handlers, saved_level = _reset_root()
    try:
        _configure_logging()
        _, stream = _capture_json_line(logging.getLogger())
        logger = logging.getLogger("httpx")
        logger.info("unregistered HTTP request")
        assert stream.getvalue() == ""
        logger.warning("unregistered failure")
        payload = json.loads(stream.getvalue())
        assert payload["event"] == "diagnostic_log"
        assert payload["logger"] == "httpx"
        assert payload["severity"] == "WARNING"
    finally:
        _restore_root(saved_handlers, saved_level)
        _reset_event_registry()


def test_json_registered_event_outputs_its_event_code():
    from app.infrastructure.observability.logging import (
        prepare_sanitizer,
        register_safe_event,
    )
    from app.main import _configure_logging

    saved_handlers, saved_level = _reset_root()
    prepare_sanitizer(None)  # collapse contract holds only with no installed sanitizer
    try:
        register_safe_event(
            "API call: model=%s provider=%s", "llm_api_call"
        )
        _configure_logging()
        root = logging.getLogger()
        _, stream = _capture_json_line(root)
        logging.getLogger("app.test.registered").info(
            "API call: model=%s provider=%s", "SECRET-MODEL", "SECRET-PROVIDER"
        )
        payload = json.loads(stream.getvalue().strip())
        assert payload["event"] == "llm_api_call"
        # Model and provider names are useful values, not credentials.
        assert "SECRET-MODEL" in payload["message"]
        assert "SECRET-PROVIDER" in payload["message"]
    finally:
        _restore_root(saved_handlers, saved_level)
        _reset_event_registry()


def test_genai_sensitive_content_never_reaches_json_or_bridge():
    from app.infrastructure.observability.logging import SafeLogBridge, LogSanitizer, prepare_sanitizer
    from app.main import _configure_logging

    saved_handlers, saved_level = _reset_root()
    capture = _CapturingOtelHandler()
    try:
        _configure_logging()
        sanitizer = LogSanitizer()
        sanitizer.configure_secret_catalog({"PROMPT-SENTINEL", "TOOLARG-SENTINEL", "RESULT-SENTINEL",
                                            "MESSAGE-BODY-SENTINEL", "SYSTEM-INSTRUCTION-SENTINEL",
                                            "EXC-SENTINEL", "EXCBODY-SENT"})
        prepare_sanitizer(sanitizer)
        root = logging.getLogger()
        _, stream = _capture_json_line(root)
        bridge = SafeLogBridge(otel_handler=capture)
        root.addHandler(bridge)
        logger = logging.getLogger("app.test.sensitive")
        logger.info(
            "prompt PROMPT-SENTINEL tool args TOOLARG-SENTINEL result RESULT-SENTINEL"
        )
        logger.info(
            "messages %s system %s",
            "MESSAGE-BODY-SENTINEL",
            "SYSTEM-INSTRUCTION-SENTINEL",
        )
        logger.error("handler failed EXC-SENTINEL", exc_info=ValueError("EXCBODY-SENT"))
        json_and_bridge = stream.getvalue() + "\n" + "\n".join(
            record.getMessage() for record in capture.records
        )
        for sentinel in (
            "PROMPT-SENTINEL",
            "TOOLARG-SENTINEL",
            "RESULT-SENTINEL",
            "MESSAGE-BODY-SENTINEL",
            "SYSTEM-INSTRUCTION-SENTINEL",
            "EXC-SENTINEL",
            "EXCBODY-SENT",
        ):
            assert sentinel not in json_and_bridge, f"leaked: {sentinel}"
        # Projection copies carry no exception payload.
        assert capture.records, "bridge should have received projected copies"
        assert all(record.exc_info is None for record in capture.records)
        assert all(record.args in ((), None) for record in capture.records)
    finally:
        _restore_root(saved_handlers, saved_level)
        _reset_event_registry()


def test_bridge_projection_copy_leaves_original_record_untouched():
    from app.infrastructure.observability.logging import (
        UNKNOWN_EVENT_CODE,
        SafeLogBridge,
    )

    capture = _CapturingOtelHandler()
    bridge = SafeLogBridge(otel_handler=capture)
    original = logging.LogRecord(
        "app.test.orig", logging.WARNING, __file__, 10, "hello %s", ("WORLD",), None
    )
    bridge.handle(original)
    assert original.msg == "hello %s"
    assert original.args == ("WORLD",)
    assert original.getMessage() == "hello WORLD"
    assert original.exc_info is None
    assert capture.records, "bridge should emit the projected copy"
    projected = capture.records[0]
    assert projected is not original
    assert projected.msg == "hello WORLD"
    assert projected.args in ((), None)
    assert projected.getMessage() == "hello WORLD"


def test_existing_external_handler_format_not_overwritten():
    from app.main import _configure_logging

    saved_handlers, saved_level = _reset_root()
    try:
        ext_stream = io.StringIO()
        external = logging.StreamHandler(ext_stream)
        external.setFormatter(logging.Formatter("EXT %(levelname)s %(message)s"))
        root = logging.getLogger()
        root.handlers = [external]
        root.setLevel(logging.INFO)
        _configure_logging()
        logging.getLogger("app.test.external").info("world %s", "EXTARG")
        # The external handler keeps its own format and receives the original
        # (unprojected) record fields.
        assert "EXT INFO world EXTARG" in ext_stream.getvalue()
        assert external in root.handlers
    finally:
        _restore_root(saved_handlers, saved_level)
        _reset_event_registry()


def test_importing_observability_logging_has_no_side_effects():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, logging; "
                "import app.infrastructure.observability.logging; "
                "root = logging.getLogger(); "
                "print(json.dumps({'handlers': len(root.handlers), 'level': root.level}))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    state = json.loads(result.stdout)
    assert state == {"handlers": 0, "level": logging.WARNING}
    assert result.stderr == ""


def test_disabled_state_adds_no_otel_bridge():
    from app.infrastructure.observability.logging import (
        SafeLogBridge,
        attach_observability_log_bridge,
    )
    from app.main import _configure_logging

    saved_handlers, saved_level = _reset_root()
    try:
        _configure_logging()
        root = logging.getLogger()
        bridges = [h for h in root.handlers if isinstance(h, SafeLogBridge)]
        assert bridges == [], "disabled state must not attach an OTel bridge"
        assert attach_observability_log_bridge() is False
    finally:
        _restore_root(saved_handlers, saved_level)
        _reset_event_registry()


def test_attach_bridge_enabled_is_idempotent_single_handler():
    from app.infrastructure.observability.logging import (
        SafeLogBridge,
        attach_observability_log_bridge,
    )

    saved_handlers, saved_level = _reset_root()
    capture = _CapturingOtelHandler()
    bridge = SafeLogBridge(otel_handler=capture)
    try:
        assert attach_observability_log_bridge(bridge=bridge) is True
        # Second call (as create_app does after service assembly) must not
        # attach a second bridge.
        assert (
            attach_observability_log_bridge(
                bridge=SafeLogBridge(otel_handler=_CapturingOtelHandler())
            )
            is True
        )
        root = logging.getLogger()
        bridges = [h for h in root.handlers if isinstance(h, SafeLogBridge)]
        assert bridges == [bridge]
        logging.getLogger("app.test.bridge").warning("hi %s", "BRIDGEARG")
        assert capture.records, "bridge must receive projected records"
        assert capture.records[0].msg != "hi %s"
    finally:
        root = logging.getLogger()
        for handler in [h for h in root.handlers if isinstance(h, SafeLogBridge)]:
            root.removeHandler(handler)
        _restore_root(saved_handlers, saved_level)
        _reset_event_registry()


def test_bridge_excludes_self_traffic_no_recursion():
    from app.infrastructure.observability.logging import SafeLogBridge

    capture = _CapturingOtelHandler()
    bridge = SafeLogBridge(otel_handler=capture)
    app_record = logging.LogRecord(
        "app.test.ok", logging.WARNING, __file__, 10, "fine", None, None
    )
    self_record = logging.LogRecord(
        "opentelemetry.exporter.otlp.proto.grpc._log_exporter",
        logging.WARNING,
        __file__,
        10,
        "export retry",
        None,
        None,
    )
    grpc_record = logging.LogRecord(
        "grpc._cython", logging.ERROR, __file__, 10, "channel failed", None, None
    )
    bridge.handle(self_record)
    bridge.handle(grpc_record)
    assert capture.records == [], "self traffic must not re-enter the bridge"
    bridge.handle(app_record)
    assert len(capture.records) == 1
    assert capture.records[0].name == "app.test.ok"


def test_bridge_swallows_inner_handler_failures_and_counts():
    from app.infrastructure.observability.logging import SafeLogBridge

    boom = _BoomOtelHandler()
    bridge = SafeLogBridge(otel_handler=boom)
    record = logging.LogRecord(
        "app.test.boom", logging.WARNING, __file__, 10, "hello", None, None
    )
    bridge.handle(record)  # must not raise
    assert boom.calls == 1
    assert bridge.emit_failure_count == 1


def test_session_id_only_from_trusted_server_context():
    from app.infrastructure.observability.logging import bound_log_session
    from app.main import _configure_logging

    saved_handlers, saved_level = _reset_root()
    try:
        _configure_logging()
        root = logging.getLogger()
        _, stream = _capture_json_line(root)
        logger = logging.getLogger("app.test.session")
        # Session-like data in client-controlled log arguments is ignored.
        logger.warning("session=%s", "CLIENT-SESSION-SPOOF")
        payload = json.loads(stream.getvalue().strip().splitlines()[-1])
        assert payload["session_id"] is None
        assert "CLIENT-SESSION-SPOOF" in payload["message"]
        # Explicit trusted binding (server-side AgentState domain) is used,
        # via the context-managed wrapper (always reset on exit).
        with bound_log_session("sess-trusted-1"):
            logger.warning("another")
            payload = json.loads(stream.getvalue().strip().splitlines()[-1])
            assert payload["session_id"] == "sess-trusted-1"
        logger.warning("after reset")
        payload = json.loads(stream.getvalue().strip().splitlines()[-1])
        assert payload["session_id"] is None
    finally:
        _restore_root(saved_handlers, saved_level)
        _reset_event_registry()


def test_session_id_falls_back_to_span_conversation_attribute():
    from opentelemetry import context as otel_context
    from opentelemetry import trace as otel_trace

    from app.main import _configure_logging

    saved_handlers, saved_level = _reset_root()
    span_context = otel_trace.SpanContext(
        trace_id=0xABCDEF1234567890ABCDEF1234567890,
        span_id=0x1122334455667788,
        is_remote=False,
    )
    fake_span = _FakeSpanWithAttributes(
        span_context, {"gen_ai.conversation.id": "sess-from-agent-state"}
    )
    try:
        _configure_logging()
        root = logging.getLogger()
        _, stream = _capture_json_line(root)
        current = otel_context.get_current()
        token = otel_context.attach(
            otel_trace.set_span_in_context(fake_span, current)
        )
        try:
            logging.getLogger("app.test.span_session").warning("hello")
        finally:
            otel_context.detach(token)
        payload = json.loads(stream.getvalue().strip())
        assert payload["session_id"] == "sess-from-agent-state"
    finally:
        _restore_root(saved_handlers, saved_level)
        _reset_event_registry()


def test_attach_local_only_mode_without_logger_provider_returns_false(monkeypatch):
    import app.infrastructure.observability.setup as obs_setup
    from app.infrastructure.observability.logging import (
        SafeLogBridge,
        attach_observability_log_bridge,
    )

    fake_handle = SimpleNamespace(mode="local_only", instance_id="inst-1")
    monkeypatch.setattr(obs_setup, "get_observability_handle", lambda: fake_handle)
    saved_handlers, saved_level = _reset_root()
    try:
        # The real global LoggerProvider is the unclaimed proxy in this
        # process (local-only mode never publishes one), so attaching must
        # be refused even though the handle mode is enabled.
        assert attach_observability_log_bridge() is False
        assert not any(
            isinstance(h, SafeLogBridge) for h in logging.getLogger().handlers
        )
    finally:
        _restore_root(saved_handlers, saved_level)
        _reset_event_registry()


def _patch_runtime_dependencies(monkeypatch, attach_calls: list[int]):
    """Stub the heavy collaborators of application_runtime for gate tests."""
    import app.infrastructure.observability.logging as obs_logging
    import app.infrastructure.observability.setup as obs_setup
    import app.main as main_module

    fake_handle = SimpleNamespace(mode="otlp", instance_id="inst-1", shutdown=lambda: None)

    def fake_attach(**_kwargs):
        attach_calls.append(1)
        return True

    monkeypatch.setattr(obs_setup, "configure_observability", lambda settings: fake_handle)
    monkeypatch.setattr(
        main_module, "build_application_services", lambda settings=None, **kw: object()
    )
    monkeypatch.setattr(obs_logging, "attach_observability_log_bridge", fake_attach)


def test_application_runtime_attaches_bridge_when_root_configured(monkeypatch):
    """ACP path: it installs its own stderr handler before entering
    application_runtime, so the runtime attaches the bridge exactly once."""
    from app.main import application_runtime

    attach_calls: list[int] = []
    _patch_runtime_dependencies(monkeypatch, attach_calls)
    saved_handlers, saved_level = _reset_root()
    try:
        root = logging.getLogger()
        root.addHandler(logging.StreamHandler(sys.stderr))  # ACP-like setup
        root.setLevel(logging.INFO)
        with application_runtime():
            pass
        assert len(attach_calls) == 1, "ACP path must attach the bridge once"
    finally:
        _restore_root(saved_handlers, saved_level)
        _reset_event_registry()


def test_application_runtime_bare_cli_path_preserves_root_lastresort(monkeypatch):
    """Execution summaries can export without suppressing root lastResort."""
    from app.main import application_runtime

    attach_calls: list[int] = []
    _patch_runtime_dependencies(monkeypatch, attach_calls)
    saved_handlers, saved_level = _reset_root()
    try:
        assert logging.getLogger().handlers == []
        with application_runtime():
            pass
        assert len(attach_calls) == 2
        assert logging.getLogger().handlers == []
    finally:
        _restore_root(saved_handlers, saved_level)
        _reset_event_registry()


def test_application_runtime_double_entry_attach_is_idempotent(monkeypatch):
    """Two sequential runtime entries (e.g. ACP reconnect) still leave
    exactly one bridge: the MARKER check-then-set is atomic under the
    registry lock, so the second attach no-ops."""
    import opentelemetry._logs as otel_logs

    import app.infrastructure.observability.logging as obs_logging
    import app.infrastructure.observability.setup as obs_setup
    import app.main as main_module
    from app.main import application_runtime

    fake_handle = SimpleNamespace(mode="otlp", instance_id="inst-1", shutdown=lambda: None)
    monkeypatch.setattr(obs_setup, "configure_observability", lambda settings: fake_handle)
    monkeypatch.setattr(
        main_module, "build_application_services", lambda settings=None, **kw: object()
    )
    monkeypatch.setattr(obs_setup, "get_observability_handle", lambda: fake_handle)
    # A non-proxy stand-in so the real attach gating passes in-process.
    monkeypatch.setattr(otel_logs, "get_logger_provider", lambda: SimpleNamespace())
    saved_handlers, saved_level = _reset_root()
    try:
        root = logging.getLogger()
        root.addHandler(logging.StreamHandler(sys.stderr))
        root.setLevel(logging.INFO)
        with application_runtime():
            pass
        with application_runtime():
            pass
        bridges = [h for h in root.handlers if isinstance(h, obs_logging.SafeLogBridge)]
        assert len(bridges) == 1, "double entry must leave exactly one bridge"
    finally:
        root = logging.getLogger()
        for handler in [
            h for h in root.handlers if isinstance(h, obs_logging.SafeLogBridge)
        ]:
            root.removeHandler(handler)
        _restore_root(saved_handlers, saved_level)
        _reset_event_registry()


def test_seed_templates_match_call_site_constants():
    """Registered seed templates are the exact constants used by their call
    sites (usage_service / main lifespan), so drift is impossible."""
    from app.application.usage_service import API_CALL_LOG_TEMPLATE
    from app.infrastructure.observability.logging import (
        UNKNOWN_EVENT_CODE,
        event_code_for,
    )
    import app.main as main_module
    from app.main import _configure_logging

    saved_handlers, saved_level = _reset_root()
    try:
        # Pre-configure root so _configure_logging only registers events.
        root = logging.getLogger()
        root.handlers = [logging.StreamHandler(io.StringIO())]
        root.setLevel(logging.INFO)
        _configure_logging()
        cases = (
            (API_CALL_LOG_TEMPLATE, "llm_api_call"),
            (main_module._SKILL_SCAN_OK_LOG_TEMPLATE, "skill_scan_ok"),
            (main_module._PLUGIN_SCAN_OK_LOG_TEMPLATE, "plugin_scan_ok"),
        )
        for template, expected_code in cases:
            record = logging.LogRecord(
                "app.test.drift", logging.INFO, __file__, 1, template, (), None
            )
            assert event_code_for(record) == expected_code, template
        # Any other template stays unknown.
        drift_record = logging.LogRecord(
            "app.test.drift", logging.INFO, __file__, 1, "drifted template", (), None
        )
        assert event_code_for(drift_record) == UNKNOWN_EVENT_CODE
    finally:
        _restore_root(saved_handlers, saved_level)
        _reset_event_registry()


# ---------------------------------------------------------------------------
# T5 (spec-261027): SafeLogBridge 把脱敏正文/异常/现场附着到当前 span event
# ---------------------------------------------------------------------------


def test_bridge_attaches_sanitized_event_to_current_span():
    from opentelemetry.sdk import trace as sdk_trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from app.infrastructure.observability import logging as obs

    secret = "E2EOTELVERIFY-7f3a-secret"
    sanitizer = obs.LogSanitizer()
    sanitizer.configure_secret_catalog({secret})
    obs.prepare_sanitizer(sanitizer)
    obs.register_safe_event("sp %s", "sp_probe")
    from opentelemetry import trace as otel_trace

    exporter = InMemorySpanExporter()
    provider = sdk_trace.TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        # A real recording span from the provider instance's own tracer; no
        # global tracer-provider mutation required.
        tracer = provider.get_tracer("bridge-t5-test")
        span = tracer.start_span("run")
        tok = otel_trace.use_span(span, end_on_exit=False)
        tok.__enter__()
        try:
            rec = logging.LogRecord(
                "app.x", logging.INFO, __file__, 1, "sp %s", (secret,), None
            )
            dummy = type("_DummyHandler", (), {"handle": lambda self, r: None})()
            obs.SafeLogBridge(otel_handler=dummy).add_event_on_span(
                obs._prepare_record(rec), rec
            )
        finally:
            tok.__exit__(None, None, None)
            span.end()
        finished = exporter.get_finished_spans()
        assert len(finished) == 1
        events = list(finished[0].events)
        assert len(events) == 1
        assert events[0].name == "sp_probe"
        assert events[0].attributes["message"] == "sp [REDACTED]"
        assert secret not in str(events)
    finally:
        try:
            exporter.shutdown()
        except Exception:
            pass
        obs.prepare_sanitizer(None)


def test_wire_log_sanitizer_publishes_ready_sanitized_mode():
    """Before ready -> safe event only; after _wire_log_sanitizer -> restored
    sanitized body; never a half-wired leak."""
    import json
    from types import SimpleNamespace
    from app.infrastructure.observability import logging as obs

    secret = "E2EOTELVERIFY-7f3a-secret"
    obs.prepare_sanitizer(None)
    obs.register_safe_event("wire %s", "wire_probe")
    try:
        # 1) not-yet-ready: no restored message
        rec = logging.LogRecord(
            "app.w", logging.INFO, __file__, 1, "wire %s", (secret,), None
        )
        pre = json.loads(obs.SafeEventJsonFormatter().format(rec))
        assert "message" not in pre and secret not in json.dumps(pre)

        # 2) ready publish driven by the production helper
        from app.main import _wire_log_sanitizer

        fake = SimpleNamespace(secrets=SimpleNamespace(secret_values={secret}))
        _wire_log_sanitizer(fake)

        post = json.loads(obs.SafeEventJsonFormatter().format(rec))
        assert post["message"] == "wire [REDACTED]"
        assert secret not in json.dumps(post)
    finally:
        obs.prepare_sanitizer(None)
        from app.application.telemetry_semantics import (
            configure_span_attribute_sanitizer,
        )

        configure_span_attribute_sanitizer(None)

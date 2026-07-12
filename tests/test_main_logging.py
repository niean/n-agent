"""Verify main.py configures root logger so application INFO logs are visible.

uvicorn 0.30+ 默认 LOGGING_CONFIG 不再为 root logger 配置 handler/level,
导致应用层 logger.info() 静默 (仅 uvicorn.access logger 单独配置仍可见)。
main.py 在模块 import 时调用 _configure_logging() 修复此问题。
"""
from __future__ import annotations

import io
import logging


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

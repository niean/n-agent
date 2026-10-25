"""Shared provider telemetry hook SPI (Infrastructure).

Both LLM provider adapters (openai_compatible / anthropic_provider) accept
an optional ``telemetry_callback`` matching :class:`ProviderTelemetryCallback`.
The Protocols are duck-typed on purpose: the concrete implementation lives
in Application (telemetry_semantics.ChatAttempt) and is injected by the
composition root; Infrastructure never imports Application.
"""
from __future__ import annotations

from typing import Any, Optional, Protocol


class ChatAttemptHook(Protocol):
    """Duck-typed chat-attempt handle (see telemetry_semantics.ChatAttempt)."""

    def set_input(self, messages: Any, tools: Any = None, system: Any = None) -> None: ...

    def set_output(self, message: Any, incomplete: bool = False) -> None: ...

    def append_output_text(self, text: Any) -> None: ...

    def append_output_tool_delta(self, index: int, *, id: Any = None,
                                 name: Any = None, arguments: Any = None) -> None: ...

    def set_error_detail(self, text: Any) -> None: ...

    def mark_output_incomplete(self) -> None: ...

    def set_usage(self, input_tokens: Any, output_tokens: Any) -> None: ...

    def set_finish_reasons(self, reasons: Any) -> None: ...

    def set_response_model(self, model: Any) -> None: ...

    def set_error(self, error_type: Any) -> None: ...


class ProviderTelemetryCallback(Protocol):
    """Called once per LLM round-trip attempt; returns a context manager
    yielding a ChatAttemptHook (caller owns entering/exiting it)."""

    def __call__(self, *, model: str, stream: Optional[bool], provider: str) -> Any: ...


def optional_hook(attempt: Any, method: str, *args: Any, **kwargs: Any) -> None:
    """Content hooks are optional on legacy duck-typed handles and isolated."""
    try:
        callback = getattr(attempt, method, None)
        if callable(callback):
            callback(*args, **kwargs)
    except Exception:
        pass


class NoopChatAttempt:
    def set_input(self, messages, tools=None, system=None) -> None:
        pass

    def set_output(self, message, incomplete=False) -> None:
        pass

    def append_output_text(self, text) -> None:
        pass

    def append_output_tool_delta(self, index, *, id=None, name=None, arguments=None) -> None:
        pass

    def set_error_detail(self, text) -> None:
        pass

    def mark_output_incomplete(self) -> None:
        pass

    def set_usage(self, input_tokens, output_tokens) -> None:
        pass

    def set_finish_reasons(self, reasons) -> None:
        pass

    def set_response_model(self, model) -> None:
        pass

    def set_error(self, error_type) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


NOOP_CHAT_ATTEMPT = NoopChatAttempt()

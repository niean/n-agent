from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ChatEventType(str, Enum):
    MESSAGE_START = "message_start"
    CONTENT_DELTA = "content_delta"
    TOOL_CALL_DELTA = "tool_call_delta"
    MESSAGE_DONE = "message_done"
    ERROR = "error"
    DONE = "done"


@dataclass(frozen=True)
class ChatEvent:
    type: ChatEventType
    content: str = ""
    tool_call: dict[str, Any] | None = None
    finish_reason: str | None = None
    error: str | None = None

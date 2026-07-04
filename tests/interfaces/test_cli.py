from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.application.events import ChatEvent, ChatEventType
from app.domain.gateway import InteractionMessage
from app.interfaces.cli import main
from app.interfaces.cli.commands import chat as chat_cmd
from app.interfaces.cli.commands import status as status_cmd


@dataclass
class FakeResponse:
    session_id: str
    messages: list
    metadata: dict


@dataclass
class FakeOutbound:
    content: str


class FakeGatewayService:
    def __init__(self):
        self.events: list[InteractionMessage] = []

    async def handle_message(self, event: InteractionMessage):
        self.events.append(event)
        return FakeResponse("session-1", [FakeOutbound("reply")], {})

    async def handle_message_stream(self, event: InteractionMessage) -> AsyncIterator[ChatEvent]:
        self.events.append(event)
        yield ChatEvent(ChatEventType.MESSAGE_START)
        yield ChatEvent(ChatEventType.CONTENT_DELTA, content="reply")
        yield ChatEvent(ChatEventType.MESSAGE_DONE, finish_reason="stop")
        yield ChatEvent(ChatEventType.DONE)


class FakeServices:
    def __init__(self):
        self.gateway_service = FakeGatewayService()

    def health_snapshot(self):
        return {"provider": {"status": "ok"}, "gateway": {"status": "ok"}}


def test_cli_status_prints_health(monkeypatch, capsys):
    monkeypatch.setattr(status_cmd, "_build_services", lambda: FakeServices())

    exit_code = main(["status"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "provider" in output
    assert "gateway" in output


def test_cli_chat_sends_message(monkeypatch, capsys):
    services = FakeServices()
    monkeypatch.setattr(chat_cmd, "_build_services", lambda: services)

    exit_code = main(["chat", "hello", "--session-source", "cli-test"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "reply" in output
    assert services.gateway_service.events[0].text == "hello"
    assert services.gateway_service.events[0].session_key.platform_session_id == "cli-test"


def test_cli_help(capsys):
    exit_code = main(["--help"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "chat" in output

from dataclasses import dataclass

from app.domain.gateway import InteractionMessage
from app.interfaces import cli


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


class FakeServices:
    def __init__(self):
        self.gateway_service = FakeGatewayService()
        self.health_snapshot = lambda: {"provider": {"status": "ok"}, "gateway": {"status": "ok"}}


def test_cli_status_prints_health(monkeypatch, capsys):
    monkeypatch.setattr(cli, "build_application_services", lambda: FakeServices())

    exit_code = cli.main(["status"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "provider" in output
    assert "gateway" in output


def test_cli_chat_sends_message(monkeypatch, capsys):
    services = FakeServices()
    monkeypatch.setattr(cli, "build_application_services", lambda: services)

    exit_code = cli.main(["chat", "hello", "--session-source", "cli-test"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "reply" in output
    assert services.gateway_service.events[0].text == "hello"
    assert services.gateway_service.events[0].session_key.source_id == "cli-test"


def test_cli_help(capsys):
    exit_code = cli.main(["--help"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "chat" in output

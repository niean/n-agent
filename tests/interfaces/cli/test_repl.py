from __future__ import annotations

from typing import Any

import pytest

from app.interfaces.cli.repl import ReplRunner


def _eof_input() -> None:
    raise EOFError()


def _make_input_fn(items: list[Any]):
    it = iter(items)

    def _fn(*_args: Any) -> str:
        item = next(it)
        return item() if callable(item) else item

    return _fn


@pytest.mark.asyncio
async def test_repl_exit_on_eof(monkeypatch, fake_console, fake_gateway_client):
    monkeypatch.setattr("builtins.input", _make_input_fn([_eof_input]))
    rc = await ReplRunner(fake_gateway_client, fake_console, conversation_id="conv-1", is_tty=False).run()
    assert rc == 0


@pytest.mark.asyncio
async def test_repl_local_help_command(monkeypatch, fake_console, fake_gateway_client):
    monkeypatch.setattr("builtins.input", _make_input_fn(["/help", _eof_input]))
    rc = await ReplRunner(fake_gateway_client, fake_console, conversation_id="conv-1", is_tty=False).run()
    assert rc == 0


@pytest.mark.asyncio
async def test_repl_confirm_after_destructive(monkeypatch, fake_console, fake_gateway_client):
    fake_gateway_client.stream_responses = [
        [
            ("message_done", {"finish_reason": "confirmation_required", "metadata": {"confirmation": {"id": "c1"}}}),
            ("done", {}),
        ],
    ]
    monkeypatch.setattr("builtins.input", _make_input_fn(["/delete", "/confirm once", _eof_input]))
    rc = await ReplRunner(fake_gateway_client, fake_console, conversation_id="conv-1", is_tty=False).run()
    assert rc == 0
    assert fake_gateway_client.last_confirm_id == "c1"

from __future__ import annotations

from typing import Any

import pytest
from prompt_toolkit.document import Document

from app.interfaces.cli.repl import ReplRunner, build_slash_completer


def _eof_input() -> None:
    raise EOFError()


def _make_input_fn(items: list[Any]):
    it = iter(items)

    def _fn(*_args: Any) -> str:
        item = next(it)
        return item() if callable(item) else item

    return _fn


@pytest.mark.asyncio
async def test_repl_exit_on_eof(monkeypatch, fake_console, fake_chat_adapter):
    monkeypatch.setattr("builtins.input", _make_input_fn([_eof_input]))
    rc = await ReplRunner(fake_chat_adapter, fake_console, conversation_id="conv-1", is_tty=False).run()
    assert rc == 0


@pytest.mark.asyncio
async def test_repl_local_help_command(monkeypatch, fake_console, fake_chat_adapter):
    monkeypatch.setattr("builtins.input", _make_input_fn(["/help", _eof_input]))
    rc = await ReplRunner(fake_chat_adapter, fake_console, conversation_id="conv-1", is_tty=False).run()
    assert rc == 0


@pytest.mark.asyncio
async def test_repl_confirm_after_destructive(monkeypatch, fake_console, fake_chat_adapter):
    fake_chat_adapter.stream_responses = [
        [
            ("message_done", {"finish_reason": "confirmation_required", "metadata": {"confirmation": {"id": "c1"}}}),
            ("done", {}),
        ],
    ]
    monkeypatch.setattr("builtins.input", _make_input_fn(["/delete", "/confirm once", _eof_input]))
    rc = await ReplRunner(fake_chat_adapter, fake_console, conversation_id="conv-1", is_tty=False).run()
    assert rc == 0
    assert fake_chat_adapter.last_confirm_id == "c1"


def test_slash_completer_filters_root_commands_after_slash_prefix():
    completer = build_slash_completer()

    completions = list(completer.get_completions(Document("/p", cursor_position=2), None))
    texts = {completion.text for completion in completions}

    assert "/provider" in texts
    assert "/platform" in texts


def test_slash_completer_keeps_nested_subcommand_completion():
    completer = build_slash_completer()

    completions = list(completer.get_completions(Document("/provider l", cursor_position=11), None))
    texts = {completion.text for completion in completions}

    assert "list" in texts

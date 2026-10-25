from __future__ import annotations

from contextlib import contextmanager

from app.interfaces.cli.commands import chat


def _fake_scope(services):
    @contextmanager
    def scope():
        yield services

    return scope


def _make_args(**kw):
    defaults = {"message": None, "session_source": "local", "conversation_id": None, "no_stream": False}
    defaults.update(kw)
    return type("A", (), defaults)()


def test_chat_with_message_returns_zero(monkeypatch, fake_services):
    monkeypatch.setattr(chat, "_services_scope", _fake_scope(fake_services))
    args = _make_args(message="hello")
    rc = chat.run(args)
    assert rc == 0


def test_chat_no_message_non_tty_empty_stdin_returns_2(monkeypatch, fake_services):
    monkeypatch.setattr(chat, "_services_scope", _fake_scope(fake_services))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdin.read", lambda: "")
    args = _make_args(message=None)
    rc = chat.run(args)
    assert rc == 2


def test_chat_conversation_id_conflicts_with_session_source(monkeypatch, fake_services):
    monkeypatch.setattr(chat, "_services_scope", _fake_scope(fake_services))
    args = _make_args(message="x", session_source="local", conversation_id="other")
    rc = chat.run(args)
    assert rc != 0

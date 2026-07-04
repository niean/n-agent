from __future__ import annotations

from app.interfaces.cli.commands import sessions


def _make_args(**kw):
    defaults = {"session_source": "local", "conversation_id": None}
    defaults.update(kw)
    return type("A", (), defaults)()


def test_sessions_uses_conversation_id(monkeypatch, fake_services):
    args = _make_args(session_source=None, conversation_id="conv-x")
    monkeypatch.setattr(sessions, "_build_services", lambda: fake_services)
    rc = sessions.run(args)
    assert rc == 0
    assert fake_services.last_conversation_id == "conv-x"

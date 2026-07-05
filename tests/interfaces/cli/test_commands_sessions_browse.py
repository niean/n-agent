from __future__ import annotations

from types import SimpleNamespace

from app.domain.gateway import GatewaySessionLink
from app.interfaces.cli.commands import sessions


class _FakeRegistry:
    def __init__(self, links):
        self._links = links
        self.calls: list = []

    async def list_session_links(self, key):
        self.calls.append(key)
        return self._links


class _FakeSessionService:
    def __init__(self):
        self.detail_calls: list[str] = []

    async def get_session_detail(self, session_id):
        self.detail_calls.append(session_id)
        return {"session_id": session_id, "task_state": {"iter": 1}, "messages": []}


class _FakeServices:
    def __init__(self, links):
        self.gateway_registry = _FakeRegistry(links)
        self.session_service = _FakeSessionService()


def _make_args(**kw):
    base = {
        "session_source": "local",
        "conversation_id": None,
        "browse": False,
        "pick": None,
        "no_interactive": False,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _link(session_id="s1", display_name="S1"):
    return GatewaySessionLink(
        conversation_id="local",
        session_id=session_id,
        display_name=display_name,
    )


def test_sessions_browse_no_links_prints_empty(monkeypatch, capsys):
    services = _FakeServices([])
    monkeypatch.setattr(sessions, "_build_services", lambda: services)
    rc = sessions.run(_make_args(browse=True, no_interactive=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert out.strip() == "[]"


def test_sessions_browse_calls_list_session_links_with_cli_source(monkeypatch):
    services = _FakeServices([_link()])
    monkeypatch.setattr(sessions, "_build_services", lambda: services)
    rc = sessions.run(_make_args(browse=True, no_interactive=True))
    assert rc == 0
    assert len(services.gateway_registry.calls) == 1
    key = services.gateway_registry.calls[0]
    assert key.source_value == "cli"
    assert key.platform is None
    assert key.platform_session_id == "local"


def test_sessions_browse_uses_conversation_id_for_key(monkeypatch):
    services = _FakeServices([_link()])
    monkeypatch.setattr(sessions, "_build_services", lambda: services)
    sessions.run(_make_args(browse=True, session_source=None, conversation_id="conv-x", no_interactive=True))
    key = services.gateway_registry.calls[0]
    assert key.platform_session_id == "conv-x"


def test_sessions_browse_non_interactive_renders_table(monkeypatch, capsys):
    services = _FakeServices([_link("s1", "First"), _link("s2", "Second")])
    monkeypatch.setattr(sessions, "_build_services", lambda: services)
    rc = sessions.run(_make_args(browse=True, no_interactive=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "s1" in out
    assert "s2" in out
    assert "First" in out
    assert "Second" in out


def test_sessions_browse_pick_shows_detail(monkeypatch, capsys):
    services = _FakeServices([_link("s1"), _link("s2")])
    monkeypatch.setattr(sessions, "_build_services", lambda: services)
    rc = sessions.run(_make_args(browse=True, pick="s2", no_interactive=True))
    assert rc == 0
    assert services.session_service.detail_calls == ["s2"]
    out = capsys.readouterr().out
    assert "task_state" in out


def test_sessions_browse_pick_nonexistent_session_returns_1(monkeypatch, capsys):
    services = _FakeServices([_link("s1")])
    monkeypatch.setattr(sessions, "_build_services", lambda: services)

    async def _raise(session_id):
        raise RuntimeError("not found")

    services.session_service.get_session_detail = _raise
    rc = sessions.run(_make_args(browse=True, pick="missing", no_interactive=True))
    assert rc == 1


def test_sessions_browse_keyboard_interrupt_returns_130(monkeypatch, capsys):
    services = _FakeServices([_link("s1")])
    monkeypatch.setattr(sessions, "_build_services", lambda: services)

    def _raise_prompt(links):
        raise KeyboardInterrupt

    monkeypatch.setattr(sessions, "_prompt_for_session", _raise_prompt)
    monkeypatch.setattr(sessions, "_is_interactive", lambda args: True)
    rc = sessions.run(_make_args(browse=True))
    assert rc == 130


def test_sessions_browse_interactive_prompts_then_detail(monkeypatch, capsys):
    services = _FakeServices([_link("s1"), _link("s2")])
    monkeypatch.setattr(sessions, "_build_services", lambda: services)
    monkeypatch.setattr(sessions, "_prompt_for_session", lambda links: "s2")
    monkeypatch.setattr(sessions, "_is_interactive", lambda args: True)
    rc = sessions.run(_make_args(browse=True))
    assert rc == 0
    assert services.session_service.detail_calls == ["s2"]


def test_sessions_non_browse_preserves_gateway_send(monkeypatch, fake_services):
    monkeypatch.setattr(sessions, "_build_services", lambda: fake_services)
    rc = sessions.run(_make_args(browse=False, session_source=None, conversation_id="conv-x"))
    assert rc == 0
    assert fake_services.last_conversation_id == "conv-x"


def test_prompt_for_session_constructs_without_crashing(monkeypatch):
    """Regression: picker construction must not crash on Event registration or BufferControl.

    Catches two prior bugs:
    - ``@buffer.on_text_changed`` decorator (Event does not support decorator syntax; use ``+=``)
    - ``Window(content=buffer)`` (Buffer is not a UIControl; must wrap in BufferControl)
    """
    from prompt_toolkit.application import Application

    captured = {}

    def _fake_run(self):
        captured["constructed"] = True
        return "s1"

    monkeypatch.setattr(Application, "run", _fake_run)
    links = [_link("s1", "First"), _link("s2", "Second")]
    result = sessions._prompt_for_session(links)
    assert captured.get("constructed") is True
    assert result == "s1"


def test_session_picker_arrow_keys_move_visible_selection():
    app = sessions._build_session_picker_app([
        _link("s1", "First"),
        _link("s2", "Second"),
    ])
    body = app.layout.container.children[0].content

    class _Event:
        def __init__(self):
            self.app = self
            self.invalidated = 0

        def invalidate(self):
            self.invalidated += 1

    event = _Event()
    down = app.key_bindings.get_bindings_for_keys(("down",))[0].handler
    up = app.key_bindings.get_bindings_for_keys(("up",))[0].handler

    assert body.text().splitlines() == ["> s1  First", "  s2  Second"]

    down(event)
    assert event.invalidated == 1
    assert body.text().splitlines() == ["  s1  First", "> s2  Second"]

    up(event)
    assert event.invalidated == 2
    assert body.text().splitlines() == ["> s1  First", "  s2  Second"]

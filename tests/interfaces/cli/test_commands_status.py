from __future__ import annotations

from app.interfaces.cli.commands import status


def test_status_returns_zero(monkeypatch, fake_services):
    monkeypatch.setattr(status, "_build_services", lambda: fake_services)
    args = type("A", (), {})()
    rc = status.run(args)
    assert rc == 0

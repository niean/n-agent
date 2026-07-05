from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from app.interfaces.cli.commands import platform as platform_cmd


def _make_view(pid="feishu"):
    return SimpleNamespace(
        platform=SimpleNamespace(value=pid),
        display_name="Feishu",
        kind=SimpleNamespace(value="cloud"),
        status="active",
        error_code=None,
        error_message=None,
        config_summary={},
        session_count=5,
        last_active_at=datetime.now(timezone.utc),
    )


class _FakePlatform:
    def __init__(self):
        self.listed = False
        self.got: list[str] = []
        self.sessions_called: list[tuple[str, int, int]] = []

    async def list_platforms(self):
        self.listed = True
        return [_make_view()]

    async def get_platform(self, platform_str):
        self.got.append(platform_str)
        if platform_str == "missing":
            from app.application.platform_service import PlatformNotFoundError
            raise PlatformNotFoundError(platform_str)
        return SimpleNamespace(platform=_make_view(platform_str), total_sessions=10, active_sessions=2)

    async def list_platform_sessions(self, platform_str, limit, offset):
        self.sessions_called.append((platform_str, limit, offset))
        conv = SimpleNamespace(
            platform_session_id="psid-1234567890abcdef",
            active_session_id="asid-1",
            updated_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        return SimpleNamespace(items=[conv], total=1, limit=limit, offset=offset)


def _args(**kw):
    base = {"platform_command": None, "json": False, "form": False, "yaml": False, "platform": None,
            "limit": None, "offset": None}
    base.update(kw)
    return SimpleNamespace(**base)


def test_platform_list(monkeypatch, capsys):
    fake = _FakePlatform()
    monkeypatch.setattr(platform_cmd, "_load_platform_service", lambda: fake)
    rc = platform_cmd.run(_args(platform_command="list"))
    assert rc == 0
    assert fake.listed
    assert "feishu" in capsys.readouterr().out


def test_platform_get(monkeypatch, capsys):
    fake = _FakePlatform()
    monkeypatch.setattr(platform_cmd, "_load_platform_service", lambda: fake)
    rc = platform_cmd.run(_args(platform_command="get", platform="feishu"))
    assert rc == 0
    assert fake.got == ["feishu"]


def test_platform_get_not_found(monkeypatch, capsys):
    fake = _FakePlatform()
    monkeypatch.setattr(platform_cmd, "_load_platform_service", lambda: fake)
    rc = platform_cmd.run(_args(platform_command="get", platform="missing"))
    assert rc == 1


def test_platform_sessions_uses_updated_at(monkeypatch, capsys):
    fake = _FakePlatform()
    monkeypatch.setattr(platform_cmd, "_load_platform_service", lambda: fake)
    rc = platform_cmd.run(_args(platform_command="sessions", platform="feishu", limit=10, offset=0))
    assert rc == 0
    out = capsys.readouterr().out
    assert "updated_at" in out or "updated" in out.lower()
    assert "last_active_at" not in out


def test_platform_sessions_redact_platform_session_id(monkeypatch, capsys):
    fake = _FakePlatform()
    monkeypatch.setattr(platform_cmd, "_load_platform_service", lambda: fake)
    rc = platform_cmd.run(_args(platform_command="sessions", platform="feishu", limit=10, offset=0, json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    psid = data[0]["platform_session_id"]
    # 脱敏后不应含完整 psid-1234567890abcdef
    assert "1234567890abcdef" not in psid
    assert "***" in psid


def test_platform_sessions_limit_offset(monkeypatch, capsys):
    fake = _FakePlatform()
    monkeypatch.setattr(platform_cmd, "_load_platform_service", lambda: fake)
    platform_cmd.run(_args(platform_command="sessions", platform="feishu", limit=20, offset=5))
    assert fake.sessions_called == [("feishu", 20, 5)]

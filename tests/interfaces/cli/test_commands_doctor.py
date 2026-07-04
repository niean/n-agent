from __future__ import annotations

from types import SimpleNamespace

from app.interfaces.cli.commands import doctor


def _make_fake_services(*, sandbox_none=False, kb_empty=False, mcp_empty=False,
                         provider_empty=False, external_memory_none=False,
                         kb_probe_fail=False):
    from app.domain.mcp import McpTransportType

    class _FakeProvider:
        async def list_providers(self):
            return [] if provider_empty else [SimpleNamespace(id="p1", is_active=True)]

    class _FakeKB:
        def __init__(self):
            self.probed: list[str] = []

        async def list_bases(self):
            return [] if kb_empty else [SimpleNamespace(id="kb1", enabled=True)]

        async def get_base(self, kid):
            return SimpleNamespace(id=kid, enabled=True)

        async def probe_base(self, kid):
            self.probed.append(kid)
            if kb_probe_fail:
                raise RuntimeError("probe failed")

    class _FakeMcp:
        def __init__(self):
            self.probed: list[str] = []

        async def list_sites(self):
            return [] if mcp_empty else [SimpleNamespace(id="s1", name="S1", enabled=True, transport_type=McpTransportType.STREAMABLE_HTTP, url="http://x", command=None, args=[], env={})]

        async def get_site(self, sid):
            return SimpleNamespace(id=sid, name="S1", enabled=True, transport_type=McpTransportType.STREAMABLE_HTTP, url="http://x", command=None, args=[], env={})

        async def probe_site(self, payload):
            self.probed.append(payload.name)

    class _FakeSchedule:
        async def list(self):
            return []

    class _FakeSession:
        async def list_sessions(self):
            return []

    class _FakeSkill:
        async def list_skills(self, include_disabled=False):
            return []

    class _FakePlugin:
        async def list_plugins(self):
            return []

    class _FakeExternalMemoryProvider:
        def __init__(self):
            self.probed: list[str] = []

        def list(self):
            return []

        def probe(self, pid):
            self.probed.append(pid)
            return "ok"

    class _FakeSandbox:
        async def list_active_sandboxes(self):
            return []

    settings = SimpleNamespace(
        provider_base_url="http://x", provider_api_key="sk-1", provider_model="m",
        sqlite_path=__file__, workspace_root=".",
        kb_enabled=False,
    )
    return SimpleNamespace(
        settings=settings,
        provider_service=_FakeProvider(),
        knowledge_service=_FakeKB(),
        mcp_service=_FakeMcp(),
        schedule_service=_FakeSchedule(),
        session_service=_FakeSession(),
        skill_service=_FakeSkill(),
        plugin_service=_FakePlugin(),
        external_memory_provider_service=None if external_memory_none else _FakeExternalMemoryProvider(),
        external_memory_service=None,
        sandbox_dashboard_service=None if sandbox_none else _FakeSandbox(),
    )


def _args(**kw):
    base = {"probe": False}
    base.update(kw)
    return SimpleNamespace(**base)


def test_doctor_all_pass(monkeypatch, capsys):
    fake = _make_fake_services()
    monkeypatch.setattr(doctor, "_load_services", lambda: fake)
    rc = doctor.run(_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "PASS" in out


def test_doctor_warn_sandbox_disabled(monkeypatch, capsys):
    fake = _make_fake_services(sandbox_none=True)
    monkeypatch.setattr(doctor, "_load_services", lambda: fake)
    rc = doctor.run(_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "WARN" in out


def test_doctor_probe_off_no_network(monkeypatch, capsys):
    fake = _make_fake_services()
    monkeypatch.setattr(doctor, "_load_services", lambda: fake)
    doctor.run(_args(probe=False))
    assert fake.knowledge_service.probed == []
    assert fake.mcp_service.probed == []


def test_doctor_probe_on_calls_probe(monkeypatch, capsys):
    fake = _make_fake_services()
    monkeypatch.setattr(doctor, "_load_services", lambda: fake)
    doctor.run(_args(probe=True))
    assert fake.knowledge_service.probed == ["kb1"]
    assert len(fake.mcp_service.probed) >= 1
    assert fake.external_memory_provider_service.probed == []


def test_doctor_single_fail_does_not_block(monkeypatch, capsys):
    fake = _make_fake_services(kb_probe_fail=True)
    monkeypatch.setattr(doctor, "_load_services", lambda: fake)
    rc = doctor.run(_args(probe=True))
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
    # 后续项仍执行
    assert "PASS" in out or "WARN" in out


def test_doctor_external_memory_none_warn(monkeypatch, capsys):
    fake = _make_fake_services(external_memory_none=True)
    monkeypatch.setattr(doctor, "_load_services", lambda: fake)
    rc = doctor.run(_args())
    assert rc == 0

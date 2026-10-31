"""T5 telemetry wiring tests: composition root (main.py) + entry lifecycle.

Covers (plan T5 S1):
- build_application_services constructs (but does not start background
  sampling for) the observability handle, exactly once per build;
- the same telemetry facade object is injected into the graph runner,
  ToolService, BudgetService and ContextService;
- the disabled state injects no-op telemetry and no provider callback;
- the provider factory closure injects begin_chat_attempt as the provider
  telemetry callback when enabled;
- create_app instruments the FastAPI app before returning, and the HTTP
  lifespan holds/closes the same handle;
- the shared application_runtime helper configures once and shuts down
  exactly once, including on the exception path;
- the CLI chat and ACP entries hold/close the same handle around agent
  turns through that helper.
"""
from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.domain.provider import ProviderConfig


def _settings(tmp_path: Path, *, otel_enabled: bool = False) -> Settings:
    skills_root = tmp_path / "skills"
    skills_root.mkdir(exist_ok=True)
    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir(exist_ok=True)
    return Settings(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        skills_root=str(skills_root),
        plugins_root=str(plugins_root),
        scheduler_enabled=False,
        feishu_enabled=False,
        artifacts_enabled=False,
        otel_enabled=otel_enabled,
    )


class FakeHandle:
    def __init__(self):
        self.shutdown_calls = 0

    def snapshot(self):
        return {}

    def shutdown(self):
        self.shutdown_calls += 1


@pytest.fixture
def fake_observability(monkeypatch):
    """Replace the SDK setup with a fake handle; no real OTel state."""
    import app.infrastructure.observability.setup as setup

    state = SimpleNamespace(handle=None, configure_calls=0)

    def fake_configure(settings, **_kwargs):
        state.configure_calls += 1
        if state.handle is None:
            state.handle = FakeHandle()
        return state.handle

    monkeypatch.setattr(setup, "configure_observability", fake_configure)
    return state


# ---------------------------------------------------------------------------
# build: handle construction + telemetry injection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("enabled", [True, False])
def test_build_registers_client_instrumentation_before_services(
    tmp_path, fake_observability, monkeypatch, enabled
):
    import app.main as main_module
    from app.infrastructure.observability import instrumentors

    calls = []
    monkeypatch.setattr(instrumentors, "instrument_clients",
                        lambda **kwargs: calls.append(kwargs) or True, raising=False)
    services = main_module.build_application_services(_settings(tmp_path, otel_enabled=enabled))
    assert len(calls) == int(enabled)
    if enabled:
        assert calls[0]["settings"] is services.settings


def test_build_constructs_handle_once_and_no_background_threads(
    tmp_path, fake_observability
):
    from app.main import build_application_services

    services = build_application_services(_settings(tmp_path, otel_enabled=True))

    assert fake_observability.configure_calls == 1
    assert services.observability_handle is fake_observability.handle
    # construction must not start background sampling/profiling threads
    assert not [
        t for t in threading.enumerate()
        if any(k in t.name.lower() for k in ("spy", "profil", "sampler", "otel"))
        and not t.daemon and t is not threading.current_thread()
    ]


def test_build_injects_same_telemetry_facade_everywhere(tmp_path, fake_observability):
    from app.main import build_application_services
    from app.application.telemetry_semantics import SemanticsTelemetry

    services = build_application_services(_settings(tmp_path, otel_enabled=True))

    assert isinstance(services.telemetry, SemanticsTelemetry)
    runner = services.chat_service.graph_runner
    assert runner._telemetry is services.telemetry
    assert services.tool_service._telemetry is services.telemetry
    assert services.tool_service._budget_service._telemetry is services.telemetry
    assert runner.context_service._telemetry is services.telemetry


def test_build_disabled_uses_noop_telemetry(tmp_path, fake_observability):
    from app.main import build_application_services
    from app.application.telemetry_semantics import NoopTelemetry

    services = build_application_services(_settings(tmp_path, otel_enabled=False))

    assert isinstance(services.telemetry, NoopTelemetry)
    runner = services.chat_service.graph_runner
    assert runner._telemetry is services.telemetry
    assert services.tool_service._telemetry is services.telemetry
    assert services.tool_service._budget_service._telemetry is services.telemetry


def test_provider_factory_injects_callback_when_enabled(tmp_path, fake_observability):
    from app.main import build_application_services

    services = build_application_services(_settings(tmp_path, otel_enabled=True))
    now = datetime.now(timezone.utc)
    config = ProviderConfig(
        id="p1",
        name="p1",
        provider_type="openai-compatible",
        base_url="http://localhost:9",
        model="gpt-4o",
        api_key_present=True,
        is_active=True,
        extra_headers=None,
        created_at=now,
        updated_at=now,
    )
    asyncio.run(services.provider_holder.swap(config, "k"))
    provider = services.provider_holder._provider
    # bound methods are recreated per attribute access; compare by value
    assert provider._telemetry_callback == services.telemetry.begin_chat_attempt


def test_provider_factory_injects_no_callback_when_disabled(tmp_path, fake_observability):
    from app.main import build_application_services

    services = build_application_services(_settings(tmp_path, otel_enabled=False))
    now = datetime.now(timezone.utc)
    config = ProviderConfig(
        id="p1",
        name="p1",
        provider_type="openai-compatible",
        base_url="http://localhost:9",
        model="gpt-4o",
        api_key_present=True,
        is_active=True,
        extra_headers=None,
        created_at=now,
        updated_at=now,
    )
    asyncio.run(services.provider_holder.swap(config, "k"))
    assert services.provider_holder._provider._telemetry_callback is None


# ---------------------------------------------------------------------------
# create_app: instrument_app before return; lifespan holds/closes handle
# ---------------------------------------------------------------------------


def test_create_app_instruments_before_return(tmp_path, fake_observability, monkeypatch):
    import app.infrastructure.observability.instrumentors as instrumentors
    import app.main as main_module

    calls = []

    def fake_instrument_app(app, **_kwargs):
        calls.append(app)
        return True

    monkeypatch.setattr(instrumentors, "instrument_app", fake_instrument_app)
    app = main_module.create_app(_settings(tmp_path, otel_enabled=True))
    assert calls == [app]


@pytest.mark.asyncio
async def test_http_lifespan_shuts_down_handle(tmp_path, fake_observability):
    from app.main import create_app

    app = create_app(_settings(tmp_path, otel_enabled=True))
    handle = fake_observability.handle
    async with app.router.lifespan_context(app):
        pass
    assert handle.shutdown_calls == 1


# ---------------------------------------------------------------------------
# application_runtime: configure once -> run -> shutdown exactly once
# ---------------------------------------------------------------------------


def _fake_runtime(monkeypatch, services=None):
    import app.main as main_module

    state = SimpleNamespace(entered=0, exited=0)

    @contextmanager
    def fake_runtime(settings=None):
        state.entered += 1
        try:
            yield services if services is not None else SimpleNamespace(
                gateway_service=object(), settings=SimpleNamespace()
            )
        finally:
            state.exited += 1

    monkeypatch.setattr(main_module, "application_runtime", fake_runtime)
    return state


def test_application_runtime_configures_and_shuts_down_once(tmp_path, fake_observability):
    import app.main as main_module
    from app.main import build_application_services

    builds = []

    def fake_build(settings=None, **_kwargs):
        builds.append(settings)
        return SimpleNamespace(gateway_service=object())

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(main_module, "build_application_services", fake_build)
    try:
        with main_module.application_runtime(_settings(tmp_path, otel_enabled=True)):
            assert fake_observability.configure_calls == 1
            assert builds and builds[0].otel_enabled
        assert fake_observability.handle.shutdown_calls == 1
    finally:
        monkeypatch.undo()


def test_application_runtime_shuts_down_on_exception(tmp_path, fake_observability):
    import app.main as main_module

    with pytest.raises(RuntimeError):
        with main_module.application_runtime(_settings(tmp_path, otel_enabled=True)):
            raise RuntimeError("boom")
    assert fake_observability.handle.shutdown_calls == 1


# ---------------------------------------------------------------------------
# CLI entries hold/close the same handle
# ---------------------------------------------------------------------------


def test_cli_chat_entry_uses_application_runtime(monkeypatch):
    from app.interfaces.cli.commands import chat

    state = _fake_runtime(monkeypatch)

    async def fake_send_once(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(chat, "_send_once", fake_send_once)
    args = SimpleNamespace(
        message="hi",
        conversation_id=None,
        session_source=None,
        no_stream=True,
    )
    assert chat.run(args) == 0
    assert state.entered == 1
    assert state.exited == 1


def test_cli_chat_entry_closes_handle_on_exception(monkeypatch):
    from app.interfaces.cli.commands import chat

    state = _fake_runtime(monkeypatch)

    async def fake_send_once(*_args, **_kwargs):
        raise RuntimeError("send failed")

    monkeypatch.setattr(chat, "_send_once", fake_send_once)
    args = SimpleNamespace(
        message="hi",
        conversation_id=None,
        session_source=None,
        no_stream=True,
    )
    with pytest.raises(RuntimeError):
        chat.run(args)
    assert state.entered == 1
    assert state.exited == 1


def test_acp_entry_uses_application_runtime(monkeypatch):
    from app.interfaces.cli.commands.acp import command

    state = _fake_runtime(monkeypatch)
    served = []

    def fake_serve(services):
        served.append(services)

    monkeypatch.setattr(command, "_serve", fake_serve)
    assert command._run_server() == 0
    assert state.entered == 1
    assert state.exited == 1
    assert len(served) == 1


def test_acp_entry_closes_handle_on_exception(monkeypatch):
    from app.interfaces.cli.commands.acp import command

    state = _fake_runtime(monkeypatch)

    def fake_serve(_services):
        raise RuntimeError("acp boom")

    monkeypatch.setattr(command, "_serve", fake_serve)
    assert command._run_server() == 1
    assert state.entered == 1
    assert state.exited == 1

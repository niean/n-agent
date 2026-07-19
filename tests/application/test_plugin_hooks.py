"""T6: Plugin hook aggregation, dispatch contract and safety isolation tests.

Covers:
- S1: _hooks aggregation (stable order, replace-not-accumulate, disabled/failed
     excluded), invoke_hook snapshot, hook_schema_version=1, shallow-copy payload.
- S2: async/sync execution isolation, per-callback wait_for timeout, exception
     isolation, log excludes payload/secret/trusted_metadata, late result discarded.
- S3: return contracts per hook type (observer ignored, pre_llm merge, transforms
     first-wins).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.plugin_service import (
    HookRegistration,
    PluginScanResult,
    PluginService,
    PluginToolRegistration,
    VALID_HOOKS,
)
from app.domain.plugin import Plugin, PluginKind, PluginManifest, PluginSource
from app.domain.tool import ToolDefinition, ToolSourceType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(plugin_hook_timeout_seconds: float = 5.0, **kwargs):
    s = MagicMock()
    s.plugin_hook_timeout_seconds = plugin_hook_timeout_seconds
    s.plugin_tool_timeout_seconds = kwargs.get("plugin_tool_timeout_seconds", 30)
    s.plugins_enabled = kwargs.get("plugins_enabled", [])
    s.plugins_disabled = kwargs.get("plugins_disabled", [])
    return s


def _make_manifest(key: str, source: PluginSource = PluginSource.BUNDLED) -> PluginManifest:
    return PluginManifest(
        key=key,
        name=key,
        version="0.1",
        description="",
        source=source,
        path=f"/plugins/{key}",
        kind=PluginKind.STANDALONE,
    )


def _make_plugin(key: str, enabled: bool = True, source: PluginSource = PluginSource.BUNDLED) -> Plugin:
    return Plugin(
        id=f"id-{key}",
        key=key,
        name=key,
        source=source,
        enabled=enabled,
        kind=PluginKind.STANDALONE,
    )


def _hook_reg(plugin_key: str, hook_name: str, callback, index: int = 0) -> HookRegistration:
    return HookRegistration(
        plugin_key=plugin_key,
        hook_name=hook_name,
        callback=callback,
        registration_index=index,
    )


def _build_service(
    *,
    registry_plugins: list[Plugin] | None = None,
    manifests: list[PluginManifest] | None = None,
    hook_registrations: dict[str, list[HookRegistration]] | None = None,
    tool_registrations: dict[str, list[PluginToolRegistration]] | None = None,
    errors: dict[str, str] | None = None,
    settings=None,
    loader_side_effect=None,
) -> PluginService:
    """Build a PluginService with the 3-phase loader API mocked.

    ``loader_side_effect`` (when provided) is a list of hook_registrations
    dicts, one per consecutive scan call, replacing the old list of
    PluginScanResult objects.
    """
    from app.application.plugin_service import PluginContext
    from app.infrastructure.plugin.file_loader import (
        DiscoveryCandidate,
        LoaderToken,
        PluginDiscoveryResult,
        PluginRegisterFailed,
        PreparedPlugin,
    )

    registry_plugins = registry_plugins or []
    registry = AsyncMock()
    registry.list_plugins.return_value = list(registry_plugins)
    registry.get_plugin.side_effect = lambda key: next(
        (p for p in registry_plugins if p.key == key), None
    )
    registry.get_secret_config.return_value = {}
    registry.replace_all_plugins = AsyncMock()
    registry.set_enabled.return_value = MagicMock()

    manifests_list = manifests or []
    _hooks = hook_registrations or {}
    _errors = errors or {}
    _tools = tool_registrations or {}

    # Build discovery candidates from manifests (discovery_index = list order)
    candidates = [
        DiscoveryCandidate(
            key=m.key, source=m.source, path=m.path,
            discovery_index=i, status="ok", diagnostic=None, manifest=m,
        )
        for i, m in enumerate(manifests_list)
    ]
    winners = {c.key: c for c in candidates}
    discovery_result = PluginDiscoveryResult(
        candidates=candidates, winners=winners, warnings=[],
    )

    # For consecutive scans with different hooks
    if loader_side_effect is not None:
        all_hook_versions = loader_side_effect
    else:
        all_hook_versions = [_hooks]
    scan_version = [0]
    current_hooks = [all_hook_versions[0]]

    loader = MagicMock()  # sync methods (discover/prepare/load_and_register)

    def _discover():
        idx = min(scan_version[0], len(all_hook_versions) - 1)
        current_hooks[0] = all_hook_versions[idx]
        scan_version[0] += 1
        return discovery_result
    loader.discover.side_effect = _discover

    def _prepare(candidate):
        return PreparedPlugin(
            manifest=candidate.manifest, source=candidate.source,
            token=LoaderToken("directory", {"path": candidate.path}), warnings=[],
        )
    loader.prepare.side_effect = _prepare

    def _load_and_register(prepared, cfg, secret):
        key = prepared.manifest.key
        if key in _errors:
            err_str = _errors[key]
            if ": " in err_str:
                code, msg = err_str.split(": ", 1)
            else:
                code, msg = "register_failed", err_str
            raise PluginRegisterFailed(code, msg)
        ctx = PluginContext(
            plugin_key=key, plugin_config=cfg or {}, secret_config=secret or {},
        )
        for h in current_hooks[0].get(key, []):
            ctx.hook_registrations.append(h)
        for t in _tools.get(key, []):
            ctx.tool_registrations.append(t)
        return ctx
    loader.load_and_register.side_effect = _load_and_register

    tool_service = MagicMock()
    tool_service.list_definitions.return_value = []
    tool_service.set_dynamic_definitions = lambda key, defs: None

    service = PluginService(
        registry=registry,
        loader=loader,
        tool_service=tool_service,
        route_refresher=lambda names: None,
        settings=settings or _make_settings(),
    )
    return service


def _set_hooks(service: PluginService, hook_name: str, regs: list[HookRegistration]) -> None:
    """Directly set _hooks for dispatcher tests, bypassing scan()."""
    service._hooks = {hook_name: tuple(regs)}


# ---------------------------------------------------------------------------
# S1: Aggregation in scan()
# ---------------------------------------------------------------------------


async def test_scan_aggregates_hooks_grouped_by_hook_name():
    """After scan, _hooks maps hook_name -> tuple of HookRegistration."""
    plugins = [_make_plugin("alpha"), _make_plugin("beta")]
    manifests = [_make_manifest("alpha"), _make_manifest("beta")]
    hooks = {
        "alpha": [
            _hook_reg("alpha", "on_session_start", lambda **kw: None),
            _hook_reg("alpha", "on_turn_start", lambda **kw: None),
        ],
        "beta": [
            _hook_reg("beta", "on_session_start", lambda **kw: None),
        ],
    }
    service = _build_service(
        registry_plugins=plugins,
        manifests=manifests,
        hook_registrations=hooks,
    )
    await service.scan()

    assert set(service._hooks.keys()) == {"on_session_start", "on_turn_start"}
    assert len(service._hooks["on_session_start"]) == 2
    assert len(service._hooks["on_turn_start"]) == 1
    # All entries are HookRegistration tuples
    for hook_name, regs in service._hooks.items():
        assert isinstance(regs, tuple)
        for reg in regs:
            assert isinstance(reg, HookRegistration)


async def test_scan_replaces_hooks_wholesale_on_consecutive_scans():
    """Two consecutive scans replace (not accumulate) _hooks."""
    plugin = _make_plugin("alpha")
    manifest = _make_manifest("alpha")

    hooks_v1 = {
        "alpha": [_hook_reg("alpha", "on_session_start", lambda **kw: None)],
    }
    hooks_v2 = {
        "alpha": [
            _hook_reg("alpha", "on_turn_start", lambda **kw: None),
            _hook_reg("alpha", "on_session_end", lambda **kw: None),
        ],
    }

    service = _build_service(
        registry_plugins=[plugin],
        manifests=[manifest],
        hook_registrations=hooks_v1,
        loader_side_effect=[hooks_v1, hooks_v2],
    )

    await service.scan()
    assert set(service._hooks.keys()) == {"on_session_start"}

    await service.scan()
    # v1's on_session_start must be gone; only v2's hooks present
    assert set(service._hooks.keys()) == {"on_turn_start", "on_session_end"}
    assert "on_session_start" not in service._hooks


async def test_scan_excludes_hooks_from_failed_plugins():
    """Plugins listed in result.errors do not contribute callbacks."""
    plugins = [_make_plugin("alpha"), _make_plugin("beta")]
    manifests = [_make_manifest("alpha"), _make_manifest("beta")]
    hooks = {
        "alpha": [_hook_reg("alpha", "on_session_start", lambda **kw: None)],
        "beta": [_hook_reg("beta", "on_session_start", lambda **kw: None)],
    }
    errors = {"beta": "register_failed: boom"}
    service = _build_service(
        registry_plugins=plugins,
        manifests=manifests,
        hook_registrations=hooks,
        errors=errors,
    )
    await service.scan()

    assert "on_session_start" in service._hooks
    regs = service._hooks["on_session_start"]
    assert len(regs) == 1
    assert regs[0].plugin_key == "alpha"


async def test_scan_excludes_hooks_from_disabled_plugins():
    """Disabled plugins do not contribute callbacks."""
    plugins = [_make_plugin("alpha", enabled=True), _make_plugin("beta", enabled=False)]
    manifests = [_make_manifest("alpha"), _make_manifest("beta")]
    hooks = {
        "alpha": [_hook_reg("alpha", "on_session_start", lambda **kw: None)],
        "beta": [_hook_reg("beta", "on_session_start", lambda **kw: None)],
    }
    service = _build_service(
        registry_plugins=plugins,
        manifests=manifests,
        hook_registrations=hooks,
    )
    await service.scan()

    assert "on_session_start" in service._hooks
    regs = service._hooks["on_session_start"]
    assert len(regs) == 1
    assert regs[0].plugin_key == "alpha"


async def test_scan_sorts_hooks_by_plugin_load_order_then_registration_index():
    """Within a hook, callbacks are sorted by (manifest load order, registration_index)."""
    # Manifest order: gamma(0), alpha(1), beta(2)
    manifests = [_make_manifest("gamma"), _make_manifest("alpha"), _make_manifest("beta")]
    hooks = {
        "alpha": [
            _hook_reg("alpha", "on_turn_start", lambda **kw: None, index=5),
            _hook_reg("alpha", "on_turn_start", lambda **kw: None, index=2),
        ],
        "beta": [
            _hook_reg("beta", "on_turn_start", lambda **kw: None, index=1),
        ],
        "gamma": [
            _hook_reg("gamma", "on_turn_start", lambda **kw: None, index=0),
        ],
    }
    plugins = [_make_plugin("alpha"), _make_plugin("beta"), _make_plugin("gamma")]
    service = _build_service(
        registry_plugins=plugins,
        manifests=manifests,
        hook_registrations=hooks,
    )
    await service.scan()

    regs = service._hooks["on_turn_start"]
    # Expected: gamma(0,0), alpha(1,2), alpha(1,5), beta(2,1)
    assert [r.plugin_key for r in regs] == ["gamma", "alpha", "alpha", "beta"]
    assert [r.registration_index for r in regs] == [0, 2, 5, 1]


# ---------------------------------------------------------------------------
# S1: Snapshot, hook_schema_version, shallow-copy
# ---------------------------------------------------------------------------


async def test_invoke_hook_returns_empty_when_no_callbacks():
    service = _build_service()
    result = await service.invoke_hook("on_session_start")
    assert result == []


async def test_invoke_hook_snapshots_at_call_start():
    """Replacing _hooks during invocation does not affect in-flight dispatch."""
    called: list[str] = []

    async def callback_a(**kwargs):
        called.append("A")
        # Replace _hooks during execution; callback_b should NOT be called
        _set_hooks(service, "on_session_start", [
            _hook_reg("p2", "on_session_start", callback_b),
        ])

    def callback_b(**kwargs):
        called.append("B")

    service = _build_service()
    _set_hooks(service, "on_session_start", [
        _hook_reg("p1", "on_session_start", callback_a),
    ])
    await service.invoke_hook("on_session_start")

    assert called == ["A"]


async def test_invoke_hook_adds_hook_schema_version_to_kwargs():
    received: list[int] = []

    def callback(**kwargs):
        received.append(kwargs.get("hook_schema_version"))

    service = _build_service()
    _set_hooks(service, "on_session_start", [
        _hook_reg("p1", "on_session_start", callback),
    ])
    await service.invoke_hook("on_session_start", session_id="s1")

    assert received == [1]


async def test_invoke_hook_shallow_copies_list_payload():
    original_list = [1, 2, 3]
    received: list[list] = []

    def callback(**kwargs):
        data = kwargs["data"]
        data.append(99)
        received.append(data)

    service = _build_service()
    _set_hooks(service, "on_session_start", [
        _hook_reg("p1", "on_session_start", callback),
    ])
    await service.invoke_hook("on_session_start", data=original_list)

    assert original_list == [1, 2, 3]  # caller's list unchanged
    assert received[0] == [1, 2, 3, 99]  # callback got a shallow copy


async def test_invoke_hook_shallow_copies_dict_payload():
    original_dict = {"a": 1}
    received: list[dict] = []

    def callback(**kwargs):
        data = kwargs["data"]
        data["b"] = 2
        received.append(data)

    service = _build_service()
    _set_hooks(service, "on_session_start", [
        _hook_reg("p1", "on_session_start", callback),
    ])
    await service.invoke_hook("on_session_start", data=original_dict)

    assert original_dict == {"a": 1}  # caller's dict unchanged
    assert received[0] == {"a": 1, "b": 2}


# ---------------------------------------------------------------------------
# S2: Execution isolation
# ---------------------------------------------------------------------------


async def test_invoke_hook_awaits_async_callback_directly():
    """Async callbacks run in the event loop thread (not a worker thread)."""
    main_thread = threading.get_ident()
    callback_thread: list[int] = []

    async def callback(**kwargs):
        callback_thread.append(threading.get_ident())

    service = _build_service()
    _set_hooks(service, "on_session_start", [
        _hook_reg("p1", "on_session_start", callback),
    ])
    await service.invoke_hook("on_session_start")

    assert callback_thread[0] == main_thread


async def test_invoke_hook_runs_sync_callback_via_to_thread():
    """Sync callbacks run in a worker thread (not the event loop thread)."""
    main_thread = threading.get_ident()
    callback_thread: list[int] = []

    def callback(**kwargs):
        callback_thread.append(threading.get_ident())

    service = _build_service()
    _set_hooks(service, "on_session_start", [
        _hook_reg("p1", "on_session_start", callback),
    ])
    await service.invoke_hook("on_session_start")

    assert callback_thread[0] != main_thread


async def test_invoke_hook_per_callback_timeout_sync():
    """Sync callback exceeding timeout is abandoned; dispatch continues."""
    called: list[str] = []

    def slow_callback(**kwargs):
        time.sleep(0.3)
        return "slow"

    def fast_callback(**kwargs):
        called.append("fast")
        return "fast"

    service = _build_service(settings=_make_settings(plugin_hook_timeout_seconds=0.1))
    _set_hooks(service, "on_session_start", [
        _hook_reg("p1", "on_session_start", slow_callback),
        _hook_reg("p2", "on_session_start", fast_callback),
    ])
    result = await service.invoke_hook("on_session_start")

    assert result == []  # observer hook
    assert called == ["fast"]  # fast callback still ran


async def test_invoke_hook_per_callback_timeout_async():
    """Async callback exceeding timeout is cancelled; dispatch continues."""
    called: list[str] = []

    async def slow_callback(**kwargs):
        await asyncio.sleep(0.3)
        return "slow"

    async def fast_callback(**kwargs):
        called.append("fast")
        return "fast"

    service = _build_service(settings=_make_settings(plugin_hook_timeout_seconds=0.1))
    _set_hooks(service, "on_session_start", [
        _hook_reg("p1", "on_session_start", slow_callback),
        _hook_reg("p2", "on_session_start", fast_callback),
    ])
    result = await service.invoke_hook("on_session_start")

    assert result == []
    assert called == ["fast"]


async def test_invoke_hook_exception_isolated_continues():
    """An exception in one callback does not break the dispatch loop."""
    called: list[str] = []

    def bad_callback(**kwargs):
        raise RuntimeError("boom")

    def good_callback(**kwargs):
        called.append("good")
        return "good"

    service = _build_service()
    _set_hooks(service, "on_session_start", [
        _hook_reg("p1", "on_session_start", bad_callback),
        _hook_reg("p2", "on_session_start", good_callback),
    ])
    result = await service.invoke_hook("on_session_start")

    assert result == []
    assert called == ["good"]


async def test_invoke_hook_log_excludes_payload_secret_and_trusted_metadata(caplog):
    """Log on exception contains plugin_key/hook_name/exc-type only; no payload,
    no secret, no trusted_metadata."""
    def bad_callback(**kwargs):
        raise RuntimeError("boom with secret message")

    service = _build_service()
    _set_hooks(service, "on_session_start", [
        _hook_reg("p1", "on_session_start", bad_callback),
    ])

    with caplog.at_level(logging.WARNING, logger="app.application.plugin_service"):
        await service.invoke_hook(
            "on_session_start",
            session_id="s1",
            metadata={"user_input": "secret message"},
            trusted_metadata={"api_key": "sk-abc123"},
        )

    all_log_text = " ".join(r.message for r in caplog.records)
    # Must contain plugin key, hook name, and exception type
    assert "p1" in all_log_text
    assert "on_session_start" in all_log_text
    assert "RuntimeError" in all_log_text
    # Must NOT contain payload content, secret, or trusted_metadata values
    assert "secret message" not in all_log_text
    assert "sk-abc123" not in all_log_text
    # Must NOT contain the exception message (which includes "boom")
    assert "boom" not in all_log_text


async def test_invoke_hook_timeout_log_excludes_sensitive_data(caplog):
    """Log on timeout contains plugin_key/hook_name only; no sensitive data."""
    def slow_callback(**kwargs):
        time.sleep(0.3)

    service = _build_service(settings=_make_settings(plugin_hook_timeout_seconds=0.1))
    _set_hooks(service, "on_session_start", [
        _hook_reg("p1", "on_session_start", slow_callback),
    ])

    with caplog.at_level(logging.WARNING, logger="app.application.plugin_service"):
        await service.invoke_hook(
            "on_session_start",
            session_id="s1",
            metadata={"user_input": "secret message"},
            trusted_metadata={"api_key": "sk-abc123"},
        )

    all_log_text = " ".join(r.message for r in caplog.records)
    assert "p1" in all_log_text
    assert "on_session_start" in all_log_text
    assert "TimeoutError" in all_log_text or "timed out" in all_log_text.lower()
    assert "secret message" not in all_log_text
    assert "sk-abc123" not in all_log_text


async def test_invoke_hook_sync_timeout_late_result_discarded():
    """After a sync timeout, the late result from the thread is NOT included."""
    late_box: list[str] = []

    def slow_callback(**kwargs):
        time.sleep(0.3)
        late_box.append("late")
        return "late"

    async def fast_callback(**kwargs):
        return "fast"

    service = _build_service(settings=_make_settings(plugin_hook_timeout_seconds=0.1))
    _set_hooks(service, "pre_llm_call", [
        _hook_reg("p1", "pre_llm_call", slow_callback),
        _hook_reg("p2", "pre_llm_call", fast_callback),
    ])
    result = await service.invoke_hook("pre_llm_call", session_id="s1")

    # Only fast_callback's "fast" is in the result; late "late" is discarded
    assert result == ["fast"]
    # Wait for the late thread to finish appending to avoid test teardown noise
    await asyncio.sleep(0.4)
    assert late_box == ["late"]  # thread did eventually complete, but result was discarded


# ---------------------------------------------------------------------------
# S3: Return contracts
# ---------------------------------------------------------------------------


async def test_observer_hooks_ignore_all_returns():
    """For observer hooks, all returns are ignored; invoke_hook returns []."""
    def callback1(**kwargs):
        return "ignored"

    def callback2(**kwargs):
        return {"also": "ignored"}

    service = _build_service()
    _set_hooks(service, "on_session_start", [
        _hook_reg("p1", "on_session_start", callback1),
        _hook_reg("p2", "on_session_start", callback2),
    ])
    result = await service.invoke_hook("on_session_start")
    assert result == []


async def test_all_observer_hooks_return_empty():
    """Every observer hook name returns [] regardless of callback returns."""
    observer_hooks = [
        "on_session_start", "on_session_end",
        "on_turn_start", "on_turn_end",
        "post_llm_call",
        "pre_tool_call", "post_tool_call",
        "on_pre_compress", "pre_finalize",
    ]
    for hook_name in observer_hooks:
        service = _build_service()
        _set_hooks(service, hook_name, [
            _hook_reg("p1", hook_name, lambda **kw: "should-be-ignored"),
        ])
        result = await service.invoke_hook(hook_name)
        assert result == [], f"observer hook {hook_name} should return []"


async def test_pre_llm_call_merges_valid_string_contexts():
    def callback1(**kwargs):
        return "context1"

    def callback2(**kwargs):
        return "context2"

    service = _build_service()
    _set_hooks(service, "pre_llm_call", [
        _hook_reg("p1", "pre_llm_call", callback1),
        _hook_reg("p2", "pre_llm_call", callback2),
    ])
    result = await service.invoke_hook("pre_llm_call", session_id="s1")
    assert result == ["context1\n\ncontext2"]


async def test_pre_llm_call_merges_string_and_dict_context():
    def callback1(**kwargs):
        return "bare string"

    def callback2(**kwargs):
        return {"context": "dict context"}

    service = _build_service()
    _set_hooks(service, "pre_llm_call", [
        _hook_reg("p1", "pre_llm_call", callback1),
        _hook_reg("p2", "pre_llm_call", callback2),
    ])
    result = await service.invoke_hook("pre_llm_call", session_id="s1")
    assert result == ["bare string\n\ndict context"]


async def test_pre_llm_call_skips_invalid_returns():
    def valid_string(**kwargs):
        return "valid"

    def invalid_int(**kwargs):
        return 42

    def invalid_dict_no_context(**kwargs):
        return {"no_context": "x"}

    def invalid_dict_non_string_context(**kwargs):
        return {"context": 42}

    def empty_string(**kwargs):
        return ""

    def returns_none(**kwargs):
        return None

    service = _build_service()
    _set_hooks(service, "pre_llm_call", [
        _hook_reg("p1", "pre_llm_call", valid_string),
        _hook_reg("p2", "pre_llm_call", invalid_int),
        _hook_reg("p3", "pre_llm_call", invalid_dict_no_context),
        _hook_reg("p4", "pre_llm_call", invalid_dict_non_string_context),
        _hook_reg("p5", "pre_llm_call", empty_string),
        _hook_reg("p6", "pre_llm_call", returns_none),
    ])
    result = await service.invoke_hook("pre_llm_call", session_id="s1")
    assert result == ["valid"]


async def test_pre_llm_call_returns_empty_when_all_invalid():
    def returns_none(**kwargs):
        return None

    def returns_int(**kwargs):
        return 42

    service = _build_service()
    _set_hooks(service, "pre_llm_call", [
        _hook_reg("p1", "pre_llm_call", returns_none),
        _hook_reg("p2", "pre_llm_call", returns_int),
    ])
    result = await service.invoke_hook("pre_llm_call", session_id="s1")
    assert result == []


async def test_transform_tool_result_first_valid_non_none_wins():
    def returns_none(**kwargs):
        return None

    def returns_string(**kwargs):
        return "transformed"

    def returns_dict(**kwargs):
        return {"transformed": True}

    service = _build_service()
    _set_hooks(service, "transform_tool_result", [
        _hook_reg("p1", "transform_tool_result", returns_none),
        _hook_reg("p2", "transform_tool_result", returns_string),
        _hook_reg("p3", "transform_tool_result", returns_dict),
    ])
    result = await service.invoke_hook("transform_tool_result", session_id="s1")
    assert result == ["transformed"]


async def test_transform_tool_result_stops_after_first_valid():
    called: list[str] = []

    def returns_none(**kwargs):
        called.append("p1")
        return None

    def returns_value(**kwargs):
        called.append("p2")
        return "first"

    def should_not_be_called(**kwargs):
        called.append("p3")
        return "second"

    service = _build_service()
    _set_hooks(service, "transform_tool_result", [
        _hook_reg("p1", "transform_tool_result", returns_none),
        _hook_reg("p2", "transform_tool_result", returns_value),
        _hook_reg("p3", "transform_tool_result", should_not_be_called),
    ])
    result = await service.invoke_hook("transform_tool_result", session_id="s1")
    assert result == ["first"]
    assert called == ["p1", "p2"]  # p3 was NOT called


async def test_transform_tool_result_accepts_dict_list_scalar():
    """transform_tool_result accepts string, dict, list, and scalar values."""
    test_values = [
        "string",
        {"key": "val"},
        [1, 2, 3],
        42,
        3.14,
        True,
    ]
    for value in test_values:
        captured = value
        service = _build_service()

        def make_cb(v):
            def cb(**kwargs):
                return v
            return cb

        _set_hooks(service, "transform_tool_result", [
            _hook_reg("p1", "transform_tool_result", make_cb(captured)),
        ])
        result = await service.invoke_hook("transform_tool_result", session_id="s1")
        assert result == [value], f"Failed for value: {value!r}"


async def test_transform_tool_result_returns_empty_when_all_none():
    service = _build_service()
    _set_hooks(service, "transform_tool_result", [
        _hook_reg("p1", "transform_tool_result", lambda **kw: None),
        _hook_reg("p2", "transform_tool_result", lambda **kw: None),
    ])
    result = await service.invoke_hook("transform_tool_result", session_id="s1")
    assert result == []


async def test_transform_llm_output_first_non_empty_string_wins():
    def returns_none(**kwargs):
        return None

    def returns_empty(**kwargs):
        return ""

    def returns_string(**kwargs):
        return "output"

    service = _build_service()
    _set_hooks(service, "transform_llm_output", [
        _hook_reg("p1", "transform_llm_output", returns_none),
        _hook_reg("p2", "transform_llm_output", returns_empty),
        _hook_reg("p3", "transform_llm_output", returns_string),
    ])
    result = await service.invoke_hook("transform_llm_output", session_id="s1")
    assert result == ["output"]


async def test_transform_llm_output_stops_after_first():
    called: list[str] = []

    def returns_none(**kwargs):
        called.append("p1")
        return None

    def returns_string(**kwargs):
        called.append("p2")
        return "output"

    def should_not_be_called(**kwargs):
        called.append("p3")
        return "second"

    service = _build_service()
    _set_hooks(service, "transform_llm_output", [
        _hook_reg("p1", "transform_llm_output", returns_none),
        _hook_reg("p2", "transform_llm_output", returns_string),
        _hook_reg("p3", "transform_llm_output", should_not_be_called),
    ])
    result = await service.invoke_hook("transform_llm_output", session_id="s1")
    assert result == ["output"]
    assert called == ["p1", "p2"]


async def test_transform_llm_output_skips_empty_and_non_string():
    def returns_int(**kwargs):
        return 42

    def returns_empty(**kwargs):
        return ""

    def returns_dict(**kwargs):
        return {"key": "val"}

    def returns_string(**kwargs):
        return "output"

    service = _build_service()
    _set_hooks(service, "transform_llm_output", [
        _hook_reg("p1", "transform_llm_output", returns_int),
        _hook_reg("p2", "transform_llm_output", returns_empty),
        _hook_reg("p3", "transform_llm_output", returns_dict),
        _hook_reg("p4", "transform_llm_output", returns_string),
    ])
    result = await service.invoke_hook("transform_llm_output", session_id="s1")
    assert result == ["output"]


async def test_transform_llm_output_returns_empty_when_no_valid():
    service = _build_service()
    _set_hooks(service, "transform_llm_output", [
        _hook_reg("p1", "transform_llm_output", lambda **kw: None),
        _hook_reg("p2", "transform_llm_output", lambda **kw: ""),
        _hook_reg("p3", "transform_llm_output", lambda **kw: 42),
    ])
    result = await service.invoke_hook("transform_llm_output", session_id="s1")
    assert result == []


async def test_invoke_hook_passes_original_kwargs_to_callbacks():
    """kwargs passed by the caller (minus shallow-copied containers) reach callbacks."""
    received: dict = {}

    def callback(**kwargs):
        received.update(kwargs)

    service = _build_service()
    _set_hooks(service, "on_turn_start", [
        _hook_reg("p1", "on_turn_start", callback),
    ])
    await service.invoke_hook(
        "on_turn_start",
        session_id="s1",
        metadata={"k": "v"},
    )

    assert received["session_id"] == "s1"
    assert received["metadata"] == {"k": "v"}
    assert received["hook_schema_version"] == 1

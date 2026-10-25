"""Tests for OtelLinkConfigService (Application layer).

Fake in-memory Store/AuditSink implement the Domain protocols; no real
SQLite. Covers: current() fallback chain (no row -> env, StoreError/invalid
stored -> last-known-good -> env), version-monotonic cache protection
against late reads/writes, strict get_resolved(), update() merge/CAS/audit
semantics, and cancellation propagation.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from app.domain.otel_link_config import (
    OTEL_LINK_CONFIG_FIELDS,
    OtelLinkConfig,
    OtelLinkConfigAuditEvent,
    OtelLinkConfigConflictError,
    OtelLinkConfigOverrides,
    OtelLinkConfigStoreError,
    OtelLinkConfigValidationError,
    StoredOtelLinkConfig,
)

ENV = OtelLinkConfig(
    metric_base_url="https://metrics.example.com",
    log_base_url="https://logs.example.com",
    trace_base_url="https://traces.example.com",
    profile_base_url="https://profiles.example.com",
)


class FakeOtelLinkConfigStore:
    """In-memory OtelLinkConfigStore with CAS + injectable failures/gates.

    get_gates / save_gates: optional asyncio.Events awaited per call index
    AFTER the row snapshot/commit, to simulate late completions.
    """

    def __init__(self, stored: StoredOtelLinkConfig | None = None) -> None:
        self._stored = stored
        self.get_error: Exception | None = None
        self.get_gates: list[asyncio.Event] = []
        self.save_gates: list[asyncio.Event] = []
        self._get_calls = 0
        self._save_calls = 0

    def set_stored(self, stored: StoredOtelLinkConfig | None) -> None:
        self._stored = stored

    async def get(self) -> StoredOtelLinkConfig | None:
        idx = self._get_calls
        self._get_calls += 1
        if idx < len(self.get_gates) and self.get_gates[idx] is not None:
            snapshot = self._stored
            await self.get_gates[idx].wait()
            if self.get_error is not None:
                raise self.get_error
            return snapshot
        if self.get_error is not None:
            raise self.get_error
        return self._stored

    async def save(
        self, overrides: OtelLinkConfigOverrides, expected_version: int, updated_by: str
    ) -> StoredOtelLinkConfig:
        idx = self._save_calls
        self._save_calls += 1
        current_version = self._stored.version if self._stored is not None else 0
        if current_version != expected_version:
            raise OtelLinkConfigConflictError("version mismatch")
        saved = StoredOtelLinkConfig(
            overrides=overrides,
            version=current_version + 1,
            updated_at="2026-10-27T00:00:00+00:00",
            updated_by=updated_by,
        )
        self._stored = saved
        if idx < len(self.save_gates) and self.save_gates[idx] is not None:
            await self.save_gates[idx].wait()
        return saved


class FakeAuditSink:
    def __init__(self) -> None:
        self.events: list[OtelLinkConfigAuditEvent] = []
        self.error: Exception | None = None
        self.gate: asyncio.Event | None = None

    async def record(self, event: OtelLinkConfigAuditEvent) -> None:
        if self.gate is not None:
            gate = self.gate
            self.gate = None  # one-shot: only the first audit call parks
            await gate.wait()
        if self.error is not None:
            raise self.error
        self.events.append(event)


def _make_service(store, sink=None, env: OtelLinkConfig = ENV):
    from app.application.otel_link_config_service import OtelLinkConfigService

    return OtelLinkConfigService(
        store=store,
        audit_sink=sink if sink is not None else FakeAuditSink(),
        metric_base_url=env.metric_base_url,
        log_base_url=env.log_base_url,
        trace_base_url=env.trace_base_url,
        profile_base_url=env.profile_base_url,
    )


def _stored(overrides: OtelLinkConfigOverrides, version: int = 1) -> StoredOtelLinkConfig:
    return StoredOtelLinkConfig(
        overrides=overrides,
        version=version,
        updated_at="2026-10-27T00:00:00+00:00",
        updated_by="tester",
    )


# --- current(): runtime read -------------------------------------------------


async def test_current_no_row_falls_back_to_env() -> None:
    svc = _make_service(FakeOtelLinkConfigStore())
    assert await svc.current() == ENV


async def test_current_override_wins_and_empty_string_disables() -> None:
    store = FakeOtelLinkConfigStore(
        _stored(OtelLinkConfigOverrides(metric_base_url="https://m.example.com", log_base_url=""))
    )
    svc = _make_service(store)
    cfg = await svc.current()
    assert cfg.metric_base_url == "https://m.example.com"
    assert cfg.log_base_url == ""
    assert cfg.trace_base_url == ENV.trace_base_url
    assert cfg.profile_base_url == ENV.profile_base_url


async def test_current_store_error_without_cache_falls_back_to_env(caplog) -> None:
    store = FakeOtelLinkConfigStore()
    store.get_error = OtelLinkConfigStoreError("db down")
    svc = _make_service(store)
    with caplog.at_level(logging.WARNING):
        cfg = await svc.current()
    assert cfg == ENV
    assert any("otel" in r.message.lower() for r in caplog.records)


async def test_current_store_error_uses_last_known_good() -> None:
    store = FakeOtelLinkConfigStore(
        _stored(OtelLinkConfigOverrides(metric_base_url="https://cached.example.com"))
    )
    svc = _make_service(store)
    assert (await svc.current()).metric_base_url == "https://cached.example.com"
    store.get_error = OtelLinkConfigStoreError("db down")
    cfg = await svc.current()
    assert cfg.metric_base_url == "https://cached.example.com"


async def test_current_invalid_stored_value_degrades_to_last_known_good() -> None:
    store = FakeOtelLinkConfigStore(
        _stored(OtelLinkConfigOverrides(metric_base_url="https://good.example.com"), version=1)
    )
    svc = _make_service(store)
    assert (await svc.current()).metric_base_url == "https://good.example.com"
    # Simulate semantic corruption written outside the service.
    store.set_stored(
        _stored(OtelLinkConfigOverrides(metric_base_url="ftp://bad.example.com"), version=2)
    )
    cfg = await svc.current()
    assert cfg.metric_base_url == "https://good.example.com"


async def test_current_invalid_stored_value_without_cache_falls_back_to_env() -> None:
    store = FakeOtelLinkConfigStore(
        _stored(OtelLinkConfigOverrides(metric_base_url="ftp://bad.example.com"), version=1)
    )
    svc = _make_service(store)
    assert await svc.current() == ENV


async def test_current_recovers_after_failed_read() -> None:
    store = FakeOtelLinkConfigStore(
        _stored(OtelLinkConfigOverrides(metric_base_url="https://db.example.com"), version=1)
    )
    svc = _make_service(store)
    store.get_error = OtelLinkConfigStoreError("db down")
    assert (await svc.current()).metric_base_url == ENV.metric_base_url
    store.get_error = None
    cfg = await svc.current()
    assert cfg.metric_base_url == "https://db.example.com"


async def test_current_late_old_read_does_not_regress_cache() -> None:
    gate = asyncio.Event()
    store = FakeOtelLinkConfigStore(
        _stored(OtelLinkConfigOverrides(metric_base_url="https://v1.example.com"), version=1)
    )
    store.get_gates = [gate]
    svc = _make_service(store)
    read_task = asyncio.create_task(svc.current())
    await asyncio.sleep(0)  # let the read capture the v1 snapshot and park
    await svc.update({"metric_base_url": "https://v2.example.com"}, 1, "tester")
    gate.set()
    await read_task
    # Cut off the store: the cache must serve the newer v2, not the late v1.
    store.get_error = OtelLinkConfigStoreError("db down")
    assert (await svc.current()).metric_base_url == "https://v2.example.com"


async def test_current_late_no_row_read_does_not_regress_cache() -> None:
    gate = asyncio.Event()
    store = FakeOtelLinkConfigStore()
    store.get_gates = [gate]
    svc = _make_service(store)
    read_task = asyncio.create_task(svc.current())
    await asyncio.sleep(0)
    await svc.update({"metric_base_url": "https://v1.example.com"}, 0, "tester")
    gate.set()
    await read_task
    store.get_error = OtelLinkConfigStoreError("db down")
    assert (await svc.current()).metric_base_url == "https://v1.example.com"


async def test_update_late_save_completion_does_not_regress_cache() -> None:
    save_gate = asyncio.Event()
    store = FakeOtelLinkConfigStore()
    store.save_gates = [save_gate, None]
    svc = _make_service(store)
    first = asyncio.create_task(svc.update({"metric_base_url": "https://v1.example.com"}, 0, "a"))
    await asyncio.sleep(0)  # first save committed v1, parked on the gate
    await svc.update({"metric_base_url": "https://v2.example.com"}, 1, "b")
    save_gate.set()
    await first
    store.get_error = OtelLinkConfigStoreError("db down")
    assert (await svc.current()).metric_base_url == "https://v2.example.com"


async def test_current_cancellation_propagates() -> None:
    store = FakeOtelLinkConfigStore()
    store.get_error = asyncio.CancelledError()
    svc = _make_service(store)
    with pytest.raises(asyncio.CancelledError):
        await svc.current()


async def test_current_does_not_validate_env_base_against_new_rules() -> None:
    long_env = OtelLinkConfig(
        metric_base_url="https://metrics.example.com/" + "a" * 3000,
        log_base_url="",
        trace_base_url="",
        profile_base_url="",
    )
    svc = _make_service(FakeOtelLinkConfigStore(), env=long_env)
    assert await svc.current() == long_env


async def test_warning_logs_do_not_leak_urls(caplog) -> None:
    secret = "secret-token-xyz"
    store = FakeOtelLinkConfigStore(
        _stored(
            OtelLinkConfigOverrides(
                metric_base_url=f"https://m.example.com/path?token={secret}"
            ),
            version=1,
        )
    )
    svc = _make_service(store)
    await svc.current()
    store.get_error = OtelLinkConfigStoreError("db down")
    with caplog.at_level(logging.WARNING):
        await svc.current()
    assert secret not in caplog.text
    assert "https://m.example.com/path" not in caplog.text


# --- get_resolved(): strict management read ----------------------------------


async def test_get_resolved_no_row_returns_version_zero_sentinel() -> None:
    svc = _make_service(FakeOtelLinkConfigStore())
    resolved = await svc.get_resolved()
    assert resolved.config == ENV
    assert resolved.version == 0
    assert resolved.overridden_fields == ()
    assert resolved.updated_at is None
    assert resolved.updated_by is None


async def test_get_resolved_returns_stored_metadata() -> None:
    store = FakeOtelLinkConfigStore(
        _stored(OtelLinkConfigOverrides(metric_base_url="https://m.example.com"), version=3)
    )
    svc = _make_service(store)
    resolved = await svc.get_resolved()
    assert resolved.version == 3
    assert resolved.overridden_fields == ("metric_base_url",)
    assert resolved.config.metric_base_url == "https://m.example.com"
    assert resolved.updated_by == "tester"


async def test_get_resolved_corrupted_data_raises_store_error() -> None:
    store = FakeOtelLinkConfigStore(
        _stored(OtelLinkConfigOverrides(metric_base_url="ftp://bad.example.com"), version=1)
    )
    svc = _make_service(store)
    with pytest.raises(OtelLinkConfigStoreError):
        await svc.get_resolved()


async def test_get_resolved_store_error_propagates() -> None:
    store = FakeOtelLinkConfigStore()
    store.get_error = OtelLinkConfigStoreError("db down")
    svc = _make_service(store)
    with pytest.raises(OtelLinkConfigStoreError):
        await svc.get_resolved()


# --- update(): validation -----------------------------------------------------


async def test_update_rejects_empty_patch() -> None:
    svc = _make_service(FakeOtelLinkConfigStore())
    with pytest.raises(OtelLinkConfigValidationError):
        await svc.update({}, 0, "tester")


async def test_update_rejects_unknown_field() -> None:
    svc = _make_service(FakeOtelLinkConfigStore())
    with pytest.raises(OtelLinkConfigValidationError):
        await svc.update({"unknown_field": "https://x.example.com"}, 0, "tester")


@pytest.mark.parametrize("bad_version", [True, -1, "1", 1.5, None])
async def test_update_rejects_invalid_expected_version(bad_version) -> None:
    svc = _make_service(FakeOtelLinkConfigStore())
    with pytest.raises(OtelLinkConfigValidationError):
        await svc.update({"metric_base_url": "https://x.example.com"}, bad_version, "tester")


@pytest.mark.parametrize("bad_value", [123, True, 1.5, ["x"]])
async def test_update_rejects_non_str_non_none_field_value(bad_value) -> None:
    svc = _make_service(FakeOtelLinkConfigStore())
    with pytest.raises(OtelLinkConfigValidationError):
        await svc.update({"metric_base_url": bad_value}, 0, "tester")


@pytest.mark.parametrize("bad_actor", ["", None, 123])
async def test_update_rejects_invalid_updated_by(bad_actor) -> None:
    svc = _make_service(FakeOtelLinkConfigStore())
    with pytest.raises(OtelLinkConfigValidationError):
        await svc.update({"metric_base_url": "https://x.example.com"}, 0, bad_actor)


async def test_update_rejects_invalid_url() -> None:
    svc = _make_service(FakeOtelLinkConfigStore())
    with pytest.raises(OtelLinkConfigValidationError):
        await svc.update({"metric_base_url": "ftp://bad.example.com"}, 0, "tester")


# --- update(): merge / CAS / response ----------------------------------------


async def test_update_partial_preserves_other_overrides() -> None:
    store = FakeOtelLinkConfigStore(
        _stored(OtelLinkConfigOverrides(log_base_url="https://logs-x.example.com"), version=1)
    )
    svc = _make_service(store)
    resolved = await svc.update({"metric_base_url": "https://m.example.com"}, 1, "tester")
    assert resolved.config.metric_base_url == "https://m.example.com"
    assert resolved.config.log_base_url == "https://logs-x.example.com"
    assert set(resolved.overridden_fields) == {"metric_base_url", "log_base_url"}


async def test_update_none_clears_override_back_to_env() -> None:
    store = FakeOtelLinkConfigStore(
        _stored(OtelLinkConfigOverrides(metric_base_url="https://m.example.com"), version=1)
    )
    svc = _make_service(store)
    resolved = await svc.update({"metric_base_url": None}, 1, "tester")
    assert resolved.config.metric_base_url == ENV.metric_base_url
    assert resolved.overridden_fields == ()


async def test_update_empty_string_disables_link() -> None:
    svc = _make_service(FakeOtelLinkConfigStore())
    resolved = await svc.update({"log_base_url": ""}, 0, "tester")
    assert resolved.config.log_base_url == ""
    assert resolved.overridden_fields == ("log_base_url",)


async def test_update_refuses_to_silently_repair_corrupt_stored_overrides() -> None:
    # Patch overwrites the same bad field -- still a StoreError, not a silent fix.
    store = FakeOtelLinkConfigStore(
        _stored(OtelLinkConfigOverrides(metric_base_url="ftp://bad.example.com"), version=1)
    )
    svc = _make_service(store)
    with pytest.raises(OtelLinkConfigStoreError):
        await svc.update({"metric_base_url": "https://good.example.com"}, 1, "tester")


async def test_update_cas_conflict_propagates() -> None:
    store = FakeOtelLinkConfigStore(_stored(OtelLinkConfigOverrides(), version=5))
    svc = _make_service(store)
    with pytest.raises(OtelLinkConfigConflictError):
        await svc.update({"metric_base_url": "https://x.example.com"}, 1, "tester")


async def test_update_response_uses_own_candidate_and_saved_version() -> None:
    sink = FakeAuditSink()
    audit_gate = asyncio.Event()
    sink.gate = audit_gate
    store = FakeOtelLinkConfigStore()
    svc = _make_service(store, sink=sink)
    first = asyncio.create_task(
        svc.update({"metric_base_url": "https://first.example.com"}, 0, "a")
    )
    await asyncio.sleep(0)  # first committed v1, now parked in audit
    second = await svc.update({"metric_base_url": "https://second.example.com"}, 1, "b")
    audit_gate.set()
    first_resolved = await first
    assert second.version == 2
    assert first_resolved.version == 1
    assert first_resolved.config.metric_base_url == "https://first.example.com"
    assert first_resolved.updated_by == "a"


async def test_update_only_validates_overridden_fields_not_env() -> None:
    long_env = OtelLinkConfig(
        metric_base_url="https://metrics.example.com/" + "a" * 3000,
        log_base_url="",
        trace_base_url="",
        profile_base_url="",
    )
    svc = _make_service(FakeOtelLinkConfigStore(), env=long_env)
    resolved = await svc.update({"log_base_url": "https://logs.example.com"}, 0, "tester")
    assert resolved.config.metric_base_url == long_env.metric_base_url
    assert resolved.config.log_base_url == "https://logs.example.com"


# --- update(): audit ----------------------------------------------------------


async def test_update_records_audit_event_with_field_states() -> None:
    sink = FakeAuditSink()
    store = FakeOtelLinkConfigStore(
        _stored(OtelLinkConfigOverrides(metric_base_url="https://old.example.com"), version=4)
    )
    svc = _make_service(store, sink=sink)
    await svc.update({"metric_base_url": "https://new.example.com"}, 4, "dashboard-local")
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.actor == "dashboard-local"
    assert event.old_version == 4
    assert event.new_version == 5
    before, after = event.changed_fields["metric_base_url"]
    assert before.override == "custom" and before.url == "https://old.example.com"
    assert after.override == "custom" and after.url == "https://new.example.com"


async def test_update_records_env_to_same_value_custom_change() -> None:
    sink = FakeAuditSink()
    svc = _make_service(FakeOtelLinkConfigStore(), sink=sink)
    await svc.update({"metric_base_url": ENV.metric_base_url}, 0, "tester")
    before, after = sink.events[0].changed_fields["metric_base_url"]
    assert before.override == "env"
    assert after.override == "custom"
    assert before.url == after.url


async def test_update_records_custom_to_env_and_custom_to_disabled() -> None:
    sink = FakeAuditSink()
    store = FakeOtelLinkConfigStore(
        _stored(
            OtelLinkConfigOverrides(
                metric_base_url="https://m.example.com", log_base_url="https://l.example.com"
            ),
            version=1,
        )
    )
    svc = _make_service(store, sink=sink)
    await svc.update({"metric_base_url": None, "log_base_url": ""}, 1, "tester")
    m_before, m_after = sink.events[0].changed_fields["metric_base_url"]
    assert (m_before.override, m_after.override) == ("custom", "env")
    l_before, l_after = sink.events[0].changed_fields["log_base_url"]
    assert (l_before.override, l_after.override) == ("custom", "disabled")
    assert l_after.url == ""


async def test_update_records_path_query_only_change_and_redacts_secret() -> None:
    secret = "secret-token-xyz"
    sink = FakeAuditSink()
    store = FakeOtelLinkConfigStore(
        _stored(OtelLinkConfigOverrides(metric_base_url="https://m.example.com/a"), version=1)
    )
    svc = _make_service(store, sink=sink)
    await svc.update(
        {"metric_base_url": f"https://m.example.com/b?token={secret}"}, 1, "tester"
    )
    before, after = sink.events[0].changed_fields["metric_base_url"]
    # Projections carry scheme://host only: path/query-only changes still recorded.
    assert before.url == after.url == "https://m.example.com"
    assert secret not in repr(sink.events[0])


async def test_update_noop_does_not_emit_audit_event() -> None:
    sink = FakeAuditSink()
    store = FakeOtelLinkConfigStore(
        _stored(OtelLinkConfigOverrides(metric_base_url="https://m.example.com"), version=1)
    )
    svc = _make_service(store, sink=sink)
    await svc.update({"metric_base_url": "https://m.example.com"}, 1, "tester")
    assert sink.events == []


async def test_update_audit_failure_does_not_roll_back(caplog) -> None:
    sink = FakeAuditSink()
    sink.error = RuntimeError("sink down")
    store = FakeOtelLinkConfigStore()
    svc = _make_service(store, sink=sink)
    with caplog.at_level(logging.WARNING):
        resolved = await svc.update({"metric_base_url": "https://m.example.com"}, 0, "tester")
    assert resolved.version == 1
    assert (await svc.current()).metric_base_url == "https://m.example.com"
    assert any("audit" in r.message.lower() for r in caplog.records)


# --- hot reload across instances ----------------------------------------------


async def test_two_service_instances_share_store_hot_reload() -> None:
    store = FakeOtelLinkConfigStore()
    svc_a = _make_service(store)
    svc_b = _make_service(store)
    assert (await svc_b.current()).metric_base_url == ENV.metric_base_url
    await svc_a.update({"metric_base_url": "https://hot.example.com"}, 0, "tester")
    assert (await svc_b.current()).metric_base_url == "https://hot.example.com"


async def test_failed_read_does_not_update_cache() -> None:
    store = FakeOtelLinkConfigStore(
        _stored(OtelLinkConfigOverrides(metric_base_url="https://v1.example.com"), version=1)
    )
    svc = _make_service(store)
    await svc.current()
    store.get_error = OtelLinkConfigStoreError("db down")
    await svc.current()
    store.get_error = None
    store.set_stored(
        _stored(OtelLinkConfigOverrides(metric_base_url="https://v2.example.com"), version=2)
    )
    assert (await svc.current()).metric_base_url == "https://v2.example.com"

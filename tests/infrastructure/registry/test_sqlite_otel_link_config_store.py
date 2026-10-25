import asyncio
import json
import sqlite3
import threading

import pytest

from app.domain.otel_link_config import (
    OtelLinkConfigConflictError,
    OtelLinkConfigOverrides,
    OtelLinkConfigStoreError,
)
from app.infrastructure.registry.sqlite_otel_link_config_store import (
    SqliteOtelLinkConfigStore,
)

VALID_TS = "2026-10-27T10:00:00+00:00"


def _store(tmp_path, **kwargs):
    return SqliteOtelLinkConfigStore(str(tmp_path / "sessions.db"), **kwargs)


def _insert_row(db_path, config_json, version, updated_at, updated_by):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO otel_link_config (id, config_json, version, updated_at, updated_by) "
            "VALUES (1, ?, ?, ?, ?)",
            (config_json, version, updated_at, updated_by),
        )
        conn.commit()


def _make_unchecked_db(tmp_path):
    """Build a DB whose otel_link_config table has no CHECK constraints, so
    illegal version values can be injected."""
    db_path = tmp_path / "unchecked.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE otel_link_config ("
            "id INTEGER PRIMARY KEY, config_json TEXT NOT NULL, version INTEGER NOT NULL, "
            "updated_at TEXT NOT NULL, updated_by TEXT NOT NULL)"
        )
        conn.commit()
    return str(db_path)


# -- basic get/save ---------------------------------------------------------


@pytest.mark.asyncio
async def test_get_no_row_returns_none(tmp_path):
    store = _store(tmp_path)
    assert await store.get() is None


@pytest.mark.asyncio
async def test_first_save_returns_version_1(tmp_path):
    store = _store(tmp_path)
    ov = OtelLinkConfigOverrides(metric_base_url="https://metrics.example.com")
    result = await store.save(ov, expected_version=0, updated_by="dashboard-local")
    assert result.version == 1
    assert result.overrides.metric_base_url == "https://metrics.example.com"
    assert result.overrides.log_base_url is None
    assert result.updated_by == "dashboard-local"
    read = await store.get()
    assert read is not None and read.version == 1
    assert read.overrides.metric_base_url == "https://metrics.example.com"


@pytest.mark.asyncio
async def test_first_save_conflict_when_row_exists(tmp_path):
    store = _store(tmp_path)
    await store.save(OtelLinkConfigOverrides(metric_base_url="https://a.example.com"), 0, "u")
    with pytest.raises(OtelLinkConfigConflictError):
        await store.save(OtelLinkConfigOverrides(metric_base_url="https://b.example.com"), 0, "u")


@pytest.mark.asyncio
async def test_cas_mismatch_raises_and_no_partial_commit(tmp_path):
    store = _store(tmp_path)
    await store.save(OtelLinkConfigOverrides(metric_base_url="https://a.example.com"), 0, "u")
    with pytest.raises(OtelLinkConfigConflictError):
        await store.save(
            OtelLinkConfigOverrides(metric_base_url="https://evil.example.com"), 999, "attacker"
        )
    read = await store.get()
    assert read is not None
    assert read.version == 1
    assert read.overrides.metric_base_url == "https://a.example.com"
    assert read.updated_by == "u"


@pytest.mark.asyncio
async def test_consecutive_saves_increment_version_and_empty_overrides_keep_row(tmp_path):
    store = _store(tmp_path)
    s1 = await store.save(OtelLinkConfigOverrides(metric_base_url="https://a.example.com"), 0, "u")
    assert s1.version == 1
    s2 = await store.save(
        OtelLinkConfigOverrides(trace_base_url="https://t.example.com"), s1.version, "u2"
    )
    assert s2.version == 2
    # restore to defaults: empty overrides, row not deleted, version keeps incrementing
    s3 = await store.save(OtelLinkConfigOverrides(), s2.version, "u3")
    assert s3.version == 3
    read = await store.get()
    assert read is not None
    assert read.version == 3
    assert read.overrides.to_dict() == {}


@pytest.mark.asyncio
async def test_update_when_no_row_raises_conflict(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(OtelLinkConfigConflictError):
        await store.save(OtelLinkConfigOverrides(metric_base_url="https://a.example.com"), 5, "u")


# -- strict parsing of corrupt data -----------------------------------------


@pytest.mark.asyncio
async def test_corrupt_non_json_raises_store_error(tmp_path):
    store = _store(tmp_path)
    _insert_row(store.path, "not-json{{{", 1, VALID_TS, "u")
    with pytest.raises(OtelLinkConfigStoreError):
        await store.get()


@pytest.mark.asyncio
async def test_corrupt_non_object_json_raises(tmp_path):
    store = _store(tmp_path)
    _insert_row(store.path, json.dumps(["https://a.example.com"]), 1, VALID_TS, "u")
    with pytest.raises(OtelLinkConfigStoreError):
        await store.get()


@pytest.mark.asyncio
async def test_corrupt_unknown_key_raises(tmp_path):
    store = _store(tmp_path)
    _insert_row(store.path, json.dumps({"unknown_field": "x"}), 1, VALID_TS, "u")
    with pytest.raises(OtelLinkConfigStoreError):
        await store.get()


@pytest.mark.asyncio
async def test_corrupt_non_str_value_raises(tmp_path):
    store = _store(tmp_path)
    _insert_row(store.path, json.dumps({"metric_base_url": 123}), 1, VALID_TS, "u")
    with pytest.raises(OtelLinkConfigStoreError):
        await store.get()


# Note: bool-as-version and int-as-updated_by cannot be injected through
# SQLite (INTEGER affinity coerces True->1; TEXT affinity coerces 42->'42'),
# so those type violations are only reachable via in-memory rows, not stored
# ones. The store still guards them; the injectable illegal values are
# version<=0 and empty updated_by.
@pytest.mark.asyncio
@pytest.mark.parametrize("bad_version", [0, -3])
async def test_corrupt_version_raises(tmp_path, bad_version):
    db_path = _make_unchecked_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO otel_link_config (id, config_json, version, updated_at, updated_by) "
            "VALUES (1, '{}', ?, ?, 'u')",
            (bad_version, VALID_TS),
        )
        conn.commit()
    store = SqliteOtelLinkConfigStore(db_path, initialize=False)
    with pytest.raises(OtelLinkConfigStoreError):
        await store.get()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_updated_by", [""])
async def test_corrupt_updated_by_raises(tmp_path, bad_updated_by):
    store = _store(tmp_path)
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "INSERT INTO otel_link_config (id, config_json, version, updated_at, updated_by) "
            "VALUES (1, '{}', 1, ?, ?)",
            (VALID_TS, bad_updated_by),
        )
        conn.commit()
    with pytest.raises(OtelLinkConfigStoreError):
        await store.get()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_ts",
    [
        123,  # not a string
        "not-a-timestamp",  # not ISO 8601
        "2026-10-27T10:00:00",  # naive, no timezone
        "2026-10-27T18:00:00+08:00",  # non-UTC offset
    ],
)
async def test_corrupt_updated_at_raises(tmp_path, bad_ts):
    store = _store(tmp_path)
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "INSERT INTO otel_link_config (id, config_json, version, updated_at, updated_by) "
            "VALUES (1, '{}', 1, ?, 'u')",
            (bad_ts,),
        )
        conn.commit()
    with pytest.raises(OtelLinkConfigStoreError):
        await store.get()


# -- concurrency / multi-instance -------------------------------------------


def test_concurrent_first_write_exactly_one_wins(tmp_path):
    db = str(tmp_path / "sessions.db")
    store_a = SqliteOtelLinkConfigStore(db)
    store_b = SqliteOtelLinkConfigStore(db)
    barrier = threading.Barrier(2)
    results: list[object] = []

    def attempt(store):
        barrier.wait()
        try:
            results.append(
                asyncio.run(
                    store.save(
                        OtelLinkConfigOverrides(metric_base_url="https://a.example.com"),
                        0,
                        "u",
                    )
                )
            )
        except OtelLinkConfigConflictError as exc:
            results.append(exc)

    t1 = threading.Thread(target=attempt, args=(store_a,))
    t2 = threading.Thread(target=attempt, args=(store_b,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    conflicts = [r for r in results if isinstance(r, OtelLinkConfigConflictError)]
    successes = [r for r in results if not isinstance(r, OtelLinkConfigConflictError)]
    assert len(conflicts) == 1 and len(successes) == 1
    assert successes[0].version == 1


@pytest.mark.asyncio
async def test_two_instances_share_db(tmp_path):
    db = str(tmp_path / "sessions.db")
    writer = SqliteOtelLinkConfigStore(db)
    reader = SqliteOtelLinkConfigStore(db, initialize=False)
    await writer.save(OtelLinkConfigOverrides(log_base_url="https://logs.example.com"), 0, "u")
    read = await reader.get()
    assert read is not None
    assert read.overrides.log_base_url == "https://logs.example.com"


# -- error wrapping / connection management ----------------------------------


@pytest.mark.asyncio
async def test_store_error_does_not_leak_raw_value(tmp_path):
    store = _store(tmp_path)
    secret = "https://secret-token.example.com/path?token=abc"
    _insert_row(store.path, secret, 1, VALID_TS, "u")  # raw URL, not JSON
    with pytest.raises(OtelLinkConfigStoreError) as exc_info:
        await store.get()
    assert secret not in str(exc_info.value)
    assert "abc" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_connect_failure_wrapped_as_store_error(tmp_path):
    # read_only on a missing DB file makes open_sqlite (mode=ro) raise
    # sqlite3.OperationalError at connect time; both get and save must wrap
    # it as OtelLinkConfigStoreError, never propagate it raw.
    missing = str(tmp_path / "missing.db")
    ro = SqliteOtelLinkConfigStore(missing, initialize=False, read_only=True)
    with pytest.raises(OtelLinkConfigStoreError):
        await ro.get()
    with pytest.raises(OtelLinkConfigStoreError) as exc_info:
        await ro.save(OtelLinkConfigOverrides(), 0, "u")
    assert not isinstance(exc_info.value, OtelLinkConfigConflictError)
    assert not isinstance(exc_info.value, sqlite3.Error)


@pytest.mark.asyncio
async def test_read_failure_wrapped_as_store_error(tmp_path):
    store = _store(tmp_path, initialize=False)  # table never created
    with pytest.raises(OtelLinkConfigStoreError) as exc_info:
        await store.get()
    assert not isinstance(exc_info.value, OtelLinkConfigConflictError)


@pytest.mark.asyncio
async def test_write_failure_on_readonly_db_wrapped_as_store_error(tmp_path):
    db = str(tmp_path / "sessions.db")
    SqliteOtelLinkConfigStore(db)  # create schema
    ro = SqliteOtelLinkConfigStore(db, initialize=False, read_only=True)
    with pytest.raises(OtelLinkConfigStoreError) as exc_info:
        await ro.save(OtelLinkConfigOverrides(metric_base_url="https://a.example.com"), 0, "u")
    assert not isinstance(exc_info.value, OtelLinkConfigConflictError)


@pytest.mark.asyncio
async def test_serialize_failure_wrapped_as_store_error(tmp_path, monkeypatch):
    store = _store(tmp_path)

    class Boom:
        def to_dict(self):
            raise RuntimeError("boom-internal-detail")

    with pytest.raises(OtelLinkConfigStoreError) as exc_info:
        await store.save(Boom(), 0, "u")
    assert "boom-internal-detail" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_save_illegal_expected_version_raises(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(OtelLinkConfigStoreError):
        await store.save(OtelLinkConfigOverrides(), -1, "u")
    with pytest.raises(OtelLinkConfigStoreError):
        await store.save(OtelLinkConfigOverrides(), True, "u")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_save_empty_updated_by_raises(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(OtelLinkConfigStoreError):
        await store.save(OtelLinkConfigOverrides(), 0, "")


def test_idempotent_schema_init(tmp_path):
    db = str(tmp_path / "sessions.db")
    SqliteOtelLinkConfigStore(db)
    SqliteOtelLinkConfigStore(db)

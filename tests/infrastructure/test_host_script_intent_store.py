import pytest


@pytest.mark.asyncio
async def test_intents_persist_and_cas_only_latest(tmp_path):
    from app.infrastructure.host_terminal.script_intent_store import (
        SQLiteHostScriptIntentStore,
    )

    store = SQLiteHostScriptIntentStore(tmp_path / "intents.db")
    first = await store.prepare(
        "demo", "scripts/run.py", "a" * 64, "foreground", floor=10
    )
    second = await store.prepare(
        "demo", "scripts/run.py", "b" * 64, "foreground", floor=first.revision
    )
    assert second.revision > first.revision > 10
    assert not await store.transition(first.revision, "prepared", "pending")
    assert await store.transition(second.revision, "prepared", "pending")
    restarted = SQLiteHostScriptIntentStore(tmp_path / "intents.db")
    assert [(i.revision, i.state) for i in await restarted.list_current()] == [
        (second.revision, "pending")
    ]


@pytest.mark.asyncio
async def test_failed_prepared_write_does_not_hide_revocable_active(tmp_path):
    from app.infrastructure.host_terminal.script_intent_store import (
        SQLiteHostScriptIntentStore,
    )

    store = SQLiteHostScriptIntentStore(tmp_path / "db")
    active = await store.prepare("demo", "scripts/run.py", "a" * 64, "foreground")
    await store.transition(active.revision, "prepared", "pending")
    await store.transition(active.revision, "pending", "active")
    await store.prepare("demo", "scripts/run.py", "b" * 64, "foreground")
    assert (await store.list_current())[0].revision == active.revision
    assert await store.transition(active.revision, "active", "pending_revoke")


@pytest.mark.asyncio
async def test_old_pending_response_cannot_resurrect_replaced_intent(tmp_path):
    from app.infrastructure.host_terminal.script_intent_store import (
        SQLiteHostScriptIntentStore,
    )

    store = SQLiteHostScriptIntentStore(tmp_path / "db")
    first = await store.prepare("demo", "scripts/run.py", "a" * 64, "foreground")
    await store.transition(first.revision, "prepared", "pending")
    second = await store.prepare("demo", "scripts/run.py", "b" * 64, "foreground")
    await store.transition(second.revision, "prepared", "pending")
    assert not await store.transition(first.revision, "pending", "active")
    assert await store.transition(second.revision, "pending", "pending_revoke")
    assert not await store.transition(second.revision, "pending", "active")

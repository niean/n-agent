import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.infrastructure.host_terminal.script_intent_store import (
    SQLiteHostScriptIntentStore,
)


@pytest.mark.asyncio
async def test_prepared_never_syncs_pending_retries_exact_payload_and_revokes(tmp_path):
    from app.application.host_script_grant_service import HostScriptGrantService

    client = SimpleNamespace(script_grants=AsyncMock(return_value=[]))
    skills = SimpleNamespace(
        resolve_script_bytes=AsyncMock(
            return_value=SimpleNamespace(sha256=hashlib.sha256(b"hello").hexdigest())
        )
    )
    service = HostScriptGrantService(
        SQLiteHostScriptIntentStore(tmp_path / "db"), client, skills
    )
    intent = await service.prepare_write("demo", "scripts/a.py", "hello", "foreground")
    await service.sync()
    assert (
        client.script_grants.await_count == 1
    )  # prepare probes host, sync ignores prepared
    await service.commit_write(intent.revision)
    client.script_grants.side_effect = RuntimeError("/private/token")
    await service.sync()
    assert (await service.list_grants())[0]["error"] == "host_script_grants_unavailable"
    client.script_grants.side_effect = lambda op, grant=None: [grant] if grant else []
    await service.sync()
    assert (await service.list_grants())[0]["state"] == "active"
    await service.revoke(intent.revision)
    assert (await service.list_grants())[0]["state"] == "revoked"
    await service.sync()
    assert (await service.list_grants())[0]["state"] == "revoked"


@pytest.mark.asyncio
async def test_warning_and_external_hash_drift_block_sync(tmp_path):
    from app.application.host_script_grant_service import HostScriptGrantService

    client = SimpleNamespace(script_grants=AsyncMock(return_value=[]))
    skills = SimpleNamespace(
        resolve_script_bytes=AsyncMock(side_effect=ValueError("blocked"))
    )
    service = HostScriptGrantService(
        SQLiteHostScriptIntentStore(tmp_path / "db"), client, skills
    )
    intent = await service.prepare_write("demo", "scripts/a.py", "hello", "foreground")
    await service.commit_write(intent.revision)
    await service.sync()
    assert (await service.list_grants())[0]["error"] == "scan_blocked"
    skills.resolve_script_bytes.side_effect = None
    skills.resolve_script_bytes.return_value = SimpleNamespace(sha256="b" * 64)
    await service.sync()
    assert (await service.list_grants())[0]["error"] == "source_mismatch"
    assert client.script_grants.await_count == 1


@pytest.mark.asyncio
async def test_remote_revision_seed_after_local_database_reset_and_no_retry_rebase(
    tmp_path,
):
    from app.application.host_script_grant_service import HostScriptGrantService
    from app.domain.host_script_grant import HostScriptGrant

    remote_revision = 3_000_000_000_000_000
    remote = HostScriptGrant(
        "demo",
        "scripts/a.py",
        "a" * 64,
        remote_revision,
        "foreground",
        "2026-11-06T00:00:00Z",
        "revoked",
    )
    client = SimpleNamespace(script_grants=AsyncMock(return_value=[remote]))
    skills = SimpleNamespace(
        resolve_script_bytes=AsyncMock(
            return_value=SimpleNamespace(sha256=hashlib.sha256(b"hello").hexdigest())
        )
    )
    service = HostScriptGrantService(
        SQLiteHostScriptIntentStore(tmp_path / "reset.db"), client, skills
    )
    intent = await service.prepare_write("demo", "scripts/a.py", "hello", "foreground")
    assert intent.revision == remote_revision + 1
    await service.commit_write(intent.revision)
    client.script_grants.side_effect = ValueError("host_script_grants_stale_revision")
    await service.sync()
    row = (await service.list_grants())[0]
    assert row["revision"] == intent.revision and row["state"] == "pending"
    assert row["error"] == "host_script_grants_stale_revision"


@pytest.mark.asyncio
async def test_new_write_reauthorizes_after_revocation_and_old_revision_cannot_revoke(
    tmp_path,
):
    from app.application.host_script_grant_service import HostScriptGrantService

    client = SimpleNamespace(
        script_grants=AsyncMock(
            side_effect=lambda op, grant=None: [grant] if grant else []
        )
    )
    skills = SimpleNamespace(
        resolve_script_bytes=AsyncMock(
            return_value=SimpleNamespace(sha256=hashlib.sha256(b"A").hexdigest())
        )
    )
    service = HostScriptGrantService(
        SQLiteHostScriptIntentStore(tmp_path / "db"), client, skills
    )
    first = await service.prepare_write("demo", "scripts/a.py", "A", "foreground")
    await service.commit_write(first.revision)
    await service.sync()
    client.script_grants.side_effect = RuntimeError("token")
    result = await service.revoke(first.revision)
    assert not result["success"] and result["state"] == "pending_revoke"
    client.script_grants.side_effect = lambda op, grant=None: [grant] if grant else []
    await service.sync()
    second = await service.prepare_write("demo", "scripts/a.py", "B", "foreground")
    await service.commit_write(second.revision)
    skills.resolve_script_bytes.return_value.sha256 = hashlib.sha256(b"B").hexdigest()
    await service.sync()
    assert (await service.list_grants())[0]["state"] == "active"
    with pytest.raises(ValueError, match="stale_revision"):
        await service.revoke(first.revision)

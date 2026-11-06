from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.skill_service import (
    SkillService,
    SkillManageRequestBuilder,
    SkillManageToolExecutor,
)
from app.domain.skill import SkillWriteOrigin
from tests.application.test_skill_manage_service import _existing_agent_skill
from app.domain.policy import PolicyOutcome


def service():
    registry = MagicMock(
        get_skill=AsyncMock(return_value=_existing_agent_skill()),
        upsert_skill=AsyncMock(),
        replace_all_skills=AsyncMock(),
    )
    loader = MagicMock(
        write_linked_file=AsyncMock(),
        scan=AsyncMock(return_value=([_existing_agent_skill()], [])),
    )
    usage = MagicMock(get=AsyncMock(return_value=None), increment_patch=AsyncMock())
    grants = MagicMock(
        prepare_write=AsyncMock(return_value=MagicMock(revision=12)),
        commit_write=AsyncMock(),
        sync=AsyncMock(),
        list_grants=AsyncMock(return_value=[{"revision": 12, "state": "active"}]),
    )
    return SkillService(
        registry,
        loader,
        usage,
        MagicMock(),
        MagicMock(snapshot=AsyncMock()),
        MagicMock(evaluate=MagicMock(return_value=PolicyOutcome.ALLOW)),
        auto_refresh=True,
        host_script_grants=grants,
    )


@pytest.mark.asyncio
async def test_only_trusted_controlled_write_prepares_before_write_and_scans():
    svc = service()
    request = SkillManageRequestBuilder.write_file(
        "demo", "scripts/a.py", "hello", SkillWriteOrigin.FOREGROUND
    )
    assert (await svc.manage_skill(request)).success
    svc.host_script_grants.prepare_write.assert_not_awaited()

    async def write(*args):
        svc.host_script_grants.prepare_write.assert_awaited_once()
        svc.host_script_grants.commit_write.assert_not_awaited()

    svc.loader.write_linked_file.side_effect = write
    result = await svc.manage_skill(request, trusted_write=True)
    assert result.success
    svc.host_script_grants.commit_write.assert_awaited_once_with(12)
    assert svc.loader.scan.await_count == 2


@pytest.mark.asyncio
async def test_executor_binds_trust_even_for_service_port():
    from app.domain.tool import ToolCallRequest

    svc = MagicMock(manage_skill=AsyncMock())
    await SkillManageToolExecutor(svc).execute(
        ToolCallRequest(
            id="1",
            name="skill_manage",
            arguments={
                "action": "write_file",
                "name": "demo",
                "file_path": "scripts/a.py",
                "file_content": "hello",
                "trusted_write": False,
            },
        )
    )
    assert svc.manage_skill.call_args.kwargs == {"trusted_write": True}


@pytest.mark.asyncio
async def test_loop_reports_scan_error_and_still_retries_revocations():
    import asyncio

    svc = service()
    svc.loader.scan.side_effect = RuntimeError("private path")
    task = asyncio.create_task(svc.run_auto_refresh(0.001))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert svc.last_auto_refresh_error == "scan_failed"
    assert svc.host_script_grants.sync.await_count > 0


@pytest.mark.asyncio
async def test_remove_reports_pending_revoke_without_claiming_rollback():
    svc = service()
    svc.loader.remove_linked_file = AsyncMock()
    svc.host_script_grants.revoke_skill = AsyncMock(return_value=False)
    request = SkillManageRequestBuilder.remove_file(
        "demo", "scripts/a.py", SkillWriteOrigin.FOREGROUND
    )
    result = await svc.manage_skill(request, trusted_write=True)
    assert result.success and "pending_revoke" in result.summary


@pytest.mark.asyncio
async def test_staged_trust_survives_approval_but_direct_origin_does_not():
    svc = service()
    svc.policy.evaluate.return_value = PolicyOutcome.REQUIRE_APPROVAL
    svc.pending.stage = AsyncMock(return_value="pid")
    request = SkillManageRequestBuilder.write_file(
        "demo", "scripts/a.py", "hello", SkillWriteOrigin.FOREGROUND
    )
    result = await svc.manage_skill(request, trusted_write=True)
    assert result.staged
    staged = svc.pending.stage.call_args.args[0]
    assert staged.payload["_trusted_write"] is True
    svc.host_script_grants.prepare_write.assert_not_awaited()
    svc.pending.approve_take = AsyncMock(return_value=staged)
    svc.pending.clear = AsyncMock()
    svc.policy.evaluate.return_value = PolicyOutcome.ALLOW
    assert (await svc.approve_pending("pid")).success
    svc.host_script_grants.prepare_write.assert_awaited_once()


@pytest.mark.asyncio
async def test_scan_failure_keeps_written_file_pending_in_summary():
    svc = service()
    svc.loader.scan.side_effect = RuntimeError("private path")
    request = SkillManageRequestBuilder.write_file(
        "demo", "scripts/a.py", "hello", SkillWriteOrigin.FOREGROUND
    )
    result = await svc.manage_skill(request, trusted_write=True)
    assert result.success and "scan_failed pending_sync" in result.summary
    assert svc.last_auto_refresh_error == "scan_failed"
    svc.host_script_grants.commit_write.assert_awaited_once()
    svc.host_script_grants.sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_stalled_old_loop_sync_cannot_clear_newer_scan_failure(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from app.application.host_script_grant_service import HostScriptGrantService
    from app.infrastructure.host_terminal.script_intent_store import (
        SQLiteHostScriptIntentStore,
    )

    svc = service()
    entered = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def stalled_sync():
        entered.set()
        await release.wait()
        completed.set()

    svc.host_script_grants.sync.side_effect = stalled_sync
    task = asyncio.create_task(svc.run_auto_refresh(60))
    try:
        await entered.wait()
        svc.loader.scan.side_effect = RuntimeError("new scan failed")
        with pytest.raises(RuntimeError):
            await svc.scan_now()
        release.set()
        await completed.wait()
        await asyncio.sleep(0)
        assert svc.last_auto_refresh_error == "scan_failed"
        client = SimpleNamespace(script_grants=AsyncMock(return_value=[]))
        grants = HostScriptGrantService(
            SQLiteHostScriptIntentStore(tmp_path / "db"), client, svc
        )
        intent = await grants.prepare_write(
            "demo", "scripts/a.py", "hello", "foreground"
        )
        await grants.commit_write(intent.revision)
        await grants.sync()
        assert (await grants.list_grants())[0]["error"] == "scan_blocked"
        assert client.script_grants.await_count == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_revoke_persistence_failure_fails_request_without_claiming_pending():
    import json
    from app.domain.tool import ToolCallRequest, ToolResultStatus

    svc = service()
    svc.loader.remove_linked_file = AsyncMock()
    svc.host_script_grants.revoke_skill = AsyncMock(
        side_effect=RuntimeError("/private/secret/database.sqlite token=secret")
    )
    request = SkillManageRequestBuilder.remove_file(
        "demo", "scripts/a.py", SkillWriteOrigin.FOREGROUND
    )
    result = await svc.manage_skill(request, trusted_write=True)
    assert not result.success
    assert result.error == "revocation_persistence_failed"
    assert result.summary == "remove_file demo completed; revocation_failed"
    assert "pending" not in result.summary
    tool_result = await SkillManageToolExecutor(svc).execute(
        ToolCallRequest(
            id="1",
            name="skill_manage",
            arguments={
                "action": "remove_file",
                "name": "demo",
                "file_path": "scripts/a.py",
            },
        )
    )
    assert tool_result.status is ToolResultStatus.ERROR
    assert json.loads(tool_result.content) == {
        "success": False,
        "error": "revocation_persistence_failed",
    }
    assert "/private" not in tool_result.content and "secret" not in tool_result.content

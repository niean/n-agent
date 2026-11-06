"""Exercise controlled writes through real file/SQLite/host policy adapters."""

import hashlib
import asyncio
import json
from pathlib import Path

import pytest

from app.application.host_script_grant_service import HostScriptGrantService
from app.application.host_terminal_tool_executor import HostTerminalToolExecutor
from app.application.skill_service import SkillManageToolExecutor, SkillService
from app.domain.host_script_grant import HostScriptGrant
from app.domain.host_terminal import HostTerminalBridgeResponse, HostTerminalStatus
from app.domain.skill_format import SkillFormatValidator
from app.domain.skill_policy import SkillPolicy
from app.domain.tool import ToolCallRequest, ToolResultStatus
from app.infrastructure.host_terminal.http_client import _request_payload
from app.infrastructure.host_terminal.script_intent_store import SQLiteHostScriptIntentStore
from app.infrastructure.registry.sqlite_skill_registry import SQLiteSkillRegistry
from app.infrastructure.skill.file_loader import SkillFileLoader, SkillFileLoaderConfig
from app.infrastructure.skill.skill_backup_store import SkillBackupStore
from app.infrastructure.skill.skill_pending_store import SkillPendingStore
from app.infrastructure.skill.skill_usage_store import SkillUsageStore
from tests.infrastructure.test_host_terminal_bridge import _bridge, _command_policy


@pytest.mark.asyncio
async def test_running_scan_discovers_disk_changes_and_preserves_user_switches(tmp_path):
    root = tmp_path / "skills"
    registry = SQLiteSkillRegistry(tmp_path / "skills.db")
    skills = SkillService(
        registry, SkillFileLoader(SkillFileLoaderConfig(root=root)), auto_refresh=True,
    )

    def write_skill(name, description):
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description} (测试技能)\n---\nUse this skill.\n"
        )

    write_skill("existing-demo", "Original description")
    await skills.scan_now()
    await skills.set_enabled("existing-demo", False)
    await skills.set_chat_selectable("existing-demo", False)
    task = asyncio.create_task(skills.run_auto_refresh(interval_seconds=0.01))
    try:
        write_skill("existing-demo", "Updated description")
        write_skill("new-demo", "Discover this new skill")

        async def wait_for_discovery():
            while True:
                names = {skill.name for skill in await skills.list_skills()}
                if "new-demo" in names:
                    return
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_discovery(), timeout=2)
        existing = await skills.get("existing-demo")
        assert existing.description.startswith("Updated description")
        assert existing.enabled is False
        assert existing.chat_selectable is False
        assert await skills.list_for_llm() == [await skills.get("new-demo")]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class InProcessHostClient:
    def __init__(self, bridge):
        self.bridge = bridge
        self.outputs = []

    async def script_grants(self, operation, grant=None):
        status, body = self.bridge.script_grants_payload({
            "operation": operation, "grant": grant.to_dict() if grant else None,
        })
        if status != 200:
            raise ValueError(body["error_code"])
        return [HostScriptGrant.from_dict(value) for value in body["grants"]]

    async def execute(self, request):
        _, body = self.bridge.execute_payload(_request_payload(request))
        self.outputs.append(body["stdout"])
        return HostTerminalBridgeResponse(
            protocol_version="1", request_id=body["request_id"],
            status=HostTerminalStatus(body["status"]), exit_code=body["exit_code"],
            stdout=body["stdout"], stderr=body["stderr"], duration_ms=body["duration_ms"],
            stdout_truncated=body["stdout_truncated"], stderr_truncated=body["stderr_truncated"],
            error_code=body["error_code"],
        )


@pytest.mark.asyncio
async def test_controlled_script_create_update_external_change_and_revoke(tmp_path):
    bridge = _bridge(tmp_path, _command_policy(Path("/bin/echo").resolve()))
    root = Path(bridge.config.skills_root)
    db = tmp_path / "app.db"
    skills = SkillService(
        SQLiteSkillRegistry(db), SkillFileLoader(SkillFileLoaderConfig(root=root)),
        usage=SkillUsageStore(str(db)), pending=SkillPendingStore(str(db)),
        backup=SkillBackupStore(root, keep=2), policy=SkillPolicy(),
        auto_refresh=True, format_validator=SkillFormatValidator(),
    )
    client = InProcessHostClient(bridge)
    grants = HostScriptGrantService(SQLiteHostScriptIntentStore(db), client, skills)
    skills.host_script_grants = grants
    manage = SkillManageToolExecutor(skills)
    executor = HostTerminalToolExecutor(
        client=client, skill_service=skills, policy_loader=bridge.policy_loader,
        script_grant_service=grants,
    )

    created = await manage.execute(ToolCallRequest("create", "skill_manage", {
        "action": "create", "name": "auto-demo",
        "content": "---\nname: auto-demo\ndescription: Run a test script (运行测试脚本)\n---\nUse the script.\n",
    }))
    assert json.loads(created.content)["success"] is True
    assert (await skills.get("auto-demo")).last_scan_status == "ok"

    async def write(source):
        result = await manage.execute(ToolCallRequest("write", "skill_manage", {
            "action": "write_file", "name": "auto-demo",
            "file_path": "scripts/run.py", "file_content": source,
        }))
        assert json.loads(result.content)["success"] is True
        assert "pending_sync" not in json.loads(result.content)["summary"]

    async def execute():
        return await executor.execute(ToolCallRequest("run", "host_terminal", {
            "target_type": "skill_script", "skill": "auto-demo",
            "script": "scripts/run.py", "args": ["hello world"],
        }))

    first = "import sys\nprint('first', sys.argv[1])\n"
    await write(first)
    first_record = (await grants.list_grants())[0]
    assert first_record["state"] == "active"
    assert first_record["sha256"] == hashlib.sha256(first.encode()).hexdigest()
    assert (await execute()).status is ToolResultStatus.SUCCESS
    assert client.outputs[-1] == "first hello world\n"

    second = "import sys\nprint('second', sys.argv[1])\n"
    await write(second)
    second_record = (await grants.list_grants())[0]
    assert second_record["revision"] > first_record["revision"]
    assert (await execute()).status is ToolResultStatus.SUCCESS
    assert client.outputs[-1] == "second hello world\n"

    (root / "auto-demo/scripts/run.py").write_text("print('external')\n")
    await skills.scan_now()
    assert (await execute()).status is not ToolResultStatus.SUCCESS
    assert (await grants.list_grants())[0]["state"] == "stale"

    await write(second)
    current = (await grants.list_grants())[0]
    assert (await grants.revoke(current["revision"]))["success"] is True
    await grants.sync()
    assert (await execute()).status is not ToolResultStatus.SUCCESS
    assert (await grants.list_grants())[0]["state"] == "revoked"

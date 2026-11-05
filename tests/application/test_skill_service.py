import asyncio
import json
import pytest
from datetime import datetime, timezone

from app.application.skill_service import (
    SkillInput,
    SkillService,
    SkillScanReport,
    SkillScanWarning,
)
from app.domain.skill import (
    Skill,
    SkillFrontmatter,
    SkillNotFoundError,
    SkillReadiness,
    SkillSource,
    SkillUsage,
)


def _usage(state="active", archived_at=None):
    if archived_at is None and state == "archived":
        archived_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    return SkillUsage(
        created_by="user", use_count=3, view_count=5, patch_count=2,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_used_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        last_viewed=None, last_patched_at=None,
        state=state, pinned=True, archived_at=archived_at,
    )


class FakeUsage:
    """记录调用的 usage 假实现；get 按 name 返回预置 SkillUsage 或 None。"""

    def __init__(self, records=None):
        self.records = dict(records or {})
        self.restore_calls = []
        self.get_calls = []
        self.initial_records = dict(self.records)

    async def get(self, name):
        self.get_calls.append(name)
        return self.records.get(name)

    async def restore_skill(self, name):
        self.restore_calls.append(name)
        from dataclasses import replace
        self.records[name] = replace(self.records[name], state="active", archived_at=None)

    def records_changed(self):
        return self.records != self.initial_records


def _service_with_usage(registry=None, loader=None, usage=None):
    registry = registry or FakeRegistry()
    loader = loader or FakeLoader()
    return SkillService(registry, loader, usage), registry, loader, usage


# 恢复语义对齐 infrastructure skill_usage_store：仅 state->active、archived_at->NULL。
_USAGE_INVARIANT_FIELDS = (
    "created_by", "use_count", "view_count", "patch_count", "created_at",
    "last_used_at", "last_viewed", "last_patched_at", "pinned",
)


def _assert_restore_only_touches_state_and_archived_at(before, after):
    assert before.state == "archived" and before.archived_at is not None
    for field in _USAGE_INVARIANT_FIELDS:
        assert getattr(after, field) == getattr(before, field), field
    assert after.state == "active"
    assert after.archived_at is None


def _skill(name, readiness=SkillReadiness.AVAILABLE, enabled=True, relative_path=None, chat_selectable=True, source=SkillSource.USER, last_scan_status="ok", last_scan_error=None):
    fm = SkillFrontmatter(
        name=name, description="d", version="", platforms=["linux"], tags=[],
        related_skills=[], author="", license="", setup_help=None,
        required_env_vars=[], raw={"name": name, "description": "d"},
    )
    return Skill(
        id=f"id-{name}", name=name, relative_path=relative_path or f"{name}/SKILL.md",
        description="d", platforms=["linux"], frontmatter=fm,
        enabled=enabled, readiness=readiness, last_scan_status=last_scan_status,
        last_scan_error=last_scan_error, last_seen_at=None,
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
        chat_selectable=chat_selectable, source=source,
    )


class FakeRegistry:
    def __init__(self):
        self.store = {}

    async def list_skills(self, include_disabled=True):
        items = list(self.store.values())
        return items if include_disabled else [s for s in items if s.enabled]

    async def get_skill(self, name):
        return self.store.get(name)

    async def upsert_skill(self, skill):
        self.store[skill.name] = skill
        return skill

    async def delete_skill(self, name):
        return self.store.pop(name, None) is not None

    async def set_enabled(self, name, enabled):
        s = self.store.get(name)
        if s is None:
            raise SkillNotFoundError(name)
        from dataclasses import replace
        updated = replace(s, enabled=enabled)
        self.store[name] = updated
        return updated

    async def set_chat_selectable(self, name, value):
        s = self.store.get(name)
        if s is None:
            raise SkillNotFoundError(name)
        from dataclasses import replace
        updated = replace(s, chat_selectable=bool(value))
        self.store[name] = updated
        return updated

    async def replace_all_skills(self, skills):
        old = self.store
        self.store = {}
        for s in skills:
            prev = old.get(s.name)
            from dataclasses import replace
            self.store[s.name] = replace(
                s,
                enabled=prev.enabled if prev else s.enabled,
                chat_selectable=prev.chat_selectable if prev else s.chat_selectable,
            )
        return list(self.store.values())


class FakeLoader:
    def __init__(self):
        self.scan_skills = []
        self.scan_warnings = []
        self.rendered = "RENDERED"
        self.linked = "LINKED"
        self.linked_files = {"references": [], "templates": [], "scripts": [], "assets": []}
        self.script_bytes = b"print('ok')\n"

    async def scan(self):
        return list(self.scan_skills), list(self.scan_warnings)

    async def render(self, skill, session_id=""):
        return self.rendered

    async def read_linked_file(self, skill, file_path):
        return self.linked

    async def list_linked_files(self, skill):
        return dict(self.linked_files)

    async def read_script_bytes(self, skill, script_relative_path):
        return self.script_bytes


@pytest.mark.asyncio
async def test_resolve_script_bytes_returns_immutable_path_free_facts():
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("photo"))
    service = SkillService(registry, loader)
    result = await service.resolve_script_bytes("photo", "scripts/photo.py")
    assert result.skill_name == "photo"
    assert result.script_relative_path == "scripts/photo.py"
    assert result.content == loader.script_bytes
    assert len(result.sha256) == 64
    assert not hasattr(result, "path")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "skill",
    [
        _skill("disabled", enabled=False),
        _skill("unsupported", readiness=SkillReadiness.UNSUPPORTED),
        _skill("scan-error", readiness=SkillReadiness.SCAN_ERROR),
        _skill("format-warning", last_scan_status="warning", last_scan_error="format_warning"),
        _skill("injection", last_scan_status="warning", last_scan_error="injection_warning"),
        _skill("unscanned", last_scan_status=None),
    ],
)
async def test_resolve_script_bytes_requires_enabled_ready_scan_success(skill):
    """host 脚本解析保持严格扫描门控：format warning 也必须在 SKILL.md 侧修复。"""
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(skill)
    service = SkillService(registry, loader)
    with pytest.raises(SkillNotFoundError):
        await service.resolve_script_bytes(skill.name, "scripts/photo.py")


@pytest.mark.asyncio
async def test_scan_now_persists_and_returns_report():
    registry, loader = FakeRegistry(), FakeLoader()
    loader.scan_skills = [_skill("a"), _skill("b")]
    loader.scan_warnings = [SkillScanWarning("a/SKILL.md", "duplicate_name", first_path="x")]
    service = SkillService(registry, loader)
    report = await service.scan_now()
    assert isinstance(report, SkillScanReport)
    assert report.skills_count == 2
    assert len(report.warnings) == 1


@pytest.mark.asyncio
async def test_scan_now_restores_archived_usage_for_scanned_skills():
    usage = FakeUsage({"a": _usage("archived"), "b": _usage("archived"),
                       "c": _usage("active"), "d": _usage("stale")})
    loader = FakeLoader()
    loader.scan_skills = [_skill("a"), _skill("b"), _skill("c"), _skill("d")]
    service, registry, loader, usage = _service_with_usage(loader=loader, usage=usage)
    report = await service.scan_now()
    assert usage.restore_calls == ["a", "b"]
    # AC4: 恢复前后仅 state/archived_at 可变，其余字段逐字段全等。
    _assert_restore_only_touches_state_and_archived_at(
        usage.initial_records["a"], usage.records["a"])
    _assert_restore_only_touches_state_and_archived_at(
        usage.initial_records["b"], usage.records["b"])
    assert report.skills_count == 4
    assert report.warnings == []


@pytest.mark.asyncio
async def test_scan_now_second_scan_writes_no_restore():
    usage = FakeUsage({"a": _usage("archived")})
    loader = FakeLoader()
    loader.scan_skills = [_skill("a")]
    service, registry, loader, usage = _service_with_usage(loader=loader, usage=usage)
    await service.scan_now()
    assert usage.restore_calls == ["a"]
    await service.scan_now()
    assert usage.restore_calls == ["a"]


@pytest.mark.asyncio
async def test_scan_now_unscanned_usage_rows_untouched():
    usage = FakeUsage({"gone": _usage("archived")})
    loader = FakeLoader()
    loader.scan_skills = [_skill("a")]
    service, registry, loader, usage = _service_with_usage(loader=loader, usage=usage)
    await service.scan_now()
    assert usage.restore_calls == []
    # AC5: 对账只遍历本次扫描候选，未扫描行连 get 都不被访问。
    assert "gone" not in usage.get_calls
    assert "gone" in usage.records and usage.records["gone"].state == "archived"


@pytest.mark.asyncio
async def test_scan_now_missing_usage_row_left_absent():
    usage = FakeUsage()
    loader = FakeLoader()
    loader.scan_skills = [_skill("a")]
    service, registry, loader, usage = _service_with_usage(loader=loader, usage=usage)
    report = await service.scan_now()
    # AC5: 缺行时 get 返回 None，不创建记录、不恢复。
    assert usage.get_calls == ["a"]
    assert usage.restore_calls == []
    assert "a" not in usage.records
    assert report.skills_count == 1


@pytest.mark.asyncio
async def test_scan_now_active_and_stale_rows_unchanged():
    usage = FakeUsage({"a": _usage("active"), "b": _usage("stale")})
    loader = FakeLoader()
    loader.scan_skills = [_skill("a"), _skill("b")]
    service, registry, loader, usage = _service_with_usage(loader=loader, usage=usage)
    await service.scan_now()
    assert usage.restore_calls == []
    # AC5: active/stale 记录逐字段不变。
    assert usage.records["a"] == usage.initial_records["a"]
    assert usage.records["b"] == usage.initial_records["b"]
    assert not usage.records_changed()


@pytest.mark.asyncio
async def test_scan_now_empty_scan_touches_no_usage():
    usage = FakeUsage({"a": _usage("archived")})
    loader = FakeLoader()
    loader.scan_skills = []
    service, registry, loader, usage = _service_with_usage(loader=loader, usage=usage)
    report = await service.scan_now()
    # AC5: 空扫描完全不访问 usage，正常返回报告。
    assert usage.get_calls == []
    assert usage.restore_calls == []
    assert usage.records["a"].state == "archived"
    assert report.skills_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("skill", [
    _skill("unsup", readiness=SkillReadiness.UNSUPPORTED),
    _skill("dis", enabled=False),
    _skill("warn", last_scan_status="warning", last_scan_error="format_warning"),
    _skill("inj", last_scan_status="warning", last_scan_error="injection_warning"),
])
async def test_scan_now_restores_archived_regardless_of_readiness_or_enabled(skill):
    usage = FakeUsage({skill.name: _usage("archived")})
    loader = FakeLoader()
    loader.scan_skills = [skill]
    service, registry, loader, usage = _service_with_usage(loader=loader, usage=usage)
    report = await service.scan_now()
    # AC5: 诊断类候选同样被恢复。
    assert usage.restore_calls == [skill.name]
    assert report.skills_count == 1
    # AC5: 扫描不篡改诊断字段。
    stored = await registry.get_skill(skill.name)
    assert stored.enabled == skill.enabled
    assert stored.readiness is skill.readiness
    assert stored.last_scan_status == skill.last_scan_status
    assert stored.last_scan_error == skill.last_scan_error
    # AC5: 严格脚本门控不因恢复而放行。
    with pytest.raises(SkillNotFoundError):
        await service.resolve_script_bytes(skill.name, "scripts/x.py")


@pytest.mark.asyncio
async def test_scan_now_reconcile_get_failure_first_candidate_propagates():
    usage = FakeUsage({"a": _usage("archived"), "b": _usage("archived"),
                       "c": _usage("archived")})
    error = RuntimeError("get_failed")
    original_get = usage.get

    async def failing_get(name):
        if name == "a":
            usage.get_calls.append(name)
            raise error
        return await original_get(name)

    usage.get = failing_get
    loader = FakeLoader()
    loader.scan_skills = [_skill("a"), _skill("b"), _skill("c")]
    service, registry, loader, usage = _service_with_usage(loader=loader, usage=usage)
    # AC6: 首条 get 失败立即传播原异常对象，无降级、无伪成功报告。
    with pytest.raises(RuntimeError) as exc_info:
        await service.scan_now()
    assert exc_info.value is error
    assert usage.get_calls == ["a"]
    assert usage.restore_calls == []


@pytest.mark.asyncio
async def test_scan_now_reconcile_restore_failure_first_candidate_propagates():
    usage = FakeUsage({"a": _usage("archived"), "b": _usage("archived"),
                       "c": _usage("archived")})
    error = RuntimeError("restore_failed")
    original_restore = usage.restore_skill

    async def failing_restore(name):
        if name == "a":
            raise error
        await original_restore(name)

    usage.restore_skill = failing_restore
    loader = FakeLoader()
    loader.scan_skills = [_skill("a"), _skill("b"), _skill("c")]
    service, registry, loader, usage = _service_with_usage(loader=loader, usage=usage)
    # AC6: 首条 restore 失败立即传播原异常对象。
    with pytest.raises(RuntimeError) as exc_info:
        await service.scan_now()
    assert exc_info.value is error
    assert usage.get_calls == ["a"]
    assert usage.restore_calls == []


@pytest.mark.asyncio
async def test_scan_now_reconcile_get_failure_mid_scan_then_rescan_converges():
    usage = FakeUsage({"a": _usage("archived"), "b": _usage("archived"),
                       "c": _usage("archived")})
    error = RuntimeError("get_failed")
    original_get = usage.get

    async def failing_get(name):
        if name == "b":
            usage.get_calls.append(name)
            raise error
        return await original_get(name)

    usage.get = failing_get
    loader = FakeLoader()
    loader.scan_skills = [_skill("a"), _skill("b"), _skill("c")]
    service, registry, loader, usage = _service_with_usage(loader=loader, usage=usage)
    # AC7: b 的 get 失败时 a 已恢复，b/c 保持 archived，注册表已替换且无回滚。
    with pytest.raises(RuntimeError) as exc_info:
        await service.scan_now()
    assert exc_info.value is error
    assert usage.get_calls == ["a", "b"]
    assert usage.restore_calls == ["a"]
    assert usage.records["a"].state == "active"
    assert usage.records["b"].state == "archived"
    assert usage.records["c"].state == "archived"
    assert set(registry.store) == {"a", "b", "c"}
    # AC7: 清除故障重扫，只恢复剩余 b/c，a 不再恢复。
    usage.get = original_get
    report = await service.scan_now()
    assert usage.restore_calls == ["a", "b", "c"]
    assert usage.records["c"].state == "active"
    assert report.skills_count == 3


@pytest.mark.asyncio
async def test_scan_now_reconcile_restore_failure_mid_scan_then_rescan_converges():
    usage = FakeUsage({"a": _usage("archived"), "b": _usage("archived"),
                       "c": _usage("archived")})
    error = RuntimeError("restore_failed")
    original_restore = usage.restore_skill

    async def failing_restore(name):
        if name == "b":
            raise error
        await original_restore(name)

    usage.restore_skill = failing_restore
    loader = FakeLoader()
    loader.scan_skills = [_skill("a"), _skill("b"), _skill("c")]
    service, registry, loader, usage = _service_with_usage(loader=loader, usage=usage)
    # AC7: b 的 restore 失败时 a 已恢复，注册表已替换且无回滚。
    with pytest.raises(RuntimeError) as exc_info:
        await service.scan_now()
    assert exc_info.value is error
    assert usage.get_calls == ["a", "b"]
    assert usage.restore_calls == ["a"]
    assert usage.records["a"].state == "active"
    assert usage.records["b"].state == "archived"
    assert usage.records["c"].state == "archived"
    assert set(registry.store) == {"a", "b", "c"}
    # AC7: 清除故障重扫，只恢复剩余 b/c（restore_calls 跨扫描累积）。
    usage.restore_skill = original_restore
    await service.scan_now()
    assert usage.restore_calls == ["a", "b", "c"]
    assert usage.records["b"].state == "active"
    assert usage.records["c"].state == "active"


@pytest.mark.asyncio
async def test_scan_now_reconcile_get_cancelled_stops_remaining_candidates():
    usage = FakeUsage({"a": _usage("archived"), "b": _usage("archived")})
    original_get = usage.get

    async def cancelled_get(name):
        usage.get_calls.append(name)
        if name == "a":
            raise asyncio.CancelledError()
        return await original_get(name)

    usage.get = cancelled_get
    loader = FakeLoader()
    loader.scan_skills = [_skill("a"), _skill("b")]
    service, registry, loader, usage = _service_with_usage(loader=loader, usage=usage)
    # AC6: get 取消沿 CancelledError 自然传播，后续候选不被访问。
    with pytest.raises(asyncio.CancelledError):
        await service.scan_now()
    assert usage.get_calls == ["a"]
    assert usage.restore_calls == []


@pytest.mark.asyncio
async def test_scan_now_reconcile_restore_cancelled_stops_remaining_candidates():
    usage = FakeUsage({"a": _usage("archived"), "b": _usage("archived")})

    async def cancelled_restore(name):
        if name == "a":
            raise asyncio.CancelledError()
        raise AssertionError("should not be reached")

    usage.restore_skill = cancelled_restore
    loader = FakeLoader()
    loader.scan_skills = [_skill("a"), _skill("b")]
    service, registry, loader, usage = _service_with_usage(loader=loader, usage=usage)
    # AC6: restore 取消立即传播，b 的 get 不发生。
    with pytest.raises(asyncio.CancelledError):
        await service.scan_now()
    assert usage.get_calls == ["a"]
    assert usage.restore_calls == []


@pytest.mark.asyncio
async def test_scan_now_scan_failure_skips_usage_calls():
    usage = FakeUsage({"a": _usage("archived")})
    loader = FakeLoader()
    scan_error = RuntimeError("scan_failed")

    async def broken_scan():
        raise scan_error

    loader.scan = broken_scan
    service, registry, loader, usage = _service_with_usage(loader=loader, usage=usage)
    # AC6: scan 失败在对账之前，原异常传播且 usage 完全未被触碰。
    with pytest.raises(RuntimeError) as exc_info:
        await service.scan_now()
    assert exc_info.value is scan_error
    assert usage.get_calls == [] and usage.restore_calls == []
    assert not usage.records_changed()


@pytest.mark.asyncio
async def test_scan_now_replace_failure_skips_usage_calls():
    usage = FakeUsage({"a": _usage("archived")})
    loader = FakeLoader()
    loader.scan_skills = [_skill("a")]
    replace_error = RuntimeError("replace_failed")

    class FailingReplaceRegistry(FakeRegistry):
        async def replace_all_skills(self, skills):
            raise replace_error

    registry = FailingReplaceRegistry()
    service = SkillService(registry, loader, usage)
    # AC6: replace_all_skills 失败在对账之前，原异常传播且 usage 完全未被触碰。
    with pytest.raises(RuntimeError) as exc_info:
        await service.scan_now()
    assert exc_info.value is replace_error
    assert usage.get_calls == [] and usage.restore_calls == []
    assert not usage.records_changed()


@pytest.mark.asyncio
async def test_scan_now_normalize_failure_skips_reconcile(monkeypatch):
    from app.application import skill_service as skill_service_module
    usage = FakeUsage({"a": _usage("archived")})
    loader = FakeLoader()
    loader.scan_skills = [_skill("a"), _skill("b")]
    loader.scan_warnings = [SkillScanWarning("a/SKILL.md", "duplicate_name", first_path="x")]
    normalize_error = RuntimeError("normalize_failed")

    def broken_normalize(w):
        raise normalize_error

    monkeypatch.setattr(skill_service_module, "_normalize_warning", broken_normalize)
    service, registry, loader, usage = _service_with_usage(loader=loader, usage=usage)
    # AC8: 归一化在对账之前；归一化失败时注册表已替换、usage 未被触碰。
    with pytest.raises(RuntimeError) as exc_info:
        await service.scan_now()
    assert exc_info.value is normalize_error
    assert set(registry.store) == {"a", "b"}
    assert usage.get_calls == [] and usage.restore_calls == []


@pytest.mark.asyncio
async def test_scan_now_preserves_full_warning_fields_and_restores():
    usage = FakeUsage({"a": _usage("archived")})
    loader = FakeLoader()
    loader.scan_skills = [_skill("a")]
    warning = SkillScanWarning(
        relative_path="a/SKILL.md", reason="duplicate_name",
        detail="duplicated with b", first_path="b/SKILL.md",
    )
    loader.scan_warnings = [warning]
    service, registry, loader, usage = _service_with_usage(loader=loader, usage=usage)
    report = await service.scan_now()
    # AC8: 四字段 warning 原样进入报告；archived 行实际被恢复。
    assert len(report.warnings) == 1
    assert report.warnings[0].relative_path == "a/SKILL.md"
    assert report.warnings[0].reason == "duplicate_name"
    assert report.warnings[0].detail == "duplicated with b"
    assert report.warnings[0].first_path == "b/SKILL.md"
    assert report.skills_count == 1
    assert usage.restore_calls == ["a"]
    assert usage.records["a"].state == "active"


@pytest.mark.asyncio
async def test_scan_now_skips_reconcile_without_usage_store():
    loader = FakeLoader()
    loader.scan_skills = [_skill("a")]
    registry = FakeRegistry()
    service = SkillService(registry, loader)  # usage 未注入
    report = await service.scan_now()
    # AC9: 无 usage 时跳过对账，扫描结果正常落注册表。
    assert report.skills_count == 1
    assert set(registry.store) == {"a"}


@pytest.mark.asyncio
async def test_render_view_returns_payload():
    registry, loader = FakeRegistry(), FakeLoader()
    s = _skill("a")
    await registry.upsert_skill(s)
    service = SkillService(registry, loader)
    payload = await service.render_view("a", session_id="sess-1")
    assert payload["success"] is True
    assert payload["name"] == "a"
    assert payload["content"] == "RENDERED"
    assert "linked_files" in payload


@pytest.mark.asyncio
async def test_render_view_disabled_returns_failure():
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("a", enabled=False))
    service = SkillService(registry, loader)
    payload = await service.render_view("a")
    assert payload["success"] is False
    assert "disabled" in payload["error"].lower()


@pytest.mark.asyncio
async def test_render_view_unsupported_returns_failure():
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("a", readiness=SkillReadiness.UNSUPPORTED))
    service = SkillService(registry, loader)
    payload = await service.render_view("a")
    assert payload["success"] is False
    assert payload["readiness"] == "unsupported"


@pytest.mark.asyncio
async def test_render_view_missing_returns_available_list():
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("a"))
    service = SkillService(registry, loader)
    payload = await service.render_view("missing")
    assert payload["success"] is False
    assert "available" in payload


@pytest.mark.asyncio
async def test_render_linked_file_returns_content():
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("a"))
    service = SkillService(registry, loader)
    payload = await service.render_linked_file("a", "references/x.md")
    assert payload["success"] is True
    assert payload["content"] == "LINKED"
    assert payload["file"] == "references/x.md"


@pytest.mark.asyncio
async def test_set_enabled_updates_registry():
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("a"))
    service = SkillService(registry, loader)
    updated = await service.set_enabled("a", False)
    assert updated.enabled is False


@pytest.mark.asyncio
async def test_set_chat_selectable_updates_registry():
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("a"))
    service = SkillService(registry, loader)
    updated = await service.set_chat_selectable("a", False)
    assert updated.chat_selectable is False
    reloaded = await registry.get_skill("a")
    assert reloaded.chat_selectable is False


@pytest.mark.asyncio
async def test_set_chat_selectable_raises_when_missing():
    service = SkillService(FakeRegistry(), FakeLoader())
    with pytest.raises(SkillNotFoundError):
        await service.set_chat_selectable("missing", False)


@pytest.mark.asyncio
async def test_list_chat_selectable_filters_out_false():
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("visible"))
    await registry.upsert_skill(_skill("hidden", chat_selectable=False))
    service = SkillService(registry, loader)
    visible = {s.name for s in await service.list_chat_selectable()}
    assert "visible" in visible
    assert "hidden" not in visible


@pytest.mark.asyncio
async def test_list_chat_selectable_excludes_disabled_and_unavailable():
    """The new method must not relax the enabled/available guards from list_for_llm."""
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("ok"))
    await registry.upsert_skill(_skill("off", enabled=False))
    await registry.upsert_skill(_skill("bad", readiness=SkillReadiness.UNSUPPORTED))
    service = SkillService(registry, loader)
    visible = {s.name for s in await service.list_chat_selectable()}
    assert visible == {"ok"}


@pytest.mark.asyncio
async def test_list_for_llm_unchanged_by_chat_selectable():
    """list_for_llm keeps the original LLM-facing index; chat_selectable is a separate filter."""
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("visible"))
    await registry.upsert_skill(_skill("hidden", chat_selectable=False))
    service = SkillService(registry, loader)
    visible = {s.name for s in await service.list_for_llm()}
    assert visible == {"visible", "hidden"}


@pytest.mark.asyncio
async def test_create_update_delete_skill_metadata():
    registry, loader = FakeRegistry(), FakeLoader()
    service = SkillService(registry, loader)
    created = await service.create_skill(
        SkillInput(
            name="manual",
            relative_path="manual/SKILL.md",
            description="Manual skill",
            platforms=["linux"],
            frontmatter={"tags": ["ops"]},
        )
    )
    assert created.name == "manual"
    assert created.frontmatter.raw["tags"] == ["ops"]

    updated = await service.update_skill(
        "manual",
        SkillInput(
            name="manual",
            relative_path="manual/SKILL.md",
            description="Updated",
            platforms=["linux", "darwin"],
            enabled=False,
        )
    )
    assert updated.description == "Updated"
    assert updated.enabled is False
    assert updated.platforms == ["linux", "darwin"]

    await service.delete_skill("manual")
    assert await registry.get_skill("manual") is None


@pytest.mark.asyncio
async def test_update_skill_preserves_source_from_current():
    """Regression: openForm 保存只改 chat_selectable 时也会发完整 PATCH，
    完整 PATCH 走 _skill_from_input，必须保留 current.source，避免 SEED/AGENT 被降级为 USER。"""
    registry, loader = FakeRegistry(), FakeLoader()
    service = SkillService(registry, loader)
    # 模拟 n-agent / skill-creator 这类 seed Skill
    seed_skill = _skill("n-agent", source=SkillSource.SEED)
    await registry.upsert_skill(seed_skill)
    agent_skill = _skill("skill-creator", source=SkillSource.AGENT)
    await registry.upsert_skill(agent_skill)

    # 用户在编辑 modal 改了描述（payload 不带 source 字段）
    updated_seed = await service.update_skill(
        "n-agent",
        SkillInput(
            name="n-agent",
            relative_path="n-agent/SKILL.md",
            description="Updated description",
            platforms=["linux"],
            enabled=True,
        ),
    )
    assert updated_seed.source == SkillSource.SEED, (
        "SEED skill must not be downgraded to USER on update"
    )

    updated_agent = await service.update_skill(
        "skill-creator",
        SkillInput(
            name="skill-creator",
            relative_path="skill-creator/SKILL.md",
            description="Updated description",
            platforms=["linux"],
            enabled=True,
        ),
    )
    assert updated_agent.source == SkillSource.AGENT, (
        "AGENT skill must not be downgraded to USER on update"
    )


@pytest.mark.asyncio
async def test_create_skill_rejects_path_traversal():
    registry, loader = FakeRegistry(), FakeLoader()
    service = SkillService(registry, loader)
    with pytest.raises(Exception):
        await service.create_skill(SkillInput(name="bad", relative_path="../SKILL.md"))


@pytest.mark.asyncio
async def test_list_skills_filters_unsupported_for_llm():
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("a"))
    await registry.upsert_skill(_skill("b", readiness=SkillReadiness.UNSUPPORTED))
    await registry.upsert_skill(_skill("c", enabled=False))
    service = SkillService(registry, loader)
    visible = await service.list_for_llm()
    assert {s.name for s in visible} == {"a"}


@pytest.mark.asyncio
async def test_build_skills_index_groups_by_category():
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("alpha", relative_path="general/alpha/SKILL.md"))
    await registry.upsert_skill(_skill("beta", relative_path="coding/beta/SKILL.md"))
    await registry.upsert_skill(_skill("gamma", relative_path="coding/gamma/SKILL.md"))
    await registry.upsert_skill(_skill("delta", readiness=SkillReadiness.UNSUPPORTED, relative_path="general/delta/SKILL.md"))
    service = SkillService(registry, loader)
    idx = await service.build_skills_index()
    assert "## Available Skills" in idx
    # general and coding categories both present
    assert "- general:" in idx
    assert "- coding:" in idx
    # available skills listed
    assert "alpha" in idx
    assert "beta" in idx
    assert "gamma" in idx
    # unsupported skill excluded
    assert "delta" not in idx


@pytest.mark.asyncio
async def test_build_skills_index_empty_when_no_skills():
    registry, loader = FakeRegistry(), FakeLoader()
    service = SkillService(registry, loader)
    idx = await service.build_skills_index()
    assert idx == ""


# ------------------------------------------------------------------
# convenience methods: list_pending / get_pending / reject_pending
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_pending_delegates_to_store():
    from unittest.mock import AsyncMock, MagicMock
    from app.domain.skill import SkillPendingWrite, SkillWriteAction, SkillWriteOrigin
    registry, loader = FakeRegistry(), FakeLoader()
    pw = SkillPendingWrite(
        "pid1", SkillWriteAction.CREATE, "demo",
        SkillWriteOrigin.FOREGROUND, "create demo", "diff",
        {}, "pending", None, None, None,
    )
    pending = MagicMock()
    pending.list = AsyncMock(return_value=[pw])
    service = SkillService(registry, loader, pending=pending)
    result = await service.list_pending()
    assert result == [pw]


@pytest.mark.asyncio
async def test_list_pending_raises_without_store():
    registry, loader = FakeRegistry(), FakeLoader()
    service = SkillService(registry, loader)
    with pytest.raises(RuntimeError, match="pending store"):
        await service.list_pending()


@pytest.mark.asyncio
async def test_get_pending_delegates_to_store():
    from unittest.mock import AsyncMock, MagicMock
    from app.domain.skill import SkillPendingWrite, SkillWriteAction, SkillWriteOrigin
    registry, loader = FakeRegistry(), FakeLoader()
    pw = SkillPendingWrite(
        "pid1", SkillWriteAction.CREATE, "demo",
        SkillWriteOrigin.FOREGROUND, "s", "d", {}, "pending", None, None, None,
    )
    pending = MagicMock()
    pending.get = AsyncMock(return_value=pw)
    service = SkillService(registry, loader, pending=pending)
    result = await service.get_pending("pid1")
    assert result is pw


@pytest.mark.asyncio
async def test_reject_pending_delegates_to_store():
    from unittest.mock import AsyncMock, MagicMock
    registry, loader = FakeRegistry(), FakeLoader()
    pending = MagicMock()
    pending.reject = AsyncMock(return_value=True)
    service = SkillService(registry, loader, pending=pending)
    result = await service.reject_pending("pid1")
    assert result is True


# ------------------------------------------------------------------
# convenience methods: list_usage / set_pinned
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_usage_iterates_skills():
    from unittest.mock import AsyncMock, MagicMock
    from app.domain.skill import SkillUsage
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("alpha"))
    await registry.upsert_skill(_skill("beta"))
    usage = MagicMock()
    usage.get = AsyncMock(side_effect=lambda name: SkillUsage(
        created_by="foreground", use_count=1, view_count=2, patch_count=0,
        created_at=None, last_used_at=None, last_viewed=None,
        last_patched_at=None, state="active", pinned=False, archived_at=None,
    ) if name == "alpha" else None)
    service = SkillService(registry, loader, usage=usage)
    result = await service.list_usage()
    names = [name for name, _ in result]
    assert names == ["alpha"]


@pytest.mark.asyncio
async def test_set_pinned_validates_skill_exists():
    from unittest.mock import AsyncMock, MagicMock
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("alpha"))
    usage = MagicMock()
    usage.set_pinned = AsyncMock()
    service = SkillService(registry, loader, usage=usage)
    await service.set_pinned("alpha", True)
    usage.set_pinned.assert_awaited_once_with("alpha", True)


@pytest.mark.asyncio
async def test_set_pinned_raises_for_missing_skill():
    from unittest.mock import AsyncMock, MagicMock
    registry, loader = FakeRegistry(), FakeLoader()
    usage = MagicMock()
    usage.set_pinned = AsyncMock()
    service = SkillService(registry, loader, usage=usage)
    with pytest.raises(SkillNotFoundError):
        await service.set_pinned("missing", True)


@pytest.mark.asyncio
async def test_set_pinned_raises_without_usage_store():
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("alpha"))
    service = SkillService(registry, loader)
    with pytest.raises(RuntimeError, match="usage store"):
        await service.set_pinned("alpha", True)


# ---------------------------------------------------------------------------
# T3: 真实组件组合回归（photo-and-upload 式残留）与 usage 缺行创建
# 本节为 real-component regression 段：引入 infrastructure 真实实现
# （SkillFileLoader / SQLiteSkillRegistry / SkillUsageStore）；仅 policy/
# pending/backup 使用 mock。上文单测段落维持 Fake 约定，不引入 infrastructure 导入。
# 注：真实注册表类名为 SQLiteSkillRegistry（计划中的 SqliteSkillRegistry 不存在）。
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock, MagicMock

from app.application.skill_service import SkillManageRequestBuilder
from app.domain.policy import PolicyOutcome
from app.domain.skill import SkillWriteOrigin
from app.infrastructure.registry.sqlite_skill_registry import SQLiteSkillRegistry
from app.infrastructure.skill.file_loader import (
    SkillFileLoader,
    SkillFileLoaderConfig,
)
from app.infrastructure.skill.skill_usage_store import SkillUsageStore


class RecordingUsage:
    """包装真实 SkillUsageStore，记录 restore_skill 调用次数。"""

    def __init__(self, inner):
        self._inner = inner
        self.restore_calls = []

    async def get(self, name):
        return await self._inner.get(name)

    async def restore_skill(self, name):
        self.restore_calls.append(name)
        return await self._inner.restore_skill(name)

    def __getattr__(self, item):
        return getattr(self._inner, item)


def _write_photo_and_upload_tree(skills_root, name="photo-and-upload"):
    """构造 photo-and-upload 式技能目录：SKILL.md + references/a.md + keep.md。"""
    skill_dir = skills_root / name
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: upload photos\n---\nbody",
        encoding="utf-8",
    )
    (skill_dir / "references" / "a.md").write_text("x", encoding="utf-8")
    (skill_dir / "references" / "keep.md").write_text("keep", encoding="utf-8")
    return skill_dir


def _usage_row(**overrides):
    # T3 真实 store 段的 _usage 对应物（对应上文 T2 单测段 _usage）：
    # 默认值刻意不同（agent 来源 + 非零计数场景），用于锁定真实 store 的
    # 字段保留语义；勿与 _usage 静默合并。
    base = dict(
        created_by="agent", use_count=7, view_count=9, patch_count=1,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_used_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
        last_viewed=datetime(2026, 1, 4, tzinfo=timezone.utc),
        last_patched_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
        state="active", pinned=True, archived_at=None,
    )
    base.update(overrides)
    return SkillUsage(**base)


async def _make_real_manage_env(tmp_path, name, usage_row=None):
    """真实 registry/loader/usage store + 仅 policy/pending/backup mock 的环境。

    磁盘构造 SKILL.md + references/a.md + references/keep.md；注册表预置真实
    domain Skill；usage 可选预置行。
    """
    skills_root = tmp_path / "skills"
    skill_dir = _write_photo_and_upload_tree(skills_root, name)

    loader = SkillFileLoader(
        SkillFileLoaderConfig(root=skills_root, current_platform="linux")
    )
    registry = SQLiteSkillRegistry(tmp_path / "sessions.db")
    store = SkillUsageStore(str(tmp_path / "sessions.db"))
    await registry.upsert_skill(_skill(name))
    if usage_row is not None:
        await store.upsert(name, usage_row)

    policy = MagicMock()
    policy.evaluate = MagicMock(return_value=PolicyOutcome.ALLOW)
    pending = MagicMock()
    pending.stage = AsyncMock(return_value="pid")
    backup = MagicMock()
    backup.snapshot = AsyncMock(return_value="sid")
    usage = RecordingUsage(store)
    service = SkillService(
        registry, loader, usage=usage, pending=pending, backup=backup,
        policy=policy, write_approval=False, backup_enabled=True,
    )
    return service, registry, usage, skill_dir, skills_root


@pytest.mark.asyncio
async def test_scan_now_revives_photo_and_upload_style_residual(tmp_path):
    """存量残留：SKILL.md 与其它文件在、目标关联文件已归档、注册表缺席、usage=archived。
    当前 scan_now 使注册表恢复且 usage=active；整目录归档的技能扫描后 usage 不恢复。"""
    skills_root = tmp_path / "skills"
    skill_dir = _write_photo_and_upload_tree(skills_root)

    loader = SkillFileLoader(
        SkillFileLoaderConfig(root=skills_root, current_platform="linux")
    )
    registry = SQLiteSkillRegistry(tmp_path / "sessions.db")
    store = SkillUsageStore(str(tmp_path / "sessions.db"))
    await store.seed_record_if_missing("photo-and-upload")
    await store.archive_skill("photo-and-upload")  # 模拟旧逻辑残留

    scanned, _ = await loader.scan()
    await loader.remove_linked_file(scanned[0], "references/a.md")
    assert not (skill_dir / "references" / "a.md").exists()
    assert (skill_dir / "SKILL.md").exists()
    assert (skill_dir / "references" / "keep.md").read_text(encoding="utf-8") == "keep"
    assert len(list((skills_root / ".archive").glob("*/references/a.md"))) == 1
    assert await registry.get_skill("photo-and-upload") is None

    usage = RecordingUsage(store)
    service = SkillService(registry, loader, usage)

    report = await service.scan_now()
    skills = await registry.list_skills(include_disabled=True)
    assert [s.name for s in skills] == ["photo-and-upload"]
    assert report.skills_count == 1
    restored = await store.get("photo-and-upload")
    assert restored.state == "active" and restored.archived_at is None
    assert restored.use_count == 0 and restored.patch_count == 0
    assert restored.pinned is False

    # 第二次扫描：已恢复记录零 restore 调用
    calls_after_first = len(usage.restore_calls)
    await service.scan_now()
    assert len(usage.restore_calls) == calls_after_first

    # 整目录归档（Curator/DELETE 路径）：目录移出后扫描不恢复 usage
    calls_before_last = len(usage.restore_calls)
    await loader.delete_skill(scanned[0])
    await store.archive_skill("photo-and-upload")
    last_report = await service.scan_now()
    assert last_report.skills_count == 0
    assert await registry.list_skills(include_disabled=True) == []
    residual = await store.get("photo-and-upload")
    assert residual.state == "archived" and residual.archived_at is not None
    assert len(usage.restore_calls) == calls_before_last


@pytest.mark.asyncio
async def test_increment_patch_creates_default_row_when_missing(tmp_path):
    """REMOVE_FILE 分支的 usage 缺行语义：increment_patch 按现有默认值建行且 patch_count=1。
    该约束只适用写入分支，不得扩大到 scan_now（scan 不创建行）。"""
    store = SkillUsageStore(str(tmp_path / "sessions.db"))
    assert await store.get("fresh-skill") is None

    await store.increment_patch("fresh-skill")

    row = await store.get("fresh-skill")
    assert row is not None
    assert row.patch_count == 1 and row.state == "active"
    assert row.created_by == "foreground" and row.pinned is False
    assert row.use_count == 0 and row.view_count == 0
    assert row.last_patched_at is not None and row.archived_at is None


@pytest.mark.asyncio
async def test_increment_patch_updates_last_patched_at_and_preserves_fields(tmp_path):
    """REMOVE_FILE 分支真实 store 回归（AC2）：已有行的 patch_count 加 1、
    last_patched_at 更新，其余历史字段（created_by/use_count/view_count/pinned/
    created_at/last_used_at/last_viewed/state/archived_at）不变。"""
    store = SkillUsageStore(str(tmp_path / "sessions.db"))
    seeded = await store.upsert("kept", _usage_row(last_patched_at=None))

    await store.increment_patch("kept")

    row = await store.get("kept")
    assert row is not None
    assert row.patch_count == 2
    assert row.last_patched_at is not None
    assert row.created_by == "agent"
    assert row.use_count == 7 and row.view_count == 9
    assert row.pinned is True and row.state == "active"
    assert row.archived_at is None
    assert row.created_at == seeded.created_at
    assert row.last_used_at == seeded.last_used_at
    assert row.last_viewed == seeded.last_viewed


@pytest.mark.asyncio
async def test_manage_skill_remove_file_real_components_active_usage_preserves_state(tmp_path):
    """AC1/AC2(a)：真实组件走 manage_skill REMOVE_FILE，初始 usage=active。
    注册表 upsert 保留条目、磁盘仅目标文件归档、usage 仅 patch_count 恰 +1 与
    last_patched_at 严格增大，其余字段逐字段不变。"""
    service, registry, usage, skill_dir, skills_root = await _make_real_manage_env(
        tmp_path, "photo-active",
        usage_row=_usage_row(state="active"),
    )
    before_usage = await usage.get("photo-active")
    before_reg = await registry.get_skill("photo-active")
    assert before_usage is not None and before_usage.state == "active"

    result = await service.manage_skill(SkillManageRequestBuilder.remove_file(
        name="photo-active", file_path="references/a.md",
        origin=SkillWriteOrigin.FOREGROUND,
    ))
    assert result.success is True and result.error is None

    # 注册表 upsert 语义：条目仍在，relative_path/last_scan_status 保留，updated_at 推进
    skills = await registry.list_skills(include_disabled=True)
    assert [s.name for s in skills] == ["photo-active"]
    after_reg = await registry.get_skill("photo-active")
    assert after_reg is not None
    assert after_reg.relative_path == before_reg.relative_path
    assert after_reg.last_scan_status == before_reg.last_scan_status
    assert after_reg.updated_at > before_reg.updated_at

    # 磁盘：a.md 归档，SKILL.md 与 keep.md 原地
    assert not (skill_dir / "references" / "a.md").exists()
    assert (skill_dir / "SKILL.md").exists()
    assert (skill_dir / "references" / "keep.md").read_text(encoding="utf-8") == "keep"
    assert len(list((skills_root / ".archive").glob("*/references/a.md"))) == 1

    # usage：active 保持 active；仅 patch_count +1 与 last_patched_at 更新
    after_usage = await usage.get("photo-active")
    assert after_usage.state == "active" and after_usage.archived_at is None
    assert after_usage.patch_count == before_usage.patch_count + 1
    assert after_usage.last_patched_at is not None
    assert after_usage.last_patched_at > before_usage.last_patched_at
    for field in ("created_by", "use_count", "view_count", "created_at",
                  "last_used_at", "last_viewed", "pinned"):
        assert getattr(after_usage, field) == getattr(before_usage, field), field


@pytest.mark.asyncio
async def test_manage_skill_remove_file_real_components_stale_usage_preserves_state(tmp_path):
    """AC1：真实组件走 manage_skill REMOVE_FILE，初始 usage=stale。
    increment_patch 不触碰 state：stale 保持 stale，不降级也不恢复为 active。"""
    service, registry, usage, skill_dir, _ = await _make_real_manage_env(
        tmp_path, "photo-stale",
        usage_row=_usage_row(state="stale"),
    )
    before_usage = await usage.get("photo-stale")
    assert before_usage is not None and before_usage.state == "stale"

    result = await service.manage_skill(SkillManageRequestBuilder.remove_file(
        name="photo-stale", file_path="references/a.md",
        origin=SkillWriteOrigin.FOREGROUND,
    ))
    assert result.success is True and result.error is None

    after_usage = await usage.get("photo-stale")
    assert after_usage.state == "stale"
    assert after_usage.patch_count == before_usage.patch_count + 1
    assert after_usage.last_patched_at is not None
    assert after_usage.last_patched_at > before_usage.last_patched_at
    for field in ("created_by", "use_count", "view_count", "created_at",
                  "last_used_at", "last_viewed", "pinned", "archived_at"):
        assert getattr(after_usage, field) == getattr(before_usage, field), field
    assert [s.name for s in await registry.list_skills(include_disabled=True)] == [
        "photo-stale"
    ]


@pytest.mark.asyncio
async def test_manage_skill_remove_file_creates_missing_usage_row(tmp_path):
    """AC2(b)：manage_skill REMOVE_FILE 入口级缺行语义——空 usage store 下
    经 increment_patch 建默认行：patch_count=1、state=active、created_by=foreground。"""
    service, registry, usage, _, _ = await _make_real_manage_env(tmp_path, "fresh-removal")
    assert await usage.get("fresh-removal") is None

    result = await service.manage_skill(SkillManageRequestBuilder.remove_file(
        name="fresh-removal", file_path="references/a.md",
        origin=SkillWriteOrigin.FOREGROUND,
    ))
    assert result.success is True and result.error is None

    row = await usage.get("fresh-removal")
    assert row is not None
    assert row.patch_count == 1 and row.state == "active"
    assert row.created_by == "foreground" and row.pinned is False
    assert row.last_patched_at is not None and row.archived_at is None


@pytest.mark.asyncio
async def test_manage_skill_delete_real_components_archives_everything(tmp_path):
    """AC3：真实 DELETE 组合——整目录移入 .archive、注册表条目删除、usage 归档。"""
    service, registry, usage, skill_dir, skills_root = await _make_real_manage_env(
        tmp_path, "sacrificial", usage_row=_usage_row()
    )

    result = await service.manage_skill(SkillManageRequestBuilder.delete(
        name="sacrificial", origin=SkillWriteOrigin.FOREGROUND,
    ))
    assert result.success is True and result.error is None

    assert not skill_dir.exists()
    archived = list((skills_root / ".archive").glob("sacrificial-*/SKILL.md"))
    assert len(archived) == 1
    assert await registry.get_skill("sacrificial") is None
    # 真实 store 行为：DELETE 走 set_state 只写 state 列，不写 archived_at
    # （_set_state_sync 仅 UPDATE state），故此处只断言 spec 约束 state=archived。
    row = await usage.get("sacrificial")
    assert row.state == "archived"


@pytest.mark.asyncio
async def test_scan_now_restores_archived_usage_field_by_field_with_real_store(tmp_path):
    """AC4：真实 store 归档行在 scan_now 恢复后逐字段对比，仅 state->active、
    archived_at->None 变化，其余历史字段（含计数、pinned、时间戳）全部保留。"""
    skills_root = tmp_path / "skills"
    _write_photo_and_upload_tree(skills_root)
    loader = SkillFileLoader(
        SkillFileLoaderConfig(root=skills_root, current_platform="linux")
    )
    registry = SQLiteSkillRegistry(tmp_path / "sessions.db")
    store = SkillUsageStore(str(tmp_path / "sessions.db"))
    await store.upsert("photo-and-upload", _usage_row(
        state="archived", archived_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
    ))
    before = await store.get("photo-and-upload")
    assert before.state == "archived" and before.archived_at is not None

    usage = RecordingUsage(store)
    service = SkillService(registry, loader, usage)
    report = await service.scan_now()

    assert report.skills_count == 1
    after = await store.get("photo-and-upload")
    assert after.state == "active" and after.archived_at is None
    for field in _USAGE_INVARIANT_FIELDS:
        assert getattr(after, field) == getattr(before, field), field
    assert len(usage.restore_calls) == 1


# ---------------------------------------------------------------------------
# T4: 受控交错（scan 与 reconcile 之间的并发归档）
# 延续 T3 真实组件约定：真实 SkillFileLoader / SQLiteSkillRegistry /
# SkillUsageStore；交错副作用通过 store 实例级 get 补丁注入。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_now_interleaved_archive_leaves_documented_inconsistency(tmp_path):
    """受控交错：记录 spec Constraints 限制，非期望行为。
    扫描返回候选后、对账 get 前，Curator 整目录归档并 archive_skill。
    结果：目录已归档但 usage 被对账恢复为 active —— 持久不一致，
    下次扫描不会处理已缺席技能（下一周期不可自愈）。本测试锁定该限制的
    存在，防止实现在无协调机制的情况下宣称自愈；实现真正修复并发不一致时，
    需同步修改本测试。"""
    import shutil

    skills_root = tmp_path / "skills"
    skill_dir = _write_photo_and_upload_tree(skills_root, name="x")
    archive_root = skills_root / ".archive"

    loader = SkillFileLoader(
        SkillFileLoaderConfig(root=skills_root, current_platform="linux")
    )
    registry = SQLiteSkillRegistry(tmp_path / "sessions.db")
    store = SkillUsageStore(str(tmp_path / "sessions.db"))
    await store.seed_record_if_missing("x")
    await store.archive_skill("x")  # 初始残留，保证对账会尝试恢复
    archived_dir_name = "x-2026-01-01T00:00:00+00:00"

    real_get = store.get
    interleaved = {"done": False}

    async def racing_get(name):
        row = await real_get(name)
        if not interleaved["done"] and row is not None and row.state == "archived":
            interleaved["done"] = True
            # 模拟并发 Curator：整目录移入 .archive 并 archive_skill
            dest = archive_root / archived_dir_name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(skill_dir), str(dest))
            await store.archive_skill(name)
        return row

    store.get = racing_get
    service = SkillService(registry, loader, store)

    report = await service.scan_now()  # 交错不使 scan_now 失败：限制本身不抛错
    assert report.skills_count == 1
    assert interleaved["done"]
    row = await store.get("x")
    assert row.state == "active"      # 对账恢复了已被并发归档的技能 —— 记录不一致存在
    assert row.archived_at is None
    assert not skill_dir.exists()     # 目录已归档，下次扫描不再处理该技能
    assert (archive_root / archived_dir_name / "SKILL.md").exists()

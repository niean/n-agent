"""T7：场景记忆隔离绑定测试（plan T7 S1）。

覆盖反例：
- 跨 case/replicate 隔离：不同会话的同名逻辑别名映射到独立项目/命名空间，
  预置事实互不可见；
- 归属保护：绑定只对绑定的 session_id 解析，其他会话解析不到；
- 全局 Provider 未被替换：overlay 解析不修改 ExternalMemoryManager 的
  全局注册表/Provider 实例，跨 HTTP 请求只经 session_id 参数解析
  （不依赖父进程 ContextVar）；
- 准备失败回收：provision 中途失败时清理已建资源，不残留半截绑定；
- mem0 无专用空间拒绝：后端无隔离 provision/delete 能力时 blocked。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.infrastructure.evaluation.scenario_memory import (
    ScenarioMemoryBinder,
    ScenarioMemoryError,
)


class _FakeMem0Provider:
    """记录 user_id/namespace 与写入事实的 mem0 替身。"""

    def __init__(self, config: dict, *, fail_provision: bool = False):
        self.config = dict(config)
        self.facts: list[str] = []
        self.deleted = False
        self._fail = fail_provision

    def provision(self, fact: str) -> None:
        if self._fail:
            raise RuntimeError("mem0 provision failed")
        self.facts.append(fact)

    def prefetch(self, query: str, *, session_id: str) -> str:
        return "\n".join(self.facts) if self.facts else ""

    def delete_all(self) -> None:
        self.deleted = True


def _binder(tmp_path: Path, *, mem0_factory=None) -> ScenarioMemoryBinder:
    return ScenarioMemoryBinder(
        file_root=tmp_path / "scenario-memory",
        mem0_factory=mem0_factory,
    )


# ---------------------------------------------------------------------------
# file 别名：独立项目目录 + 跨会话隔离
# ---------------------------------------------------------------------------


def test_file_alias_binds_dedicated_project_per_session(tmp_path):
    binder = _binder(tmp_path)
    r1 = binder.prepare("sess-a", "file_1", fact="文件记忆1")
    r2 = binder.prepare("sess-b", "file_1", fact="文件记忆2")
    assert r1["kind"] == "file"
    # 独立目录：跨 case/replicate 不共享
    assert r1["project_dir"] != r2["project_dir"]
    assert Path(r1["project_dir"]).is_dir()
    provider_a = binder.resolve("sess-a", "file_1")
    provider_b = binder.resolve("sess-b", "file_1")
    assert provider_a is not provider_b
    # 事实不跨会话：各自只看到自己项目的预置事实
    assert "文件记忆1" in provider_a.prefetch("文件记忆", session_id="sess-a")
    assert "文件记忆2" not in provider_a.prefetch("文件记忆", session_id="sess-a")
    assert "文件记忆2" in provider_b.prefetch("文件记忆", session_id="sess-b")
    assert "文件记忆1" not in provider_b.prefetch("文件记忆", session_id="sess-b")


def test_binding_resolves_only_for_bound_session(tmp_path):
    binder = _binder(tmp_path)
    binder.prepare("sess-a", "file_1", fact="文件记忆1")
    assert binder.resolve("other-session", "file_1") is None
    assert binder.resolve("sess-a", "unknown_alias") is None


def test_file_memory_dir_lies_within_dedicated_root(tmp_path):
    binder = _binder(tmp_path)
    receipt = binder.prepare("sess-a", "file_1", fact="文件记忆1")
    project_dir = Path(receipt["project_dir"])
    assert project_dir.resolve().is_relative_to((tmp_path / "scenario-memory").resolve())
    assert receipt["fact_sha256"]


# ---------------------------------------------------------------------------
# mem0：独立 Provider 实例与专用命名空间
# ---------------------------------------------------------------------------


def test_mem0_binding_creates_dedicated_provider_and_namespace(tmp_path):
    created = []

    def factory(config: dict) -> _FakeMem0Provider:
        provider = _FakeMem0Provider(config)
        created.append(provider)
        return provider

    binder = _binder(tmp_path, mem0_factory=factory)
    r1 = binder.prepare("sess-a", "mem0-e2e-test", fact="喜欢樱桃")
    r2 = binder.prepare("sess-b", "mem0-e2e-test", fact="喜欢樱桃")
    # 每 attempt 独立 Provider 实例与专用命名空间
    assert len(created) == 2
    assert created[0] is not created[1]
    assert created[0].config["user_id"] != created[1].config["user_id"]
    assert created[0].config["agent_id"] != created[1].config["agent_id"]
    # 预置合成事实进入专用空间
    assert created[0].facts == ["喜欢樱桃"]
    assert r1["kind"] == "mem0"
    assert r1["user_id"] == created[0].config["user_id"]
    provider = binder.resolve("sess-a", "mem0-e2e-test")
    assert "喜欢樱桃" in provider.prefetch("樱桃", session_id="sess-a")


def test_mem0_without_isolation_factory_is_blocked(tmp_path):
    binder = _binder(tmp_path, mem0_factory=None)
    with pytest.raises(ScenarioMemoryError):
        binder.prepare("sess-a", "mem0-e2e-test", fact="喜欢樱桃")
    assert binder.resolve("sess-a", "mem0-e2e-test") is None


def test_mem0_provision_failure_is_blocked(tmp_path):
    def factory(config: dict) -> _FakeMem0Provider:
        return _FakeMem0Provider(config, fail_provision=True)

    binder = _binder(tmp_path, mem0_factory=factory)
    with pytest.raises(ScenarioMemoryError):
        binder.prepare("sess-a", "mem0-e2e-test", fact="喜欢樱桃")
    assert binder.resolve("sess-a", "mem0-e2e-test") is None


def test_mem0_prepare_waits_until_fact_searchable(tmp_path):
    """mem0 后端 add 后异步索引：prepare 轮询到事实可检索才返回绑定，
    否则首轮 prefetch 拿不到预置事实（R2 S4 实测失败模式）。"""
    created = []

    class _IndexingProvider(_FakeMem0Provider):
        def __init__(self, config):
            super().__init__(config)
            self.searches = 0

        def prefetch(self, query, *, session_id):
            self.searches += 1
            # 前两次搜索：后端尚未索引到预置事实
            return "\n".join(self.facts) if self.searches >= 3 else ""

    def factory(config: dict) -> _IndexingProvider:
        provider = _IndexingProvider(config)
        created.append(provider)
        return provider

    binder = ScenarioMemoryBinder(
        file_root=tmp_path / "scenario-memory",
        mem0_factory=factory,
        mem0_poll_seconds=5.0,
        mem0_poll_interval=0.01,
    )
    binder.prepare("sess-a", "mem0-e2e-test", fact="喜欢樱桃")
    provider = created[0]
    assert provider.searches >= 3
    assert "喜欢樱桃" in provider.prefetch("樱桃", session_id="sess-a")


def test_mem0_fact_never_searchable_is_blocked_and_cleaned(tmp_path):
    """轮询窗口内事实始终不可检索：blocked + 回收专用命名空间，
    不留下"预置成功但检索不到"的半截绑定。"""
    created = []

    class _NeverSearchableProvider(_FakeMem0Provider):
        def prefetch(self, query, *, session_id):
            # 事实写入但搜索恒为空（模拟后端索引失败）
            return ""

    def factory(config: dict) -> _NeverSearchableProvider:
        provider = _NeverSearchableProvider(config)
        created.append(provider)
        return provider

    binder = ScenarioMemoryBinder(
        file_root=tmp_path / "scenario-memory",
        mem0_factory=factory,
        mem0_poll_seconds=0.05,
        mem0_poll_interval=0.01,
    )
    with pytest.raises(ScenarioMemoryError):
        binder.prepare("sess-a", "mem0-e2e-test", fact="喜欢樱桃")
    assert created[0].deleted is True
    assert binder.resolve("sess-a", "mem0-e2e-test") is None


# ---------------------------------------------------------------------------
# 全局 Provider 未被替换 + 准备失败回收 + 删除
# ---------------------------------------------------------------------------


def test_overlay_does_not_touch_global_provider_registry(tmp_path):
    """overlay 解析按 session_id 参数进行（跨 HTTP 请求安全），
    不注册/替换全局 Provider、不依赖 ContextVar。"""
    binder = _binder(tmp_path)
    binder.prepare("sess-a", "file_1", fact="文件记忆1")
    assert binder.global_provider_names() == ()
    # resolve 是纯函数式查询：同一输入同一结果，无隐藏状态迁移
    p1 = binder.resolve("sess-a", "file_1")
    p2 = binder.resolve("sess-a", "file_1")
    assert p1 is p2


def test_delete_binding_removes_dedicated_resources(tmp_path):
    created = []

    def factory(config: dict) -> _FakeMem0Provider:
        provider = _FakeMem0Provider(config)
        created.append(provider)
        return provider

    binder = _binder(tmp_path, mem0_factory=factory)
    file_receipt = binder.prepare("sess-f", "file_1", fact="文件记忆1")
    mem0_receipt = binder.prepare("sess-m", "mem0-e2e-test", fact="喜欢樱桃")
    binder.delete("sess-f", "file_1")
    binder.delete("sess-m", "mem0-e2e-test")
    assert not Path(file_receipt["project_dir"]).exists()
    assert created[0].deleted is True
    assert binder.resolve("sess-f", "file_1") is None
    assert binder.resolve("sess-m", "mem0-e2e-test") is None


def test_unknown_alias_shape_is_rejected(tmp_path):
    binder = _binder(tmp_path)
    with pytest.raises(ScenarioMemoryError):
        binder.prepare("sess-a", "not-a-known-alias", fact="x")


def test_delete_after_restart_reconstructs_dedicated_spaces(tmp_path):
    """T9 S4 实测缺口：TTL 清理跨进程重启后 _bindings 为空，
    delete 此前变 no-op（专用目录/命名空间永不清除）。空间定位由
    (session_id, alias) 确定性派生，重启后必须可重建并精确删除。"""
    created = []

    def factory(config: dict) -> _FakeMem0Provider:
        provider = _FakeMem0Provider(config)
        created.append(provider)
        return provider

    binder = _binder(tmp_path, mem0_factory=factory)
    file_receipt = binder.prepare("sess-rf", "file_1", fact="文件记忆1")
    binder.prepare("sess-rm", "mem0-e2e-test", fact="喜欢樱桃")
    mem0_user_id = created[0].config["user_id"]

    # 模拟容器重启：全新 binder 实例（_bindings 为空），同 file_root
    restarted = _binder(tmp_path, mem0_factory=factory)
    restarted.delete("sess-rf", "file_1")
    restarted.delete("sess-rm", "mem0-e2e-test")
    assert not Path(file_receipt["project_dir"]).exists()
    assert created[1].config["user_id"] == mem0_user_id
    assert created[1].deleted is True


# ---------------------------------------------------------------------------
# ExternalMemoryManager overlay：会话绑定实例随请求解析
# ---------------------------------------------------------------------------


def test_manager_prefetch_uses_session_binding_overlay(tmp_path):
    from app.application.external_memory_manager import ExternalMemoryManager
    from app.infrastructure.memory.builtin_project import BuiltinProjectMemory

    binder = _binder(tmp_path)
    binder.prepare("sess-a", "file_1", fact="文件记忆1")
    binder.prepare("sess-b", "file_1", fact="文件记忆2")

    class _GlobalProvider:
        def __init__(self):
            self.calls = 0

        @property
        def name(self):
            return "builtin"

        def prefetch(self, query, *, session_id):
            self.calls += 1
            return "全局记忆"

    manager = ExternalMemoryManager()
    global_provider = _GlobalProvider()
    manager.add_provider(global_provider)
    manager.set_binding_resolver(binder.resolve)

    # 场景会话：alias 只解析到绑定实例；全局 Provider 未被调用
    context = manager.prefetch_all(
        "文件记忆", session_id="sess-a", enabled_override=["file_1"],
    )
    assert "文件记忆1" in context and "文件记忆2" not in context
    assert global_provider.calls == 0
    # 非 scenario alias 或未绑定会话：不解析、不降级
    assert manager.prefetch_all(
        "q", session_id="sess-a", enabled_override=["unknown"],
    ) == ""
    # 全局名 override：走既有全局路径
    assert "全局记忆" in manager.prefetch_all(
        "q", session_id="sess-a", enabled_override=["builtin"],
    )
    assert global_provider.calls == 1


# ---------------------------------------------------------------------------
# S4：prepare 进入资源账目，TTL 到期经 evidence deleter 精确删除绑定
# ---------------------------------------------------------------------------


async def test_prepare_registers_intent_and_ttl_expiry_deletes_binding(tmp_path):
    from app.application.evaluation_scenario_resources import ScenarioResourceLedger
    from app.infrastructure.evaluation.scenario_store import SQLiteScenarioResourceStore

    binder = _binder(tmp_path)
    store = SQLiteScenarioResourceStore(str(tmp_path / "evaluations.db"))

    deleted = []

    class _EvidenceDeleter:
        async def delete_evidence(self, resource):
            deleted.append(dict(resource))
            session_id, _, alias = str(resource["external_id"]).rpartition("|")
            binder.delete(session_id, alias)

    ledger = ScenarioResourceLedger(
        store,
        stopper=type("_S", (), {"stop": staticmethod(lambda *a, **k: "stopped")})(),
        file_deleter=type("_F", (), {"delete_file": staticmethod(lambda *a, **k: "deleted")})(),
        evidence_deleter=_EvidenceDeleter(),
        evidence_retention_seconds=10.0,
        clock=lambda: now[0],
    )
    now = [1000.0]

    async def prepare(session_id, alias, context):
        receipt = binder.prepare(session_id, alias, fact="文件记忆1")
        business_key = (
            f"{context['execution_id']}:{context['case_id']}:memory_binding:"
            f"{session_id}.{alias}"
        )
        ref = str(receipt.get("project_dir") or "")
        await ledger.register_intent(type("I", (), dict(
            business_key=business_key,
            execution_id=context["execution_id"],
            case_id=context["case_id"],
            resource_kind="memory_binding",
            release_rule="release_active_resources",
            retention_rule="retain_evidence_default",
            external_id=f"{session_id}|{alias}",
            ref=ref,
            expected_sha256=None,
            process_identity=None,
        ))())
        await ledger.confirm_owned(business_key, external_id=f"{session_id}|{alias}", ref=ref)
        return receipt

    receipt = await prepare("eval-exp-x-case-r0-a0", "file_1", {
        "execution_id": "exec0001", "case_id": "memory-file-chat",
    })
    project_dir = Path(receipt["project_dir"])
    assert project_dir.is_dir()
    await ledger.register_retention("exec0001")
    # TTL 到期：evidence deleter 路由 binder 精确删除
    now[0] = 2000.0
    await ledger.expire_retention()
    assert not project_dir.exists()
    assert binder.resolve("eval-exp-x-case-r0-a0", "file_1") is None
    assert deleted and deleted[0]["resource_kind"] == "memory_binding"

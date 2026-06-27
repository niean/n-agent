from app.application.external_memory_manager import ExternalMemoryManager


class FakeProvider:
    def __init__(self, name, schemas=None, available=True):
        self._name = name
        self._schemas = schemas or []
        self._available = available
        self.initialized = False
        self.shutdown_called = False
    @property
    def name(self): return self._name
    def is_available(self): return self._available
    def initialize(self, session_id, **kw): self.initialized = True
    def system_prompt_block(self): return f"# {self._name}" if self._available else ""
    def prefetch(self, query, *, session_id): return ""
    def sync_turn(self, u, a, *, session_id): pass
    def get_tool_schemas(self): return list(self._schemas)
    def handle_tool_call(self, name, args, **kw): return '{"success": true}'
    def shutdown(self): self.shutdown_called = True
    def on_session_switch(self, *a, **kw): pass
    def on_session_end(self, *a, **kw): pass
    def on_pre_compress(self, msgs): return None
    def on_delegation(self, *a, **kw): pass
    def on_memory_write(self, *a, **kw): pass


def test_builtin_and_multi_project_coexist_with_external_query():
    m = ExternalMemoryManager()
    builtin = FakeProvider("builtin", [{"name": "external_memory", "parameters": {"type": "object"}}])
    multi = FakeProvider("multi-project", [{"name": "multi_external_memory", "parameters": {"type": "object"}}])
    mem0 = FakeProvider("mem0", [{"name": "mem0_search", "parameters": {"type": "object"}}])
    m.add_provider(builtin)
    m.add_provider(multi)
    m.swap_external_query_provider(mem0)
    # 三个 slot 都在
    names = [p["name"] for p in m.list_providers()]
    assert "builtin" in names and "multi-project" in names and "mem0" in names
    # 工具面包含三类
    tool_names = [d.name for d in m.get_tool_definitions()]
    assert "external_memory" in tool_names
    assert "multi_external_memory" in tool_names
    assert "mem0_search" in tool_names


def test_swap_replaces_external_query_only():
    m = ExternalMemoryManager()
    multi = FakeProvider("multi-project")
    m.add_provider(multi)
    mem0 = FakeProvider("mem0", [{"name": "mem0_search", "parameters": {"type": "object"}}])
    honcho = FakeProvider("honcho", [{"name": "honcho_search", "parameters": {"type": "object"}}])
    m.swap_external_query_provider(mem0)
    m.swap_external_query_provider(honcho)
    # mem0 已被 shutdown
    assert mem0.shutdown_called is True
    # multi-project 未被影响
    assert multi.shutdown_called is False
    tool_names = [d.name for d in m.get_tool_definitions()]
    assert "mem0_search" not in tool_names
    assert "honcho_search" in tool_names


def test_swap_none_clears_external_query():
    m = ExternalMemoryManager()
    mem0 = FakeProvider("mem0", [{"name": "mem0_search", "parameters": {"type": "object"}}])
    m.swap_external_query_provider(mem0)
    m.swap_external_query_provider(None)
    assert mem0.shutdown_called is True
    tool_names = [d.name for d in m.get_tool_definitions()]
    assert "mem0_search" not in tool_names


def test_tool_surface_callback_fires_on_swap():
    m = ExternalMemoryManager()
    fired = []
    m.register_tool_surface_callback(lambda: fired.append(True))
    mem0 = FakeProvider("mem0", [{"name": "mem0_search", "parameters": {"type": "object"}}])
    m.swap_external_query_provider(mem0)
    assert fired == [True]
    m.swap_external_query_provider(None)
    assert fired == [True, True]


def test_swap_during_lock_returns_swapping():
    # swap 期间工具调用返回 provider_swapping
    # 通过显式持锁模拟 swap 进行中的状态
    import threading
    m = ExternalMemoryManager()
    mem0 = FakeProvider("mem0", [{"name": "mem0_search", "parameters": {"type": "object"}}])
    m.swap_external_query_provider(mem0)
    # 显式持锁，模拟 swap 进行中
    with m._swap_lock:
        result = m.handle_tool_call("mem0_search", {}, agent_context="primary", session_id="s1")
    import json as _json
    parsed = _json.loads(result)
    assert parsed.get("success") is False
    assert parsed.get("error") == "provider_swapping"


def test_swap_callback_failure_sets_flag():
    # 回调异常不阻塞 swap，但 swap 返回 tool_surface_refresh_failed=True
    m = ExternalMemoryManager()
    def bad_cb():
        raise RuntimeError("boom")
    m.register_tool_surface_callback(bad_cb)
    mem0 = FakeProvider("mem0", [{"name": "mem0_search", "parameters": {"type": "object"}}])
    result = m.swap_external_query_provider(mem0)
    assert result["tool_surface_refresh_failed"] is True
    # provider 仍被装载
    assert "mem0_search" in [d.name for d in m.get_tool_definitions()]


def test_swap_callback_success_no_flag():
    m = ExternalMemoryManager()
    m.register_tool_surface_callback(lambda: None)
    mem0 = FakeProvider("mem0", [{"name": "mem0_search", "parameters": {"type": "object"}}])
    result = m.swap_external_query_provider(mem0)
    assert result["tool_surface_refresh_failed"] is False


def test_at_most_one_external_query_via_add_provider():
    m = ExternalMemoryManager()
    mem0 = FakeProvider("mem0", [{"name": "mem0_search", "parameters": {"type": "object"}}])
    m.add_provider(mem0)  # 应该走 external-query slot
    honcho = FakeProvider("honcho", [{"name": "honcho_search", "parameters": {"type": "object"}}])
    # add_provider 第二个 external-query 应被拒绝（warning，不抛异常）
    m.add_provider(honcho)
    tool_names = [d.name for d in m.get_tool_definitions()]
    assert "mem0_search" in tool_names
    assert "honcho_search" not in tool_names


def test_list_providers_includes_slot():
    m = ExternalMemoryManager()
    m.add_provider(FakeProvider("builtin", [{"name": "external_memory", "parameters": {"type": "object"}}]))
    m.add_provider(FakeProvider("multi-project", [{"name": "multi_external_memory", "parameters": {"type": "object"}}]))
    m.swap_external_query_provider(FakeProvider("mem0", [{"name": "mem0_search", "parameters": {"type": "object"}}]))
    items = {p["name"]: p for p in m.list_providers()}
    assert items["builtin"]["slot"] == "builtin"
    assert items["multi-project"]["slot"] == "multi-project"
    assert items["mem0"]["slot"] == "external-query"
    # external-query active provider 带 active=True
    assert items["mem0"]["active"] is True
    # builtin/multi-project 不带 active 字段
    assert "active" not in items["builtin"]
    assert "active" not in items["multi-project"]


def test_list_providers_no_active_external_query():
    m = ExternalMemoryManager()
    m.add_provider(FakeProvider("builtin", [{"name": "external_memory", "parameters": {"type": "object"}}]))
    items = m.list_providers()
    assert len(items) == 1
    assert items[0]["name"] == "builtin"
    assert "active" not in items[0]


class FakeMultiProject(FakeProvider):
    def __init__(self, projects):
        super().__init__("multi-project")
        self._projects = projects
    def list_projects(self):
        return list(self._projects)


def test_resolve_provider_slot_returns_known_slots():
    m = ExternalMemoryManager()
    m.add_provider(FakeProvider("builtin"))
    m.add_provider(FakeMultiProject(["proj-a", "proj-b"]))
    m.swap_external_query_provider(FakeProvider("mem0"))
    assert m.resolve_provider_slot("builtin") == "builtin"
    assert m.resolve_provider_slot("proj-a") == "multi-project"
    assert m.resolve_provider_slot("proj-b") == "multi-project"
    assert m.resolve_provider_slot("mem0") == "external-query"
    # 未装载的 name 返回 None（已删除或未知）
    assert m.resolve_provider_slot("gone") is None


from app.application.external_memory_manager import ExternalMemoryManager
from app.infrastructure.memory.multi_project import MultiProjectMemory


class _EchoExternalProvider:
    """Test double for external query providers (mem0 / holographic / honcho).

    No Markdown snapshot — prefetch returns a query-based fragment.
    """

    @property
    def name(self) -> str:
        return "echo"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        pass

    def system_prompt_block(self) -> str:
        return ""

    def prefetch(self, query: str, *, session_id: str) -> str:
        if not query:
            return ""
        return f"echo recalled for: {query}"

    def queue_prefetch(self, query: str, *, session_id: str) -> None:
        pass

    def sync_turn(self, user_content, assistant_content, *, session_id) -> None:
        pass

    def get_tool_schemas(self):
        return []

    def handle_tool_call(self, tool_name, args, **kwargs):
        return '{"success": false, "error": "no tools"}'

    def shutdown(self) -> None:
        pass


def test_multi_project_memory_uses_selected_project_names(tmp_path):
    project_dir = tmp_path / "locals" / "external-memory" / "project_memory_1"
    project_dir.mkdir(parents=True)
    (project_dir / "memory.md").write_text("project_memory_1 says hello", encoding="utf-8")

    memory = MultiProjectMemory(tmp_path, memory_base_path="./locals/external-memory")
    memory.initialize(session_id="")
    manager = ExternalMemoryManager()
    manager.add_provider(memory)

    prompt = manager.build_system_prompt(enabled_override=["builtin", "project_memory_1"])

    assert "## External Memory: project_memory_1" in prompt
    assert "project_memory_1 says hello" in prompt


def test_multi_project_memory_is_not_enabled_by_builtin_default(tmp_path):
    project_dir = tmp_path / "locals" / "external-memory" / "project_memory_1"
    project_dir.mkdir(parents=True)
    (project_dir / "memory.md").write_text("project memory should stay hidden", encoding="utf-8")

    memory = MultiProjectMemory(tmp_path, memory_base_path="./locals/external-memory")
    memory.initialize(session_id="")
    manager = ExternalMemoryManager()
    manager.add_provider(memory)

    prompt = manager.build_system_prompt(enabled_override=["builtin"])

    assert "project memory should stay hidden" not in prompt


def test_echo_provider_appears_in_list_providers():
    manager = ExternalMemoryManager()
    manager.add_provider(_EchoExternalProvider())
    names = [p["name"] for p in manager.list_providers()]
    assert "echo" in names


def test_echo_provider_prefetch_wrapped_into_memory_context():
    manager = ExternalMemoryManager()
    manager.add_provider(_EchoExternalProvider())
    manager.set_global_enabled(["echo"])
    result = manager.prefetch_all("hello world", session_id="s1", enabled_override=["echo"])
    assert "<memory-context>" in result
    assert "echo recalled for: hello world" in result
    assert "</memory-context>" in result


def test_echo_provider_failure_does_not_block_other_providers(tmp_path):
    from app.infrastructure.memory.builtin_project import BuiltinProjectMemory

    memory_dir = tmp_path / "locals" / "external-memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "memory.md").write_text("Python FastAPI", encoding="utf-8")
    builtin = BuiltinProjectMemory(tmp_path, memory_path="./locals/external-memory")
    builtin.initialize(session_id="", project_root=str(tmp_path))

    class _FailingProvider(_EchoExternalProvider):
        @property
        def name(self) -> str:
            return "failing"

        def prefetch(self, query: str, *, session_id: str) -> str:
            raise RuntimeError("backend down")

    manager = ExternalMemoryManager()
    manager.add_provider(builtin)
    manager.add_provider(_FailingProvider())
    result = manager.prefetch_all(
        "Python", session_id="s1", enabled_override=["builtin", "failing"]
    )
    # Builtin still returned despite failing provider throwing
    assert "Python FastAPI" in result
    assert "<memory-context>" in result


def test_echo_provider_isolated_from_builtin(tmp_path):
    from app.infrastructure.memory.builtin_project import BuiltinProjectMemory

    memory_dir = tmp_path / "locals" / "external-memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "memory.md").write_text("Python FastAPI", encoding="utf-8")
    builtin = BuiltinProjectMemory(tmp_path, memory_path="./locals/external-memory")
    builtin.initialize(session_id="", project_root=str(tmp_path))

    manager = ExternalMemoryManager()
    manager.add_provider(builtin)
    manager.add_provider(_EchoExternalProvider())

    # Only echo enabled — builtin must not contribute
    result = manager.prefetch_all(
        "Python", session_id="s1", enabled_override=["echo"]
    )
    assert "echo recalled for: Python" in result
    assert "Python FastAPI" not in result


def test_sync_all_skips_non_primary_agent_context(tmp_path):
    """sync_all must only fire for agent_context=primary."""
    calls: list[tuple[str, str, str]] = []

    class _SpyProvider(_EchoExternalProvider):
        @property
        def name(self) -> str:
            return "spy"

        def sync_turn(self, user_content, assistant_content, *, session_id) -> None:
            calls.append((user_content, assistant_content, session_id))

    manager = ExternalMemoryManager()
    manager.add_provider(_SpyProvider())
    manager.set_global_enabled(["spy"])

    manager.sync_all(
        "u", "a", session_id="s1", agent_context="subagent", enabled_override=["spy"]
    )
    manager.sync_all(
        "u", "a", session_id="s1", agent_context="cron", enabled_override=["spy"]
    )
    manager.sync_all(
        "u", "a", session_id="s1", agent_context="unattended", enabled_override=["spy"]
    )
    assert calls == []

    manager.sync_all(
        "u", "a", session_id="s1", agent_context="primary", enabled_override=["spy"]
    )
    assert calls == [("u", "a", "s1")]


def test_sync_all_invokes_builtin_sync_turn_writing_observations(tmp_path):
    """End-to-end: sync_all on builtin provider produces observations.md entry."""
    from app.infrastructure.memory.builtin_project import BuiltinProjectMemory

    memory_dir = tmp_path / "locals" / "external-memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "memory.md").write_text("", encoding="utf-8")
    builtin = BuiltinProjectMemory(tmp_path, memory_path="./locals/external-memory")
    builtin.initialize(session_id="", project_root=str(tmp_path))

    manager = ExternalMemoryManager()
    manager.add_provider(builtin)

    manager.sync_all(
        "用 Python FastAPI 实现 API",
        "已实现",
        session_id="s1",
        agent_context="primary",
        enabled_override=["builtin"],
    )
    obs_file = tmp_path / "locals" / "external-memory" / "observations.md"
    assert obs_file.exists()
    content = obs_file.read_text(encoding="utf-8")
    assert "python" in content.lower()
    assert "fastapi" in content.lower()


def test_sync_all_skips_disabled_provider(tmp_path):
    """Disabled provider must not be invoked by sync_all."""
    calls: list[str] = []

    class _SpyProvider(_EchoExternalProvider):
        @property
        def name(self) -> str:
            return "spy"

        def sync_turn(self, user_content, assistant_content, *, session_id) -> None:
            calls.append(session_id)

    manager = ExternalMemoryManager()
    manager.add_provider(_SpyProvider())
    manager.set_global_enabled(["builtin"])  # spy not enabled

    manager.sync_all(
        "u", "a", session_id="s1", agent_context="primary", enabled_override=["builtin"]
    )
    assert calls == []


def test_sync_all_failing_provider_does_not_block_builtin(tmp_path):
    """A provider whose sync_turn raises must not block other providers."""
    from app.infrastructure.memory.builtin_project import BuiltinProjectMemory

    memory_dir = tmp_path / "locals" / "external-memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "memory.md").write_text("", encoding="utf-8")
    builtin = BuiltinProjectMemory(tmp_path, memory_path="./locals/external-memory")
    builtin.initialize(session_id="", project_root=str(tmp_path))

    class _FailingProvider(_EchoExternalProvider):
        @property
        def name(self) -> str:
            return "failing"

        def sync_turn(self, user_content, assistant_content, *, session_id) -> None:
            raise RuntimeError("backend down")

    manager = ExternalMemoryManager()
    manager.add_provider(builtin)
    manager.add_provider(_FailingProvider())

    # Must not raise
    manager.sync_all(
        "Python FastAPI",
        "ok",
        session_id="s1",
        agent_context="primary",
        enabled_override=["builtin", "failing"],
    )
    # Builtin still wrote its observation despite failing provider throwing
    obs_file = tmp_path / "locals" / "external-memory" / "observations.md"
    assert obs_file.exists()
    assert "python" in obs_file.read_text(encoding="utf-8").lower()


class _SessionHookCapturingProvider(_EchoExternalProvider):
    """Captures on_session_switch / on_session_end / on_delegation invocations."""

    def __init__(self, name: str = "capturing"):
        self._name = name
        self.switch_calls: list[tuple[str, dict]] = []
        self.end_calls: list[str] = []
        self.delegation_calls: list[tuple[str, str, dict]] = []

    @property
    def name(self) -> str:
        return self._name

    def on_session_switch(self, new_session_id: str, **kwargs) -> None:
        self.switch_calls.append((new_session_id, dict(kwargs)))

    def on_session_end(self, session_id: str) -> None:
        self.end_calls.append(session_id)

    def on_delegation(self, task: str, result: str, **kwargs) -> None:
        self.delegation_calls.append((task, result, dict(kwargs)))


def test_on_session_switch_invokes_all_providers():
    """on_session_switch fans out to every registered provider with kwargs."""
    manager = ExternalMemoryManager()
    capturing_a = _SessionHookCapturingProvider(name="builtin")
    capturing_b = _SessionHookCapturingProvider(name="b")
    manager.add_provider(capturing_a)
    manager.add_provider(capturing_b)

    manager.on_session_switch("s1", parent_session_id="s0", reset=False)

    assert capturing_a.switch_calls == [("s1", {"parent_session_id": "s0", "reset": False})]
    assert capturing_b.switch_calls == [("s1", {"parent_session_id": "s0", "reset": False})]


def test_on_session_end_invokes_all_providers():
    """on_session_end fans out to every registered provider."""
    manager = ExternalMemoryManager()
    capturing_a = _SessionHookCapturingProvider(name="builtin")
    capturing_b = _SessionHookCapturingProvider(name="b")
    manager.add_provider(capturing_a)
    manager.add_provider(capturing_b)

    manager.on_session_end("s1")

    assert capturing_a.end_calls == ["s1"]
    assert capturing_b.end_calls == ["s1"]


def test_on_session_end_failing_provider_does_not_block_others():
    """A provider whose on_session_end raises must not block other providers."""
    manager = ExternalMemoryManager()

    class _FailingEnd(_SessionHookCapturingProvider):
        def on_session_end(self, session_id: str) -> None:
            raise RuntimeError("backend down")

    failing = _FailingEnd(name="builtin")
    survivor = _SessionHookCapturingProvider(name="survivor")
    manager.add_provider(failing)
    manager.add_provider(survivor)

    # Must not raise
    manager.on_session_end("s1")
    assert survivor.end_calls == ["s1"]


def test_on_delegation_invokes_all_providers():
    """on_delegation fans out task+result+child_session_id to every provider."""
    manager = ExternalMemoryManager()
    capturing_a = _SessionHookCapturingProvider(name="builtin")
    capturing_b = _SessionHookCapturingProvider(name="b")
    manager.add_provider(capturing_a)
    manager.add_provider(capturing_b)

    manager.on_delegation("summarize docs", "done", child_session_id="child-1")

    expected = [("summarize docs", "done", {"child_session_id": "child-1"})]
    assert capturing_a.delegation_calls == expected
    assert capturing_b.delegation_calls == expected


def test_on_delegation_failing_provider_does_not_block_others():
    """A provider whose on_delegation raises must not block other providers."""
    manager = ExternalMemoryManager()

    class _FailingDelegation(_SessionHookCapturingProvider):
        def on_delegation(self, task: str, result: str, **kwargs) -> None:
            raise RuntimeError("backend down")

    failing = _FailingDelegation(name="builtin")
    survivor = _SessionHookCapturingProvider(name="survivor")
    manager.add_provider(failing)
    manager.add_provider(survivor)

    # Must not raise
    manager.on_delegation("task", "result", child_session_id="child-1")
    assert survivor.delegation_calls == [("task", "result", {"child_session_id": "child-1"})]


class _CountingFailingProvider(_EchoExternalProvider):
    """Provider that fails prefetch N times then succeeds; counts invocations."""

    def __init__(self, name: str, fail_times: int):
        self._name = name
        self._fail_times = fail_times
        self._calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def calls(self) -> int:
        return self._calls

    def prefetch(self, query: str, *, session_id: str) -> str:
        self._calls += 1
        if self._calls <= self._fail_times:
            raise RuntimeError("backend down")
        return f"recovered after {self._calls} calls"


def test_breaker_skips_provider_after_threshold_consecutive_failures():
    """After threshold consecutive failures, the breaker skips subsequent calls."""
    manager = ExternalMemoryManager(breaker_threshold=3, breaker_cooldown_secs=120.0)
    failing = _CountingFailingProvider(name="flaky", fail_times=10)
    manager.add_provider(failing)
    manager.set_global_enabled(["flaky"])

    # 3 failures trip the breaker (threshold=3)
    for _ in range(3):
        manager.prefetch_all("q", session_id="s1", enabled_override=["flaky"])
    assert failing.calls == 3

    # 4th call should be skipped by the breaker — provider not invoked
    result = manager.prefetch_all("q", session_id="s1", enabled_override=["flaky"])
    assert failing.calls == 3  # no new invocation
    assert result == ""  # no content returned


def test_breaker_resets_failure_count_on_success():
    """A successful call resets the consecutive failure counter."""
    manager = ExternalMemoryManager(breaker_threshold=3, breaker_cooldown_secs=120.0)
    # Fail twice (below threshold), then succeed — counter should reset.
    provider = _CountingFailingProvider(name="flaky", fail_times=2)
    manager.add_provider(provider)
    manager.set_global_enabled(["flaky"])

    manager.prefetch_all("q", session_id="s1", enabled_override=["flaky"])  # fail 1
    manager.prefetch_all("q", session_id="s1", enabled_override=["flaky"])  # fail 2
    assert provider.calls == 2

    # 3rd call succeeds — counter resets, breaker still closed
    manager.prefetch_all("q", session_id="s1", enabled_override=["flaky"])
    assert provider.calls == 3

    # Two more failures should NOT trip (counter was reset)
    provider2 = _CountingFailingProvider(name="flaky2", fail_times=10)
    manager2 = ExternalMemoryManager(breaker_threshold=3, breaker_cooldown_secs=120.0)
    manager2.add_provider(provider2)
    manager2.set_global_enabled(["flaky2"])
    manager2.prefetch_all("q", session_id="s1", enabled_override=["flaky2"])  # fail 1
    manager2.prefetch_all("q", session_id="s1", enabled_override=["flaky2"])  # fail 2
    assert provider2.calls == 2  # still below threshold, not skipped


def test_breaker_recovers_after_cooldown_elapses():
    """After cooldown elapses, the breaker allows a retry."""
    now = [1000.0]

    def fake_clock() -> float:
        return now[0]

    manager = ExternalMemoryManager(
        breaker_threshold=2,
        breaker_cooldown_secs=60.0,
        breaker_clock=fake_clock,
    )
    provider = _CountingFailingProvider(name="flaky", fail_times=10)
    manager.add_provider(provider)
    manager.set_global_enabled(["flaky"])

    # 2 failures trip the breaker (threshold=2)
    manager.prefetch_all("q", session_id="s1", enabled_override=["flaky"])  # fail 1
    manager.prefetch_all("q", session_id="s1", enabled_override=["flaky"])  # fail 2
    assert provider.calls == 2

    # 3rd call skipped — breaker open
    manager.prefetch_all("q", session_id="s1", enabled_override=["flaky"])
    assert provider.calls == 2

    # Advance time past cooldown — breaker should reset and allow retry
    now[0] = 1000.0 + 61.0
    manager.prefetch_all("q", session_id="s1", enabled_override=["flaky"])
    assert provider.calls == 3  # invoked again (will fail, re-tripping)


def test_breaker_does_not_affect_other_providers():
    """A tripped breaker on one provider does not skip another."""
    manager = ExternalMemoryManager(breaker_threshold=2, breaker_cooldown_secs=120.0)

    class _AlwaysFailing(_EchoExternalProvider):
        @property
        def name(self) -> str:
            return "builtin"

        def prefetch(self, query: str, *, session_id: str) -> str:
            raise RuntimeError("backend down")

    class _AlwaysSucceed(_EchoExternalProvider):
        @property
        def name(self) -> str:
            return "healthy"

    manager.add_provider(_AlwaysFailing())
    manager.add_provider(_AlwaysSucceed())
    manager.set_global_enabled(["builtin", "healthy"])

    # Trip failing provider's breaker
    manager.prefetch_all("q", session_id="s1", enabled_override=["builtin", "healthy"])
    manager.prefetch_all("q", session_id="s1", enabled_override=["builtin", "healthy"])

    # 3rd call — failing skipped, healthy still called
    result = manager.prefetch_all(
        "q", session_id="s1", enabled_override=["builtin", "healthy"]
    )
    assert "echo recalled for: q" in result  # healthy still contributed
    assert "<memory-context>" in result


def test_breaker_skips_sync_turn_after_threshold_failures():
    """sync_turn path is also protected by the breaker."""
    calls: list[int] = []

    class _FailingSync(_EchoExternalProvider):
        @property
        def name(self) -> str:
            return "failing"

        def sync_turn(self, user_content, assistant_content, *, session_id) -> None:
            calls.append(1)
            raise RuntimeError("backend down")

    manager = ExternalMemoryManager(breaker_threshold=2, breaker_cooldown_secs=120.0)
    manager.add_provider(_FailingSync())
    manager.set_global_enabled(["failing"])

    manager.sync_all("u", "a", session_id="s1", agent_context="primary", enabled_override=["failing"])
    manager.sync_all("u", "a", session_id="s1", agent_context="primary", enabled_override=["failing"])
    assert len(calls) == 2

    # 3rd call skipped by breaker
    manager.sync_all("u", "a", session_id="s1", agent_context="primary", enabled_override=["failing"])
    assert len(calls) == 2  # no new invocation


def test_breaker_skips_handle_tool_call_after_threshold_failures():
    """handle_tool_call path is also protected by the breaker."""
    calls: list[int] = []

    class _FailingTool(_EchoExternalProvider):
        @property
        def name(self) -> str:
            return "failing"

        def get_tool_schemas(self):
            return [
                {
                    "name": "failing_tool",
                    "description": "fails",
                    "parameters": {"type": "object", "properties": {}},
                }
            ]

        def handle_tool_call(self, tool_name, args, **kwargs):
            calls.append(1)
            raise RuntimeError("backend down")

    manager = ExternalMemoryManager(breaker_threshold=2, breaker_cooldown_secs=120.0)
    manager.add_provider(_FailingTool())
    manager.set_global_enabled(["failing"])

    for _ in range(2):
        manager.handle_tool_call(
            "failing_tool", {}, agent_context="primary", session_id="s1", enabled_override=["failing"]
        )
    assert len(calls) == 2

    # 3rd call — breaker open, returns cooldown error without invoking provider
    result = manager.handle_tool_call(
        "failing_tool", {}, agent_context="primary", session_id="s1", enabled_override=["failing"]
    )
    assert len(calls) == 2  # no new invocation
    import json as _json
    payload = _json.loads(result)
    assert payload["success"] is False
    assert payload["error"] == "provider in cooldown"


def test_add_provider_rejects_second_external_provider():
    """G10: at most one external provider — second external is rejected with a warning."""
    manager = ExternalMemoryManager()

    class _ExternalWithTool(_EchoExternalProvider):
        def __init__(self, name: str) -> None:
            self._name = name

        @property
        def name(self) -> str:
            return self._name

        def get_tool_schemas(self):
            return [
                {
                    "name": f"{self._name}_tool",
                    "description": "test tool",
                    "parameters": {"type": "object", "properties": {}},
                }
            ]

    first = _ExternalWithTool("first")
    second = _ExternalWithTool("second")
    manager.add_provider(first)
    # Second external provider must be rejected (warning + return), not raise.
    manager.add_provider(second)

    names = [p["name"] for p in manager.list_providers()]
    assert names == ["first"]
    # Second provider's tool must not be routed.
    assert not manager.has_tool("second_tool")
    assert manager.has_tool("first_tool")


def test_add_provider_accepts_builtin_alongside_external():
    """G10: builtin provider is always accepted regardless of external presence."""
    manager = ExternalMemoryManager()

    class _Builtin(_EchoExternalProvider):
        @property
        def name(self) -> str:
            return "builtin"

    external = _EchoExternalProvider()  # name="echo"
    manager.add_provider(_Builtin())
    manager.add_provider(external)

    names = [p["name"] for p in manager.list_providers()]
    assert names == ["builtin", "echo"]


def test_add_provider_rejects_third_after_builtin_and_external():
    """G10: with builtin + 1 external already registered, a third external is rejected."""
    manager = ExternalMemoryManager()

    class _Builtin(_EchoExternalProvider):
        @property
        def name(self) -> str:
            return "builtin"

    class _Other(_EchoExternalProvider):
        def __init__(self, name: str) -> None:
            self._name = name

        @property
        def name(self) -> str:
            return self._name

    manager.add_provider(_Builtin())
    manager.add_provider(_Other("echo"))
    manager.add_provider(_Other("third"))

    names = [p["name"] for p in manager.list_providers()]
    assert names == ["builtin", "echo"]

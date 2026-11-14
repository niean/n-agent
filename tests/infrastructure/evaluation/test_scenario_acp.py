"""T12 真实 ACP 通道（plan T12，spec 5.1）反例与纪律测试。

反例覆盖（plan S1）：缺 agent 认证方法、session/new 后无 prompt 应答、
协议错误/断管、prompt 超时、意外重复会话（session/new 超时不重试）、
敏感 stderr。通道纪律：spawn 用类型化 argv + 独立进程组 + env 白名单
（复用 T11 subprocess_env）；副作用前登记 intent（acp_process 带进程
身份，防 PID 复用）；任一失败必须停止子进程（kill 记录）；协议阶段
每阶段至多一次尝试（超时不重跑，杜绝重复会话）；工具证据经正式端口
增量归属（同 T11）；stderr 脱敏有界。
"""
import asyncio
import json

import pytest

from app.infrastructure.evaluation.scenario_process import (
    subprocess_env,
)


class _FakeError(Exception):
    pass


class _FakeHandle:
    def __init__(self, *, process_identity="pid=77:start=1700000100.0",
                 stderr_tail=""):
        self.process_identity = process_identity
        self._stderr_tail = stderr_tail
        self.kills = []

    def stderr_text(self):
        return self._stderr_tail

    async def kill(self):
        self.kills.append("kill")


class _FakeDriver:
    """SDK 驱动替身：记录调用序，按 stage 注入失败/延迟。"""

    def __init__(self, handle, *, auth_methods=("prov-agent",),
                 auth_kinds=None, fail=None, delays=None,
                 prompt_stop_reason="end_turn"):
        self.handle = handle
        self.calls = []
        self._auth_methods = list(auth_methods)
        self._auth_kinds = auth_kinds or {}
        self._fail = fail or {}
        self._delays = delays or {}
        self._prompt_stop_reason = prompt_stop_reason

    async def _maybe(self, stage):
        delay = self._delays.get(stage)
        if delay:
            await asyncio.sleep(delay)
        if stage in self._fail:
            raise _FakeError(self._fail[stage])

    async def initialize(self):
        self.calls.append("initialize")
        await self._maybe("initialize")
        return {
            "auth_methods": [
                {"id": m, "kind": self._auth_kinds.get(m, "agent")}
                for m in self._auth_methods
            ],
        }

    async def authenticate(self, method_id):
        self.calls.append(("authenticate", method_id))
        await self._maybe("authenticate")
        return {"ok": True}

    async def new_session(self, cwd):
        self.calls.append(("new_session", cwd))
        await self._maybe("new_session")
        return {"session_id": "acp-new-1"}

    async def prompt(self, session_id, text):
        self.calls.append(("prompt", session_id, text))
        await self._maybe("prompt")
        return {"stop_reason": self._prompt_stop_reason, "text": "北京 晴 22°C"}


def _request(turns=None, *, session_id="eval-experiment-e1-acp-weather-chat-r0-a0",
             execution_id="e1", case_id="acp-weather-chat"):
    return {
        "session": {"namespace": "ns", "session_id": session_id},
        "turns": turns or [
            {"step_id": "turn-1", "message": "查一下北京天气。"}
        ],
        "scenario_context": {"execution_id": execution_id, "case_id": case_id},
    }


async def _async(value):
    return value


def _runner(handle, driver, **overrides):
    from app.infrastructure.evaluation.scenario_adapters import ScenarioAcpRunner

    kwargs = dict(
        spawn=lambda: _async(handle),
        driver_factory=lambda h: driver,
        tool_reader=lambda session_id: _async([]),
    )
    kwargs.update(overrides)
    return ScenarioAcpRunner(**kwargs)


def _run(runner, request):
    return asyncio.run(runner.run_target(None, request))


# ---------------------------------------------------------------------------
# 受控 spawn 原语（scenario_process）
# ---------------------------------------------------------------------------


def test_spawn_process_typed_argv_own_group_and_whitelisted_env():
    """spawn 用类型化 argv（create_subprocess_exec 语义，无 shell）、
    独立进程组、env 白名单（复用 subprocess_env）。"""
    from app.infrastructure.evaluation.scenario_process import spawn_process

    captured = {}

    class _Proc:
        pid = 5566

        def __init__(self):
            self.stdin = object()
            self.stdout = object()
            self.stderr = object()

        def kill(self):
            pass

        async def wait(self):
            return 0

    killed = []
    import os as _os
    import signal as _signal

    original_killpg = _os.killpg
    _os.killpg = lambda pid, sig: killed.append((pid, sig))

    async def fake_create(*argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return _Proc()

    try:
        async def main():
            handle = await spawn_process(
                ["n-agent", "acp"],
                create=fake_create,
                env={"N_AGENT_SQLITE_PATH": "/app/locals/sessions.db",
                     "N_AGENT_E2E_JUDGE_API_KEY": "sk-secret", "OTHER": "x"},
                stderr_sink=lambda text: None,
            )
            await handle.kill()

        asyncio.run(main())
    finally:
        _os.killpg = original_killpg
    assert killed == [(5566, _signal.SIGKILL)]
    assert captured["argv"] == ("n-agent", "acp")
    assert "shell" not in captured
    assert captured["start_new_session"] is True
    env = captured["env"]
    assert env["N_AGENT_SQLITE_PATH"] == "/app/locals/sessions.db"
    assert "N_AGENT_E2E_JUDGE_API_KEY" not in env
    assert "OTHER" not in env


def test_kill_process_identity_kills_group_and_treats_absent_as_stopped():
    import os as _os
    import signal

    from app.infrastructure.evaluation.scenario_process import (
        kill_process_identity,
    )

    killed = []
    original = _os.killpg
    _os.killpg = lambda pid, sig: killed.append((pid, sig))
    try:
        # 进程仍存活：整组 SIGKILL
        assert asyncio.run(kill_process_identity(
            "pid=99:start=1700000000.0", alive=lambda pid: True,
        )) == "stopped"
        assert (99, signal.SIGKILL) in killed
        # 进程已不存在（幂等释放）：不杀，直接 stopped
        killed.clear()
        assert asyncio.run(kill_process_identity(
            "pid=99:start=1700000000.0", alive=lambda pid: False,
        )) == "stopped"
        assert killed == []
    finally:
        _os.killpg = original


def test_kill_process_identity_refuses_reused_pid_without_start_match():
    """PID 复用防护：进程存在但启动时刻与登记不符时拒绝杀（防误杀）。"""
    import os as _os

    from app.infrastructure.evaluation.scenario_process import (
        kill_process_identity,
    )

    killed = []
    original = _os.killpg
    _os.killpg = lambda pid, sig: killed.append((pid, sig))
    try:
        with pytest.raises(RuntimeError, match="manual_attention"):
            asyncio.run(kill_process_identity(
                "pid=99:start=1700000000.0", alive=lambda pid: True,
                start_time=lambda pid: 1888888888.0,
            ))
        assert killed == []
    finally:
        _os.killpg = original


# ---------------------------------------------------------------------------
# ACP 适配器纪律
# ---------------------------------------------------------------------------


def test_acp_happy_path_records_protocol_and_kills_process():
    hooks = []

    def before(process_identity, context):
        hooks.append(("before", process_identity))
        return "e1:acp-weather-chat:acp_process:eval-experiment-e1-acp-weather-chat-r0-a0"

    def confirmed(business_key, session_id, conversation_id):
        hooks.append(("confirmed", business_key, session_id))

    handle = _FakeHandle()
    driver = _FakeDriver(handle)
    result = _run(_runner(handle, driver, acp_intent_before=before,
                          acp_intent_confirmed=confirmed), _request())
    assert result["status"] == "succeeded"
    evidence = result["output"]["scenario_evidence"]
    # 副作用前登记 intent（spawn 后、协议前），成功后回填会话 ID
    assert hooks[0] == ("before", "pid=77:start=1700000100.0")
    assert hooks[1][0] == "confirmed"
    assert hooks[1][2] == "acp-new-1"
    # 协议顺序：initialize -> authenticate -> session/new -> session/prompt
    assert driver.calls[0] == "initialize"
    assert driver.calls[1] == ("authenticate", "prov-agent")
    assert driver.calls[2] == ("new_session", "/workspace")
    assert driver.calls[3][:2] == ("prompt", "acp-new-1")
    assert evidence["protocol"] == [
        "initialize", "authenticate", "session/new", "session/prompt",
    ]
    acp = evidence["acp"]
    assert acp["source"] == "acp"
    assert acp["session_id"] == "acp-new-1"
    assert acp["process_identity"] == "pid=77:start=1700000100.0"
    assert evidence["status"] == "complete"
    assert result["output"]["final_answer"] == "北京 晴 22°C"
    # 子进程为单项临时进程：成功也必须停止（kill 恰一次）
    assert handle.kills == ["kill"]


def test_acp_binds_step_run_ids_from_tool_port():
    """工具调用增量归属（同 T11）：每轮 prompt 后经正式端口读工具记录，
    新 run_id 归属当轮，记录去重聚合。"""
    turn_call = {"tool_name": "web_fetch", "run_id": "run-a",
                 "status": "success", "result": "weather"}

    async def tool_reader(session_id):
        return [turn_call]

    handle = _FakeHandle()
    driver = _FakeDriver(handle)
    result = _run(_runner(handle, driver, tool_reader=tool_reader), _request())
    evidence = result["output"]["scenario_evidence"]
    steps = {s["step_id"]: s for s in evidence["steps"]}
    assert steps["turn-1"]["run_id"] == "run-a"
    assert evidence["tool_calls"] == [turn_call]


def test_acp_missing_agent_auth_method_refuses_session():
    """缺 authenticate（无 agent 认证方法，仅终端引导）：不建会话、
    不发 prompt；无持久副作用（进程已杀）允许安全重试。"""
    handle = _FakeHandle()
    driver = _FakeDriver(
        handle, auth_methods=("terminal-setup",),
        auth_kinds={"terminal-setup": "terminal"},
    )
    result = _run(_runner(handle, driver), _request())
    assert result["status"] == "transport_error"
    assert result["error_type"] == "acp_auth_unavailable"
    assert "side_effects_unobserved" not in result
    assert driver.calls == ["initialize"]
    assert handle.kills == ["kill"]


def test_acp_protocol_error_before_session_is_retry_safe():
    handle = _FakeHandle()
    driver = _FakeDriver(handle, fail={"new_session": "invalid params"})
    result = _run(_runner(handle, driver), _request())
    assert result["status"] == "transport_error"
    assert result["error_type"] == "acp_protocol_failed"
    assert "side_effects_unobserved" not in result
    assert not any(c[0] == "prompt" for c in driver.calls if isinstance(c, tuple))
    assert handle.kills == ["kill"]


def test_acp_disconnect_after_session_new_no_retry_ledger_known():
    """断管（session/new 后 prompt 断开）：会话 id 在案（settle 保留为
    证据）、进程已停 -> 不标 unknown；服务端副作用可能已发生 ->
    side_effects_unobserved 不重跑目标；子进程必须停止。"""
    unknowns = []

    def unknown(business_key, reason):
        unknowns.append((business_key, reason))

    handle = _FakeHandle()
    driver = _FakeDriver(handle, fail={"prompt": "connection closed"})
    result = _run(_runner(handle, driver, acp_intent_before=lambda pid, ctx: "bk",
                          acp_intent_unknown=unknown), _request())
    assert result["status"] == "transport_error"
    assert result["side_effects_unobserved"] is True
    # 账目全知（会话 id 在案、进程已杀）：不标 unknown
    assert unknowns == []
    evidence = result["output"]["scenario_evidence"]
    assert evidence["status"] == "incomplete"
    assert evidence["acp"]["session_id"] == "acp-new-1"
    assert handle.kills == ["kill"]
    assert len([c for c in driver.calls if isinstance(c, tuple) and c[0] == "prompt"]) == 1


def test_acp_prompt_timeout_kills_process_and_does_not_retry():
    handle = _FakeHandle()
    driver = _FakeDriver(handle, delays={"prompt": 1.0})
    result = _run(
        _runner(handle, driver, stage_timeout_seconds=0.05,
                acp_intent_before=lambda pid, ctx: "bk",
                acp_intent_unknown=lambda bk, reason: _async(None)),
        _request(),
    )
    assert result["status"] == "transport_error"
    assert result["side_effects_unobserved"] is True
    evidence = result["output"]["scenario_evidence"]
    assert evidence["status"] == "incomplete"
    assert "timed out" in evidence["reason"]
    assert handle.kills == ["kill"]
    assert len([c for c in driver.calls if isinstance(c, tuple) and c[0] == "prompt"]) == 1


def test_acp_session_new_timeout_is_not_retried_no_duplicate_session():
    """意外重复会话防护：session/new 超时后绝不上界重试（第二次
    session/new 会造成重复会话）；fail-closed + intent unknown。"""
    unknowns = []

    def unknown(business_key, reason):
        unknowns.append((business_key, reason))

    handle = _FakeHandle()
    driver = _FakeDriver(handle, delays={"new_session": 1.0})
    result = _run(
        _runner(handle, driver, stage_timeout_seconds=0.05,
                acp_intent_before=lambda pid, ctx: "bk",
                acp_intent_unknown=unknown),
        _request(),
    )
    assert result["status"] == "transport_error"
    assert result["side_effects_unobserved"] is True
    # 关键断言：new_session 恰好一次（无重试 -> 无重复会话）
    assert len([c for c in driver.calls if isinstance(c, tuple) and c[0] == "new_session"]) == 1
    assert unknowns and "session/new" in unknowns[0][1]
    assert handle.kills == ["kill"]


def test_acp_stderr_is_redacted_and_bounded():
    handle = _FakeHandle(stderr_tail="boot ok token=sk-secret-abcdef " + "x" * 9000)
    driver = _FakeDriver(handle)
    result = _run(
        _runner(handle, driver,
                redactor=lambda text: text.replace("sk-secret-abcdef", "[REDACTED]")),
        _request(),
    )
    acp = result["output"]["scenario_evidence"]["acp"]
    assert "sk-secret-abcdef" not in acp["stderr"]
    assert "[REDACTED]" in acp["stderr"]
    assert len(acp["stderr"]) <= 4100
    assert "truncated" in acp["stderr"]


def test_acp_intent_register_failure_kills_process_no_side_effects():
    def before(process_identity, context):
        raise RuntimeError("ledger down")

    handle = _FakeHandle()
    driver = _FakeDriver(handle)
    result = _run(_runner(handle, driver, acp_intent_before=before), _request())
    assert result["status"] == "business_error"
    assert "acp_intent_register_failed" in result["error_type"]
    assert driver.calls == []
    assert handle.kills == ["kill"]


def test_acp_spawn_failure_is_honest_not_absence():
    async def broken_spawn():
        raise FileNotFoundError(2, "No such file", "n-agent")

    from app.infrastructure.evaluation.scenario_adapters import ScenarioAcpRunner

    registered = []
    runner = ScenarioAcpRunner(
        spawn=broken_spawn,
        driver_factory=lambda h: _FakeDriver(h),
        acp_intent_before=lambda pid, ctx: registered.append(pid) or "bk",
        tool_reader=lambda session_id: _async([]),
    )
    result = _run(runner, _request())
    assert result["status"] == "transport_error"
    assert result["error_type"] == "acp_spawn_failed"
    assert result["side_effects_unobserved"] is True
    assert registered == []


def test_acp_session_new_refusal_is_business_error():
    """agent 拒绝（stop_reason=refusal 形态）：如实 business_error，
    不伪装传输失败。"""
    handle = _FakeHandle()
    driver = _FakeDriver(handle, prompt_stop_reason="refusal")
    result = _run(_runner(handle, driver), _request())
    assert result["status"] == "business_error"
    assert handle.kills == ["kill"]


# ---------------------------------------------------------------------------
# protocol_sequence 断言（证据契约）
# ---------------------------------------------------------------------------


def _evidence_content(evidence, assertions=None):
    return {
        "assertions": assertions or [
            {"assertion_type": "protocol_sequence", "step_id": None,
             "params": {"entries": ["initialize", "authenticate",
                                    "session/new", "session/prompt"]}},
        ],
        "scenario_evidence": evidence,
    }


def _complete_acp_evidence(protocol=None):
    return {
        "status": "complete",
        "protocol": protocol or ["initialize", "authenticate",
                                 "session/new", "session/prompt"],
        "acp": {"source": "acp", "session_id": "acp-new-1",
                "process_identity": "pid=77:start=1700000100.0",
                "stages": [], "stderr": ""},
    }


def test_protocol_sequence_assertion_passes_on_complete_acp_flow():
    from app.application.evaluation_scenario_assertions import (
        evaluate_scenario_assertions,
    )

    value = evaluate_scenario_assertions(_evidence_content(_complete_acp_evidence()))
    assert value.status == "value"
    assert value.typed_value is True


def test_protocol_sequence_assertion_fails_on_missing_stage():
    from app.application.evaluation_scenario_assertions import (
        evaluate_scenario_assertions,
    )

    value = evaluate_scenario_assertions(_evidence_content(
        _complete_acp_evidence(protocol=["initialize", "session/new",
                                         "session/prompt"]),
    ))
    assert value.typed_value is False


# ---------------------------------------------------------------------------
# 路由与组合根接线
# ---------------------------------------------------------------------------


def test_acp_profile_routes_to_scenario_acp_runner():
    """profile=acp 路由到 ScenarioAcpRunner（冻结配置纪律同源），普通
    agent 适配不能替代 ACP。"""
    import inspect

    from app.infrastructure.evaluation import execution_runtime

    source = inspect.getsource(execution_runtime.ProfileRoutingTargetRunner)
    assert '"acp"' in source
    assert "acp_factory" in source


def test_main_wires_acp_ports():
    """组合根接线：acp intent 钩子/工具端口/脱敏器/spawn 原语/stopper
    acp 分支就位；acp 准入随接线启用。"""
    import inspect

    from app import main as app_main

    source = inspect.getsource(app_main)
    assert "_scenario_acp_intent_before" in source
    assert "ScenarioAcpRunner(" in source
    assert "acp_runner=_scenario_acp_runner" in source or "acp_factory" in source
    assert "acp_process" in source

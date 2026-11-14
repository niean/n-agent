"""T11 真实 CLI 通道（plan T11，spec 5.1）反例与纪律测试。

反例覆盖（plan S1）：非零退出、超时、无法唯一解析会话、错误数据库、
进程身份变化、递归领取实验。通道纪律：类型化 argv 禁 shell=True、
独立进程组、环境白名单（N_AGENT_* 非 E2E 密钥 + 递归防护）、输出
脱敏有界、副作用前登记 intent、解析失败不重跑目标。
"""
import asyncio
import json

import pytest

from app.infrastructure.evaluation.scenario_process import (
    ProcessResult,
    run_process,
    subprocess_env,
)


def _proc(*, exit_code=0, stdout="", stderr="", timed_out=False,
          spawn_error=None, identity="pid=42:start=1700000000.0"):
    return ProcessResult(
        argv=("n-agent",), exit_code=exit_code, stdout=stdout, stderr=stderr,
        duration_seconds=0.1, process_identity=identity,
        timed_out=timed_out, spawn_error=spawn_error,
    )


def _request(turns=None, *, session_id="eval-experiment-e1-cli-weather-chat-r0-a0",
             execution_id="e1", case_id="cli-weather-chat"):
    return {
        "session": {"namespace": "ns", "session_id": session_id},
        "turns": turns or [
            {"step_id": "turn-1", "message": "查一下北京天气。"}
        ],
        "scenario_context": {"execution_id": execution_id, "case_id": case_id},
    }


def _cli_runner(**overrides):
    from app.infrastructure.evaluation.scenario_adapters import ScenarioCliRunner

    kwargs = dict(
        tool_reader=lambda session_id: _async([]),
    )
    kwargs.update(overrides)
    return ScenarioCliRunner(**kwargs)


async def _async(value):
    return value


def _run(runner, request):
    return asyncio.run(runner.run_target(None, request))


# ---------------------------------------------------------------------------
# 受控子进程原语
# ---------------------------------------------------------------------------


def test_subprocess_env_whitelist_blocks_e2e_secrets_and_guards_recursion():
    env = subprocess_env({
        "PATH": "/usr/bin", "HOME": "/root",
        "N_AGENT_SQLITE_PATH": "/app/locals/sessions.db",
        "N_AGENT_E2E_JUDGE_API_KEY": "sk-secret",
        "N_AGENT_E2E_BROWSER_PASSWORD": "pw",
        "UNRELATED": "x",
    })
    assert env["N_AGENT_SQLITE_PATH"] == "/app/locals/sessions.db"
    assert env["PATH"] == "/usr/bin"
    # E2E 密钥绝不下发；递归防护显式关闭评估 worker
    assert "N_AGENT_E2E_JUDGE_API_KEY" not in env
    assert "N_AGENT_E2E_BROWSER_PASSWORD" not in env
    assert env["N_AGENT_EVALUATION_ENABLED"] == "false"
    assert "UNRELATED" not in env


def test_run_process_uses_typed_argv_no_shell_and_records_identity(tmp_path):
    """类型化 argv 执行（禁 shell=True）、独立进程组、进程身份按
    pid=<n>:start=<ts> 记录（资源账目契约同形）。"""
    captured = {}

    def fake_popen(argv, **kwargs):
        captured.update(kwargs)
        captured["argv"] = argv

        class _P:
            pid = 4242

            def communicate(self, timeout=None):
                return ("out", "err")

            returncode = 0

        return _P()

    result = run_process(
        ["n-agent", "chat", "hi", "--no-stream"],
        timeout_seconds=5, popen_factory=fake_popen,
    )
    assert result.exit_code == 0
    assert result.stdout == "out"
    # shell 禁用 + 独立进程组
    assert captured["shell"] is False
    assert captured["start_new_session"] is True
    # 进程身份与资源账目契约同形
    import re

    assert re.match(r"^pid=\d+:start=\d+(\.\d+)?$", result.process_identity)


def test_run_process_spawn_failure_recorded_not_raised():
    def fake_popen(argv, **kwargs):
        raise FileNotFoundError(2, "No such file", "n-agent")

    result = run_process(
        ["n-agent", "chat", "hi"], timeout_seconds=5,
        popen_factory=fake_popen,
    )
    # spawn 失败如实记录（不当不存在也不崩账），exit_code None
    assert result.exit_code is None
    assert "FileNotFoundError" in result.spawn_error
    assert result.process_identity is None


def test_run_process_redacts_and_bounds_output():
    def fake_popen(argv, **kwargs):
        class _P:
            pid = 7

            def communicate(self, timeout=None):
                return ("x" * 9000, "token=sk-secret-abcdef tail")

            returncode = 0

        return _P()

    result = run_process(
        ["n-agent", "chat", "hi"], timeout_seconds=5,
        redactor=lambda text: text.replace("sk-secret-abcdef", "[REDACTED]"),
        output_limit=100, popen_factory=fake_popen,
    )
    assert "sk-secret-abcdef" not in result.stderr
    assert "[REDACTED]" in result.stderr
    assert result.stdout.startswith("x")
    assert "truncated" in result.stdout


def test_run_process_timeout_kills_process_group():
    import signal

    killed = []

    def fake_popen(argv, **kwargs):
        class _P:
            pid = 99

            def communicate(self, timeout=None):
                raise __import__("subprocess").TimeoutExpired(argv, timeout)

            def kill(self):
                killed.append("kill")

            returncode = -9

        return _P()

    original_killpg = __import__("os").killpg
    import os as _os

    def fake_killpg(pid, sig):
        killed.append((pid, sig))

    _os.killpg = fake_killpg
    try:
        result = run_process(
            ["n-agent", "chat", "hi"], timeout_seconds=0.1,
            popen_factory=fake_popen,
        )
    finally:
        _os.killpg = original_killpg
    assert result.timed_out is True
    # 独立进程组：整组 SIGKILL
    assert (99, signal.SIGKILL) in killed


# ---------------------------------------------------------------------------
# CLI 适配器纪律
# ---------------------------------------------------------------------------


def _queue_runner(responses, *, hooks=None, **overrides):
    """按调用序返回预置结果的 run_process 替身。

    调用序：sessions（建会话）-> sessions --browse -> chat×每轮。"""
    calls = []
    queue = list(responses)

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return queue.pop(0)

    runner = _cli_runner(run_process=fake_run, **overrides)
    return runner, calls


def test_cli_happy_path_binds_session_and_collects_evidence():
    hooks = []

    def before(conversation_id, context):
        hooks.append(("before", conversation_id))
        return f"e1:cli-weather-chat:cli_conversation:{conversation_id}"

    def confirmed(business_key, session_id, conversation_id):
        hooks.append(("confirmed", business_key, session_id))

    def tool_reader(session_id):
        return _async([
            {"tool_name": "web_fetch", "status": "success",
             "result": json.dumps({"url": "https://wttr.in/beijing"})},
        ])

    runner, calls = _queue_runner(
        [
            _proc(exit_code=0, stdout=""),                      # sessions（建会话）
            _proc(exit_code=0, stdout=json.dumps(
                [{"session_id": "cli-sess-1", "conversation_id": "c1"}])),  # browse
            _proc(exit_code=0, stdout="北京 晴 22°C"),           # chat turn-1
        ],
        tool_reader=tool_reader,
        cli_intent_before=before, cli_intent_confirmed=confirmed,
    )
    result = _run(runner, _request())
    assert result["status"] == "succeeded"
    evidence = result["output"]["scenario_evidence"]
    # intent 先于副作用，成功后回填真实会话 ID
    assert hooks[0] == ("before", "eval-experiment-e1-cli-weather-chat-r0-a0")
    assert hooks[1][0] == "confirmed"
    assert hooks[1][2] == "cli-sess-1"
    # 类型化 argv：chat 命令逐轮真实发送
    assert calls[2][:2] == ["n-agent", "chat"]
    assert "--conversation-id" in calls[2]
    cli = evidence["cli"]
    assert cli["source"] == "cli"
    assert cli["session_id"] == "cli-sess-1"
    step = cli["steps"][0]
    assert step["exit_code"] == 0
    assert "北京" in step["stdout"]
    assert step["process_identity"] == "pid=42:start=1700000000.0"
    assert evidence["tool_calls"][0]["tool_name"] == "web_fetch"
    assert evidence["status"] == "complete"
    assert result["output"]["final_answer"] == "北京 晴 22°C"


def test_cli_tool_port_error_cannot_become_complete_empty_evidence():
    async def broken_reader(session_id):
        raise ConnectionError("tool port unavailable")
    runner, _ = _queue_runner([
        _proc(stdout=""), _proc(stdout=json.dumps([{"session_id": "cli-sess-1"}])),
        _proc(stdout="北京晴22度"),
    ], tool_reader=broken_reader)
    evidence = _run(runner, _request())["output"]["scenario_evidence"]
    assert evidence["status"] == "incomplete"
    assert "tool evidence read failed" in evidence["reason"]


def test_cli_binds_step_run_ids_from_tool_port():
    """工具调用归属（run_id 权威索引）：CLI 子进程不透出 run_id，
    适配器在每轮 chat 后经正式端口读工具记录，把新出现的 run_id
    归属到当轮（增量归属），使 step 级 tool_succeeded 断言可核。"""
    turn1_call = {"tool_name": "web_fetch", "run_id": "run-a",
                  "status": "success", "result": "weather"}
    turn2_call = {"tool_name": "web_fetch", "run_id": "run-b",
                  "status": "success", "result": "weather2"}
    reads = {"n": 0}

    async def tool_reader(session_id):
        reads["n"] += 1
        if reads["n"] == 1:
            return [turn1_call]
        return [turn1_call, turn2_call]

    runner, calls = _queue_runner(
        [
            _proc(exit_code=0, stdout=""),                      # sessions
            _proc(exit_code=0, stdout=json.dumps(
                [{"session_id": "s1", "conversation_id": "c1"}])),
            _proc(exit_code=0, stdout="第一轮回答"),             # turn-1
            _proc(exit_code=0, stdout="第二轮回答"),             # turn-2
        ],
        tool_reader=tool_reader,
    )
    request = _request(turns=[
        {"step_id": "turn-1", "message": "问一"},
        {"step_id": "turn-2", "message": "问二"},
    ])
    result = _run(runner, request)
    evidence = result["output"]["scenario_evidence"]
    steps = {s["step_id"]: s for s in evidence["steps"]}
    assert steps["turn-1"]["run_id"] == "run-a"
    assert steps["turn-2"]["run_id"] == "run-b"
    # 工具记录去重聚合（每轮全量读，不重复计入）
    assert evidence["tool_calls"] == [turn1_call, turn2_call]
    assert result["output"]["scenario_evidence"]["status"] == "complete"


def test_cli_nonzero_exit_is_incomplete_not_pass():
    runner, calls = _queue_runner(
        [
            _proc(exit_code=0, stdout=""),
            _proc(exit_code=0, stdout=json.dumps(
                [{"session_id": "s1", "conversation_id": "c1"}])),
            _proc(exit_code=2, stderr="boom", stdout=""),
        ],
    )
    result = _run(runner, _request())
    assert result["status"] == "succeeded"  # 执行本身完成，证据如实
    evidence = result["output"]["scenario_evidence"]
    assert evidence["status"] == "incomplete"
    assert "exit_code=2" in evidence["reason"]
    assert result["output"]["completion_reported"] is False


def test_cli_timeout_is_incomplete_and_recorded():
    runner, _ = _queue_runner(
        [
            _proc(exit_code=0, stdout=""),
            _proc(exit_code=0, stdout=json.dumps(
                [{"session_id": "s1", "conversation_id": "c1"}])),
            _proc(timed_out=True, exit_code=-9, stdout="", stderr=""),
        ],
    )
    result = _run(runner, _request())
    evidence = result["output"]["scenario_evidence"]
    assert evidence["status"] == "incomplete"
    assert "timed out" in evidence["reason"]
    assert evidence["cli"]["steps"][0]["timed_out"] is True


def test_cli_ambiguous_session_resolution_rejects_and_marks_unknown():
    unknowns = []

    def unknown(business_key, reason):
        unknowns.append((business_key, reason))

    runner, calls = _queue_runner(
        [
            _proc(exit_code=0, stdout=""),   # sessions（副作用已可能发生）
            _proc(exit_code=0, stdout=json.dumps(
                [{"session_id": "s1"}, {"session_id": "s2"}])),
        ],
        cli_intent_before=lambda cid, ctx: f"e1:cli-weather-chat:cli_conversation:{cid}",
        cli_intent_unknown=unknown,
    )
    result = _run(runner, _request())
    # 拒绝猜测：transport_error + side_effects_unobserved，不发送 chat
    assert result["status"] == "transport_error"
    assert result["error_type"] == "cli_session_unresolved"
    assert result["side_effects_unobserved"] is True
    assert len(calls) == 2  # 只有 sessions/browse，无 chat
    assert unknowns and "ambiguous" in unknowns[0][1]


def test_cli_broken_browse_output_is_fail_closed():
    """错误数据库/非法输出：browse 非法 JSON 如实标记 unknown，
    不猜会话、不发消息。"""
    runner, calls = _queue_runner(
        [
            _proc(exit_code=0, stdout=""),
            _proc(exit_code=1, stderr="sqlite disk I/O error"),
        ],
        cli_intent_before=lambda cid, ctx: f"e1:cli-weather-chat:cli_conversation:{cid}",
        cli_intent_unknown=lambda bk, reason: _async(None),
    )
    result = _run(runner, _request())
    assert result["status"] == "transport_error"
    assert result["side_effects_unobserved"] is True
    assert len(calls) == 2


def test_cli_spawn_failure_is_honest_not_absence():
    runner, _ = _queue_runner(
        [
            _proc(spawn_error="FileNotFoundError: n-agent"),
            _proc(spawn_error="FileNotFoundError: n-agent"),
        ],
    )
    result = _run(runner, _request())
    assert result["status"] == "transport_error"
    assert result["side_effects_unobserved"] is True


# ---------------------------------------------------------------------------
# cli_channel 断言
# ---------------------------------------------------------------------------


def _evidence_content(evidence):
    return {
        "assertions": [
            {"assertion_type": "cli_channel", "step_id": None, "params": {}},
        ],
        "scenario_evidence": evidence,
    }


def _complete_evidence():
    return {
        "status": "complete",
        "cli": {
            "source": "cli", "conversation_id": "c1", "session_id": "s1",
            "steps": [{"step_id": "turn-1", "exit_code": 0,
                       "stdout": "北京 晴", "timed_out": False,
                       "spawn_error": None,
                       "process_identity": "pid=42:start=1700000000.0"}],
        },
    }


def test_cli_channel_assertion_passes_on_complete_real_channel():
    from app.application.evaluation_scenario_assertions import (
        evaluate_scenario_assertions,
    )

    value = evaluate_scenario_assertions(_evidence_content(_complete_evidence()))
    assert value.status == "value"
    assert value.typed_value is True


def test_cli_channel_assertion_fails_on_failed_step_or_non_cli_source():
    from app.application.evaluation_scenario_assertions import (
        evaluate_scenario_assertions,
    )

    broken = _complete_evidence()
    broken["cli"]["steps"][0]["exit_code"] = 2
    assert evaluate_scenario_assertions(
        _evidence_content(broken)
    ).typed_value is False
    impersonated = _complete_evidence()
    impersonated["cli"]["source"] = "dashboard"
    assert evaluate_scenario_assertions(
        _evidence_content(impersonated)
    ).typed_value is False
    unbound = _complete_evidence()
    unbound["cli"]["session_id"] = None
    assert evaluate_scenario_assertions(
        _evidence_content(unbound)
    ).typed_value is False


def test_cli_channel_assertion_skips_on_incomplete_evidence():
    from app.application.evaluation_scenario_assertions import (
        evaluate_scenario_assertions,
    )

    incomplete = _complete_evidence()
    incomplete["status"] = "incomplete"
    incomplete["reason"] = "cli step turn-1 timed out"
    value = evaluate_scenario_assertions(_evidence_content(incomplete))
    assert value.status == "skipped"
    assert value.typed_value is None


# ---------------------------------------------------------------------------
# 路由与组合根接线
# ---------------------------------------------------------------------------


def test_cli_profile_routes_to_scenario_cli_runner():
    """profile=cli 路由到 ScenarioCliRunner（冻结配置纪律同源），普通
    agent 适配不能替代 CLI。"""
    import inspect

    from app.infrastructure.evaluation import execution_runtime

    source = inspect.getsource(execution_runtime.ProfileRoutingTargetRunner)
    assert '"cli"' in source
    assert "cli_factory" in source


def test_converter_declares_cli_channel_assertion():
    """转换规则：cli 题生成 cli_channel 断言（协议入口真实核验），
    expect.tools 的 tool_succeeded 照常生成。"""
    from app.application.evaluation_e2e_conversion import convert_e2e_conversations

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "cli-weather-chat.json"
        src.write_text(json.dumps({
            "schema_version": 1,
            "id": "cli-weather-chat",
            "title": "TUI对话交互",
            "source_session_id": "cli-x",
            "channel": "cli",
            "requires": ["web"],
            "options": {},
            "messages": [{
                "id": "m1",
                "content": "查一下北京天气。",
                "expect": {"rubric": "回答包含北京当前天气", "tools": ["web_fetch"]},
            }],
            "side_effects": [],
            "cleanup": ["session"],
        }, ensure_ascii=False), encoding="utf-8")
        result = convert_e2e_conversations(Path(tmp))
    case = result.manifest["cases"][0]
    assert case["scenario"]["execution"]["profile"] == "cli"
    kinds = [a["assertion_type"] for a in case["scenario"]["assertions"]]
    assert "cli_channel" in kinds
    assert "tool_succeeded" in kinds


def test_main_wires_cli_ports():
    """组合根接线：cli intent 钩子/工具端口/脱敏器/进程原语就位；
    cli 准入随接线启用。"""
    import inspect

    from app import main as app_main

    source = inspect.getsource(app_main)
    assert "_scenario_cli_intent_before" in source
    assert "ScenarioCliRunner(" in source
    assert "cli_runner=_scenario_cli_runner" in source or (
        "cli_factory" in source
    )

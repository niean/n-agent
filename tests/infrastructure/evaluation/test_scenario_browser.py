"""T9：多轮浏览器接入测试（plan T9 S1）。

覆盖反例：
- 两轮跨浏览器会话：navigate/close 逐步工具证据 + 浏览器会话证据
  （正式端口 list_sessions 形态），closed 或真实不存在均规范化；
- 首轮失败末轮成功：turn-1 error 结局按业务失败，不因末轮成功而通过；
- web_fetch 替代导航：禁止工具出现在正式端口证据即失败；
- close 失败：browser_close 工具失败或浏览器仍 active 不得判成功；
- 读取失败不当不存在：浏览器状态读取失败产生 unknown 记录，
  closed 断言不通过（确认缺失只来自成功的空集合）；
- 会话返回 null：状态端口返回 null 按读取失败处理；
- 资源账目：浏览器会话 owned 登记 + 执行释放经 stopper 真实关闭；
  同会话重复观察幂等；unknown 记录不登记。
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.application.evaluation_scenario_assertions import (
    evaluate_scenario_assertions,
)


def _sse(chunks: list[dict], *, terminated: bool = True) -> bytes:
    lines = [f"data: {json.dumps(c, ensure_ascii=False)}" for c in chunks]
    if terminated:
        lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode()


def _chunk(text: str) -> dict:
    return {"choices": [{"index": 0, "delta": {"content": text},
                         "finish_reason": None}]}


def _finish(run_id="run-br", finish_reason="stop") -> dict:
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            "run_id": run_id}


def _browser_runner(*, browser_state_reader=None, tool_reader=None):
    from app.infrastructure.evaluation.scenario_adapters import (
        ScenarioDashboardSSERunner,
    )

    bodies = [
        _sse([_chunk("打开登录页"), _finish("run-nav")]),
        _sse([_chunk("已关闭"), _finish("run-close")]),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/chat/sessions":
            return httpx.Response(200)
        return httpx.Response(200, content=bodies.pop(0))

    return ScenarioDashboardSSERunner(
        base_url="http://agent.test",
        model="target-model",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        browser_state_reader=browser_state_reader,
        tool_evidence_reader=tool_reader
        or (lambda session_id: _empty_tool_reader()),
    )


async def _empty_tool_reader():
    return []


_BROWSER_REQUEST = {
    "session": {"namespace": "ns", "session_id": "sess-br"},
    "turns": [
        {"step_id": "turn-1", "message": "打开登录页"},
        {"step_id": "turn-2", "message": "结束浏览器"},
    ],
}


async def _run(runner):
    try:
        return await runner.run_target(
            type("T", (), {"kind": "agent_run"})(), _BROWSER_REQUEST
        )
    finally:
        await runner.aclose()


# ---------------------------------------------------------------------------
# 适配器：浏览器会话证据采集
# ---------------------------------------------------------------------------


async def test_two_turn_browser_evidence_collected():
    async def browser_reader(session_id):
        return [
            {"id": "bs-1", "status": "closed",
             "closed_at": "2026-11-14T00:00:00+00:00"},
        ]

    async def tool_reader(session_id):
        return [
            {"id": "c1", "run_id": "run-nav", "tool_name": "browser_navigate",
             "status": "success", "result": "ok"},
            {"id": "c2", "run_id": "run-close", "tool_name": "browser_close",
             "status": "success", "result": "ok"},
        ]

    result = await _run(_browser_runner(
        browser_state_reader=browser_reader, tool_reader=tool_reader,
    ))
    assert result["status"] == "succeeded"
    evidence = result["output"]["scenario_evidence"]
    # 两轮工具证据逐步保留（step 归属经 run_id）
    assert [t["tool_name"] for t in evidence["tool_calls"]] == [
        "browser_navigate", "browser_close",
    ]
    assert evidence["steps"][0]["run_id"] == "run-nav"
    assert evidence["steps"][1]["run_id"] == "run-close"
    # 浏览器会话证据：状态 + 关闭时间
    assert evidence["browser"] == [
        {"browser_session_id": "bs-1", "state": "closed",
         "closed_at": "2026-11-14T00:00:00+00:00"},
    ]


async def test_browser_read_failure_is_unknown_not_absence():
    async def browser_reader(session_id):
        raise RuntimeError("port down")

    result = await _run(_browser_runner(browser_state_reader=browser_reader))
    assert result["status"] == "succeeded"
    browser = result["output"]["scenario_evidence"]["browser"]
    assert len(browser) == 1
    assert browser[0]["state"] == "unknown"
    assert browser[0]["reason"]


async def test_browser_port_null_is_unknown_not_absence():
    async def browser_reader(session_id):
        return None

    result = await _run(_browser_runner(browser_state_reader=browser_reader))
    browser = result["output"]["scenario_evidence"]["browser"]
    assert browser[0]["state"] == "unknown"


async def test_browser_confirmed_absence_is_empty_list():
    async def browser_reader(session_id):
        return []

    result = await _run(_browser_runner(browser_state_reader=browser_reader))
    assert result["output"]["scenario_evidence"]["browser"] == []


async def test_browser_observer_receives_records_with_context():
    seen = []

    async def observer(session_id, records, context):
        seen.append((session_id, records, context))

    async def browser_reader(session_id):
        return [{"id": "bs-9", "status": "active", "closed_at": None}]

    runner = _browser_runner(
        browser_state_reader=browser_reader,
    )
    runner._browser_observer = observer
    try:
        result = await runner.run_target(
            type("T", (), {"kind": "agent_run"})(),
            {
                **_BROWSER_REQUEST,
                "scenario_context": {
                    "execution_id": "exec-1", "case_id": "browser-login-pod",
                },
            },
        )
    finally:
        await runner.aclose()
    assert result["status"] == "succeeded"
    assert seen and seen[0][0] == "sess-br"
    assert seen[0][1][0]["browser_session_id"] == "bs-9"
    assert seen[0][2] == {"execution_id": "exec-1", "case_id": "browser-login-pod"}


# ---------------------------------------------------------------------------
# 判定器：browser_state 断言纪律
# ---------------------------------------------------------------------------


def _browser_content(**overrides):
    base = {
        "assertions": [
            {"assertion_type": "tool_succeeded", "step_id": "turn-1",
             "params": {"tool": "browser_navigate"}},
            {"assertion_type": "tool_absent", "step_id": "turn-1",
             "params": {"tool": "web_fetch"}},
            {"assertion_type": "tool_succeeded", "step_id": "turn-2",
             "params": {"tool": "browser_close"}},
            {"assertion_type": "browser_state",
             "params": {"state": "closed"}},
        ],
        "scenario_evidence": {
            "status": "complete",
            "steps": [
                {"step_id": "turn-1", "run_id": "run-nav", "answer": "a"},
                {"step_id": "turn-2", "run_id": "run-close", "answer": "b"},
            ],
            "tool_calls": [
                {"run_id": "run-nav", "tool_name": "browser_navigate",
                 "status": "success", "result": "ok"},
                {"run_id": "run-close", "tool_name": "browser_close",
                 "status": "success", "result": "ok"},
            ],
            "browser": [
                {"browser_session_id": "bs-1", "state": "closed",
                 "closed_at": "2026-11-14T00:00:00+00:00"},
            ],
        },
    }
    base.update(overrides)
    return base


def test_browser_happy_path_passes():
    value = evaluate_scenario_assertions(_browser_content())
    assert value.status == "value"
    assert value.typed_value is True


def test_web_fetch_instead_of_navigation_fails():
    content = _browser_content()
    evidence = content["scenario_evidence"]
    evidence["tool_calls"] = [
        {"run_id": "run-nav", "tool_name": "web_fetch",
         "status": "success", "result": "<html>login</html>"},
        {"run_id": "run-close", "tool_name": "browser_close",
         "status": "success", "result": "ok"},
    ]
    value = evaluate_scenario_assertions(content)
    assert value.status == "value"
    assert value.typed_value is False


def test_close_tool_failure_is_not_success():
    content = _browser_content()
    content["scenario_evidence"]["tool_calls"][1]["status"] = "error"
    value = evaluate_scenario_assertions(content)
    assert value.status == "value"
    assert value.typed_value is False


def test_still_active_browser_fails_closed_assertion():
    content = _browser_content()
    content["scenario_evidence"]["browser"] = [
        {"browser_session_id": "bs-1", "state": "active", "closed_at": None},
    ]
    value = evaluate_scenario_assertions(content)
    assert value.status == "value"
    assert value.typed_value is False


def test_unknown_browser_record_is_not_absence():
    """读取失败（unknown 记录）不得当作 closed/absent 通过。"""
    content = _browser_content()
    content["scenario_evidence"]["browser"] = [
        {"state": "unknown", "reason": "port down"},
    ]
    value = evaluate_scenario_assertions(content)
    assert value.status == "value"
    assert value.typed_value is False


def test_confirmed_absence_normalizes_to_closed():
    """成功读取的空集合是确认不存在：closed 断言规范化通过。"""
    content = _browser_content()
    content["scenario_evidence"]["browser"] = []
    value = evaluate_scenario_assertions(content)
    assert value.status == "value"
    assert value.typed_value is True


def test_first_turn_failure_is_not_saved_by_last_turn():
    """turn-1 finish error：整体业务失败，末轮成功不挽救（多轮纪律）。"""
    content = _browser_content()
    # turn-1 无 browser_navigate 成功记录（首轮失败）
    content["scenario_evidence"]["tool_calls"] = [
        {"run_id": "run-close", "tool_name": "browser_close",
         "status": "success", "result": "ok"},
    ]
    value = evaluate_scenario_assertions(content)
    assert value.status == "value"
    assert value.typed_value is False


# ---------------------------------------------------------------------------
# 转换器：browser-login-pod 声明终态 browser_state 断言
# ---------------------------------------------------------------------------


def test_browser_case_declares_browser_state_closed():
    from app.application.evaluation_e2e_conversion import (
        SOURCE_DIR_RELPATH,
        convert_e2e_conversations,
    )

    result = convert_e2e_conversations(
        SOURCE_DIR_RELPATH, case_ids=("browser-login-pod",)
    )
    case = next(
        c for c in result.manifest["cases"]
        if c["case_id"] == "browser-login-pod"
    )
    assertions = case["scenario"]["assertions"]
    browser_states = [
        item for item in assertions
        if item["assertion_type"] == "browser_state"
    ]
    assert len(browser_states) == 1
    assert browser_states[0]["params"]["state"] == "closed"
    # 原样例的两轮工具断言保持不变
    tool_assertions = [
        (item["assertion_type"], item.get("step_id"), item["params"]["tool"])
        for item in assertions
        if item["assertion_type"] in ("tool_succeeded", "tool_absent")
    ]
    assert ("tool_succeeded", "turn-1", "browser_navigate") in tool_assertions
    assert ("tool_succeeded", "turn-2", "browser_close") in tool_assertions
    assert ("tool_absent", "turn-1", "web_fetch") in tool_assertions


# ---------------------------------------------------------------------------
# 组合根接线：main.py 浏览器证据与释放通道
# ---------------------------------------------------------------------------


def test_main_wires_browser_evidence_and_release():
    """T9 组合根契约（静态源码断言，同 T7 会话准备线程化测试风格）：

    - ledger stopper 为真实停止端口 _ScenarioActiveStopper（browser
      真实关闭），BrowserService 创建后回填 browser_closer；
    - browser_state_reader/browser_observer 闭包接线进
      build_execution_service（证据读取 + 账目观察）。"""
    from pathlib import Path

    source = Path("app/main.py").read_text(encoding="utf-8")
    # 真实停止端口替换占位并接线账本
    assert "stopper=_scenario_active_stopper" in source
    assert "_scenario_active_stopper = _ScenarioActiveStopper()" in source
    # BrowserService 创建后回填（组合根顺序：账本先于浏览器服务）
    assert "_scenario_active_stopper.bind_browser_closer(browser_service)" in source
    # stopper 实现包含 browser 真实关闭路径（get_session_by_id +
    # close_session），且先于关闭复核状态（已 closed 幂等）
    stopper_body = source.split("class _ScenarioActiveStopper")[1].split("class ")[0]
    assert "get_session_by_id" in stopper_body
    assert "close_session" in stopper_body
    # 证据读取与账目观察闭包接线
    assert "browser_state_reader=_scenario_browser_state_reader" in source
    assert "browser_observer=_scenario_browser_observer" in source
    assert "async def _scenario_browser_state_reader" in source
    assert "async def _scenario_browser_observer" in source
    # 观察闭包走账本 owned 回填登记（T9 资源纪律）
    observer_body = source.split("async def _scenario_browser_observer")[1].split("\n    async def ")[0]
    assert "observe_browser_session" in observer_body



async def test_browser_intents_registered_and_released(tmp_path):
    from app.application.evaluation_scenario_resources import (
        ScenarioResourceLedger,
    )
    from app.infrastructure.evaluation.scenario_store import (
        SQLiteScenarioResourceStore,
    )

    stopped = []

    class _Stopper:
        async def stop(self, resource_kind, external_id, process_identity=None):
            stopped.append((resource_kind, external_id))
            return "stopped"

    store = SQLiteScenarioResourceStore(str(tmp_path / "evaluations.db"))
    ledger = ScenarioResourceLedger(
        store,
        stopper=_Stopper(),
        file_deleter=type("_F", (), {"delete_file": staticmethod(lambda *a, **k: "deleted")})(),
        evidence_deleter=type("_E", (), {"delete_evidence": staticmethod(lambda *a, **k: None)})(),
        evidence_retention_seconds=0.0,
    )
    state = await ledger.observe_browser_session(
        execution_id="exec-br", case_id="browser-login-pod",
        browser_session_id="bs-1",
    )
    assert state == "owned"
    # 幂等：重复观察不新增账目
    again = await ledger.observe_browser_session(
        execution_id="exec-br", case_id="browser-login-pod",
        browser_session_id="bs-1",
    )
    assert again == "owned"
    rows = await store.list_for_execution("exec-br")
    assert len(rows) == 1
    assert rows[0]["resource_kind"] == "browser"
    assert rows[0]["external_id"] == "bs-1"
    report = await ledger.release_execution("exec-br")
    assert report["released"] == 1
    assert stopped == [("browser", "bs-1")]


async def test_browser_unknown_record_is_not_registered(tmp_path):
    from app.application.evaluation_scenario_resources import (
        ScenarioResourceLedger,
    )
    from app.infrastructure.evaluation.scenario_store import (
        SQLiteScenarioResourceStore,
    )

    store = SQLiteScenarioResourceStore(str(tmp_path / "evaluations.db"))
    ledger = ScenarioResourceLedger(
        store,
        stopper=type("_S", (), {"stop": staticmethod(lambda *a, **k: "stopped")})(),
        file_deleter=type("_F", (), {"delete_file": staticmethod(lambda *a, **k: "deleted")})(),
        evidence_deleter=type("_E", (), {"delete_evidence": staticmethod(lambda *a, **k: None)})(),
        evidence_retention_seconds=0.0,
    )
    # unknown 记录无 browser_session_id：不登记（无归属可释放）
    state = await ledger.observe_browser_session(
        execution_id="exec-x", case_id="c",
        browser_session_id=None,
    )
    assert state == "skipped"
    assert not await store.list_for_execution("exec-x")


async def test_release_adopts_expired_lease_before_stopping(tmp_path):
    """T9 S4 实测缺口：执行超过租约期（或跨进程重建后）settle 时，
    owned 记录租约已过期——release 必须先收养再释放，而不是抛
    lease lost 让执行单整体 failed。"""
    from app.application.evaluation_scenario_resources import (
        ScenarioResourceLedger,
    )
    from app.infrastructure.evaluation.scenario_store import (
        SQLiteScenarioResourceStore,
    )

    stopped = []
    clock_t = [1000.0]

    class _Stopper:
        async def stop(self, resource_kind, external_id, process_identity=None):
            stopped.append((resource_kind, external_id))
            return "stopped"

    store = SQLiteScenarioResourceStore(str(tmp_path / "evaluations.db"))
    ledger = ScenarioResourceLedger(
        store,
        stopper=_Stopper(),
        file_deleter=type("_F", (), {"delete_file": staticmethod(lambda *a, **k: "deleted")})(),
        evidence_deleter=type("_E", (), {"delete_evidence": staticmethod(lambda *a, **k: None)})(),
        evidence_retention_seconds=0.0,
        clock=lambda: clock_t[0],
    )
    await ledger.observe_browser_session(
        execution_id="exec-lease", case_id="browser-login-pod",
        browser_session_id="bs-lease",
    )
    # 模拟执行/重建耗时超过默认租约（600s）
    clock_t[0] += 3600.0
    report = await ledger.release_execution("exec-lease")
    assert report["released"] == 1
    assert stopped == [("browser", "bs-lease")]
    rows = await store.list_for_execution("exec-lease")
    assert rows[0]["state"] == "released"


async def test_register_retention_adopts_expired_lease(tmp_path):
    """settle 先登记保留：register_retention 同样要收养过期租约，
    否则长执行/跨重建后 settle 在登记保留一步即抛 lease lost。"""
    from app.application.evaluation_scenario_resources import (
        ScenarioResourceLedger,
    )
    from app.infrastructure.evaluation.scenario_store import (
        SQLiteScenarioResourceStore,
    )

    clock_t = [1000.0]
    store = SQLiteScenarioResourceStore(str(tmp_path / "evaluations.db"))
    ledger = ScenarioResourceLedger(
        store,
        stopper=type("_S", (), {"stop": staticmethod(lambda *a, **k: "stopped")})(),
        file_deleter=type("_F", (), {"delete_file": staticmethod(lambda *a, **k: "deleted")})(),
        evidence_deleter=type("_E", (), {"delete_evidence": staticmethod(lambda *a, **k: None)})(),
        evidence_retention_seconds=604800.0,
        clock=lambda: clock_t[0],
    )
    from app.domain.evaluation.scenario_resources import ResourceIntent

    await ledger.register_intent(
        ResourceIntent(
            business_key="exec-rt:c:memory_binding:s.a",
            execution_id="exec-rt", case_id="c",
            resource_kind="memory_binding",
            release_rule="release_active_resources",
            retention_rule="retain_evidence_default",
            external_id="s|a", ref=None, expected_sha256=None,
            process_identity=None,
        )
    )
    await ledger.confirm_owned("exec-rt:c:memory_binding:s.a", external_id="s|a")
    clock_t[0] += 3600.0
    count = await ledger.register_retention("exec-rt")
    assert count == 1
    rows = await store.list_for_execution("exec-rt")
    assert rows[0]["state"] == "retained"

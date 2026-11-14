"""T10：Task/制品/文件接入测试（plan T10 S1）。

覆盖反例（plan T10 S1 逐项）：
- create_task 响应断网：task_create 步骤调用异常 -> transport_error +
  side_effects_unobserved，intent 标记 unknown（不重跑目标，等待幂等
  键回查收敛）；
- worker 未完成/600 秒超时：任务在 deadline 内未到终态 -> 证据
  incomplete（读取失败不等于空集合），task_terminal 断言不得通过；
- 重复任务：固定幂等键，unknown 恢复回查命中同一 task，不创建第二个
  任务（禁止递归、单一层级）；
- 分页漏制品：artifact 列表 cursor 分页全量累加，next_cursor 不前进
  报错，数量不足 -> artifact_set 断言失败；
- basename 相同但 storage_ref 错误：受控内部端口核对实际 storage_ref
  （workspace: 前缀 + 基名匹配），错误如实判负（不复制 E2E 的仅
  basename 兜底）；
- 文件穿越/symlink/hash 变化：workspace 文件读取端口拒绝穿越/symlink/
  非普通文件，失败不当通过；释放时 hash 变更 -> cleanup_failed；
- 清理时 worker 仍写入：删除端口删除前复核 hash，变更拒删。
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

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


def _finish(run_id="run-t", finish_reason="stop") -> dict:
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            "run_id": run_id}


class _Clock:
    """可推进的时钟（sleep 推进虚拟时间；轮询测试不真等）。"""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _task_runner(
    *,
    bodies: list[bytes] | None = None,
    task_creator=None,
    task_reader=None,
    task_runs_reader=None,
    artifact_lister=None,
    workspace_file_reader=None,
    task_intent_before=None,
    task_intent_confirmed=None,
    task_intent_unknown=None,
    tool_reader=None,
    clock=None,
    deadline_seconds: float = 600.0,
):
    from app.infrastructure.evaluation.scenario_adapters import (
        ScenarioDashboardSSERunner,
    )

    remaining = list(bodies or [])

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/chat/sessions":
            return httpx.Response(200)
        return httpx.Response(200, content=remaining.pop(0))

    return ScenarioDashboardSSERunner(
        base_url="http://agent.test",
        model="target-model",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        tool_evidence_reader=tool_reader or (lambda session_id: _async([])),
        task_creator=task_creator,
        task_reader=task_reader,
        task_runs_reader=task_runs_reader,
        artifact_lister=artifact_lister,
        workspace_file_reader=workspace_file_reader,
        task_intent_before=task_intent_before,
        task_intent_confirmed=task_intent_confirmed,
        task_intent_unknown=task_intent_unknown,
        clock=clock.time if clock else None,
        sleep=clock.sleep if clock else None,
        task_deadline_seconds=deadline_seconds,
    )


async def _async(value):
    return value


_TASK_REQUEST = {
    "session": {"namespace": "ns", "session_id": "sess-task"},
    "turns": [
        {
            "step_id": "turn-1",
            "action": "task_create",
            "task": {
                "title": "任务：产出制品",
                "body": "在工作区创建两个文件并提交制品",
                "goal_mode": True,
                "priority": 2,
            },
        },
    ],
    "scenario_context": {"execution_id": "exec-t", "case_id": "task-artifact-cmd"},
    "scenario_files": ["task-output-a.txt", "task-output-b.md"],
}


def _created_task(task_id="t_abc123", status="queued"):
    return {"task_id": task_id, "title": "任务：产出制品", "status": status}


async def _run(runner, request=None):
    try:
        return await runner.run_target(
            type("T", (), {"kind": "agent_run"})(),
            request or _TASK_REQUEST,
        )
    finally:
        await runner.aclose()


# ---------------------------------------------------------------------------
# task_create 步骤执行
# ---------------------------------------------------------------------------


async def test_task_create_step_creates_and_collects_evidence():
    """task_api 步骤：副作用前登记 intent -> create -> 回填 owned ->
    轮询终态 -> 收集任务/制品/文件证据。"""
    hooks = []

    def before(payload, context):
        hooks.append(("before", dict(payload)))
        return "exec-t:task-artifact-cmd:task:turn-1"

    def confirmed(business_key, task_id):
        hooks.append(("confirmed", business_key, task_id))

    def unknown(business_key, reason):
        hooks.append(("unknown", business_key, reason))

    async def creator(payload, session_id, context):
        assert payload["goal_mode"] is True
        assert payload["priority"] == 2
        return _created_task()

    async def reader(task_id):
        return {"task_id": task_id, "status": "succeeded", "title": "任务"}

    async def runs_reader(task_id):
        # 真实 TaskRun 无 artifacts 字段（finish_run 不持久化提交制品）；
        # 原始 storage_ref 由注册侧 origin_ref 标签承载
        return [{"run_id": "wr-1", "status": "succeeded", "tools": []}]

    async def lister(source_context_ref):
        return [{"id": "art-1", "name": "report.md", "kind": "markdown",
                 "origin_ref": "artifact://sha256:abc", "content": "# 实际调研报告"}]

    async def file_reader(rel_path):
        return None  # task_api 例未声明文件场景由另一测试覆盖

    clock = _Clock()
    result = await _run(_task_runner(
        task_creator=creator, task_reader=reader, task_runs_reader=runs_reader,
        artifact_lister=lister,
        workspace_file_reader=file_reader,
        task_intent_before=before, task_intent_confirmed=confirmed,
        task_intent_unknown=unknown, clock=clock,
    ))
    assert result["status"] == "succeeded"
    evidence = result["output"]["scenario_evidence"]
    # intent 先于副作用（before 在 creator 前），成功后回填 owned
    assert hooks[0][0] == "before"
    assert hooks[1][:2] == ("confirmed", "exec-t:task-artifact-cmd:task:turn-1")
    assert hooks[1][2] == "t_abc123"
    assert not any(h[0] == "unknown" for h in hooks)
    # 任务证据：终态 + worker runs
    assert evidence["tasks"][0]["task_id"] == "t_abc123"
    assert evidence["steps"][0]["task_id"] == "t_abc123"
    assert evidence["tasks"][0]["status"] == "succeeded"
    assert evidence["tasks"][0]["runs"] == [
        {"run_id": "wr-1", "status": "succeeded", "tools": []}
    ]
    # 制品证据：注册列表（id/name/kind）+ 注册侧固化 origin_ref 标签
    assert evidence["artifacts"] == [
        {"id": "art-1", "name": "report.md", "kind": "markdown",
         "storage_ref": "artifact://sha256:abc", "content": "# 实际调研报告"}
    ]


async def test_task_create_transport_failure_marks_unknown():
    """create_task 响应断网：transport_error + side_effects_unobserved，
    intent 标记 unknown（等待幂等键回查，不重跑目标）。"""
    unknowns = []

    def before(payload, context):
        return "exec-t:task-artifact-cmd:task:turn-1"

    def unknown(business_key, reason):
        unknowns.append((business_key, reason))

    async def creator(payload, session_id, context):
        raise ConnectionError("network down")

    result = await _run(_task_runner(
        task_creator=creator, task_intent_before=before,
        task_intent_unknown=unknown,
    ))
    assert result["status"] == "transport_error"
    assert result.get("side_effects_unobserved") is True
    assert unknowns and unknowns[0][0] == "exec-t:task-artifact-cmd:task:turn-1"
    assert unknowns[0][1]


async def test_task_deadline_exceeded_is_incomplete_not_absence():
    """worker 未完成（600s 截止）：任务证据 incomplete，
    task_terminal 断言不通过——超时不等于任务失败，更不等于通过。"""
    async def creator(payload, session_id, context):
        return _created_task()

    async def reader(task_id):
        return {"task_id": task_id, "status": "running", "title": "任务"}

    clock = _Clock()
    result = await _run(_task_runner(
        task_creator=creator, task_reader=reader, clock=clock,
        deadline_seconds=600.0,
    ))
    assert result["status"] == "succeeded"
    evidence = result["output"]["scenario_evidence"]
    assert evidence["status"] == "incomplete"
    assert "deadline" in evidence.get("reason", "")
    # 断言判定：incomplete 证据 -> skipped，不判通过
    content = {
        "assertions": [
            {"assertion_type": "task_terminal",
             "params": {"status": "succeeded", "within_seconds": 600}},
        ],
        "scenario_evidence": evidence,
    }
    value = evaluate_scenario_assertions(content)
    assert value.status == "skipped"
    assert value.typed_value is None


async def test_task_failed_terminal_is_collected_honestly():
    """worker 终态失败：证据如实记录 failed，task_terminal 断言判负。"""
    async def creator(payload, session_id, context):
        return _created_task(status="failed")

    async def reader(task_id):
        return {"task_id": task_id, "status": "failed", "title": "任务"}

    result = await _run(_task_runner(task_creator=creator, task_reader=reader))
    evidence = result["output"]["scenario_evidence"]
    assert evidence["status"] == "complete"
    assert evidence["tasks"][0]["status"] == "failed"
    content = {
        "assertions": [
            {"assertion_type": "task_terminal",
             "params": {"status": "succeeded", "within_seconds": 600}},
        ],
        "scenario_evidence": evidence,
    }
    value = evaluate_scenario_assertions(content)
    assert value.typed_value is False


async def test_chat_task_binding_from_create_tool_call():
    """chat 题（task-artifact-chat）：agent 自行调用 create_task，
    task_id 从正式端口 create_task 成功记录绑定（端口实测形态：content
    为序列化 JSON 字符串，任务在 task.id 嵌套），随证据轮询终态。"""
    bodies = [
        _sse([_chunk("已创建任务"), _finish("run-chat")]),
    ]

    async def tool_reader(session_id):
        return [
            {"id": "c1", "run_id": "run-chat", "tool_name": "create_task",
             "status": "success",
             "result": json.dumps(
                 {"tool_call_id": "c1", "name": "create_task",
                  "status": "success",
                  "content": json.dumps(
                      {"success": True,
                       "task": {"id": "t_agent1", "status": "queued"}},
                      ensure_ascii=False),
                  "duration_ms": 10},
                 ensure_ascii=False)},
        ]

    async def reader(task_id):
        assert task_id == "t_agent1"
        return {"task_id": task_id, "status": "succeeded", "title": "调研"}

    async def runs_reader(task_id):
        return [{"run_id": "wr-1", "status": "succeeded",
                 "tools": [{"tool_name": "web_fetch", "status": "success"}]}]

    async def lister(source_context_ref):
        assert source_context_ref == "t_agent1"
        return [{"id": "art-9", "name": "report.md", "kind": "markdown",
                 "origin_ref": "artifact://sha256:xyz"}]

    request = {
        "session": {"namespace": "ns", "session_id": "sess-chat"},
        "turns": [{"step_id": "turn-1",
                   "message": "用 task 调研博客并产出报告"}],
        "scenario_context": {"execution_id": "exec-c",
                             "case_id": "task-artifact-chat"},
    }
    result = await _run(_task_runner(
        bodies=bodies, tool_reader=tool_reader, task_reader=reader,
        task_runs_reader=runs_reader, artifact_lister=lister,
    ), request)
    assert result["status"] == "succeeded"
    evidence = result["output"]["scenario_evidence"]
    assert evidence["tasks"][0]["task_id"] == "t_agent1"
    # worker runs 内 web_fetch 逐步保留（递归收集任务关系内证据）
    assert evidence["tasks"][0]["runs"][0]["tools"][0]["tool_name"] == "web_fetch"
    assert evidence["artifacts"][0]["storage_ref"] == "artifact://sha256:xyz"


# ---------------------------------------------------------------------------
# 制品与文件证据纪律
# ---------------------------------------------------------------------------


async def test_artifact_pagination_accumulates_and_detects_stuck_cursor():
    """分页漏制品：cursor 分页全量累加；next_cursor 不前进报错
    （证据 incomplete），不得静默截断。"""
    async def creator(payload, session_id, context):
        return _created_task()

    async def reader(task_id):
        return {"task_id": task_id, "status": "succeeded", "title": "t"}

    pages = iter([
        {"items": [{"id": "a1", "name": "f1.txt", "kind": "text"}],
         "next_cursor": {"page": 2}},
        {"items": [{"id": "a2", "name": "f2.txt", "kind": "text"}],
         "next_cursor": None},
    ])
    calls = []

    async def lister(source_context_ref, *, cursor=None):
        calls.append(cursor)
        return next(pages)

    result = await _run(_task_runner(
        task_creator=creator, task_reader=reader, artifact_lister=lister,
    ))
    evidence = result["output"]["scenario_evidence"]
    assert [a["id"] for a in evidence["artifacts"]] == ["a1", "a2"]
    assert calls == [None, {"page": 2}]

    async def stuck_lister(source_context_ref, *, cursor=None):
        # next_cursor 永不前进（与上次相同），适配器必须报错而非无限翻页
        return {"items": [{"id": "a1", "name": "f1.txt", "kind": "text"}],
                "next_cursor": {"page": 2}}

    result = await _run(_task_runner(
        task_creator=creator, task_reader=reader,
        artifact_lister=stuck_lister,
    ))
    evidence = result["output"]["scenario_evidence"]
    assert evidence["status"] == "incomplete"
    assert "cursor" in evidence.get("reason", "")


async def test_task_deadline_anchors_creation_before_evidence_collection():
    from datetime import datetime, timezone
    clock = _Clock()
    clock.now = 1300
    async def reader(task_id):
        return {"task_id": task_id, "status": "running",
                "created_at": datetime.fromtimestamp(1_800_001000, timezone.utc).isoformat()}
    runner = _task_runner(task_reader=reader, clock=clock)
    runner._wall_clock = lambda: 1_800_000_000 + clock.time()
    try:
        assert await runner._poll_task_terminal("task-1") is None
        assert clock.now == 1600  # created at 1000, not collection at 1300 + 600
    finally:
        await runner.aclose()


async def test_worker_run_port_failure_marks_task_evidence_incomplete():
    async def creator(payload, session_id, context): return _created_task()
    async def reader(task_id): return {"task_id": task_id, "status": "succeeded"}
    async def broken_runs(task_id): raise ConnectionError("worker runs unavailable")
    result = await _run(_task_runner(task_creator=creator, task_reader=reader, task_runs_reader=broken_runs))
    evidence = result["output"]["scenario_evidence"]
    assert evidence["status"] == "incomplete"
    assert "task runs read failed" in evidence["reason"]


async def test_workspace_file_content_and_storage_ref_verification():
    """task_api 文件题：工作区文件实际内容 + 制品实际 storage_ref
    核对（workspace: 前缀 + 基名匹配）；basename 相同但 storage_ref
    错误 -> 判定层如实判负。"""

    async def creator(payload, session_id, context):
        return _created_task()

    async def reader(task_id):
        return {"task_id": task_id, "status": "succeeded", "title": "t"}

    async def runs_reader(task_id):
        return [{"run_id": "wr-1", "status": "succeeded", "tools": []}]

    async def lister(source_context_ref):
        return [
            {"id": "a1", "name": "task-output-a.txt", "kind": "text",
             "origin_ref": "workspace://scratch/task-output-a.txt"},
            {"id": "a2", "name": "task-output-b.md", "kind": "markdown",
             "origin_ref": "workspace://scratch/other-path.md"},
        ]

    async def file_reader(rel_path):
        contents = {
            "task-output-a.txt": "artifact a",
            "task-output-b.md": "# artifact b",
        }
        return {"filename": rel_path, "content": contents[rel_path],
                "sha256": "deadbeef"}

    request = dict(_TASK_REQUEST)
    result = await _run(_task_runner(
        task_creator=creator, task_reader=reader, task_runs_reader=runs_reader,
        artifact_lister=lister,
        workspace_file_reader=file_reader,
    ))
    evidence = result["output"]["scenario_evidence"]
    assert evidence["status"] == "complete"
    assert evidence["files"] == [
        {"filename": "task-output-a.txt", "content": "artifact a",
         "sha256": "deadbeef"},
        {"filename": "task-output-b.md", "content": "# artifact b",
         "sha256": "deadbeef"},
    ]
    # storage_ref 来自注册侧 origin_ref 标签（原始 ref 权威源），判定层核对基名
    assert evidence["artifacts"][1]["storage_ref"] == (
        "workspace://scratch/other-path.md"
    )

    # 判定：file_content 通过；artifact_set 数量通过，但 storage_ref
    # 基名核对（scenario 断言声明 storage_ref_prefix + workspace_files）
    content = {
        "assertions": [
            {"assertion_type": "task_terminal",
             "params": {"status": "succeeded", "within_seconds": 600}},
            {"assertion_type": "file_content",
             "params": {"filename": "task-output-a.txt",
                        "contains": "artifact a"}},
            {"assertion_type": "artifact_set",
             "params": {"min_count": 2,
                        "storage_ref_prefix": "workspace:",
                        "workspace_files": ["task-output-a.txt",
                                            "task-output-b.md"]}},
        ],
        "scenario_evidence": evidence,
    }
    value = evaluate_scenario_assertions(content)
    assert value.status == "value"
    assert value.typed_value is False  # a2 的 storage_ref 基名不符


def test_artifact_set_storage_ref_prefix_mismatch_fails():
    """storage_ref 前缀不符（非 workspace: 提交）如实判负。"""
    evidence = {
        "status": "complete",
        "tasks": [{"task_id": "t1", "status": "succeeded"}],
        "artifacts": [
            {"id": "a1", "name": "task-output-a.txt",
             "storage_ref": "artifact://sha256:zzz"},
        ],
        "files": [],
    }
    content = {
        "assertions": [
            {"assertion_type": "artifact_set",
             "params": {"min_count": 1,
                        "storage_ref_prefix": "workspace:",
                        "workspace_files": ["task-output-a.txt"]}},
        ],
        "scenario_evidence": evidence,
    }
    value = evaluate_scenario_assertions(content)
    assert value.typed_value is False


def test_workspace_file_read_failure_is_not_pass():
    """文件读取失败（端口异常/拒绝）不当通过：file_content 断言在
    证据缺失时按失败处理。"""
    evidence = {
        "status": "complete",
        "tasks": [{"task_id": "t1", "status": "succeeded"}],
        "artifacts": [],
        "files": [
            {"filename": "task-output-a.txt", "content": None,
             "sha256": None, "error": "workspace symlink forbidden"},
        ],
    }
    content = {
        "assertions": [
            {"assertion_type": "file_content",
             "params": {"filename": "task-output-a.txt",
                        "contains": "artifact a"}},
        ],
        "scenario_evidence": evidence,
    }
    value = evaluate_scenario_assertions(content)
    assert value.status == "value"
    assert value.typed_value is False


# ---------------------------------------------------------------------------
# 转换器：task_api 题声明 storage_ref 核对目标
# ---------------------------------------------------------------------------


def test_converter_declares_storage_ref_verification_targets():
    """task-artifact-cmd 的 artifact_registered 带 storage_ref_prefix：
    断言须携带 storage_ref_prefix 与 workspace_files 基名核对目标
    （受控内部端口核对实际 storage_ref，不做仅 basename 兜底）。"""
    from app.application.evaluation_e2e_conversion import (
        SOURCE_DIR_RELPATH,
        convert_e2e_conversations,
    )

    result = convert_e2e_conversations(
        SOURCE_DIR_RELPATH, case_ids=("task-artifact-cmd",)
    )
    case = next(
        c for c in result.manifest["cases"]
        if c["case_id"] == "task-artifact-cmd"
    )
    artifact_assertions = [
        a for a in case["scenario"]["assertions"]
        if a["assertion_type"] == "artifact_set"
    ]
    assert len(artifact_assertions) == 1
    params = artifact_assertions[0]["params"]
    assert params["min_count"] == 2
    assert params["storage_ref_prefix"] == "workspace:"
    assert sorted(params["workspace_files"]) == [
        "task-output-a.txt", "task-output-b.md",
    ]
    # 文件内容断言保持（filename/contains）
    file_assertions = {
        a["params"]["filename"]: a["params"]["contains"]
        for a in case["scenario"]["assertions"]
        if a["assertion_type"] == "file_content"
    }
    assert file_assertions == {
        "task-output-a.txt": "artifact a",
        "task-output-b.md": "# artifact b",
    }


# ---------------------------------------------------------------------------
# 工作区文件读取端口：路径纪律
# ---------------------------------------------------------------------------


def test_workspace_file_reader_rejects_traversal_symlink_and_non_regular(tmp_path):
    from app.infrastructure.evaluation.workspace_file_reader import (
        WorkspaceFileError,
        read_workspace_file,
    )

    root = tmp_path / "scratch"
    (root / "sub").mkdir(parents=True)
    (root / "ok.txt").write_text("artifact a", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    # 正常读取：内容 + sha256
    record = read_workspace_file(root, "ok.txt")
    assert record["filename"] == "ok.txt"
    assert record["content"] == "artifact a"
    assert len(record["sha256"]) == 64

    # 仅真正不存在 -> None
    assert read_workspace_file(root, "missing.txt") is None

    with pytest.raises(WorkspaceFileError):
        read_workspace_file(root, "../outside.txt")  # 穿越
    with pytest.raises(WorkspaceFileError):
        read_workspace_file(root, "/etc/hosts")  # 绝对路径
    link = root / "sub" / "link.txt"
    link.symlink_to(outside)
    with pytest.raises(WorkspaceFileError):
        read_workspace_file(root, "sub/link.txt")  # symlink
    # 非普通文件（目录）
    with pytest.raises(WorkspaceFileError):
        read_workspace_file(root, "sub")


# ---------------------------------------------------------------------------
# 组合根接线：main.py task/制品/文件通道
# ---------------------------------------------------------------------------


def test_main_wires_task_ports_and_release_paths():
    """T10 组合根契约（静态源码断言，同 T7/T9 风格）：

    - execution_runtime 把 task/制品/文件端口贯通到 dashboard SSE 适配器；
    - main.py 组合根注入真实端口（TaskService.create_task 固定幂等键 +
    intent 纪律、TaskRun.artifacts 实际 storage_ref、工作区安全读取）；
    - stopper task 分支真实取消并回读终态；工作区文件删除端口带
      hash 复核（清理时 worker 仍写入 -> 拒删）；
    - task_api profile 准入。"""
    from pathlib import Path

    runtime = Path("app/infrastructure/evaluation/execution_runtime.py").read_text(
        encoding="utf-8"
    )
    for port in (
        "task_creator", "task_reader", "task_runs_reader", "artifact_lister",
        "workspace_file_reader", "task_intent_before", "task_intent_confirmed",
        "task_intent_unknown",
    ):
        assert f"{port}=" in runtime, port
    assert "task_creator=task_creator" in runtime
    assert "workspace_file_reader=workspace_file_reader" in runtime

    source = Path("app/main.py").read_text(encoding="utf-8")
    # 端口注入 build_execution_service
    for port in (
        "task_creator=_scenario_task_creator",
        "task_reader=_scenario_task_reader",
        "task_runs_reader=_scenario_task_runs_reader",
        "artifact_lister=_scenario_artifact_lister",
        "workspace_file_reader=_scenario_workspace_file_reader",
        "task_intent_before=_scenario_task_intent_before",
        "task_intent_confirmed=_scenario_task_intent_confirmed",
        "task_intent_unknown=_scenario_task_intent_unknown",
    ):
        assert port in source, port
    # 固定幂等键（重复任务不重建：重试经既有幂等键回查）
    assert "idempotency_key=" in source
    # stopper task 分支：真实取消 + 回读终态
    stopper_body = source.split("class _ScenarioActiveStopper")[1].split("class ")[0]
    assert "cancel_task" in stopper_body
    # 工作区文件删除端口（hash 复核）替换 noop
    assert "_ScenarioWorkspaceFileDeleter" in source
    assert "file_deleter=_scenario_workspace_file_deleter" in source
    assert "_ScenarioNoopFileDeleter" not in source
    # task_api 准入（task 端口接线时；判定在 execution_runtime）
    assert '"task_api"' in runtime


def test_task_api_profile_routes_to_scenario_runner():
    """task_api profile 必须路由到场景适配器（task_create 步骤不经
    chat 通道）；落到 chat runner 时任务永不创建（S4 实测缺口）。"""
    from pathlib import Path

    runtime = Path(
        "app/infrastructure/evaluation/execution_runtime.py"
    ).read_text(encoding="utf-8")
    assert '"task_api"' in runtime
    # 路由分支与 dashboard 共用场景适配器（chat 轮次为零时不发 SSE）
    assert "task_api" in runtime.split("class ProfileRoutingTargetRunner")[1].split("class ")[0] \
        or "task_api" in runtime.split("class ProfileRoutingTargetRunner")[1]


def test_main_stopper_task_release_reads_back_terminal():
    """stopper task 释放语义：回读非终态先取消，取消后回读终态才算
    释放；取消失败/未终态抛出保持 owned 可恢复。"""
    import asyncio as _asyncio

    from app.main import _ScenarioActiveStopper

    calls = []

    class _TaskCloser:
        async def cancel_task(self, task_id):
            calls.append(("cancel", task_id))
            return {"task_id": task_id}

        async def get_task(self, task_id):
            calls.append(("read", task_id))
            # 取消前活跃（running），取消后回读到终态（cancelled）
            value = "cancelled" if ("cancel", task_id) in calls else "running"
            return type(
                "T", (),
                {"id": task_id, "title": "t", "status": type(
                    "S", (), {"value": value})(),
                 "created_at": None, "updated_at": None},
            )()

    stopper = _ScenarioActiveStopper()
    stopper.bind_task_closer(_TaskCloser())
    result = _asyncio.run(stopper.stop("task", "t_abc"))
    assert result == "stopped"
    assert ("cancel", "t_abc") in calls
    assert ("read", "t_abc") in calls



# ---------------------------------------------------------------------------
# 资源账目：task/workspace_file intent 与释放
# ---------------------------------------------------------------------------


async def test_task_and_workspace_file_intents_released(tmp_path):
    """task intent（副作用前登记）+ workspace_file intent（读证据后
    按 hash 登记）；释放经 stopper/file_deleter 端口真实执行；
    清理时 hash 变更（worker 仍写入）-> cleanup_failed。"""
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

    deleted = {"hash": ["a" * 64]}

    class _Deleter:
        async def delete_file(self, ref, expected_sha256):
            if expected_sha256 != deleted["hash"][0]:
                return "hash_changed"
            return "deleted"

    store = SQLiteScenarioResourceStore(str(tmp_path / "evaluations.db"))
    ledger = ScenarioResourceLedger(
        store, stopper=_Stopper(), file_deleter=_Deleter(),
        evidence_deleter=type("_E", (), {
            "delete_evidence": staticmethod(lambda *a, **k: None)})(),
        evidence_retention_seconds=0.0,
    )
    # task intent：副作用前登记，成功后回填
    await ledger.register_intent(
        _task_intent("exec-r:task-artifact-cmd:task:turn-1", "exec-r")
    )
    await ledger.confirm_owned(
        "exec-r:task-artifact-cmd:task:turn-1", external_id="t_abc123",
    )
    # workspace_file intent：证据读取后按实际 hash 登记
    file_key = "exec-r:task-artifact-cmd:workspace_file:task-output-a.txt"
    await ledger.register_intent(
        _file_intent(file_key, "exec-r", "task-output-a.txt",
                     expected_sha256="a" * 64)
    )
    await ledger.confirm_owned(
        file_key, external_id="task-output-a.txt", ref="task-output-a.txt",
    )
    report = await ledger.release_execution("exec-r")
    assert report["released"] == 2
    assert ("task", "t_abc123") in stopped


def _task_intent(business_key, execution_id):
    from app.domain.evaluation.scenario_resources import ResourceIntent

    return ResourceIntent(
        business_key=business_key, execution_id=execution_id,
        case_id="task-artifact-cmd", resource_kind="task",
        release_rule="release_active_resources",
        retention_rule="retain_evidence_default",
        external_id=None, ref=None, expected_sha256=None,
        process_identity=None,
    )


def _file_intent(business_key, execution_id, filename, *, expected_sha256):
    from app.domain.evaluation.scenario_resources import ResourceIntent

    return ResourceIntent(
        business_key=business_key, execution_id=execution_id,
        case_id="task-artifact-cmd", resource_kind="workspace_file",
        release_rule="release_active_resources",
        retention_rule="retain_evidence_default",
        external_id=filename, ref=filename,
        expected_sha256=expected_sha256, process_identity=None,
    )


async def test_workspace_file_hash_changed_at_release_is_cleanup_failed(tmp_path):
    """清理时 worker 仍写入：删除端口复核 hash 变更 -> 拒删，
    账本 cleanup_failed（不伪装全部删除）。"""
    from app.application.evaluation_scenario_resources import (
        ScenarioResourceLedger,
    )
    from app.infrastructure.evaluation.scenario_store import (
        SQLiteScenarioResourceStore,
    )

    class _Stopper:
        async def stop(self, resource_kind, external_id, process_identity=None):
            return "stopped"

    class _Deleter:
        async def delete_file(self, ref, expected_sha256):
            return "hash_changed"  # worker 在证据读取后继续写入

    store = SQLiteScenarioResourceStore(str(tmp_path / "evaluations.db"))
    ledger = ScenarioResourceLedger(
        store, stopper=_Stopper(), file_deleter=_Deleter(),
        evidence_deleter=type("_E", (), {
            "delete_evidence": staticmethod(lambda *a, **k: None)})(),
        evidence_retention_seconds=0.0,
    )
    file_key = "exec-w:task-artifact-cmd:workspace_file:task-output-a.txt"
    await ledger.register_intent(
        _file_intent(file_key, "exec-w", "task-output-a.txt",
                     expected_sha256="a" * 64)
    )
    await ledger.confirm_owned(file_key, external_id="task-output-a.txt",
                               ref="task-output-a.txt")
    report = await ledger.release_execution("exec-w")
    assert report["cleanup_failed"] == 1
    rows = await store.list_for_execution("exec-w")
    assert rows[0]["state"] == "cleanup_failed"


async def test_observe_owned_encodes_non_contract_external_id(tmp_path):
    """T10 S4 实测缺口：真实制品 ID 是 base64url（含大写字母），而
    business_key 标识符契约只允许小写——observe_owned 对不满足契约的
    external_id 必须做确定性编码后登记，而不是让账目登记抛
    ScenarioResourceError 使整个执行单失败。external_id 本体保持原样
    入账（释放时按真实 ID 停止/删除）。"""
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
    artifact_id = "NEPMVEzrNnOshxz4dGZOfA"  # sessions.db 真实形状
    state = await ledger.observe_owned(
        execution_id="exec-7ad82960f42f4e2d949a3a2412ad3048",
        case_id="task-artifact-cmd",
        resource_kind="artifact",
        external_id=artifact_id,
        ref="task-output-a.txt",
    )
    assert state == "owned"
    rows = await store.list_for_execution(
        "exec-7ad82960f42f4e2d949a3a2412ad3048"
    )
    assert len(rows) == 1
    assert rows[0]["external_id"] == artifact_id
    assert rows[0]["resource_kind"] == "artifact"
    # business_key 本身必须满足标识符契约（可幂等回查）
    assert re.match(r"^[a-z0-9][a-z0-9_:.\-]{0,199}$", rows[0]["business_key"])
    # 幂等：同 external_id 重复观察返回既有状态，不新增账目
    again = await ledger.observe_owned(
        execution_id="exec-7ad82960f42f4e2d949a3a2412ad3048",
        case_id="task-artifact-cmd",
        resource_kind="artifact",
        external_id=artifact_id,
        ref="task-output-a.txt",
    )
    assert again == "owned"
    assert len(await store.list_for_execution(
        "exec-7ad82960f42f4e2d949a3a2412ad3048"
    )) == 1


def test_main_stopper_task_already_terminal_is_idempotent_release():
    """T10 S4 实测缺口：固定命令题在释放前已是 succeeded——cancel_task
    对终态任务抛 TaskStateError，无条件先取消会让 release_execution 抛
    RuntimeError 使执行单整体 failed。回读终态在先：已终态幂等视为
    已释放（与 browser"已 closed 不做二次关闭"同纪律），仅非终态才取消。"""
    import asyncio as _asyncio

    from app.main import _ScenarioActiveStopper

    calls = []

    class _TaskCloser:
        async def cancel_task(self, task_id):
            calls.append(("cancel", task_id))
            raise AssertionError("终态任务不应触发取消")

        async def get_task(self, task_id):
            calls.append(("read", task_id))
            return type(
                "T", (),
                {"id": task_id, "title": "t", "status": type(
                    "S", (), {"value": "succeeded"})(),
                 "created_at": None, "updated_at": None},
            )()

    stopper = _ScenarioActiveStopper()
    stopper.bind_task_closer(_TaskCloser())
    result = _asyncio.run(stopper.stop("task", "t_612b9ebc39da4fdd"))
    assert result == "stopped"
    assert calls == [("read", "t_612b9ebc39da4fdd")]


def test_artifact_set_storage_ref_match_passes():
    """T10 S4 实测缺口：最终集合比较写成 list == set（sorted(set(...))
    是列表，右侧是集合），恒 False——storage_ref 全部匹配也判负。正向
    用例锁定：前缀匹配 + 基名集合一致 -> True。"""
    evidence = {
        "status": "complete",
        "artifacts": [
            {"id": "a1", "name": "task-output-a.txt", "kind": "text",
             "storage_ref": "workspace:task-output-a.txt"},
            {"id": "a2", "name": "task-output-b.md", "kind": "markdown",
             "storage_ref": "workspace:task-output-b.md"},
        ],
    }
    content = {
        "assertions": [
            {"assertion_type": "artifact_set",
             "params": {"min_count": 2,
                        "storage_ref_prefix": "workspace:",
                        "workspace_files": ["task-output-a.txt",
                                            "task-output-b.md"]}},
        ],
        "scenario_evidence": evidence,
    }
    value = evaluate_scenario_assertions(content)
    assert value.status == "value"
    assert value.typed_value is True

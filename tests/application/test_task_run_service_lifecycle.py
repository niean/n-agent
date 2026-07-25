import dataclasses

import pytest

from app.application.task_agent_executor import TaskAgentResult
from app.application.task_run_service import TaskRunService
from app.domain.task import (
    FinishRunResult,
    Task,
    TaskConflictError,
    TaskEvent,
    TaskRun,
    TaskRunOutcome,
    TaskStatus,
)
from app.domain.task_policy import TaskPolicy


def _task(**kwargs) -> Task:
    defaults = {"id": "t1", "title": "完成报告", "board": "default"}
    defaults.update(kwargs)
    return Task(**defaults)


def _svc(lifecycle_writer=None, result_writer=None) -> TaskRunService:
    return TaskRunService(
        registry=object(),
        dispatcher=object(),
        executor=object(),
        policy=TaskPolicy(),
        lifecycle_writer=lifecycle_writer,
        result_writer=result_writer,
    )


class _FakeWriter:
    """三参 writer：同时服务 lifecycle_writer（带 card）和 result_writer（card=None）。

    result_writer 以两参 ``writer(session_id, content)`` 调用时，card 默认 None，
    calls 记录三元组 ``(session_id, content, None)``，与 lifecycle_writer 共用同一格式。
    """

    def __init__(self, raises=None):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.raises = raises

    async def __call__(
        self, session_id: str, content: str, card: dict | None = None,
    ):
        self.calls.append((session_id, content, card))
        if self.raises:
            raise self.raises


# ---------------------------------------------------------------------------
# _lifecycle_text (pure mapping)
# ---------------------------------------------------------------------------

def test_lifecycle_text_waiting_approval():
    svc = _svc()
    text = svc._lifecycle_text(_task(), TaskStatus.WAITING_APPROVAL, summary="改用 PDF")
    assert text is not None
    assert "等待批准" in text and "改用 PDF" in text and "完成报告" in text


def test_lifecycle_text_all_endings_return_card():
    """所有任务结束情况均写任务状态卡片（含 summary/error）；QUEUED 不写。"""
    svc = _svc()
    # WAITING_APPROVAL（非终态）仍为状态卡片
    text = svc._lifecycle_text(_task(), TaskStatus.WAITING_APPROVAL, summary="改用 PDF")
    assert text is not None and "等待批准" in text and "改用 PDF" in text
    # 所有终态均写状态卡片（含 summary/error）
    s = svc._lifecycle_text(_task(id="t1", title="完成报告"), TaskStatus.SUCCEEDED, summary="已生成 Q3 总结")
    assert s is not None and "已完成" in s and "Q3 总结" in s
    f = svc._lifecycle_text(_task(id="t2", title="修复"), TaskStatus.FAILED, error="超时")
    assert f is not None and "已失败" in f and "超时" in f
    assert "已取消" in svc._lifecycle_text(_task(id="t3"), TaskStatus.CANCELLED)
    assert "已过期" in svc._lifecycle_text(_task(id="t4"), TaskStatus.EXPIRED)


def test_terminal_result_text_all_endings():
    """所有任务结束情况均产出最终结果正文（普通消息）。"""
    svc = _svc()
    t = _task(id="t1", title="完成报告")
    # SUCCEEDED：含 summary
    s = svc._terminal_result_text(t, TaskStatus.SUCCEEDED, summary="已生成 Q3 总结")
    assert "已完成" in s and "完成报告" in s and "已生成 Q3 总结" in s
    # SUCCEEDED：summary 为空仅展示标题
    assert "已完成" in svc._terminal_result_text(t, TaskStatus.SUCCEEDED, summary=None)
    assert "已完成" in svc._terminal_result_text(t, TaskStatus.SUCCEEDED, summary="   ")
    # FAILED：含 error
    f = svc._terminal_result_text(t, TaskStatus.FAILED, error="超时")
    assert "已失败" in f and "超时" in f
    assert "已失败" in svc._terminal_result_text(t, TaskStatus.FAILED, error=None)
    # CANCELLED / EXPIRED
    assert "已取消" in svc._terminal_result_text(t, TaskStatus.CANCELLED)
    assert "已过期" in svc._terminal_result_text(t, TaskStatus.EXPIRED)
    # 非终态返回 None
    assert svc._terminal_result_text(t, TaskStatus.WAITING_APPROVAL, summary="x") is None
    assert svc._terminal_result_text(t, TaskStatus.QUEUED) is None


def test_lifecycle_text_queued_returns_none():
    """自动重试到 QUEUED 不写生命周期（下次 run_claim 写'开始运行'）。"""
    svc = _svc()
    assert svc._lifecycle_text(_task(), TaskStatus.QUEUED) is None


# ---------------------------------------------------------------------------
# _write_lifecycle (best-effort behavior)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_lifecycle_skipped_when_writer_none():
    svc = _svc(lifecycle_writer=None)
    # 不应抛
    await svc._write_lifecycle(_task(), "[任务状态] 开始运行: t1 - 完成报告")


@pytest.mark.asyncio
async def test_write_lifecycle_records_call_with_origin_session():
    writer = _FakeWriter()
    svc = _svc(lifecycle_writer=writer)
    task = _task(origin_session_id="dashboard-s1")
    await svc._write_lifecycle(task, "[任务状态] 开始运行: t1 - 完成报告")
    assert writer.calls == [
        ("dashboard-s1", "[任务状态] 开始运行: t1 - 完成报告", None),
    ]


@pytest.mark.asyncio
async def test_write_lifecycle_swallows_writer_exception():
    writer = _FakeWriter(raises=RuntimeError("boom"))
    svc = _svc(lifecycle_writer=writer)
    # 不应抛（best-effort，不阻断终结）
    await svc._write_lifecycle(_task(), "x")
    assert len(writer.calls) == 1


# ---------------------------------------------------------------------------
# _write_result (best-effort behavior, ui.task_result 普通消息)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_result_skipped_when_writer_none():
    svc = _svc(result_writer=None)
    # 不应抛
    await svc._write_result(_task(), "任务已完成：完成报告")


@pytest.mark.asyncio
async def test_write_result_records_call_with_origin_session():
    writer = _FakeWriter()
    svc = _svc(result_writer=writer)
    task = _task(origin_session_id="dashboard-s1")
    await svc._write_result(task, "任务已完成：完成报告")
    assert writer.calls == [("dashboard-s1", "任务已完成：完成报告", None)]


@pytest.mark.asyncio
async def test_write_result_swallows_writer_exception():
    writer = _FakeWriter(raises=RuntimeError("boom"))
    svc = _svc(result_writer=writer)
    # 不应抛（best-effort，不阻断终结）
    await svc._write_result(_task(), "x")
    assert len(writer.calls) == 1


@pytest.mark.asyncio
async def test_write_result_if_terminal_writes_for_all_endings():
    """所有终态（SUCCEEDED/FAILED/CANCELLED/EXPIRED）均写最终结果；非终态不写。"""
    writer = _FakeWriter()
    svc = _svc(result_writer=writer)
    task = _task(origin_session_id="dashboard-s1")
    for status in (TaskStatus.SUCCEEDED, TaskStatus.FAILED,
                   TaskStatus.CANCELLED, TaskStatus.EXPIRED):
        await svc._write_result_if_terminal(task, status, summary="ok", error="err")
    assert len(writer.calls) == 4
    assert all(sid == "dashboard-s1" for (sid, _c, _card) in writer.calls)
    assert any("已完成" in c for (_sid, c, _card) in writer.calls)
    assert any("已失败" in c for (_sid, c, _card) in writer.calls)
    assert any("已取消" in c for (_sid, c, _card) in writer.calls)
    assert any("已过期" in c for (_sid, c, _card) in writer.calls)
    # 非终态不写
    writer.calls.clear()
    await svc._write_result_if_terminal(task, TaskStatus.WAITING_APPROVAL, summary="ok")
    await svc._write_result_if_terminal(task, TaskStatus.QUEUED, summary="ok")
    await svc._write_result_if_terminal(task, TaskStatus.RUNNING, summary="ok")
    assert writer.calls == []


# ---------------------------------------------------------------------------
# run_claim writes "开始运行" before executor runs
# ---------------------------------------------------------------------------

class _FakeExecutorRaises:
    async def run(self, task, run_id, claim_lock):
        return TaskAgentResult(status=TaskRunOutcome.COMPLETED, output="done")


class _FakeRegistryConflict:
    """finish_run raises TaskConflictError -> _finish returns None (no terminal write)."""

    async def finish_run(self, cmd):
        raise TaskConflictError("cas conflict")


class _FakeRegistrySuccess:
    """finish_run CAS 成功 -> 返回 FinishRunResult（task 状态切到 target）。"""

    def __init__(self, task: Task):
        self.task = task
        self.last_cmd = None

    async def finish_run(self, cmd):
        self.last_cmd = cmd
        updated = dataclasses.replace(self.task, status=cmd.target_task_status)
        run = TaskRun(id=cmd.run_id, task_id=cmd.task_id, outcome=cmd.outcome)
        event = TaskEvent(
            id=1, task_id=cmd.task_id, kind="finished",
            payload={"outcome": cmd.outcome.value}, run_id=cmd.run_id,
        )
        return FinishRunResult(task=updated, run=run, terminal_event=event)


class _FakeRecoverRegistry:
    """list_running + recover_run：stale 恢复为 EXPIRED，返回 FinishRunResult。"""

    def __init__(self, task: Task, run: TaskRun):
        self._task = task
        self._run = run
        self.recover_calls = []

    async def list_running(self):
        return [self._task]

    async def recover_run(self, cmd):
        self.recover_calls.append(cmd)
        # RecoverRunCommand 无 target_task_status，按 outcome 派生（EXPIRED -> EXPIRED）
        target = (TaskStatus.EXPIRED if cmd.outcome == TaskRunOutcome.EXPIRED
                  else TaskStatus.FAILED)
        updated = dataclasses.replace(self._task, status=target)
        event = TaskEvent(
            id=1, task_id=cmd.task_id, kind="finished",
            payload={"outcome": cmd.outcome.value}, run_id=cmd.run_id,
        )
        return FinishRunResult(task=updated, run=self._run, terminal_event=event)


class _FakeDispatcherNoop:
    async def inspect(self):
        return {"active": []}

    async def cancel(self, token):
        return False


@pytest.mark.asyncio
async def test_run_claim_writes_running_lifecycle_with_origin_session():
    writer = _FakeWriter()
    svc = TaskRunService(
        registry=_FakeRegistryConflict(),
        dispatcher=_FakeDispatcherNoop(),
        executor=_FakeExecutorRaises(),
        policy=TaskPolicy(),
        lifecycle_writer=writer,
    )
    task = _task(
        id="t1", title="完成报告", status=TaskStatus.RUNNING,
        origin_session_id="dashboard-s1",
    )
    await svc.run_claim(task, run_id=1, claim_lock="cl")
    # 开始运行 写到 origin session（card=None，纯文本 lifecycle）
    assert any(
        "开始运行" in c and sid == "dashboard-s1"
        for (sid, c, _card) in writer.calls
    )
    # 开始运行 card 为 None
    assert all(card is None for (_sid, _c, card) in writer.calls)
    # CAS conflict -> 无终态写入
    assert not any("已完成" in c for (_sid, c, _card) in writer.calls)


@pytest.mark.asyncio
async def test_run_claim_no_writer_does_not_crash():
    svc = TaskRunService(
        registry=_FakeRegistryConflict(),
        dispatcher=_FakeDispatcherNoop(),
        executor=_FakeExecutorRaises(),
        policy=TaskPolicy(),
        lifecycle_writer=None,
    )
    task = _task(id="t1", title="完成报告", status=TaskStatus.RUNNING)
    # 不应抛
    await svc.run_claim(task, run_id=1, claim_lock="cl")


@pytest.mark.asyncio
async def test_finish_succeeded_writes_card_and_result():
    """SUCCEEDED CAS 成功：同时写任务状态卡片（ui.task_lifecycle）与最终结果消息（ui.task_result）。"""
    lifecycle_writer = _FakeWriter()
    result_writer = _FakeWriter()
    task = _task(
        id="t1", title="完成报告", status=TaskStatus.RUNNING,
        origin_session_id="dashboard-s1",
    )
    svc = TaskRunService(
        registry=_FakeRegistrySuccess(task),
        dispatcher=_FakeDispatcherNoop(),
        executor=object(),
        policy=TaskPolicy(),
        lifecycle_writer=lifecycle_writer,
        result_writer=result_writer,
    )
    await svc._finish(
        task=task, run_id=1, claim_lock="cl",
        outcome=TaskRunOutcome.COMPLETED, summary="已生成 Q3 总结",
    )
    # 任务状态卡片（含 summary）；SUCCEEDED 无交互动作 -> card=None
    assert any(
        "已完成" in c and "Q3 总结" in c
        for (_sid, c, _card) in lifecycle_writer.calls
    )
    assert all(sid == "dashboard-s1" for (sid, _c, _card) in lifecycle_writer.calls)
    assert all(card is None for (_sid, _c, card) in lifecycle_writer.calls)
    # 最终结果消息（普通消息，含 summary）
    assert len(result_writer.calls) == 1
    assert result_writer.calls[0][0] == "dashboard-s1"
    assert result_writer.calls[0][2] is None  # result_writer 不带 card
    assert "已完成" in result_writer.calls[0][1] and "Q3 总结" in result_writer.calls[0][1]


@pytest.mark.asyncio
async def test_finish_failed_writes_card_and_result():
    """FAILED CAS 成功：同时写任务状态卡片（含 error）与最终结果消息（含 error）。"""
    lifecycle_writer = _FakeWriter()
    result_writer = _FakeWriter()
    task = _task(
        id="t1", title="完成报告", status=TaskStatus.RUNNING,
        origin_session_id="dashboard-s1",
    )
    svc = TaskRunService(
        registry=_FakeRegistrySuccess(task),
        dispatcher=_FakeDispatcherNoop(),
        executor=object(),
        policy=TaskPolicy(),
        lifecycle_writer=lifecycle_writer,
        result_writer=result_writer,
    )
    await svc._finish(
        task=task, run_id=1, claim_lock="cl",
        outcome=TaskRunOutcome.ABORTED, error="工具不可用",
    )
    assert any(
        "已失败" in c and "工具不可用" in c
        for (_sid, c, _card) in lifecycle_writer.calls
    )
    # FAILED 有交互动作（retry/cancel）-> card 为 dict
    assert all(card is not None for (_sid, _c, card) in lifecycle_writer.calls)
    assert len(result_writer.calls) == 1
    assert "已失败" in result_writer.calls[0][1] and "工具不可用" in result_writer.calls[0][1]
    assert result_writer.calls[0][0] == "dashboard-s1"


@pytest.mark.asyncio
async def test_finish_cancelled_writes_card_and_result():
    """CANCELLED CAS 成功：同时写任务状态卡片与最终结果消息。"""
    result_writer = _FakeWriter()
    lifecycle_writer = _FakeWriter()
    task = _task(
        id="t1", title="完成报告", status=TaskStatus.RUNNING,
        origin_session_id="dashboard-s1",
    )
    svc = TaskRunService(
        registry=_FakeRegistrySuccess(task),
        dispatcher=_FakeDispatcherNoop(),
        executor=object(),
        policy=TaskPolicy(),
        lifecycle_writer=lifecycle_writer,
        result_writer=result_writer,
    )
    await svc._finish(
        task=task, run_id=1, claim_lock="cl",
        outcome=TaskRunOutcome.TERMINATED,
    )
    assert any("已取消" in c for (_sid, c, _card) in lifecycle_writer.calls)
    # CANCELLED 无交互动作 -> card=None
    assert all(card is None for (_sid, _c, card) in lifecycle_writer.calls)
    assert len(result_writer.calls) == 1 and "已取消" in result_writer.calls[0][1]


@pytest.mark.asyncio
async def test_finish_waiting_approval_writes_card_not_result():
    """WAITING_APPROVAL 仍写状态卡片（非终态），不写最终结果。"""
    lifecycle_writer = _FakeWriter()
    result_writer = _FakeWriter()
    task = _task(
        id="t1", title="完成报告", status=TaskStatus.RUNNING,
        origin_session_id="dashboard-s1",
    )
    svc = TaskRunService(
        registry=_FakeRegistrySuccess(task),
        dispatcher=_FakeDispatcherNoop(),
        executor=object(),
        policy=TaskPolicy(),
        lifecycle_writer=lifecycle_writer,
        result_writer=result_writer,
    )
    await svc._finish(
        task=task, run_id=1, claim_lock="cl",
        outcome=TaskRunOutcome.WAITING_APPROVAL, summary="修改方案A",
    )
    assert any("等待批准" in c for (_sid, c, _card) in lifecycle_writer.calls)
    # WAITING_APPROVAL 有交互动作 -> card 为 dict
    assert all(card is not None for (_sid, _c, card) in lifecycle_writer.calls)
    assert result_writer.calls == []


@pytest.mark.asyncio
async def test_recover_stale_writes_card_and_result():
    """恢复结束的任务消息：stale lease 恢复为 EXPIRED 时同时写任务状态卡片与最终结果消息。"""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    task = _task(
        id="t_stale", title="过期任务", status=TaskStatus.RUNNING,
        origin_session_id="dashboard-s1",
        claim_lock="old-lock", current_run_id=1, worker_token="wt_1",
        claim_expires=now - timedelta(minutes=5),
    )
    run = TaskRun(id=1, task_id="t_stale", claim_lock="old-lock")
    result_writer = _FakeWriter()
    lifecycle_writer = _FakeWriter()
    svc = TaskRunService(
        registry=_FakeRecoverRegistry(task, run),
        dispatcher=_FakeDispatcherNoop(),
        executor=object(),
        policy=TaskPolicy(),
        lifecycle_writer=lifecycle_writer,
        result_writer=result_writer,
    )
    recovered = await svc._recover_stale_executions(now, svc._fallback_config())
    assert recovered == 1
    # 任务状态卡片；EXPIRED 有交互动作（retry）-> card 为 dict
    assert any(
        "已过期" in c and "过期任务" in c
        for (_sid, c, _card) in lifecycle_writer.calls
    )
    assert all(sid == "dashboard-s1" for (sid, _c, _card) in lifecycle_writer.calls)
    assert all(card is not None for (_sid, _c, card) in lifecycle_writer.calls)
    # 最终结果消息（普通消息）
    assert len(result_writer.calls) == 1
    assert result_writer.calls[0][0] == "dashboard-s1"
    assert "已过期" in result_writer.calls[0][1] and "过期任务" in result_writer.calls[0][1]


def test_decide_target_status_aborted_is_failed_no_retry():
    """Worker 主动快速失败（ABORTED）-> FAILED 终态，绕过断路器不重试。
    回归 t_a742046a521d46eb：worker 判定无法继续应落 FAILED，而非 SUCCEEDED。"""
    svc = _svc()
    # consecutive_failures=0, max_retries=3：系统失败本可重试到 QUEUED
    task = _task(
        id="t1", status=TaskStatus.RUNNING,
        consecutive_failures=0, max_retries=3,
    )
    assert svc._decide_target_status(task, TaskRunOutcome.ABORTED) is TaskStatus.FAILED
    # 对比：系统失败 FAILED 在 max_retries 内 -> QUEUED（可重试）
    assert svc._decide_target_status(task, TaskRunOutcome.FAILED) is TaskStatus.QUEUED


def test_decide_target_status_user_terminated_is_cancelled():
    """用户取消（TERMINATED）-> CANCELLED。worker 不得触发取消语义。"""
    svc = _svc()
    task = _task(id="t1", status=TaskStatus.RUNNING)
    assert svc._decide_target_status(task, TaskRunOutcome.TERMINATED) is TaskStatus.CANCELLED


# ---------------------------------------------------------------------------
# _lifecycle_card (T4: 版本化 card payload)
# ---------------------------------------------------------------------------

def test_lifecycle_card_waiting_approval_payload():
    """WAITING_APPROVAL card（默认 approval）：8 字段 + actions=[approve,reject]
    + summary=提案 + interaction_type='approval'。"""
    svc = _svc()
    task = _task(id="t1", title="完成报告", status=TaskStatus.RUNNING)
    card = svc._lifecycle_card(task, TaskStatus.WAITING_APPROVAL, summary="改用 PDF")
    assert card is not None
    assert card["schema_version"] == 1
    assert card["kind"] == "task_lifecycle"
    assert card["task_id"] == "t1"
    assert card["status"] == "waiting_approval"
    assert card["title"] == "完成报告"
    assert card["summary"] == "改用 PDF"
    assert card["available_actions"] == ["approve", "reject"]
    assert card["interaction_type"] == "approval"


def test_lifecycle_card_waiting_approval_intent_request_payload():
    """WAITING_APPROVAL card（intent_request）：actions=[revise,cancel]
    + interaction_type='intent_request'。"""
    svc = _svc()
    task = _task(id="t1", title="完成报告", status=TaskStatus.RUNNING)
    card = svc._lifecycle_card(
        task, TaskStatus.WAITING_APPROVAL, summary="需补充信息",
        interaction_type="intent_request",
    )
    assert card is not None
    assert card["status"] == "waiting_approval"
    assert card["available_actions"] == ["revise", "cancel"]
    assert card["interaction_type"] == "intent_request"


def test_lifecycle_card_waiting_approval_explicit_approval_interaction_type():
    """显式 interaction_type='approval' 与默认行为一致。"""
    svc = _svc()
    task = _task(id="t1", title="完成报告", status=TaskStatus.RUNNING)
    card = svc._lifecycle_card(
        task, TaskStatus.WAITING_APPROVAL, summary="提案A",
        interaction_type="approval",
    )
    assert card is not None
    assert card["available_actions"] == ["approve", "reject"]
    assert card["interaction_type"] == "approval"


def test_lifecycle_card_failed_payload():
    """FAILED card：actions=[retry,cancel] + summary 优先 error。不含 interaction_type。"""
    svc = _svc()
    task = _task(id="t2", title="修复", status=TaskStatus.RUNNING)
    card = svc._lifecycle_card(task, TaskStatus.FAILED, error="超时", summary="重试耗尽")
    assert card is not None
    assert card["status"] == "failed"
    assert card["summary"] == "超时"  # error 优先
    assert card["available_actions"] == ["retry", "cancel"]
    # FAILED card 不携带 interaction_type 字段（仅 waiting_approval 有）
    assert "interaction_type" not in card


def test_lifecycle_card_failed_ignores_interaction_type_argument():
    """FAILED card 即使传入 interaction_type 也不写入 payload。"""
    svc = _svc()
    task = _task(id="t2", title="修复", status=TaskStatus.RUNNING)
    card = svc._lifecycle_card(
        task, TaskStatus.FAILED, error="超时", interaction_type="approval",
    )
    assert card is not None
    assert "interaction_type" not in card


def test_lifecycle_card_failed_falls_back_to_summary():
    """FAILED 无 error 时 summary 后备。"""
    svc = _svc()
    task = _task(id="t2", title="修复")
    card = svc._lifecycle_card(task, TaskStatus.FAILED, summary="重试耗尽")
    assert card["summary"] == "重试耗尽"


def test_lifecycle_card_failed_empty_summary():
    """FAILED 无 error 无 summary -> summary=''。"""
    svc = _svc()
    task = _task(id="t2", title="修复")
    card = svc._lifecycle_card(task, TaskStatus.FAILED)
    assert card["summary"] == ""


def test_lifecycle_card_expired_payload():
    """EXPIRED card：actions=[retry] + summary 优先 error。"""
    svc = _svc()
    task = _task(id="t3", title="过期任务", status=TaskStatus.RUNNING)
    card = svc._lifecycle_card(task, TaskStatus.EXPIRED, error="lease expired")
    assert card is not None
    assert card["status"] == "expired"
    assert card["summary"] == "lease expired"
    assert card["available_actions"] == ["retry"]


def test_lifecycle_card_expired_falls_back_to_summary():
    """EXPIRED 无 error 时 summary 后备。"""
    svc = _svc()
    task = _task(id="t3", title="过期任务")
    card = svc._lifecycle_card(task, TaskStatus.EXPIRED, summary="heartbeat stale")
    assert card["summary"] == "heartbeat stale"


def test_lifecycle_card_expired_uses_stable_fallback():
    """EXPIRED 空摘要使用稳定文案'任务运行已过期'。"""
    svc = _svc()
    task = _task(id="t3", title="过期任务")
    card = svc._lifecycle_card(task, TaskStatus.EXPIRED)
    assert card["summary"] == "任务运行已过期"


def test_lifecycle_card_returns_none_for_non_interactive():
    """QUEUED/RUNNING/SUCCEEDED/CANCELLED 无交互动作 -> None。"""
    svc = _svc()
    task = _task(id="t4", title="x")
    assert svc._lifecycle_card(task, TaskStatus.QUEUED) is None
    assert svc._lifecycle_card(task, TaskStatus.RUNNING) is None
    assert svc._lifecycle_card(task, TaskStatus.SUCCEEDED, summary="done") is None
    assert svc._lifecycle_card(task, TaskStatus.CANCELLED) is None


def test_lifecycle_card_uses_target_status_not_task_status():
    """传入的 target_status 与 task.status 故意不一致，证明函数用 target snapshot。"""
    svc = _svc()
    task = _task(id="t1", title="完成报告", status=TaskStatus.RUNNING)
    card = svc._lifecycle_card(task, TaskStatus.WAITING_APPROVAL, summary="提案A")
    assert card["status"] == "waiting_approval"  # target_status，不是 "running"
    # task.status 未被改动
    assert task.status == TaskStatus.RUNNING


# ---------------------------------------------------------------------------
# _write_lifecycle_for_status card 传递行为（T4）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_lifecycle_for_status_passes_card_for_interactive_statuses():
    """waiting/failed/expired 传 dict card；succeeded/cancelled 传 None。"""
    writer = _FakeWriter()
    svc = _svc(lifecycle_writer=writer)
    task = _task(id="t1", title="完成报告", origin_session_id="dashboard-s1")
    # 交互态 -> card
    await svc._write_lifecycle_for_status(task, TaskStatus.WAITING_APPROVAL, summary="提案")
    await svc._write_lifecycle_for_status(task, TaskStatus.FAILED, error="超时")
    await svc._write_lifecycle_for_status(task, TaskStatus.EXPIRED)
    # 非交互终态 -> None
    await svc._write_lifecycle_for_status(task, TaskStatus.SUCCEEDED, summary="done")
    await svc._write_lifecycle_for_status(task, TaskStatus.CANCELLED)
    # QUEUED -> _lifecycle_text 返回 None -> 不写
    await svc._write_lifecycle_for_status(task, TaskStatus.QUEUED)
    assert len(writer.calls) == 5
    interactive = ("waiting_approval", "failed", "expired")
    for i, status in enumerate(interactive):
        _sid, _content, card = writer.calls[i]
        assert card is not None
        assert card["status"] == status
        assert card["schema_version"] == 1
    # SUCCEEDED / CANCELLED -> card=None
    assert writer.calls[3][2] is None
    assert writer.calls[4][2] is None


@pytest.mark.asyncio
async def test_write_lifecycle_for_status_passes_interaction_type_to_card():
    """interaction_type 从 _write_lifecycle_for_status 透传到 _lifecycle_card。

    默认（不传 interaction_type）-> waiting_approval card 带 interaction_type='approval'，
    actions=[approve,reject]；intent_request -> interaction_type='intent_request'，
    actions=[revise,cancel]。
    """
    writer = _FakeWriter()
    svc = _svc(lifecycle_writer=writer)
    task = _task(id="t1", title="完成报告", origin_session_id="dashboard-s1")
    # 默认 approval
    await svc._write_lifecycle_for_status(task, TaskStatus.WAITING_APPROVAL, summary="提案A")
    # intent_request
    await svc._write_lifecycle_for_status(
        task, TaskStatus.WAITING_APPROVAL, summary="需补充信息",
        interaction_type="intent_request",
    )
    assert len(writer.calls) == 2
    _sid1, _content1, card1 = writer.calls[0]
    assert card1["interaction_type"] == "approval"
    assert card1["available_actions"] == ["approve", "reject"]
    _sid2, _content2, card2 = writer.calls[1]
    assert card2["interaction_type"] == "intent_request"
    assert card2["available_actions"] == ["revise", "cancel"]


@pytest.mark.asyncio
async def test_lifecycle_writer_exception_does_not_rollback_or_duplicate():
    """lifecycle writer 抛错时，既有 CAS 结果不回滚、writer 不重试（best-effort）。"""
    writer = _FakeWriter(raises=RuntimeError("writer boom"))
    task = _task(
        id="t1", title="完成报告", status=TaskStatus.RUNNING,
        origin_session_id="dashboard-s1",
    )
    registry = _FakeRegistrySuccess(task)
    svc = TaskRunService(
        registry=registry,
        dispatcher=_FakeDispatcherNoop(),
        executor=object(),
        policy=TaskPolicy(),
        lifecycle_writer=writer,
    )
    # _finish 不应抛（best-effort 吞 writer 异常）
    await svc._finish(
        task=task, run_id=1, claim_lock="cl",
        outcome=TaskRunOutcome.WAITING_APPROVAL, summary="提案A",
    )
    # CAS 成功一次（target 切到 WAITING_APPROVAL，未回滚）
    assert registry.last_cmd is not None
    assert registry.last_cmd.target_task_status == TaskStatus.WAITING_APPROVAL
    # writer 仅尝试一次（不重试、不重复）
    assert len(writer.calls) == 1
    # 写入的 card 仍是结构化对象（即便 writer 抛错，card 已构造并传递）
    _sid, _content, card = writer.calls[0]
    assert card is not None
    assert card["status"] == "waiting_approval"

import pytest

from app.application.task_agent_executor import TaskAgentResult
from app.application.task_run_service import TaskRunService
from app.domain.task import (
    Task,
    TaskConflictError,
    TaskRunOutcome,
    TaskStatus,
)
from app.domain.task_policy import TaskPolicy


def _task(**kwargs) -> Task:
    defaults = {"id": "t1", "title": "完成报告", "board": "default"}
    defaults.update(kwargs)
    return Task(**defaults)


def _svc(lifecycle_writer=None) -> TaskRunService:
    return TaskRunService(
        registry=object(),
        dispatcher=object(),
        executor=object(),
        policy=TaskPolicy(),
        lifecycle_writer=lifecycle_writer,
    )


class _FakeWriter:
    def __init__(self, raises=None):
        self.calls: list[tuple[str, str]] = []
        self.raises = raises

    async def __call__(self, session_id: str, content: str):
        self.calls.append((session_id, content))
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


def test_lifecycle_text_succeeded():
    svc = _svc()
    text = svc._lifecycle_text(_task(), TaskStatus.SUCCEEDED, summary="已生成 Q3 总结")
    assert text is not None and "已完成" in text and "已生成 Q3 总结" in text


def test_lifecycle_text_failed_uses_error():
    svc = _svc()
    text = svc._lifecycle_text(_task(), TaskStatus.FAILED, error="超时")
    assert text is not None and "已失败" in text and "超时" in text


def test_lifecycle_text_cancelled_and_expired():
    svc = _svc()
    assert "已取消" in svc._lifecycle_text(_task(), TaskStatus.CANCELLED)
    assert "已过期" in svc._lifecycle_text(_task(), TaskStatus.EXPIRED)


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
    assert writer.calls == [("dashboard-s1", "[任务状态] 开始运行: t1 - 完成报告")]


@pytest.mark.asyncio
async def test_write_lifecycle_swallows_writer_exception():
    writer = _FakeWriter(raises=RuntimeError("boom"))
    svc = _svc(lifecycle_writer=writer)
    # 不应抛（best-effort，不阻断终结）
    await svc._write_lifecycle(_task(), "x")
    assert len(writer.calls) == 1


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
    # 开始运行 写到 origin session
    assert any("开始运行" in c and sid == "dashboard-s1" for (sid, c) in writer.calls)
    # CAS conflict -> 无终态写入
    assert not any("已完成" in c for (_sid, c) in writer.calls)


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

"""agent_graph finalize 线上评估捕获钩子测试（plan T16 S1）。

覆盖：
- finalize 后 best-effort 捕获：p=1 时样本入队、快照内容含 query/最终回答/
  工具轨迹/模型版本；默认 p=0 仅计 eligible 不捕获。
- 捕获异常不回滚业务成功：捕获路径注入故障（脱敏器抛错/入队抛错）时
  finalize 正常完成，业务状态不受影响。
- 业务完成钩子不等待网络/Judge：后台消费者被挂起时 finalize 照常返回。
- 未注入捕获服务时行为完全向后兼容。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from app.application.agent_graph import AgentGraphRunner
from app.application.evaluation_capture import (
    EvaluationCaptureQueue,
    EvaluationCaptureService,
    EvaluationSamplingPolicy,
)
from app.application.tool_service import ToolService, builtin_tool_definitions
from app.domain.agent import AgentState
from app.domain.provider import LLMResult, ModelInfo
from app.domain.session import ConversationSession
from app.domain.tool import ToolExecutionContext
from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.infrastructure.tools.builtin import build_builtin_tool_executor

from app.application.evaluation_service import SubmitSampleResult


class FakeEvidenceStore:
    def __init__(self, fail_first: int = 0):
        self.saved = []
        self._fail_first = fail_first

    async def save_snapshot(self, target, snapshot):
        if self._fail_first > 0:
            self._fail_first -= 1
            raise RuntimeError("evidence store down")
        ref = f"snapshots/{len(self.saved):064x}.json"
        self.saved.append((target, snapshot, ref))
        return ref


class FakeEvaluationService:
    def __init__(self):
        self.requests = []
        self.registered: tuple = ()

    async def definitions_for_target(self, target_kind):
        return self.registered

    async def submit_sample(self, request):
        self.requests.append(request)
        return SubmitSampleResult(sample_id=f"s-{len(self.requests)}", job_ids=())


class DirectProvider:
    def __init__(self):
        self.calls = 0

    async def list_models(self):
        return [ModelInfo("test", "test", "fake")]

    async def supports_tools(self, model: str):
        return True

    async def chat(self, messages, tools, stream, model, options):
        self.calls += 1
        return LLMResult(message={"role": "assistant", "content": "hello"}, finish_reason="stop")


class CalculatorProvider:
    """一次 tool_calls（calculator）后给出最终回答。"""

    def __init__(self):
        self.calls = 0

    async def list_models(self):
        return [ModelInfo("test", "test", "fake")]

    async def supports_tools(self, model: str):
        return True

    async def chat(self, messages, tools, stream, model, options):
        self.calls += 1
        if self.calls == 1:
            return LLMResult(
                message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "calculator", "arguments": '{"expression":"1+2"}'},
                        }
                    ],
                },
                finish_reason="tool_calls",
            )
        return LLMResult(message={"role": "assistant", "content": "result is 3"}, finish_reason="stop")


class LoopProvider:
    """永远要求 tool_calls，用于触发 ITERATION_LIMIT 失败路径。"""

    def __init__(self):
        self.calls = 0

    async def list_models(self):
        return [ModelInfo("test", "test", "fake")]

    async def supports_tools(self, model: str):
        return True

    async def chat(self, messages, tools, stream, model, options):
        self.calls += 1
        return LLMResult(
            message={
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call-{self.calls}",
                        "type": "function",
                        "function": {"name": "calculator", "arguments": '{"expression":"1+2"}'},
                    }
                ],
            },
            finish_reason="tool_calls",
        )


class ExplodingQueue:
    def put_nowait(self, item):
        raise RuntimeError("queue down")


def make_capture_service(*, rate: float = 1.0, failure_rate: float = 0.0, **kwargs) -> EvaluationCaptureService:
    params: dict[str, Any] = {
        "policy": EvaluationSamplingPolicy(rate=rate, failure_rate=failure_rate),
        "queue_maxsize": 100,
        "evidence_store": FakeEvidenceStore(),
        "evaluation_service": FakeEvaluationService(),
    }
    params.update(kwargs)
    return EvaluationCaptureService(**params)


def make_runner(tmp_path, provider, capture, iteration_limit: int = 3) -> tuple[AgentGraphRunner, SQLiteMemoryStore]:
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    runner = AgentGraphRunner(
        provider,
        ToolService(
            build_builtin_tool_executor(tmp_path),
            builtin_tool_definitions(),
            evaluation_capture=capture,
        ),
        store,
        HeuristicSummarizer(),
        iteration_limit=iteration_limit,
        evaluation_capture=capture,
    )
    return runner, store


async def run_chat(tmp_path, provider, capture, *, user_content="hello task", iteration_limit=3):
    runner, store = make_runner(tmp_path, provider, capture, iteration_limit)
    await store.create_session(ConversationSession(id="s1"))
    state = AgentState(
        session_id="s1",
        input_messages=[{"role": "user", "content": user_content}],
    )
    # 生产路径由 chat_service 注入带 session_id 的 ToolExecutionContext；
    # 测试同形注入，使工具轨迹证据能按会话登记
    options = {
        "tool_execution_context": ToolExecutionContext(
            session_id="s1", run_id=state.run_id,
        ),
    }
    state = await runner.run(state, "test-model", options)
    return state, service_counters(capture)


def service_counters(capture):
    if capture is None:
        return None
    return capture.counters.snapshot()


async def wait_until(condition, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.01)
    return False


@pytest.mark.asyncio
async def test_finalize_captures_selected_run(tmp_path):
    capture = make_capture_service(rate=1.0)
    state, counters = await run_chat(
        tmp_path, DirectProvider(), capture, user_content="say hello",
    )
    assert state.final_message["content"] == "hello"
    assert counters["eligible"] == 1
    assert counters["selected"] == 1
    assert counters["captured"] == 1
    assert counters["capture_error"] == 0
    items = capture.queue.queued_items()
    assert len(items) == 1
    content = items[0].snapshot.content
    assert content["query"] == "say hello"
    assert content["final_answer"] == "hello"
    assert content["model"] == "test-model"
    assert items[0].target.session_id == "s1"


@pytest.mark.asyncio
async def test_finalize_snapshot_includes_tool_traces(tmp_path):
    capture = make_capture_service(rate=1.0)
    state, counters = await run_chat(
        tmp_path, CalculatorProvider(), capture, user_content="calc 1+2",
    )
    assert state.final_message["content"] == "result is 3"
    assert counters["captured"] == 1
    content = capture.queue.queued_items()[0].snapshot.content
    tools = content["tool_traces"]
    assert tools, "快照必须包含允许的工具轨迹"
    calculator = [trace for trace in tools if trace.get("tool") == "calculator"]
    assert calculator
    assert calculator[0]["status"] == "success"


@pytest.mark.asyncio
async def test_default_sampling_off_records_eligible_only(tmp_path):
    capture = make_capture_service(rate=0.0)
    state, counters = await run_chat(tmp_path, DirectProvider(), capture)
    assert state.final_message["content"] == "hello"
    assert counters["eligible"] == 1
    assert counters["selected"] == 0
    assert counters["captured"] == 0
    assert capture.queue.queued_items() == []


@pytest.mark.asyncio
async def test_failure_targeted_cohort_captures_failed_run(tmp_path):
    capture = make_capture_service(rate=0.0, failure_rate=1.0)
    state, counters = await run_chat(
        tmp_path, LoopProvider(), capture, iteration_limit=1,
    )
    assert state.error
    assert counters["selected"] == 1
    assert counters["captured"] == 1
    content = capture.queue.queued_items()[0].snapshot.content
    assert content["sampling"]["cohort"] == "failure"
    assert content["failed"] is True


@pytest.mark.asyncio
async def test_capture_failure_does_not_break_finalize(tmp_path):
    def exploding_redactor(text):
        raise RuntimeError("redactor down")

    capture = make_capture_service(rate=1.0, redactor=exploding_redactor)
    state, counters = await run_chat(tmp_path, DirectProvider(), capture)
    # 业务成功不受捕获异常影响
    assert state.final_message["content"] == "hello"
    assert state.error is None
    assert counters["capture_error"] == 1
    assert counters["captured"] == 0


@pytest.mark.asyncio
async def test_enqueue_failure_does_not_break_finalize(tmp_path):
    capture = make_capture_service(rate=1.0)
    capture.queue = ExplodingQueue()
    state, counters = await run_chat(tmp_path, DirectProvider(), capture)
    assert state.final_message["content"] == "hello"
    assert state.error is None
    assert counters["capture_error"] == 1


@pytest.mark.asyncio
async def test_finalize_does_not_wait_for_blocked_consumer(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_handler(item):
        started.set()
        await release.wait()

    queue = EvaluationCaptureQueue(maxsize=10, handler=blocked_handler)
    capture = make_capture_service(rate=1.0)
    capture.queue = queue
    await queue.start()
    try:
        state, counters = await run_chat(tmp_path, DirectProvider(), capture)
        # finalize 已返回（业务完成），消费者仍被挂起
        assert state.final_message["content"] == "hello"
        assert started.is_set()
        assert not queue.done
        assert counters["captured"] == 1
    finally:
        release.set()
        await queue.stop()


@pytest.mark.asyncio
async def test_background_consumer_registers_sample(tmp_path):
    capture = make_capture_service(rate=1.0)
    await capture.start()
    try:
        state, counters = await run_chat(tmp_path, DirectProvider(), capture)
        assert state.final_message["content"] == "hello"
        evaluation = capture.evaluation_service
        assert await wait_until(lambda: len(evaluation.requests) == 1)
        request = evaluation.requests[0]
        assert request.service == "n-agent"
        assert request.target.kind == "agent_run"
        assert request.snapshot_ref
        # 反向索引：session + agent_run 标识必须随线上样本持久化（T18 删除
        # 传播依赖，spec 6.3 evaluation_evidence_targets）
        targets = request.evidence_targets
        kinds = {(t["subject_kind"], t["subject_id"]) for t in targets}
        assert ("session", "s1") in kinds
        assert any(kind == "agent_run" for kind, _ in kinds)
    finally:
        await capture.stop()


@pytest.mark.asyncio
async def test_no_capture_service_backward_compatible(tmp_path):
    state, counters = await run_chat(tmp_path, DirectProvider(), None)
    assert state.final_message["content"] == "hello"
    assert counters is None


def test_remember_run_model_evicts_oldest_not_clear_all(tmp_path):
    """容量超限按插入序淘汰最旧，不清空全部（并发在途 run 不被误伤，Mi2）。"""
    runner, _store = make_runner(
        tmp_path, DirectProvider(), None,
    )
    runner._RUN_MODELS_MAX = 3
    runner._remember_run_model("run-1", "m1")
    runner._remember_run_model("run-2", "m2")
    runner._remember_run_model("run-3", "m3")
    runner._remember_run_model("run-4", "m4")
    # 最旧（run-1）被淘汰，其余在途条目全部保留；新条目在位
    assert "run-1" not in runner._run_models
    assert runner._run_models == {
        "run-2": "m2", "run-3": "m3", "run-4": "m4",
    }
    # finalize 捕获时 pop 取走后不再残留
    assert runner._run_models.pop("run-4", None) == "m4"
    assert "run-4" not in runner._run_models

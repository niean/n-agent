# app.infrastructure.evaluation.target_runner 测试（plan T13，spec 9.1 / 11.1）。
#
# 覆盖：
# - 受信实验采样器：根 span 强制采样（覆盖 ParentBased）；子 span 继续
#   ParentBased 继承；无受信上下文委托 base；伪造 baggage 不能开启覆盖。
# - experiment_item_root_scope：实验身份属性 + root_observation_id = 根真实
#   span ID；replay Link 原目标真实身份（tracer 经 monkeypatch 注入本地
#   provider，不触碰全局 TracerProvider）。
# - 冻结快照环境指纹：commit 读取 / 失败标 external_unpinned / 缺失字段标记。
# - N-Agent / N-KB 目标适配：多轮共享会话、传输错误分类、检索条目契约。
from __future__ import annotations

import asyncio
import hashlib

import pytest
from opentelemetry import context as context_api
from opentelemetry import propagate, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import SpanContext, TraceFlags

from app.application import telemetry_semantics as ts
from app.application.chat_service import ChatCompletionResult
from app.domain.evaluation.models import (
    EXTERNAL_UNPINNED,
    canonical_json_bytes,
)
from app.infrastructure.evaluation.target_runner import (
    TRUSTED_EXPERIMENT_CONTEXT_KEY,
    ChatCompletionTargetRunner,
    KBDirectTargetRunner,
    TrustedExperimentSampler,
    build_frozen_environment,
    default_git_commit_reader,
    experiment_item_root_scope,
)
from app.infrastructure.observability import setup as observability_setup

HEX32 = "a" * 32
HEX16 = "b" * 16


class FakeExporter:
    def __init__(self):
        self.spans = []

    def export(self, spans):
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


@pytest.fixture()
def traced(monkeypatch):
    """本地 TracerProvider + 受信采样器；注入 ts.get_tracer（不替换全局
    TracerProvider）。返回 (provider, exporter)。"""
    exporter = FakeExporter()
    provider = TracerProvider(
        sampler=TrustedExperimentSampler(ParentBased(TraceIdRatioBased(0.0)))
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        ts, "get_tracer", lambda: provider.get_tracer(
            "nagent.infrastructure.evaluation.target_runner"
        )
    )
    return provider, exporter


def _sampled_spans(exporter: FakeExporter) -> list:
    return [
        span
        for span in exporter.spans
        if span.context.trace_flags & TraceFlags.SAMPLED
    ]


# ---------------------------------------------------------------------------
# TrustedExperimentSampler
# ---------------------------------------------------------------------------


def test_trusted_context_forces_root_sampling_overriding_parent_based(traced):
    _, exporter = traced  # base ratio=0：普通根一律 DROP
    with experiment_item_root_scope({
        "experiment_id": "exp-1",
        "experiment_name": "exp-a",
        "dataset_id": "ds-1",
        "item_id": "case-1#r0",
    }):
        span = trace.get_current_span()
        assert span.get_span_context().is_valid
    assert _sampled_spans(exporter), "实验项根必须被强制采样"


def test_child_spans_remain_parent_based_under_trusted_context(traced):
    _, exporter = traced
    with experiment_item_root_scope({"item_id": "case-1#r0"}) as root:
        tracer = traced[0].get_tracer("test")
        with tracer.start_as_current_span("child.business") as child:
            assert child.get_span_context().trace_id == int(root.trace_id, 16)
    # 根 + 子 span 均被采样：子 span 经 ParentBased 继承根的强制采样决定
    assert len(_sampled_spans(exporter)) == 2


def test_sampler_without_trusted_context_defers_to_base():
    sampler = TrustedExperimentSampler(ParentBased(TraceIdRatioBased(0.0)))
    result = sampler.should_sample(
        parent_context=None, trace_id=0xABCDEF, name="plain.root"
    )
    assert result.decision.value == 0  # DROP（委托 ParentBased(ratio=0)）


def test_sampler_with_valid_parent_defers_to_parent_based():
    # 受信上下文中有有效父级（子 span 场景）：委托 ParentBased——未采样
    # 父级的子 span 仍被丢弃，覆盖逻辑不扩散到子 span
    sampler = TrustedExperimentSampler(ParentBased(TraceIdRatioBased(1.0)))
    parent_sc = SpanContext(
        trace_id=0xABCDEF0123456789,
        span_id=0x1234567890ABCDEF,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.DEFAULT),  # 未采样父级
    )
    ctx = context_api.set_value(
        TRUSTED_EXPERIMENT_CONTEXT_KEY,
        object(),  # 非本模块令牌：即使键被占位也不覆盖
        trace.set_span_in_context(trace.NonRecordingSpan(parent_sc)),
    )
    result = sampler.should_sample(
        parent_context=ctx, trace_id=0xABCDEF0123456789, name="child"
    )
    assert result.decision.value == 0  # 继承未采样父级 -> DROP


def test_forged_baggage_cannot_enable_override(traced):
    _, exporter = traced
    # 客户端经 header 伪造 baggage（实验身份/受信标记）：不能开启根采样
    # 覆盖
    carrier = {
        "baggage": "nagent.evaluation.experiment.id=exp-1,nagent.trusted=true"
    }
    ctx = propagate.extract(carrier)
    tracer = traced[0].get_tracer("test")
    span = tracer.start_span("forged.root", context=ctx)
    span_context = span.get_span_context()
    if span_context.is_valid:
        assert not (span_context.trace_flags & TraceFlags.SAMPLED)
    span.end()
    assert not _sampled_spans(exporter)


def test_setup_build_sampler_wraps_trusted_sampler():
    sampler = observability_setup._build_sampler(0.5)
    assert type(sampler).__name__ == "TrustedExperimentSampler"
    assert type(sampler.base).__name__ == "ParentBased"
    assert sampler.base._root.rate == 0.5


def test_root_scope_with_proxy_tracer_yields_invalid_handle(monkeypatch):
    # OTel 禁用（无 SDK provider 注册）的真实 proxy 路径：ProxyTracer 解析到
    # NoOpTracer，start_span 返回 NonRecordingSpan——句柄无效、不抛错、
    # 不产生任何 span（实验分析标 unavailable）。
    # 注：`trace._TRACER_PROVIDER` 是上游私有全局（钉 opentelemetry 1.45.1，
    # 与 evaluation_telemetry.get_logger 的私有布局耦合同一模式）；置 None
    # 即"未注册 SDK provider"的缺省态，跨测试顺序确定性复现禁用路径。
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)
    assert type(trace.get_tracer("probe")).__name__ == "ProxyTracer"
    with experiment_item_root_scope({
        "experiment_id": "exp-1",
        "experiment_name": "exp-a",
        "dataset_id": "ds-1",
        "item_id": "case-1#r0",
    }) as root:
        assert root.is_valid is False
        assert root.trace_id is None
        assert root.span_id is None


# ---------------------------------------------------------------------------
# experiment_item_root_scope：身份属性 / root_observation_id / replay Link
# ---------------------------------------------------------------------------


def test_root_scope_sets_experiment_identity_and_root_observation_id(traced):
    _, exporter = traced
    with experiment_item_root_scope({
        "experiment_id": "exp-1",
        "experiment_name": "exp-a",
        "dataset_id": "ds-1",
        "item_id": "case-1#r0",
    }):
        pass
    assert len(exporter.spans) == 1
    span = exporter.spans[0]
    attrs = span.attributes
    assert attrs["nagent.evaluation.experiment.id"] == "exp-1"
    assert attrs["nagent.evaluation.experiment.name"] == "exp-a"
    assert attrs["nagent.evaluation.experiment.dataset_id"] == "ds-1"
    assert attrs["nagent.evaluation.experiment.item_id"] == "case-1#r0"
    # root_observation_id 必须等于根的真实 span ID（spec 11.2）
    assert (
        attrs["nagent.evaluation.experiment.root_observation_id"]
        == format(span.context.span_id, "016x")
    )


def test_root_scope_links_original_target_for_replay(traced):
    _, exporter = traced
    with experiment_item_root_scope({
        "experiment_id": "exp-2",
        "experiment_name": "exp-src-replay",
        "dataset_id": "ds-1",
        "item_id": "replay:sample-1",
        "mode": "replay",
        "links": ((HEX32, HEX16),),
    }):
        pass
    span = exporter.spans[0]
    assert span.links, "replay 评分实验项必须 Link 原目标"
    link_context = span.links[0].context
    assert format(link_context.trace_id, "032x") == HEX32
    assert format(link_context.span_id, "016x") == HEX16
    assert link_context.is_remote


# ---------------------------------------------------------------------------
# 冻结快照环境指纹
# ---------------------------------------------------------------------------


def test_build_frozen_environment_reads_commit_and_fingerprints():
    frozen = build_frozen_environment(
        commit_reader=lambda: "f" * 40,
        fingerprints={
            "system_prompt_hash": "e" * 64,
            "kb_corpus_config": "corp-1",
        },
    )
    assert frozen["code_commit"] == "f" * 40
    assert frozen["system_prompt_hash"] == "e" * 64
    assert frozen["kb_corpus_config"] == "corp-1"
    for missing in (
        "target_model_request_version",
        "target_model_response_version",
        "tool_inventory_hash",
        "skill_inventory_hash",
        "mcp_inventory_hash",
    ):
        assert frozen[missing] == EXTERNAL_UNPINNED


def test_build_frozen_environment_commit_failure_marks_unpinned():
    def _boom():
        raise RuntimeError("git unavailable")

    frozen = build_frozen_environment(commit_reader=_boom)
    assert frozen["code_commit"] == EXTERNAL_UNPINNED


def test_build_frozen_environment_hashes_structured_values():
    # 契约：应用层只接受非空字符串指纹；结构化值由本层 canonical-JSON
    # SHA-256 折叠为哈希（保留结构可校验性），None/空串标 external_unpinned
    frozen = build_frozen_environment(
        commit_reader=lambda: "f" * 40,
        fingerprints={
            "tool_inventory_hash": {"tools": ["browser.open", "kb.search"]},
            "kb_corpus_config": {"corpus": "corp-1", "top_k": 5},
            "system_prompt_hash": "",
        },
    )
    expected = hashlib.sha256(
        canonical_json_bytes({"tools": ["browser.open", "kb.search"]})
    ).hexdigest()
    assert frozen["tool_inventory_hash"] == expected
    assert len(frozen["kb_corpus_config"]) == 64
    assert all(c in "0123456789abcdef" for c in frozen["kb_corpus_config"])
    assert frozen["system_prompt_hash"] == EXTERNAL_UNPINNED


def test_default_git_commit_reader_reads_repo_head():
    commit = default_git_commit_reader()
    assert isinstance(commit, str)
    assert len(commit) == 40
    assert all(c in "0123456789abcdef" for c in commit)


# ---------------------------------------------------------------------------
# N-Agent 目标适配（ChatCompletionService 注入）
# ---------------------------------------------------------------------------


class FakeChatService:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[object] = []
        self._error = error

    async def complete(self, request):
        self.calls.append(request)
        if self._error is not None:
            raise self._error
        return ChatCompletionResult(
            session_id=request.session_id or "api-x",
            model=request.model,
            message={"content": f"answer-{len(self.calls)}"},
            finish_reason="stop",
            run_id=f"run-{len(self.calls)}",
        )


def test_chat_completion_target_runner_shares_session_across_turns():
    chat = FakeChatService()
    runner = ChatCompletionTargetRunner(chat=chat, model="gpt-x")
    result = asyncio.run(
        runner.run_target(
            object(),
            {
                "mode": "execute",
                "target_config": {"model": "gpt-x"},
                "session": {
                    "namespace": "eval-experiment-e1-case-1",
                    "session_id": "eval-experiment-e1-case-1-r0",
                },
                "turns": [{"message": "t1"}, {"message": "t2"}],
            },
        )
    )
    assert len(chat.calls) == 2  # 多轮逐轮执行
    assert chat.calls[0].session_id == "eval-experiment-e1-case-1-r0"
    assert chat.calls[1].session_id == chat.calls[0].session_id  # 共享会话
    assert result["status"] == "succeeded"
    assert result["output"]["final_answer"] == "answer-2"
    assert result["run_id"] == "run-2"


def test_chat_completion_target_runner_transport_error():
    chat = FakeChatService(error=ConnectionError("boom"))
    runner = ChatCompletionTargetRunner(chat=chat, model="gpt-x")
    result = asyncio.run(
        runner.run_target(
            object(),
            {
                "session": {"namespace": "ns", "session_id": "sid"},
                "turns": [{"message": "t1"}],
            },
        )
    )
    assert result["status"] == "transport_error"
    assert result["error_type"] == "network"
    assert result["side_effects"] == []  # 无业务副作用，允许安全重试


def test_chat_completion_missing_finish_reason_reports_not_completed():
    # finish_reason 缺失按未完成处理（保守口径，不默认成功）
    class NoFinishReasonChat:
        async def complete(self, request):
            return ChatCompletionResult(
                session_id=request.session_id or "api-x",
                model=request.model,
                message={"content": "answer"},
                finish_reason=None,
                run_id="run-1",
            )

    runner = ChatCompletionTargetRunner(chat=NoFinishReasonChat(), model="m")
    result = asyncio.run(
        runner.run_target(
            object(),
            {"session": {"namespace": "ns", "session_id": "sid"},
             "turns": [{"message": "t1"}]},
        )
    )
    assert result["status"] == "succeeded"
    assert result["output"]["completion_reported"] is False


# ---------------------------------------------------------------------------
# N-KB 目标适配（直调 adapter 注入）
# ---------------------------------------------------------------------------


def test_kb_direct_target_runner_returns_retrieval_entries():
    async def retrieve(payload):
        assert payload["query"] == "query-1"
        assert payload["top_k"] == 5
        return {
            "retrieval_id": "ret-1",
            "trace_id": HEX32,
            "span_id": HEX16,
            "corpus_revision": "corp-1",
            "results": [
                {
                    "corpus_revision": "corp-1",
                    "document_revision": "doc-1",
                    "chunk_id": "chunk-1",
                    "rank": 1,
                }
            ],
        }

    runner = KBDirectTargetRunner(retrieve=retrieve)
    result = asyncio.run(
        runner.run_target(
            object(),
            {
                "session": {"namespace": "ns", "session_id": "sid"},
                "turns": [{"query": "query-1"}],
            },
        )
    )
    assert result["status"] == "succeeded"
    assert len(result["retrievals"]) == 1
    assert result["retrievals"][0]["retrieval_id"] == "ret-1"
    assert result["retrievals"][0]["trace_id"] == HEX32
    assert result["output"]["corpus_revision"] == "corp-1"


def test_kb_direct_target_runner_error_is_transport():
    async def retrieve(payload):
        raise TimeoutError("kb timeout")

    runner = KBDirectTargetRunner(retrieve=retrieve)
    result = asyncio.run(
        runner.run_target(
            object(),
            {"session": {}, "turns": [{"query": "q"}]},
        )
    )
    assert result["status"] == "transport_error"
    assert result["retrievals"] == []

"""T8：N-KB 检索证据判定与比较分组测试（plan T8 S1）。

覆盖反例（证据纪律，全部 fail-closed）：
- 错误 backend（非 n_kb）与无关空结果不通过；
- 捏造正文：回答中的"引用"必须能在真实检索结果原文中找到连续片段，
  编造的无依据内容不通过；
- 缺原文引用且缺标题+链接不通过；require_quote 时标题+链接不替代原文；
- 消费文档版本缺失（document_revision 缺失）不通过——版本不可考的
  检索条目不充当证据；
- 禁止 web/browser 工具：web_fetch 出现在正式端口证据即失败；
- 检索证据由适配器从正式端口 search_knowledge 记录派生，不合成；
- 比较分组：scope/profile/fixture/binding 不同的结果不混成同一可比
  总体；派生固定题单列标注，不顶替源题；非场景 case 给出不可比原因。
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.application.evaluation_e2e_conversion import (
    SOURCE_DIR_RELPATH,
    convert_e2e_conversations,
)
from app.application.evaluation_scenario_assertions import (
    evaluate_scenario_assertions,
)

KB_TITLE = "新增预算单元操作指引"
KB_SOURCE = "https://kb.example.com/docs/budget-unit"
KB_TEXT = (
    "新增预算单元的入口位于设置页，进入预算中心后点击新增预算单元按钮，"
    "填写名称与额度后保存即可创建。"
)


def _kb_result(**overrides):
    item = {
        "title": KB_TITLE,
        "content": KB_TEXT,
        "source": KB_SOURCE,
        "document_revision": "rev-7",
    }
    item.update(overrides)
    return item


def _kb_content(**overrides):
    base = {
        "assertions": [
            {
                "assertion_type": "kb_source_and_quote",
                "params": {"min_results": 1, "require_quote": False},
            },
        ],
        "scenario_evidence": {
            "status": "complete",
            "steps": [
                {"step_id": "turn-1", "run_id": "run-a", "answer": ""},
            ],
            "tool_calls": [
                {
                    "run_id": "run-a",
                    "tool_name": "search_knowledge",
                    "status": "success",
                    "result": "see retrievals",
                },
            ],
            "retrievals": [
                {
                    "retrieval_id": "ret-1",
                    "backend": "n_kb",
                    "run_id": "run-a",
                    "query": "新增预算单元",
                    "results": [_kb_result()],
                },
            ],
        },
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# kb_source_and_quote：n_kb 来源 / 非空相关条目 / 引用纪律
# ---------------------------------------------------------------------------


def test_kb_quote_from_retrieved_content_passes():
    answer = f"知识库原文：{KB_TEXT}"
    content = _kb_content(
        **{
            "scenario_evidence": {
                **_kb_content()["scenario_evidence"],
                "steps": [
                    {"step_id": "turn-1", "run_id": "run-a", "answer": answer},
                ],
            },
        }
    )
    value = evaluate_scenario_assertions(content)
    assert value.status == "value"
    assert value.typed_value is True


def test_kb_wrong_backend_fails():
    evidence = _kb_content()["scenario_evidence"]
    evidence["retrievals"][0]["backend"] = "ragflow"
    value = evaluate_scenario_assertions(_kb_content(scenario_evidence=evidence))
    assert value.status == "value"
    assert value.typed_value is False


def test_kb_empty_results_fails():
    evidence = _kb_content()["scenario_evidence"]
    evidence["retrievals"][0]["results"] = []
    value = evaluate_scenario_assertions(_kb_content(scenario_evidence=evidence))
    assert value.status == "value"
    assert value.typed_value is False


def test_kb_unrelated_blank_content_fails():
    """results 非空但条目正文为空白（无关空结果）：不充当证据。"""
    evidence = _kb_content()["scenario_evidence"]
    evidence["retrievals"][0]["results"] = [
        _kb_result(content="   ", title="无关条目"),
    ]
    value = evaluate_scenario_assertions(_kb_content(scenario_evidence=evidence))
    assert value.status == "value"
    assert value.typed_value is False


def test_kb_missing_document_revision_fails():
    """消费文档版本缺失：版本不可考的条目不通过（不伪造可比证据）。"""
    evidence = _kb_content()["scenario_evidence"]
    evidence["retrievals"][0]["results"] = [
        _kb_result(document_revision=None),
    ]
    value = evaluate_scenario_assertions(_kb_content(scenario_evidence=evidence))
    assert value.status == "value"
    assert value.typed_value is False


def test_legacy_kb_verified_content_snapshot_is_not_an_upstream_revision():
    import hashlib
    content = _kb_content()
    item = content["scenario_evidence"]["retrievals"][0]["results"][0]
    item["document_revision"] = None
    item["document_id"] = "d1"
    item["chunk_id"] = "c1"
    item["chunk_content_hash"] = hashlib.sha256(KB_TEXT.encode()).hexdigest()
    item["metadata"] = {"document_snapshot_source": "verified_document_content",
                        "document_snapshot_sha256": "a" * 64}
    content["scenario_evidence"]["steps"][0]["answer"] = KB_TEXT
    assert evaluate_scenario_assertions(content).typed_value is True
    item["metadata"]["document_snapshot_sha256"] = "unverified"
    assert evaluate_scenario_assertions(content).typed_value is False


def test_kb_fabricated_body_fails():
    """捏造正文：回答文字不是任何检索条目原文的连续片段 -> 不通过。"""
    fabricated = "新增预算单元请在首页右上角点击绿色按钮，输入手机号即可创建。"
    evidence = _kb_content()["scenario_evidence"]
    evidence["steps"][0]["answer"] = fabricated
    value = evaluate_scenario_assertions(_kb_content(scenario_evidence=evidence))
    assert value.status == "value"
    assert value.typed_value is False


def test_kb_title_and_link_alternative_passes():
    """原 rubric 备选路径：条目只有标题+链接时，回答包含标题与链接
    （来自真实检索结果，不编造）即满足确定性部分。"""
    answer = (
        f"未检索到具体流程正文，仅找到条目《{KB_TITLE}》：{KB_SOURCE}。"
        "以上为标题与链接，无流程正文说明。"
    )
    evidence = _kb_content()["scenario_evidence"]
    evidence["retrievals"][0]["results"] = [
        _kb_result(content=KB_TITLE, title=KB_TITLE, source=KB_SOURCE),
    ]
    evidence["steps"][0]["answer"] = answer
    value = evaluate_scenario_assertions(_kb_content(scenario_evidence=evidence))
    assert value.status == "value"
    assert value.typed_value is True


def test_kb_title_link_with_fabricated_link_fails():
    """链接不在检索结果中（编造来源）：标题+链接路径不通过。"""
    answer = f"《{KB_TITLE}》：https://evil.example.com/fake-link。无流程正文。"
    evidence = _kb_content()["scenario_evidence"]
    evidence["retrievals"][0]["results"] = [
        _kb_result(content=KB_TITLE, title=KB_TITLE, source=KB_SOURCE),
    ]
    evidence["steps"][0]["answer"] = answer
    value = evaluate_scenario_assertions(_kb_content(scenario_evidence=evidence))
    assert value.status == "value"
    assert value.typed_value is False


def test_kb_require_quote_rejects_title_link_only():
    evidence = _kb_content()["scenario_evidence"]
    evidence["retrievals"][0]["results"] = [
        _kb_result(content=KB_TITLE, title=KB_TITLE, source=KB_SOURCE),
    ]
    evidence["steps"][0]["answer"] = f"《{KB_TITLE}》 {KB_SOURCE}，无流程正文。"
    content = _kb_content(
        assertions=[
            {
                "assertion_type": "kb_source_and_quote",
                "params": {"min_results": 1, "require_quote": True},
            }
        ],
        scenario_evidence=evidence,
    )
    value = evaluate_scenario_assertions(content)
    assert value.status == "value"
    assert value.typed_value is False


def test_kb_no_quote_no_title_link_fails():
    evidence = _kb_content()["scenario_evidence"]
    evidence["steps"][0]["answer"] = "已经完成检索，请参考知识库。"
    value = evaluate_scenario_assertions(_kb_content(scenario_evidence=evidence))
    assert value.status == "value"
    assert value.typed_value is False


def test_kb_cross_run_retrieval_not_counted():
    evidence = _kb_content()["scenario_evidence"]
    evidence["retrievals"][0]["run_id"] = "run-other"
    value = evaluate_scenario_assertions(_kb_content(scenario_evidence=evidence))
    assert value.status == "value"
    assert value.typed_value is False


def test_kb_short_paraphrase_is_not_a_quote():
    """低于最小引用长度的转述不算原文引用（防碎片匹配）。"""
    evidence = _kb_content()["scenario_evidence"]
    evidence["steps"][0]["answer"] = "入口位于设置页。"
    content = _kb_content(
        assertions=[
            {
                "assertion_type": "kb_source_and_quote",
                "params": {"min_results": 1, "require_quote": True},
            }
        ],
        scenario_evidence=evidence,
    )
    value = evaluate_scenario_assertions(content)
    assert value.status == "value"
    assert value.typed_value is False


# ---------------------------------------------------------------------------
# 禁止 web/browser 工具（正式端口证据）
# ---------------------------------------------------------------------------


def test_web_tool_presence_fails_forbidden_assertion():
    """R2 纪律：rag-nkb 场景禁止 web_fetch/browser 工具；正式端口出现
    web_fetch 记录即失败（kb 断言本身通过也不救）。"""
    evidence = _kb_content()["scenario_evidence"]
    evidence["steps"][0]["answer"] = f"原文：{KB_TEXT}"
    evidence["tool_calls"] = [
        {
            "run_id": "run-a",
            "tool_name": "search_knowledge",
            "status": "success",
            "result": "see retrievals",
        },
        {
            "run_id": "run-a",
            "tool_name": "web_fetch",
            "status": "success",
            "result": "<html>leaked</html>",
        },
    ]
    content = _kb_content(
        assertions=[
            {
                "assertion_type": "kb_source_and_quote",
                "params": {"min_results": 1, "require_quote": False},
            },
            {
                "assertion_type": "tool_absent",
                "step_id": "turn-1",
                "params": {"tool": "web_fetch"},
            },
            {
                "assertion_type": "tool_absent",
                "step_id": "turn-1",
                "params": {"tool": "browser_navigate"},
            },
        ],
        scenario_evidence=evidence,
    )
    value = evaluate_scenario_assertions(content)
    assert value.status == "value"
    assert value.typed_value is False


# ---------------------------------------------------------------------------
# 适配器：检索证据从正式端口 search_knowledge 记录派生（不合成）
# ---------------------------------------------------------------------------


def _sse(chunks: list[dict], *, terminated: bool = True) -> bytes:
    lines = [f"data: {json.dumps(c, ensure_ascii=False)}" for c in chunks]
    if terminated:
        lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode()


def _sse_chunks():
    return [
        {"choices": [{"index": 0, "delta": {"content": "原文引用完成"},
                      "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "run_id": "run-kb"},
    ]


async def test_adapter_collects_retrieval_evidence_from_tool_port():
    from app.infrastructure.evaluation.scenario_adapters import (
        ScenarioDashboardSSERunner,
    )

    body = _sse(_sse_chunks())

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/chat/sessions":
            return httpx.Response(200)
        return httpx.Response(200, content=body)

    async def tool_reader(session_id):
        return [
            {
                "id": "call-kb-1",
                "run_id": "run-kb",
                "tool_name": "search_knowledge",
                "status": "success",
                "result": {
                    "site": "N-KB",
                    "query": "新增预算单元",
                    "results": [_kb_result()],
                },
            },
            {
                "id": "call-other",
                "run_id": "run-kb",
                "tool_name": "web_fetch",
                "status": "success",
                "result": "<html>weather</html>",
            },
        ]

    runner = ScenarioDashboardSSERunner(
        base_url="http://agent.test",
        model="target-model",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        tool_evidence_reader=tool_reader,
    )
    try:
        result = await runner.run_target(
            type("T", (), {"kind": "agent_run"})(),
            {
                "session": {"namespace": "ns", "session_id": "sess-kb"},
                "turns": [{"step_id": "turn-1", "message": "检索新增预单"}],
            },
        )
    finally:
        await runner.aclose()
    assert result["status"] == "succeeded"
    evidence = result["output"]["scenario_evidence"]
    assert evidence["status"] == "complete"
    retrievals = evidence["retrievals"]
    assert len(retrievals) == 1
    entry = retrievals[0]
    assert entry["backend"] == "n_kb"
    assert entry["run_id"] == "run-kb"
    assert entry["retrieval_id"] == "call-kb-1"
    assert entry["results"] == [_kb_result()]
    # web_fetch 记录不进入检索证据（仍留在 tool_calls 供 tool_absent 判定）
    assert evidence["tool_calls"][1]["tool_name"] == "web_fetch"


async def test_adapter_failed_kb_tool_yields_no_retrieval_entry():
    from app.infrastructure.evaluation.scenario_adapters import (
        ScenarioDashboardSSERunner,
    )

    body = _sse(_sse_chunks())

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/chat/sessions":
            return httpx.Response(200)
        return httpx.Response(200, content=body)

    async def tool_reader(session_id):
        return [
            {
                "id": "call-kb-err",
                "run_id": "run-kb",
                "tool_name": "search_knowledge",
                "status": "error",
                "result": {"error": "knowledge search failed"},
            },
        ]

    runner = ScenarioDashboardSSERunner(
        base_url="http://agent.test",
        model="target-model",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        tool_evidence_reader=tool_reader,
    )
    try:
        result = await runner.run_target(
            type("T", (), {"kind": "agent_run"})(),
            {
                "session": {"namespace": "ns", "session_id": "sess-kb"},
                "turns": [{"step_id": "turn-1", "message": "检索"}],
            },
        )
    finally:
        await runner.aclose()
    assert result["status"] == "succeeded"
    assert result["output"]["scenario_evidence"]["retrievals"] == []


def test_main_session_prepare_offloads_blocking_binder_to_thread():
    """组合根契约：binder.prepare 是阻塞调用（mem0 索引轮询 time.sleep），
    事件循环内直接调用会让执行单心跳停摆（T8 S4 实测 lease 过期中断）。
    _scenario_session_prepare 必须经 asyncio.to_thread 离循环执行。"""
    from pathlib import Path as _Path

    source = (
        _Path(__file__).resolve().parents[2] / "app" / "main.py"
    ).read_text(encoding="utf-8")
    start = source.index("async def _scenario_prepare_memory")
    body = source[start : start + 4000]
    assert "asyncio.to_thread" in body
    assert "asyncio.to_thread(binder.prepare" in body


async def test_session_prepare_blocked_is_business_error_not_transport():
    """场景记忆准备 blocked（ScenarioMemoryError）是业务失败而非传输
    失败：不重试（R2 S4 实测 mem0 索引延迟曾被误判 transport_exhausted）。"""
    from app.infrastructure.evaluation.scenario_adapters import (
        ScenarioDashboardSSERunner,
    )
    from app.infrastructure.evaluation.scenario_memory import ScenarioMemoryError

    body = _sse(_sse_chunks())

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/chat/sessions":
            return httpx.Response(200)
        return httpx.Response(200, content=body)

    async def prepare(session_id, alias, context):
        raise ScenarioMemoryError("mem0 预置事实不可检索，绑定 blocked")

    runner = ScenarioDashboardSSERunner(
        base_url="http://agent.test",
        model="target-model",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        session_prepare=prepare,
    )
    try:
        result = await runner.run_target(
            type("T", (), {"kind": "agent_run"})(),
            {
                "session": {"namespace": "ns", "session_id": "sess-mem0"},
                "turns": [{"step_id": "turn-1", "message": "我喜欢吃什么水果"}],
                "scenario_options": {"external_memory_enabled": "mem0-e2e-test"},
            },
        )
    finally:
        await runner.aclose()
    assert result["status"] == "business_error"
    assert result["error_type"] == "scenario_memory_unavailable"


async def test_adapter_parses_serialized_tool_result_wrapper():
    """正式端口实测形态：result 是序列化 JSON 字符串，负载在 content 下，
    携带 kb_id/backend_type（Legacy N-KB 实测记录形状）。"""
    from app.infrastructure.evaluation.scenario_adapters import (
        ScenarioDashboardSSERunner,
    )

    body = _sse(_sse_chunks())
    record = {
        "id": "call-serialized",
        "run_id": "run-kb",
        "tool_name": "search_knowledge",
        "status": "success",
        "result": json.dumps(
            {
                "tool_call_id": "call-serialized",
                "name": "search_knowledge",
                "status": "success",
                "content": {
                    "kb_id": "legacy-n-kb",
                    "kb_name": "Legacy N-KB",
                    "backend_type": "n_kb",
                    "query": "新增预算单元 新增预单",
                    "results": [
                        {
                            "id": "chunk-0",
                            "title": None,
                            "content": "# 流程常见问题 新增预单",
                            "score": 0.9,
                            "source": "流程常见问题.md",
                            "metadata": {},
                        }
                    ],
                },
                "duration_ms": 12,
            },
            ensure_ascii=False,
        ),
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/chat/sessions":
            return httpx.Response(200)
        return httpx.Response(200, content=body)

    async def tool_reader(session_id):
        return [record]

    runner = ScenarioDashboardSSERunner(
        base_url="http://agent.test",
        model="target-model",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        tool_evidence_reader=tool_reader,
    )
    try:
        result = await runner.run_target(
            type("T", (), {"kind": "agent_run"})(),
            {
                "session": {"namespace": "ns", "session_id": "sess-kb"},
                "turns": [{"step_id": "turn-1", "message": "检索"}],
            },
        )
    finally:
        await runner.aclose()
    assert result["status"] == "succeeded"
    retrievals = result["output"]["scenario_evidence"]["retrievals"]
    assert len(retrievals) == 1
    entry = retrievals[0]
    assert entry["backend"] == "n_kb"
    assert entry["kb_id"] == "legacy-n-kb"
    assert entry["query"] == "新增预算单元 新增预单"
    assert entry["results"][0]["content"] == "# 流程常见问题 新增预单"


# ---------------------------------------------------------------------------
# 比较分组（scope/profile/fixture/binding；派生单列；非场景给出原因）
# ---------------------------------------------------------------------------


def _scenario_case(case_id, *, scope="fixed_fixture", profile="dashboard",
                   fixture=None, bindings=None, derived_from=None):
    source = {
        "repo_path": "tests/e2e/conversations/x.json",
        "source_case_id": case_id,
        "source_sha256": "a" * 64,
        "rules_version": "e2e-conversion-v1",
    }
    if derived_from is not None:
        source["derived_from"] = derived_from
    scenario = {
        "execution": {
            "profile": profile,
            "steps": [
                {"step_id": "turn-1", "action": "chat", "payload": {"message": "hi"}},
            ],
            "timeout_seconds": 300,
        },
        "requirements": {},
        "options": {},
        "assertions": [
            {"assertion_type": "result_contains", "params": {"text": "hi"}},
        ],
        "expected_output": {"steps": {"turn-1": {"rubric": "回答"}}},
        "bindings": bindings or {},
        "lifecycle": {
            "release_rule": "release_active_resources",
            "retention_rule": "retain_evidence_default",
        },
        "required_metrics": {"回答": "agent.scenario_quality"},
        "source": source,
        "comparison_scope": scope,
    }
    case = {
        "case_id": case_id,
        "input": {"message": "hi"},
        "target_kind": "agent_run",
        "expected_output": {"steps": {"turn-1": {"rubric": "回答"}}},
        "scenario": scenario,
    }
    if fixture is not None:
        case["fixture"] = fixture
        case["fixture_sha256"] = "b" * 64
    return case


def _binding(alias, material="m1"):
    return {alias: {"material": material, "sha256": "c" * 64}}


def test_comparison_groups_split_by_scope():
    from app.application.evaluation_analytics import (
        build_scenario_comparison_groups,
    )

    cases = [
        _scenario_case("fixed-a"),
        _scenario_case("live-a", scope="live_smoke"),
    ]
    groups = build_scenario_comparison_groups(cases)
    scopes = {group["scope"] for group in groups["groups"]}
    assert scopes == {"fixed_fixture", "live_smoke"}
    live = next(g for g in groups["groups"] if g["scope"] == "live_smoke")
    assert live["case_ids"] == ["live-a"]
    assert live["note"]
    assert groups["reasons"]


def test_comparison_groups_split_by_profile_and_fixture_and_binding():
    from app.application.evaluation_analytics import (
        build_scenario_comparison_groups,
    )

    cases = [
        _scenario_case("dash-a", profile="dashboard"),
        _scenario_case("agent-a", profile="agent"),
        _scenario_case("fx-a", fixture="assets/a.png"),
        _scenario_case("bind-a", bindings=_binding("file_1")),
        _scenario_case("bind-b", bindings=_binding("file_1", material="m2")),
    ]
    groups = build_scenario_comparison_groups(cases)
    keys = {
        (group["scope"], group["profile"], group["fixture"],
         tuple(group["bindings"]))
        for group in groups["groups"]
    }
    assert len(keys) == len(groups["groups"]) == 5


def test_derived_case_annotated_and_separate():
    from app.application.evaluation_analytics import (
        build_scenario_comparison_groups,
    )

    cases = [
        _scenario_case("rag-nkb-chat", scope="live_smoke"),
        _scenario_case(
            "rag-nkb-chat-fixed",
            scope="fixed_fixture",
            derived_from="rag-nkb-chat",
        ),
    ]
    groups = build_scenario_comparison_groups(cases)
    derived = next(
        g for g in groups["groups"]
        if "rag-nkb-chat-fixed" in g["case_ids"]
    )
    assert "rag-nkb-chat-fixed" in derived["derived_case_ids"]
    # 派生题不与源题同组（scope 不同即分组），也不顶替源题
    source_group = next(
        g for g in groups["groups"] if "rag-nkb-chat" in g["case_ids"]
    )
    assert source_group["case_ids"] == ["rag-nkb-chat"]
    assert "rag-nkb-chat-fixed" not in source_group["case_ids"]


def test_non_scenario_cases_grouped_with_reason():
    from app.application.evaluation_analytics import (
        build_scenario_comparison_groups,
    )

    cases = [
        _scenario_case("fixed-a"),
        {
            "case_id": "legacy-a",
            "input": {"message": "hi"},
            "target_kind": "agent_run",
            "expected_output": "hi",
        },
    ]
    groups = build_scenario_comparison_groups(cases)
    legacy = next(
        g for g in groups["groups"] if "legacy-a" in g["case_ids"]
    )
    assert legacy["scope"] is None
    assert legacy["note"]


# ---------------------------------------------------------------------------
# 执行详情：比较分组随 GET /chat/evaluations/executions/{id} 返回
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("selected", [None, ["fixed-a"]])
async def test_execution_get_attaches_comparison_groups(selected):
    from app.application.evaluation_execution import EvaluationExecutionService

    manifest_cases = [
        _scenario_case("fixed-a"),
        _scenario_case("live-a", scope="live_smoke"),
    ]

    class _Store:
        async def get(self, execution_id):
            return {
                "execution_id": execution_id,
                "experiment_id": None,
                "phase": "completed",
                "report": {},
                "payload": {"manifest": {"cases": manifest_cases}, "selected_case_ids": selected},
            }

    class _Ledger:
        async def experiment_job_status_counts(self, experiment_id):
            return {}

    service = EvaluationExecutionService(
        store=_Store(), ledger=_Ledger(), review=None, runner=None,
        readiness=None,
    )
    result = await service.get("exec-1")
    groups = result["comparison_groups"]
    scopes = {group["scope"] for group in groups["groups"]}
    assert scopes == ({"fixed_fixture", "live_smoke"} if selected is None else {"fixed_fixture"})


# ---------------------------------------------------------------------------
# 转换器：rag-nkb-chat 声明 kb 断言与禁止工具
# ---------------------------------------------------------------------------


def test_rag_nkb_case_declares_kb_assertion_and_forbidden_tools():
    result = convert_e2e_conversations(
        SOURCE_DIR_RELPATH, case_ids=("rag-nkb-chat",)
    )
    case = next(
        c for c in result.manifest["cases"] if c["case_id"] == "rag-nkb-chat"
    )
    assertion_types = {
        item["assertion_type"] for item in case["scenario"]["assertions"]
    }
    assert "kb_source_and_quote" in assertion_types
    kb = next(
        item for item in case["scenario"]["assertions"]
        if item["assertion_type"] == "kb_source_and_quote"
    )
    assert kb["params"]["min_results"] >= 1
    assert kb["params"]["require_quote"] is False
    forbidden = {
        item["params"]["tool"]
        for item in case["scenario"]["assertions"]
        if item["assertion_type"] == "tool_absent"
    }
    assert {"web_fetch", "browser_navigate", "browser_click"} <= forbidden
    # 必需工具与证据能力照旧声明
    assert "search_knowledge" in case["scenario"]["requirements"]["required_tools"]

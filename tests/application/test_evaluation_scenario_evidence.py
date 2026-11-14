"""T3：场景证据判定与确定性指标处理器测试。

覆盖 spec-261114-expand-e2e-experiments 5.2/5.3：
- complete/incomplete/unavailable 证据集合三态语义；
- 工具 error 包装、空结果、禁止工具出现、跨 run 混证据、首轮失败末轮
  成功等反例不得通过；
- 读取失败/截断按 evidence_missing，不可当作空集合或通过；
- 完整空集合（确认未调用）与不可用集合语义分离。
"""
from __future__ import annotations

import pytest

from app.application.evaluation_scenario_assertions import (
    evaluate_scenario_assertions,
)
from app.domain.evaluation.state import SkipReason


def _content(**overrides):
    base = {
        "assertions": [
            {
                "assertion_type": "tool_succeeded",
                "step_id": "turn-1",
                "params": {"tool": "web_fetch"},
            },
        ],
        "scenario_evidence": {
            "status": "complete",
            "steps": [
                {"step_id": "turn-1", "run_id": "run-a", "answer": "北京 晴"},
            ],
            "tool_calls": [
                {
                    "step_id": "turn-1",
                    "run_id": "run-a",
                    "tool_name": "web_fetch",
                    "status": "success",
                    "result": "<html>weather</html>",
                },
            ],
        },
    }
    base.update(overrides)
    return base


def test_happy_path_all_assertions_pass():
    value = evaluate_scenario_assertions(_content())
    assert value.status == "value"
    assert value.typed_value is True


def test_formal_port_tool_name_field_is_matched():
    """正式端口记录字段是 tool_name（dashboard _tool_call_to_dict 契约）：
    证据记录按 tool_name 匹配断言（_content 夹具即端口形状）。"""
    value = evaluate_scenario_assertions(_content())
    assert value.status == "value" and value.typed_value is True


def test_record_without_tool_name_does_not_match_assertion():
    """工具名缺失的记录不匹配任何断言（fail-closed，不猜测）。"""
    content = _content()
    del content["scenario_evidence"]["tool_calls"][0]["tool_name"]
    value = evaluate_scenario_assertions(content)
    assert value.status == "value" and value.typed_value is False


def test_tool_error_wrapped_not_success():
    content = _content()
    content["scenario_evidence"]["tool_calls"][0]["status"] = "error"
    value = evaluate_scenario_assertions(content)
    assert value.status == "value" and value.typed_value is False


def test_success_with_empty_result_not_success():
    content = _content()
    content["scenario_evidence"]["tool_calls"][0]["result"] = ""
    value = evaluate_scenario_assertions(content)
    assert value.typed_value is False


def test_forbidden_tool_presence_fails():
    content = _content(assertions=[
        {"assertion_type": "tool_absent", "step_id": "turn-1",
         "params": {"tool": "web_fetch"}},
    ])
    value = evaluate_scenario_assertions(content)
    assert value.typed_value is False


def test_cross_run_tool_evidence_not_mixed():
    # 工具调用 run_id 不属于任何步骤 run_id：不得充当本 case 的证据
    content = _content()
    content["scenario_evidence"]["tool_calls"][0]["run_id"] = "run-OTHER"
    value = evaluate_scenario_assertions(content)
    assert value.typed_value is False


def test_first_turn_failure_not_offset_by_second_turn():
    content = _content(assertions=[
        {"assertion_type": "tool_succeeded", "step_id": "turn-1",
         "params": {"tool": "browser_navigate"}},
        {"assertion_type": "tool_succeeded", "step_id": "turn-2",
         "params": {"tool": "browser_close"}},
    ])
    content["scenario_evidence"]["steps"] = [
        {"step_id": "turn-1", "run_id": "run-a", "answer": "opened"},
        {"step_id": "turn-2", "run_id": "run-b", "answer": "closed"},
    ]
    content["scenario_evidence"]["tool_calls"] = [
        {"step_id": "turn-1", "run_id": "run-a", "tool_name": "browser_navigate",
         "status": "error", "result": ""},
        {"step_id": "turn-2", "run_id": "run-b", "tool_name": "browser_close",
         "status": "success", "result": "closed"},
    ]
    value = evaluate_scenario_assertions(content)
    assert value.typed_value is False


def test_unavailable_evidence_is_skipped_not_pass():
    content = _content()
    content["scenario_evidence"]["status"] = "unavailable"
    content["scenario_evidence"]["reason"] = "snapshot read failed"
    content["scenario_evidence"]["tool_calls"] = []
    value = evaluate_scenario_assertions(content)
    assert value.status == "skipped"
    assert value.skip_reason == SkipReason.evidence_missing


def test_incomplete_evidence_cannot_confirm_absence():
    content = _content(assertions=[
        {"assertion_type": "tool_absent", "step_id": "turn-1",
         "params": {"tool": "web_fetch"}},
    ])
    content["scenario_evidence"]["status"] = "incomplete"
    content["scenario_evidence"]["reason"] = "truncated tool log"
    content["scenario_evidence"]["tool_calls"] = []
    value = evaluate_scenario_assertions(content)
    assert value.status == "skipped"
    assert value.skip_reason == SkipReason.evidence_missing


def test_complete_empty_set_confirms_absence():
    content = _content(assertions=[
        {"assertion_type": "tool_absent", "step_id": "turn-1",
         "params": {"tool": "web_fetch"}},
    ])
    content["scenario_evidence"]["tool_calls"] = []
    value = evaluate_scenario_assertions(content)
    assert value.typed_value is True


def test_missing_evidence_block_skipped():
    content = _content()
    content.pop("scenario_evidence")
    value = evaluate_scenario_assertions(content)
    assert value.status == "skipped"
    assert value.skip_reason == SkipReason.evidence_missing


def test_missing_assertions_block_skipped():
    content = _content()
    content.pop("assertions")
    value = evaluate_scenario_assertions(content)
    assert value.status == "skipped"


def test_result_contains_matches_step_answer():
    content = _content(assertions=[
        {"assertion_type": "result_contains", "step_id": "turn-1",
         "params": {"text": "晴"}},
    ])
    assert evaluate_scenario_assertions(content).typed_value is True
    content["scenario_evidence"]["steps"][0]["answer"] = "多云"
    assert evaluate_scenario_assertions(content).typed_value is False


def test_task_terminal_and_artifact_and_file_assertions():
    content = _content(assertions=[
        {"assertion_type": "task_terminal", "step_id": None,
         "params": {"status": "succeeded", "within_seconds": 600}},
        {"assertion_type": "artifact_set", "step_id": None,
         "params": {"min_count": 2}},
        {"assertion_type": "file_content", "step_id": None,
         "params": {"filename": "task-output-a.txt", "contains": "artifact a"}},
    ])
    content["scenario_evidence"]["tasks"] = [
        {"task_id": "t-1", "status": "succeeded", "elapsed_seconds": 120},
    ]
    content["scenario_evidence"]["artifacts"] = [
        {"artifact_id": "a-1"}, {"artifact_id": "a-2"},
    ]
    content["scenario_evidence"]["files"] = [
        {"filename": "task-output-a.txt", "content": "artifact a"},
    ]
    assert evaluate_scenario_assertions(content).typed_value is True

    content["scenario_evidence"]["tasks"][0]["status"] = "failed"
    assert evaluate_scenario_assertions(content).typed_value is False


def test_task_timeout_exceeds_deadline_fails():
    content = _content(assertions=[
        {"assertion_type": "task_terminal", "step_id": None,
         "params": {"status": "succeeded", "within_seconds": 600}},
    ])
    content["scenario_evidence"]["tasks"] = [
        {"task_id": "t-1", "status": "succeeded", "elapsed_seconds": 601},
    ]
    assert evaluate_scenario_assertions(content).typed_value is False


def test_file_hash_change_read_failure_is_incomplete():
    content = _content(assertions=[
        {"assertion_type": "file_content", "step_id": None,
         "params": {"filename": "f.txt", "contains": "x"}},
    ])
    content["scenario_evidence"]["status"] = "incomplete"
    content["scenario_evidence"]["reason"] = "file read failed"
    content["scenario_evidence"]["files"] = []
    value = evaluate_scenario_assertions(content)
    assert value.status == "skipped"


def test_browser_state_closed_or_absent_normalized():
    content = _content(assertions=[
        {"assertion_type": "browser_state", "step_id": None,
         "params": {"state": "closed"}},
    ])
    content["scenario_evidence"]["browser"] = [{"state": "closed"}]
    assert evaluate_scenario_assertions(content).typed_value is True
    content["scenario_evidence"]["browser"] = [{"state": "absent"}]
    assert evaluate_scenario_assertions(content).typed_value is True
    content["scenario_evidence"]["browser"] = [{"state": "active"}]
    assert evaluate_scenario_assertions(content).typed_value is False


def test_protocol_sequence_order_enforced():
    content = _content(assertions=[
        {"assertion_type": "protocol_sequence", "step_id": None,
         "params": {"entries": ["initialize", "authenticate",
                                "session/new", "session/prompt"]}},
    ])
    content["scenario_evidence"]["protocol"] = [
        "initialize", "authenticate", "session/new", "session/prompt",
    ]
    assert evaluate_scenario_assertions(content).typed_value is True
    # 顺序错误（authenticate 缺失）不得通过
    content["scenario_evidence"]["protocol"] = [
        "initialize", "session/new", "session/prompt",
    ]
    assert evaluate_scenario_assertions(content).typed_value is False


def test_kb_source_and_quote_deterministic_part():
    """T8 收紧后的 kb 断言：n_kb 来源 + 可消费条目（正文非空且带
    document_revision）+ 回答引用真实检索原文；错误 backend 不通过。"""
    kb_text = "新增预算单元的入口位于设置页，进入预算中心后点击新增预算单元即可创建。"
    content = _content(
        assertions=[
            {"assertion_type": "kb_source_and_quote", "step_id": "turn-1",
             "params": {"kb_id": "kb-1", "min_results": 1}},
        ],
    )
    content["scenario_evidence"]["steps"][0]["answer"] = f"原文：{kb_text}"
    content["scenario_evidence"]["retrievals"] = [
        {
            "step_id": "turn-1",
            "run_id": "run-a",
            "tool_name": "search_knowledge",
            "backend": "n_kb",
            "kb_id": "kb-1",
            "results": [{
                "title": "新增预算单元",
                "content": kb_text,
                "document_revision": "rev-1",
            }],
        },
    ]
    assert evaluate_scenario_assertions(content).typed_value is True
    content["scenario_evidence"]["retrievals"][0]["backend"] = "ragflow"
    assert evaluate_scenario_assertions(content).typed_value is False

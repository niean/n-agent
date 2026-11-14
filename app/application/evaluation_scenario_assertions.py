"""场景断言确定性判定器（plan T3，spec-261114-expand-e2e-experiments 5.2/5.3）。

``agent.scenario_assertions`` 指标的内容构建器：对快照 content 中的
``scenario_evidence`` 逐条执行 case 声明的结构化断言，输出统一
EvaluationValue（boolean）。

证据纪律（spec 5.2，全部硬约束）：
- 证据集合三态：complete / incomplete / unavailable；
- 读取失败、截断、producer 声明缺口 -> skipped(evidence_missing)，
  不当作空集合，更不当作通过；
- complete 空集合是"确认未发生"，可确认 tool_absent；incomplete 不行；
- 工具成功 = status == "success" 且 result 非空（error 包装、空结果、
  纯 whitespace 都不算成功）；
- 跨 run 混证据禁止：tool/retrieval 记录的 run_id 必须属于所引用步骤
  的 run_id 集合，否则该记录不充当证据（断言按无证据失败/跳过）；
- 任一断言失败即整体 False；全部通过才 True；无断言 not_applicable。
"""
from __future__ import annotations

from pathlib import Path
import hashlib
from typing import Any, Mapping

from app.domain.evaluation.models import EvaluationValue
from app.domain.evaluation.state import SkipReason


def _skip(reason: SkipReason, explanation: str) -> EvaluationValue:
    return EvaluationValue(
        typed_value=None,
        status="skipped",
        skip_reason=reason,
        explanation=explanation,
    )


def _as_list(raw: Any) -> list:
    return raw if isinstance(raw, list) else []


def _step_run_ids(evidence: Mapping[str, Any]) -> dict[str, set[str]]:
    """step_id -> 该步骤归属 run_id 集合（跨 run 证据过滤的权威索引）。"""
    mapping: dict[str, set[str]] = {}
    for step in _as_list(evidence.get("steps")):
        if not isinstance(step, Mapping):
            continue
        step_id = step.get("step_id")
        run_id = step.get("run_id")
        if isinstance(step_id, str) and isinstance(run_id, str) and run_id:
            mapping.setdefault(step_id, set()).add(run_id)
    return mapping


def _run_id_allowed(
    record: Mapping[str, Any], step_id: str | None, run_ids: dict[str, set[str]]
) -> bool:
    """记录 run_id 必须属于所引用步骤（或任一步骤）的 run_id 集合。"""
    record_run = record.get("run_id")
    if not isinstance(record_run, str) or not record_run:
        return False
    if step_id is not None:
        return record_run in run_ids.get(step_id, set())
    return any(record_run in ids for ids in run_ids.values())


def _text_contains(haystack: Any, needle: str) -> bool:
    return isinstance(haystack, str) and needle in haystack


def _check_tool_succeeded(
    params: Mapping[str, Any],
    step_id: str | None,
    evidence: Mapping[str, Any],
    run_ids: dict[str, set[str]],
) -> bool:
    tool = params.get("tool")
    for call in _as_list(evidence.get("tool_calls")):
        if not isinstance(call, Mapping):
            continue
        # 正式端口记录字段是 tool_name（dashboard _tool_call_to_dict 契约）；
        # 工具名缺失的记录不匹配任何断言（fail-closed，不猜测）
        if call.get("tool_name") != tool:
            continue
        if not _run_id_allowed(call, step_id, run_ids):
            continue
        if call.get("status") == "success" and call.get("result"):
            return True
    return False


def _check_tool_absent(
    params: Mapping[str, Any],
    step_id: str | None,
    evidence: Mapping[str, Any],
    run_ids: dict[str, set[str]],
) -> bool:
    tool = params.get("tool")
    for call in _as_list(evidence.get("tool_calls")):
        if not isinstance(call, Mapping):
            continue
        if call.get("tool_name") != tool:
            continue
        if not _run_id_allowed(call, step_id, run_ids):
            continue
        return False
    return True


def _check_result_contains(
    params: Mapping[str, Any],
    step_id: str | None,
    evidence: Mapping[str, Any],
    run_ids: dict[str, set[str]],
) -> bool:
    text = params.get("text")
    for step in _as_list(evidence.get("steps")):
        if not isinstance(step, Mapping):
            continue
        if step_id is not None and step.get("step_id") != step_id:
            continue
        if _text_contains(step.get("answer"), text):
            return True
    return False


def _check_task_terminal(
    params: Mapping[str, Any],
    step_id: str | None,
    evidence: Mapping[str, Any],
    run_ids: dict[str, set[str]],
) -> bool:
    expected_status = params.get("status")
    within = params.get("within_seconds")
    tasks = _as_list(evidence.get("tasks"))
    if not tasks:
        return False
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        if task.get("status") != expected_status:
            return False
        if within is not None:
            elapsed = task.get("elapsed_seconds")
            if not isinstance(elapsed, (int, float)) or elapsed > within:
                return False
    return True


def _check_artifact_set(
    params: Mapping[str, Any],
    step_id: str | None,
    evidence: Mapping[str, Any],
    run_ids: dict[str, set[str]],
) -> bool:
    min_count = params.get("min_count")
    min_count = int(min_count) if isinstance(min_count, (int, float)) else 1
    artifacts = [
        item
        for item in _as_list(evidence.get("artifacts"))
        if isinstance(item, Mapping)
    ]
    if len(artifacts) < min_count:
        return False
    # T10：storage_ref 实际核对（受控内部端口回读，不复制 E2E 的仅
    # basename 兜底）——前缀必须匹配，且 storage_ref 基名集合与声明的
    # workspace 文件基名一致（不同子目录同名文件不塌缩）
    prefix = params.get("storage_ref_prefix")
    if prefix is None:
        return True
    declared = [
        str(path)
        for path in _as_list(params.get("workspace_files"))
        if isinstance(path, (str, Path))
    ] if isinstance(params.get("workspace_files"), list) else []
    if not declared:
        # 声明核对目标却无法核对：fail-closed
        return False
    declared_names = sorted({str(path).rsplit("/", 1)[-1] for path in declared})
    observed_names: list[str] = []
    for item in artifacts:
        storage_ref = item.get("storage_ref")
        if not isinstance(storage_ref, str) or not storage_ref.startswith(
            str(prefix)
        ):
            return False
        # 实际 ref 形如 "workspace:<rel_path>"（冒号分隔，可能无斜杠）：
        # 先剥前缀再取末段基名（T10 S4 实测：直接按 "/" 切会把
        # "workspace:" 泄进基名集合导致全匹配也判负）
        tail = storage_ref[len(str(prefix)):].rstrip("/")
        observed_names.append(tail.rsplit("/", 1)[-1])
    # 集合对集合比较（T10 S4 实测：sorted(set(...))==set(...) 是列表对
    # 集合，恒 False，全匹配也判负）
    return set(observed_names) == set(declared_names)


def _check_file_content(
    params: Mapping[str, Any],
    step_id: str | None,
    evidence: Mapping[str, Any],
    run_ids: dict[str, set[str]],
) -> bool:
    filename = params.get("filename")
    contains = params.get("contains")
    for file in _as_list(evidence.get("files")):
        if not isinstance(file, Mapping):
            continue
        if file.get("filename") != filename:
            continue
        # 文件名命中但读取内容为 None：读取失败按失败处理（不当作通过）
        if contains is None:
            return file.get("content") is not None
        return _text_contains(file.get("content"), contains)
    return False


def _check_browser_state(
    params: Mapping[str, Any],
    step_id: str | None,
    evidence: Mapping[str, Any],
    run_ids: dict[str, set[str]],
) -> bool:
    expected = params.get("state")
    records = [
        record
        for record in _as_list(evidence.get("browser"))
        if isinstance(record, Mapping)
    ]
    for record in records:
        state = record.get("state")
        if state == expected:
            return True
        # closed/absent 归一化：两者语义等价（无活跃浏览器会话）
        if expected in ("closed", "absent") and state in (
            "closed",
            "absent",
        ):
            return True
    # 确认不存在：成功的空集合（closed/absent 均规范化；T9）。
    # unknown 记录（读取失败）使 records 非空，不会走到这里——
    # 读取失败不当不存在
    return not records and expected in ("closed", "absent")


def _check_protocol_sequence(
    params: Mapping[str, Any],
    step_id: str | None,
    evidence: Mapping[str, Any],
    run_ids: dict[str, set[str]],
) -> bool:
    entries = params.get("entries")
    if not isinstance(entries, list) or not entries:
        return False
    observed = [
        e for e in _as_list(evidence.get("protocol")) if isinstance(e, str)
    ]
    # 有序子序列匹配：允许夹带额外事件，但声明序列必须按顺序全部出现
    cursor = 0
    for item in observed:
        if cursor < len(entries) and item == entries[cursor]:
            cursor += 1
    return cursor == len(entries)


# kb_source_and_quote 判定的最小原文引用长度（低于该长度的转述不算
# 原样引用，防碎片匹配伪装成引用）
KB_QUOTE_MIN_LENGTH = 12

# KnowledgeSnippet 正文候选键（与 SPI _map_nkb_result 同序：content >
# snippet > text；tool 端透传的是后端原始条目）
_KB_CONTENT_KEYS = ("content", "snippet", "text")


def _kb_text(item: Any, *keys: str) -> str:
    if not isinstance(item, Mapping):
        return ""
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _kb_consumable(item: Any) -> bool:
    """检索条目可消费：正文非空且消费文档版本可考（缺版本不充当证据）。"""
    text = _kb_text(item, *_KB_CONTENT_KEYS)
    if not text:
        return False
    if _kb_text(item, "document_revision"):
        return True
    # Legacy live retrieval can be anchored to an independently verified source
    # snapshot. Keep that hash separate from upstream revision identity; deletion
    # of ANY upstream version conservatively revokes an unversioned dependency.
    metadata = item.get("metadata") if isinstance(item, Mapping) else None
    if not isinstance(metadata, Mapping):
        return False
    digest = metadata.get("document_snapshot_sha256")
    return (metadata.get("document_snapshot_source") == "verified_document_content"
            and isinstance(digest, str) and len(digest) == 64
            and all(c in "0123456789abcdef" for c in digest)
            and bool(_kb_text(item, "document_id")) and bool(_kb_text(item, "chunk_id"))
            and item.get("chunk_content_hash") == hashlib.sha256(text.encode("utf-8")).hexdigest())


def _answer_for(evidence: Mapping[str, Any], step_id: str | None) -> str:
    steps = [
        step
        for step in _as_list(evidence.get("steps"))
        if isinstance(step, Mapping)
    ]
    if step_id is not None:
        for step in steps:
            if step.get("step_id") == step_id:
                return step.get("answer") if isinstance(step.get("answer"), str) else ""
        return ""
    if not steps:
        return ""
    last = steps[-1].get("answer")
    return last if isinstance(last, str) else ""


def _check_kb_source_and_quote(
    params: Mapping[str, Any],
    step_id: str | None,
    evidence: Mapping[str, Any],
    run_ids: dict[str, set[str]],
) -> bool:
    """n_kb 来源 + 非空可消费条目 + 引用纪律（plan T8，spec R2 行）。

    判定链（全部 fail-closed）：
    - 只接受 backend == "n_kb" 的检索记录，且 run_id/step 归属校验同
      其他证据（跨 run 混证据禁止）；
    - 条目可消费：正文非空（无关空结果不算）且 document_revision 非空
      （消费文档版本缺失不充当证据）；可消费条目数 >= min_results；
    - 回答引用纪律（按原 rubric 两条路径）：
      a) 原文引用：回答包含某可消费条目正文的连续片段（长度 >=
         KB_QUOTE_MIN_LENGTH）——引用必须来自真实检索原文，捏造正文
         不通过；
      b) 标题+链接（仅 require_quote=False）：回答同时包含某可消费
         条目的 title 与 source/url（标题与链接都必须来自检索结果，
         编造来源不通过）；
    - require_quote=True 时仅接受路径 a。
    """
    kb_id = params.get("kb_id")
    min_results = params.get("min_results")
    min_results = int(min_results) if isinstance(min_results, (int, float)) else 1
    require_quote = params.get("require_quote") is True
    answer = _answer_for(evidence, step_id)
    if not answer:
        return False
    consumable_texts: list[str] = []
    title_link_hits: list[tuple[str, str]] = []
    counted = 0
    for retrieval in _as_list(evidence.get("retrievals")):
        if not isinstance(retrieval, Mapping):
            continue
        if step_id is not None and retrieval.get("step_id") != step_id:
            continue
        if not _run_id_allowed(retrieval, step_id, run_ids):
            continue
        if kb_id is not None and retrieval.get("kb_id") != kb_id:
            continue
        if retrieval.get("backend") != "n_kb":
            continue
        for item in _as_list(retrieval.get("results")):
            if not _kb_consumable(item):
                continue
            counted += 1
            consumable_texts.append(_kb_text(item, *_KB_CONTENT_KEYS))
            title = _kb_text(item, "title")
            link = _kb_text(item, "source", "url")
            if title and link:
                title_link_hits.append((title, link))
    if counted < min_results:
        return False
    # 路径 a：原文引用（回答含可消费条目正文的连续片段）；短于最小引用
    # 长度的条目不可引用（无法与转述区分），走标题+链接路径
    for text in consumable_texts:
        if len(text) < KB_QUOTE_MIN_LENGTH:
            continue
        for start in range(len(text) - KB_QUOTE_MIN_LENGTH + 1):
            if text[start : start + KB_QUOTE_MIN_LENGTH] in answer:
                return True
    # 路径 b：标题+链接（require_quote=False 时可用）
    if not require_quote:
        for title, link in title_link_hits:
            if title in answer and link in answer:
                return True
    return False


def _check_cli_channel(
    params: Mapping[str, Any],
    step_id: str | None,
    evidence: Mapping[str, Any],
    run_ids: dict[str, set[str]],
) -> bool:
    """T11：CLI 通道真实入口核验。

    通过条件：来源标记为 cli（普通 agent 适配不冒充）、真实会话已
    绑定、每个步骤真实执行（exit 0 且有输出、无超时/进程身份缺失）。
    证据不完整由框架层 skipped，不在此判通过。"""
    cli = evidence.get("cli")
    if not isinstance(cli, Mapping):
        return False
    if str(cli.get("source") or "") != "cli":
        return False
    if not cli.get("session_id") or not cli.get("conversation_id"):
        return False
    steps = [
        step for step in _as_list(cli.get("steps"))
        if isinstance(step, Mapping)
    ]
    if not steps:
        return False
    for step in steps:
        if step.get("exit_code") != 0:
            return False
        if not str(step.get("stdout") or ""):
            return False
        if step.get("timed_out") or step.get("spawn_error"):
            return False
        # 进程身份缺失：无法证明真实子进程执行（spawn 替身/记录丢失）
        if not str(step.get("process_identity") or ""):
            return False
    return True


_CHECKERS = {
    "tool_succeeded": _check_tool_succeeded,
    "tool_absent": _check_tool_absent,
    "result_contains": _check_result_contains,
    "task_terminal": _check_task_terminal,
    "artifact_set": _check_artifact_set,
    "file_content": _check_file_content,
    "browser_state": _check_browser_state,
    "protocol_sequence": _check_protocol_sequence,
    "kb_source_and_quote": _check_kb_source_and_quote,
    "cli_channel": _check_cli_channel,
}


def evaluate_scenario_assertions(content: Mapping[str, Any]) -> EvaluationValue:
    """``agent.scenario_assertions`` deterministic 构建器（content -> 值）。

    content 契约：``assertions``（ScenarioCase 断言的 JSON 形态）+
    ``scenario_evidence``（执行期采集证据集合）。缺块按 evidence_missing
    skipped——缺失/跳过不算通过（plan T3 S4 硬约束）。"""
    assertions_raw = content.get("assertions")
    evidence_raw = content.get("scenario_evidence")
    if not isinstance(assertions_raw, list):
        return _skip(SkipReason.evidence_missing, "snapshot 缺少 assertions 声明")
    if not isinstance(evidence_raw, Mapping):
        return _skip(SkipReason.evidence_missing, "snapshot 缺少 scenario_evidence")
    status = evidence_raw.get("status")
    if status == "unavailable":
        return _skip(
            SkipReason.evidence_missing,
            f"scenario evidence unavailable: {evidence_raw.get('reason') or '未知原因'}",
        )
    # complete 空集合可确认"未发生"；incomplete 不能确认缺席
    if status != "complete":
        return _skip(
            SkipReason.evidence_missing,
            f"scenario evidence incomplete: {evidence_raw.get('reason') or '未知原因'}",
        )
    assertions = [a for a in assertions_raw if isinstance(a, Mapping)]
    if not assertions:
        return _skip(
            SkipReason.not_applicable, "case 未声明任何场景断言"
        )
    run_ids = _step_run_ids(evidence_raw)
    results: list[bool] = []
    for assertion in assertions:
        checker = _CHECKERS.get(str(assertion.get("assertion_type") or ""))
        if checker is None:
            # 未知断言类型按失败处理（fail-closed，不静默放过）
            results.append(False)
            continue
        step_id = assertion.get("step_id")
        step_id = step_id if isinstance(step_id, str) and step_id else None
        params_raw = assertion.get("params")
        params = params_raw if isinstance(params_raw, Mapping) else {}
        results.append(checker(params, step_id, evidence_raw, run_ids))
    return EvaluationValue(
        typed_value=all(results),
        status="value",
        explanation=(
            f"scenario assertions: {sum(results)}/{len(results)} passed"
        ),
    )

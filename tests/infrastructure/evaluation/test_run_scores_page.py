"""run-scores 游标编解码与过滤指纹（ns 绑定）单元测试 + 聚合分页集成用例。"""
import base64
import json
from contextlib import contextmanager
from uuid import uuid4

import pytest

from app.domain.evaluation.models import EvaluationValidationError
from app.infrastructure.evaluation.store import (
    SQLiteEvaluationStore,
    _encode_run_scores_cursor,
    _parse_run_scores_cursor,
    _results_filter_fingerprint,
    _run_scores_filter_fingerprint,
)
from tests.infrastructure.evaluation.test_evaluation_store import (
    _auto_result_payload,
    _definition_payload,
    _fixed_store_now,
    _job_payload,
    _sample_payload,
    _seed_auto_result,
    _seed_human_result,
)


def _raw_token(payload) -> str:
    raw = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


_MISSING = object()


def _run_token(**overrides) -> str:
    payload = {
        "v": 2,
        "ns": "run",
        "fp": "fp1",
        "snap": "2026-11-24T00:00:00+00:00",
        "cur": ["2026-11-23T10:00:00+00:00", "n-agent", "run-1"],
    }
    for key, value in overrides.items():
        if value is _MISSING:
            payload.pop(key, None)
        else:
            payload[key] = value
    return _raw_token(payload)


def _normalized_filters(**overrides) -> dict:
    base = {
        "target_service": None,
        "target_kind": "agent_run",
        "target_subject_id": None,
        "target_subject_revision": None,
        "definition_id": None,
        "definition_version": None,
        "experiment_id": None,
        "evaluation_scene": "online",
        "status": None,
        "provenance": None,
        "created_from": None,
        "created_to": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 编解码
# ---------------------------------------------------------------------------


def test_run_cursor_roundtrip():
    token = _encode_run_scores_cursor(
        "fp1", "2026-11-24T00:00:00+00:00",
        "2026-11-23T10:00:00+00:00", "n-agent", "run-1",
    )
    parsed = _parse_run_scores_cursor(token)
    assert parsed["fp"] == "fp1"
    assert parsed["snap"] == "2026-11-24T00:00:00+00:00"
    assert parsed["cur"] == ["2026-11-23T10:00:00+00:00", "n-agent", "run-1"]


def test_run_cursor_roundtrip_z_suffix():
    token = _encode_run_scores_cursor(
        "abc123", "2026-11-24T00:00:00Z", "2026-11-23T10:00:00Z", "svc", "r9"
    )
    parsed = _parse_run_scores_cursor(token)
    assert parsed["snap"] == "2026-11-24T00:00:00Z"
    assert parsed["cur"] == ["2026-11-23T10:00:00Z", "svc", "r9"]


@pytest.mark.parametrize(
    "bad",
    [
        # results v2 游标（无 ns）不得被 run-scores 接受
        _raw_token({"v": 2, "fp": "fp", "snap": None, "cur": ["t", "r"]}),
        "",  # 空串
        "a|b",  # results legacy 形状
        "not-base64!!",
        base64.urlsafe_b64encode(b"{}").decode(),  # 非 dict 结构字段缺失
        _run_token(ns="other"),  # 命名空间不符
        _run_token(cur=["a", "b"]),  # 边界长度不符（results 形状）
        _run_token(v=1),  # 版本不符
        _run_token(v="2"),  # 版本类型不符
        _raw_token(["not", "a", "dict"]),  # 顶层非 dict
        _raw_token("just-a-string"),  # 顶层非 dict
    ],
)
def test_run_cursor_rejects_foreign_and_malformed(bad):
    with pytest.raises(EvaluationValidationError):
        _parse_run_scores_cursor(bad)


@pytest.mark.parametrize(
    "overrides",
    [
        # fp：缺失 / 空串 / 非字符串
        {"fp": _MISSING},
        {"fp": ""},
        {"fp": 123},
        {"fp": None},
        # snap：缺失 / null / 空串 / 无时区 / 非法日期
        {"snap": _MISSING},
        {"snap": None},
        {"snap": ""},
        {"snap": "   "},
        {"snap": "2026-11-24T00:00:00"},
        {"snap": "2026-11-24T00:00:00+07:00", "cur": ["bad-ts", "svc", "r"]},
        {"snap": "not-a-date+00:00"},
        # cur：长度不符 / 非字符串元素 / 空字段 / 非法时间
        {"cur": []},
        {"cur": ["a"]},
        {"cur": ["a", "b"]},
        {"cur": ["a", "b", "c", "d"]},
        {"cur": ["a", "b", None]},
        {"cur": [123, "svc", "r"]},
        {"cur": ["", "svc", "r"]},
        {"cur": ["2026-11-23T10:00:00+00:00", "", "r"]},
        {"cur": ["2026-11-23T10:00:00+00:00", "svc", "   "]},
        {"cur": ["2026-11-23T10:00:00", "svc", "r"]},  # 无时区
        {"cur": ["not-a-date", "svc", "r"]},  # 非法日期
    ],
)
def test_run_cursor_rejects_invalid_fields(overrides):
    token = _run_token(**overrides)
    with pytest.raises(EvaluationValidationError):
        _parse_run_scores_cursor(token)


# ---------------------------------------------------------------------------
# 过滤指纹
# ---------------------------------------------------------------------------


def test_run_fingerprint_stable():
    fp = _run_scores_filter_fingerprint(_normalized_filters())
    assert fp == _run_scores_filter_fingerprint(_normalized_filters())


@pytest.mark.parametrize(
    "override",
    [
        {"definition_id": "def-1"},
        {"definition_version": "v2"},
        {"provenance": "auto"},
        {"status": "value"},
        {"created_from": "2026-11-01T00:00:00+00:00"},
        {"created_to": "2026-11-30T00:00:00+00:00"},
    ],
)
def test_run_fingerprint_changes_per_allowed_filter_field(override):
    base_fp = _run_scores_filter_fingerprint(_normalized_filters())
    changed_fp = _run_scores_filter_fingerprint(_normalized_filters(**override))
    assert base_fp != changed_fp


def test_run_fingerprint_binds_namespace_vs_results_fingerprint():
    # 同一组规范化过滤，run-scores 与 results 指纹必须不同（ns 隔离）
    filters = _normalized_filters()
    assert _run_scores_filter_fingerprint(filters) != _results_filter_fingerprint(**filters)
    filters_with_values = _normalized_filters(status="value", provenance="auto")
    assert (
        _run_scores_filter_fingerprint(filters_with_values)
        != _results_filter_fingerprint(**filters_with_values)
    )


def test_run_fingerprint_changes_when_fixed_scene_differs():
    fp = _run_scores_filter_fingerprint(_normalized_filters())
    assert fp != _run_scores_filter_fingerprint(
        _normalized_filters(evaluation_scene="experiment")
    )


# ---------------------------------------------------------------------------
# 聚合分页集成用例（query_run_scores_page_counted / query_run_scores_page）
# ---------------------------------------------------------------------------

# 固定快照上界（晚于所有种子时间戳，早于真实时钟，保证确定性）
_SNAP = "2026-11-21T00:00:00+00:00"


def _ts(step: int) -> str:
    """确定性种子时间戳（2026-11-20 起，分钟步进）。"""
    return f"2026-11-20T{10 + step // 60:02d}:{step % 60:02d}:00+00:00"


def _late_ts(step: int) -> str:
    """晚于 _SNAP 的确定性时间戳（翻页间追加用）。"""
    return f"2026-11-22T{10 + step // 60:02d}:{step % 60:02d}:00+00:00"


def _run_target(run_id, session_id=None, service="n-agent") -> dict:
    """agent_run 目标（run_id 可为空串/None 模拟缺失身份）。"""
    target = {
        "service": service,
        "kind": "agent_run",
        "subject_id": f"subject-{run_id or 'blank'}",
        "subject_revision": "rev-1",
    }
    if run_id is not None:
        target["run_id"] = run_id
    if session_id is not None:
        target["session_id"] = session_id
    return target


def _metric_of(row, result_id):
    return next(m for m in row["metrics"] if m["result_id"] == result_id)


def _assert_metric_shape(metric):
    assert set(metric) == {
        "result_id", "metric_name", "typed_value", "passed",
        "status", "provenance", "created_at",
    }


async def _seed_auto_raw(
    store, *, ts, run_id, session_id=None, result_id=None,
    metric_name="answer_relevance", definition_id="def-A",
    definition_version="v1", status="value", typed_value=0.87,
    supersedes_result_id=None, service="n-agent",
):
    """可控时间戳/身份/结果 id 的自动结果种子，返回 result_id。"""
    target = _run_target(run_id, session_id, service)
    with _fixed_store_now(ts):
        await store.save_definition(_definition_payload(
            definition_id=definition_id,
            version=definition_version,
            metric_name=metric_name,
        ))
        sample_id = await store.save_sample(_sample_payload(
            producer_event_id=uuid4().hex, target=target, service=service,
        ))
        job_id = await store.save_job(_job_payload(
            sample_id,
            definition_id=definition_id,
            definition_version=definition_version,
            idempotency_key=uuid4().hex,
        ))
        payload = _auto_result_payload(
            job_id,
            metric_name=metric_name,
            metric_version=definition_version,
            status=status,
            typed_value=typed_value,
        )
        if result_id is not None:
            payload["result_id"] = result_id
        if supersedes_result_id is not None:
            payload["supersedes_result_id"] = supersedes_result_id
        return await store.save_result(payload)


@pytest.mark.asyncio
async def test_run_scores_groups_by_service_and_run(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluation.db"))
    run1_a = await _seed_auto_raw(
        store, ts=_ts(1), run_id="run-1", session_id="sess-1",
        definition_id="def-faith", metric_name="faithfulness",
    )
    run1_b = await _seed_auto_raw(
        store, ts=_ts(2), run_id="run-1", session_id="sess-1",
        definition_id="def-rel", metric_name="answer_relevance",
    )
    await _seed_auto_raw(store, ts=_ts(3), run_id="run-2", session_id="sess-2")
    await _seed_auto_raw(store, ts=_ts(4), run_id="run-3")  # 无 session_id
    # 缺失身份行：run 身份空串，不得成组
    await _seed_auto_raw(
        store, ts=_ts(5), run_id="", session_id="sess-orphan",
    )

    page = await store.query_run_scores_page_counted()
    assert page["total_count"] == 3
    assert page["total_pages"] == 1
    assert page["next_cursor"] is None
    by_run = {row["run_id"]: row for row in page["items"]}
    assert set(by_run) == {"run-1", "run-2", "run-3"}

    run1 = by_run["run-1"]
    assert run1["target_service"] == "n-agent"
    assert run1["result_count"] == 2 == len(run1["metrics"])
    assert run1["session_id"] == "sess-1"
    assert run1["last_created_at"] == _ts(2)
    # metrics 按 created_at DESC
    assert [m["metric_name"] for m in run1["metrics"]] == [
        "answer_relevance", "faithfulness",
    ]
    assert run1["metrics"][0]["result_id"] == run1_b
    assert run1["metrics"][1]["result_id"] == run1_a
    for metric in run1["metrics"]:
        _assert_metric_shape(metric)
        assert metric["provenance"] == "automatic"
        assert metric["status"] == "value"

    assert by_run["run-3"]["session_id"] is None  # 前端走未关联回退
    assert by_run["run-2"]["session_id"] == "sess-2"
    for row in page["items"]:
        assert row["result_count"] == len(row["metrics"])


@pytest.mark.asyncio
async def test_run_scores_session_conflict_returns_null(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluation.db"))
    await _seed_auto_raw(store, ts=_ts(1), run_id="run-9", session_id="sess-A")
    await _seed_auto_raw(store, ts=_ts(2), run_id="run-9", session_id="sess-B")
    page = await store.query_run_scores_page_counted()
    assert page["total_count"] == 1
    row = page["items"][0]
    assert row["run_id"] == "run-9"
    assert row["result_count"] == 2
    assert row["session_id"] is None  # 冲突回退 None


@pytest.mark.asyncio
async def test_run_scores_keyset_pagination_no_overlap(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluation.db"))
    # run-a 多结果（ts1/ts2），run-b 与 run-a 同 last_created_at（ts2），
    # run-c / run-d 各一条，共 4 组，limit=1 逐页翻完
    run_a_1 = await _seed_auto_raw(store, ts=_ts(1), run_id="run-a",
                                   definition_id="def-a1", metric_name="m1")
    run_a_2 = await _seed_auto_raw(store, ts=_ts(2), run_id="run-a",
                                   definition_id="def-a2", metric_name="m2")
    run_b = await _seed_auto_raw(store, ts=_ts(2), run_id="run-b")
    await _seed_auto_raw(store, ts=_ts(3), run_id="run-c")
    await _seed_auto_raw(store, ts=_ts(4), run_id="run-d")

    # 同 last_created_at 的两组按 (target_service DESC, run_id DESC)：
    # run-b 先于 run-a
    expected_runs = ["run-d", "run-c", "run-b", "run-a"]
    seen: list[str] = []
    totals: list[int] = []
    cursor = None
    for _ in range(4):
        page = await store.query_run_scores_page_counted(limit=1, cursor=cursor)
        totals.append(page["total_count"])
        assert page["total_pages"] == 4
        assert len(page["items"]) == 1
        row = page["items"][0]
        seen.append(row["run_id"])
        if row["run_id"] == "run-a":
            # 单 run metrics 不被截断
            assert row["result_count"] == 2 == len(row["metrics"])
            assert {m["result_id"] for m in row["metrics"]} == {run_a_1, run_a_2}
        cursor = page["next_cursor"]
    assert seen == expected_runs
    assert cursor is None
    assert totals == [4, 4, 4, 4]  # total_count 恒定


@pytest.mark.asyncio
async def test_run_scores_filters_apply_before_grouping(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluation.db"))
    ok_id = await _seed_auto_raw(store, ts=_ts(1), run_id="run-1",
                                 session_id="sess-1", status="value")
    err_id = await _seed_auto_raw(store, ts=_ts(2), run_id="run-1",
                                  session_id="sess-1", status="error")
    await _seed_auto_raw(store, ts=_ts(3), run_id="run-2", status="value")

    unfiltered = await store.query_run_scores_page_counted()
    assert unfiltered["total_count"] == 2
    run1 = next(r for r in unfiltered["items"] if r["run_id"] == "run-1")
    assert run1["result_count"] == 2
    assert run1["last_created_at"] == _ts(2)

    filtered = await store.query_run_scores_page_counted(status="value")
    assert filtered["total_count"] == 2  # run-1/run-2 仍在，各剩 1 条
    run1_f = next(r for r in filtered["items"] if r["run_id"] == "run-1")
    assert run1_f["result_count"] == 1 == len(run1_f["metrics"])
    assert run1_f["metrics"][0]["result_id"] == ok_id
    assert run1_f["last_created_at"] == _ts(1)  # 同步收敛

    error_only = await store.query_run_scores_page_counted(status="error")
    assert error_only["total_count"] == 1
    assert error_only["items"][0]["run_id"] == "run-1"
    assert [m["result_id"] for m in error_only["items"][0]["metrics"]] == [err_id]


@pytest.mark.asyncio
async def test_run_scores_excludes_experiment_results(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluation.db"))
    online = await _seed_auto_raw(store, ts=_ts(1), run_id="run-1",
                                  session_id="sess-1")
    with _fixed_store_now(_ts(2)):
        await store.save_definition(_definition_payload(
            definition_id="def-exp", version="v1",
        ))
        sample_id = await store.save_sample(_sample_payload(
            producer_event_id=uuid4().hex,
            target=_run_target("run-1", "sess-1"),
        ))
        job_id = await store.save_job(_job_payload(
            sample_id, definition_id="def-exp", definition_version="v1",
            idempotency_key=uuid4().hex, experiment_id="exp-1",
        ))
        await store.save_result(_auto_result_payload(job_id))

    page = await store.query_run_scores_page_counted()
    assert page["total_count"] == 1  # 实验场景结果不进入 run-scores 聚合
    row = page["items"][0]
    assert row["run_id"] == "run-1"
    assert row["result_count"] == 1 == len(row["metrics"])
    assert row["metrics"][0]["result_id"] == online


@pytest.mark.asyncio
async def test_run_scores_cursor_invalid_cases(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluation.db"))
    await _seed_auto_raw(store, ts=_ts(1), run_id="run-1")
    await _seed_auto_raw(store, ts=_ts(2), run_id="run-2")
    page = await store.query_run_scores_page_counted(limit=1)
    assert page["next_cursor"] is not None

    # 旧 results v2 游标不得被 run-scores 接受
    results_page = await store.query_results_page_counted(limit=1)
    assert results_page["next_cursor"] is not None
    with pytest.raises(EvaluationValidationError):
        await store.query_run_scores_page_counted(
            cursor=results_page["next_cursor"])

    # 篡改过滤后旧 token（fingerprint 不匹配）
    with pytest.raises(EvaluationValidationError):
        await store.query_run_scores_page_counted(
            status="value", cursor=page["next_cursor"])

    # 非法 snap（无时区）
    with pytest.raises(EvaluationValidationError):
        await store.query_run_scores_page_counted(
            cursor=_run_token(snap="2026-11-20T10:00:00"))

    # 空字符串 cursor 是显式非法游标
    with pytest.raises(EvaluationValidationError):
        await store.query_run_scores_page_counted(cursor="")


@pytest.mark.asyncio
async def test_run_scores_supersede_and_tombstone_visible(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluation.db"))
    old = await _seed_auto_raw(store, ts=_ts(1), run_id="run-1",
                               session_id="sess-1")
    current = await _seed_auto_raw(store, ts=_ts(2), run_id="run-1",
                                   session_id="sess-1",
                                   supersedes_result_id=old)
    doomed = await _seed_auto_raw(store, ts=_ts(3), run_id="run-2")
    await store.record_deletion_tombstone({
        "subject_kind": "result", "subject_id": doomed,
    })

    page = await store.query_run_scores_page_counted()
    assert page["total_count"] == 1  # 被 supersede 与 tombstone 的不参与聚合
    row = page["items"][0]
    assert row["run_id"] == "run-1"
    assert row["result_count"] == 1 == len(row["metrics"])
    assert row["metrics"][0]["result_id"] == current


@pytest.mark.asyncio
async def test_run_scores_page_triple_matches_counted(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluation.db"))
    await _seed_auto_raw(store, ts=_ts(1), run_id="run-1")
    await _seed_auto_raw(store, ts=_ts(2), run_id="run-2")
    await _seed_auto_raw(store, ts=_ts(3), run_id="run-3")

    with _fixed_store_now(_SNAP):
        items, cursor, snap = await store.query_run_scores_page(limit=2)
        counted = await store.query_run_scores_page_counted(limit=2)
    assert snap == _SNAP
    assert items == counted["items"]
    assert cursor == counted["next_cursor"]
    assert snap == counted["snapshot_at"]
    assert len(items) == 2
    assert counted["total_count"] == 3

    items2, cursor2, snap2 = await store.query_run_scores_page(
        limit=2, cursor=cursor)
    assert snap2 == _SNAP  # 翻页复用 token 内快照上界
    assert [row["run_id"] for row in items2] == ["run-1"]
    assert cursor2 is None

    with pytest.raises(EvaluationValidationError):
        await store.query_run_scores_page(limit=0)


@pytest.mark.asyncio
async def test_run_scores_empty_set_limit_bounds_and_full_last_page(tmp_path):
    empty_store = SQLiteEvaluationStore(str(tmp_path / "empty.db"))
    page = await empty_store.query_run_scores_page_counted()
    assert page["items"] == []
    assert page["next_cursor"] is None
    assert (page["total_count"], page["total_pages"]) == (0, 1)

    store = SQLiteEvaluationStore(str(tmp_path / "evaluation.db"))
    for step, run_id in enumerate(("run-a", "run-b", "run-c"), start=1):
        await _seed_auto_raw(store, ts=_ts(step), run_id=run_id)

    # 末页恰满：无 cursor
    full = await store.query_run_scores_page_counted(limit=3)
    assert len(full["items"]) == 3
    assert full["next_cursor"] is None
    assert full["total_pages"] == 1
    partial = await store.query_run_scores_page_counted(limit=2)
    assert partial["next_cursor"] is not None

    # 同时间不同服务同名 run：服务是分组键一部分，两独立组
    with _fixed_store_now(_ts(9)):
        await _seed_auto_raw(store, ts=_ts(9), run_id="run-x",
                             service="svc-b", definition_id="def-xb")
        await _seed_auto_raw(store, ts=_ts(9), run_id="run-x",
                             service="svc-a", definition_id="def-xa")
    tie = await store.query_run_scores_page_counted(limit=100)
    tie_rows = [r for r in tie["items"] if r["run_id"] == "run-x"]
    assert len(tie_rows) == 2
    assert {r["target_service"] for r in tie_rows} == {"svc-a", "svc-b"}
    # 同 last_created_at 同 run_id 时按 target_service DESC：svc-b 在前
    assert [r["target_service"] for r in tie_rows[:2]] == ["svc-b", "svc-a"]


@pytest.mark.asyncio
async def test_run_scores_limit_cap_and_default(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluation.db"))
    for step in range(201):
        await _seed_auto_raw(store, ts=_ts(step + 1), run_id=f"run-{step:03d}")

    # limit=201 封顶 200 后计算页数
    capped = await store.query_run_scores_page_counted(limit=201)
    assert len(capped["items"]) == 200
    assert capped["total_count"] == 201
    assert capped["total_pages"] == 2
    assert capped["next_cursor"] is not None

    # 默认 50
    default = await store.query_run_scores_page_counted()
    assert len(default["items"]) == 50
    assert default["total_count"] == 201
    assert default["total_pages"] == 5

    for bad_limit in (0, -1, "50", True, 1.5):
        with pytest.raises(EvaluationValidationError):
            await store.query_run_scores_page_counted(limit=bad_limit)


@pytest.mark.asyncio
async def test_run_scores_snapshot_excludes_late_and_midpage_mutations(
    tmp_path,
):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluation.db"))
    run_z = await _seed_auto_raw(store, ts=_ts(0), run_id="run-z")
    run_a_1 = await _seed_auto_raw(store, ts=_ts(1), run_id="run-a",
                                   definition_id="def-a1", metric_name="m1")
    run_a_2 = await _seed_auto_raw(store, ts=_ts(2), run_id="run-a",
                                   definition_id="def-a2", metric_name="m2")
    await _seed_auto_raw(store, ts=_ts(3), run_id="run-b")

    with _fixed_store_now(_SNAP):
        first = await store.query_run_scores_page_counted(limit=1)
    assert first["snapshot_at"] == _SNAP
    assert first["total_count"] == 3
    assert first["items"][0]["run_id"] == "run-b"

    # 翻页间变化：supersede run-a 的 ts2 结果（替代者晚于 snap）+ 追加新 run
    await _seed_auto_raw(store, ts=_late_ts(1), run_id="run-a",
                         definition_id="def-a3", metric_name="m3",
                         supersedes_result_id=run_a_2)
    await _seed_auto_raw(store, ts=_late_ts(2), run_id="run-new")

    second = await store.query_run_scores_page_counted(
        limit=1, cursor=first["next_cursor"])
    # 晚于 snap 的追加结果被排除；被取代的 ts2 结果退出当前视图
    assert second["snapshot_at"] == _SNAP
    assert second["total_count"] == 3  # run-b + run-a + run-z，run-new 不计入
    row = second["items"][0]
    assert row["run_id"] == "run-a"
    assert row["result_count"] == 1 == len(row["metrics"])
    assert row["metrics"][0]["result_id"] == run_a_1

    # 翻页间 tombstone 仍生效：run-a 仅存结果被删后彻底消失
    await store.record_deletion_tombstone({
        "subject_kind": "result", "subject_id": run_a_1,
    })
    third = await store.query_run_scores_page_counted(
        limit=1, cursor=second["next_cursor"])
    assert third["total_count"] == 2
    assert [r["run_id"] for r in third["items"]] == ["run-z"]
    assert third["items"][0]["metrics"][0]["result_id"] == run_z
    assert third["next_cursor"] is None


@pytest.mark.asyncio
async def test_run_scores_member_tiebreak_and_identity_fallback(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluation.db"))
    # 同 created_at 按 result_id 倒序；同名指标不同 result_id 保留
    c_id = await _seed_auto_raw(store, ts=_ts(1), run_id="run-m",
                                result_id="c" * 32, definition_id="def-c",
                                metric_name="dup_metric")
    b_id = await _seed_auto_raw(store, ts=_ts(2), run_id="run-m",
                                result_id="b" * 32, definition_id="def-b",
                                metric_name="m2")
    a_id = await _seed_auto_raw(store, ts=_ts(2), run_id="run-m",
                                result_id="a" * 32, definition_id="def-a2",
                                metric_name="m1")
    page = await store.query_run_scores_page_counted()
    row = page["items"][0]
    assert row["run_id"] == "run-m"
    assert [m["result_id"] for m in row["metrics"]] == ["b" * 32, "a" * 32,
                                                        "c" * 32]
    assert [m["metric_name"] for m in row["metrics"]] == ["m2", "m1",
                                                          "dup_metric"]

    # sample 空 run_id 回退身份表（含 session 回退）
    target = _run_target("", None)
    target["session_id"] = ""
    with _fixed_store_now(_ts(3)):
        await store.save_definition(_definition_payload())
        sample_id = await store.save_sample(_sample_payload(
            producer_event_id=uuid4().hex, target=target))
        job_id = await store.save_job(_job_payload(
            sample_id, idempotency_key=uuid4().hex))
        recovered = await store.save_result(_auto_result_payload(job_id))
    await store.save_sample_identity(
        sample_id, "sess-rec", "run-rec", "audit", {})

    page2 = await store.query_run_scores_page_counted()
    assert page2["total_count"] == 2
    rec = next(r for r in page2["items"] if r["run_id"] == "run-rec")
    assert rec["session_id"] == "sess-rec"
    assert [m["result_id"] for m in rec["metrics"]] == [recovered]

    # 最终 NULL 身份排除：target 完全无 run_id 且无身份旁表
    await _seed_auto_raw(store, ts=_ts(4), run_id=None, session_id="sess-bare",
                         definition_id="def-bare")
    bare_store_rows = await store.query_run_scores_page_counted()
    assert bare_store_rows["total_count"] == 2  # NULL run_id 不成组
    assert {r["run_id"] for r in bare_store_rows["items"]} == {
        "run-m", "run-rec"}


@pytest.mark.asyncio
async def test_run_scores_identity_tombstone_hides_fallback_group(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluation.db"))
    with _fixed_store_now(_ts(1)):
        await store.save_definition(_definition_payload())
        sample_id = await store.save_sample(_sample_payload(
            producer_event_id=uuid4().hex, target=_run_target("", None)))
        job_id = await store.save_job(_job_payload(
            sample_id, idempotency_key=uuid4().hex))
        await store.save_result(_auto_result_payload(job_id))
    await store.save_sample_identity(sample_id, None, "run-rec", "audit", {})

    page = await store.query_run_scores_page_counted()
    assert page["total_count"] == 1
    assert page["items"][0]["run_id"] == "run-rec"

    # 身份 tombstone 经 si JOIN 可见性隐藏回退组（结果行未被 tombstone）
    await store.record_deletion_tombstone({
        "subject_kind": "agent_run", "subject_id": "run-rec",
    })
    after = await store.query_run_scores_page_counted()
    assert after["total_count"] == 0
    assert after["items"] == []


@pytest.mark.asyncio
async def test_run_scores_definition_filters_and_human_auto_mix(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluation.db"))
    v1_id = await _seed_auto_raw(store, ts=_ts(1), run_id="run-1",
                                 session_id="sess-1",
                                 definition_id="def-A", definition_version="v1")
    v2_id = await _seed_auto_raw(store, ts=_ts(2), run_id="run-1",
                                 session_id="sess-1",
                                 definition_id="def-A", definition_version="v2")
    human_id = await _seed_human_result(
        store, definition_id="def-H", definition_version="v1",
        metric_name="human_metric",
        target=_run_target("run-1", "sess-1"),
    )

    page = await store.query_run_scores_page_counted()
    assert page["total_count"] == 1
    row = page["items"][0]
    assert row["run_id"] == "run-1"
    assert row["target_service"] == "n-agent"  # 自动与人评同服务同组
    assert row["session_id"] == "sess-1"
    assert row["result_count"] == 3 == len(row["metrics"])
    assert [m["result_id"] for m in row["metrics"]] == [
        human_id, v2_id, v1_id]
    assert {m["provenance"] for m in row["metrics"]} == {"automatic", "human"}
    assert _metric_of(row, human_id)["provenance"] == "human"
    assert _metric_of(row, human_id)["typed_value"] == 0.5

    versioned = await store.query_run_scores_page_counted(
        definition_version="v2")
    assert versioned["total_count"] == 1
    assert versioned["items"][0]["result_count"] == 1
    assert versioned["items"][0]["metrics"][0]["result_id"] == v2_id

    human_only = await store.query_run_scores_page_counted(provenance="human")
    assert human_only["total_count"] == 1
    assert [m["result_id"] for m in human_only["items"][0]["metrics"]] == [
        human_id]

    auto_only = await store.query_run_scores_page_counted(
        provenance="automatic")
    assert auto_only["items"][0]["result_count"] == 2

    from_created = await store.query_run_scores_page_counted(
        created_from=_ts(2))
    assert from_created["items"][0]["result_count"] == 2
    assert [m["result_id"] for m in from_created["items"][0]["metrics"]] == [
        human_id, v2_id]

    to_created = await store.query_run_scores_page_counted(
        created_to=_ts(2))  # 半开区间 [from, to)
    assert to_created["total_count"] == 1
    assert [m["result_id"] for m in to_created["items"][0]["metrics"]] == [
        v1_id]

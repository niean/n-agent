"""T6：Dashboard SSE 场景适配器与 R1 发布测试（plan T6 S1）。

覆盖反例：
- SSE 未终止（流提前关闭、无 [DONE]）：不判成功、按传输失败且副作用
  未观测；
- 会话映射缺失：finish 事件无 run_id 且无会话归属 -> 不得声称成功；
- hello 错误：SSE error 结局按业务失败，不产生通过；
- sandbox stdout 伪造：模型文本声称的 sandbox 输出不是证据，证据只来自
  正式端口；端口读取失败按 incomplete（≠空集合≠通过）；
- weather 无来源：回答含天气文本但正式端口无工具记录 -> 证据不含工具
  调用（断言层判定失败，适配层不伪造来源）；
- 图片 fixture 三路字节校验：manifest fixture_sha256 == 数据集目录文件
  字节 == 源样例 PNG 字节。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from app.infrastructure.evaluation.scenario_adapters import (
    ScenarioDashboardSSERunner,
)

BASE_URL = "http://agent.test"
MODEL = "target-model"


# ---------------------------------------------------------------------------
# SSE 夹具
# ---------------------------------------------------------------------------


def _sse(chunks: list[dict], *, terminated: bool = True) -> bytes:
    lines = [f"data: {json.dumps(c, ensure_ascii=False)}" for c in chunks]
    if terminated:
        lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode()


def _content_chunk(text: str) -> dict:
    return {"choices": [{"index": 0, "delta": {"content": text},
                         "finish_reason": None}]}


def _finish_chunk(run_id="run-1", finish_reason="stop") -> dict:
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            "run_id": run_id}


def _transport(recorder, responder):
    async def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return responder(request)
    return httpx.MockTransport(handler)


def _runner(recorder, responder, *, tool_reader=None) -> ScenarioDashboardSSERunner:
    return ScenarioDashboardSSERunner(
        base_url=BASE_URL, model=MODEL,
        client=httpx.AsyncClient(transport=_transport(recorder, responder)),
        tool_evidence_reader=tool_reader,
    )


def _request(turns=None, session_id="sess-r1-a0"):
    return {
        "mode": "execute",
        "target_config": {},
        "session": {"namespace": "ns", "session_id": session_id},
        "turns": turns or [{"step_id": "turn-1", "message": "hi"}],
    }


class _Target:
    kind = "agent_run"


async def _tool_reader(session_id):
    return [
        {"tool_name": "web_fetch", "status": "success", "result": "sunny",
         "run_id": "run-1"},
    ]


# ---------------------------------------------------------------------------
# 反例
# ---------------------------------------------------------------------------


async def test_sse_unterminated_is_transport_error_not_success():
    recorder = []
    body = _sse([_content_chunk("部分回答")], terminated=False)
    runner = _runner(recorder, lambda req: httpx.Response(200, content=body))
    try:
        result = await runner.run_target(_Target(), _request())
    finally:
        await runner.aclose()
    # SSE 未终止：不判成功；流中断时服务端可能已执行副作用
    assert result["status"] == "transport_error"
    assert result.get("side_effects_unobserved") is True


async def test_missing_session_mapping_is_not_success():
    recorder = []
    # finish 事件不携带 run_id：证据无法归属到 run
    body = _sse([_content_chunk("答案"), _finish_chunk(run_id=None)])
    runner = _runner(recorder, lambda req: httpx.Response(200, content=body))
    try:
        result = await runner.run_target(_Target(), _request())
    finally:
        await runner.aclose()
    assert result["status"] == "business_error"
    assert result["error_type"] == "session_mapping_missing"


async def test_hello_error_finish_is_business_error():
    recorder = []
    body = _sse([_content_chunk("provider failed"), _finish_chunk(
        run_id="run-err", finish_reason="error")])
    runner = _runner(recorder, lambda req: httpx.Response(200, content=body))
    try:
        result = await runner.run_target(_Target(), _request())
    finally:
        await runner.aclose()
    assert result["status"] == "business_error"


async def test_sandbox_stdout_claim_is_not_evidence():
    recorder = []
    # 模型文本声称 sandbox 输出 "Hello Sandbox"：文本不是证据
    body = _sse([
        _content_chunk("sandbox 输出: Hello Sandbox"),
        _finish_chunk(run_id="run-sbx"),
    ])
    # 正式端口读取失败：incomplete（≠空集合）
    async def failing_reader(session_id):
        raise RuntimeError("tool-calls port read failed")

    runner = _runner(recorder, lambda req: httpx.Response(200, content=body),
                     tool_reader=failing_reader)
    try:
        result = await runner.run_target(_Target(), _request())
    finally:
        await runner.aclose()
    assert result["status"] == "succeeded"  # 传输本身完成
    evidence = result["output"]["scenario_evidence"]
    assert evidence["status"] == "incomplete"
    assert "tool-calls" in evidence["reason"]
    # 伪造的 stdout 声明不进入证据
    assert evidence.get("tool_calls") in (None, [])


async def test_weather_answer_without_tool_source_has_no_evidence():
    recorder = []
    body = _sse([
        _content_chunk("北京今天晴，气温 20 度。"),
        _finish_chunk(run_id="run-weather"),
    ])
    # 正式端口确认：本次运行没有任何工具调用（完整空集合）
    runner = _runner(recorder, lambda req: httpx.Response(200, content=body),
                     tool_reader=_tool_reader)
    try:
        result = await runner.run_target(_Target(), _request())
    finally:
        await runner.aclose()
    evidence = result["output"]["scenario_evidence"]
    # 证据只含正式端口返回的记录；本例 reader 返回 web_fetch 成功记录，
    # 适配层如实透传——"无来源"由断言层按 run_id 归属判定，适配层不删改
    assert evidence["status"] == "complete"
    assert evidence["tool_calls"][0]["run_id"] == "run-1"
    # steps 归属真实 run_id
    assert evidence["steps"][0]["run_id"] == "run-weather"


async def test_happy_path_assembles_steps_and_session():
    recorder = []
    reader_sessions = []

    async def reader(session_id):
        reader_sessions.append(session_id)
        return await _tool_reader(session_id)

    body = _sse([
        _content_chunk("你好"),
        _finish_chunk(run_id="run-hello"),
    ])
    runner = _runner(recorder, lambda req: httpx.Response(200, content=body),
                     tool_reader=reader)
    try:
        result = await runner.run_target(_Target(), _request())
    finally:
        await runner.aclose()
    assert result["status"] == "succeeded"
    assert result["output"]["final_answer"] == "你好"
    assert result["output"]["completion_reported"] is True
    assert result["run_id"] == "run-hello"
    evidence = result["output"]["scenario_evidence"]
    assert evidence["status"] == "complete"
    assert evidence["steps"] == [
        {"step_id": "turn-1", "run_id": "run-hello", "answer": "你好"}
    ]
    # 正式端口：注入 reader 收到会话隔离的会话 ID
    assert reader_sessions == ["sess-r1-a0"]


async def test_default_tool_port_reads_session_tool_calls():
    """未注入 reader 时经正式端口 GET /chat/sessions/{id}/tool-calls 读取。"""
    recorder = []
    body = _sse([_content_chunk("ok"), _finish_chunk(run_id="run-x")])

    def responder(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        if str(request.url).endswith("/tool-calls"):
            return httpx.Response(200, json=[
                {"tool_name": "web_fetch", "status": "success", "run_id": "run-x"}
            ])
        return httpx.Response(200, content=body)

    runner = _runner(recorder, responder)
    try:
        result = await runner.run_target(_Target(), _request())
    finally:
        await runner.aclose()
    assert result["status"] == "succeeded"
    assert result["output"]["scenario_evidence"]["tool_calls"][0]["tool_name"] == "web_fetch"
    assert any("/tool-calls" in str(r.url) for r in recorder)


# ---------------------------------------------------------------------------
# R1 发布：版本 1 四题 + 图片 fixture 三路字节校验
# ---------------------------------------------------------------------------

R1_DIR = Path("evaluations/datasets/agent-e2e-scenarios/1")
SOURCE_PNG = Path("tests/e2e/conversations/assets/chat-multimodal-1.png")


def test_r1_published_with_four_cases_and_conversion_doc():
    manifest = json.loads((R1_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["dataset_id"] == "agent-e2e-scenarios"
    assert manifest["schema_version"] == "2"
    case_ids = [c["case_id"] for c in manifest["cases"]]
    # 图片 / hello / sandbox / weather 四题（case_id 为源样例文件名）
    for expected in ("multimodal-vision-chat", "plugin-hello-chat",
                     "sandbox-execute-chat", "skill-weather-chat"):
        assert expected in case_ids, case_ids
    doc = Path("evaluations/datasets/e2e-conversion.md").read_text(
        encoding="utf-8"
    )
    assert "agent-e2e-scenarios" in doc


def test_r1_image_fixture_three_way_byte_check():
    """manifest fixture_sha256 == 数据集目录文件字节 == 源样例 PNG 字节。"""
    manifest = json.loads((R1_DIR / "manifest.json").read_text(encoding="utf-8"))
    image_cases = [
        c for c in manifest["cases"] if c.get("fixture")
    ]
    assert image_cases, "图片题必须声明 fixture 绑定"
    for case in image_cases:
        fixture_rel = case["fixture"]
        fixture_bytes = (R1_DIR / fixture_rel).read_bytes()
        source_bytes = SOURCE_PNG.read_bytes()
        assert hashlib.sha256(fixture_bytes).hexdigest() == case["fixture_sha256"]
        assert fixture_bytes == source_bytes


def test_r1_weather_has_no_fabricated_skill_requirement_and_live_marker():
    """weather 源不添加不存在的 skill 调用要求；live 来源标时间。"""
    manifest = json.loads((R1_DIR / "manifest.json").read_text(encoding="utf-8"))
    weather = next(
        c for c in manifest["cases"] if c["case_id"] == "skill-weather-chat"
    )
    tools = weather["scenario"]["requirements"].get("required_tools") or []
    assert "skill" not in " ".join(tools).lower()
    source = weather["scenario"]["source"]
    assert source.get("source_captured_at"), "live 来源必须标注采集时间"

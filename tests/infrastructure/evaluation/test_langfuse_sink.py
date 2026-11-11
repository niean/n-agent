# tests/infrastructure/evaluation/test_langfuse_sink.py
"""T9: Langfuse Score 投影适配器测试（httpx MockTransport，无网络）。

覆盖 plan T9 S1 全部要点与补充实施要求（spec 11.1 / 13 为权威契约）：
- external_id = UUIDv5(project_id + result_id + projection_version)：同
  result 重试复用同 ID；重评（新 result_id）/新投影版本得到新 ID；
- POST /api/public/scores 字段映射（spec 11.1 表）：name={metric}.v{major}
  （judge/human 后缀语义）、value 类型转换（BOOLEAN→0/1 + dataType、
  NUMERIC 有限 float、CATEGORICAL 字符串）、configId、traceId+observationId
  （observation 评分两者都发；session 评分仅 sessionId）、comment 脱敏后
  <=2000 UTF-8 字节、不伪造 source（POST body 无 source 字段）；
- 失败分类（spec 13 表）：409/4xx schema 冲突 -> dead_letter 类异常；
  429/5xx/网络/超时 -> retry 类异常（error_type=network/rate_limit/timeout）；
  回读 404 -> None（Score 先于 Trace 到达的有界等待由 worker 调度）；
- unlinked：目标无 trace_id 且非 session 评分、缺 observation 身份、
  sampled=false、OTel 禁用 -> unlinked，即使存在 trace_id 也不默认可关联；
- 密钥：构造注入（Basic 认证），endpoint 启动期固定；不接受样本/浏览器
  传入地址；异常与错误消息不泄漏密钥；
- ScoreConfig（spec 11.1：definition 输出 schema -> configId）：首次使用
  创建并持久化、复用不再创建、回读 schema hash 校验、已有配置冲突隔离
  （dead_letter 类）、409 按名解析。
"""
from __future__ import annotations

import json
import uuid

import httpx
import pytest

from app.domain.evaluation.errors import EvaluationNotFoundError
from app.domain.evaluation.models import canonical_json_bytes
from app.infrastructure.evaluation.langfuse_sink import (
    LangfuseScoreSink,
    ScoreDeliveryConflictError,
    ScoreDeliveryRetryableError,
    build_score_name,
    compute_external_id,
)

BASE_URL = "http://langfuse.test"
PUBLIC_KEY = "pk-test"
SECRET_KEY = "sk-test-secret"
PROJECT = "n-agent-local-project"


# ---------------------------------------------------------------------------
# 夹具：固定 4.76.0 形态的内存 API（scores / score-configs 路由）
# ---------------------------------------------------------------------------


class FakeLangfuseAPI:
    """MockTransport 处理器：可注入状态码故障与回读载荷漂移。"""

    def __init__(
        self,
        *,
        score_status: int = 201,
        score_error: Exception | None = None,
        read_back: "str | dict | None" = "echo",
        delete_status: int = 202,
        delete_error: Exception | None = None,
        config_status: int = 201,
        config_error: Exception | None = None,
        config_409_name_hit: bool = False,
        config_list_status: int = 200,
        config_drift: dict | None = None,
    ) -> None:
        self.score_status = score_status
        self.score_error = score_error
        self.read_back = read_back  # "echo" | dict | None(空列表)
        self.delete_status = delete_status
        self.delete_error = delete_error
        self.config_status = config_status
        self.config_error = config_error
        self.config_409_name_hit = config_409_name_hit
        self.config_list_status = config_list_status
        self.config_drift = config_drift or {}
        self.requests: list[httpx.Request] = []
        self.posts: list[dict] = []
        self.configs: dict[str, dict] = {}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path == "/api/public/scores":
            if self.score_error is not None:
                raise self.score_error
            if self.score_status != 201:
                return httpx.Response(self.score_status, json={})
            body = json.loads(request.content)
            self.posts.append(body)
            return httpx.Response(201, json={"id": body["id"]})
        # 4.76.0 events_only：GET 按_id_过滤的 v3 列表路由
        if request.method == "GET" and path == "/api/public/v3/scores":
            if request.url.params.get("id") is None:
                raise AssertionError("v3 回读必须携带 id 过滤参数")
            if self.read_back is None:
                return httpx.Response(200, json={"data": []})
            payload = (
                self.read_back
                if isinstance(self.read_back, dict)
                else (self.posts[-1] if self.posts else {})
            )
            return httpx.Response(
                200, json={"data": [self._read_back_body(payload, request.url.params.get("id"))]}
            )
        if request.method == "DELETE" and path.startswith("/api/public/scores/"):
            if self.delete_error is not None:
                raise self.delete_error
            return httpx.Response(self.delete_status)
        if request.method == "POST" and path == "/api/public/score-configs":
            if self.config_error is not None:
                raise self.config_error
            body = json.loads(request.content)
            # 409 表示同名配置已存在：仍然登记，供按名解析命中
            config = {"id": "cfg-1", **body}
            self.configs["cfg-1"] = config
            if self.config_status != 201:
                return httpx.Response(self.config_status, json={})
            return httpx.Response(201, json=config)
        if request.method == "GET" and path == "/api/public/score-configs":
            if self.config_list_status != 200:
                return httpx.Response(self.config_list_status, json={})
            name = request.url.params.get("name")
            for config in self.configs.values():
                if config.get("name") == name:
                    return httpx.Response(200, json={"data": [config]})
            return httpx.Response(200, json={"data": []})
        if request.method == "GET" and path.startswith(
            "/api/public/score-configs/"
        ):
            config_id = path.rsplit("/", 1)[-1]
            if config_id not in self.configs:
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(
                200, json={**self.configs[config_id], **self.config_drift}
            )
        raise AssertionError(f"unexpected request: {request.method} {path}")

    @staticmethod
    def _read_back_body(payload: dict, external_id: str) -> dict:
        # 4.76.0 v3 Score 条目形态：不含 configId/traceId/observationId/
        # sessionId/comment（目标与 config 关联不可回读）
        return {
            "id": external_id,
            "timestamp": "2026-11-11T00:00:00.000000Z",
            "source": "API",
            "name": payload.get("name"),
            "value": payload.get("value"),
            "dataType": payload.get("dataType"),
            "environment": "default",
            "projectId": "n-agent-local-project",
            "createdAt": "2026-11-11T00:00:00.000000Z",
            "updatedAt": "2026-11-11T00:00:00.000000Z",
        }

    def score_posts(self) -> list[dict]:
        return [
            json.loads(r.content)
            for r in self.requests
            if r.method == "POST" and r.url.path == "/api/public/scores"
        ]

    def config_requests(self) -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if "/score-configs" in r.url.path
        ]


class FakeConfigStore:
    """SinkConfigStore 端口的内存实现（测试专用）。"""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], dict] = {}
        self.save_count = 0

    async def get_sink_config(
        self, sink: str, project: str, definition_version: str
    ) -> dict:
        row = self.rows.get((sink, project, definition_version))
        if row is None:
            raise EvaluationNotFoundError(
                f"sink_config not found: {sink}/{project}/{definition_version}"
            )
        return row

    async def save_sink_config(self, config: dict) -> None:
        key = (
            config["sink"],
            config["project"],
            config["definition_version"],
        )
        self.rows[key] = dict(config)
        self.save_count += 1


def _definition_row(
    *,
    metric_name: str = "kb.recall_at_5",
    method: str = "deterministic",
    version: str = "v1",
    definition_id: str = "def-recall",
    output_type: str = "number",
    range_or_categories: list | tuple | None = (0, 1),
) -> dict:
    definition = {
        "definition_id": definition_id,
        "version": version,
        "metric_name": metric_name,
        "method": method,
        "output_type": output_type,
        "range_or_categories": (
            list(range_or_categories) if range_or_categories is not None else None
        ),
    }
    return {
        "definition_id": definition_id,
        "version": version,
        "metric_name": metric_name,
        "target_kind": "retrieval",
        "method": method,
        "definition_hash": "d" * 64,
        "definition": definition,
    }


def _target(**overrides) -> dict:
    target = {
        "service": "n-agent",
        "kind": "retrieval",
        "subject_id": "sub-1",
        "subject_revision": "r1",
        "trace_id": "a" * 32,
        "span_id": "b" * 16,
    }
    for key, value in overrides.items():
        if value is None:
            target.pop(key, None)
        else:
            target[key] = value
    return target


def _result(**overrides) -> dict:
    result = {
        "result_id": "res-1",
        "job_id": "job-1",
        "provenance": "automatic",
        "metric_name": "kb.recall_at_5",
        "metric_version": "v1",
        "status": "value",
        "typed_value": 0.8,
        "passed": None,
    }
    result.update(overrides)
    return result


def _context(**overrides) -> dict:
    context = {
        "result": _result(),
        "target": _target(),
        "definition": _definition_row(),
        "linkability": {"otel_enabled": True, "sampled": True},
        "projection_version": "v1",
    }
    context.update(overrides)
    return context


def _make_sink(
    api: FakeLangfuseAPI, **overrides
) -> LangfuseScoreSink:
    kwargs = {
        "base_url": BASE_URL,
        "public_key": PUBLIC_KEY,
        "secret_key": SECRET_KEY,
        "project": PROJECT,
        "config_store": FakeConfigStore(),
        "transport": httpx.MockTransport(api),
    }
    kwargs.update(overrides)
    return LangfuseScoreSink(**kwargs)


async def _project_deliverable(sink: LangfuseScoreSink, context: dict) -> dict:
    projection = await sink.project(context)
    assert projection["outcome"] == "deliverable"
    return projection


# ---------------------------------------------------------------------------
# external_id：UUIDv5(project + result_id + projection_version)
# ---------------------------------------------------------------------------


async def test_external_id_is_deterministic_uuid5():
    sink = _make_sink(FakeLangfuseAPI())
    first = await sink.project(_context())
    second = await sink.project(_context())
    # 同 result 重试复用同 ID
    assert first["external_id"] == second["external_id"]
    parsed = uuid.UUID(first["external_id"])
    assert parsed.version == 5


async def test_external_id_scoped_by_project_result_and_projection():
    base = compute_external_id(
        project=PROJECT, result_id="res-1", projection_version="v1"
    )
    assert (
        compute_external_id(
            project=PROJECT, result_id="res-1", projection_version="v1"
        )
        == base
    )
    # 重评（新 result_id）得到新 ID（不覆盖旧 Score）
    assert (
        compute_external_id(
            project=PROJECT, result_id="res-2", projection_version="v1"
        )
        != base
    )
    # 新投影版本得到新 ID
    assert (
        compute_external_id(
            project=PROJECT, result_id="res-1", projection_version="v2"
        )
        != base
    )
    # 跨 project 隔离
    assert (
        compute_external_id(
            project="other-project", result_id="res-1", projection_version="v1"
        )
        != base
    )


async def test_payload_serialization_is_byte_stable():
    sink = _make_sink(FakeLangfuseAPI())
    first = await _project_deliverable(sink, _context())
    second = await _project_deliverable(sink, _context())
    assert canonical_json_bytes(first["payload"]) == canonical_json_bytes(
        second["payload"]
    )


# ---------------------------------------------------------------------------
# POST /api/public/scores 字段映射（spec 11.1 表）
# ---------------------------------------------------------------------------


async def test_post_score_field_mapping_numeric():
    api = FakeLangfuseAPI()
    sink = _make_sink(api)
    projection = await _project_deliverable(sink, _context())
    await sink.send(projection["payload"])

    posts = api.score_posts()
    assert len(posts) == 1
    request = next(
        r
        for r in api.requests
        if r.method == "POST" and r.url.path == "/api/public/scores"
    )
    assert request.url.path == "/api/public/scores"
    # Basic 认证（public:secret 构造注入），密钥不进 URL
    assert request.headers["Authorization"].startswith("Basic ")
    assert SECRET_KEY not in str(request.url)
    body = posts[0]
    assert body["id"] == projection["external_id"]
    assert body["name"] == "kb.recall_at_5.v1"
    assert body["value"] == 0.8
    assert body["dataType"] == "NUMERIC"
    assert body["configId"] == "cfg-1"
    # observation 评分同时发送 traceId + observationId，不发送 sessionId
    assert body["traceId"] == "a" * 32
    assert body["observationId"] == "b" * 16
    assert "sessionId" not in body
    # 不伪造 source（source 由 Langfuse 决定）
    assert "source" not in body
    # comment 附 result_id 与定义 hash 的短关联说明
    assert "res-1" in body["comment"]
    assert "d" * 64 in body["comment"]


async def test_score_name_suffix_semantics():
    # deterministic -> {metric}.v{major}
    assert build_score_name(
        "kb.recall_at_5", "deterministic", "automatic", "v1"
    ) == ("kb.recall_at_5.v1")
    # llm_judge -> {metric}.judge-v{major}
    assert build_score_name(
        "rag.faithfulness", "llm_judge", "automatic", "v1"
    ) == ("rag.faithfulness.judge-v1")
    # human -> {metric}.human-v{major}
    assert build_score_name(
        "feedback.helpful", "human", "human", "v1"
    ) == ("feedback.helpful.human-v1")
    # 破坏性语义变化换 major：v2 定义得到 .v2
    assert build_score_name(
        "kb.recall_at_5", "deterministic", "automatic", "v2.1"
    ) == ("kb.recall_at_5.v2")


async def test_judge_score_name_in_request():
    api = FakeLangfuseAPI()
    sink = _make_sink(api)
    context = _context(
        result=_result(
            metric_name="rag.faithfulness", metric_version="v1", typed_value=0.5
        ),
        definition=_definition_row(
            metric_name="rag.faithfulness",
            method="llm_judge",
            definition_id="def-faith",
        ),
    )
    projection = await _project_deliverable(sink, context)
    await sink.send(projection["payload"])
    assert api.score_posts()[-1]["name"] == "rag.faithfulness.judge-v1"


async def test_human_score_name_in_request():
    api = FakeLangfuseAPI()
    sink = _make_sink(api)
    context = _context(
        result=_result(
            result_id="res-human",
            job_id=None,
            provenance="human",
            metric_name="feedback.helpful",
            metric_version="v1",
            typed_value=True,
        ),
        definition=_definition_row(
            metric_name="feedback.helpful",
            method="human",
            definition_id="def-helpful",
            output_type="boolean",
            range_or_categories=None,
        ),
    )
    projection = await _project_deliverable(sink, context)
    await sink.send(projection["payload"])
    body = api.score_posts()[-1]
    assert body["name"] == "feedback.helpful.human-v1"
    assert body["value"] == 1
    assert body["dataType"] == "BOOLEAN"


async def test_boolean_value_conversion():
    api = FakeLangfuseAPI()
    sink = _make_sink(api)
    for typed, expected in ((True, 1), (False, 0)):
        projection = await _project_deliverable(
            sink,
            _context(
                result=_result(
                    result_id=f"res-{typed}", typed_value=typed, passed=typed
                ),
                definition=_definition_row(
                    definition_id="def-bool",
                    output_type="boolean",
                    range_or_categories=None,
                ),
            ),
        )
        await sink.send(projection["payload"])
        body = api.score_posts()[-1]
        assert body["value"] == expected
        assert body["dataType"] == "BOOLEAN"


async def test_numeric_value_is_finite_float():
    api = FakeLangfuseAPI()
    sink = _make_sink(api)
    projection = await _project_deliverable(
        sink, _context(result=_result(typed_value=1))
    )
    await sink.send(projection["payload"])
    body = api.score_posts()[-1]
    assert body["value"] == 1.0
    assert isinstance(body["value"], float)


async def test_categorical_value_and_datatype():
    api = FakeLangfuseAPI()
    sink = _make_sink(api)
    projection = await _project_deliverable(
        sink,
        _context(
            result=_result(
                result_id="res-cat",
                metric_name="agent.outcome",
                metric_version="v1",
                typed_value="success",
            ),
            definition=_definition_row(
                metric_name="agent.outcome",
                definition_id="def-outcome",
                output_type="categorical",
                range_or_categories=("success", "failure"),
            ),
        ),
    )
    await sink.send(projection["payload"])
    body = api.score_posts()[-1]
    assert body["value"] == "success"
    assert body["dataType"] == "CATEGORICAL"


async def test_session_score_sends_only_session_id():
    api = FakeLangfuseAPI()
    sink = _make_sink(api)
    context = _context(
        target=_target(kind="session_feedback", session_id="sess-1"),
    )
    projection = await _project_deliverable(sink, context)
    await sink.send(projection["payload"])
    body = api.score_posts()[-1]
    assert body["sessionId"] == "sess-1"
    assert "traceId" not in body
    assert "observationId" not in body


async def test_comment_redacted_then_limited_to_2000_utf8_bytes():
    api = FakeLangfuseAPI()
    sink = _make_sink(api)
    # 脱敏：Bearer 凭据被替换；限长：超长解释截到 <=2000 UTF-8 字节且不
    # 切断多字节字符
    long_text = "Bearer abc123 " + "评" * 3000
    projection = await _project_deliverable(
        sink, _context(result=_result(explanation=long_text))
    )
    await sink.send(projection["payload"])
    comment = api.score_posts()[-1]["comment"]
    assert len(comment.encode("utf-8")) <= 2000
    assert "abc123" not in comment
    assert "[REDACTED]" in comment
    # 不出现被切断的半个字符
    comment.encode("utf-8").decode("utf-8")


# ---------------------------------------------------------------------------
# unlinked：不创建悬空 Score（spec 11.1 / 13）
# ---------------------------------------------------------------------------


async def test_unlinked_without_trace_id_sends_no_request():
    api = FakeLangfuseAPI()
    sink = _make_sink(api)
    projection = await sink.project(_context(target=_target(trace_id=None)))
    assert projection["outcome"] == "unlinked"
    assert api.requests == []


async def test_unlinked_missing_observation_identity_even_with_trace():
    api = FakeLangfuseAPI()
    sink = _make_sink(api)
    projection = await sink.project(_context(target=_target(span_id=None)))
    assert projection["outcome"] == "unlinked"
    assert api.requests == []


@pytest.mark.parametrize("flag", ["sampled", "otel_enabled"])
async def test_unlinked_when_not_sampled_or_otel_disabled(flag):
    api = FakeLangfuseAPI()
    sink = _make_sink(api)
    linkability = {"otel_enabled": True, "sampled": True}
    linkability[flag] = False
    projection = await sink.project(_context(linkability=linkability))
    assert projection["outcome"] == "unlinked"
    assert api.requests == []


async def test_unlinked_session_score_without_session_id():
    api = FakeLangfuseAPI()
    sink = _make_sink(api)
    projection = await sink.project(
        _context(target=_target(kind="session_feedback", session_id=None))
    )
    assert projection["outcome"] == "unlinked"
    assert api.requests == []


# ---------------------------------------------------------------------------
# 发送状态分类（spec 13 表）
# ---------------------------------------------------------------------------


async def test_send_2xx_is_accepted_only():
    """2xx 只代表 accepted；verified 必须回读匹配（worker 状态机覆盖）。"""
    api = FakeLangfuseAPI()
    sink = _make_sink(api)
    projection = await _project_deliverable(sink, _context())
    external_id = await sink.send(projection["payload"])
    assert external_id == projection["external_id"]


async def test_send_409_is_dead_letter_class():
    api = FakeLangfuseAPI(score_status=409)
    sink = _make_sink(api)
    projection = await _project_deliverable(sink, _context())
    with pytest.raises(ScoreDeliveryConflictError) as exc_info:
        await sink.send(projection["payload"])
    assert exc_info.value.delivery_outcome == "dead_letter"
    assert exc_info.value.error_type == "schema_conflict"


@pytest.mark.parametrize("status_code", [400, 422])
async def test_send_other_4xx_is_dead_letter_class(status_code):
    api = FakeLangfuseAPI(score_status=status_code)
    sink = _make_sink(api)
    projection = await _project_deliverable(sink, _context())
    with pytest.raises(ScoreDeliveryConflictError) as exc_info:
        await sink.send(projection["payload"])
    assert exc_info.value.delivery_outcome == "dead_letter"


async def test_send_429_is_retryable_rate_limit():
    api = FakeLangfuseAPI(score_status=429)
    sink = _make_sink(api)
    projection = await _project_deliverable(sink, _context())
    with pytest.raises(ScoreDeliveryRetryableError) as exc_info:
        await sink.send(projection["payload"])
    assert exc_info.value.delivery_outcome == "retry"
    assert exc_info.value.error_type == "rate_limit"


async def test_send_5xx_is_retryable_network():
    api = FakeLangfuseAPI(score_status=503)
    sink = _make_sink(api)
    projection = await _project_deliverable(sink, _context())
    with pytest.raises(ScoreDeliveryRetryableError) as exc_info:
        await sink.send(projection["payload"])
    assert exc_info.value.error_type == "network"


async def test_send_network_error_is_retryable():
    api = FakeLangfuseAPI(score_error=httpx.ConnectError("connection refused"))
    sink = _make_sink(api)
    projection = await _project_deliverable(sink, _context())
    with pytest.raises(ScoreDeliveryRetryableError) as exc_info:
        await sink.send(projection["payload"])
    assert exc_info.value.error_type == "network"


async def test_send_timeout_is_retryable_timeout():
    api = FakeLangfuseAPI(score_error=httpx.ReadTimeout("timed out"))
    sink = _make_sink(api)
    projection = await _project_deliverable(sink, _context())
    with pytest.raises(ScoreDeliveryRetryableError) as exc_info:
        await sink.send(projection["payload"])
    assert exc_info.value.error_type == "timeout"


async def test_read_back_absent_returns_none():
    """Score 先于 Trace 到达：v3 按 id 过滤列表为空返回 None，由
    worker 有界等待（4.76.0 events_only 无 GET-by-id 路由）。"""
    api = FakeLangfuseAPI(read_back=None)
    sink = _make_sink(api)
    assert await sink.read_back("external-1") is None


async def test_read_back_200_returns_score_with_api_source():
    api = FakeLangfuseAPI()
    sink = _make_sink(api)
    projection = await _project_deliverable(sink, _context())
    await sink.send(projection["payload"])
    read = await sink.read_back(projection["external_id"])
    assert read["id"] == projection["external_id"]
    # source 由 Langfuse 决定（API 写入显示 API）；本适配器不伪造
    assert read["source"] == "API"


async def test_read_back_5xx_is_retryable():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/public/v3/scores":
            return httpx.Response(500, json={})
        raise AssertionError(f"unexpected request: {request.url}")

    failing = LangfuseScoreSink(
        base_url=BASE_URL,
        public_key=PUBLIC_KEY,
        secret_key=SECRET_KEY,
        project=PROJECT,
        config_store=FakeConfigStore(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ScoreDeliveryRetryableError) as exc_info:
        await failing.read_back("ext-1")
    assert exc_info.value.error_type == "network"


# ---------------------------------------------------------------------------
# 回读核验：4.76.0 events_only 下 v3 条目仅含 id/name/value/dataType 等
# 字段（无 traceId/observationId/sessionId/configId），核验降级为
# id/name/dataType/value 匹配；目标一致性由 external_id 的 UUIDv5
# 确定性（project + result_id + projection_version）保证
# ---------------------------------------------------------------------------


def _matching_read_back(payload: dict) -> dict:
    return {
        "id": payload["id"],
        "name": payload["name"],
        "value": payload["value"],
        "dataType": payload["dataType"],
        "source": "API",
    }


async def test_verify_accepts_fully_matching_read_back():
    api = FakeLangfuseAPI()
    sink = _make_sink(api)
    projection = await _project_deliverable(sink, _context())
    assert await sink.verify(
        projection["payload"], _matching_read_back(projection["payload"])
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"id": "other-id"},
        {"value": 0.1},
        {"dataType": "BOOLEAN"},
        {"name": "other-name"},
    ],
)
async def test_verify_rejects_mismatched_read_back(mutation):
    api = FakeLangfuseAPI()
    sink = _make_sink(api)
    projection = await _project_deliverable(sink, _context())
    read_back = _matching_read_back(projection["payload"])
    read_back.update(mutation)
    assert not await sink.verify(projection["payload"], read_back)


async def test_verify_rejects_missing_read_back():
    """回读缺失（v3 空列表 -> None）不视为 verified。"""
    api = FakeLangfuseAPI()
    sink = _make_sink(api)
    projection = await _project_deliverable(sink, _context())
    assert not await sink.verify(projection["payload"], None)


# ---------------------------------------------------------------------------
# delete_external：ExternalDeleter 端口（spec 14 删除传播，T20/M2）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("delete_status", [202, 204])
async def test_delete_external_2xx_returns_deleted(delete_status):
    """4.76.0 删除为异步受理（202 queued）；204 兼容旧合同。"""
    api = FakeLangfuseAPI(delete_status=delete_status)
    sink = _make_sink(api)
    assert await sink.delete_external("external-1") == "deleted"


async def test_delete_external_404_returns_not_found_and_idempotent():
    """重复删除同一 ID 第二次得到 404（not_found）——与"外部对象已
    不存在"同一语义，幂等不误报。"""
    api = FakeLangfuseAPI(delete_status=404)
    sink = _make_sink(api)
    assert await sink.delete_external("external-1") == "not_found"
    assert await sink.delete_external("external-1") == "not_found"


async def test_delete_external_deleted_then_read_back_absent():
    """删除编排收敛形状：202/204 -> deleted，v3 回读空列表 -> None
    （runner 按 deleted + 缺席 -> done）。"""
    api = FakeLangfuseAPI(read_back=None)
    sink = _make_sink(api)
    outcome = await sink.delete_external("external-1")
    assert outcome == "deleted"
    assert await sink.read_back("external-1") is None


async def test_delete_external_429_is_retryable_rate_limit():
    api = FakeLangfuseAPI(delete_status=429)
    sink = _make_sink(api)
    with pytest.raises(ScoreDeliveryRetryableError) as exc_info:
        await sink.delete_external("external-1")
    assert exc_info.value.delivery_outcome == "retry"
    assert exc_info.value.error_type == "rate_limit"


async def test_delete_external_5xx_is_retryable_network():
    api = FakeLangfuseAPI(delete_status=503)
    sink = _make_sink(api)
    with pytest.raises(ScoreDeliveryRetryableError) as exc_info:
        await sink.delete_external("external-1")
    assert exc_info.value.error_type == "network"


async def test_delete_external_network_error_is_retryable():
    api = FakeLangfuseAPI(delete_error=httpx.ConnectError("refused"))
    sink = _make_sink(api)
    with pytest.raises(ScoreDeliveryRetryableError) as exc_info:
        await sink.delete_external("external-1")
    assert exc_info.value.error_type == "network"


async def test_delete_external_timeout_is_retryable_timeout():
    api = FakeLangfuseAPI(delete_error=httpx.ReadTimeout("timed out"))
    sink = _make_sink(api)
    with pytest.raises(ScoreDeliveryRetryableError) as exc_info:
        await sink.delete_external("external-1")
    assert exc_info.value.error_type == "timeout"


async def test_delete_external_401_is_conflict_auth():
    api = FakeLangfuseAPI(delete_status=401)
    sink = _make_sink(api)
    with pytest.raises(ScoreDeliveryConflictError) as exc_info:
        await sink.delete_external("external-1")
    assert exc_info.value.delivery_outcome == "dead_letter"
    assert exc_info.value.error_type == "auth"


@pytest.mark.parametrize("status_code", [400, 422])
async def test_delete_external_other_4xx_is_conflict(status_code):
    api = FakeLangfuseAPI(delete_status=status_code)
    sink = _make_sink(api)
    with pytest.raises(ScoreDeliveryConflictError):
        await sink.delete_external("external-1")


async def test_delete_external_non_204_2xx_is_unknown():
    """合同外 2xx（非 204）：删除结果未知，按可重试异常处理（删除编排
    捕获后任务退避，不宣称完成）。"""
    api = FakeLangfuseAPI(delete_status=200)
    sink = _make_sink(api)
    with pytest.raises(ScoreDeliveryRetryableError):
        await sink.delete_external("external-1")


# ---------------------------------------------------------------------------
# 密钥与 endpoint 固定（spec 11.1）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base_url",
    ["ftp://langfuse.test", "http://", "", "http://user:pass@langfuse.test"],
)
def test_constructor_rejects_unsafe_base_url(base_url):
    with pytest.raises(ValueError):
        LangfuseScoreSink(
            base_url=base_url,
            public_key=PUBLIC_KEY,
            secret_key=SECRET_KEY,
            project=PROJECT,
            config_store=FakeConfigStore(),
        )


async def test_endpoint_fixed_at_construction_ignores_input_addresses():
    """样本/浏览器传入的任意地址不改变请求 endpoint。"""
    api = FakeLangfuseAPI()
    sink = _make_sink(api)
    context = _context(
        target=_target(langfuse_url="http://evil.example/api"),
        result=_result(langfuse_url="http://evil.example/api"),
    )
    projection = await _project_deliverable(sink, context)
    await sink.send(projection["payload"])
    score_request = next(
        r
        for r in api.requests
        if r.method == "POST" and r.url.path == "/api/public/scores"
    )
    assert score_request.url.host == "langfuse.test"
    assert "evil.example" not in str(score_request.url)


async def test_secrets_never_leak_into_error_messages():
    api = FakeLangfuseAPI(score_status=503)
    sink = _make_sink(api)
    projection = await _project_deliverable(sink, _context())
    with pytest.raises(ScoreDeliveryRetryableError) as exc_info:
        await sink.send(projection["payload"])
    message = str(exc_info.value)
    assert SECRET_KEY not in message
    assert PUBLIC_KEY not in message


# ---------------------------------------------------------------------------
# ScoreConfig：definition_version -> 创建/回读/schema hash 校验
# ---------------------------------------------------------------------------


async def test_score_config_created_and_persisted_on_first_use():
    api = FakeLangfuseAPI()
    sink = _make_sink(api)
    projection = await _project_deliverable(sink, _context())
    assert projection["payload"]["configId"] == "cfg-1"
    config_requests = api.config_requests()
    # POST 创建 + GET 回读各一次
    assert [r.method for r in config_requests] == ["POST", "GET"]
    # 已持久化（含 schema_hash）；key 按 score name 维度
    config_store = sink._config_store
    row = await config_store.get_sink_config(
        "langfuse", PROJECT, "def-recall@v1:kb.recall_at_5.v1"
    )
    assert row["external_score_config_id"] == "cfg-1"
    assert row["schema_hash"]


async def test_score_config_reused_without_recreate():
    api = FakeLangfuseAPI()
    sink = _make_sink(api)
    await _project_deliverable(sink, _context())
    first_creates = len(
        [r for r in api.config_requests() if r.method == "POST"]
    )
    await _project_deliverable(sink, _context(result=_result(result_id="res-2")))
    # 复用持久化配置：不再创建（回读校验允许，POST 创建仅一次）
    assert (
        len([r for r in api.config_requests() if r.method == "POST"])
        == first_creates
        == 1
    )


async def test_score_config_schema_mismatch_is_conflict():
    api = FakeLangfuseAPI(config_drift={"dataType": "BOOLEAN"})
    config_store = FakeConfigStore()
    await config_store.save_sink_config(
        {
            "sink": "langfuse",
            "project": PROJECT,
            "definition_version": "def-recall@v1:kb.recall_at_5.v1",
            "external_score_config_id": "cfg-1",
            "schema_hash": "",
        }
    )
    sink = _make_sink(api, config_store=config_store)
    with pytest.raises(ScoreDeliveryConflictError):
        await sink.project(_context())


async def test_score_config_409_resolved_by_name():
    api = FakeLangfuseAPI(
        config_status=409, config_409_name_hit=True
    )
    sink = _make_sink(api)
    projection = await _project_deliverable(sink, _context())
    assert projection["payload"]["configId"] == "cfg-1"
    # 409 后按名解析成功并持久化
    config_store = sink._config_store
    row = await config_store.get_sink_config(
        "langfuse", PROJECT, "def-recall@v1:kb.recall_at_5.v1"
    )
    assert row["external_score_config_id"] == "cfg-1"


# ---------------------------------------------------------------------------
# 评审修正（Fix 1/2）：ScoreConfig 按 score name 维度；非有限值 dead_letter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("typed", [float("nan"), float("inf"), float("-inf")])
async def test_non_finite_numeric_rejected_as_dead_letter_class(typed):
    """NaN/±inf 是永久数据缺陷（领域层落库校验已拒绝此类值）：按
    dead_letter 类失败抛出（隔离上报、可显式补发），不得以普通异常把
    投递 wedge 在 pending 被反复重领，也不得静默丢弃。"""
    api = FakeLangfuseAPI()
    sink = _make_sink(api)
    with pytest.raises(ScoreDeliveryConflictError) as exc_info:
        await sink.project(_context(result=_result(typed_value=typed)))
    assert exc_info.value.delivery_outcome == "dead_letter"
    assert exc_info.value.error_type == "non_finite_value"
    assert api.requests == []


@pytest.mark.parametrize(
    "typed, expected_error_type",
    [
        ("", "empty_categorical"),
        (["x"], "illegal_value_type"),
        ({"k": 1}, "illegal_value_type"),
    ],
)
async def test_empty_categorical_and_illegal_type_rejected_as_dead_letter_class(
    typed, expected_error_type
):
    """空 CATEGORICAL 串与非法 typed_value 类型同为永久数据缺陷：按
    dead_letter 类失败抛出（error_type 区分缺陷类别，便于运维定位），
    不发出任何 HTTP 请求，也不得把投递 wedge 在 pending。"""
    api = FakeLangfuseAPI()
    sink = _make_sink(api)
    with pytest.raises(ScoreDeliveryConflictError) as exc_info:
        await sink.project(_context(result=_result(typed_value=typed)))
    assert exc_info.value.delivery_outcome == "dead_letter"
    assert exc_info.value.error_type == expected_error_type
    assert api.requests == []


async def test_human_score_config_name_matches_score_name():
    """human 投影的 ScoreConfig name 与 score name 一致
    （{metric}.human-v{major}），且按 name 维度独立持久化。"""
    api = FakeLangfuseAPI()
    sink = _make_sink(api)
    context = _context(
        result=_result(
            result_id="res-human",
            job_id=None,
            provenance="human",
            metric_name="feedback.helpful",
            metric_version="v1",
            typed_value=True,
        ),
        definition=_definition_row(
            metric_name="feedback.helpful",
            method="human",
            definition_id="def-helpful",
            output_type="boolean",
            range_or_categories=None,
        ),
    )
    projection = await _project_deliverable(sink, context)
    # score name 为 human 后缀
    assert projection["payload"]["name"] == "feedback.helpful.human-v1"
    # ScoreConfig 创建请求的 name 与 score name 一致
    config_post = next(
        r for r in api.requests if r.method == "POST"
        and r.url.path == "/api/public/score-configs"
    )
    assert json.loads(config_post.content)["name"] == (
        "feedback.helpful.human-v1"
    )
    # 按 score name 维度持久化（human/judge/deterministic 各自独立键）
    config_store = sink._config_store
    row = await config_store.get_sink_config(
        "langfuse", PROJECT, "def-helpful@v1:feedback.helpful.human-v1"
    )
    assert row["external_score_config_id"] == "cfg-1"
    # score 携带的 configId 即该 config
    assert projection["payload"]["configId"] == "cfg-1"


async def test_same_definition_different_provenance_gets_distinct_configs():
    """同 definition_version 下 human 与 deterministic 后缀各自独立
    ScoreConfig（config/score 配对不串）。"""
    api = FakeLangfuseAPI()
    sink = _make_sink(api)
    deterministic = _context()
    await _project_deliverable(sink, deterministic)
    human = _context(
        result=_result(
            result_id="res-human",
            job_id=None,
            provenance="human",
            metric_name="kb.recall_at_5",
            metric_version="v1",
            typed_value=0.8,
        ),
    )
    await _project_deliverable(sink, human)
    config_posts = [
        json.loads(r.content)
        for r in api.requests
        if r.method == "POST" and r.url.path == "/api/public/score-configs"
    ]
    # 两次创建：name 分别为 deterministic 与 human 后缀
    assert sorted(c["name"] for c in config_posts) == [
        "kb.recall_at_5.human-v1",
        "kb.recall_at_5.v1",
    ]
    # judge / deterministic 基线不变：deterministic 配置按 .v{major} 命名
    row = await sink._config_store.get_sink_config(
        "langfuse", PROJECT, "def-recall@v1:kb.recall_at_5.v1"
    )
    assert row["external_score_config_id"] == "cfg-1"


# ---------------------------------------------------------------------------
# 质量复审修正：comment 截断保留关联说明；auth 分类；按名解析瞬时错误
# 可重试；schema hash 漂移校验
# ---------------------------------------------------------------------------


async def test_comment_truncation_keeps_correlation_note():
    """超长解释截断后关联说明（result_id/definition_hash）必须保留在
    末尾——截断的是说明部分，不是开头 max_bytes；总长 <=2000 UTF-8
    字节且不切断多字节字符。"""
    api = FakeLangfuseAPI()
    sink = _make_sink(api)
    long_text = "说明" * 3000  # 远超 2000 字节
    projection = await _project_deliverable(
        sink, _context(result=_result(explanation=long_text))
    )
    await sink.send(projection["payload"])
    comment = api.score_posts()[-1]["comment"]
    assert len(comment.encode("utf-8")) <= 2000
    # 关联说明在末尾完整保留
    expected_note = f"result_id=res-1 definition_hash={'d' * 64}"
    assert comment.endswith(expected_note)
    # 截断只作用于解释部分：解释确实被截短
    assert long_text not in comment
    comment.encode("utf-8").decode("utf-8")


@pytest.mark.parametrize("status_code", [401, 403])
async def test_send_auth_rejection_is_dead_letter_auth(status_code):
    """401/403 认证/授权失败仍属 dead_letter 类（不可重试），但单列
    error_type=auth 供运维直接定位密钥/权限配置问题。"""
    api = FakeLangfuseAPI(score_status=status_code)
    sink = _make_sink(api)
    projection = await _project_deliverable(sink, _context())
    with pytest.raises(ScoreDeliveryConflictError) as exc_info:
        await sink.send(projection["payload"])
    assert exc_info.value.delivery_outcome == "dead_letter"
    assert exc_info.value.error_type == "auth"


async def test_read_back_auth_rejection_is_dead_letter_auth():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/public/v3/scores":
            return httpx.Response(403, json={})
        raise AssertionError(f"unexpected request: {request.url}")

    failing = LangfuseScoreSink(
        base_url=BASE_URL,
        public_key=PUBLIC_KEY,
        secret_key=SECRET_KEY,
        project=PROJECT,
        config_store=FakeConfigStore(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ScoreDeliveryConflictError) as exc_info:
        await failing.read_back("ext-1")
    assert exc_info.value.delivery_outcome == "dead_letter"
    assert exc_info.value.error_type == "auth"


@pytest.mark.parametrize(
    "status_code, expected_error_type",
    [
        (429, "rate_limit"),
        (500, "network"),
        (503, "network"),
    ],
)
async def test_resolve_config_by_name_transient_errors_are_retryable(
    status_code, expected_error_type
):
    """409 后按名解析遇 429/5xx 是瞬时错误（retry 类退避），不得误判为
    永久冲突把投递打进 dead_letter。"""
    api = FakeLangfuseAPI(
        config_status=409, config_list_status=status_code
    )
    sink = _make_sink(api)
    with pytest.raises(ScoreDeliveryRetryableError) as exc_info:
        await sink.project(_context())
    assert exc_info.value.delivery_outcome == "retry"
    assert exc_info.value.error_type == expected_error_type


async def test_resolve_config_by_name_4xx_is_conflict():
    """按名解析遇非 429 的 4xx 仍是永久拒绝（dead_letter 类）。"""
    api = FakeLangfuseAPI(config_status=409, config_list_status=400)
    sink = _make_sink(api)
    with pytest.raises(ScoreDeliveryConflictError) as exc_info:
        await sink.project(_context())
    assert exc_info.value.delivery_outcome == "dead_letter"


async def test_score_config_schema_hash_drift_is_conflict():
    """schema_hash 落库必须有消费方：远端描述符与持久化 hash 不一致
    （值等价但类型/形态漂移，如 minValue 数值变字符串，逐字段 float
    相等比较放行）-> 冲突隔离，不静默放行。"""
    api = FakeLangfuseAPI(config_drift={"minValue": "0.0"})
    sink = _make_sink(api)
    # 首次创建并持久化（hash 来自本地描述符）
    await _project_deliverable(sink, _context())
    # 复用路径回读：float("0.0") == 0.0 使逐字段比较通过，hash 校验拦截
    with pytest.raises(ScoreDeliveryConflictError) as exc_info:
        await sink.project(_context(result=_result(result_id="res-2")))
    assert exc_info.value.delivery_outcome == "dead_letter"

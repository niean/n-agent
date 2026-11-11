# tests/infrastructure/evaluation/test_delivery_worker.py
"""T9: 可恢复 delivery 调度器测试（真实 SQLiteEvaluationStore + MockTransport）。

覆盖 plan T9 补充实施要求：
- 独立 claim/lease：过期租约可重领，并发 claim 不重复；
- 冻结 payload/external_id 先登记再发送：重启后重试复用同 ID 同 payload
  （payload 序列化字节级稳定），绝不重新评分；
- 一个 sink 故障不阻塞另一个 sink；不触发重新评分；
- 状态机：2xx -> accepted（非 verified）；有界回读匹配 -> verified；
  409/4xx -> dead_letter（隔离上报）；429/5xx/网络 -> 指数退避
  1s~5min、24h 上限 -> dead_letter；管理员显式补发（dead_letter ->
  pending）复用冻结 ID/payload；
- 关闭/恢复出口：disabled 保留状态，恢复后补发未 verified 的结果；
- 仅 succeeded 结果投影：skipped/error/cancelled 不写 0 或伪类别；
- OTel 投递独立：本 worker 只处理注册的 sink 适配器，otel 行保持
  pending（pending/emitted/failed 语义由 T10 定义，SDK enqueue 不记
  delivered/verified）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from app.domain.evaluation.state import EvaluationDeliveryStatus
from app.domain.evaluation.errors import (
    EvaluationConflictError,
    EvaluationNotFoundError,
)
from app.domain.evaluation.models import canonical_json_bytes
from app.infrastructure.evaluation.langfuse_sink import (
    LangfuseScoreSink,
    ScoreDeliveryRetryableError,
)
from app.infrastructure.evaluation.store import SQLiteEvaluationStore

BASE_URL = "http://langfuse.test"
PUBLIC_KEY = "pk-test"
SECRET_KEY = "sk-test-secret"
PROJECT = "n-agent-local-project"

TRACE_ID = "a" * 32
SPAN_ID = "b" * 16


# ---------------------------------------------------------------------------
# 夹具：固定 4.76.0 形态内存 API（scores + score-configs 路由）
# ---------------------------------------------------------------------------


class LangfuseTestAPI:
    """MockTransport 处理器：可注入状态码故障与回读载荷。"""

    def __init__(
        self,
        *,
        score_status: int = 201,
        score_error: Exception | None = None,
        read_back: "str | dict | None" = "echo",
    ) -> None:
        self.score_status = score_status
        self.score_error = score_error
        self.read_back = read_back  # "echo" | dict | None(404)
        self.configs: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []
        self.posts: list[dict] = []

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
        if request.method == "POST" and path == "/api/public/score-configs":
            body = json.loads(request.content)
            config = {"id": "cfg-1", **body}
            self.configs[config["id"]] = config
            return httpx.Response(201, json=config)
        if request.method == "GET" and path == "/api/public/score-configs":
            name = request.url.params.get("name")
            return httpx.Response(
                200,
                json={"data": [c for c in self.configs.values() if c.get("name") == name]},
            )
        if request.method == "GET" and path.startswith(
            "/api/public/score-configs/"
        ):
            config_id = path.rsplit("/", 1)[-1]
            if config_id not in self.configs:
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(200, json=self.configs[config_id])
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
        }

    def score_posts(self) -> list[dict]:
        return [
            json.loads(r.content)
            for r in self.requests
            if r.method == "POST" and r.url.path == "/api/public/scores"
        ]


class FakeConfigStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], dict] = {}

    async def get_sink_config(self, sink, project, definition_version):
        row = self.rows.get((sink, project, definition_version))
        if row is None:
            raise EvaluationNotFoundError(
                f"sink_config not found: {sink}/{project}/{definition_version}"
            )
        return row

    async def save_sink_config(self, config):
        self.rows[
            (config["sink"], config["project"], config["definition_version"])
        ] = dict(config)


class FakeSink:
    """ScoreSink 端口的可编程假实现（secondary sink / 故障注入）。

    默认行为：可投递、发送成功且回读即匹配（verify True）。"""

    def __init__(
        self,
        *,
        external_id: str = "fake-ext-1",
        send_error: Exception | None = None,
    ) -> None:
        self.external_id = external_id
        self.send_error = send_error
        self.sent: list[dict] = []

    async def project(self, context: dict) -> dict:
        return {
            "outcome": "deliverable",
            "external_id": self.external_id,
            "payload": {"external_id": self.external_id},
        }

    async def send(self, payload: dict) -> str:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(dict(payload))
        return self.external_id

    async def read_back(self, external_id: str) -> dict | None:
        return {"id": external_id}

    async def verify(self, payload: dict, read_back: dict | None) -> bool:
        return (
            read_back is not None
            and read_back.get("id") == payload.get("external_id")
        )


class FakeClock:
    """可控时钟（退避/租约推进用）。"""

    def __init__(self) -> None:
        self.t = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kwargs) -> None:
        self.t = self.t + timedelta(**kwargs)


def _definition_mapping() -> dict:
    return {
        "definition_id": "def-recall",
        "version": "v1",
        "metric_name": "kb.recall_at_5",
        "target_kind": "retrieval",
        "method": "deterministic",
        "output_type": "number",
        "range_or_categories": [0, 1],
    }


def _langfuse_sink(api: LangfuseTestAPI) -> LangfuseScoreSink:
    return LangfuseScoreSink(
        base_url=BASE_URL,
        public_key=PUBLIC_KEY,
        secret_key=SECRET_KEY,
        project=PROJECT,
        config_store=FakeConfigStore(),
        transport=httpx.MockTransport(api),
    )


class DeliveryHarness:
    """每个测试一个真实 store + 种子 result/delivery。"""

    def __init__(self, tmp_path: Path, *, created_now: datetime | None = None):
        self.store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        self.now = created_now or datetime.now(timezone.utc)

    async def seed(
        self,
        *,
        result_id: str = "res-1",
        status: str = "value",
        typed_value=0.8,
        target: dict | None = None,
        sinks: tuple = (("langfuse", "v1"),),
        metric_name: str = "kb.recall_at_5",
    ) -> list[dict]:
        target = target or {
            "service": "n-agent",
            "kind": "retrieval",
            "subject_id": "sub-1",
            "subject_revision": "r1",
            "trace_id": TRACE_ID,
            "span_id": SPAN_ID,
        }
        await self.store.save_definition(
            {
                "definition_id": "def-recall",
                "version": "v1",
                "metric_name": metric_name,
                "target_kind": "retrieval",
                "method": "deterministic",
                "definition": _definition_mapping(),
            }
        )
        sample_id = await self.store.save_sample(
            {
                "service": "n-agent",
                "producer_event_id": f"event-{result_id}",
                "target": target,
            }
        )
        job_id = await self.store.save_job(
            {
                "sample_id": sample_id,
                "definition_id": "def-recall",
                "definition_version": "v1",
            }
        )
        claim = await self.store.claim_next_job("seed-worker", 300)
        result = {
            "result_id": result_id,
            "job_id": job_id,
            "provenance": "automatic",
            "metric_name": metric_name,
            "metric_version": "v1",
            "status": status,
            "skip_reason": None if status == "value" else "missing_reference",
            "typed_value": typed_value if status == "value" else None,
            "evidence_hash": "",
        }
        deliveries = [
            {"sink": sink, "projection_version": version, "payload": {"seed": 1}}
            for sink, version in sinks
        ]
        await self.store.commit_result_with_deliveries(
            result,
            deliveries,
            lease_token=claim["lease_token"],
            job_terminal_status=(
                "succeeded" if status == "value" else "skipped"
            ),
            now=self.now,
        )
        rows = await self.store.list_deliveries()
        return [r for r in rows if r["result_id"] == result_id]

    def delivery(self, rows, sink: str = "langfuse") -> dict:
        return next(r for r in rows if r["sink"] == sink)

    async def seed_human(
        self,
        *,
        result_id: str = "res-human",
        trace_id: str | None = None,
        span_id: str | None = None,
        subject_id: str = "run-human",
        tombstone: bool = False,
    ) -> str:
        """人评 result + pending delivery（同事务，spec 145/262 合同）；
        返回 result_id。target 无 trace 身份 -> 投影应判 unlinked。"""
        await self.store.save_definition(
            {
                "definition_id": "feedback.helpful",
                "version": "v1",
                "metric_name": "feedback.helpful",
                "target_kind": "agent_run",
                "method": "human",
                "definition": {
                    "definition_id": "feedback.helpful",
                    "version": "v1",
                    "metric_name": "feedback.helpful",
                    "target_kind": "agent_run",
                    "method": "human",
                    "output_type": "boolean",
                    "range_or_categories": [],
                },
            }
        )
        feedback = {
            "actor_ref": "user-1",
            "definition_id": "feedback.helpful",
            "definition_version": "v1",
            "target": {
                "service": "n-agent",
                "kind": "agent_run",
                "subject_id": subject_id,
                "subject_revision": "stop",
                "trace_id": trace_id,
                "span_id": span_id,
            },
            "value": True,
            "revision": 1,
        }
        result = {
            "result_id": result_id,
            "provenance": "human",
            "metric_name": "feedback.helpful",
            "metric_version": "v1",
            "status": "value",
            "skip_reason": None,
            "typed_value": True,
            "passed": True,
            "evidence_hash": "",
        }
        deliveries = [
            {
                "sink": "langfuse",
                "projection_version": "v1",
                "payload": {
                    "job_id": None,
                    "sample_id": None,
                    "metric_name": "feedback.helpful",
                    "metric_version": "v1",
                    "status": "value",
                    "typed_value": True,
                    "evidence_hash": "",
                },
            }
        ]
        _feedback_id, result_id = await self.store.save_feedback_with_result(
            feedback, result, deliveries
        )
        if tombstone:
            await self.store.record_deletion_tombstones_batch(
                [{
                    "subject_kind": "agent_run",
                    "subject_id": subject_id,
                    "subject_revision": "stop",
                    "reason": "seed",
                }]
            )
        return result_id


def _make_worker(store, sinks, **overrides):
    from app.infrastructure.evaluation.delivery_worker import (
        EvaluationDeliveryWorker,
    )

    kwargs = {
        "store": store,
        "sinks": sinks,
        "poll_interval_seconds": 0.01,
        "verify_poll_attempts": 2,
        "verify_poll_interval_seconds": 0.0,
        "retry_backoff_base_seconds": 1.0,
        "shutdown_grace_seconds": 0.5,
        # 显式注入可关联状态（worker 缺省是保守 False/False：忘注入 =
        # 不投递）；unlinked 行为由单测显式传 sampled=False 覆盖
        "linkability_provider": lambda: {
            "otel_enabled": True,
            "sampled": True,
        },
    }
    kwargs.update(overrides)
    return EvaluationDeliveryWorker(**kwargs)


# ---------------------------------------------------------------------------
# 状态机：pending -> accepted -> verified
# ---------------------------------------------------------------------------


async def test_happy_path_pending_to_verified(tmp_path):
    """POST 2xx -> accepted（非 verified）；有界回读匹配 -> verified。"""
    harness = DeliveryHarness(tmp_path)
    rows = await harness.seed()
    delivery_id = rows[0]["delivery_id"]
    api = LangfuseTestAPI()
    worker = _make_worker(harness.store, {"langfuse": _langfuse_sink(api)})

    assert await worker.run_once() == delivery_id
    row = await harness.store.get_delivery(delivery_id)
    assert row["status"] == "verified"
    assert row["external_id"] == api.score_posts()[0]["id"]
    # 密钥不进任何请求 URL
    assert all(SECRET_KEY not in str(r.url) for r in api.requests)


async def test_accepted_is_not_verified_without_read_back_match(tmp_path):
    """回读 404（Score 先于 Trace 到达）：accepted 不跳级 verified，
    进入退避重试等待关联。"""
    harness = DeliveryHarness(tmp_path)
    rows = await harness.seed()
    delivery_id = rows[0]["delivery_id"]
    api = LangfuseTestAPI(read_back=None)
    worker = _make_worker(harness.store, {"langfuse": _langfuse_sink(api)})
    await worker.run_once()
    row = await harness.store.get_delivery(delivery_id)
    assert row["status"] == "retry_wait"
    assert row["last_error_type"] == "verify_pending"


async def test_verify_mismatch_retries_not_verified(tmp_path):
    """回读不匹配（value 漂移）：不 verified，退避重试。"""
    harness = DeliveryHarness(tmp_path)
    rows = await harness.seed()
    delivery_id = rows[0]["delivery_id"]
    drift = {
        "id": "other-id",
        "value": 0.1,
        "dataType": "NUMERIC",
        "configId": "cfg-1",
    }
    api = LangfuseTestAPI(read_back=drift)
    worker = _make_worker(harness.store, {"langfuse": _langfuse_sink(api)})
    await worker.run_once()
    row = await harness.store.get_delivery(delivery_id)
    assert row["status"] == "retry_wait"
    assert row["status"] != "verified"


# ---------------------------------------------------------------------------
# 仅 succeeded 投影 / unlinked
# ---------------------------------------------------------------------------


async def test_skipped_result_never_scores(tmp_path):
    """skipped/error/cancelled 不写 0 或伪类别：unlinked，不发请求。"""
    harness = DeliveryHarness(tmp_path)
    rows = await harness.seed(status="skipped", typed_value=None)
    delivery_id = rows[0]["delivery_id"]
    api = LangfuseTestAPI()
    worker = _make_worker(harness.store, {"langfuse": _langfuse_sink(api)})
    await worker.run_once()
    row = await harness.store.get_delivery(delivery_id)
    assert row["status"] == "unlinked"
    assert row["last_error_type"] == "not_projected"
    assert api.requests == []


async def test_unlinked_target_without_trace_sends_no_request(tmp_path):
    harness = DeliveryHarness(tmp_path)
    target = {
        "service": "n-agent",
        "kind": "retrieval",
        "subject_id": "sub-1",
        "subject_revision": "r1",
    }
    rows = await harness.seed(target=target)
    delivery_id = rows[0]["delivery_id"]
    api = LangfuseTestAPI()
    worker = _make_worker(harness.store, {"langfuse": _langfuse_sink(api)})
    await worker.run_once()
    row = await harness.store.get_delivery(delivery_id)
    assert row["status"] == "unlinked"
    assert api.requests == []


async def test_unlinked_when_linkability_disabled(tmp_path):
    """sampled=false（即使存在 trace_id）-> unlinked，无默认可关联。"""
    harness = DeliveryHarness(tmp_path)
    rows = await harness.seed()
    delivery_id = rows[0]["delivery_id"]
    api = LangfuseTestAPI()
    worker = _make_worker(
        harness.store,
        {"langfuse": _langfuse_sink(api)},
        linkability_provider=lambda: {"otel_enabled": True, "sampled": False},
    )
    await worker.run_once()
    row = await harness.store.get_delivery(delivery_id)
    assert row["status"] == "unlinked"
    assert api.requests == []


# ---------------------------------------------------------------------------
# 一个 sink 故障不阻塞另一个；不重新评分
# ---------------------------------------------------------------------------


async def test_one_sink_failure_does_not_block_other(tmp_path):
    harness = DeliveryHarness(tmp_path)
    rows = await harness.seed(sinks=(("langfuse", "v1"), ("secondary", "v1")))
    langfuse_id = harness.delivery(rows, "langfuse")["delivery_id"]
    secondary_id = harness.delivery(rows, "secondary")["delivery_id"]
    failing = FakeSink(
        external_id="fake-1",
        send_error=ScoreDeliveryRetryableError(
            "sink down", error_type="network"
        ),
    )
    worker = _make_worker(
        harness.store,
        {"langfuse": _langfuse_sink(LangfuseTestAPI()), "secondary": failing},
    )
    await worker.run_once()
    await worker.run_once()
    langfuse_row = await harness.store.get_delivery(langfuse_id)
    secondary_row = await harness.store.get_delivery(secondary_id)
    assert langfuse_row["status"] == "verified"
    assert secondary_row["status"] == "retry_wait"


async def test_langfuse_failure_does_not_block_secondary(tmp_path):
    harness = DeliveryHarness(tmp_path)
    rows = await harness.seed(sinks=(("langfuse", "v1"), ("secondary", "v1")))
    langfuse_id = harness.delivery(rows, "langfuse")["delivery_id"]
    secondary_id = harness.delivery(rows, "secondary")["delivery_id"]
    worker = _make_worker(
        harness.store,
        {
            "langfuse": _langfuse_sink(LangfuseTestAPI(score_status=503)),
            "secondary": FakeSink(),
        },
    )
    await worker.run_once()
    await worker.run_once()
    assert (await harness.store.get_delivery(langfuse_id))["status"] == (
        "retry_wait"
    )
    assert (await harness.store.get_delivery(secondary_id))["status"] == (
        "verified"
    )


async def test_schema_conflict_goes_dead_letter(tmp_path):
    """409 schema 冲突 -> dead_letter 隔离上报，不无限重试。"""
    harness = DeliveryHarness(tmp_path)
    rows = await harness.seed()
    delivery_id = rows[0]["delivery_id"]
    api = LangfuseTestAPI(score_status=409)
    worker = _make_worker(harness.store, {"langfuse": _langfuse_sink(api)})
    await worker.run_once()
    row = await harness.store.get_delivery(delivery_id)
    assert row["status"] == "dead_letter"
    assert row["last_error_type"] == "schema_conflict"


async def test_non_finite_value_goes_dead_letter_not_wedged(tmp_path):
    """非有限值（NaN）是永久数据缺陷（领域层落库校验已拒绝此类值，
    到达此处即账本数据缺陷）：经投递调度器落 dead_letter（隔离上报、
    可显式补发），不以普通异常把投递 wedge 在 pending 被反复重领，
    也不静默丢弃。"""
    harness = DeliveryHarness(tmp_path)
    rows = await harness.seed(typed_value=float("nan"))
    delivery_id = rows[0]["delivery_id"]
    api = LangfuseTestAPI()
    worker = _make_worker(harness.store, {"langfuse": _langfuse_sink(api)})
    await worker.run_once()
    row = await harness.store.get_delivery(delivery_id)
    assert row["status"] == "dead_letter"
    assert row["last_error_type"] == "non_finite_value"
    assert api.requests == []


# ---------------------------------------------------------------------------
# 冻结 payload：同 ID 同 payload 重试；重启恢复；24h dead-letter；补发
# ---------------------------------------------------------------------------


async def test_restart_reuses_frozen_id_and_payload(tmp_path):
    """第一次失败冻结 payload；重启（新 worker 实例）后重试同 ID 同 payload
    （payload 序列化字节级稳定），不重新评分。"""
    harness = DeliveryHarness(tmp_path)
    rows = await harness.seed()
    delivery_id = rows[0]["delivery_id"]
    api_a = LangfuseTestAPI(score_status=503)
    worker_a = _make_worker(harness.store, {"langfuse": _langfuse_sink(api_a)})
    await worker_a.run_once()
    frozen = await harness.store.get_delivery(delivery_id)
    assert frozen["status"] == "retry_wait"
    assert frozen["external_id"]
    assert frozen["payload_json"]

    # 重启：全新 worker 实例 + 全新 sink（共享同一 store）；时钟推进
    # 越过退避窗口
    clock = FakeClock()
    worker_a = _make_worker(
        harness.store, {"langfuse": _langfuse_sink(api_a)}, clock=clock
    )
    clock.advance(seconds=10)
    api_b = LangfuseTestAPI()
    worker_b = _make_worker(
        harness.store, {"langfuse": _langfuse_sink(api_b)}, clock=clock
    )
    await worker_b.run_once()
    posts = api_a.score_posts() + api_b.score_posts()
    assert len(posts) == 2
    # 同 ID 同 payload（字节级稳定）
    assert posts[0] == posts[1]
    row = await harness.store.get_delivery(delivery_id)
    assert row["external_id"] == frozen["external_id"]


async def test_retry_backoff_growth_with_cap(tmp_path):
    """429/5xx 指数退避：1s -> 2s（封顶 5min 由 retry_backoff_max 保证）。"""
    clock = FakeClock()
    harness = DeliveryHarness(tmp_path)
    rows = await harness.seed()
    delivery_id = rows[0]["delivery_id"]
    api = LangfuseTestAPI(score_status=503)
    worker = _make_worker(
        harness.store,
        {"langfuse": _langfuse_sink(api)},
        retry_backoff_base_seconds=1.0,
        retry_backoff_max_seconds=300.0,
        clock=clock,
    )
    await worker.run_once()
    first = await harness.store.get_delivery(delivery_id)
    assert first["status"] == "retry_wait"
    assert first["retry_count"] == 1
    first_delay = datetime.fromisoformat(first["next_retry_at"]) - clock.t
    assert timedelta(seconds=0.9) < first_delay <= timedelta(seconds=1.1)
    # 时钟推进越过退避：重试第二次，退避翻倍（1s -> 2s）
    clock.advance(seconds=5)
    await worker.run_once()
    second = await harness.store.get_delivery(delivery_id)
    second_delay = datetime.fromisoformat(second["next_retry_at"]) - clock.t
    assert timedelta(seconds=1.9) < second_delay <= timedelta(seconds=2.1)


async def test_retry_window_expires_to_dead_letter(tmp_path):
    """自 created_at 起 24h 退避窗口超时 -> dead_letter（窗口锚点是投递
    创建时间，不是首次失败时间）。"""
    past = datetime.now(timezone.utc) - timedelta(hours=25)
    harness = DeliveryHarness(tmp_path, created_now=past)
    rows = await harness.seed()
    delivery_id = rows[0]["delivery_id"]
    api = LangfuseTestAPI(score_status=503)
    worker = _make_worker(harness.store, {"langfuse": _langfuse_sink(api)})
    await worker.run_once()
    row = await harness.store.get_delivery(delivery_id)
    assert row["status"] == "dead_letter"


async def test_dead_letter_explicit_replay_reuses_frozen_payload(tmp_path):
    """显式补发（管理员）：dead_letter -> pending，复用冻结 ID/payload，
    不新建投递、不重新评分。"""
    past = datetime.now(timezone.utc) - timedelta(hours=25)
    harness = DeliveryHarness(tmp_path, created_now=past)
    rows = await harness.seed()
    delivery_id = rows[0]["delivery_id"]
    api_a = LangfuseTestAPI(score_status=503)
    worker = _make_worker(harness.store, {"langfuse": _langfuse_sink(api_a)})
    await worker.run_once()
    assert (await harness.store.get_delivery(delivery_id))["status"] == (
        "dead_letter"
    )
    frozen = await harness.store.get_delivery(delivery_id)
    # 服务层 retry_delivery 的存储路径：dead_letter -> pending；时钟推进
    # 越过首领取租约（真实补发发生在管理员操作之后）
    await harness.store.record_delivery_transition(delivery_id, "pending")

    clock = FakeClock()
    clock.advance(seconds=400)
    api_b = LangfuseTestAPI()
    worker_b = _make_worker(
        harness.store, {"langfuse": _langfuse_sink(api_b)}, clock=clock
    )
    await worker_b.run_once()
    row = await harness.store.get_delivery(delivery_id)
    assert row["status"] == "verified"
    assert row["external_id"] == frozen["external_id"]
    posts = api_a.score_posts() + api_b.score_posts()
    assert len(posts) == 2
    assert posts[0] == posts[1]


# ---------------------------------------------------------------------------
# claim/lease 独立性
# ---------------------------------------------------------------------------


async def test_claims_are_not_duplicated_between_workers(tmp_path):
    harness = DeliveryHarness(tmp_path)
    await harness.seed()
    worker = _make_worker(harness.store, {"langfuse": FakeSink()})
    claim = await worker.claim()
    assert claim is not None
    claim_again = await worker.claim()
    assert claim_again is None  # 租约未过期，不重复领取


async def test_expired_lease_is_reclaimable_after_restart(tmp_path):
    harness = DeliveryHarness(tmp_path)
    await harness.seed()
    worker = _make_worker(
        harness.store, {"langfuse": FakeSink()}, lease_seconds=60
    )
    claim = await worker.claim()
    assert claim is not None
    # 模拟崩溃后租约过期：新 worker 以足够大的时钟偏移领取
    worker_b = _make_worker(
        harness.store,
        {"langfuse": FakeSink()},
        lease_seconds=60,
        clock=lambda: datetime.now(timezone.utc) + timedelta(seconds=120),
    )
    reclaimed = await worker_b.claim()
    assert reclaimed is not None
    assert reclaimed["delivery_id"] == claim["delivery_id"]


async def test_crash_during_verify_window_recovers(tmp_path):
    """发送后（accepted）回读窗口内崩溃：租约过期后重领，复用冻结
    payload 重投并 verified，不重新评分。"""
    harness = DeliveryHarness(tmp_path)
    rows = await harness.seed()
    delivery_id = rows[0]["delivery_id"]
    clock = FakeClock()

    class CrashAfterSendAPI(LangfuseTestAPI):
        """POST 成功（accepted 落账）后回读阶段崩溃的模拟。"""

        async def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/public/v3/scores":
                raise RuntimeError("worker crashed during verify window")
            return await super().__call__(request)

    # 第一次 worker：发送成功（POST 201 -> accepted）后回读阶段崩溃
    api_a = CrashAfterSendAPI()
    worker_a = _make_worker(
        harness.store, {"langfuse": _langfuse_sink(api_a)}, clock=clock
    )
    await worker_a.run_once()  # 不上抛（fire-and-forget 隔离）
    frozen = await harness.store.get_delivery(delivery_id)
    assert frozen["status"] == "accepted"
    assert frozen["payload_json"]
    # 重启：租约过期后重领 accepted 行，重投并 verified
    clock.advance(seconds=400)
    api_b = LangfuseTestAPI()  # POST 201 + 回读 echo 匹配
    worker_b = _make_worker(
        harness.store, {"langfuse": _langfuse_sink(api_b)}, clock=clock
    )
    await worker_b.run_once()
    row = await harness.store.get_delivery(delivery_id)
    assert row["status"] == "verified"
    assert row["external_id"] == frozen["external_id"]
    assert len(api_b.score_posts()) == 1  # 重投一次（同 ID 同 payload）


async def test_unknown_sink_deliveries_are_not_claimed(tmp_path):
    """OTel 投递独立：本 worker 只处理注册的 sink；otel 行保持 pending，
    SDK enqueue 语义（pending/emitted/failed）由 T10 定义，绝不记录
    delivered/verified。"""
    harness = DeliveryHarness(tmp_path)
    rows = await harness.seed(sinks=(("langfuse", "v1"), ("otel", "v1")))
    otel_id = harness.delivery(rows, "otel")["delivery_id"]
    api = LangfuseTestAPI()
    worker = _make_worker(harness.store, {"langfuse": _langfuse_sink(api)})
    # 只有一个可领取投递（langfuse）：otel 不被本 worker 领取
    first = await worker.claim()
    assert first is not None
    assert first["sink"] == "langfuse"
    assert (await harness.store.get_delivery(otel_id))["status"] == "pending"


# ---------------------------------------------------------------------------
# 关闭/恢复出口
# ---------------------------------------------------------------------------


async def test_disable_and_recover_sink_replays_unverified(tmp_path):
    """关闭出口 -> disabled 保留状态；恢复出口 -> 未 verified 回 pending
    补发。"""
    harness = DeliveryHarness(tmp_path)
    rows = await harness.seed(sinks=(("langfuse", "v1"), ("secondary", "v1")))
    langfuse_id = harness.delivery(rows, "langfuse")["delivery_id"]
    secondary_id = harness.delivery(rows, "secondary")["delivery_id"]
    # 回读 404：langfuse 第一次投递后停在 retry_wait（未 verified）
    # lease_seconds=0：租约即刻到期，模拟管理员操作后补发可立即领取
    api = LangfuseTestAPI(read_back=None)
    worker = _make_worker(
        harness.store,
        {"langfuse": _langfuse_sink(api), "secondary": FakeSink()},
        lease_seconds=0,
    )
    await worker.run_once()
    await worker.run_once()
    # 关闭出口：非终态投递 -> disabled
    await worker.disable_sink("langfuse")
    disabled_rows = await harness.store.list_deliveries()
    statuses = {r["delivery_id"]: r["status"] for r in disabled_rows}
    assert statuses[langfuse_id] == "disabled"
    assert statuses[secondary_id] == "verified"  # 其他 sink 不受影响
    # 恢复出口：未 verified 回 pending
    await worker.enable_sink("langfuse")
    rows_after = await harness.store.list_deliveries()
    statuses = {r["delivery_id"]: r["status"] for r in rows_after}
    assert statuses[langfuse_id] == "pending"
    # 补发：复用冻结 payload（此处回读 404 仍不 verified，但已真实补发）
    await worker.run_once()
    final = await harness.store.get_delivery(langfuse_id)
    assert final["status"] in ("accepted", "retry_wait")
    assert len(api.score_posts()) == 2  # 补发真实发生（同 ID 同 payload）


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


async def test_start_stop_is_idempotent_and_bounded(tmp_path):
    """生命周期（Phase 5 扫描修复：补断言）：双 start/stop 幂等，stop
    有界回收后台任务——循环任务终结、不泄漏 pending task、投递不丢失
    （503 故障下停留 retry_wait/pending，无异常泄漏）。"""
    harness = DeliveryHarness(tmp_path)
    await harness.seed()
    api = LangfuseTestAPI(score_status=503)
    worker = _make_worker(harness.store, {"langfuse": _langfuse_sink(api)})
    await worker.start()
    await worker.start()  # 幂等：不重复建循环
    assert worker._loop_task is not None
    loop_task = worker._loop_task
    await worker.stop()
    await worker.stop()  # 幂等
    # 有界回收证据：循环任务已终结、实例句柄清空、无未回收 task
    assert worker._loop_task is None
    assert loop_task.done()
    rows = await harness.store.list_deliveries()
    assert all(r["status"] in ("pending", "retry_wait") for r in rows)


async def test_process_isolates_unexpected_errors(tmp_path):
    """意外异常不上抛（fire-and-forget 隔离），投递留待租约过期重领。"""
    harness = DeliveryHarness(tmp_path)
    rows = await harness.seed()
    delivery_id = rows[0]["delivery_id"]

    class ExplodingSink(FakeSink):
        async def project(self, context):
            raise RuntimeError("bug")

    worker = _make_worker(harness.store, {"langfuse": ExplodingSink()})
    await worker.run_once()  # 不抛
    row = await harness.store.get_delivery(delivery_id)
    # 意外错误不 dead_letter、不 verified：保留 pending 待租约过期重领
    assert row["status"] == "pending"


# ---------------------------------------------------------------------------
# 质量复审修正：冻结后重试不重新投影；freeze CAS；dead_letter 不随出口
# 开关自动补发
# ---------------------------------------------------------------------------


class ProjectCountingSink:
    """包装 sink，统计 project 调用次数（重试不得重新投影）。"""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.project_calls = 0

    async def project(self, context):
        self.project_calls += 1
        return await self._inner.project(context)

    async def send(self, payload):
        return await self._inner.send(payload)

    async def read_back(self, external_id):
        return await self._inner.read_back(external_id)

    async def verify(self, payload, read_back):
        return await self._inner.verify(payload, read_back)


async def test_retry_reuses_frozen_payload_without_reprojection(tmp_path):
    """冻结后重试复用冻结 external_id/payload 重投：重试期间 linkability
    配置翻转（sampled=false）不得把已发送 Score 改判 unlinked，也不重新
    调用 sink.project（避免重复 ScoreConfig IO）。"""
    clock = FakeClock()
    harness = DeliveryHarness(tmp_path)
    rows = await harness.seed()
    delivery_id = rows[0]["delivery_id"]
    linkability = {"otel_enabled": True, "sampled": True}
    api_a = LangfuseTestAPI(score_status=503)
    counting_a = ProjectCountingSink(_langfuse_sink(api_a))
    worker_a = _make_worker(
        harness.store,
        {"langfuse": counting_a},
        linkability_provider=lambda: linkability,
        clock=clock,
    )
    await worker_a.run_once()
    frozen = await harness.store.get_delivery(delivery_id)
    assert frozen["status"] == "retry_wait"
    assert frozen["payload_json"]

    # linkability 翻转 + 全新 worker/sink/store 共享：重试必须仍重投
    # 冻结 payload（而不是 unlinked）
    linkability["sampled"] = False
    clock.advance(seconds=10)
    api_b = LangfuseTestAPI()
    counting_b = ProjectCountingSink(_langfuse_sink(api_b))
    worker_b = _make_worker(
        harness.store,
        {"langfuse": counting_b},
        linkability_provider=lambda: linkability,
        clock=clock,
    )
    await worker_b.run_once()
    row = await harness.store.get_delivery(delivery_id)
    assert row["status"] == "verified"
    assert counting_b.project_calls == 0  # 不重新投影
    posts = api_a.score_posts() + api_b.score_posts()
    assert len(posts) == 2
    assert posts[0] == posts[1]  # 同 ID 同 payload（冻结不可变）
    assert row["external_id"] == frozen["external_id"]


async def test_freeze_cas_conflict_retains_pending(tmp_path):
    """freeze CAS 重断言：声明行与库内冻结内容分叉（异常改写/并发写）
    -> EvaluationConflictError（意外异常语义），投递保留 pending 待人工
    排查——不丢失、不 dead_letter、不覆盖冻结内容。"""
    harness = DeliveryHarness(tmp_path)
    rows = await harness.seed()
    delivery_id = rows[0]["delivery_id"]
    clock = FakeClock()
    api = LangfuseTestAPI(read_back=None)  # 回读 404：停 retry_wait
    worker = _make_worker(
        harness.store, {"langfuse": _langfuse_sink(api)}, clock=clock
    )
    await worker.run_once()
    frozen = await harness.store.get_delivery(delivery_id)
    assert frozen["payload_json"]
    # 领取（声明行持有冻结 payload A）后，库内冻结内容被异常改写为 B
    clock.advance(seconds=10)  # 越过退避窗口，可重领
    claim = await worker.claim()
    assert claim is not None
    divergent = {
        "id": frozen["external_id"],
        "name": "kb.recall_at_5.v1",
        "value": 0.9,
        "dataType": "NUMERIC",
        "traceId": TRACE_ID,
        "observationId": SPAN_ID,
        "comment": "tampered",
        "configId": "cfg-1",
    }
    divergent_json = canonical_json_bytes(divergent).decode("utf-8")
    conn = sqlite3.connect(str(tmp_path / "evaluations.db"))
    conn.execute(
        "UPDATE evaluation_deliveries SET payload_json = ?, payload_hash = ? "
        "WHERE delivery_id = ?",
        (
            divergent_json,
            hashlib.sha256(divergent_json.encode("utf-8")).hexdigest(),
            delivery_id,
        ),
    )
    conn.commit()
    conn.close()
    # 陈旧声明行处理：freeze CAS 冲突 -> 意外异常隔离，投递保留 pending
    await worker.process(claim)
    row = await harness.store.get_delivery(delivery_id)
    assert row["status"] == "pending"
    assert row["payload_json"] == divergent_json  # 冻结内容未被覆盖


async def test_disable_skips_dead_letter_and_enable_does_not_replay_it(tmp_path):
    """dead_letter 不随出口开关自动补发：disable 只迁移非终态非
    dead_letter 投递；enable 只恢复 disabled -> pending；dead_letter 只
    响应管理员显式 replay（绕过开关即绕过 replay 合同）。"""
    harness = DeliveryHarness(tmp_path)
    await harness.seed(result_id="res-1")
    await harness.seed(result_id="res-2")
    api = LangfuseTestAPI(score_status=409)
    worker = _make_worker(harness.store, {"langfuse": _langfuse_sink(api)})
    await worker.run_once()  # 先领到的 -> dead_letter
    api.score_status = 503
    await worker.run_once()  # 后领到的 -> retry_wait
    rows = await harness.store.list_deliveries()
    dead_id = next(r["delivery_id"] for r in rows if r["status"] == "dead_letter")
    retry_id = next(r["delivery_id"] for r in rows if r["status"] == "retry_wait")

    assert await worker.disable_sink("langfuse") == 1
    statuses = {
        r["delivery_id"]: r["status"] for r in await harness.store.list_deliveries()
    }
    assert statuses[dead_id] == "dead_letter"  # dead_letter 不随开关迁移
    assert statuses[retry_id] == "disabled"
    assert await worker.enable_sink("langfuse") == 1
    statuses = {
        r["delivery_id"]: r["status"] for r in await harness.store.list_deliveries()
    }
    assert statuses[dead_id] == "dead_letter"  # enable 不自动补发
    assert statuses[retry_id] == "pending"
    # 仅显式 replay 覆盖 dead_letter（复用冻结 payload 正常重投）
    await harness.store.record_delivery_transition(dead_id, "pending")
    api.score_status = 201
    clock = FakeClock()
    clock.advance(seconds=400)  # 越过前两次领取的租约
    replay_worker = _make_worker(
        harness.store,
        {"langfuse": _langfuse_sink(api)},
        clock=clock,
    )
    for _ in range(2):  # 领取顺序不定：两次覆盖 pending 的两条投递
        await replay_worker.run_once()
    row = await harness.store.get_delivery(dead_id)
    assert row["status"] in ("accepted", "verified")


# ---------------------------------------------------------------------------
# 人评 result 投递（T22 缺口修复：submit_feedback 同事务登记 sink 投递，
# 投递 worker 对 job_id=None 的 human 结果经 feedback 解析投影输入）
# ---------------------------------------------------------------------------


async def test_human_result_without_trace_goes_unlinked(tmp_path):
    """无真实身份（无 trace_id 且非 session 评分）的人评 result：
    投递 worker 投影判 unlinked，不发请求、不建悬空 Score（spec 11.1/277）。"""
    harness = DeliveryHarness(tmp_path)
    result_id = await harness.seed_human()
    api = LangfuseTestAPI()
    worker = _make_worker(harness.store, {"langfuse": _langfuse_sink(api)})
    delivery_id = next(
        r["delivery_id"]
        for r in await harness.store.list_deliveries()
        if r["result_id"] == result_id
    )

    assert await worker.run_once() == delivery_id
    row = await harness.store.get_delivery(delivery_id)
    assert row["status"] == "unlinked"
    assert api.posts == []


async def test_human_result_with_trace_delivers_human_score(tmp_path):
    """有真实身份的人评 result：按 human 后缀 score name 正常投递
    （T9 评审修正 (a)：human/judge/deterministic 各自独立 ScoreConfig）。"""
    harness = DeliveryHarness(tmp_path)
    result_id = await harness.seed_human(trace_id=TRACE_ID, span_id=SPAN_ID)
    api = LangfuseTestAPI()
    worker = _make_worker(harness.store, {"langfuse": _langfuse_sink(api)})
    delivery_id = next(
        r["delivery_id"]
        for r in await harness.store.list_deliveries()
        if r["result_id"] == result_id
    )

    await worker.run_once()
    row = await harness.store.get_delivery(delivery_id)
    assert row["status"] == "verified", (row["status"], row.get("error_type"), row.get("last_error_type"))
    posts = api.score_posts()
    assert len(posts) == 1
    assert posts[0]["name"] == "feedback.helpful.human-v1"
    assert posts[0]["traceId"] == TRACE_ID


async def test_human_delivery_deleted_subject_goes_unlinked(tmp_path):
    """T18 门禁对等：目标主体已删除的人评投递不新发送——改判 unlinked
    （error_type=subject_deleted），不进 dead_letter、不产生重发循环。"""
    harness = DeliveryHarness(tmp_path)
    result_id = await harness.seed_human(trace_id=TRACE_ID, span_id=SPAN_ID, tombstone=True)
    api = LangfuseTestAPI()
    worker = _make_worker(harness.store, {"langfuse": _langfuse_sink(api)})
    delivery_id = next(
        r["delivery_id"]
        for r in await harness.store.list_deliveries()
        if r["result_id"] == result_id
    )

    await worker.run_once()
    row = await harness.store.get_delivery(delivery_id)
    assert row["status"] == "unlinked"
    assert row["last_error_type"] == "subject_deleted"
    assert api.posts == []


# ---------------------------------------------------------------------------
# 质量复审 M1/m3：陈旧 worker 的改判必须经 lease CAS 拒绝
# ---------------------------------------------------------------------------


async def test_retry_classification_respects_lease_cas(tmp_path):
    """M1（质量复审）：retry 分类调度必须透传 lease_token CAS——投递已被
    其他 worker 重领时，陈旧 worker 的改判被拒绝（EvaluationConflictError
    上抛，由 process 隔离语义吸收），不得把他人持有的投递改写为
    retry_wait（破坏单一持有者模型）。"""
    harness = DeliveryHarness(tmp_path)
    rows = await harness.seed()
    delivery_id = rows[0]["delivery_id"]
    stale_worker = _make_worker(
        harness.store, {"langfuse": _langfuse_sink(LangfuseTestAPI())}
    )
    # worker-a 以 0 秒租约领取（立即过期），worker-b 重领（租约刷新）
    first = await harness.store.claim_next_delivery("worker-a", 0)
    assert first is not None and first["delivery_id"] == delivery_id
    second = await harness.store.claim_next_delivery("worker-b", 300)
    assert second is not None and second["delivery_id"] == delivery_id
    assert second["lease_token"] != first["lease_token"]

    with pytest.raises(EvaluationConflictError):
        await stale_worker.handle_send_error(
            delivery_id,
            ScoreDeliveryRetryableError("sink down", error_type="network"),
            lease_token=first["lease_token"],
        )
    # worker-b 持有的投递未被改写（保持 pending，未进 retry_wait）
    row = await harness.store.get_delivery(delivery_id)
    assert row["status"] == "pending"


async def test_disable_sink_respects_stale_lease_cas(tmp_path, monkeypatch):
    """m3（质量复审）：disable_sink 对带历史租约的投递改判携带 lease CAS
    ——列表读取后投递被其他 worker 重领（TOCTOU 窗口）时跳过，不抢改。"""
    harness = DeliveryHarness(tmp_path)
    rows = await harness.seed()
    delivery_id = rows[0]["delivery_id"]
    worker = _make_worker(
        harness.store, {"langfuse": _langfuse_sink(LangfuseTestAPI())}
    )
    # 制造 retry_wait + 历史租约（schedule_delivery_retry 保留原 lease_token）
    first = await harness.store.claim_next_delivery("worker-a", 300)
    assert first is not None and first["delivery_id"] == delivery_id
    await harness.store.schedule_delivery_retry(
        delivery_id,
        error_type="network",
        next_retry_at=harness.now,
        lease_token=first["lease_token"],
    )
    row = await harness.store.get_delivery(delivery_id)
    assert row["status"] == "retry_wait"
    assert row["lease_token"] == first["lease_token"]

    # TOCTOU 注入：disable_sink 读取列表后、迁移前，worker-b 重领
    # （retry_wait 且 next_retry_at 已到期才可领取——注入到期时间）
    original_list = harness.store.list_deliveries

    async def list_then_reclaim(*args, **kwargs):
        listed = await original_list(*args, **kwargs)
        reclaimed = await harness.store.claim_next_delivery("worker-b", 300)
        assert reclaimed is not None and reclaimed["delivery_id"] == delivery_id
        return listed

    monkeypatch.setattr(harness.store, "list_deliveries", list_then_reclaim)

    disabled = await worker.disable_sink("langfuse", reason="manual")
    monkeypatch.undo()
    # 重领后的投递不得被抢改 disabled（worker-b 继续持有）
    assert disabled == 0
    row = await harness.store.get_delivery(delivery_id)
    assert row["status"] == "pending"


# ---------------------------------------------------------------------------
# #13 OTel sink：无回读能力，accepted 即投递成功终态
# ---------------------------------------------------------------------------


async def test_otel_sink_accepted_is_terminal_without_verify_poll(
    tmp_path, monkeypatch
):
    """supports_readback=False：accepted 不进 verify_pending 退避循环。"""
    from app.infrastructure.evaluation.otel_sink import OtelScoreSink

    harness = DeliveryHarness(tmp_path)
    rows = await harness.seed(sinks=(("otel", "v1"),))
    delivery_id = harness.delivery(rows, sink="otel")["delivery_id"]
    monkeypatch.setattr(
        "app.infrastructure.evaluation.otel_sink.emit_result",
        lambda **kwargs: "emitted",
    )
    worker = _make_worker(harness.store, {"otel": OtelScoreSink()})
    assert await worker.run_once() == delivery_id
    row = await harness.store.get_delivery(delivery_id)
    assert row["status"] == "accepted"
    assert row["status"] != "retry_wait"
    # 二次调度不再消费该终态投递
    assert await worker.run_once() is None
    assert (await harness.store.get_delivery(delivery_id))["status"] == (
        "accepted"
    )


async def test_otel_sink_disabled_goes_dead_letter(tmp_path, monkeypatch):
    """emit_result noop（OTel 禁用）：dead_letter 隔离，不伪造成功。"""
    from app.infrastructure.evaluation.otel_sink import OtelScoreSink

    harness = DeliveryHarness(tmp_path)
    rows = await harness.seed(sinks=(("otel", "v1"),))
    delivery_id = harness.delivery(rows, sink="otel")["delivery_id"]
    monkeypatch.setattr(
        "app.infrastructure.evaluation.otel_sink.emit_result",
        lambda **kwargs: "noop",
    )
    worker = _make_worker(harness.store, {"otel": OtelScoreSink()})
    await worker.run_once()
    row = await harness.store.get_delivery(delivery_id)
    assert row["status"] == "dead_letter"
    assert row["last_error_type"] == "otel_disabled"


async def test_otel_sink_sync_failure_goes_retry(tmp_path, monkeypatch):
    """emit_result failed：retry 类退避，复用冻结 payload 重试。"""
    from app.infrastructure.evaluation.otel_sink import OtelScoreSink

    harness = DeliveryHarness(tmp_path)
    rows = await harness.seed(sinks=(("otel", "v1"),))
    delivery_id = harness.delivery(rows, sink="otel")["delivery_id"]
    states = iter(["failed", "emitted"])
    monkeypatch.setattr(
        "app.infrastructure.evaluation.otel_sink.emit_result",
        lambda **kwargs: next(states),
    )
    clock = FakeClock()
    worker = _make_worker(
        harness.store, {"otel": OtelScoreSink()}, clock=clock
    )
    await worker.run_once()
    row = await harness.store.get_delivery(delivery_id)
    assert row["status"] == "retry_wait"
    frozen_payload = json.loads(row["payload_json"])
    assert frozen_payload["result_id"] == "res-1"  # 冻结 payload 已登记
    # 退避到期后重发（复用冻结 payload，不重新投影）
    clock.advance(seconds=10)
    assert await worker.run_once() == delivery_id
    assert (await harness.store.get_delivery(delivery_id))["status"] == (
        "accepted"
    )


async def test_accepted_terminal_for_non_readback_sink_not_reclaimed(tmp_path):
    """24 项三方审阅 #11：OTel（无回读）accepted 是成功终态——租约过期
    不得被重领迁回 pending 重发；可回读 sink（langfuse）accepted 过期
    仍可重领（崩溃恢复）。"""
    harness = DeliveryHarness(tmp_path)
    rows = await harness.seed(sinks=(("langfuse", "v1"), ("otel", "v1")))
    lf_row = harness.delivery(rows, "langfuse")
    otel_row = harness.delivery(rows, "otel")
    clock = FakeClock()

    class NoReadbackSink(FakeSink):
        supports_readback = False

    # 先各自领取并记 accepted（合法状态迁移，占用租约）
    otel_worker = _make_worker(
        harness.store, {"otel": NoReadbackSink()},
        lease_seconds=300, clock=clock,
    )
    lf_worker = _make_worker(
        harness.store, {"langfuse": FakeSink()},
        lease_seconds=300, clock=clock,
    )
    otel_claim = await otel_worker.claim()
    assert otel_claim is not None
    lf_claim = await lf_worker.claim()
    assert lf_claim is not None
    for claim in (otel_claim, lf_claim):
        await harness.store.record_delivery_transition(
            claim["delivery_id"],
            EvaluationDeliveryStatus.accepted.value,
            lease_token=claim["lease_token"],
            now=clock(),
        )
    clock.advance(seconds=400)  # 租约（300s）过期

    # otel worker：accepted 过期行不可领取（成功终态，不重发）
    assert await otel_worker.claim() is None
    # langfuse worker（可回读）：accepted 过期行重领迁回 pending
    claim = await lf_worker.claim()
    assert claim is not None
    assert claim["delivery_id"] == lf_row["delivery_id"]
    assert claim["status"] == EvaluationDeliveryStatus.pending.value

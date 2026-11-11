"""T17 Producer 接收端路由测试（N-KB 样本入口）。

覆盖 plan T17 S1 全部需求：

- 认证：服务专属 B 类凭据（Authorization 绑定 service=n-kb）；匿名/错误
  凭据 401；凭据与声明 service 不匹配 403；
- evaluation.sample.v1 校验：必需字段完整；非法 422；202 仅在持久化后
  返回；重复 event 同 hash 返回原 sample_id、异 hash 409 + 隔离计数；
- evaluation.producer-state.v1：高水位幂等（低 sequence 409 拒绝、同
  sequence 重发幂等无累加）；epoch 变更接受；心跳过期 180s 覆盖率
  unknown；
- evaluation.target-deleted.v1：写 tombstone 成功才 ACK（写失败 500）；
  迟到快照含已删文档版本拒绝（新 retrieval_id 不绕过 tombstone）；
- 容量满 503；请求大小上限 413；
- 封闭消息枚举：未知 type 422。

路由层薄：认证/校验/映射在 routes，业务语义全部复用 application 层
既有实现（submit_sample 幂等、handle_document_deleted tombstone 传播、
producer state 高水位 store UPSERT）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.evaluation_deletion import EvaluationDeletionService
from app.application.evaluation_service import (
    EvaluationCapacityUsage,
    EvaluationService,
)
from app.domain.evaluation.models import EvidenceSnapshot, EvaluationTarget
from app.infrastructure.evaluation.evidence_store import LocalEvidenceStore
from app.infrastructure.evaluation.store import SQLiteEvaluationStore
from app.interfaces.http.evaluation_routes import (
    SAMPLE_MESSAGE_TYPE,
    TARGET_DELETED_MESSAGE_TYPE,
    BearerTokenProducerAuthenticator,
    producer_ingress_counters,
    register_evaluation_producer_routes,
    reset_producer_ingress_counters,
)

SAMPLE_TYPE = SAMPLE_MESSAGE_TYPE
STATE_TYPE = "evaluation.producer-state.v1"
DELETED_TYPE = TARGET_DELETED_MESSAGE_TYPE


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeConfigService:
    """管理面路由 config_service stub（本组测试不触达配置端点）。"""

    async def get_resolved(self):  # pragma: no cover - 未触达
        raise AssertionError("unexpected config access")


class _FakeAuthenticator:
    """可编程 B 类凭据认证器（测试注入，不读真实配置）。"""

    def __init__(
        self,
        *,
        token: str = "tok-n-kb",
        service: str = "n-kb",
        disabled: bool = False,
    ) -> None:
        self.token = token
        self.service = service
        self.disabled = disabled
        self.calls: list[str | None] = []

    def authenticate(self, authorization: str | None) -> str | None:
        self.calls.append(authorization)
        if self.disabled or not self.token:
            return None
        if authorization == f"Bearer {self.token}":
            return self.service
        return None


class _RaisingDeletionService:
    """handle_document_deleted 一律抛错的假服务（tombstone 写失败场景）。"""

    async def handle_document_deleted(
        self, document_id: str, document_revision: str, *, reason: str = ""
    ) -> dict[str, Any]:
        raise RuntimeError("tombstone store unavailable")


class _ScopeOverrideASGI:
    """ASGI 包装：改写 scope 头部，模拟真实网关行为用于测试。

    - ``raw_headers``：注入原始字节头部（非 ASCII Authorization 经
      uvicorn latin-1 解码后可达认证器）；
    - ``lie_content_length``：把 Content-Length 谎报为指定值；
    - ``drop_content_length``：整条剥离 Content-Length（chunked 客户端）。
    """

    def __init__(
        self,
        app: Any,
        *,
        raw_headers: list[tuple[bytes, bytes]] | None = None,
        lie_content_length: int | None = None,
        drop_content_length: bool = False,
    ) -> None:
        self._app = app
        self._raw_headers = raw_headers or []
        self._lie_content_length = lie_content_length
        self._drop_content_length = drop_content_length

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            has_raw_auth = any(key == b"authorization" for key, _ in self._raw_headers)
            drop_keys = {b"content-length"}
            if has_raw_auth:
                drop_keys.add(b"authorization")
            headers = [
                (key, value)
                for key, value in scope["headers"]
                if key not in drop_keys
            ]
            if not self._drop_content_length:
                if self._lie_content_length is not None:
                    headers.append(
                        (b"content-length", str(self._lie_content_length).encode())
                    )
                else:
                    headers.extend(
                        (key, value)
                        for key, value in scope["headers"]
                        if key == b"content-length"
                    )
            headers.extend(self._raw_headers)
            scope = dict(scope)
            scope["headers"] = headers
        await self._app(scope, receive, send)


def _drive_asgi(
    app: Any,
    *,
    body_chunks: list[bytes],
    headers: list[tuple[bytes, bytes]],
) -> tuple[int | None, int]:
    """手动驱动 ASGI app：分片 body + 统计 receive 消费次数。

    返回 (响应 status, 实际消费的 body 分片数)——用于断言流式超限
    提前 413 且后续分片不再消费（M1 停止读取语义）。"""
    import asyncio

    path = "/evaluation/producer/messages"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "headers": headers,
    }
    messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(body_chunks) - 1,
        }
        for index, chunk in enumerate(body_chunks)
    ]
    consumed: list[int] = []

    async def receive() -> dict[str, Any]:
        consumed.append(1)
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def main() -> None:
        await app(scope, receive, send)

    asyncio.run(main())
    status = None
    for message in sent:
        if message["type"] == "http.response.start":
            status = message["status"]
    return status, len(consumed)


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _sample_message(**overrides: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "type": SAMPLE_TYPE,
        "schema_version": SAMPLE_TYPE,
        "producer_event_id": "evt-nkb-1",
        "service": "n-kb",
        "retrieval_id": "ret-1",
        "captured_at": "2026-11-11T00:00:00+00:00",
        "query": "how to deploy",
        "results": [
            {
                "document_id": "doc-1",
                "document_revision": "rev-1",
                "chunk_id": "chunk-1",
                "chunk_content_hash": "a" * 64,
                "rank": 1,
                "similarity_score": 0.9,
                "snippet": "deploy with docker",
            }
        ],
        "corpus_revision": "corpus-1",
        "trace_ref": {
            "trace_id": "b" * 32,
            "span_id": "c" * 16,
            "sampled": True,
        },
        "integrity": {
            "original_bytes": 120,
            "truncated": False,
            "incomplete": False,
            "observed_bytes": 120,
            "redaction_version": "redact-v1",
        },
    }
    msg.update(overrides)
    return msg


def _state_message(**overrides: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "type": STATE_TYPE,
        "schema_version": STATE_TYPE,
        "service": "n-kb",
        "producer_id": "n-kb-outbox-1",
        "counter_epoch": "epoch-1",
        "sequence": 5,
        "eligible_count": 100,
        "selected_count": 5,
        "captured_count": 5,
        "capture_error_count": 0,
        "queue_full_count": 0,
        "observed_at": "2026-11-11T00:00:00+00:00",
    }
    msg.update(overrides)
    return msg


def _deleted_message(**overrides: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "type": DELETED_TYPE,
        "schema_version": DELETED_TYPE,
        "service": "n-kb",
        "document_id": "doc-1",
        "document_revision": "rev-1",
        "reason": "kb document deleted",
    }
    msg.update(overrides)
    return msg


class _Harness:
    """真实 application/infrastructure 栈 + 假认证器组成的路由测试环境。"""

    def __init__(self, tmp_path: Any, authenticator: _FakeAuthenticator) -> None:
        self.authenticator = authenticator
        self.store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        self.evidence_store = LocalEvidenceStore(
            tmp_path / "evidence", self.store
        )
        self.evaluation_service = EvaluationService(self.store, enabled=True)
        self.deletion_service = EvaluationDeletionService(
            store=self.store, evidence_store=self.evidence_store
        )
        self.app = FastAPI()
        register_evaluation_producer_routes(
            self.app,
            authenticator=self.authenticator,
            evaluation_service=self.evaluation_service,
            deletion_service=self.deletion_service,
            evidence_store=self.evidence_store,
        )
        self.client = TestClient(self.app)

    def post(self, payload: Any, *, token: str | None = "tok-n-kb") -> Any:
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        data = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
        return self.client.post(
            "/evaluation/producer/messages",
            content=data,
            headers={"Content-Type": "application/json", **headers},
        )


@pytest.fixture(autouse=True)
def _isolated_ingress_counters():
    """每个测试独立复位隔离计数（Mi4：避免跨测试污染；对齐 T20 Gauge
    接线测试的复位入口 reset_producer_ingress_counters）。"""
    reset_producer_ingress_counters()
    yield
    reset_producer_ingress_counters()


@pytest.fixture()
def harness(tmp_path: Any) -> _Harness:
    return _Harness(tmp_path, _FakeAuthenticator())


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------


class TestAuthentication:
    def test_anonymous_request_rejected_401(self, harness: _Harness) -> None:
        resp = harness.post(_sample_message(), token=None)
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["code"] == "invalid_credentials"

    def test_wrong_credential_rejected_401(self, harness: _Harness) -> None:
        resp = harness.post(_sample_message(), token="wrong-token")
        assert resp.status_code == 401

    def test_valid_credential_accepted(self, harness: _Harness) -> None:
        resp = harness.post(_sample_message())
        assert resp.status_code == 202
        assert resp.json()["sample_id"]

    def test_disabled_channel_rejected_401(self, tmp_path: Any) -> None:
        # token 为空 = 通道关闭（B 类配置未提供凭据），一律 401
        harness = _Harness(tmp_path, _FakeAuthenticator(token="", disabled=True))
        resp = harness.post(_sample_message())
        assert resp.status_code == 401

    def test_declared_service_mismatch_rejected_403(self, harness: _Harness) -> None:
        # 凭据有效但声明 service 与绑定 service（n-kb）不匹配
        resp = harness.post(_sample_message(service="n-foo"))
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "service_mismatch"

    def test_state_message_service_mismatch_rejected_403(
        self, harness: _Harness
    ) -> None:
        resp = harness.post(_state_message(service="n-foo"))
        assert resp.status_code == 403

    def test_deleted_message_service_mismatch_rejected_403(
        self, harness: _Harness
    ) -> None:
        resp = harness.post(_deleted_message(service="n-foo"))
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 真实 Bearer 认证器（B 类凭据默认实现；含非 ASCII 头部编码边界）
# ---------------------------------------------------------------------------


class TestBearerTokenAuthenticator:
    def test_valid_token_returns_bound_service(self) -> None:
        auth = BearerTokenProducerAuthenticator("tok-n-kb", service="n-kb")
        assert auth.authenticate("Bearer tok-n-kb") == "n-kb"

    def test_wrong_token_returns_none(self) -> None:
        auth = BearerTokenProducerAuthenticator("tok-n-kb")
        assert auth.authenticate("Bearer nope") is None

    def test_missing_header_returns_none(self) -> None:
        auth = BearerTokenProducerAuthenticator("tok-n-kb")
        assert auth.authenticate(None) is None

    def test_empty_token_disables_channel(self) -> None:
        auth = BearerTokenProducerAuthenticator("")
        assert auth.authenticate("Bearer tok-n-kb") is None
        assert auth.authenticate(None) is None

    def test_non_ascii_header_returns_none_not_raise(self) -> None:
        # uvicorn 按 latin-1 解码头部，非 ASCII 字节可达认证器；
        # compare_digest 对非 ASCII str 抛 TypeError——必须按无效凭据
        # 返回 None（映射 401），不得以 500 泄露编码边界
        auth = BearerTokenProducerAuthenticator("tok-n-kb")
        assert auth.authenticate("Bearer caféÿ-nonascii") is None

    def test_route_rejects_non_ascii_authorization_401(self, tmp_path: Any) -> None:
        # 路由级复现：原始字节头部经 ASGI scope 注入（模拟网关 latin-1
        # 透传），非 ASCII Authorization → 401 且 unauthorized 计数递增
        harness = _Harness(
            tmp_path, BearerTokenProducerAuthenticator("tok-n-kb", service="n-kb")
        )
        before = producer_ingress_counters().get("unauthorized", 0)
        harness.client = TestClient(
            _ScopeOverrideASGI(
                harness.app,
                raw_headers=[(b"authorization", b"Bearer caf\xff-nonascii")],
            )
        )
        resp = harness.post(_sample_message())
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "invalid_credentials"
        after = producer_ingress_counters().get("unauthorized", 0)
        assert after == before + 1

    def test_route_accepts_valid_raw_header_through_gateway(
        self, tmp_path: Any
    ) -> None:
        # 网关包装本身不破坏正常认证（对照组）
        harness = _Harness(
            tmp_path, BearerTokenProducerAuthenticator("tok-n-kb", service="n-kb")
        )
        harness.client = TestClient(
            _ScopeOverrideASGI(
                harness.app, raw_headers=[(b"authorization", b"Bearer tok-n-kb")]
            )
        )
        resp = harness.post(_sample_message())
        assert resp.status_code == 202

    def test_route_real_authenticator_matrix(self, tmp_path: Any) -> None:
        harness = _Harness(
            tmp_path, BearerTokenProducerAuthenticator("tok-n-kb", service="n-kb")
        )
        assert harness.post(_sample_message(), token="tok-n-kb").status_code == 202
        assert harness.post(_sample_message(), token="wrong").status_code == 401
        assert harness.post(_sample_message(), token=None).status_code == 401
        # 声明 service 与绑定身份（n-kb）不匹配 403
        resp = harness.post(_sample_message(service="n-foo"))
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 413 字节复核绕过路径（Content-Length 谎报 / 缺失）
# ---------------------------------------------------------------------------


class TestSizeLimitBypass:
    def _harness_with_limit(self, tmp_path: Any) -> _Harness:
        harness = _Harness(tmp_path, _FakeAuthenticator())
        harness.app = FastAPI()
        register_evaluation_producer_routes(
            harness.app,
            authenticator=harness.authenticator,
            evaluation_service=harness.evaluation_service,
            deletion_service=harness.deletion_service,
            evidence_store=harness.evidence_store,
            max_message_bytes=128,
        )
        return harness

    def test_lying_content_length_still_rejected_413(self, tmp_path: Any) -> None:
        # 谎报 Content-Length=1，实际正文远超上限 → 实际字节复核拦截
        harness = self._harness_with_limit(tmp_path)
        harness.client = TestClient(
            _ScopeOverrideASGI(harness.app, lie_content_length=1)
        )
        resp = harness.post(_sample_message(query="x" * 500))
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "payload_too_large"

    def test_missing_content_length_still_rejected_413(self, tmp_path: Any) -> None:
        # chunked/无 Content-Length 客户端 → 实际字节复核拦截
        harness = self._harness_with_limit(tmp_path)
        harness.client = TestClient(
            _ScopeOverrideASGI(harness.app, drop_content_length=True)
        )
        resp = harness.post(_sample_message(query="x" * 500))
        assert resp.status_code == 413

    def test_malformed_content_length_413(self, tmp_path: Any) -> None:
        # Mi5：畸形 Content-Length（"abc"）按超限处理，不 500
        harness = self._harness_with_limit(tmp_path)
        body = json.dumps(_sample_message()).encode()
        status, _consumed = _drive_asgi(
            harness.app,
            body_chunks=[body],
            headers=[
                (b"content-type", b"application/json"),
                (b"authorization", b"Bearer tok-n-kb"),
                (b"content-length", b"abc"),
            ],
        )
        assert status == 413

    def test_chunked_oversized_stream_stops_reading(self, tmp_path: Any) -> None:
        # M1：chunked 分片流式超限——累计字节越过阈值立即 413，且后续
        # 分片不再消费（无全量缓冲的内存耗尽 DoS 面）
        harness = self._harness_with_limit(tmp_path)
        body = json.dumps(_sample_message(query="x" * 300)).encode()
        chunk_size = 100
        chunks = [body[i:i + chunk_size] for i in range(0, len(body), chunk_size)]
        assert len(chunks) >= 3
        status, consumed = _drive_asgi(
            harness.app,
            body_chunks=chunks,
            headers=[
                (b"content-type", b"application/json"),
                (b"authorization", b"Bearer tok-n-kb"),
            ],
        )
        assert status == 413
        assert consumed < len(chunks)


# ---------------------------------------------------------------------------
# evaluation.sample.v1 校验与幂等
# ---------------------------------------------------------------------------


class TestSampleValidation:
    @pytest.mark.parametrize(
        "missing_field",
        [
            "schema_version",
            "producer_event_id",
            "retrieval_id",
            "captured_at",
            "query",
            "results",
            "corpus_revision",
            "integrity",
        ],
    )
    def test_missing_required_field_422(
        self, harness: _Harness, missing_field: str
    ) -> None:
        msg = _sample_message()
        del msg[missing_field]
        resp = harness.post(msg)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "protocol_invalid"

    def test_schema_version_must_match_type_422(self, harness: _Harness) -> None:
        resp = harness.post(_sample_message(schema_version="evaluation.sample.v2"))
        assert resp.status_code == 422

    def test_empty_producer_event_id_422(self, harness: _Harness) -> None:
        resp = harness.post(_sample_message(producer_event_id=""))
        assert resp.status_code == 422

    def test_empty_retrieval_id_422(self, harness: _Harness) -> None:
        resp = harness.post(_sample_message(retrieval_id=""))
        assert resp.status_code == 422

    def test_empty_corpus_revision_422(self, harness: _Harness) -> None:
        resp = harness.post(_sample_message(corpus_revision=""))
        assert resp.status_code == 422

    def test_results_must_be_list_422(self, harness: _Harness) -> None:
        resp = harness.post(_sample_message(results="not-a-list"))
        assert resp.status_code == 422

    def test_result_entry_missing_identity_422(self, harness: _Harness) -> None:
        bad = _sample_message()
        del bad["results"][0]["document_revision"]
        resp = harness.post(bad)
        assert resp.status_code == 422

    def test_result_entry_bad_rank_422(self, harness: _Harness) -> None:
        bad = _sample_message()
        bad["results"][0]["rank"] = 0
        resp = harness.post(bad)
        assert resp.status_code == 422

    def test_empty_results_allowed(self, harness: _Harness) -> None:
        # spec 7.2：失败、空结果同样可形成样本
        resp = harness.post(_sample_message(results=[], query=""))
        assert resp.status_code == 202

    def test_invalid_trace_id_422(self, harness: _Harness) -> None:
        resp = harness.post(
            _sample_message(trace_ref={"trace_id": "xyz", "sampled": True})
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "protocol_invalid"

    def test_unknown_field_name_not_echoed(self, harness: _Harness) -> None:
        # Mi1：客户端可控键名不回显进 422 响应（无键名反射点）
        resp = harness.post(_sample_message(long_attack_key="x" * 100))
        assert resp.status_code == 422
        assert "long_attack_key" not in resp.json()["error"]["message"]

    def test_top_k_non_integer_422(self, harness: _Harness) -> None:
        resp = harness.post(_sample_message(top_k="five"))
        assert resp.status_code == 422

    def test_filters_non_object_422(self, harness: _Harness) -> None:
        resp = harness.post(_sample_message(filters=[1, 2]))
        assert resp.status_code == 422

    def test_snippet_non_string_422(self, harness: _Harness) -> None:
        bad = _sample_message()
        bad["results"][0]["snippet"] = 123
        resp = harness.post(bad)
        assert resp.status_code == 422

    def test_similarity_score_non_numeric_422(self, harness: _Harness) -> None:
        bad = _sample_message()
        bad["results"][0]["similarity_score"] = "high"
        resp = harness.post(bad)
        assert resp.status_code == 422

    def test_integrity_redaction_version_required_422(
        self, harness: _Harness
    ) -> None:
        integrity = _sample_message()["integrity"]
        del integrity["redaction_version"]
        resp = harness.post(_sample_message(integrity=integrity))
        assert resp.status_code == 422

    def test_unparseable_captured_at_422(self, harness: _Harness) -> None:
        resp = harness.post(_sample_message(captured_at="not-a-timestamp"))
        assert resp.status_code == 422

    def test_naive_captured_at_422(self, harness: _Harness) -> None:
        # spec 7.2：captured_at 是 UTC 时间；无时区偏移的 naive 时间戳拒绝
        resp = harness.post(_sample_message(captured_at="2026-11-11T00:00:00"))
        assert resp.status_code == 422

    def test_malformed_json_422(self, harness: _Harness) -> None:
        resp = harness.post("{not-json", token="tok-n-kb")
        assert resp.status_code == 422

    def test_non_object_body_422(self, harness: _Harness) -> None:
        resp = harness.post(json.dumps([1, 2, 3]), token="tok-n-kb")
        assert resp.status_code == 422

    def test_202_only_after_persistence(self, harness: _Harness) -> None:
        resp = harness.post(_sample_message())
        assert resp.status_code == 202
        # N4：202 成功响应与错误响应同样带 no-store（对齐统一契约）
        assert resp.headers["cache-control"] == "no-store"
        sample_id = resp.json()["sample_id"]
        row = _run(harness.store.load_sample(sample_id))
        assert row["producer_event_id"] == "evt-nkb-1"
        assert row["service"] == "n-kb"
        # 快照正文已持久化（evidence store），ref/hash 回填样本行
        assert row["snapshot_ref"].startswith("snapshots/")
        assert len(row["snapshot_hash"]) == 64

    def test_no_job_expansion_for_producer_sample(self, harness: _Harness) -> None:
        resp = harness.post(_sample_message())
        assert resp.json()["job_ids"] == []

    def test_snapshot_content_carries_chunks_slot(self, harness: _Harness) -> None:
        """24 项三方审阅 #23：producer 快照从 results 派生 chunks 槽位
        （kb.context_relevance 证据源），不再恒 skipped(evidence_missing)。"""
        resp = harness.post(_sample_message())
        assert resp.status_code == 202
        sample_id = resp.json()["sample_id"]
        row = _run(harness.store.load_sample(sample_id))
        snapshot = _run(harness.evidence_store.load_snapshot(row["snapshot_ref"]))
        content = snapshot.content
        assert content["chunks"] == ["deploy with docker"]
        assert content["query"] == "how to deploy"


class TestSampleIdempotency:
    def test_same_event_same_hash_returns_original_sample_id(
        self, harness: _Harness
    ) -> None:
        first = harness.post(_sample_message())
        second = harness.post(_sample_message())
        assert first.status_code == 202
        assert second.status_code == 202
        assert second.json()["sample_id"] == first.json()["sample_id"]

    def test_same_event_different_hash_409_and_isolated(
        self, harness: _Harness
    ) -> None:
        harness.post(_sample_message())
        before = producer_ingress_counters().get("event_conflict", 0)
        # 同 producer_event_id，正文不同（query 变化）→ hash 不同
        resp = harness.post(_sample_message(query="different query"))
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "event_conflict"
        after = producer_ingress_counters().get("event_conflict", 0)
        assert after == before + 1

    def test_conflict_does_not_create_new_sample(self, harness: _Harness) -> None:
        first = harness.post(_sample_message())
        harness.post(_sample_message(query="different query"))
        # 原 producer 样本行保持原内容（幂等坐标未被冲突覆盖）
        row = _run(harness.store.load_sample(first.json()["sample_id"]))
        assert row["producer_event_id"] == "evt-nkb-1"
        # 原消息重放仍返回原 sample_id
        replay = harness.post(_sample_message())
        assert replay.status_code == 202
        assert replay.json()["sample_id"] == first.json()["sample_id"]


# ---------------------------------------------------------------------------
# evaluation.producer-state.v1 高水位
# ---------------------------------------------------------------------------


class TestProducerState:
    def test_first_state_accepted_and_persisted(self, harness: _Harness) -> None:
        resp = harness.post(_state_message())
        assert resp.status_code == 202
        body = resp.json()
        assert body["applied"] is True
        row = _run(
            harness.store.get_producer_state("n-kb-outbox-1", "epoch-1")
        )
        assert row["sequence"] == 5
        assert row["eligible_count"] == 100
        assert row["captured_count"] == 5

    def test_same_sequence_resend_idempotent_no_accumulation(
        self, harness: _Harness
    ) -> None:
        harness.post(_state_message())
        # 同 sequence 重发（网络重试场景）：幂等接受、不累加计数
        resp = harness.post(_state_message())
        assert resp.status_code == 202
        body = resp.json()
        assert body["applied"] is False
        row = _run(
            harness.store.get_producer_state("n-kb-outbox-1", "epoch-1")
        )
        assert row["eligible_count"] == 100
        assert row["selected_count"] == 5

    def test_lower_sequence_rejected_409(self, harness: _Harness) -> None:
        harness.post(_state_message())
        resp = harness.post(_state_message(sequence=3))
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "stale_sequence"

    def test_epoch_change_accepted(self, harness: _Harness) -> None:
        harness.post(_state_message())
        # 计数存储重建必须换 epoch：新 epoch 序列从 0 重新开始也接受
        resp = harness.post(_state_message(counter_epoch="epoch-2", sequence=0))
        assert resp.status_code == 202
        assert resp.json()["applied"] is True
        row = _run(harness.store.get_producer_state("n-kb-outbox-1", "epoch-2"))
        assert row["sequence"] == 0

    def test_higher_sequence_updates_counters(self, harness: _Harness) -> None:
        harness.post(_state_message())
        resp = harness.post(
            _state_message(
                sequence=6,
                eligible_count=120,
                captured_count=6,
                capture_error_count=1,
            )
        )
        assert resp.status_code == 202
        row = _run(harness.store.get_producer_state("n-kb-outbox-1", "epoch-1"))
        assert row["sequence"] == 6
        assert row["eligible_count"] == 120
        assert row["capture_error_count"] == 1

    def test_heartbeat_expired_180s_coverage_unknown(self, harness: _Harness) -> None:
        stale_at = (
            datetime.now(timezone.utc) - timedelta(seconds=200)
        ).isoformat()
        resp = harness.post(_state_message(observed_at=stale_at))
        assert resp.status_code == 202
        body = resp.json()
        assert body["heartbeat_stale"] is True
        assert body["coverage"] == "unknown"

    def test_fresh_heartbeat_coverage_current(self, harness: _Harness) -> None:
        fresh_at = (
            datetime.now(timezone.utc) - timedelta(seconds=10)
        ).isoformat()
        resp = harness.post(_state_message(observed_at=fresh_at))
        assert resp.status_code == 202
        body = resp.json()
        assert body["heartbeat_stale"] is False
        assert body["coverage"] == "current"

    def test_missing_counts_422(self, harness: _Harness) -> None:
        msg = _state_message()
        del msg["eligible_count"]
        resp = harness.post(msg)
        assert resp.status_code == 422

    def test_negative_sequence_422(self, harness: _Harness) -> None:
        resp = harness.post(_state_message(sequence=-1))
        assert resp.status_code == 422

    def test_unparseable_observed_at_422(self, harness: _Harness) -> None:
        resp = harness.post(_state_message(observed_at="not-a-timestamp"))
        assert resp.status_code == 422

    def test_unknown_field_rejected_422(self, harness: _Harness) -> None:
        # Mi5：state 白名单拒绝路径
        resp = harness.post(_state_message(unknown_field=1))
        assert resp.status_code == 422
        assert "unknown_field" not in resp.json()["error"]["message"]

    def test_garbage_count_values_422(self, harness: _Harness) -> None:
        # Mi3：路由层防御纵深——计数非负整数校验（domain 共用助手）
        for bad in (True, "5", -1, None):
            resp = harness.post(_state_message(eligible_count=bad))
            assert resp.status_code == 422


# ---------------------------------------------------------------------------
# evaluation.target-deleted.v1
# ---------------------------------------------------------------------------


class TestTargetDeleted:
    def test_tombstone_written_then_ack(self, harness: _Harness) -> None:
        # 先有带文档证据目标的生产者样本
        sample_resp = harness.post(_sample_message())
        assert sample_resp.status_code == 202
        resp = harness.post(_deleted_message())
        assert resp.status_code == 202
        body = resp.json()
        assert body["tombstone"] is True
        report = body["cascade"]
        assert report["samples"] >= 1
        assert report["revoked_job_ids"] == []

    def test_late_sample_with_deleted_revision_rejected(
        self, harness: _Harness
    ) -> None:
        harness.post(_sample_message())
        resp = harness.post(_deleted_message())
        assert resp.status_code == 202
        # 迟到样本：新 retrieval_id + 新 producer_event_id，但携带已删文档
        # 版本 → tombstone 门禁拒绝，不能绕过
        late = _sample_message(
            producer_event_id="evt-late-1",
            retrieval_id="ret-late-1",
        )
        resp = harness.post(late)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "deletion_conflict"

    def test_tombstone_write_failure_no_ack(self, tmp_path: Any) -> None:
        harness = _Harness(tmp_path, _FakeAuthenticator())
        harness.deletion_service = _RaisingDeletionService()
        # 重建 app：注入 tombstone 写失败的删除服务
        harness.app = FastAPI()
        register_evaluation_producer_routes(
            harness.app,
            authenticator=harness.authenticator,
            evaluation_service=harness.evaluation_service,
            deletion_service=harness.deletion_service,
            evidence_store=harness.evidence_store,
        )
        harness.client = TestClient(harness.app)
        resp = harness.post(_deleted_message())
        assert resp.status_code == 500
        body = resp.json()
        assert body["error"]["code"] == "tombstone_write_failed"

    def test_missing_document_id_422(self, harness: _Harness) -> None:
        msg = _deleted_message()
        del msg["document_id"]
        resp = harness.post(msg)
        assert resp.status_code == 422

    def test_missing_document_revision_422(self, harness: _Harness) -> None:
        resp = harness.post(_deleted_message(document_revision=""))
        assert resp.status_code == 422

    def test_unknown_field_rejected_422(self, harness: _Harness) -> None:
        # Mi5：deleted 白名单拒绝路径
        resp = harness.post(_deleted_message(unknown_field=1))
        assert resp.status_code == 422
        assert "unknown_field" not in resp.json()["error"]["message"]


# ---------------------------------------------------------------------------
# 容量与请求大小
# ---------------------------------------------------------------------------


class TestCapacityAndSize:
    def test_capacity_full_503(self, tmp_path: Any) -> None:
        harness = _Harness(tmp_path, _FakeAuthenticator())

        async def _at_limit() -> EvaluationCapacityUsage:
            return EvaluationCapacityUsage(
                pending_jobs=10**9, evidence_bytes=0
            )

        harness.evaluation_service = EvaluationService(
            harness.store, enabled=True, capacity_usage_provider=_at_limit
        )
        harness.app = FastAPI()
        register_evaluation_producer_routes(
            harness.app,
            authenticator=harness.authenticator,
            evaluation_service=harness.evaluation_service,
            deletion_service=harness.deletion_service,
            evidence_store=harness.evidence_store,
        )
        harness.client = TestClient(harness.app)
        resp = harness.post(_sample_message())
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "capacity_full"

    def test_oversized_body_413(self, tmp_path: Any) -> None:
        harness = _Harness(tmp_path, _FakeAuthenticator())
        harness.app = FastAPI()
        register_evaluation_producer_routes(
            harness.app,
            authenticator=harness.authenticator,
            evaluation_service=harness.evaluation_service,
            deletion_service=harness.deletion_service,
            evidence_store=harness.evidence_store,
            max_message_bytes=128,
        )
        harness.client = TestClient(harness.app)
        big = _sample_message(query="x" * 500)
        resp = harness.post(big)
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "payload_too_large"


# ---------------------------------------------------------------------------
# 封闭消息枚举
# ---------------------------------------------------------------------------


class TestClosedMessageEnum:
    def test_unknown_type_422(self, harness: _Harness) -> None:
        msg = _sample_message()
        msg["type"] = "evaluation.judge.v9"
        resp = harness.post(msg)
        assert resp.status_code == 422
        assert "封闭枚举" in resp.json()["error"]["message"]

    def test_missing_type_422(self, harness: _Harness) -> None:
        msg = _sample_message()
        del msg["type"]
        resp = harness.post(msg)
        assert resp.status_code == 422

    def test_no_arbitrary_source_field_accepted(self, harness: _Harness) -> None:
        # 拒绝任意 source / 脚本路径 / Judge URL 之类的开放字段语义：
        # 消息体不提供任何可执行/可寻址的自由字段，未知顶层键按 422 拒绝
        msg = _sample_message()
        msg["source"] = "file:///tmp/evil.py"
        resp = harness.post(msg)
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 测试工具
# ---------------------------------------------------------------------------


def _run(awaitable: Any) -> Any:
    import asyncio

    return asyncio.run(awaitable)


# ---------------------------------------------------------------------------
# T21 管理面路由（/chat/evaluations/*）
# ---------------------------------------------------------------------------

from app.application.evaluation_admin import EvaluationAdminService
from app.application.evaluation_analytics import EvaluationAnalyticsService
from app.application.evaluation_annotation import AnnotationService
from app.application.evaluation_config import EvaluationConfigService
from app.application.evaluation_gate import (
    default_gate_policy,
    gate_policy_content,
)
from app.application.evaluation_review import EvaluationReviewService
from app.config import Settings
from app.domain.evaluation.models import EvaluatorDefinition
from app.infrastructure.evaluation.config_store import (
    SqliteEvaluationConfigStore,
)
from app.interfaces.http.evaluation_routes import (
    register_evaluation_management_routes,
)


class _FakeAnnotationEvidenceReader:
    """annotation 服务 evidence_reader 假实现：忽略 ref 返回固定正文
    （正文为非空 Mapping 即按 available 处理，与 service 判定一致）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def load_snapshot(self, snapshot_ref: str) -> EvidenceSnapshot:
        self.calls += 1
        return EvidenceSnapshot(
            content={"query": "如何重置密码", "answer": "在设置页点击重置"},
            redaction_version="v1",
        )


class _ManagementHarness:
    """管理面路由测试环境：真实 store/service/config/review 栈。

    producer 端点不注册（管理面独立注册函数），凭据同源保护沿用
    Dashboard 约束（无 CORS、同文件错误契约）。"""

    def __init__(self, tmp_path: Any) -> None:
        self.store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        self.evidence_store = LocalEvidenceStore(
            tmp_path / "evidence", self.store
        )
        self.evaluation_service = EvaluationService(self.store, enabled=True)
        settings = Settings(
            provider_base_url="",
            provider_api_key="",
            provider_model="",
            sqlite_path=str(tmp_path / "sessions.db"),
            workspace_root=str(tmp_path),
            evaluation_worker_max_concurrency=4,
        )
        self.config_service = EvaluationConfigService(
            settings,
            SqliteEvaluationConfigStore(str(tmp_path / "config.db")),
        )
        self.datasets_root = tmp_path / "datasets"
        self.review_service = EvaluationReviewService(
            store=self.store,
            datasets_root=str(self.datasets_root),
            evidence_reader=self.evidence_store,
        )
        # Task 9：评估工作台管理面三服务（admin/annotation/analytics）
        self.annotation_evidence_reader = _FakeAnnotationEvidenceReader()
        self.admin_service = EvaluationAdminService(
            self.store,
            judge_ready=lambda: True,
            evaluation_service=self.evaluation_service,
        )
        self.annotation_service = AnnotationService(
            self.store,
            self.evaluation_service,
            evidence_reader=self.annotation_evidence_reader,
            agent_run_lookup=None,
            # 读侧 tombstone 门禁与组合根同构（store.has_deletion_tombstone）
            tombstone_reader=(
                lambda kind, subject_id, subject_revision: self.store.has_deletion_tombstone(
                    kind, subject_id, subject_revision
                )
            ),
        )
        self.analytics_service = EvaluationAnalyticsService(self.store)
        self.app = FastAPI()
        register_evaluation_management_routes(
            self.app,
            evaluation_service=self.evaluation_service,
            config_service=self.config_service,
            review_service=self.review_service,
            evaluation_admin_service=self.admin_service,
            evaluation_annotation_service=self.annotation_service,
            evaluation_analytics_service=self.analytics_service,
        )
        self.client = TestClient(self.app)
        self.tmp_path = tmp_path

    # -- seeding ------------------------------------------------------------

    async def _seed_definition(self, definition_id="def-answer-relevance", version="v1") -> None:
        from app.domain.evaluation.models import EvaluatorDefinition

        definition = EvaluatorDefinition(
            definition_id=definition_id,
            version=version,
            metric_name="answer_relevance",
            target_kind="agent_run",
            method="deterministic",
            required_fields=["query", "answer"],
            evidence_policy="snapshot_required",
            output_type="number",
            range_or_categories=(0, 1),
            direction="higher_is_better",
            material_hash={"rubric": "b" * 64},
            judge_model=None,
            judge_params={},
            timeout_seconds=60,
            budget=None,
            ci_admissible=False,
        )
        await self.evaluation_service.register_definition(definition)

    async def _seed_sample_job(
        self,
        *,
        sample_id_suffix="",
        experiment_id=None,
        target_kind="retrieval",
        subject_id=None,
        definition_id="def-answer-relevance",
        definition_version="v1",
        metric_name="answer_relevance",
        result_status="value",
        typed_value=0.2,
        passed=False,
    ) -> dict:
        """种子一个自动结果（sample -> job -> claim -> commit）。

        每次只留一个 queued job：save 后立即 claim/commit，保证
        claim_next_job 领到的就是刚保存的 job（lease 不串位）。"""
        from app.domain.evaluation.models import EvaluationTarget

        sid = subject_id or f"search-1{sample_id_suffix}"
        target = EvaluationTarget(
            service="n-agent",
            kind=target_kind,
            subject_id=sid,
            subject_revision="rev-1",
            retrieval_id=sid,
            trace_id="a" * 32,
            span_id="b" * 16,
        )
        sample_id = await self.store.save_sample({
            "service": "n-agent",
            "producer_event_id": f"evt-1{sample_id_suffix}",
            "target": {
                "service": target.service,
                "kind": target.kind,
                "subject_id": target.subject_id,
                "subject_revision": target.subject_revision,
            },
            "snapshot_ref": "",
            "snapshot_hash": "a" * 64,
            "schema_version": "evaluation.sample.v1",
            "captured_at": "2026-11-10T00:00:00+00:00",
            "sample_probability": 1.0,
            "sampling_policy_version": "sp-1",
            "evidence_targets": [],
        })
        job_id = await self.store.save_job({
            "sample_id": sample_id,
            "definition_id": definition_id,
            "definition_version": definition_version,
            "requested_by": "eval-worker",
            "idempotency_key": f"idem-1{sample_id_suffix}",
            "experiment_id": experiment_id,
        })
        claimed = await self.store.claim_next_job("w", 300)
        assert claimed is not None and claimed["job_id"] == job_id
        result_id = await self.store.commit_result_with_deliveries(
            {
                "job_id": job_id,
                "provenance": "automatic",
                "metric_name": metric_name,
                "metric_version": definition_version,
                "status": result_status,
                "typed_value": typed_value,
                "passed": passed,
                "evidence_hash": "c" * 64,
            },
            (
                [{
                    "sink": "langfuse",
                    "projection_version": "score-v1",
                    "payload": {"value": typed_value},
                }]
                if result_status == "value"
                else []
            ),
            lease_token=claimed["lease_token"],
            job_terminal_status=(
                "succeeded" if result_status == "value" else "error"
            ),
        )
        return {
            "sample_id": sample_id,
            "job_id": job_id,
            "result_id": result_id,
        }

    async def _seed_annotation_result(
        self,
        *,
        subject_id="run-1",
        subject_revision="stop",
        event="evt-a1",
        snapshot_ref="snapshots/abc.json",
        typed_value=0.2,
    ) -> str:
        """播种一条 automatic + agent_run 来源结果（quality.judge 定义 +
        sample + job + result），返回 result_id（人工标注用例的来源）。"""
        definition = EvaluatorDefinition(
            definition_id="quality.judge",
            version="v1",
            metric_name="quality.judge",
            target_kind="agent_run",
            method="llm_judge",
            required_fields=("query", "answer"),
            evidence_policy="snapshot_required",
            output_type="number",
            range_or_categories=(0.0, 1.0),
            direction="higher_is_better",
            material_hash={"rubric": "b" * 64},
            judge_model="gpt-judge",
            judge_params={"rubric": "按相关性 0-1 打分"},
            timeout_seconds=60,
            budget=None,
            ci_admissible=True,
        )
        await self.evaluation_service.register_definition(definition)
        sample_id = await self.store.save_sample({
            "service": "n-agent",
            "producer_event_id": event,
            "target": {
                "service": "n-agent",
                "kind": "agent_run",
                "subject_id": subject_id,
                "subject_revision": subject_revision,
                "run_id": subject_id,
                "session_id": "s-1",
            },
            "snapshot_ref": snapshot_ref,
            "snapshot_hash": "c" * 64,
        })
        job_id = await self.store.save_job({
            "sample_id": sample_id,
            "definition_id": "quality.judge",
            "definition_version": "v1",
        })
        return await self.store.save_result({
            "job_id": job_id,
            "provenance": "automatic",
            "metric_name": "quality.judge",
            "status": "value",
            "typed_value": typed_value,
        })

    async def _seed_target_tombstone(
        self, subject_id: str, subject_revision: str
    ) -> str:
        """写入 agent_run 目标删除 tombstone（读侧门禁命中场景）。"""
        return await self.store.record_deletion_tombstone({
            "subject_kind": "agent_run",
            "subject_id": subject_id,
            "subject_revision": subject_revision,
        })

    # -- http helpers -------------------------------------------------------

    def get(self, path: str) -> Any:
        return self.client.get(path)

    def post(self, path: str, payload: Any) -> Any:
        return self.client.post(
            path, content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )

    def post_raw(self, path: str, raw: bytes) -> Any:
        """POST 原始字节体（NaN/Infinity 等非标准 JSON 字面量场景）。"""
        return self.client.post(
            path, content=raw,
            headers={"Content-Type": "application/json"},
        )

    def patch(self, path: str, payload: Any) -> Any:
        return self.client.patch(
            path, content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )


@pytest.fixture()
def mgmt(tmp_path: Any) -> _ManagementHarness:
    harness = _ManagementHarness(tmp_path)
    _run(harness._seed_definition())
    return harness


class TestManagementExperiments:
    def test_post_experiment_returns_202_and_id(self, mgmt: _ManagementHarness) -> None:
        resp = mgmt.post("/chat/evaluations/experiments", {
            "dataset_id": "ds-1",
            "dataset_version": "v1",
            "dataset_hash": "a" * 64,
            "target_config": {"provider": "x"},
            "evaluator_set": [
                {"definition_id": "def-answer-relevance", "definition_version": "v1"},
            ],
            "mode": "execute",
        })
        assert resp.status_code == 202
        body = resp.json()
        assert body["experiment_id"]
        assert body["status"] == "created"

    def test_post_experiment_invalid_mode_422(self, mgmt: _ManagementHarness) -> None:
        resp = mgmt.post("/chat/evaluations/experiments", {
            "dataset_id": "ds-1",
            "dataset_version": "v1",
            "dataset_hash": "a" * 64,
            "target_config": {},
            "evaluator_set": [
                {"definition_id": "def-answer-relevance", "definition_version": "v1"},
            ],
            "mode": "teleport",
        })
        assert resp.status_code == 422

    def test_post_experiment_unknown_definition_404(self, mgmt: _ManagementHarness) -> None:
        resp = mgmt.post("/chat/evaluations/experiments", {
            "dataset_id": "ds-1",
            "dataset_version": "v1",
            "dataset_hash": "a" * 64,
            "target_config": {},
            "evaluator_set": [
                {"definition_id": "missing", "definition_version": "v1"},
            ],
        })
        assert resp.status_code == 404

    def test_post_experiment_bad_hash_422(self, mgmt: _ManagementHarness) -> None:
        resp = mgmt.post("/chat/evaluations/experiments", {
            "dataset_id": "ds-1",
            "dataset_version": "v1",
            "dataset_hash": "not-a-hash",
            "target_config": {},
            "evaluator_set": [
                {"definition_id": "def-answer-relevance", "definition_version": "v1"},
            ],
        })
        assert resp.status_code == 422

    def test_get_experiment_list_contains_created(self, mgmt: _ManagementHarness) -> None:
        created = mgmt.post("/chat/evaluations/experiments", {
            "dataset_id": "ds-1",
            "dataset_version": "v1",
            "dataset_hash": "a" * 64,
            "target_config": {},
            "evaluator_set": [
                {"definition_id": "def-answer-relevance", "definition_version": "v1"},
            ],
            "manifest_json": '{"dataset_id": "ds-1"}',
        }).json()
        resp = mgmt.get("/chat/evaluations/experiments")
        assert resp.status_code == 200
        items = resp.json()["items"]
        ids = [row["experiment_id"] for row in items]
        assert created["experiment_id"] in ids
        # Mi1：列表行剥离 manifest_json（正文只在详情接口透出）
        assert all("manifest_json" not in row for row in items)

    def test_get_experiment_detail_overview_shape(self, mgmt: _ManagementHarness) -> None:
        created = mgmt.post("/chat/evaluations/experiments", {
            "dataset_id": "ds-1",
            "dataset_version": "v1",
            "dataset_hash": "a" * 64,
            "target_config": {},
            "evaluator_set": [
                {"definition_id": "def-answer-relevance", "definition_version": "v1"},
            ],
        }).json()
        _run(mgmt._seed_sample_job(experiment_id=created["experiment_id"]))
        resp = mgmt.get(
            f"/chat/evaluations/experiments/{created['experiment_id']}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["experiment"]["status"] == "created"
        assert body["coverage"]["results_total"] == 1
        assert "answer_relevance" in body["distribution"]
        assert body["delivery_status"]["langfuse"]["pending"] == 1
        assert "gate_report_ref" in body
        assert body["baseline"] is None

    def test_get_experiment_unknown_404(self, mgmt: _ManagementHarness) -> None:
        resp = mgmt.get("/chat/evaluations/experiments/nope")
        assert resp.status_code == 404


class TestManagementResults:
    def test_results_page_excludes_content_and_paginates(
        self, mgmt: _ManagementHarness
    ) -> None:
        _run(mgmt._seed_sample_job(sample_id_suffix="-1"))
        _run(mgmt._seed_sample_job(sample_id_suffix="-2"))
        resp = mgmt.get("/chat/evaluations/results?limit=1")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 1
        assert body["next_cursor"]
        # 不默认返回正文：条目不含快照正文/解释全文
        item = body["items"][0]
        assert "snapshot_content" not in item
        assert "explanation" not in item
        page2 = mgmt.get(
            f"/chat/evaluations/results?limit=1&cursor={body['next_cursor']}"
        )
        assert page2.status_code == 200
        assert len(page2.json()["items"]) == 1
        assert (
            page2.json()["items"][0]["result_id"]
            != item["result_id"]
        )

    def test_results_status_filter(self, mgmt: _ManagementHarness) -> None:
        _run(mgmt._seed_sample_job(sample_id_suffix="-1"))
        resp = mgmt.get("/chat/evaluations/results?status=error")
        assert resp.status_code == 200
        assert resp.json()["items"] == []
        resp = mgmt.get("/chat/evaluations/results?status=value")
        assert len(resp.json()["items"]) == 1

    def test_result_detail_404_unknown(self, mgmt: _ManagementHarness) -> None:
        resp = mgmt.get("/chat/evaluations/results/missing")
        assert resp.status_code == 404

    def test_result_detail_evidence_and_delivery_links(
        self, mgmt: _ManagementHarness
    ) -> None:
        seeded = _run(mgmt._seed_sample_job())
        resp = mgmt.get(f"/chat/evaluations/results/{seeded['result_id']}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["result"]["result_id"] == seeded["result_id"]
        assert body["evidence_summary"]["snapshot_hash"] == "a" * 64
        assert body["deliveries"][0]["sink"] == "langfuse"
        assert body["job"]["job_id"] == seeded["job_id"]


class TestManagementFeedback:
    def test_feedback_submit_and_idempotent_reuse(self, mgmt: _ManagementHarness) -> None:
        _run(
            mgmt.evaluation_service.register_feedback_helpful_definition()
        )
        payload = {
            "target": {
                "service": "n-agent",
                "kind": "agent_run",
                "subject_id": "run-1",
                "subject_revision": "rev-1",
            },
            "definition_id": "feedback.helpful",
            "definition_version": "v1",
            "value": True,
            "request_key": "fb-key-1",
        }
        resp = mgmt.post("/chat/evaluations/feedback", payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["revision"] == 1
        assert body["reused"] is False
        # 同幂等键同 payload：复用
        again = mgmt.post("/chat/evaluations/feedback", payload)
        assert again.status_code == 200
        assert again.json()["reused"] is True
        assert again.json()["feedback_id"] == body["feedback_id"]

    def test_feedback_revision_cas_conflict_409(self, mgmt: _ManagementHarness) -> None:
        _run(
            mgmt.evaluation_service.register_feedback_helpful_definition()
        )
        target = {
            "service": "n-agent",
            "kind": "agent_run",
            "subject_id": "run-1",
            "subject_revision": "rev-1",
        }
        first = mgmt.post("/chat/evaluations/feedback", {
            "target": target,
            "definition_id": "feedback.helpful",
            "definition_version": "v1",
            "value": True,
        })
        assert first.status_code == 200
        # expected_revision=0 但当前已是 1：CAS 冲突
        conflict = mgmt.post("/chat/evaluations/feedback", {
            "target": target,
            "definition_id": "feedback.helpful",
            "definition_version": "v1",
            "value": False,
            "expected_revision": 0,
        })
        assert conflict.status_code == 409

    def test_feedback_unknown_definition_404(self, mgmt: _ManagementHarness) -> None:
        resp = mgmt.post("/chat/evaluations/feedback", {
            "target": {
                "service": "n-agent",
                "kind": "agent_run",
                "subject_id": "run-1",
                "subject_revision": "rev-1",
            },
            "definition_id": "missing",
            "definition_version": "v1",
            "value": True,
        })
        assert resp.status_code == 404

    def test_feedback_bad_value_422(self, mgmt: _ManagementHarness) -> None:
        _run(
            mgmt.evaluation_service.register_feedback_helpful_definition()
        )
        resp = mgmt.post("/chat/evaluations/feedback", {
            "target": {
                "service": "n-agent",
                "kind": "agent_run",
                "subject_id": "run-1",
                "subject_revision": "rev-1",
            },
            "definition_id": "feedback.helpful",
            "definition_version": "v1",
            "value": "yes-not-bool",
        })
        assert resp.status_code == 422


class TestManagementConfig:
    def test_get_config_returns_resolved_with_version(
        self, mgmt: _ManagementHarness
    ) -> None:
        resp = mgmt.get("/chat/evaluations/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == 0
        assert "evaluation_enabled" in body["config"]
        assert body["overridden_fields"] == []

    def test_patch_config_cas_and_validation(self, mgmt: _ManagementHarness) -> None:
        ok = mgmt.patch("/chat/evaluations/config", {
            "patch": {"evaluation_enabled": True},
            "expected_version": 0,
        })
        assert ok.status_code == 200
        assert ok.json()["version"] == 1
        # CAS 冲突
        conflict = mgmt.patch("/chat/evaluations/config", {
            "patch": {"evaluation_enabled": False},
            "expected_version": 0,
        })
        assert conflict.status_code == 409
        # 未知键
        unknown = mgmt.patch("/chat/evaluations/config", {
            "patch": {"nope": 1},
            "expected_version": 1,
        })
        assert unknown.status_code == 422
        # 越界（超过 B 类上限）
        over = mgmt.patch("/chat/evaluations/config", {
            "patch": {"evaluation_worker_concurrency": 999},
            "expected_version": 1,
        })
        assert over.status_code == 422


class TestManagementDeliveryRetry:
    def test_retry_dead_letter_delivery(self, mgmt: _ManagementHarness) -> None:
        async def _seed():
            seeded = await mgmt._seed_sample_job()
            deliveries = await mgmt.store.list_deliveries(seeded["result_id"])
            await mgmt.store.record_delivery_transition(
                deliveries[0]["delivery_id"], "dead_letter"
            )
            return seeded

        seeded = _run(_seed())
        resp = mgmt.post("/chat/evaluations/deliveries/retry", {
            "sink": "langfuse",
            "result_id": seeded["result_id"],
        })
        assert resp.status_code == 200
        assert resp.json()["delivery"]["status"] == "pending"

    def test_retry_terminal_status_409(self, mgmt: _ManagementHarness) -> None:
        async def _seed():
            seeded = await mgmt._seed_sample_job()
            deliveries = await mgmt.store.list_deliveries(seeded["result_id"])
            # 终态 verified 需经 accepted 回读（pending -> accepted -> verified）
            await mgmt.store.record_delivery_transition(
                deliveries[0]["delivery_id"], "accepted"
            )
            await mgmt.store.record_delivery_transition(
                deliveries[0]["delivery_id"], "verified"
            )
            return seeded

        seeded = _run(_seed())
        resp = mgmt.post("/chat/evaluations/deliveries/retry", {
            "sink": "langfuse",
            "result_id": seeded["result_id"],
        })
        assert resp.status_code == 409

    def test_retry_unknown_sink_404(self, mgmt: _ManagementHarness) -> None:
        seeded = _run(mgmt._seed_sample_job())
        resp = mgmt.post("/chat/evaluations/deliveries/retry", {
            "sink": "nope",
            "result_id": seeded["result_id"],
        })
        assert resp.status_code == 404

    def test_retry_missing_body_fields_422(self, mgmt: _ManagementHarness) -> None:
        resp = mgmt.post("/chat/evaluations/deliveries/retry", {"sink": "langfuse"})
        assert resp.status_code == 422


class TestManagementReviewQueue:
    async def _seed_variants(self, mgmt: _ManagementHarness) -> dict:
        from app.application.evaluation_service import SubmitFeedbackRequest
        from app.domain.evaluation.models import EvaluationTarget

        # low_score：numeric 0.2（低于阈值 0.5，默认种子即 0.2）
        low = await mgmt._seed_sample_job(sample_id_suffix="-low")
        # anomalous：error 状态 result（无值、无投递）
        error = await mgmt._seed_sample_job(
            sample_id_suffix="-err",
            result_status="error",
            typed_value=None,
            passed=None,
        )
        # disputed：同 definition（feedback.helpful）+ subject（agent_run
        # run-1）上自动 False 与人评 True 冲突
        await mgmt.evaluation_service.register_feedback_helpful_definition()
        disputed = await mgmt._seed_sample_job(
            sample_id_suffix="-disputed",
            target_kind="agent_run",
            subject_id="run-1",
            definition_id="feedback.helpful",
            metric_name="feedback.helpful",
            typed_value=False,
            passed=False,
        )
        human = await mgmt.evaluation_service.submit_feedback(
            "dashboard",
            SubmitFeedbackRequest(
                target=EvaluationTarget(
                    service="n-agent",
                    kind="agent_run",
                    subject_id="run-1",
                    subject_revision="rev-1",
                ),
                definition_id="feedback.helpful",
                definition_version="v1",
                value=True,
            ),
        )
        return {"low": low, "error": error, "disputed": disputed, "human": human}

    def test_low_score_filter(self, mgmt: _ManagementHarness) -> None:
        seeded = _run(self._seed_variants(mgmt))
        resp = mgmt.get(
            "/chat/evaluations/review-queue?reason=low_score&threshold=0.5"
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        ids = [item["result_id"] for item in items]
        assert all(item["reason"] == "low_score" for item in items)
        # 低分 numeric 0.2 与 boolean passed=False 都属低分
        assert seeded["low"]["result_id"] in ids
        assert seeded["disputed"]["result_id"] in ids

    def test_anomalous_filter_includes_error(self, mgmt: _ManagementHarness) -> None:
        seeded = _run(self._seed_variants(mgmt))
        resp = mgmt.get("/chat/evaluations/review-queue?reason=anomalous")
        assert resp.status_code == 200
        items = resp.json()["items"]
        ids = [item["result_id"] for item in items]
        assert seeded["error"]["result_id"] in ids
        assert all(item["reason"] == "anomalous" for item in items)

    def test_disputed_filter_matches_conflicting_human(
        self, mgmt: _ManagementHarness
    ) -> None:
        seeded = _run(self._seed_variants(mgmt))
        resp = mgmt.get("/chat/evaluations/review-queue?reason=disputed")
        assert resp.status_code == 200
        items = resp.json()["items"]
        ids = [item["result_id"] for item in items]
        # 自动 boolean False 与人评 True（同 definition+subject）冲突
        assert seeded["disputed"]["result_id"] in ids
        assert all(item["reason"] == "disputed" for item in items)

    def test_invalid_reason_422(self, mgmt: _ManagementHarness) -> None:
        resp = mgmt.get("/chat/evaluations/review-queue?reason=bogus")
        assert resp.status_code == 422


class TestManagementReviewExport:
    def _fixture_files(self) -> dict:
        import hashlib

        fixture_name = "fixtures/case-1.json"
        rubric_name = "rubrics/case-1.json"
        fixture_body = b'{"question": "q1"}'
        rubric_body = b'{"criteria": "answer mentions deployment"}'
        return {
            "fixture_name": fixture_name,
            "fixture_body": fixture_body,
            "fixture_sha256": hashlib.sha256(fixture_body).hexdigest(),
            "rubric_name": rubric_name,
            "rubric_body": rubric_body,
            "rubric_sha256": hashlib.sha256(rubric_body).hexdigest(),
        }

    def _case_payload(self, files: dict, **overrides) -> dict:
        case = {
            "case_id": "case-1",
            "input": {"query": "what is deployment"},
            "target_kind": "agent_run",
            "expected_output": "deployed successfully",
            "fixture": files["fixture_name"],
            "fixture_sha256": files["fixture_sha256"],
            "rubric_ref": files["rubric_name"],
            "rubric_sha256": files["rubric_sha256"],
            "slice": "review",
        }
        case.update(overrides)
        return case

    def _write_fixture_files(self, mgmt: _ManagementHarness, files: dict) -> None:
        """fixture/rubric 落盘到导出数据集目录（manifest 同级，相对路径
        解析基准为 manifest 所在目录）。"""
        dataset_dir = mgmt.datasets_root / "ds-review" / "v1"
        fixture_path = dataset_dir / files["fixture_name"]
        fixture_path.parent.mkdir(parents=True, exist_ok=True)
        fixture_path.write_bytes(files["fixture_body"])
        rubric_path = dataset_dir / files["rubric_name"]
        rubric_path.parent.mkdir(parents=True, exist_ok=True)
        rubric_path.write_bytes(files["rubric_body"])

    def test_export_writes_valid_manifest_and_audit(
        self, mgmt: _ManagementHarness
    ) -> None:
        files = self._fixture_files()
        self._write_fixture_files(mgmt, files)
        resp = mgmt.post("/chat/evaluations/review-queue/export", {
            "dataset_id": "ds-review",
            "dataset_version": "v1",
            "target_kind": "agent_run",
            "approved_by": "dashboard",
            "source_result_ids": ["r-1", "r-2"],
            "cases": [self._case_payload(files)],
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["dataset_hash"]
        manifest_path = mgmt.datasets_root / "ds-review" / "v1" / "manifest.json"
        assert manifest_path.is_file()
        # T12 校验：落盘 manifest 可被权威加载器加载
        from app.application.evaluation_dataset import load_dataset_manifest

        manifest = load_dataset_manifest(manifest_path)
        assert manifest.dataset_id == "ds-review"
        # 审计记录落库
        audits = _run(mgmt.review_service.list_export_audits())
        assert len(audits) == 1
        assert audits[0]["approved_by"] == "dashboard"
        assert audits[0]["source_result_ids"] == ["r-1", "r-2"]

    def test_export_rejects_identity_leak_422(
        self, mgmt: _ManagementHarness
    ) -> None:
        files = self._fixture_files()
        self._write_fixture_files(mgmt, files)
        resp = mgmt.post("/chat/evaluations/review-queue/export", {
            "dataset_id": "ds-review",
            "dataset_version": "v1",
            "target_kind": "agent_run",
            "approved_by": "dashboard",
            "source_result_ids": [],
            "cases": [self._case_payload(
                files, input={"query": "q", "session_id": "sess-123"}
            )],
        })
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "identity_leak"

    def test_export_rejects_missing_truth_422(
        self, mgmt: _ManagementHarness
    ) -> None:
        files = self._fixture_files()
        self._write_fixture_files(mgmt, files)
        case = self._case_payload(files)
        del case["expected_output"]
        resp = mgmt.post("/chat/evaluations/review-queue/export", {
            "dataset_id": "ds-review",
            "dataset_version": "v1",
            "target_kind": "agent_run",
            "approved_by": "dashboard",
            "source_result_ids": [],
            "cases": [case],
        })
        assert resp.status_code == 422

    def test_export_rejects_missing_fixture_422(
        self, mgmt: _ManagementHarness
    ) -> None:
        files = self._fixture_files()
        resp = mgmt.post("/chat/evaluations/review-queue/export", {
            "dataset_id": "ds-review",
            "dataset_version": "v1",
            "target_kind": "agent_run",
            "approved_by": "dashboard",
            "source_result_ids": [],
            "cases": [self._case_payload(files)],
        })
        assert resp.status_code == 422

    def test_export_rejects_path_escape_422(
        self, mgmt: _ManagementHarness
    ) -> None:
        # 路径卫生：dataset_id/fixture 引用不得逃逸出数据集根目录
        files = self._fixture_files()
        self._write_fixture_files(mgmt, files)
        resp = mgmt.post("/chat/evaluations/review-queue/export", {
            "dataset_id": "../escape",
            "dataset_version": "v1",
            "target_kind": "agent_run",
            "approved_by": "dashboard",
            "source_result_ids": [],
            "cases": [self._case_payload(files)],
        })
        assert resp.status_code == 422
        case = self._case_payload(files)
        case["fixture"] = "../../etc/passwd"
        resp = mgmt.post("/chat/evaluations/review-queue/export", {
            "dataset_id": "ds-review",
            "dataset_version": "v2",
            "target_kind": "agent_run",
            "approved_by": "dashboard",
            "source_result_ids": [],
            "cases": [case],
        })
        assert resp.status_code == 422

    def test_export_rejects_absolute_fixture_path_422(
        self, mgmt: _ManagementHarness
    ) -> None:
        # m3：fixture 引用绝对路径（逃逸出数据集目录）必须拒绝
        files = self._fixture_files()
        self._write_fixture_files(mgmt, files)
        case = self._case_payload(files)
        case["fixture"] = "/etc/passwd"
        resp = mgmt.post("/chat/evaluations/review-queue/export", {
            "dataset_id": "ds-review",
            "dataset_version": "v-abs",
            "target_kind": "agent_run",
            "approved_by": "dashboard",
            "source_result_ids": [],
            "cases": [case],
        })
        assert resp.status_code == 422

    def test_export_inline_content_end_to_end(
        self, mgmt: _ManagementHarness
    ) -> None:
        """M1 端到端锁定：UI 内联 payload（fixture_content + 每 case
        审核人金标 + 可选 rubric）→ 导出成功 → manifest 与 fixture/
        rubric 落盘 → T12 权威加载器回读通过。"""
        resp = mgmt.post("/chat/evaluations/review-queue/export", {
            "dataset_id": "ds-ui",
            "dataset_version": "v1",
            "target_kind": "agent_run",
            "approved_by": "dashboard",
            "source_result_ids": ["r-1"],
            "cases": [
                {
                    "case_id": "case-1",
                    "input": {"query": "what is deployment"},
                    "target_kind": "agent_run",
                    "expected_output": "deployed successfully",
                    "fixture_content": {"query": "what is deployment"},
                    "rubric_content": {"criteria": "答案须提及部署结果"},
                    "slice": "review",
                },
                {
                    "case_id": "case-2",
                    "input": {"query": "who enabled it"},
                    "target_kind": "agent_run",
                    "expected_output": "operator alice",
                    "fixture_content": {"query": "who enabled it"},
                    # 无 rubric_content：服务端写默认评阅说明（非金标）
                    "slice": "review",
                },
            ],
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["case_count"] == 2
        dataset_dir = mgmt.datasets_root / "ds-ui" / "v1"
        manifest_path = dataset_dir / "manifest.json"
        assert manifest_path.is_file()
        assert (dataset_dir / "fixtures" / "case-1.json").is_file()
        assert (dataset_dir / "fixtures" / "case-2.json").is_file()
        assert (dataset_dir / "rubrics" / "case-1.json").is_file()
        assert (dataset_dir / "rubrics" / "case-2.json").is_file()
        # manifest 不含内联传输字段与身份键
        import json as _json

        manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
        for case in manifest["cases"]:
            assert "fixture_content" not in case
            assert "rubric_content" not in case
            assert "expected_output" in case
        # T12 权威回读
        from app.application.evaluation_dataset import load_dataset_manifest

        assert load_dataset_manifest(manifest_path).dataset_id == "ds-ui"
        # 每案金标逐个落 manifest（非单值套全批）
        outputs = {c["case_id"]: c["expected_output"] for c in manifest["cases"]}
        assert outputs["case-1"] == "deployed successfully"
        assert outputs["case-2"] == "operator alice"
        # 失败不落半截：非法 case（缺金标）→ 整体 422 且同版本无 manifest
        bad = mgmt.post("/chat/evaluations/review-queue/export", {
            "dataset_id": "ds-ui",
            "dataset_version": "v2",
            "target_kind": "agent_run",
            "approved_by": "dashboard",
            "source_result_ids": [],
            "cases": [
                {
                    "case_id": "case-1",
                    "input": {"query": "q"},
                    "target_kind": "agent_run",
                    "fixture_content": {"query": "q"},
                    "slice": "review",
                },
            ],
        })
        assert bad.status_code == 422
        assert not (mgmt.datasets_root / "ds-ui" / "v2" / "manifest.json").exists()
        assert not (mgmt.datasets_root / "ds-ui" / "v2" / "fixtures").exists()

    def test_export_error_messages_no_paths_or_sha(
        self, mgmt: _ManagementHarness
    ) -> None:
        """m2：导出 422 文案不含服务器绝对路径与 sha 值。"""
        files = self._fixture_files()
        self._write_fixture_files(mgmt, files)
        case = self._case_payload(files)
        case["fixture_sha256"] = "e" * 64
        resp = mgmt.post("/chat/evaluations/review-queue/export", {
            "dataset_id": "ds-review",
            "dataset_version": "v-msg",
            "target_kind": "agent_run",
            "approved_by": "dashboard",
            "source_result_ids": [],
            "cases": [case],
        })
        assert resp.status_code == 422
        message = resp.json()["error"]["message"]
        assert str(mgmt.datasets_root) not in message
        assert "e" * 64 not in message
        assert "sha" not in message.lower() or "一致" in message

    def test_export_duplicate_version_409(self, mgmt: _ManagementHarness) -> None:
        files = self._fixture_files()
        self._write_fixture_files(mgmt, files)
        payload = {
            "dataset_id": "ds-review",
            "dataset_version": "v1",
            "target_kind": "agent_run",
            "approved_by": "dashboard",
            "source_result_ids": [],
            "cases": [self._case_payload(files)],
        }
        assert mgmt.post("/chat/evaluations/review-queue/export", payload).status_code == 200
        again = mgmt.post("/chat/evaluations/review-queue/export", payload)
        assert again.status_code == 409

    def test_export_audit_failure_rolls_back_files(
        self, mgmt: _ManagementHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M1：审计写入纳入回滚保护——save_export_audit 失败时本次落盘
        的 manifest 与内联文件被清理（避免"有数据集无审计"且同版本重试
        409 永锁），调用方收到 500（Mi2 兜底契约）。"""

        async def _boom(*args: object, **kwargs: object) -> str:
            raise RuntimeError("audit sink down")

        monkeypatch.setattr(
            mgmt.review_service._store, "save_export_audit", _boom
        )
        resp = mgmt.post("/chat/evaluations/review-queue/export", {
            "dataset_id": "ds-audit-fail",
            "dataset_version": "v1",
            "target_kind": "agent_run",
            "approved_by": "dashboard",
            "source_result_ids": [],
            "cases": [
                {
                    "case_id": "case-1",
                    "input": {"query": "q"},
                    "target_kind": "agent_run",
                    "expected_output": "truth",
                    "fixture_content": {"query": "q"},
                    "slice": "review",
                },
            ],
        })
        assert resp.status_code == 500
        assert resp.json()["error"]["code"] == "internal_error"
        dataset_dir = mgmt.datasets_root / "ds-audit-fail" / "v1"
        assert not (dataset_dir / "manifest.json").exists()
        assert not (dataset_dir / "fixtures" / "case-1.json").exists()
        # N2：回滚后不残留空目录（fixtures/rubrics/数据集目录全清）
        assert not dataset_dir.exists()
        audits = _run(mgmt.review_service.list_export_audits(
            dataset_id="ds-audit-fail"
        ))
        assert audits == []

    def test_export_rejects_identity_key_variants_422(
        self, mgmt: _ManagementHarness
    ) -> None:
        """M2：身份键黑名单归一化匹配——Session_ID/sessionId/traceId/
        RUN_ID 等大小写与分隔符变体同被拦截，无法绕过。"""
        for key in ("Session_ID", "sessionId", "traceId", "RUN_ID", "run-id"):
            resp = mgmt.post("/chat/evaluations/review-queue/export", {
                "dataset_id": "ds-variant",
                "dataset_version": "v1",
                "target_kind": "agent_run",
                "approved_by": "dashboard",
                "source_result_ids": [],
                "cases": [{
                    "case_id": "case-1",
                    "input": {"query": "q", key: "leak-value"},
                    "target_kind": "agent_run",
                    "expected_output": "truth",
                    "fixture_content": {"query": "q"},
                    "slice": "review",
                }],
            })
            assert resp.status_code == 422, (key, resp.text)
            assert resp.json()["error"]["code"] == "identity_leak"

    def test_export_duplicate_case_id_422_cleans_files(
        self, mgmt: _ManagementHarness
    ) -> None:
        """Mi11a：重复 case_id → manifest 落盘后 T12 回读 fail-fast →
        422，且本次写入的 fixture 与 manifest 已被真实清理（非半截
        数据集），不残留空目录。"""
        cases = [
            {
                "case_id": "case-1",
                "input": {"query": "q1"},
                "target_kind": "agent_run",
                "expected_output": "truth-1",
                "fixture_content": {"query": "q1"},
                "slice": "review",
            },
            {
                "case_id": "case-1",
                "input": {"query": "q2"},
                "target_kind": "agent_run",
                "expected_output": "truth-2",
                "fixture_content": {"query": "q2"},
                "slice": "review",
            },
        ]
        resp = mgmt.post("/chat/evaluations/review-queue/export", {
            "dataset_id": "ds-dup",
            "dataset_version": "v1",
            "target_kind": "agent_run",
            "approved_by": "dashboard",
            "source_result_ids": [],
            "cases": cases,
        })
        assert resp.status_code == 422
        dataset_dir = mgmt.datasets_root / "ds-dup" / "v1"
        assert not (dataset_dir / "manifest.json").exists()
        assert not (dataset_dir / "fixtures" / "case-1.json").exists()
        assert not dataset_dir.exists()

    def test_low_score_scan_cap(self, mgmt: _ManagementHarness,
                                monkeypatch: pytest.MonkeyPatch) -> None:
        """Mi11b：low_score 扫描有结果数上限（模块常量封顶），存在大量
        低分行时不会无界分页扫描。"""
        from app.application import evaluation_review as review_mod

        monkeypatch.setattr(review_mod, "_QUEUE_MAX_RESULTS", 3)
        monkeypatch.setattr(review_mod, "_QUEUE_PAGE_SIZE", 2)

        async def _seed() -> None:
            for index in range(5):
                await mgmt._seed_sample_job(
                    sample_id_suffix=f"-low-{index}",
                    subject_id=f"low-subject-{index}",
                    typed_value=0.1,
                )

        _run(_seed())
        items = _run(mgmt.review_service.review_queue(
            reason="low_score", threshold=0.9, limit=50
        ))
        assert len(items) <= 3
        assert all(item["reason"] == "low_score" for item in items)

    def test_export_empty_cases_422(self, mgmt: _ManagementHarness) -> None:
        resp = mgmt.post("/chat/evaluations/review-queue/export", {
            "dataset_id": "ds-review",
            "dataset_version": "v1",
            "target_kind": "agent_run",
            "approved_by": "dashboard",
            "source_result_ids": [],
            "cases": [],
        })
        assert resp.status_code == 422


class TestManagementOverview:
    def test_overview_aggregates_capacity_budget_producer(
        self, mgmt: _ManagementHarness
    ) -> None:
        """M3a/e：概览聚合端点（容量积压/日预算/producer 心跳/低覆盖/
        证据保留天数）。"""
        resp = mgmt.get("/chat/evaluations/overview")
        assert resp.status_code == 200
        body = resp.json()
        assert "pending_jobs" in body["capacity"]
        assert "max_pending_jobs" in body["capacity"]
        assert "date_key" in body["budget"]
        assert body["budget"]["daily_call_ceiling"] == 0
        assert body["producer_states"] == []
        assert body["experiment_results"] == {}
        assert body["evidence_retention_days"] == 7



# ---------------------------------------------------------------------------
# 30 项人工评审修复轮（#5/#15/#30）
# ---------------------------------------------------------------------------


class TestProducerSampleDefinitionsResolution:
    def test_sample_resolves_definitions_and_expands_jobs(self, tmp_path: Any) -> None:
        """#5：producer sample 按目标种类解析已注册自动定义并展开作业——
        生产入口不再产生"只落样本不开作业"的死样本。"""
        harness = _Harness(tmp_path, _FakeAuthenticator())
        import asyncio as _asyncio

        _asyncio.run(
            harness.evaluation_service.register_preset_definitions("judge-m")
        )
        # T5 新语义（spec 4.3）：注册默认 disabled，usage 显式启用后才
        # 展开作业（存量定义由一次性回填启用；此处模拟启用后的线上状态）
        async def _enable_all() -> None:
            for row in await harness.store.list_definitions_with_usage():
                if row["method"] == "human" or row["usage"] is None:
                    continue
                await harness.store.upsert_definition_usage(
                    row["definition_id"], row["version"],
                    online_enabled=True,
                    expected_revision=row["usage"]["revision"],
                    updated_by="test",
                )

        _asyncio.run(_enable_all())
        msg = _sample_message(producer_event_id="evt-defs-1")
        resp = harness.post(msg)
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert len(body["job_ids"]) >= 1

    def test_sample_without_definitions_persists_sample_only(self, tmp_path: Any) -> None:
        harness = _Harness(tmp_path, _FakeAuthenticator())
        msg = _sample_message(producer_event_id="evt-nodefs-1")
        resp = harness.post(msg)
        assert resp.status_code == 202
        assert resp.json()["job_ids"] == []


class TestFeedbackTargetVerification:
    def _management_harness(self, tmp_path: Any, lookup: Any) -> Any:
        from fastapi import FastAPI as _FastAPI
        from app.interfaces.http.evaluation_routes import (
            register_evaluation_management_routes,
        )

        store = SQLiteEvaluationStore(str(tmp_path / "mgmt.db"))
        service = EvaluationService(store, enabled=True)
        app = _FastAPI()
        register_evaluation_management_routes(
            app,
            evaluation_service=service,
            config_service=_FakeConfigService(),
            review_service=None,
            agent_run_lookup=lookup,
        )
        import asyncio as _asyncio

        _asyncio.run(service.register_feedback_helpful_definition())
        return TestClient(app), service

    def test_agent_run_target_not_found_rejected(self, tmp_path: Any) -> None:
        """#15：agent_run 人评目标必须对应真实 Run——lookup 未命中返回
        422 target_not_found，不落悬空 Score。"""
        async def lookup(subject_id: str) -> dict | None:
            return None

        client, _ = self._management_harness(tmp_path, lookup)
        resp = client.post("/chat/evaluations/feedback", json={
            "target": {
                "service": "n-agent",
                "kind": "agent_run",
                "subject_id": "run-missing",
                "subject_revision": "v1",
            },
            "definition_id": "feedback.helpful",
            "definition_version": "v1",
            "value": True,
        })
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "target_not_found"

    def test_agent_run_target_with_real_run_accepted(self, tmp_path: Any) -> None:
        async def lookup(subject_id: str) -> dict | None:
            return {"run_id": subject_id}

        client, _ = self._management_harness(tmp_path, lookup)
        resp = client.post("/chat/evaluations/feedback", json={
            "target": {
                "service": "n-agent",
                "kind": "agent_run",
                "subject_id": "run-real",
                "subject_revision": "v1",
            },
            "definition_id": "feedback.helpful",
            "definition_version": "v1",
            "value": True,
        })
        assert resp.status_code == 200, resp.text

    def test_real_run_enriches_target_identity(self, tmp_path: Any) -> None:
        """24 项三方审阅 #21：lookup 命中时从真实 Run 恢复身份
        （session_id/run_id/service 归一），人评不再恒 unlinked。"""
        async def lookup(subject_id: str) -> dict | None:
            return {"run_id": subject_id, "session_id": "sess-real-1"}

        client, service = self._management_harness(tmp_path, lookup)
        resp = client.post("/chat/evaluations/feedback", json={
            "target": {
                "service": "n-agent",
                "kind": "agent_run",
                "subject_id": "run-real",
                "subject_revision": "v1",
            },
            "definition_id": "feedback.helpful",
            "definition_version": "v1",
            "value": True,
        })
        assert resp.status_code == 200, resp.text
        # 目标身份经 target_json 落库：从最新反馈行读取
        import sqlite3 as _sqlite3

        conn = _sqlite3.connect(str(tmp_path / "mgmt.db"))
        conn.row_factory = _sqlite3.Row
        row = conn.execute(
            "SELECT target_json FROM evaluation_feedback "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        target = json.loads(row["target_json"])
        assert target["session_id"] == "sess-real-1"
        assert target["run_id"] == "run-real"
        assert target["service"] == "n-agent"

    def test_agent_run_service_mismatch_rejected(self, tmp_path: Any) -> None:
        """#21：agent_run 目标声明 service 与真实归属不一致 -> 422。"""
        async def lookup(subject_id: str) -> dict | None:
            return {"run_id": subject_id, "session_id": "sess-1"}

        client, _ = self._management_harness(tmp_path, lookup)
        resp = client.post("/chat/evaluations/feedback", json={
            "target": {
                "service": "n-kb",
                "kind": "agent_run",
                "subject_id": "run-real",
                "subject_revision": "v1",
            },
            "definition_id": "feedback.helpful",
            "definition_version": "v1",
            "value": True,
        })
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "service_mismatch"

    def test_non_agent_run_target_skips_lookup(self, tmp_path: Any) -> None:
        async def lookup(subject_id: str) -> dict | None:
            raise AssertionError("retrieval 目标不应触发 run 核验")

        client, _ = self._management_harness(tmp_path, lookup)
        resp = client.post("/chat/evaluations/feedback", json={
            "target": {
                "service": "n-kb",
                "kind": "retrieval",
                "subject_id": "ret-1",
                "subject_revision": "v1",
            },
            "definition_id": "feedback.helpful",
            "definition_version": "v1",
            "value": False,
        })
        assert resp.status_code == 200, resp.text

    def test_comment_ref_passthrough(self, tmp_path: Any) -> None:
        """#30：人评接口透传 comment_ref（此前静默丢弃）。"""
        captured: dict = {}

        async def lookup(subject_id: str) -> dict | None:
            return {"run_id": subject_id}

        client, service = self._management_harness(tmp_path, lookup)
        original = service.submit_feedback

        async def spy(actor, request):
            captured["comment_ref"] = request.comment_ref
            return await original(actor, request)

        service.submit_feedback = spy  # type: ignore[method-assign]
        resp = client.post("/chat/evaluations/feedback", json={
            "target": {
                "service": "n-agent",
                "kind": "agent_run",
                "subject_id": "run-cmt",
                "subject_revision": "v1",
            },
            "definition_id": "feedback.helpful",
            "definition_version": "v1",
            "value": True,
            "comment_ref": "comment-store:abc123",
        })
        assert resp.status_code == 200
        assert captured["comment_ref"] == "comment-store:abc123"


class TestDatasetManagement:
    @staticmethod
    def payload():
        return {
            'dataset_id': 'manual', 'dataset_version': 'v1',
            'target_kind': 'agent_run', 'approved_by': 'reviewer',
            'cases': [{'case_id': 'one', 'input': {'query': '2+2?'},
                       'target_kind': 'agent_run', 'expected_output': '4',
                       'fixture_content': {'query': '2+2?'}}],
        }

    def test_import_list_detail_and_duplicate(self, mgmt):
        assert mgmt.get('/chat/evaluations/datasets').json() == {'items': []}
        response = mgmt.post('/chat/evaluations/datasets', self.payload())
        assert response.status_code == 200
        assert 'manifest_path' not in response.json()
        items = mgmt.get('/chat/evaluations/datasets').json()['items']
        assert len(items) == 1
        assert items[0]['case_count'] == 1
        assert items[0]['status'] == 'valid'
        detail = mgmt.get('/chat/evaluations/datasets/manual/v1')
        assert detail.status_code == 200
        assert detail.json()['cases'][0]['expected_output'] == '4'
        assert detail.headers['cache-control'] == 'no-store'
        assert mgmt.post('/chat/evaluations/datasets', self.payload()).status_code == 409
        assert mgmt.get('/chat/evaluations/datasets/manual/missing').status_code == 404

    def test_invalid_import_and_corrupt_manifest(self, mgmt):
        payload = self.payload()
        payload['cases'][0].pop('expected_output')
        assert mgmt.post('/chat/evaluations/datasets', payload).status_code == 422
        assert mgmt.post('/chat/evaluations/datasets', self.payload()).status_code == 200
        manifest = mgmt.datasets_root / 'manual/v1/manifest.json'
        content = json.loads(manifest.read_text())
        content['cases'][0]['expected_output'] = 'tampered'
        manifest.write_text(json.dumps(content))
        items = mgmt.get('/chat/evaluations/datasets').json()['items']
        assert items[0]['status'] == 'invalid'
        response = mgmt.get('/chat/evaluations/datasets/manual/v1')
        assert response.status_code == 422
        assert str(mgmt.datasets_root) not in response.text

    def test_symlink_escape_and_unsafe_id_rejected(self, mgmt):
        outside = mgmt.tmp_path / 'outside'
        outside.mkdir()
        mgmt.datasets_root.mkdir(exist_ok=True)
        (mgmt.datasets_root / 'manual').symlink_to(outside, target_is_directory=True)
        assert mgmt.get('/chat/evaluations/datasets').json() == {'items': []}
        assert mgmt.post('/chat/evaluations/datasets', self.payload()).status_code == 422
        assert not (outside / 'v1').exists()
        payload = self.payload()
        payload['dataset_id'] = '..'
        assert mgmt.post('/chat/evaluations/datasets', payload).status_code == 422

    @pytest.mark.asyncio
    async def test_concurrent_import_keeps_version_immutable(self, mgmt):
        import asyncio
        from app.domain.evaluation.errors import EvaluationConflictError

        results = await asyncio.gather(
            mgmt.review_service.import_dataset(self.payload()),
            mgmt.review_service.import_dataset(self.payload()),
            return_exceptions=True,
        )
        assert sum(isinstance(item, dict) for item in results) == 1
        assert sum(isinstance(item, EvaluationConflictError) for item in results) == 1
        dataset = await mgmt.review_service.get_dataset('manual', 'v1')
        assert dataset['cases'][0]['expected_output'] == '4'


# ---------------------------------------------------------------------------
# Task 9：评估器管理 / 人工标注 / 分析聚合 / 实验对比 / 结果扩展
# ---------------------------------------------------------------------------

import base64
from urllib.parse import quote


class TestEvaluatorManagement:
    """评估器管理路由（spec 4.2/4.3）：能力目录/列表/详情/版本创建/启停。"""

    @staticmethod
    def _deterministic_payload(**overrides: Any) -> dict:
        payload = {
            "definition_id": "kb.recall_at_5",
            "version": "v2",
            "metric_name": "kb.recall_at_5",
            "target_kind": "retrieval",
            "method": "deterministic",
            "output_type": "number",
        }
        payload.update(overrides)
        return payload

    def test_capabilities_200_with_judge_state(
        self, mgmt: _ManagementHarness
    ) -> None:
        resp = mgmt.get("/chat/evaluations/evaluators/capabilities")
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store"
        body = resp.json()
        assert body["judge_configured"] is True
        entries = body["entries"]
        assert any(
            entry["method"] == "deterministic"
            and entry["metric_name"] == "kb.recall_at_5"
            and entry["target_kind"] == "retrieval"
            for entry in entries
        )
        assert all(
            "executable" in entry and "online_forbidden" in entry
            for entry in entries
        )
        assert all(
            entry["online_forbidden"] is True
            for entry in entries
            if entry["method"] == "human"
        )

    def test_list_evaluators_filter_and_pagination(
        self, mgmt: _ManagementHarness
    ) -> None:
        created = mgmt.post(
            "/chat/evaluations/evaluators", self._deterministic_payload()
        )
        assert created.status_code == 200
        resp = mgmt.get("/chat/evaluations/evaluators")
        assert resp.status_code == 200
        items = resp.json()["items"]
        identities = {
            (item["definition_id"], item["version"]) for item in items
        }
        assert ("def-answer-relevance", "v1") in identities
        assert ("kb.recall_at_5", "v2") in identities
        assert all("usage" in item for item in items)
        filtered = mgmt.get(
            "/chat/evaluations/evaluators?target_kind=retrieval"
        )
        assert filtered.status_code == 200
        assert filtered.json()["items"]
        assert all(
            item["target_kind"] == "retrieval"
            for item in filtered.json()["items"]
        )
        by_method = mgmt.get("/chat/evaluations/evaluators?method=human")
        assert all(
            item["method"] == "human"
            for item in by_method.json()["items"]
        )
        page1 = mgmt.get("/chat/evaluations/evaluators?limit=1")
        body = page1.json()
        assert len(body["items"]) == 1
        assert body["next_cursor"]
        page2 = mgmt.get(
            f"/chat/evaluations/evaluators?limit=1&cursor={body['next_cursor']}"
        )
        assert page2.status_code == 200
        assert len(page2.json()["items"]) == 1
        assert (
            page2.json()["items"][0]["definition_id"]
            != body["items"][0]["definition_id"]
        )

    def test_list_evaluators_invalid_params_422(
        self, mgmt: _ManagementHarness
    ) -> None:
        assert mgmt.get("/chat/evaluations/evaluators?limit=0").status_code == 422
        assert mgmt.get(
            "/chat/evaluations/evaluators?limit=abc"
        ).status_code == 422
        bad_cursor = mgmt.get("/chat/evaluations/evaluators?cursor=%%")
        assert bad_cursor.status_code == 422

    def test_get_evaluator_detail_and_404(
        self, mgmt: _ManagementHarness
    ) -> None:
        resp = mgmt.get("/chat/evaluations/evaluators/def-answer-relevance/v1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["definition"]["definition_id"] == "def-answer-relevance"
        assert body["definition"]["version"] == "v1"
        assert body["usage"]["online_enabled"] is False
        assert body["calibration"] is None
        missing = mgmt.get("/chat/evaluations/evaluators/missing/v1")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "not_found"

    def test_create_evaluator_disabled_usage_then_enable_cas(
        self, mgmt: _ManagementHarness
    ) -> None:
        resp = mgmt.post(
            "/chat/evaluations/evaluators", self._deterministic_payload()
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["definition"]["version"] == "v2"
        # spec 4.3：新增版本默认不用于线上
        assert body["usage"]["online_enabled"] is False
        usage_path = "/chat/evaluations/evaluators/kb.recall_at_5/v2/usage"
        ok = mgmt.patch(usage_path, {
            "online_enabled": True,
            "expected_revision": body["usage"]["revision"],
        })
        assert ok.status_code == 200
        assert ok.json()["usage"]["online_enabled"] is True
        # 旧 revision 再写：409 conflict
        stale = mgmt.patch(usage_path, {
            "online_enabled": False,
            "expected_revision": body["usage"]["revision"],
        })
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "conflict"

    def test_create_evaluator_idempotent_same_content(
        self, mgmt: _ManagementHarness
    ) -> None:
        first = mgmt.post(
            "/chat/evaluations/evaluators", self._deterministic_payload()
        )
        assert first.status_code == 200
        usage_path = "/chat/evaluations/evaluators/kb.recall_at_5/v2/usage"
        enabled = mgmt.patch(usage_path, {
            "online_enabled": True,
            "expected_revision": first.json()["usage"]["revision"],
        })
        assert enabled.status_code == 200
        # 同键同版本同内容幂等重放：usage 停用状态不被复活
        replay = mgmt.post(
            "/chat/evaluations/evaluators", self._deterministic_payload()
        )
        assert replay.status_code == 200
        assert replay.json()["usage"]["online_enabled"] is True

    def test_create_evaluator_conflict_different_content_409(
        self, mgmt: _ManagementHarness
    ) -> None:
        assert mgmt.post(
            "/chat/evaluations/evaluators", self._deterministic_payload()
        ).status_code == 200
        conflict = mgmt.post(
            "/chat/evaluations/evaluators",
            self._deterministic_payload(ci_admissible=True),
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "conflict"

    def test_create_evaluator_unknown_field_422(
        self, mgmt: _ManagementHarness
    ) -> None:
        resp = mgmt.post(
            "/chat/evaluations/evaluators",
            self._deterministic_payload(material_hash={"rubric": "x"}),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "protocol_invalid"

    def test_create_evaluator_unsupported_combo_422(
        self, mgmt: _ManagementHarness
    ) -> None:
        resp = mgmt.post(
            "/chat/evaluations/evaluators",
            self._deterministic_payload(
                target_kind="agent_run", version="v3"
            ),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "unsupported_capability"

    def test_create_evaluator_nan_rejected_422(
        self, mgmt: _ManagementHarness
    ) -> None:
        raw = (
            '{"definition_id": "agent.answer_correctness", "version": "v2",'
            ' "metric_name": "agent.answer_correctness",'
            ' "target_kind": "agent_run", "method": "llm_judge",'
            ' "output_type": "number", "judge_model": "gpt-judge",'
            ' "judge_params": {"temperature": NaN}}'
        )
        resp = mgmt.post_raw("/chat/evaluations/evaluators", raw.encode("utf-8"))
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "protocol_invalid"

    def test_set_usage_unknown_field_422(
        self, mgmt: _ManagementHarness
    ) -> None:
        resp = mgmt.patch(
            "/chat/evaluations/evaluators/def-answer-relevance/v1/usage",
            {
                "online_enabled": True,
                "expected_revision": 1,
                "bogus_field": True,
            },
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "protocol_invalid"

    def test_create_evaluator_human_enable_422(
        self, mgmt: _ManagementHarness
    ) -> None:
        created = mgmt.post("/chat/evaluations/evaluators", {
            "definition_id": "manual.metric",
            "version": "v1",
            "metric_name": "manual.metric",
            "target_kind": "agent_run",
            "method": "human",
            "output_type": "boolean",
        })
        assert created.status_code == 200
        # human 方法禁止线上启用（spec 6.2/4.3）
        resp = mgmt.patch(
            "/chat/evaluations/evaluators/manual.metric/v1/usage",
            {"online_enabled": True, "expected_revision": 1},
        )
        assert resp.status_code == 422

    def test_set_usage_invalid_body_422(
        self, mgmt: _ManagementHarness
    ) -> None:
        resp = mgmt.patch(
            "/chat/evaluations/evaluators/def-answer-relevance/v1/usage",
            {"online_enabled": "yes", "expected_revision": 1},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "protocol_invalid"

    def test_new_routes_404_when_management_gated_off(
        self, tmp_path: Any
    ) -> None:
        # 条件注册分支（register_evaluation_management_routes）：传入
        # config_service 等既有依赖但三个新服务参数留 None 时，新路由
        # 不注册 -> 框架原生 404；旧路由仍经既有服务返回 200
        harness = _ManagementHarness(tmp_path)
        gated_app = FastAPI()
        register_evaluation_management_routes(
            gated_app,
            evaluation_service=harness.evaluation_service,
            config_service=harness.config_service,
            review_service=harness.review_service,
        )
        gated_client = TestClient(gated_app)
        for path in (
            "/chat/evaluations/evaluators/capabilities",
            "/chat/evaluations/evaluators",
            "/chat/evaluations/annotations",
            "/chat/evaluations/analytics",
        ):
            assert gated_client.get(path).status_code == 404
        assert gated_client.get("/chat/evaluations/overview").status_code == 200
        assert gated_client.get("/chat/evaluations/config").status_code == 200


class TestAnnotations:
    """人工标注路由（spec 4.4）：标注队列/上下文/受约束提交。"""

    def test_annotation_queue_shape_and_pagination(
        self, mgmt: _ManagementHarness
    ) -> None:
        result_id = _run(mgmt._seed_annotation_result())
        resp = mgmt.get("/chat/evaluations/annotations")
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store"
        body = resp.json()
        assert body["snapshot_at"]
        assert body["next_cursor"] is None
        item = body["items"][0]
        assert item["source_result_id"] == result_id
        assert item["target_kind"] == "agent_run"
        assert item["definition_id"] == "quality.judge"
        assert item["latest_human_feedback"] is None
        _run(mgmt._seed_annotation_result(subject_id="run-2", event="evt-a2"))
        page1 = mgmt.get("/chat/evaluations/annotations?limit=1")
        assert len(page1.json()["items"]) == 1
        assert page1.json()["next_cursor"]
        page2 = mgmt.get(
            "/chat/evaluations/annotations?limit=1&cursor="
            + quote(page1.json()["next_cursor"])
        )
        assert page2.status_code == 200
        assert len(page2.json()["items"]) == 1
        assert (
            page2.json()["items"][0]["source_result_id"]
            != page1.json()["items"][0]["source_result_id"]
        )

    def test_annotation_queue_invalid_filters_422(
        self, mgmt: _ManagementHarness
    ) -> None:
        assert mgmt.get(
            "/chat/evaluations/annotations?state=wat"
        ).status_code == 422
        assert mgmt.get(
            "/chat/evaluations/annotations?reason=wat"
        ).status_code == 422
        assert mgmt.get(
            "/chat/evaluations/annotations?limit=0"
        ).status_code == 422

    def test_annotation_queue_state_filter(
        self, mgmt: _ManagementHarness
    ) -> None:
        result_id = _run(mgmt._seed_annotation_result())
        assert mgmt.get(
            "/chat/evaluations/annotations?state=annotated"
        ).json()["items"] == []
        submit = mgmt.post(
            f"/chat/evaluations/annotations/{result_id}",
            {"value": 0.9, "request_key": "rk-state", "expected_revision": 0},
        )
        assert submit.status_code == 200
        annotated = mgmt.get(
            "/chat/evaluations/annotations?state=annotated"
        ).json()["items"]
        assert [item["source_result_id"] for item in annotated] == [result_id]
        assert annotated[0]["latest_human_feedback"]["revision"] == 1
        pending = mgmt.get(
            "/chat/evaluations/annotations?state=pending"
        ).json()["items"]
        assert all(
            item["source_result_id"] != result_id for item in pending
        )

    def test_annotation_context_200(self, mgmt: _ManagementHarness) -> None:
        result_id = _run(mgmt._seed_annotation_result())
        resp = mgmt.get(f"/chat/evaluations/annotations/{result_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["source_result_id"] == result_id
        assert body["target"]["kind"] == "agent_run"
        assert body["target"]["subject_id"] == "run-1"
        assert body["definition"]["metric_name"] == "quality.judge"
        assert body["evidence_status"] == "available"
        assert body["can_submit"] is True
        assert body["latest_human_feedback"] is None
        assert body["current_revision"] == 0

    def test_annotation_context_404(self, mgmt: _ManagementHarness) -> None:
        resp = mgmt.get("/chat/evaluations/annotations/missing-result")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_annotation_context_410_target_deleted(
        self, mgmt: _ManagementHarness
    ) -> None:
        result_id = _run(mgmt._seed_annotation_result(subject_id="run-gone"))
        _run(mgmt._seed_target_tombstone("run-gone", "stop"))
        resp = mgmt.get(f"/chat/evaluations/annotations/{result_id}")
        assert resp.status_code == 410
        assert resp.json()["error"]["code"] == "target_deleted"

    def test_annotation_submit_success_and_replay(
        self, mgmt: _ManagementHarness
    ) -> None:
        result_id = _run(mgmt._seed_annotation_result())
        payload = {
            "value": 0.9,
            "request_key": "rk-1",
            "expected_revision": 0,
        }
        resp = mgmt.post(f"/chat/evaluations/annotations/{result_id}", payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["feedback_id"]
        assert body["result_id"] and body["result_id"] != result_id
        assert body["revision"] == 1
        assert body["duplicate"] is False
        assert body["is_current"] is True
        # 同 request_key 同 payload 重放：返回原成功且 duplicate=true
        replay = mgmt.post(f"/chat/evaluations/annotations/{result_id}", payload)
        assert replay.status_code == 200
        assert replay.json()["duplicate"] is True
        assert replay.json()["feedback_id"] == body["feedback_id"]
        assert replay.json()["result_id"] == body["result_id"]

    def test_annotation_submit_replay_different_payload_409(
        self, mgmt: _ManagementHarness
    ) -> None:
        result_id = _run(mgmt._seed_annotation_result())
        first = mgmt.post(f"/chat/evaluations/annotations/{result_id}", {
            "value": 0.9, "request_key": "rk-2", "expected_revision": 0,
        })
        assert first.status_code == 200
        conflict = mgmt.post(f"/chat/evaluations/annotations/{result_id}", {
            "value": 0.5, "request_key": "rk-2", "expected_revision": 1,
        })
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "conflict"

    def test_annotation_submit_stale_revision_409(
        self, mgmt: _ManagementHarness
    ) -> None:
        result_id = _run(mgmt._seed_annotation_result())
        first = mgmt.post(f"/chat/evaluations/annotations/{result_id}", {
            "value": 0.9, "request_key": "rk-3a", "expected_revision": 0,
        })
        assert first.status_code == 200
        stale = mgmt.post(f"/chat/evaluations/annotations/{result_id}", {
            "value": 0.8, "request_key": "rk-3b", "expected_revision": 0,
        })
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "conflict"

    def test_annotation_submit_invalid_value_422(
        self, mgmt: _ManagementHarness
    ) -> None:
        result_id = _run(mgmt._seed_annotation_result())
        resp = mgmt.post(f"/chat/evaluations/annotations/{result_id}", {
            "value": 1.5, "request_key": "rk-4", "expected_revision": 0,
        })
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "protocol_invalid"

    def test_annotation_submit_missing_request_key_422(
        self, mgmt: _ManagementHarness
    ) -> None:
        result_id = _run(mgmt._seed_annotation_result())
        resp = mgmt.post(f"/chat/evaluations/annotations/{result_id}", {
            "value": 0.9, "expected_revision": 0,
        })
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "protocol_invalid"

    def test_annotation_submit_actor_in_body_rejected_422(
        self, mgmt: _ManagementHarness
    ) -> None:
        result_id = _run(mgmt._seed_annotation_result())
        resp = mgmt.post(f"/chat/evaluations/annotations/{result_id}", {
            "value": 0.9,
            "request_key": "rk-actor",
            "expected_revision": 0,
            "actor": "someone-else",
        })
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "protocol_invalid"

    def test_annotation_submit_evidence_unavailable_422(
        self, mgmt: _ManagementHarness
    ) -> None:
        result_id = _run(mgmt._seed_annotation_result(snapshot_ref=""))
        resp = mgmt.post(f"/chat/evaluations/annotations/{result_id}", {
            "value": 0.9, "request_key": "rk-ev", "expected_revision": 0,
        })
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "evidence_unavailable"

    def test_annotation_submit_unsupported_target_kind_422_no_score(
        self, mgmt: _ManagementHarness
    ) -> None:
        seeded = _run(mgmt._seed_sample_job())  # retrieval 目标
        human_rows, _ = _run(mgmt.store.query_results_page(provenance="human"))
        assert human_rows == []
        resp = mgmt.post(
            f"/chat/evaluations/annotations/{seeded['result_id']}",
            {"value": 0.9, "request_key": "rk-kind", "expected_revision": 0},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "target_kind_not_supported"
        # 不创建评分
        human_rows, _ = _run(mgmt.store.query_results_page(provenance="human"))
        assert human_rows == []


class TestAnalytics:
    """概览分析路由（spec 4.5）：统一口径聚合 + 过滤校验。"""

    def test_analytics_shape_with_seeded_group(
        self, mgmt: _ManagementHarness
    ) -> None:
        _run(mgmt._seed_sample_job())
        resp = mgmt.get("/chat/evaluations/analytics")
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store"
        body = resp.json()
        assert body["snapshot_at"]
        assert body["window"]["from"] < body["window"]["to"]
        assert body["buckets"]
        assert all(
            "date" in bucket and "total" in bucket
            for bucket in body["buckets"]
        )
        assert body["coverage"]["status"] == "unknown"
        assert body["coverage"]["reason"]
        group = next(
            g for g in body["groups"]
            if g["definition_id"] == "def-answer-relevance"
        )
        assert group["provenance"] == "automatic"
        assert group["output_type"] == "number"
        assert group["count"] == 1
        assert group["mean"] == 0.2
        # 未定义阈值不造通过率
        assert "pass_rate" not in group

    def test_analytics_filter_validation_422(
        self, mgmt: _ManagementHarness
    ) -> None:
        # from >= to
        assert mgmt.get(
            "/chat/evaluations/analytics?created_from="
            + quote("2026-11-10T00:00:00+00:00")
            + "&created_to="
            + quote("2026-11-01T00:00:00+00:00")
        ).status_code == 422
        # 非法 ISO
        assert mgmt.get(
            "/chat/evaluations/analytics?created_from=not-a-time"
        ).status_code == 422
        # 跨度超过 90 天上限
        assert mgmt.get(
            "/chat/evaluations/analytics?created_from="
            + quote("2026-01-01T00:00:00+00:00")
        ).status_code == 422

    def test_analytics_filters_narrow_groups(
        self, mgmt: _ManagementHarness
    ) -> None:
        _run(mgmt._seed_sample_job())
        resp = mgmt.get("/chat/evaluations/analytics?definition_id=nope")
        assert resp.status_code == 200
        body = resp.json()
        assert body["groups"] == []
        assert all(bucket["total"] == 0 for bucket in body["buckets"])


class TestCompareRoute:
    """只读实验对比路由（spec 4.6）+ 注册顺序断言。"""

    def _create_experiment(
        self,
        mgmt: _ManagementHarness,
        *,
        dataset_id: str = "ds-cmp",
        dataset_hash: str | None = None,
        seed_result: bool = False,
    ) -> str:
        payload = {
            "dataset_id": dataset_id,
            "dataset_version": "v1",
            "dataset_hash": dataset_hash or "a" * 64,
            "target_config": {},
            "evaluator_set": [
                {
                    "definition_id": "def-answer-relevance",
                    "definition_version": "v1",
                },
            ],
        }
        resp = mgmt.post("/chat/evaluations/experiments", payload)
        assert resp.status_code == 202
        experiment_id = resp.json()["experiment_id"]
        if seed_result:
            # 冻结快照核验（judge_config_hash）需要观测到的定义行：
            # 无结果实验的账本重建快照缺配置字段，描述性对比也 fail-closed
            suffix = f"-cmp{getattr(self, '_cmp_seq', 0)}"
            self._cmp_seq = getattr(self, "_cmp_seq", 0) + 1
            _run(mgmt._seed_sample_job(
                experiment_id=experiment_id,
                sample_id_suffix=suffix,
            ))
        return experiment_id

    def test_compare_descriptive_and_policy(
        self, mgmt: _ManagementHarness
    ) -> None:
        candidate = self._create_experiment(mgmt, seed_result=True)
        baseline = self._create_experiment(mgmt, seed_result=True)
        resp = mgmt.post("/chat/evaluations/experiments/compare", {
            "candidate_id": candidate,
            "baseline_id": baseline,
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["evidence_basis"] == "ledger_reconstruction"
        assert isinstance(body["metrics"], list)
        # 无 policy 不产生门禁结论（spec 4.6）
        assert "conclusion" not in body
        with_policy = mgmt.post("/chat/evaluations/experiments/compare", {
            "candidate_id": candidate,
            "baseline_id": baseline,
            "policy": gate_policy_content(default_gate_policy()),
        })
        assert with_policy.status_code == 200
        policy_body = with_policy.json()
        assert policy_body["conclusion"]["state"] in {
            "gate_pass", "gate_fail", "inconclusive", "infrastructure_error",
        }
        assert policy_body["policy_fingerprint"]

    def test_compare_incomparable_422(self, mgmt: _ManagementHarness) -> None:
        candidate = self._create_experiment(mgmt)
        baseline = self._create_experiment(mgmt, dataset_hash="b" * 64)
        resp = mgmt.post("/chat/evaluations/experiments/compare", {
            "candidate_id": candidate,
            "baseline_id": baseline,
        })
        assert resp.status_code == 422
        error = resp.json()["error"]
        assert error["code"] == "incomparable_experiments"
        assert error["diff_dimensions"]["dataset_hash"] is True

    def test_compare_unknown_experiment_404(
        self, mgmt: _ManagementHarness
    ) -> None:
        resp = mgmt.post("/chat/evaluations/experiments/compare", {
            "candidate_id": "missing-a",
            "baseline_id": "missing-b",
        })
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_compare_invalid_policy_422(
        self, mgmt: _ManagementHarness
    ) -> None:
        candidate = self._create_experiment(mgmt)
        baseline = self._create_experiment(mgmt)
        resp = mgmt.post("/chat/evaluations/experiments/compare", {
            "candidate_id": candidate,
            "baseline_id": baseline,
            "policy": {"schema_version": "bogus"},
        })
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "protocol_invalid"

    def test_compare_unknown_field_422(
        self, mgmt: _ManagementHarness
    ) -> None:
        resp = mgmt.post("/chat/evaluations/experiments/compare", {
            "candidate_id": "cand-1",
            "baseline_id": "base-1",
            "bogus_field": True,
        })
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "protocol_invalid"

    def test_compare_route_registered_before_dynamic_detail(
        self, mgmt: _ManagementHarness
    ) -> None:
        experiment_paths = []
        for route in mgmt.app.router.routes:
            path = getattr(route, "path", "")
            if path.startswith("/chat/evaluations/experiments"):
                experiment_paths.append(
                    (path, tuple(sorted(getattr(route, "methods", []) or [])))
                )
        compare_index = experiment_paths.index(
            ("/chat/evaluations/experiments/compare", ("POST",))
        )
        detail_index = experiment_paths.index(
            ("/chat/evaluations/experiments/{experiment_id}", ("GET",))
        )
        # 字面路由先于动态路由注册（不同方法本就互不遮蔽，顺序仍固定）
        assert compare_index < detail_index


class TestResultsExtended:
    """results 路由扩展（spec 7）：新过滤 + snapshot_at + 游标兼容。"""

    @staticmethod
    def _legacy_cursor(row: Any) -> str:
        raw = json.dumps(
            {"c": row["created_at"], "r": row["result_id"]}, sort_keys=True
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii")

    def test_results_new_filters_and_snapshot_at(
        self, mgmt: _ManagementHarness
    ) -> None:
        _run(mgmt._seed_sample_job(sample_id_suffix="-1"))
        _run(mgmt._seed_sample_job(sample_id_suffix="-2"))
        resp = mgmt.get("/chat/evaluations/results?provenance=human")
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["snapshot_at"]
        resp = mgmt.get(
            "/chat/evaluations/results?provenance=automatic&definition_version=v1"
        )
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 2
        assert resp.json()["snapshot_at"]
        assert mgmt.get(
            "/chat/evaluations/results?definition_version=v9"
        ).json()["items"] == []

    def test_results_created_window_filter(
        self, mgmt: _ManagementHarness
    ) -> None:
        _run(mgmt._seed_sample_job())
        now = datetime.now(timezone.utc)
        inside = mgmt.get(
            "/chat/evaluations/results?created_from="
            + quote((now - timedelta(days=1)).isoformat())
            + "&created_to="
            + quote((now + timedelta(days=1)).isoformat())
        )
        assert inside.status_code == 200
        assert len(inside.json()["items"]) == 1
        outside = mgmt.get(
            "/chat/evaluations/results?created_from="
            + quote((now + timedelta(days=1)).isoformat())
            + "&created_to="
            + quote((now + timedelta(days=2)).isoformat())
        )
        assert outside.status_code == 200
        assert outside.json()["items"] == []

    def test_results_legacy_cursor_still_works(
        self, mgmt: _ManagementHarness
    ) -> None:
        # 历史客户端持有的旧 b64 {"c","r"} 游标：无新增过滤时保持兼容
        # （游标指向最新一行 -> 下一页是更旧的一行）
        _run(mgmt._seed_sample_job(sample_id_suffix="-1"))
        _run(mgmt._seed_sample_job(sample_id_suffix="-2"))
        first = mgmt.get("/chat/evaluations/results?limit=1")
        newest = first.json()["items"][0]
        legacy = self._legacy_cursor({
            "created_at": newest["created_at"],
            "result_id": newest["result_id"],
        })
        resp = mgmt.get(
            f"/chat/evaluations/results?limit=1&cursor={quote(legacy)}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 1
        assert body["items"][0]["result_id"] != newest["result_id"]

    def test_results_new_filters_with_legacy_cursor_422_cursor_invalid(
        self, mgmt: _ManagementHarness
    ) -> None:
        seeded = _run(mgmt._seed_sample_job())
        row = _run(mgmt.store.load_result(seeded["result_id"]))
        legacy = self._legacy_cursor(row)
        resp = mgmt.get(
            f"/chat/evaluations/results?provenance=automatic&cursor={quote(legacy)}"
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "cursor_invalid"

    def test_results_malformed_cursor_422(
        self, mgmt: _ManagementHarness
    ) -> None:
        bad = base64.urlsafe_b64encode(b"not-json").decode("ascii")
        resp = mgmt.get(f"/chat/evaluations/results?cursor={quote(bad)}")
        assert resp.status_code == 422

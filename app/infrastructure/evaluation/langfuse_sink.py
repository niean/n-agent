# app/infrastructure/evaluation/langfuse_sink.py
"""Langfuse Score 投影适配器（plan T9，spec 11.1 / 13）。

独立 Infrastructure REST adapter，首期不引入 Langfuse tracing SDK、不替换
全局 OTel Provider（spec 11.1）。固定 Langfuse 4.76.0 合同：

- ``POST /api/public/scores``（Basic 认证 public:secret）：字段映射按
  spec 11.1 表——name=`{metric}.v{major}`（judge/human 后缀语义）、
  value 类型转换（BOOLEAN→0/1 + dataType、NUMERIC 有限 float、
  CATEGORICAL 字符串）、configId、observation 评分同时发送
  traceId+observationId、session 评分仅 sessionId、comment 先脱敏再限制
  2000 UTF-8 字节并附 result_id 与定义 hash 的短关联说明；POST body 永不
  伪造 source（source 由 Langfuse 决定，API 写入应显示 API）。
- ``GET /api/public/v3/scores?id={id}``：有界回读核验（4.76.0 events_only
  无 GET-by-id 路由，改用 v3 列表按 id 过滤）；空列表返回 None（Score 可
  先于 Trace 到达，等待关联由调度器有界重试）。v3 条目仅含 id/name/
  value/dataType 等字段（无目标与 configId），核验降级为 id/name/
  dataType/value 匹配，目标一致性由 external_id 的 UUIDv5 确定性保证。
- ``DELETE /api/public/scores/{id}``：外部删除端（Domain ``ExternalDeleter``
  端口，spec 14 删除传播）；202（4.76.0 异步受理）/204 -> "deleted"、
  404 -> "not_found"，可重试/冲突异常按既有分类（删除编排捕获后任务退避，
  绝不宣称完成）。
- ``POST/GET /api/public/score-configs``：ScoreConfig 按 score name 维度
  创建、回读与 schema hash 校验（同一 definition_version 下 human/judge/
  deterministic 后缀各对应独立 config，config name 与 score name 恒
  一致）；已有配置 schema 漂移 -> 冲突隔离（dead_letter 类），不静默覆盖。
- 非有限数值（NaN/±inf）、空类别串与非法类型是永久数据缺陷 -> dead_letter
  类失败（隔离上报、可显式补发）：此类值在领域层结果落库时已被拒绝，到达
  此处即为账本数据缺陷，不得以普通异常 wedge 投递，也不得静默丢弃
  （error_type 区分 non_finite_value / empty_categorical /
  illegal_value_type，便于运维定位缺陷类别）。

身份（spec 6.1 四组身份独立）：external_id = UUIDv5(project_id + result_id
+ projection_version)，同一投递重试复用同 ID 同 payload（payload 以
canonical JSON 字节级稳定序列化）；重评（新 result_id）与新投影版本得到
新 ID，同 Trace 的多个检索/重评/人评不互相覆盖。

unlinked（spec 11.1/13，无默认可关联）：目标无 trace_id 且非 session 评分、
observation 评分缺 span_id、sampled=false、OTel 禁用 -> unlinked，不发送
任何请求、不创建 ScoreConfig、不伪造业务 Trace。

安全边界：endpoint 启动期构造固定（scheme/host 校验、拒绝 URL 内嵌凭据），
不接受样本或浏览器传入任意地址；public/secret key 构造注入，仅用于
Authorization 头，绝不写入日志或异常消息。

失败分类（spec 13 表，异常属性供可恢复投递调度器识别）：
- 409/4xx schema 冲突、ScoreConfig 漂移 -> ``ScoreDeliveryConflictError``
  （delivery_outcome="dead_letter"，隔离上报，不无限重试）；其中 401/403
  单列 error_type="auth"（认证/授权配置错误，运维可直接定位密钥问题）；
- 429/5xx/网络/超时 -> ``ScoreDeliveryRetryableError``
  （delivery_outcome="retry"，error_type=network/rate_limit/timeout），
  退避由调度器管理，客户端 max_retries=0（httpx 无 SDK 重试）。该分类
  对 scores 与 score-configs 全部请求路径一致（含 409 后的按名解析）：
  瞬时错误不得被误判为永久冲突把投递打进 dead_letter。

URL 校验说明（对齐 app/infrastructure/memory/external/http_client.py 的
模式而非逐条复制）：scheme 白名单 + hostname 必填 + 拒绝 URL 内嵌凭据。
有意不做 DNS 解析与私网地址阻断——Langfuse 部署 endpoint 本就是运维固定
的内部地址（B 类 env-only），私网阻断会直接破坏 Docker 内部署；该客户端
不消费用户输入 URL，攻击面与按请求解析的外部 SaaS 客户端不同。
"""
from __future__ import annotations

import base64
import hashlib
import math
import uuid
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlparse

import httpx

from app.domain.evaluation.errors import EvaluationNotFoundError
from app.domain.evaluation.models import canonical_json_bytes
from app.infrastructure.observability.sanitizer import LogSanitizer

# spec 13：单 comment 上限 2000 UTF-8 字节（脱敏先于限长）
COMMENT_MAX_BYTES: int = 2000

# 固定 4.76.0 API 路径。events_only 模式：Score 创建走经典 POST
# /api/public/scores（仍开放），回读无 GET-by-id 路由，改用 v3 列表
# 路由按 id 过滤（条目仅含 id/name/value/dataType 等字段，不含
# traceId/observationId/sessionId/configId）；删除走经典
# DELETE /api/public/scores/{id}（202 异步受理）。
_SCORES_PATH = "/api/public/scores"
_SCORES_V3_PATH = "/api/public/v3/scores"
_SCORE_CONFIGS_PATH = "/api/public/score-configs"

_SCORE_DATA_TYPES: dict[str, str] = {
    "number": "NUMERIC",
    "boolean": "BOOLEAN",
    "categorical": "CATEGORICAL",
}


class LangfuseSinkError(Exception):
    """Langfuse 投影适配器错误基类。"""


class ScoreDeliveryConflictError(LangfuseSinkError):
    """dead_letter 类失败（spec 13）：schema 冲突等不可重试错误，隔离
    上报，不无限重试；可由管理员显式补发。"""

    def __init__(self, message: str, *, error_type: str = "schema_conflict") -> None:
        super().__init__(message)
        self.delivery_outcome = "dead_letter"
        self.error_type = error_type


class ScoreDeliveryRetryableError(LangfuseSinkError):
    """可重试类失败（spec 13）：429/5xx/网络/超时，由调度器指数退避。"""

    def __init__(self, message: str, *, error_type: str = "network") -> None:
        if error_type not in {"network", "rate_limit", "timeout"}:
            raise ValueError(
                f"ScoreDeliveryRetryableError 只接受可重试分类: {error_type}"
            )
        super().__init__(message)
        self.delivery_outcome = "retry"
        self.error_type = error_type


class SinkConfigStore(Protocol):
    """ScoreConfig 持久化端口（SQLiteEvaluationStore 的 sink_configs 形状）。"""

    async def get_sink_config(
        self, sink: str, project: str, definition_version: str
    ) -> Mapping[str, Any]: ...

    async def save_sink_config(self, config: Mapping[str, Any]) -> None: ...


def _parse_major_version(version: str) -> int:
    """从定义/结果版本解析 major（'v2.1'/'2.1'/'2' -> 2；不可解析 -> 1）。
    破坏性语义变化必须换 major（spec 11.1：换名规则）。"""
    text = (version or "").strip().lstrip("vV")
    digits = ""
    for ch in text:
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits else 1


def build_score_name(
    metric_name: str, method: str, provenance: str, version: str
) -> str:
    """Langfuse Score name（spec 11.1）：{metric}.v{major}，judge/human
    后缀语义。人评一律 .human-v{major}（provenance 权威），LLM Judge 为
    .judge-v{major}，deterministic 无后缀。"""
    major = _parse_major_version(version)
    if provenance == "human":
        return f"{metric_name}.human-v{major}"
    if method == "llm_judge":
        return f"{metric_name}.judge-v{major}"
    return f"{metric_name}.v{major}"


def compute_external_id(
    *, project: str, result_id: str, projection_version: str
) -> str:
    """external_id = UUIDv5(project_id + result_id + projection_version)。

    同 result 重试复用同 ID；不能只用 trace_id + metric_name，否则同
    Trace 的多个检索、重评和人评会相互覆盖（spec 11.1）。"""
    name = f"{project}:{result_id}:{projection_version}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def _convert_value(typed_value: Any) -> tuple[Any, str]:
    """typed_value -> (Langfuse value, dataType)（spec 11.1 类型转换）。

    BOOLEAN 显式 0/1；NUMERIC 有限 float；CATEGORICAL 合法字符串。

    非有限数值（NaN/±inf）、空类别串与非法类型是永久性数据缺陷
    （领域层 ``validate_evaluation_value`` 在结果落库时已拒绝此类值；
    到达本函数即说明账本数据已被污染）。按 dead_letter 类失败抛出
    （``ScoreDeliveryConflictError``）而非普通异常：普通异常会让投递
    以 pending 保留并被反复重领（永久 wedge），dead_letter 则隔离上报、
    可由管理员显式补发——数据缺陷必须可见，不得静默丢弃。error_type
    区分缺陷类别（non_finite_value / empty_categorical /
    illegal_value_type），便于运维定位账本污染来源。"""
    if isinstance(typed_value, bool):
        return (1 if typed_value else 0), "BOOLEAN"
    if isinstance(typed_value, (int, float)):
        value = float(typed_value)
        if not math.isfinite(value):
            raise ScoreDeliveryConflictError(
                f"NUMERIC value 必须有限（永久数据缺陷）: {typed_value!r}",
                error_type="non_finite_value",
            )
        return value, "NUMERIC"
    if isinstance(typed_value, str):
        if not typed_value:
            raise ScoreDeliveryConflictError(
                "CATEGORICAL value 不得为空串（永久数据缺陷）",
                error_type="empty_categorical",
            )
        return typed_value, "CATEGORICAL"
    raise ScoreDeliveryConflictError(
        f"typed_value 必须是 bool/有限数值/字符串（永久数据缺陷）: "
        f"{type(typed_value)!r}",
        error_type="illegal_value_type",
    )


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """UTF-8 边界安全截断：不切断多字节字符（超限截到 max_bytes 内）。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes]
    # 回退到最后一个完整 UTF-8 字符边界
    while truncated:
        try:
            return truncated.decode("utf-8")
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    return ""


def redact_comment(
    explanation: str,
    *,
    result_id: str,
    definition_hash: str,
    redactor: Callable[[str], str],
    max_bytes: int = COMMENT_MAX_BYTES,
) -> str:
    """comment = 脱敏(explanation) + 脱敏(result_id/定义 hash 关联说明)，
    脱敏先于限长（spec 11.1 / 13）。

    超限时只截断说明部分（UTF-8 边界安全），关联说明恒保留在末尾——
    截断后仍可定位来源 result 与定义，不因长解释丢失关联信息。说明与
    说明分别过 redactor（LogSanitizer 规则逐段适用，结果与整体脱敏
    一致）；预算不足以容纳关联说明本身时（异常配置）对说明做兜底
    截断，总长仍不超过 max_bytes。"""
    note = redactor(f"result_id={result_id} definition_hash={definition_hash}")
    if not explanation:
        return _truncate_utf8(note, max_bytes)
    body = redactor(explanation)
    separator = "\n"
    budget = (
        max_bytes
        - len(note.encode("utf-8"))
        - len(separator.encode("utf-8"))
    )
    if budget < 0:
        # 关联说明本身超限（异常配置）：对关联说明兜底截断
        return _truncate_utf8(note, max_bytes)
    return f"{_truncate_utf8(body, budget)}{separator}{note}"


class LangfuseScoreSink:
    """Langfuse Score 投影/投递/回读/核验适配器（Domain ``ScoreSink``
    端口实现）。

    构造注入全部配置（endpoint/密钥/project/ScoreConfig 存储），组合根
    负责 B 类 env-only 缺省；密钥仅用于 Authorization 头，不进日志与
    异常。``redactor`` 缺省使用 LogSanitizer（脱敏规则与凭证目录由启动
    注入方决定）；测试可注入固定替换函数。
    """

    def __init__(
        self,
        *,
        base_url: str,
        public_key: str,
        secret_key: str,
        project: str,
        config_store: SinkConfigStore,
        transport: httpx.AsyncTransport | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
        redactor: Callable[[str], str] | None = None,
        comment_max_bytes: int = COMMENT_MAX_BYTES,
    ) -> None:
        self._base_url = self._validate_base_url(base_url)
        if not public_key or not secret_key:
            raise ValueError(
                "Langfuse public_key/secret_key 必须显式提供（构造注入）"
            )
        if not project:
            raise ValueError("Langfuse project 必须显式提供")
        if config_store is None:
            raise ValueError("Langfuse sink 需要 config_store")
        self._public_key = public_key
        self._secret_key = secret_key
        self._project = project
        self._config_store = config_store
        self._transport = transport
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._redactor = redactor or LogSanitizer().sanitize
        self._comment_max_bytes = comment_max_bytes

    @staticmethod
    def _validate_base_url(base_url: str) -> str:
        """endpoint 启动期固定：scheme 白名单 + hostname 必填 + 拒绝 URL
        内嵌凭据（对齐 memory/external/http_client 的校验模式；有意不做
        DNS 解析/私网阻断，见模块 docstring）。"""
        if not base_url:
            raise ValueError("Langfuse base_url 必须显式提供")
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"不支持的 URL scheme: {parsed.scheme!r}")
        if not parsed.hostname:
            raise ValueError("Langfuse base_url 缺少 hostname")
        if parsed.username or parsed.password:
            raise ValueError("Langfuse base_url 不得内嵌凭据")
        return base_url.rstrip("/")

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            token = base64.b64encode(
                f"{self._public_key}:{self._secret_key}".encode("utf-8")
            ).decode("ascii")
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={"Authorization": f"Basic {token}"},
                timeout=httpx.Timeout(self._timeout_seconds),
                transport=self._transport,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Domain ScoreSink 端口
    # ------------------------------------------------------------------

    async def project(self, context: Mapping[str, Any]) -> Mapping[str, Any]:
        """Domain ``ScoreSink`` 端口：构造投递投影（冻结 payload +
        external_id，含 configId）。

        unlinked 判定先于 ScoreConfig 创建：目标无真实身份、未采样或
        OTel 禁用时不发送请求、不创建悬空 Score、也不建 ScoreConfig
        （spec 11.1/13，无默认可关联）。Score name 以结果实际 provenance
        计算（human -> .human-v{major}），ScoreConfig 按 name 维度建/验，
        保证 config name 与 score name 恒一致。"""
        result = context["result"]
        target = context["target"]
        definition = context["definition"]
        linkability = context.get("linkability") or {}
        projection_version = context["projection_version"]
        score_name = build_score_name(
            definition["metric_name"],
            definition["method"],
            result.get("provenance") or "automatic",
            result.get("metric_version") or definition["version"],
        )

        probe = self._build_score_payload(
            result=result,
            target=target,
            definition=definition,
            linkability=linkability,
            projection_version=projection_version,
            score_name=score_name,
        )
        if probe is None:
            return {"outcome": "unlinked"}
        config_id = await self.ensure_config_id(
            definition, score_name=score_name
        )
        payload = {"id": None, **probe, "configId": config_id}
        external_id = compute_external_id(
            project=self._project,
            result_id=result["result_id"],
            projection_version=projection_version,
        )
        payload["id"] = external_id
        return {
            "outcome": "deliverable",
            "external_id": external_id,
            "payload": payload,
        }

    async def send(self, payload: Mapping[str, Any]) -> str:
        """发送已冻结 payload：2xx 返回 external_id（accepted，非
        verified）；分类失败按 spec 13 抛冲突/可重试异常。"""
        client = self._ensure_client()
        try:
            response = await client.post(
                _SCORES_PATH,
                content=canonical_json_bytes(dict(payload)),
                headers={"Content-Type": "application/json"},
            )
        except httpx.TimeoutException as exc:
            raise ScoreDeliveryRetryableError(
                f"Langfuse Score 请求超时: {exc}", error_type="timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise ScoreDeliveryRetryableError(
                f"Langfuse Score 网络错误: {exc}", error_type="network"
            ) from exc
        if response.status_code == 429:
            raise ScoreDeliveryRetryableError(
                "Langfuse Score 限流（HTTP 429）", error_type="rate_limit"
            )
        if response.status_code == 409:
            raise ScoreDeliveryConflictError(
                "Langfuse Score schema 冲突（HTTP 409）"
            )
        if not 200 <= response.status_code < 300:
            if response.status_code in (401, 403):
                # 认证/授权失败单列（运维直接定位密钥/权限配置问题）
                raise ScoreDeliveryConflictError(
                    f"Langfuse Score 认证/授权失败（HTTP "
                    f"{response.status_code}）",
                    error_type="auth",
                )
            if 400 <= response.status_code < 500:
                # 4xx schema 冲突隔离上报（dead_letter），不无限重试
                raise ScoreDeliveryConflictError(
                    f"Langfuse Score 请求被拒绝（HTTP {response.status_code}）"
                )
            raise ScoreDeliveryRetryableError(
                f"Langfuse Score HTTP 错误: {response.status_code}",
                error_type="network",
            )
        external_id = payload.get("id")
        if not isinstance(external_id, str) or not external_id:
            raise LangfuseSinkError("已冻结 payload 缺少 id 字段")
        return external_id

    async def read_back(self, external_id: str) -> Mapping[str, Any] | None:
        """按 external_id 回读 Score（4.76.0 events_only 合同）：v3 列表
        路由按 id 过滤，空列表 -> None（Score 先于 Trace 到达的有界等待
        由调度器重试），429/5xx/网络 -> 可重试异常，路由级 404/其他 4xx
        -> 冲突异常（固定 4.76.0 合同下 v3 路由必须存在，缺失视为部署
        不满足合同）。"""
        client = self._ensure_client()
        try:
            response = await client.get(
                _SCORES_V3_PATH, params={"id": external_id}
            )
        except httpx.TimeoutException as exc:
            raise ScoreDeliveryRetryableError(
                f"Langfuse Score 回读超时: {exc}", error_type="timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise ScoreDeliveryRetryableError(
                f"Langfuse Score 回读网络错误: {exc}", error_type="network"
            ) from exc
        if response.status_code == 429:
            raise ScoreDeliveryRetryableError(
                "Langfuse Score 回读限流（HTTP 429）", error_type="rate_limit"
            )
        if not 200 <= response.status_code < 300:
            if response.status_code in (401, 403):
                raise ScoreDeliveryConflictError(
                    f"Langfuse Score 回读认证/授权失败（HTTP "
                    f"{response.status_code}）",
                    error_type="auth",
                )
            if 400 <= response.status_code < 500:
                # 含路由级 404：4.76.0 合同下 v3 回读路由缺失
                raise ScoreDeliveryConflictError(
                    "Langfuse Score 回读被拒绝（HTTP "
                    f"{response.status_code}）"
                )
            raise ScoreDeliveryRetryableError(
                f"Langfuse Score 回读 HTTP 错误: {response.status_code}",
                error_type="network",
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ScoreDeliveryConflictError(
                f"Langfuse Score 回读不是 JSON: {exc}"
            ) from exc
        if not isinstance(body, dict) or not isinstance(
            body.get("data"), list
        ):
            raise ScoreDeliveryConflictError(
                "Langfuse Score 回读必须是 {data: [...] } 列表形态"
            )
        data = body["data"]
        if not data:
            return None
        item = data[0]
        if not isinstance(item, dict):
            raise ScoreDeliveryConflictError(
                "Langfuse Score 回读条目必须是对象"
            )
        return item

    async def verify(
        self,
        payload: Mapping[str, Any],
        read_back: Mapping[str, Any] | None,
    ) -> bool:
        """回读核验（spec 13，4.76.0 events_only 降级合同）：v3 条目仅含
        id/name/value/dataType 等字段，无法回读目标（traceId/
        observationId/sessionId）与 configId；核验降级为
        id/name/dataType/value 匹配。目标一致性由 external_id 的 UUIDv5
        确定性（project + result_id + projection_version，spec 11.1）保证
        ——同 result 重投复用同 ID，不同 result 不可能同 ID。None（空
        列表）一律 False。source 由 Langfuse 决定（API 写入应显示 API），
        不参与匹配也不本地伪造。"""
        if read_back is None:
            return False
        if read_back.get("id") != payload.get("id"):
            return False
        if read_back.get("name") != payload.get("name"):
            return False
        if read_back.get("dataType") != payload.get("dataType"):
            return False
        if read_back.get("value") != payload.get("value"):
            return False
        return True

    async def delete_external(self, external_id: str) -> str:
        """删除外部 Score（Domain ``ExternalDeleter`` 端口，spec 14）。

        Langfuse 公共 API ``DELETE /api/public/scores/{scoreId}``
        （4.76.0 events_only 合同：202 异步受理、Score 不存在 404，
        204 兼容旧同步合同）：
        202/204 -> "deleted"；404 -> "not_found"（删除轮的连续缺席重复
        核对由删除编排以 ``read_back`` 收敛，首次 404 不作结论）；
        429/5xx/网络/超时 -> 可重试异常、4xx -> 冲突异常（删除编排捕获
        后按 "unknown" 处理，任务保留 deletion_pending 退避，绝不宣称
        完成）。幂等语义：重复删除同一 ID 第二次得到 404（not_found），
        与"外部对象已不存在"同一语义，不产生误报。"""
        client = self._ensure_client()
        try:
            response = await client.delete(f"{_SCORES_PATH}/{external_id}")
        except httpx.TimeoutException as exc:
            raise ScoreDeliveryRetryableError(
                f"Langfuse Score 删除超时: {exc}", error_type="timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise ScoreDeliveryRetryableError(
                f"Langfuse Score 删除网络错误: {exc}", error_type="network"
            ) from exc
        if response.status_code in (202, 204):
            return "deleted"
        if response.status_code == 404:
            return "not_found"
        if response.status_code == 429:
            raise ScoreDeliveryRetryableError(
                "Langfuse Score 删除限流（HTTP 429）", error_type="rate_limit"
            )
        if not 200 <= response.status_code < 300:
            if response.status_code in (401, 403):
                raise ScoreDeliveryConflictError(
                    f"Langfuse Score 删除认证/授权失败（HTTP "
                    f"{response.status_code}）",
                    error_type="auth",
                )
            if 400 <= response.status_code < 500:
                raise ScoreDeliveryConflictError(
                    f"Langfuse Score 删除被拒绝（HTTP "
                    f"{response.status_code}）"
                )
            raise ScoreDeliveryRetryableError(
                f"Langfuse Score 删除 HTTP 错误: {response.status_code}",
                error_type="network",
            )
        # 2xx 非 202/204（合同外形态）：删除结果未知，不宣称完成
        raise ScoreDeliveryRetryableError(
            f"Langfuse Score 删除返回非 202/204 的 2xx（合同外）: "
            f"{response.status_code}",
            error_type="network",
        )

    # ------------------------------------------------------------------
    # 投影构建（纯逻辑 + ScoreConfig IO）
    # ------------------------------------------------------------------

    def _build_score_payload(
        self,
        *,
        result: Mapping[str, Any],
        target: Mapping[str, Any],
        definition: Mapping[str, Any],
        linkability: Mapping[str, Any],
        projection_version: str,
        score_name: str,
    ) -> dict[str, Any] | None:
        """构造 Score payload（不含 id/configId，由 project 补充）。

        ``score_name`` 由 project 按结果实际 provenance 统一计算（Score
        与其 ScoreConfig 的 name 恒一致）。返回 None 表示 unlinked：不
        创建悬空 Score（spec 11.1）。仅 succeeded（status=value）结果
        投影；skipped/error/cancelled 由调度器直接落 unlinked（不写 0、
        不伪造类别）。"""
        # 仅 succeeded 结果投影正式分值（spec 11.1）
        if result.get("status") != "value":
            return None
        otel_enabled = bool(linkability.get("otel_enabled"))
        sampled = bool(linkability.get("sampled"))
        is_session = target.get("kind") == "session_feedback"
        if is_session:
            session_id = target.get("session_id")
            if not session_id or not otel_enabled or not sampled:
                return None
            target_fields: dict[str, Any] = {"sessionId": session_id}
        else:
            # observation 评分要求同时提供两个真实 ID（trace_id +
            # span_id）；无真实身份 / 未采样 / OTel 禁用 -> unlinked，
            # 即使存在 trace_id 也不默认可关联
            trace_id = target.get("trace_id")
            span_id = target.get("span_id")
            if not trace_id or not span_id or not otel_enabled or not sampled:
                return None
            target_fields = {"traceId": trace_id, "observationId": span_id}

        typed_value = result.get("typed_value")
        if typed_value is None:
            return None
        value, data_type = _convert_value(typed_value)

        payload: dict[str, Any] = {
            "name": score_name,
            "value": value,
            "dataType": data_type,
            **target_fields,
        }
        # explanation（如投影输入携带）脱敏后入 comment，附 result_id 与
        # 定义 hash 的短关联说明
        payload["comment"] = redact_comment(
            str(result.get("explanation") or ""),
            result_id=result["result_id"],
            definition_hash=str(definition.get("definition_hash") or ""),
            redactor=self._redactor,
            max_bytes=self._comment_max_bytes,
        )
        # configId 由 project 调用 ensure_config_id 异步补齐（ScoreConfig
        # 创建/回读/校验见下方实现）
        return payload

    async def ensure_config_id(
        self, definition: Mapping[str, Any], *, score_name: str
    ) -> str:
        """Score name -> ScoreConfig（spec 11.1）：按投递的 score name
        维度持久化与校验（同一 definition_version 下 human/judge/
        deterministic 后缀各对应独立 config），保证 config name 与 score
        name 恒一致。本地已持久化则回读校验后复用；否则创建 ScoreConfig
        （409 按名解析）、回读匹配后持久化；schema 漂移冲突隔离
        （dead_letter 类），不静默覆盖。"""
        definition_id = definition["definition_id"]
        version = definition["version"]
        key = f"{definition_id}@{version}:{score_name}"
        parsed = definition.get("definition") or {}
        descriptor = self._config_descriptor(definition, parsed, score_name)
        schema_hash = hashlib.sha256(
            canonical_json_bytes(descriptor)
        ).hexdigest()
        stored: Mapping[str, Any] | None = None
        try:
            stored = await self._config_store.get_sink_config(
                "langfuse", self._project, key
            )
        except EvaluationNotFoundError:
            stored = None
        if stored is not None:
            config_id = stored["external_score_config_id"]
            remote = await self._get_score_config(config_id)
            if remote is None:
                # 远端配置已被删除：重新创建（无历史可冲突）
                return await self._create_score_config(
                    key, descriptor, schema_hash
                )
            self._assert_config_matches(remote, descriptor, config_id)
            self._assert_schema_hash_matches(
                stored, remote, descriptor, config_id
            )
            return config_id
        return await self._create_score_config(key, descriptor, schema_hash)

    def _config_descriptor(
        self,
        definition: Mapping[str, Any],
        parsed: Mapping[str, Any],
        score_name: str,
    ) -> dict[str, Any]:
        """本地 ScoreConfig 期望 schema（hash 锚点）：name（与投递的
        score name 一致）/dataType 与定义输出 schema（范围或类别）。"""
        output_type = parsed.get("output_type") or "number"
        data_type = _SCORE_DATA_TYPES[output_type]
        descriptor: dict[str, Any] = {
            "name": score_name,
            "dataType": data_type,
        }
        bounds = parsed.get("range_or_categories")
        if output_type == "number" and bounds:
            low, high = bounds
            descriptor["minValue"] = float(low)
            descriptor["maxValue"] = float(high)
        elif output_type == "categorical":
            descriptor["categories"] = [
                {"label": str(c), "value": str(c)} for c in (bounds or ())
            ]
        return descriptor

    async def _create_score_config(
        self, key: str, descriptor: dict[str, Any], schema_hash: str
    ) -> str:
        client = self._ensure_client()
        body: dict[str, Any] = {
            "name": descriptor["name"],
            "dataType": descriptor["dataType"],
        }
        if "minValue" in descriptor:
            body["minValue"] = descriptor["minValue"]
            body["maxValue"] = descriptor["maxValue"]
        if "categories" in descriptor:
            body["categories"] = descriptor["categories"]
        try:
            response = await client.post(
                _SCORE_CONFIGS_PATH,
                content=canonical_json_bytes(body),
                headers={"Content-Type": "application/json"},
            )
        except httpx.TimeoutException as exc:
            raise ScoreDeliveryRetryableError(
                f"Langfuse ScoreConfig 请求超时: {exc}", error_type="timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise ScoreDeliveryRetryableError(
                f"Langfuse ScoreConfig 网络错误: {exc}", error_type="network"
            ) from exc
        if response.status_code == 409:
            # 已存在：按名解析（不静默覆盖远端配置）
            return await self._resolve_config_by_name(
                key, descriptor, schema_hash
            )
        if response.status_code == 429:
            raise ScoreDeliveryRetryableError(
                "Langfuse ScoreConfig 限流（HTTP 429）",
                error_type="rate_limit",
            )
        if not 200 <= response.status_code < 300:
            if response.status_code in (401, 403):
                raise ScoreDeliveryConflictError(
                    f"Langfuse ScoreConfig 创建认证/授权失败（HTTP "
                    f"{response.status_code}）",
                    error_type="auth",
                )
            if 400 <= response.status_code < 500:
                raise ScoreDeliveryConflictError(
                    "Langfuse ScoreConfig 创建被拒绝（HTTP "
                    f"{response.status_code}）"
                )
            raise ScoreDeliveryRetryableError(
                f"Langfuse ScoreConfig HTTP 错误: {response.status_code}",
                error_type="network",
            )
        try:
            created = response.json()
        except ValueError as exc:
            raise ScoreDeliveryConflictError(
                f"Langfuse ScoreConfig 响应不是 JSON: {exc}"
            ) from exc
        config_id = created.get("id")
        if not isinstance(config_id, str) or not config_id:
            raise ScoreDeliveryConflictError(
                "Langfuse ScoreConfig 响应缺少 id"
            )
        remote = await self._get_score_config(config_id)
        self._assert_config_matches(remote, descriptor, config_id)
        await self._persist_config(key, config_id, schema_hash)
        return config_id

    async def _resolve_config_by_name(
        self, key: str, descriptor: dict[str, Any], schema_hash: str
    ) -> str:
        client = self._ensure_client()
        try:
            response = await client.get(
                _SCORE_CONFIGS_PATH,
                params={"name": descriptor["name"]},
            )
        except httpx.TimeoutException as exc:
            raise ScoreDeliveryRetryableError(
                f"Langfuse ScoreConfig 查询超时: {exc}", error_type="timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise ScoreDeliveryRetryableError(
                f"Langfuse ScoreConfig 查询网络错误: {exc}",
                error_type="network",
            ) from exc
        if not 200 <= response.status_code < 300:
            # 按名解析失败分类与其他路径一致：429/5xx 是瞬时错误（重试），
            # 只有非 429 的 4xx 才是永久拒绝（dead_letter）——否则一次
            # 瞬时错误会把投递永久打进 dead_letter
            if response.status_code == 429:
                raise ScoreDeliveryRetryableError(
                    "Langfuse ScoreConfig 按名解析限流（HTTP 429）",
                    error_type="rate_limit",
                )
            if response.status_code in (401, 403):
                raise ScoreDeliveryConflictError(
                    f"Langfuse ScoreConfig 按名解析认证/授权失败（HTTP "
                    f"{response.status_code}）",
                    error_type="auth",
                )
            if 400 <= response.status_code < 500:
                raise ScoreDeliveryConflictError(
                    f"Langfuse ScoreConfig 按名解析被拒绝（HTTP "
                    f"{response.status_code}）"
                )
            raise ScoreDeliveryRetryableError(
                f"Langfuse ScoreConfig 按名解析 HTTP 错误: "
                f"{response.status_code}",
                error_type="network",
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ScoreDeliveryConflictError(
                f"Langfuse ScoreConfig 列表不是 JSON: {exc}"
            ) from exc
        items = data.get("data") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ScoreDeliveryConflictError(
                "Langfuse ScoreConfig 列表形态非法"
            )
        for item in items:
            if (
                isinstance(item, dict)
                and item.get("name") == descriptor["name"]
            ):
                config_id = item.get("id")
                if not isinstance(config_id, str) or not config_id:
                    raise ScoreDeliveryConflictError(
                        "Langfuse ScoreConfig 条目缺少 id"
                    )
                self._assert_config_matches(item, descriptor, config_id)
                await self._persist_config(key, config_id, schema_hash)
                return config_id
        raise ScoreDeliveryConflictError(
            "Langfuse ScoreConfig 409 但按名未解析到配置"
        )

    async def _get_score_config(self, config_id: str) -> dict[str, Any] | None:
        client = self._ensure_client()
        try:
            response = await client.get(f"{_SCORE_CONFIGS_PATH}/{config_id}")
        except httpx.TimeoutException as exc:
            raise ScoreDeliveryRetryableError(
                f"Langfuse ScoreConfig 回读超时: {exc}", error_type="timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise ScoreDeliveryRetryableError(
                f"Langfuse ScoreConfig 回读网络错误: {exc}",
                error_type="network",
            ) from exc
        if response.status_code == 404:
            return None
        if not 200 <= response.status_code < 300:
            if response.status_code in (401, 403):
                raise ScoreDeliveryConflictError(
                    f"Langfuse ScoreConfig 回读认证/授权失败（HTTP "
                    f"{response.status_code}）",
                    error_type="auth",
                )
            if 400 <= response.status_code < 500:
                raise ScoreDeliveryConflictError(
                    f"Langfuse ScoreConfig 回读被拒绝（HTTP "
                    f"{response.status_code}）"
                )
            raise ScoreDeliveryRetryableError(
                f"Langfuse ScoreConfig 回读 HTTP 错误: "
                f"{response.status_code}",
                error_type="network",
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ScoreDeliveryConflictError(
                f"Langfuse ScoreConfig 回读不是 JSON: {exc}"
            ) from exc
        return data if isinstance(data, dict) else None

    @staticmethod
    def _assert_config_matches(
        remote: Mapping[str, Any] | None,
        descriptor: Mapping[str, Any],
        config_id: str,
    ) -> None:
        """回读 schema 校验：name/dataType/范围或类别必须与本地定义一致，
        冲突隔离不静默覆盖（spec 11.1）。"""
        if remote is None:
            raise ScoreDeliveryConflictError(
                f"Langfuse ScoreConfig 回读缺失: {config_id}"
            )
        if remote.get("name") != descriptor["name"]:
            raise ScoreDeliveryConflictError(
                f"Langfuse ScoreConfig name 漂移: {config_id} "
                f"remote={remote.get('name')!r} local={descriptor['name']!r}"
            )
        if remote.get("dataType") != descriptor["dataType"]:
            raise ScoreDeliveryConflictError(
                f"Langfuse ScoreConfig dataType 漂移: {config_id} "
                f"remote={remote.get('dataType')!r} "
                f"local={descriptor['dataType']!r}"
            )
        if "minValue" in descriptor:
            if remote.get("minValue") is None or remote.get("maxValue") is None:
                raise ScoreDeliveryConflictError(
                    f"Langfuse ScoreConfig 范围字段缺失: {config_id}"
                )
            if float(remote.get("minValue")) != descriptor["minValue"] or float(
                remote.get("maxValue")
            ) != descriptor["maxValue"]:
                raise ScoreDeliveryConflictError(
                    f"Langfuse ScoreConfig 范围漂移: {config_id}"
                )
        if "categories" in descriptor:
            remote_categories = remote.get("categories") or []
            remote_labels = sorted(
                str(c.get("label"))
                for c in remote_categories
                if isinstance(c, dict)
            )
            local_labels = sorted(
                str(c["label"]) for c in descriptor["categories"]
            )
            if remote_labels != local_labels:
                raise ScoreDeliveryConflictError(
                    f"Langfuse ScoreConfig 类别漂移: {config_id}"
                )

    def _assert_schema_hash_matches(
        self,
        stored: Mapping[str, Any],
        remote: Mapping[str, Any],
        descriptor: Mapping[str, Any],
        config_id: str,
    ) -> None:
        """持久化 schema_hash 与远端实时描述符一致性校验（schema_hash
        落库必须有消费方，否则只是死数据）：逐字段相等比较对"值相等但
        类型/形态漂移"（如 minValue 数值变字符串）不设防，这里用与落库
        时同一 canonical JSON hash 锚点兜底——从远端按本地描述符键集
        投影后哈希，与存储 hash 不一致即冲突隔离（dead_letter 类）。
        旧行缺 schema_hash 时由逐字段校验兜底，不误伤。"""
        stored_hash = stored.get("schema_hash")
        if not stored_hash:
            return
        projection = self._normalize_descriptor_projection(
            {key: remote[key] for key in descriptor if key in remote}
        )
        remote_hash = hashlib.sha256(
            canonical_json_bytes(projection)
        ).hexdigest()
        if remote_hash != stored_hash:
            raise ScoreDeliveryConflictError(
                f"Langfuse ScoreConfig schema hash 漂移: {config_id} "
                f"(stored={stored_hash} remote={remote_hash})"
            )

    @staticmethod
    def _normalize_descriptor_projection(value: Any) -> Any:
        """hash 投影归一化：JSON 数值不区分 int/float（远端 0 与本地
        0.0 语义相同），统一转 float 后再哈希，避免合法配置被误判漂移；
        bool 是数值但语义独立，保持原样。"""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, list):
            return [
                LangfuseScoreSink._normalize_descriptor_projection(item)
                for item in value
            ]
        if isinstance(value, dict):
            return {
                key: LangfuseScoreSink._normalize_descriptor_projection(item)
                for key, item in value.items()
            }
        return value

    async def _persist_config(
        self, key: str, config_id: str, schema_hash: str
    ) -> None:
        await self._config_store.save_sink_config(
            {
                "sink": "langfuse",
                "project": self._project,
                "definition_version": key,
                "external_score_config_id": config_id,
                "schema_hash": schema_hash,
            }
        )

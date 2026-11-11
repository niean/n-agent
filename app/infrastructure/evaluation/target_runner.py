# app/infrastructure/evaluation/target_runner.py
"""实验目标执行适配与受信实验采样（plan T13，spec 9.1 / 11.1）。

本模块是 T13 的 Infrastructure 侧落点（plan "明确新增 Infrastructure target
adapter 与可信实验 sampler"）：

- ``ChatCompletionTargetRunner``：N-Agent 目标适配——冻结配置驱动注入的
  ChatCompletionService 逐轮执行（多轮 case 共享同一 session_id）；本模块
  位于 Infrastructure，Application 编排只依赖 Domain ``EvaluationTargetRunner``
  端口，不 import 本模块，也不使用 httpx。本适配为证据无关适配：
  ChatCompletionResult 契约不携带副作用证明，``side_effects`` 恒为空列表，
  不伪造副作用证据；因此编排层的"仅无副作用传输失败可重试"门禁对本
  适配的 transport 失败按无副作用处理（重试门只约束上报副作用证据的
  适配器）；
  已知后果：agent_run 项的 agent.task_success 在证据缺失时按
  skipped(evidence_missing) 处理（不伪造 false/0），待 T16/T20 接线真实
  副作用证据提取后恢复计分。
- ``KBDirectTargetRunner``：N-KB 目标适配——经组合根注入的直调 adapter
  （async callable）执行检索；不引入 HTTP 客户端（N-KB 接入形态由组合根
  决定）。
- ``build_frozen_environment``：冻结快照环境指纹（spec 9.1）——代码 commit
  经只读 ``git rev-parse HEAD`` 读取（绝不执行任何变更性 git 命令）；指纹
  字符串值原样冻结，结构化值由本层 canonical-JSON SHA-256 折叠为哈希
  （应用层契约只接受非空字符串，见 evaluation_experiment）；缺失/空项标
  ``external_unpinned``。
- ``TrustedExperimentSampler`` + ``experiment_item_root_scope``：实验项根采样
  （spec 11.1）——仅当 Context 中存在服务端私有的受信实验令牌且父 span
  无效（即正在创建实验项根）时强制 RECORD_AND_SAMPLE（覆盖 ParentBased）；
  子 span 有有效父级，继续走 ParentBased 继承；远端 N-KB 继承其自身父级
  决定。受信令牌是模块私有的 ``Context`` 键值，客户端 header/baggage 只能
  产生 baggage 键，永远无法构造该键，因此不能开启此覆盖。OTel 整体禁用时
  proxy tracer 返回 NonRecordingSpan，句柄无效，实验分析标 unavailable；
  本模块不替换全局 TracerProvider，也不在业务 span 结束后补造采样。

组合根接线：``app/infrastructure/observability/setup.py`` 的 ``_build_sampler``
将 ``ParentBased(TraceIdRatioBased(ratio))`` 包裹进 ``TrustedExperimentSampler``；
实验编排（Application）经构造注入 ``experiment_item_root_scope`` 触发受信
上下文（文档化端口，Application 不感知 sampler 实现）。
"""
from __future__ import annotations

import hashlib
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping

from opentelemetry import context as context_api
from opentelemetry import trace
from opentelemetry.trace import Link, SpanContext, SpanKind, TraceFlags

from app.application.evaluation_experiment import ExperimentRootHandle
from app.application import telemetry_semantics as ts
from app.domain.evaluation.models import (
    EXTERNAL_UNPINNED,
    FROZEN_ENVIRONMENT_FIELDS,
    canonical_json_bytes,
)
from app.infrastructure.observability.experiment_sampler import (
    TRUSTED_EXPERIMENT_CONTEXT_KEY,
    TRUSTED_EXPERIMENT_TOKEN,
    TrustedExperimentSampler,
)

# 受信实验采样器与受信上下文键的权威定义在
# app/infrastructure/observability/experiment_sampler.py（observability 包
# 边界：无 app.* 业务依赖）；此处 re-export 作为组合根/测试的单一引用面。
__all__ = [
    "ChatCompletionTargetRunner",
    "CompositeTargetRunner",
    "KBDirectTargetRunner",
    "TrustedExperimentSampler",
    "TRUSTED_EXPERIMENT_CONTEXT_KEY",
    "build_frozen_environment",
    "default_git_commit_reader",
    "experiment_item_root_scope",
]

# ---------------------------------------------------------------------------
# 实验项根 scope（受信实验上下文的唯一写入方）
# ---------------------------------------------------------------------------

# 实验身份属性键（T11 Collector 白名单契约，精确映射 langfuse.experiment.*）
EXPERIMENT_ID_ATTR = "nagent.evaluation.experiment.id"
EXPERIMENT_NAME_ATTR = "nagent.evaluation.experiment.name"
EXPERIMENT_DATASET_ID_ATTR = "nagent.evaluation.experiment.dataset_id"
EXPERIMENT_ITEM_ID_ATTR = "nagent.evaluation.experiment.item_id"
EXPERIMENT_ROOT_OBSERVATION_ID_ATTR = (
    "nagent.evaluation.experiment.root_observation_id"
)

_MAX_IDENTITY_LENGTH = 128


@contextmanager
def experiment_item_root_scope(
    identity: Mapping[str, Any],
) -> Iterator[ExperimentRootHandle]:
    """实验项根 Trace 作用域（每个 case/replicate 一个根，包住该项全部
    业务调用）。

    - 在创建根前向 Context 写入服务端私有受信令牌，由 TrustedExperimentSampler
      对根强制采样；子 span（作用域内的业务/Judge 调用）有有效父级，继续
      ParentBased；
    - 注入 T11 白名单的实验身份属性（experiment id/name/dataset_id/item_id）
      与 root_observation_id（必须等于根的真实 span ID）；
    - ``links`` 为 ((trace_id, span_id), ...)：replay 经 Link 关联原目标真实
      身份，不改写原业务历史；
    - OTel 禁用（proxy tracer）时句柄无效（is_valid=False），实验分析标
      unavailable，不抛错。
    """
    links: list[Link] = []
    for link_ref in identity.get("links") or ():
        try:
            link_trace, link_span = link_ref
            links.append(
                Link(
                    SpanContext(
                        trace_id=int(str(link_trace), 16),
                        span_id=int(str(link_span), 16),
                        is_remote=True,
                        trace_flags=TraceFlags(TraceFlags.SAMPLED),
                    )
                )
            )
        except Exception:
            continue  # 非法引用不构造假 Link
    attributes = ts.sanitize_attributes(
        {
            EXPERIMENT_ID_ATTR: str(identity.get("experiment_id") or "")[
                :_MAX_IDENTITY_LENGTH
            ],
            EXPERIMENT_NAME_ATTR: str(identity.get("experiment_name") or "")[
                :_MAX_IDENTITY_LENGTH
            ],
            EXPERIMENT_DATASET_ID_ATTR: str(
                identity.get("dataset_id") or ""
            )[:_MAX_IDENTITY_LENGTH],
            EXPERIMENT_ITEM_ID_ATTR: str(identity.get("item_id") or "")[
                :_MAX_IDENTITY_LENGTH
            ],
        }
    )
    trusted_context = context_api.set_value(
        TRUSTED_EXPERIMENT_CONTEXT_KEY,
        TRUSTED_EXPERIMENT_TOKEN,
        context_api.get_current(),
    )
    try:
        span = ts.get_tracer().start_span(
            "experiment.item",
            context=trusted_context,
            kind=SpanKind.INTERNAL,
            attributes=attributes,
            links=links,
        )
    except Exception:
        span = trace.NonRecordingSpan(trace.INVALID_SPAN_CONTEXT)
    span_context = span.get_span_context()
    is_valid = bool(getattr(span_context, "is_valid", False))
    handle = ExperimentRootHandle(
        trace_id=format(span_context.trace_id, "032x") if is_valid else None,
        span_id=format(span_context.span_id, "016x") if is_valid else None,
        is_valid=is_valid,
    )
    if is_valid:
        # root_observation_id 必须等于实验项根的真实 span ID（spec 11.2）
        try:
            span.set_attribute(
                EXPERIMENT_ROOT_OBSERVATION_ID_ATTR, handle.span_id
            )
        except Exception:
            pass
    use = None
    try:
        use = trace.use_span(span, end_on_exit=False, record_exception=False)
        use.__enter__()
    except Exception:
        use = None
    try:
        yield handle
    finally:
        if use is not None:
            try:
                use.__exit__(None, None, None)
            except Exception:
                pass
        try:
            span.end()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 冻结快照环境指纹（spec 9.1）
# ---------------------------------------------------------------------------


def default_git_commit_reader(repo_dir: str | None = None) -> str:
    """只读读取当前代码 commit（``git rev-parse HEAD``）。

    禁止任何变更性 git 命令；读取失败抛异常，由调用方标记
    external_unpinned。
    """
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
        cwd=repo_dir,
    )
    return proc.stdout.strip()


def _freeze_fingerprint_value(value: Any) -> str:
    """指纹值折叠：字符串原样；结构化值做 canonical-JSON SHA-256 哈希
    （保留结构可校验性，应用层只接受非空字符串）；None/空串标
    external_unpinned；无法 JSON 规范化的值按领域契约 fail-fast。"""
    if isinstance(value, str):
        return value if value else EXTERNAL_UNPINNED
    if value is None:
        return EXTERNAL_UNPINNED
    payload = value if isinstance(value, Mapping) else {"value": value}
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def build_frozen_environment(
    *,
    commit_reader: Callable[..., str] | None = None,
    fingerprints: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构建冻结快照环境指纹（spec 9.1）：commit 读取失败或指纹字段缺失
    一律标 ``external_unpinned``（不承诺 bitwise reproducibility）。"""
    reader = commit_reader or default_git_commit_reader
    try:
        commit = reader()
    except Exception:
        commit = None
    provided = dict(fingerprints or {})
    frozen: dict[str, Any] = {}
    for field_name in FROZEN_ENVIRONMENT_FIELDS:
        if field_name == "code_commit":
            frozen[field_name] = _freeze_fingerprint_value(commit)
        else:
            frozen[field_name] = _freeze_fingerprint_value(
                provided.get(field_name)
            )
    return frozen


# ---------------------------------------------------------------------------
# 目标执行适配（N-Agent / N-KB）
# ---------------------------------------------------------------------------


def _classify_exception(
    exc: Exception, classifier: Callable[[Exception], str] | None
) -> tuple[str, str]:
    """异常 -> (status, error_type)。默认：异常视为传输失败（网络/超时），
    业务失败语义由组合根注入分类器声明。"""
    if classifier is not None:
        verdict = classifier(exc)
        if verdict == "business":
            return "business_error", "business_error"
    return "transport_error", "network"


def _turn_messages(turn: Mapping[str, Any]) -> list[dict[str, Any]]:
    """单轮输入 -> Chat messages：显式 messages 优先，否则 message 文本。"""
    messages = turn.get("messages")
    if isinstance(messages, list) and messages:
        return [dict(m) for m in messages]
    return [{"role": "user", "content": str(turn.get("message", ""))}]


# 检索默认 top_k（turn 未显式声明时的保守取值）
DEFAULT_KB_TOP_K = 5


class ChatCompletionTargetRunner:
    """N-Agent 目标适配：冻结配置驱动注入的 ChatCompletionService。

    多轮 case（request["turns"] 数组）在同一 session_id 内逐轮执行（共享
    会话）；每 replicate 由编排层分配独立 session_id，实现 replicate 独立
    业务执行。运行健康/工具约束证据由快照内容承载。本适配为证据无关
    适配：
    ChatCompletionResult 契约不携带副作用证明，因此 ``side_effects`` 恒为
    空列表、输出不携带 side_effect_evidence 字段，不伪造副作用证据；编排层
    的重试门只约束上报副作用证据的适配器，对本适配的 transport 失败按无
    副作用处理。已知后果：agent_run 项的 agent.task_success 在证据缺失时
    按 skipped(evidence_missing) 处理，待 T16/T20 接线真实副作用证据提取后
    恢复计分。
    """

    def __init__(
        self,
        *,
        chat: Any,
        model: str,
        error_classifier: Callable[[Exception], str] | None = None,
    ) -> None:
        self._chat = chat
        self._model = model
        self._classify = error_classifier

    async def run_target(
        self, target: Any, request: Mapping[str, Any]
    ) -> dict[str, Any]:
        from app.application.chat_service import ChatCompletionInput

        session = request.get("session") or {}
        session_id = session.get("session_id")
        namespace = session.get("namespace")
        turns = list(request.get("turns") or ())
        turn_outputs: list[dict[str, Any]] = []
        run_id: str | None = None
        try:
            for turn in turns:
                result = await self._chat.complete(
                    ChatCompletionInput(
                        model=self._model,
                        messages=_turn_messages(turn),
                        stream=False,
                        session_id=session_id,
                        metadata={
                            "execution_kind": "evaluation_experiment",
                            "session_namespace": namespace or "",
                        },
                    )
                )
                message = getattr(result, "message", None) or {}
                run_id = getattr(result, "run_id", None) or run_id
                session_id = getattr(result, "session_id", None) or session_id
                # finish_reason 缺失按未完成处理（保守口径，不默认成功）
                completion_reported = (
                    getattr(result, "finish_reason", None) == "stop"
                )
                turn_outputs.append(
                    {
                        "final_answer": message.get("content"),
                        "completion_reported": completion_reported,
                        "model_response_version": getattr(result, "model", None),
                        "run_id": getattr(result, "run_id", None),
                    }
                )
        except Exception as exc:  # noqa: BLE001 - 分类后受控返回
            status, error_type = _classify_exception(exc, self._classify)
            return {
                "status": status,
                "error_type": error_type,
                "output": {},
                # 证据无关适配：不产出副作用证据，恒为空（不伪造）
                "side_effects": [],
                "retrievals": [],
                "run_id": run_id,
            }
        last = turn_outputs[-1] if turn_outputs else {}
        return {
            "status": "succeeded",
            "output": {
                "completion_reported": bool(
                    last.get("completion_reported", False)
                ),
                "final_answer": last.get("final_answer"),
                "tool_calls": [],
                "allowed_tools": [],
                "turns": turn_outputs,
                "model_response_version": last.get("model_response_version"),
            },
            # 证据无关适配：不产出副作用证据，恒为空（不伪造）
            "side_effects": [],
            "retrievals": [],
            "run_id": run_id,
        }


class KBDirectTargetRunner:
    """N-KB 目标适配：经组合根注入的直调 adapter 执行冻结配置检索。

    每次 retrieve 必须返回唯一 retrieval_id 与真实 trace_id/span_id（KB
    指标附着真实 retrieval.search observation 的前提）；adapter 不可用时
    产生的失败按异常分类（默认传输失败）。
    """

    def __init__(
        self,
        *,
        retrieve: Callable[[Mapping[str, Any]], Any],
        error_classifier: Callable[[Exception], str] | None = None,
    ) -> None:
        self._retrieve = retrieve
        self._classify = error_classifier

    async def run_target(
        self, target: Any, request: Mapping[str, Any]
    ) -> dict[str, Any]:
        session = request.get("session") or {}
        entries: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        corpus_revision: str | None = None
        try:
            for turn in request.get("turns") or ():
                response = await self._retrieve(
                    {
                        "query": turn.get("query") or turn.get("message") or "",
                        "top_k": turn.get("top_k", DEFAULT_KB_TOP_K),
                        "filters": dict(turn.get("filters") or {}),
                        "session_id": session.get("session_id"),
                    }
                )
                entry = {
                    "retrieval_id": str(response.get("retrieval_id") or ""),
                    "trace_id": response.get("trace_id"),
                    "span_id": response.get("span_id"),
                    "corpus_revision": response.get("corpus_revision"),
                    "query": turn.get("query") or turn.get("message") or "",
                    "results": list(response.get("results") or []),
                    "chunks": list(response.get("chunks") or []),
                }
                # #7：逐条 result 盖 corpus_revision 章——worker kb 指标
                # 构建器按 result 行读取 corpus_revision（qrels 按
                # corpus+document+chunk 标注），顶层单一 revision 覆盖多
                # 检索条目时无法归属；条目缺失 revision 时以 None 章
                # （worker 构建器按契约失败处理，不静默猜配）
                for result_row in entry["results"]:
                    if isinstance(result_row, dict) and not result_row.get(
                        "corpus_revision"
                    ):
                        result_row["corpus_revision"] = entry["corpus_revision"]
                entries.append(entry)
                results.extend(entry["results"])
                corpus_revision = entry["corpus_revision"] or corpus_revision
        except Exception as exc:  # noqa: BLE001 - 分类后受控返回
            status, error_type = _classify_exception(exc, self._classify)
            return {
                "status": status,
                "error_type": error_type,
                "output": {},
                "side_effects": [],
                "retrievals": [],
                "run_id": None,
            }
        return {
            "status": "succeeded",
            "output": {
                "results": results,
                "corpus_revision": corpus_revision,
            },
            "side_effects": [],
            "retrievals": entries,
            "run_id": None,
        }


class AgentHTTPTargetRunner:
    """N-Agent agent_run 目标适配（#3）：经本机 HTTP /v1/chat/completions
    执行冻结配置的多轮对话目标。

    CLI 进程不重建应用全图——agent_run 目标要求完整 Chat/Provider/Skill
    图，只有运行中的 N-Agent 服务进程拥有；本适配调用该服务进程的
    OpenAI 兼容端点（stream=false），复用其 run/账本/观测链路：

    - 会话隔离：X-Session-ID 承载编排层分配的 session_id（同 case 多轮
      共享会话，同 replicate 独立会话）；
    - metadata 透传 execution_kind=evaluation_experiment + session_
      namespace（chat_service 按此打点实验会话隔离）；
    - 契约：succeeded 输出 final_answer/completion_reported/run_id/
      turns；finish_reason != "stop" 的轮次按未完成处理；HTTP/网络失败
      按传输失败分类（无副作用观测，可安全重试）。证据无关适配（不产
      副作用证据，恒空列表，不伪造）。
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: float = 120.0,
        client: Any = None,
        error_classifier: Callable[[Exception], str] | None = None,
    ) -> None:
        import httpx as _httpx

        if not base_url or not model:
            raise ValueError("agent base_url 与 model 必须显式提供")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._classify = error_classifier
        self._client = client or _httpx.AsyncClient(
            timeout=_httpx.Timeout(timeout_seconds)
        )

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass

    async def run_target(
        self, target: Any, request: Mapping[str, Any]
    ) -> dict[str, Any]:
        session = request.get("session") or {}
        session_id = session.get("session_id")
        namespace = session.get("namespace")
        turn_outputs: list[dict[str, Any]] = []
        run_id: str | None = None
        try:
            for turn in request.get("turns") or ():
                response = await self._client.post(
                    f"{self._base_url}/v1/chat/completions",
                    json={
                        "model": self._model,
                        "messages": _turn_messages(turn),
                        "stream": False,
                        "metadata": {
                            "execution_kind": "evaluation_experiment",
                            "session_namespace": namespace or "",
                        },
                    },
                    headers=(
                        {"X-Session-ID": session_id} if session_id else {}
                    ),
                )
                if response.status_code >= 500:
                    raise RuntimeError(
                        f"agent HTTP 错误: {response.status_code}"
                    )
                if response.status_code != 200:
                    # 4xx：请求契约问题（如 session scope mismatch）按
                    # 业务失败处理，不重试
                    return {
                        "status": "business_error",
                        "error_type": f"agent_http_{response.status_code}",
                        "output": {},
                        "side_effects": [],
                        "retrievals": [],
                        "run_id": run_id,
                    }
                data = response.json()
                choices = data.get("choices") or []
                message = (
                    choices[0].get("message") if choices else None
                ) or {}
                run_id = str(data.get("run_id") or "") or run_id
                turn_outputs.append(
                    {
                        "final_answer": message.get("content"),
                        "completion_reported": (
                            data.get("choices", [{}])[0].get("finish_reason")
                            == "stop"
                        ),
                        "model_response_version": data.get("model"),
                        "run_id": data.get("run_id"),
                    }
                )
                new_session = response.headers.get("x-session-id")
                if new_session:
                    session_id = new_session
        except Exception as exc:  # noqa: BLE001 - 分类后受控返回
            status, error_type = _classify_exception(exc, self._classify)
            return {
                "status": status,
                "error_type": error_type,
                "output": {},
                "side_effects": [],
                # 24 项三方审阅 #16：HTTP 适配驱动真实业务执行，超时/断连
                # 时服务端可能已执行写工具——空 side_effects 只是未采集，
                # 不能证明无副作用（spec 9.1 重试安全前提不成立）
                "side_effects_unobserved": True,
                "retrievals": [],
                "run_id": run_id,
            }
        if not turn_outputs:
            return {
                "status": "business_error",
                "error_type": "empty_turns",
                "output": {},
                "side_effects": [],
                "retrievals": [],
                "run_id": run_id,
            }
        last = turn_outputs[-1]
        return {
            "status": "succeeded",
            "output": {
                "completion_reported": bool(
                    last.get("completion_reported", False)
                ),
                "final_answer": last.get("final_answer"),
                "tool_calls": [],
                "allowed_tools": [],
                "turns": turn_outputs,
                "model_response_version": last.get("model_response_version"),
            },
            "side_effects": [],
            "retrievals": [],
            "run_id": run_id,
        }


class CompositeTargetRunner:
    """按目标 kind 分派的组合适配（T22 组合根接线用）。

    ``runners`` 以 ``EvaluationTarget.kind`` 为键（agent_run / retrieval）。
    未接线的 kind 按传输失败诚实上报（error_type=target_unwired）：不伪造
    成功，也不以契约违规 fail-fast 整个实验——该项经编排层单项隔离记
    error 结局，其余项继续。"""

    def __init__(self, *, runners: Mapping[str, Any]) -> None:
        self._runners = dict(runners)

    async def run_target(
        self, target: Any, request: Mapping[str, Any]
    ) -> dict[str, Any]:
        runner = self._runners.get(getattr(target, "kind", ""))
        if runner is None:
            return {
                "status": "transport_error",
                "error_type": "target_unwired",
                "output": {},
                "side_effects": [],
                "retrievals": [],
                "run_id": None,
            }
        return await runner.run_target(target, request)

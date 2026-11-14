"""场景通道执行适配（plan T6）：Dashboard SSE(stream=true) + 正式端口证据。

``ScenarioDashboardSSERunner`` 消费实际 ``/chat/completions`` SSE 流
（等待 ``data: [DONE]`` 终止标记），工具证据通过正式端口（会话隔离的
``/chat/sessions/{id}/tool-calls``）读取真实 run/tool 记录——模型输出
文本不是证据，不从回答正文合成工具调用。不改变旧
``AgentHTTPTargetRunner`` 非流式合同。

证据纪律（spec 5.2）：
- 会话映射缺失（finish 事件无 run_id 且无会话归属）-> business_error，
  不得声称成功；
- SSE 未终止（流提前关闭）-> transport_error + side_effects_unobserved
  （服务端可能已执行副作用，不伪造无副作用）；
- 正式端口读取失败 -> scenario_evidence status=incomplete（读取失败
  不等于空集合，更不等于通过）；
- 适配层只透传正式端口证据，不做任何删改或来源合成。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Mapping

from app.infrastructure.evaluation.scenario_memory import ScenarioMemoryError
from app.infrastructure.evaluation.scenario_process import run_process as _default_run_process
from app.infrastructure.evaluation.target_runner import (
    _classify_exception,
    _turn_messages,
)


def _kb_retrievals(records: Any) -> list[dict[str, Any]]:
    """正式端口 search_knowledge 成功记录 -> 检索证据条目（plan T8）。

    只透传不合成：results 为后端原始条目（含 title/content/source/
    document_revision），backend 取记录 backend_type（search_knowledge
    即 N-KB 检索工具），kb_id 随条目透传；run_id 沿用记录归属供跨 run
    证据过滤。端口实测形态：result 是序列化 JSON 字符串，工具负载在
    content 键下——两种形态（dict 直载 / str 包装）都接受。"""
    entries: list[dict[str, Any]] = []
    for call in records if isinstance(records, list) else []:
        if not isinstance(call, Mapping):
            continue
        if call.get("tool_name") != "search_knowledge":
            continue
        if call.get("status") != "success":
            continue
        payload: Any = call.get("result")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                continue
        if (
            isinstance(payload, Mapping)
            and isinstance(payload.get("content"), Mapping)
        ):
            # tool-calls 端口 result_json 包装：{tool_call_id, name,
            # status, content, duration_ms}
            payload = payload["content"]
        if not isinstance(payload, Mapping):
            continue
        results = payload.get("results")
        if not isinstance(results, list):
            continue
        backend = payload.get("backend_type") or "n_kb"
        entries.append(
            {
                "retrieval_id": str(call.get("id") or ""),
                "backend": str(backend),
                "kb_id": payload.get("kb_id"),
                "query": payload.get("query"),
                "results": list(results),
                "run_id": call.get("run_id"),
            }
        )
    return entries


TERMINAL_TASK_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "expired"}
)


async def _call_hook(hook, *args):
    """钩子同步/异步双兼容（组合根注入的账目钩子为异步，测试可用同步）。"""
    result = hook(*args)
    if result is not None and hasattr(result, "__await__"):
        result = await result
    return result


def _tool_record_payload(call: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """tool-calls 端口记录的 result 载荷（dict 直载或序列化 JSON 字符串，
    负载在 content 键下——与 _kb_retrievals 同一包装契约；content 本身
    再为序列化字符串时也解析）。"""
    payload: Any = call.get("result")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    if isinstance(payload, Mapping) and isinstance(payload.get("content"), Mapping):
        payload = payload["content"]
    elif (
        isinstance(payload, Mapping) and isinstance(payload.get("content"), str)
    ):
        try:
            payload["content"] = json.loads(payload["content"])
        except ValueError:
            pass
        if isinstance(payload.get("content"), Mapping):
            payload = payload["content"]
    return payload if isinstance(payload, Mapping) else None


def _task_ids_from_tool_calls(records: Any) -> list[str]:
    """create_task 成功记录 -> 绑定 task_id（chat 题 agent 自行创建，
    从正式端口记录绑定，不重跑目标）。端口实测形态：result 为序列化
    JSON 字符串，任务负载在 content（可能再为序列化字符串）的 task.id
    嵌套。"""
    ids: list[str] = []
    for call in records if isinstance(records, list) else []:
        if not isinstance(call, Mapping):
            continue
        if call.get("tool_name") != "create_task" or call.get("status") != "success":
            continue
        payload = _tool_record_payload(call)
        if payload is None:
            continue
        task: Any = payload.get("task")
        if isinstance(task, str):
            try:
                task = json.loads(task)
            except ValueError:
                task = None
        if isinstance(task, Mapping):
            candidate = task.get("id") or task.get("task_id")
        else:
            candidate = payload.get("task_id")
        if isinstance(candidate, str) and candidate and candidate not in ids:
            ids.append(candidate)
    return ids


class ScenarioDashboardSSERunner:
    """Dashboard SSE 场景目标适配（agent/dashboard profile 共用）。

    会话管理：按编排层分配的 session_id 先经 ``POST /chat/sessions``
    创建（Dashboard 端点要求既有会话），随后逐轮 ``POST /chat/completions``
    （stream=true + X-Session-ID）。每 replicate 独立会话，由编排层保证。
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: float = 300.0,
        client: Any = None,
        tool_evidence_reader: Callable[[str], Any] | None = None,
        error_classifier: Callable[[Exception], str] | None = None,
        session_prepare: Callable[[str, str], Any] | None = None,
        browser_state_reader: Callable[[str], Any] | None = None,
        browser_observer: Callable[[str, Any], Any] | None = None,
        task_creator: Callable[..., Any] | None = None,
        task_reader: Callable[[str], Any] | None = None,
        task_runs_reader: Callable[[str], Any] | None = None,
        artifact_lister: Callable[[str], Any] | None = None,
        workspace_file_reader: Callable[[str], Any] | None = None,
        task_intent_before: Callable[[Mapping[str, Any], Any], Any] | None = None,
        task_intent_confirmed: Callable[[str, str], Any] | None = None,
        task_intent_unknown: Callable[[str, str], Any] | None = None,
        workspace_resource_observer: Callable[[Any, Any], Any] | None = None,
        clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Any] | None = None,
        task_deadline_seconds: float = 600.0,
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
        self._tool_reader = tool_evidence_reader
        # T7：场景记忆准备钩子（session_id, alias, context）——在首轮前绑定
        # 独立记忆空间、预置合成事实并登记资源 intent
        self._session_prepare = session_prepare
        # T9：浏览器会话证据（session_id -> 会话记录列表/None）与账目观察
        # 钩子（session_id, records, context）——组合根注入
        self._browser_state_reader = browser_state_reader
        self._browser_observer = browser_observer
        # T10：Task/制品/文件端口（组合根注入，全部失败 fail-closed）
        self._task_creator = task_creator
        self._task_reader = task_reader
        self._task_runs_reader = task_runs_reader
        self._artifact_lister = artifact_lister
        self._workspace_file_reader = workspace_file_reader
        # T10：task intent 纪律钩子（副作用前登记 / 成功回填 / 断网未知）
        self._task_intent_before = task_intent_before
        self._task_intent_confirmed = task_intent_confirmed
        self._task_intent_unknown = task_intent_unknown
        # T10：文件/制品资源登记观察（证据读取后按实际 hash/id 登记，
        # 释放走执行级两阶段纪律）
        self._workspace_resource_observer = workspace_resource_observer
        import time as _time
        self._wall_clock = wall_clock or _time.time

        self._clock = clock or _time.monotonic
        self._sleep = sleep
        self._task_deadline_seconds = float(task_deadline_seconds)
        self._options: dict[str, Any] = {}

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # SSE 消费
    # ------------------------------------------------------------------

    async def _consume_sse(
        self, session_id: str, turn: Mapping[str, Any], namespace: str = "",
    ) -> dict[str, Any]:
        """单轮 SSE 消费：返回 {answer, run_id, finish_reason, terminated}。"""
        import httpx as _httpx

        answer_parts: list[str] = []
        run_id: str | None = None
        finish_reason: str | None = None
        terminated = False
        payload = {
            "model": self._model,
            "messages": _turn_messages(turn),
            "stream": True,
            "metadata": {
                "execution_kind": "evaluation_experiment",
                "session_namespace": namespace,
            },
        }
        if self._options:
            payload["options"] = dict(self._options)
        async with self._client.stream(
            "POST",
            f"{self._base_url}/chat/completions",
            json=payload,
            headers={"X-Session-ID": session_id},
        ) as response:
            if response.status_code >= 500:
                raise RuntimeError(
                    f"agent SSE HTTP 错误: {response.status_code}"
                )
            if response.status_code != 200:
                return {
                    "business_error": True,
                    "error_type": f"agent_http_{response.status_code}",
                }
            async for line in response.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    terminated = True
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                choices = chunk.get("choices") or []
                delta = (choices[0].get("delta") if choices else None) or {}
                content = delta.get("content")
                if isinstance(content, str) and content:
                    answer_parts.append(content)
                if choices and choices[0].get("finish_reason"):
                    finish_reason = str(choices[0]["finish_reason"])
                chunk_run_id = chunk.get("run_id")
                if isinstance(chunk_run_id, str) and chunk_run_id:
                    run_id = chunk_run_id
        return {
            "answer": "".join(answer_parts),
            "run_id": run_id,
            "finish_reason": finish_reason,
            "terminated": terminated,
        }

    async def _ensure_session(self, session_id: str) -> dict[str, Any] | None:
        """创建会话（幂等：已存在按 Dashboard 端点语义返回既有/错误）。"""
        response = await self._client.request(
            "POST",
            f"{self._base_url}/chat/sessions",
            params={"session_id": session_id},
        )
        if response.status_code == 200:
            return None
        return {
            "status": "business_error",
            "error_type": f"session_create_{response.status_code}",
            "output": {},
            "side_effects": [],
            "retrievals": [],
            "run_id": None,
        }

    async def _read_tool_evidence(self, session_id: str) -> dict[str, Any]:
        """正式端口读取真实工具证据；读取失败按 incomplete（≠空集合）。

        plan T8：search_knowledge 成功记录派生为检索证据条目（backend
        标记 n_kb，results 原样透传后端条目含 title/content/source/
        document_revision），不合成、不改写；失败记录与非法载荷不产生
        检索条目（断言层按无证据失败，不当作通过）。"""
        if self._tool_reader is not None:
            try:
                records = await self._tool_reader(session_id)
            except Exception as exc:  # noqa: BLE001 - 读取失败如实上报
                return {
                    "status": "incomplete",
                    "reason": f"tool-calls port read failed: {exc}",
                }
            return {
                "status": "complete",
                "tool_calls": list(records or []),
                "retrievals": _kb_retrievals(records or []),
            }
        response = await self._client.get(
            f"{self._base_url}/chat/sessions/{session_id}/tool-calls",
        )
        if response.status_code != 200:
            return {
                "status": "incomplete",
                "reason": f"tool-calls port HTTP {response.status_code}",
            }
        try:
            records = response.json()
        except ValueError as exc:
            return {
                "status": "incomplete",
                "reason": f"tool-calls port payload invalid: {exc}",
            }
        return {
            "status": "complete",
            "tool_calls": list(records or []),
            "retrievals": _kb_retrievals(records or []),
        }

    async def _read_browser_evidence(self, session_id: str) -> list[dict[str, Any]]:
        """正式端口读取浏览器会话证据（plan T9）。

        成功读取 -> 记录列表（空列表 = 确认不存在，可规范化 closed/
        absent）；读取失败或端口返回 null -> 单条 unknown 记录（读取
        失败不当不存在，closed 断言不得借此通过）。"""
        if self._browser_state_reader is None:
            return []
        try:
            sessions = await self._browser_state_reader(session_id)
        except Exception as exc:  # noqa: BLE001 - 读取失败如实上报
            return [
                {"state": "unknown", "reason": f"browser sessions read failed: {exc}"}
            ]
        if sessions is None:
            return [
                {"state": "unknown", "reason": "browser sessions port returned null"}
            ]
        records: list[dict[str, Any]] = []
        for session in sessions if isinstance(sessions, list) else []:
            if not isinstance(session, Mapping):
                continue
            records.append(
                {
                    "browser_session_id": session.get("id"),
                    "state": str(session.get("status") or "unknown"),
                    "closed_at": session.get("closed_at"),
                }
            )
        return records

    # ------------------------------------------------------------------
    # T10：Task/制品/文件证据
    # ------------------------------------------------------------------

    async def _execute_task_create_step(
        self, turn: Mapping[str, Any], request: Mapping[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        """task_create 步骤：intent 副作用前登记 -> create -> 回填 owned。

        返回 (error_response | None, task_id)。create 异常按分类返回
        transport/business + side_effects_unobserved（intent 标记 unknown，
        等待幂等键回查收敛，不重跑目标）；intent 登记失败 fail-closed。"""
        if self._task_creator is None:
            return {
                "status": "business_error",
                "error_type": "task_port_unwired",
                "output": {}, "side_effects": [], "retrievals": [],
                "run_id": None,
            }, ""
        context = request.get("scenario_context")
        payload = turn.get("task")
        payload = dict(payload) if isinstance(payload, Mapping) else {}
        # step_id 进入钩子/创建者上下文（幂等键与资源账目按步骤归属）
        step_id = str(turn.get("step_id") or "")
        if isinstance(context, Mapping):
            context = {**context, "step_id": step_id}
        else:
            context = {"step_id": step_id}
        business_key: str | None = None
        if self._task_intent_before is not None:
            try:
                business_key = str(await _call_hook(
                    self._task_intent_before, payload, context
                ))
            except Exception as exc:  # noqa: BLE001 - 登记失败不留未归属副作用
                return {
                    "status": "business_error",
                    "error_type": f"task_intent_register_failed: {exc}",
                    "output": {}, "side_effects": [], "retrievals": [],
                    "run_id": None,
                }, ""
        session = request.get("session") or {}
        try:
            task = await self._task_creator(
                payload, str(session.get("session_id") or ""), context,
            )
        except Exception as exc:  # noqa: BLE001 - 分类后受控返回
            if business_key and self._task_intent_unknown is not None:
                try:
                    await _call_hook(
                        self._task_intent_unknown,
                        business_key, f"task create failed: {exc}",
                    )
                except Exception:  # noqa: BLE001 - 账目标记不改变失败语义
                    pass
            status, error_type = _classify_exception(exc, self._classify)
            return {
                "status": status,
                "error_type": error_type or "task_create_failed",
                "output": {}, "side_effects": [],
                "side_effects_unobserved": True, "retrievals": [],
                "run_id": None,
            }, ""
        task_id = str((task or {}).get("task_id") or (task or {}).get("id") or "")
        if not task_id:
            if business_key and self._task_intent_unknown is not None:
                try:
                    await _call_hook(
                        self._task_intent_unknown,
                        business_key, "task create response missing task_id",
                    )
                except Exception:  # noqa: BLE001
                    pass
            return {
                "status": "business_error",
                "error_type": "task_create_response_invalid",
                "output": {}, "side_effects": [],
                "side_effects_unobserved": True, "retrievals": [],
                "run_id": None,
            }, ""
        if business_key and self._task_intent_confirmed is not None:
            try:
                await _call_hook(self._task_intent_confirmed, business_key, task_id)
            except Exception as exc:  # noqa: BLE001 - 回填失败 fail-closed
                return {
                    "status": "business_error",
                    "error_type": f"task_intent_confirm_failed: {exc}",
                    "output": {}, "side_effects": [], "retrievals": [],
                    "run_id": None,
                }, ""
        return None, task_id

    async def _poll_task_terminal(self, task_id: str) -> dict[str, Any] | None:
        """轮询任务终态；deadline 内未终态返回 None（不伪造终局）。"""
        import asyncio as _asyncio

        deadline = self._clock() + self._task_deadline_seconds
        while True:
            task = await self._task_reader(task_id)
            # Collection can start long after creation (later turns/other
            # tasks). Never grant a fresh 600-second window to an old task.
            try:
                from datetime import datetime
                created = datetime.fromisoformat(str((task or {}).get("created_at")))
                if created.tzinfo is not None:
                    age = max(0.0, self._wall_clock() - created.timestamp())
                    remaining_from_creation = max(0.0, self._task_deadline_seconds - age)
                    deadline = min(deadline, self._clock() + remaining_from_creation)
            except (TypeError, ValueError):
                pass  # Legacy ports still have the bounded collection window.
            status = str((task or {}).get("status") or "").lower()
            if status in TERMINAL_TASK_STATUSES:
                return dict(task)
            remaining = deadline - self._clock()
            if remaining <= 0:
                return None
            pause = min(5.0, remaining)
            if self._sleep is not None:
                await self._sleep(pause)
            else:
                await _asyncio.sleep(pause)

    @staticmethod
    def _task_elapsed_seconds(task: Mapping[str, Any]) -> float | None:
        created_at = task.get("created_at")
        updated_at = task.get("updated_at")
        try:
            from datetime import datetime as _dt

            start = _dt.fromisoformat(str(created_at))
            end = _dt.fromisoformat(str(updated_at))
            return max(0.0, (end - start).total_seconds())
        except (ValueError, TypeError):
            return None

    async def _collect_task_evidence(self, task_id: str) -> dict[str, Any]:
        """单任务证据：终态 + worker runs（读取失败/超时如实记录）。"""
        entry: dict[str, Any] = {"task_id": task_id}
        try:
            task = await self._poll_task_terminal(task_id)
        except Exception as exc:  # noqa: BLE001 - 读取失败不当不存在
            entry["error"] = f"task read failed: {exc}"
            return entry
        if task is None:
            entry["error"] = (
                "task deadline exceeded before terminal status "
                f"({int(self._task_deadline_seconds)}s)"
            )
            return entry
        entry["title"] = task.get("title")
        entry["status"] = str(task.get("status") or "").lower()
        elapsed = self._task_elapsed_seconds(task)
        if elapsed is not None:
            entry["elapsed_seconds"] = elapsed
        if self._task_runs_reader is not None:
            try:
                entry["runs"] = list(await self._task_runs_reader(task_id) or [])
            except Exception as exc:  # noqa: BLE001
                entry["runs_error"] = f"task runs read failed: {exc}"
        return entry

    async def _collect_artifacts(
        self, task_ids: list[str], task_entries: list[Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], str | None]:
        """制品证据：注册列表（id/name/kind）+ 注册侧固化的原始
        storage_ref（origin_ref 标签，注册时从提交的 TaskArtifact 固化；
        finish_run 不持久化提交制品，TaskRun 无 artifacts 字段）。
        游标不前进/读取失败 -> (部分, 原因)。"""
        if self._artifact_lister is None:
            return [], None
        artifacts: list[dict[str, Any]] = []
        for task_id, entry in zip(task_ids, task_entries):
            items: list[Any] = []
            encoded_cursor: str | None = None
            while True:
                try:
                    page = (await self._artifact_lister(task_id) if encoded_cursor is None
                            else await self._artifact_lister(task_id, cursor=next_cursor))
                except Exception as exc:  # noqa: BLE001 - 读取失败不当空集合
                    return artifacts, f"artifacts list read failed: {exc}"
                if isinstance(page, Mapping) and "items" in page:
                    page_items = page.get("items")
                    next_cursor = page.get("next_cursor")
                elif isinstance(page, list):
                    page_items, next_cursor = page, None
                else:
                    return artifacts, "artifacts list invalid response"
                items.extend(page_items or [])
                if not next_cursor:
                    break
                encoded = json.dumps(next_cursor, sort_keys=True)
                if encoded == encoded_cursor:
                    return artifacts, "artifacts list cursor not advancing"
                encoded_cursor = encoded
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                artifacts.append({
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "kind": item.get("kind"),
                    "storage_ref": item.get("origin_ref"),
                    **({"content": item["content"]} if "content" in item else {}),
                })
        return artifacts, None

    async def _collect_files(
        self, rel_paths: Any,
    ) -> list[dict[str, Any]]:
        """工作区文件证据：受控端口安全读取（穿越/symlink 拒绝在端口
        内），失败/缺失记录 error——content None 让 file_content 断言判负。"""
        if self._workspace_file_reader is None:
            return []
        files: list[dict[str, Any]] = []
        for rel in rel_paths if isinstance(rel_paths, (list, tuple)) else []:
            filename = str(rel)
            try:
                record = await self._workspace_file_reader(filename)
            except Exception as exc:  # noqa: BLE001 - 读取失败不当通过
                files.append({
                    "filename": filename, "content": None, "sha256": None,
                    "error": str(exc),
                })
                continue
            if record is None:
                files.append({
                    "filename": filename, "content": None, "sha256": None,
                    "error": "workspace file missing",
                })
                continue
            files.append({
                "filename": str(record.get("filename") or filename),
                "content": record.get("content"),
                "sha256": record.get("sha256"),
            })
        return files

    # ------------------------------------------------------------------
    # Domain Evaluator 端口
    # ------------------------------------------------------------------

    async def run_target(
        self, target: Any, request: Mapping[str, Any]
    ) -> dict[str, Any]:
        session = request.get("session") or {}
        session_id = session.get("session_id")
        if not session_id:
            return {
                "status": "business_error",
                "error_type": "session_mapping_missing",
                "output": {},
                "side_effects": [],
                "retrievals": [],
                "run_id": None,
            }
        try:
            create_error = await self._ensure_session(str(session_id))
            if create_error is not None:
                return create_error
            # T7：场景选项（记忆逻辑别名）随请求进入 chat options，
            # 服务端首轮锁定 profile；prepare 失败按 blocked 上报
            scenario_options = request.get("scenario_options")
            self._options = (
                {"external_memory_enabled": [str(scenario_options["external_memory_enabled"])]}
                if isinstance(scenario_options, Mapping)
                and scenario_options.get("external_memory_enabled")
                else {}
            )
            if self._options and self._session_prepare is not None:
                try:
                    await self._session_prepare(
                        str(session_id),
                        str(self._options["external_memory_enabled"][0]),
                        request.get("scenario_context"),
                    )
                except ScenarioMemoryError:
                    # 场景记忆准备 blocked 是业务失败而非传输失败：
                    # 不重试（重试只会重复同一缺项），如实上报能力缺口
                    return {
                        "status": "business_error",
                        "error_type": "scenario_memory_unavailable",
                        "output": {},
                        "side_effects": [],
                        "retrievals": [],
                        "run_id": None,
                    }
            turn_outputs: list[dict[str, Any]] = []
            last_run_id: str | None = None
            task_ids: list[str] = []
            namespace = str(session.get("namespace") or "")
            for turn in request.get("turns") or ():
                if str(turn.get("action") or "") == "task_create":
                                        # T10：task_api 步骤不经 SSE——intent 前置登记 +
                    # create + 回填 owned；失败受控返回
                    error, created_task_id = (
                        await self._execute_task_create_step(turn, request)
                    )
                    if error is not None:
                        return error
                    if created_task_id:
                        task_ids.append(created_task_id)
                    turn_outputs.append({
                        "step_id": turn.get("step_id"),
                        "task_id": created_task_id,
                        "final_answer": None,
                        "completion_reported": True,
                        "run_id": None,
                    })
                    continue
                outcome = await self._consume_sse(
                    str(session_id), turn, namespace
                )
                if outcome.get("business_error"):
                    return {
                        "status": "business_error",
                        "error_type": outcome["error_type"],
                        "output": {},
                        "side_effects": [],
                        "retrievals": [],
                        "run_id": last_run_id,
                    }
                if not outcome.get("terminated"):
                    # SSE 未终止：流中断时服务端可能已执行副作用
                    return {
                        "status": "transport_error",
                        "error_type": "sse_unterminated",
                        "output": {},
                        "side_effects": [],
                        "side_effects_unobserved": True,
                        "retrievals": [],
                        "run_id": last_run_id,
                    }
                run_id = outcome.get("run_id")
                finish_reason = outcome.get("finish_reason")
                if finish_reason == "error":
                    # hello 错误：SSE error 结局按业务失败，不判成功
                    return {
                        "status": "business_error",
                        "error_type": "agent_stream_error",
                        "output": {},
                        "side_effects": [],
                        "retrievals": [],
                        "run_id": run_id or last_run_id,
                    }
                if not run_id:
                    # 会话映射缺失：finish 事件未透出 run_id，证据无法
                    # 归属到持久化 run
                    return {
                        "status": "business_error",
                        "error_type": "session_mapping_missing",
                        "output": {},
                        "side_effects": [],
                        "retrievals": [],
                        "run_id": last_run_id,
                    }
                last_run_id = run_id or last_run_id
                turn_outputs.append(
                    {
                        "step_id": turn.get("step_id"),
                        "final_answer": outcome.get("answer"),
                        "completion_reported": finish_reason == "stop",
                        "run_id": run_id,
                    }
                )
        except Exception as exc:  # noqa: BLE001 - 分类后受控返回
            status, error_type = _classify_exception(exc, self._classify)
            return {
                "status": status,
                "error_type": error_type,
                "output": {},
                "side_effects": [],
                "side_effects_unobserved": True,
                "retrievals": [],
                "run_id": None,
            }
        if not turn_outputs:
            return {
                "status": "business_error",
                "error_type": "empty_turns",
                "output": {},
                "side_effects": [],
                "retrievals": [],
                "run_id": last_run_id,
            }
        evidence = await self._read_tool_evidence(str(session_id))
        # T9：浏览器会话证据 + 账目观察（owned 回填登记；释放走执行级
        # 两阶段纪律）。登记失败 fail-closed 上抛，不留未归属资源
        evidence["browser"] = await self._read_browser_evidence(str(session_id))
        if self._browser_observer is not None and isinstance(
            evidence["browser"], list
        ):
            await self._browser_observer(
                str(session_id),
                evidence["browser"],
                request.get("scenario_context"),
            )
        # T10：chat 题 agent 自行创建的任务从 create_task 成功记录绑定
        # （不重跑目标）；task_api 题在步骤执行时已绑定
        for bound in _task_ids_from_tool_calls(evidence.get("tool_calls")):
            if bound not in task_ids:
                task_ids.append(bound)
        incomplete_reasons: list[str] = []
        task_entries: list[dict[str, Any]] = []
        for task_id in task_ids:
            entry = await self._collect_task_evidence(task_id)
            if entry.get("error") or entry.get("runs_error"):
                incomplete_reasons.append(
                    f"task {task_id}: {entry.get('error') or entry.get('runs_error')}"
                )
            task_entries.append(entry)
        evidence["tasks"] = task_entries
        artifacts, artifact_error = await self._collect_artifacts(
            task_ids, task_entries
        )
        evidence["artifacts"] = artifacts
        if artifact_error:
            incomplete_reasons.append(artifact_error)
        evidence["files"] = await self._collect_files(
            request.get("scenario_files")
        )
        if incomplete_reasons:
            # 任务未终态/制品读取失败：证据 incomplete（读取失败不等于
            # 空集合，task_terminal/artifact_set 断言不得借此通过）
            evidence["status"] = "incomplete"
            evidence["reason"] = "; ".join(incomplete_reasons)
        if self._workspace_resource_observer is not None:
            # 文件（实际 hash）与制品（注册 id）进入资源账目——登记失败
            # fail-closed 上抛，不留未归属资源
            await self._workspace_resource_observer(
                evidence, request.get("scenario_context"),
            )
        evidence["steps"] = [
            {
                "step_id": turn.get("step_id"),
                "run_id": turn.get("run_id"),
                "answer": turn.get("final_answer"),
                **({"task_id": turn["task_id"]} if turn.get("task_id") else {}),
            }
            for turn in turn_outputs
        ]
        last = turn_outputs[-1]
        return {
            "status": "succeeded",
            "output": {
                "completion_reported": all(
                    turn.get("completion_reported") for turn in turn_outputs
                ),
                "final_answer": last.get("final_answer"),
                "turns": turn_outputs,
                "scenario_evidence": evidence,
                "session_id": session_id,
                "model_response_version": self._model,
            },
            # 副作用证据由 scenario_evidence/正式端口承载，此处恒空不伪造
            "side_effects": [],
            "retrievals": [],
            "run_id": last_run_id,
        }


class ScenarioCliRunner:
    """真实 CLI 通道适配器（plan T11，spec 5.1）。

    容器内 subprocess 调固定 n-agent 入口（类型化 argv，禁止
    shell=True）；独立进程组 + 有界限时；环境白名单（N_AGENT_* 非
    E2E 密钥 + 递归防护 N_AGENT_EVALUATION_ENABLED=false）；输出脱敏
    有界。会话经真实 Gateway 唯一解析（多条/零条拒绝猜测，fail-closed），
    解析前登记 cli_conversation intent，解析失败（副作用可能已发生）
    标记 unknown，不重跑目标。工具证据经正式端口（会话 tool-calls）
    读取，来源核对按真实通道证据，普通 agent 适配不能替代。"""

    def __init__(
        self,
        *,
        run_process=None,
        redactor=None,
        tool_reader: Callable[[str], Any] | None = None,
        cli_intent_before: Callable[..., Any] | None = None,
        cli_intent_confirmed: Callable[..., Any] | None = None,
        cli_intent_unknown: Callable[..., Any] | None = None,
        step_timeout_seconds: float = 180.0,
        output_limit: int = 4000,
        clock: Callable[[], float] | None = None,
    ) -> None:
        # 注意：局部 import run_process 会遮蔽同名注入参数（实测让替身
        # 失效、真实子进程在宿主执行）——默认值经模块级导入提供
        self._run_process = run_process or _default_run_process
        self._redactor = redactor
        self._tool_reader = tool_reader
        self._cli_intent_before = cli_intent_before
        self._cli_intent_confirmed = cli_intent_confirmed
        self._cli_intent_unknown = cli_intent_unknown
        self._step_timeout_seconds = float(step_timeout_seconds)
        self._output_limit = int(output_limit)
        import time as _time

        self._clock = clock or _time.monotonic

    # ------------------------------------------------------------------
    # 受控执行
    # ------------------------------------------------------------------

    def _execute(self, argv: list[str]) -> dict[str, Any]:
        result = self._run_process(
            argv,
            timeout_seconds=self._step_timeout_seconds,
            redactor=self._redactor,
            output_limit=self._output_limit,
            clock=self._clock,
        )
        return {
            "argv": list(result.argv),
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_seconds": result.duration_seconds,
            "process_identity": result.process_identity,
            "timed_out": result.timed_out,
            "spawn_error": result.spawn_error,
        }

    async def run_target(
        self, target: Any, request: Mapping[str, Any]
    ) -> dict[str, Any]:
        session = request.get("session") or {}
        conversation_id = str(session.get("session_id") or "")
        context = request.get("scenario_context")
        if not conversation_id:
            return self._error("business_error", "session_mapping_missing")
        turns = [
            turn for turn in request.get("turns") or ()
            if isinstance(turn, Mapping) and turn.get("message")
        ]
        if not turns:
            return self._error("business_error", "empty_turns")
        # 副作用前登记 intent（第一条 sessions 命令会经 Gateway 建会话）
        business_key: str | None = None
        if self._cli_intent_before is not None:
            try:
                business_key = str(await _call_hook(
                    self._cli_intent_before, conversation_id, context,
                ))
            except Exception as exc:  # noqa: BLE001 - 登记失败不留未归属副作用
                return self._error(
                    "business_error", f"cli_intent_register_failed: {exc}",
                )
        # 会话解析（第一条命令触发 Gateway 建会话；--browse 只读回读）
        prepare = self._execute([
            "n-agent", "sessions", "--conversation-id", conversation_id,
            "--no-interactive", "--json",
        ])
        browse = self._execute([
            "n-agent", "sessions", "--browse", "--conversation-id",
            conversation_id, "--no-interactive", "--json",
        ])
        session_id = await self._resolve_session_id(
            browse, business_key, "cli session resolution failed",
        )
        if isinstance(session_id, dict):
            return session_id
        if business_key and self._cli_intent_confirmed is not None:
            try:
                await _call_hook(
                    self._cli_intent_confirmed,
                    business_key, session_id, conversation_id,
                )
            except Exception as exc:  # noqa: BLE001 - 回填失败 fail-closed
                return self._error(
                    "business_error", f"cli_intent_confirm_failed: {exc}",
                )
        # 逐轮真实 CLI 发送；每轮 chat 返回即该轮 run 已完成，紧接经
        # 正式端口读工具记录，把新出现的 run_id 增量归属到当轮（CLI
        # 子进程不透出 run_id，step 级断言靠此权威索引做跨 run 过滤）
        steps: list[dict[str, Any]] = []
        last_answer = ""
        seen_run_ids: set[str] = set()
        step_run_ids: dict[str, str | None] = {}
        tool_calls_all: list[Any] = []
        tool_call_keys: set[str] = set()
        incomplete_reasons: list[str] = []
        import json as _json

        for turn in turns:
            outcome = self._execute([
                "n-agent", "chat", str(turn.get("message") or ""),
                "--conversation-id", conversation_id, "--no-stream",
            ])
            steps.append({
                "step_id": turn.get("step_id"),
                "exit_code": outcome["exit_code"],
                "stdout": outcome["stdout"],
                "stderr": outcome["stderr"],
                "duration_seconds": outcome["duration_seconds"],
                "process_identity": outcome["process_identity"],
                "timed_out": outcome["timed_out"],
                "spawn_error": outcome["spawn_error"],
            })
            if outcome["exit_code"] == 0 and outcome["stdout"]:
                last_answer = outcome["stdout"]
            if self._tool_reader is None:
                continue
            try:
                calls = list(await self._tool_reader(session_id) or [])
            except Exception as exc:  # noqa: BLE001 - 读取失败不当空集合
                incomplete_reasons.append(f"tool evidence read failed: {exc}")
                continue
            for call in calls:
                if not isinstance(call, Mapping):
                    continue
                key = _json.dumps(call, sort_keys=True, ensure_ascii=False)
                if key not in tool_call_keys:
                    tool_call_keys.add(key)
                    tool_calls_all.append(call)
            new_run_ids = {
                str(call.get("run_id"))
                for call in calls
                if isinstance(call, Mapping) and call.get("run_id")
            } - seen_run_ids
            seen_run_ids |= new_run_ids
            # 单轮应对应单 run；多 run 为非预期形态，宁可留空（fail-closed
            # 断言按缺归属处理），不猜其中一个
            step_run_ids[str(turn.get("step_id"))] = (
                next(iter(new_run_ids)) if len(new_run_ids) == 1 else None
            )
        evidence: dict[str, Any] = {
            "status": "complete",
            "cli": {
                "source": "cli",
                "conversation_id": conversation_id,
                "session_id": session_id,
                "prepare": prepare,
                "browse": browse,
                "steps": steps,
            },
        }
        evidence["tool_calls"] = tool_calls_all
        if self._tool_reader is None:
            incomplete_reasons.append("tool evidence reader unwired")
        # 步骤失败/超时/spawn 失败：证据 incomplete（不当通过也不当不存在）
        for step in steps:
            if step["timed_out"]:
                incomplete_reasons.append(
                    f"cli step {step['step_id']} timed out"
                )
            elif step["exit_code"] != 0:
                incomplete_reasons.append(
                    f"cli step {step['step_id']} exit_code={step['exit_code']}"
                )
            elif step["spawn_error"]:
                incomplete_reasons.append(
                    f"cli step {step['step_id']} spawn failed"
                )
        if incomplete_reasons:
            evidence["status"] = "incomplete"
            evidence["reason"] = "; ".join(incomplete_reasons)
        evidence["steps"] = [
            {
                "step_id": step["step_id"],
                "run_id": step_run_ids.get(str(step["step_id"])),
                "answer": step["stdout"] if step["exit_code"] == 0 else None,
            }
            for step in steps
        ]
        return {
            "status": "succeeded",
            "output": {
                "completion_reported": all(
                    step["exit_code"] == 0 and step["stdout"]
                    for step in steps
                ),
                "final_answer": last_answer,
                "turns": [
                    {
                        "step_id": step["step_id"],
                        "final_answer": (
                            step["stdout"] if step["exit_code"] == 0 else None
                        ),
                        "completion_reported": bool(
                            step["exit_code"] == 0 and step["stdout"]
                        ),
                        "run_id": step_run_ids.get(str(step["step_id"])),
                    }
                    for step in steps
                ],
                "scenario_evidence": evidence,
                "session_id": session_id,
                "model_response_version": None,
            },
            # 副作用证据由 scenario_evidence/正式端口承载，此处恒空不伪造
            "side_effects": [],
            "retrievals": [],
            "run_id": None,
        }

    async def _resolve_session_id(
        self,
        browse: Mapping[str, Any],
        business_key: str | None,
        prefix: str,
    ) -> Any:
        """--browse 输出 -> 唯一真实会话 ID；多条/零条/非法拒绝猜测。"""
        import json as _json

        if browse.get("spawn_error") or browse.get("timed_out"):
            return await self._unknown(
                business_key,
                f"{prefix}: browse failed: "
                f"{browse.get('spawn_error') or 'timeout'}",
            )
        if browse.get("exit_code") != 0:
            return await self._unknown(
                business_key,
                f"{prefix}: rc={browse.get('exit_code')}: "
                f"{str(browse.get('stderr'))[:200]}",
            )
        try:
            rows = _json.loads(browse.get("stdout") or "null")
        except ValueError as exc:
            return await self._unknown(
                business_key, f"{prefix}: browse output not JSON: {exc}",
            )
        if not isinstance(rows, list):
            return await self._unknown(
                business_key, f"{prefix}: browse output not a list",
            )
        ids = [
            str(row.get("session_id")) for row in rows
            if isinstance(row, Mapping) and row.get("session_id")
        ]
        if len(ids) != 1:
            return await self._unknown(
                business_key, f"{prefix}: session resolution ambiguous: {ids}",
            )
        return ids[0]

    async def _unknown(
        self, business_key: str | None, reason: str,
    ) -> dict[str, Any]:
        """解析失败但副作用（建会话）可能已发生：intent 标记 unknown，
        不重跑目标（幂等键回查收敛）。"""
        if business_key and self._cli_intent_unknown is not None:
            try:
                await _call_hook(self._cli_intent_unknown, business_key, reason)
            except Exception:  # noqa: BLE001 - 账目标记不改变失败语义
                pass
        return self._error(
            "transport_error", "cli_session_unresolved",
            side_effects_unobserved=True,
        )

    @staticmethod
    def _error(
        status: str, error_type: str, *, side_effects_unobserved: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": status,
            "error_type": error_type,
            "output": {},
            "side_effects": [],
            "retrievals": [],
            "run_id": None,
        }
        if side_effects_unobserved:
            payload["side_effects_unobserved"] = True
        return payload


class SdkAcpDriver:
    """ACP SDK 驱动（生产实现）：经 acp.connect_to_agent 走 stdio
    JSON-RPC，执行 initialize -> authenticate -> session/new ->
    session/prompt。测试经 driver_factory 注入替身，不把测试 runner
    引入应用。权限请求按 allow 选项放行（eval 场景策略内）；文件/
    终端请求一律拒绝（eval 客户端不提供）；session/update 只消费不
    回显（应答以 session/prompt 返回值为准）。"""

    def __init__(self, handle) -> None:
        self._handle = handle
        self._conn = None
        # session/prompt 的应答正文经 session/update 通知流式到达
        # （AgentMessageChunk）；prompt 返回值只带 stop_reason。缓冲
        # 当轮 agent 消息文本，prompt 结束后作为应答证据。
        self._message_text: list[str] = []

    async def _connect(self):
        if self._conn is None:
            import acp
            from acp.schema import AgentMessageChunk, AllowedOutcome

            # 闭包捕获 driver 本体：client 方法内的 self 是 client 实例，
            # 不是 SdkAcpDriver（实测错误引用使应答收集静默失效）
            driver = self

            class _EvalClient(acp.Client):
                async def request_permission(
                    self, options, session_id, tool_call, **kwargs,
                ):
                    allow = next(
                        (o for o in (options or [])
                         if str(getattr(o, "kind", "")).startswith("allow")),
                        (options or [None])[0],
                    )
                    return AllowedOutcome(
                        option_id=str(getattr(allow, "option_id", "") or ""),
                    )

                async def session_update(
                    self, session_id, update, **kwargs,
                ):
                    # 只收集 agent 消息文本作为应答证据；工具调用链经
                    # 正式端口（会话 tool-calls）核对，不从这里合成
                    if isinstance(update, AgentMessageChunk):
                        block = getattr(update, "content", None)
                        chunk = getattr(block, "text", None)
                        if chunk:
                            driver._message_text.append(str(chunk))
                    return None

                async def write_text_file(
                    self, content, path, session_id, **kwargs,
                ):
                    raise RuntimeError("eval 客户端不提供文件写入")

                async def read_text_file(
                    self, path, session_id, limit=None, line=None, **kwargs,
                ):
                    raise RuntimeError("eval 客户端不提供文件读取")

                async def create_terminal(
                    self, command, session_id, args=None, cwd=None, **kwargs,
                ):
                    raise RuntimeError("eval 客户端不提供终端")

            self._conn = acp.connect_to_agent(
                _EvalClient(),
                self._handle.process.stdin,
                self._handle.process.stdout,
                use_unstable_protocol=True,
            )
        return self._conn

    async def initialize(self) -> dict:
        import acp
        from acp.schema import Implementation

        conn = await self._connect()
        response = await conn.initialize(
            protocol_version=acp.PROTOCOL_VERSION,
            client_info=Implementation(
                name="n-agent-eval", title="N-Agent Eval", version="1",
            ),
        )
        # kind 归一为适配器契约词汇：AuthMethodAgent -> "agent"（其余
        # 视为需人工终端引导，不自动认证）
        return {
            "auth_methods": [
                {
                    "id": m.id,
                    "kind": (
                        "agent"
                        if type(m).__name__ == "AuthMethodAgent"
                        else "terminal"
                    ),
                }
                for m in (getattr(response, "auth_methods", None) or [])
            ],
        }

    async def authenticate(self, method_id: str) -> dict:
        conn = await self._connect()
        await conn.authenticate(method_id=method_id)
        return {"ok": True}

    async def new_session(self, cwd: str) -> dict:
        conn = await self._connect()
        response = await conn.new_session(cwd=cwd, mcp_servers=[])
        return {"session_id": response.session_id}

    async def prompt(self, session_id: str, text: str) -> dict:
        import acp

        conn = await self._connect()
        self._message_text = []
        response = await conn.prompt(
            [acp.text_block(text)], session_id=session_id,
        )
        return {
            "stop_reason": response.stop_reason,
            "text": "".join(self._message_text),
        }



class ScenarioAcpRunner:
    """真实 ACP 通道适配器（plan T12，spec 5.1）。

    每项 spawn 独立 n-agent acp 子进程（scenario_process.spawn_process：
    类型化 argv、独立进程组、白名单 env、进程身份入账），经 ACP SDK
    驱动执行 initialize -> authenticate -> session/new -> session/prompt。
    协议阶段每阶段至多一次尝试：session/new 超时/错误绝不重试（第二次
    会造成意外重复会话）。副作用前登记 intent：spawn 后登记 acp_process
    （进程身份防 PID 复用），session/new 成功后回填会话 ID 并登记
    session 保留意图。任一失败/结束必须停止子进程（kill 幂等）；
    session/new 之后失败的会话与服务端副作用可能已发生 ->
    side_effects_unobserved（不重跑目标）。工具证据经正式端口（会话
    tool-calls）增量归属（同 T11）；stderr 脱敏有界。
    """

    def __init__(
        self,
        *,
        spawn=None,
        driver_factory=None,
        redactor=None,
        tool_reader: Callable[[str], Any] | None = None,
        acp_intent_before: Callable[..., Any] | None = None,
        acp_intent_confirmed: Callable[..., Any] | None = None,
        acp_intent_unknown: Callable[..., Any] | None = None,
        stage_timeout_seconds: float = 180.0,
        output_limit: int = 4000,
        cwd: str = "/workspace",
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._spawn = spawn
        self._driver_factory = driver_factory
        self._redactor = redactor
        self._tool_reader = tool_reader
        self._acp_intent_before = acp_intent_before
        self._acp_intent_confirmed = acp_intent_confirmed
        self._acp_intent_unknown = acp_intent_unknown
        self._stage_timeout_seconds = float(stage_timeout_seconds)
        self._output_limit = int(output_limit)
        self._cwd = cwd
        import time as _time

        self._clock = clock or _time.monotonic

    # ------------------------------------------------------------------
    # 受控执行
    # ------------------------------------------------------------------

    async def run_target(
        self, target: Any, request: Mapping[str, Any]
    ) -> dict[str, Any]:
        from app.infrastructure.evaluation.scenario_process import (
            redact_bounded,
        )

        session = request.get("session") or {}
        conversation_id = str(session.get("session_id") or "")
        context = request.get("scenario_context")
        if not conversation_id:
            return self._error("business_error", "session_mapping_missing")
        turns = [
            turn for turn in request.get("turns") or ()
            if isinstance(turn, Mapping) and turn.get("message")
        ]
        if not turns:
            return self._error("business_error", "empty_turns")

        handle = None
        try:
            try:
                handle = await self._spawn()
            except Exception as exc:  # noqa: BLE001 - spawn 失败如实记录
                return self._error(
                    "transport_error", "acp_spawn_failed",
                    side_effects_unobserved=True,
                )
            # 副作用前登记 intent（acp_process，携带进程身份防 PID 复用）
            business_key: str | None = None
            if self._acp_intent_before is not None:
                hook_context = dict(context) if isinstance(context, dict) else {}
                hook_context.setdefault("session_ref", conversation_id)
                try:
                    business_key = str(await _call_hook(
                        self._acp_intent_before,
                        handle.process_identity, hook_context,
                    ))
                except Exception as exc:  # noqa: BLE001
                    return self._error(
                        "business_error",
                        f"acp_intent_register_failed: {exc}",
                    )
            stages: list[dict[str, Any]] = []
            protocol: list[str] = []

            async def _stage(name, func):
                started = self._clock()
                try:
                    detail = await asyncio.wait_for(
                        func(), timeout=self._stage_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    return None, {
                        "stage": name, "ok": False, "timed_out": True,
                        "duration_seconds": round(self._clock() - started, 3),
                    }
                except Exception as exc:  # noqa: BLE001 - 协议错误如实记录
                    # RequestError.data 携带对端异常详情（SDK 把 agent 侧
                    # 异常包装成 internal_error(data)），必须透出，不留
                    # "Internal error" 诊断盲区
                    detail = str(exc)
                    data = getattr(exc, "data", None)
                    if data:
                        detail = f"{detail} data={data}"[:400]
                    return None, {
                        "stage": name, "ok": False,
                        "error": f"{type(exc).__name__}: {detail}"[:400],
                        "duration_seconds": round(self._clock() - started, 3),
                    }
                return detail, {
                    "stage": name, "ok": True,
                    "duration_seconds": round(self._clock() - started, 3),
                }

            driver = self._driver_factory(handle)

            # initialize：无持久副作用；agent 认证方法缺失 -> 拒绝建会话
            init_detail, init_stage = await _stage("initialize", driver.initialize)
            stages.append(init_stage)
            if init_detail is None:
                return self._protocol_failed(
                    "initialize", init_stage, side_effects=False,
                    handle=handle, stages=stages, protocol=protocol,
                    conversation_id=conversation_id,
                    incomplete_reasons=[],
                )
            agent_methods = [
                m for m in init_detail.get("auth_methods") or []
                if isinstance(m, Mapping) and m.get("kind") == "agent"
                and m.get("id")
            ]
            if not agent_methods:
                return self._protocol_failed(
                    "initialize",
                    {**init_stage, "error": "no agent auth method advertised"},
                    side_effects=False, error_type="acp_auth_unavailable",
                    handle=handle, stages=stages, protocol=protocol,
                    conversation_id=conversation_id,
                    incomplete_reasons=[],
                )
            protocol.append("initialize")

            auth_detail, auth_stage = await _stage(
                "authenticate",
                lambda: driver.authenticate(str(agent_methods[0]["id"])),
            )
            stages.append(auth_stage)
            if auth_detail is None:
                return self._protocol_failed(
                    "authenticate", auth_stage, side_effects=False,
                    handle=handle, stages=stages, protocol=protocol,
                    conversation_id=conversation_id,
                    incomplete_reasons=[],
                )
            protocol.append("authenticate")

            new_detail, new_stage = await _stage(
                "session/new", lambda: driver.new_session(self._cwd),
            )
            stages.append(new_stage)
            if new_detail is None:
                if not new_stage.get("timed_out"):
                    # 显式协议错误（如 invalid params）：会话确定未创建，
                    # 可安全重试
                    return self._protocol_failed(
                        "session/new", new_stage, side_effects=False,
                        handle=handle, stages=stages, protocol=protocol,
                        conversation_id=conversation_id,
                        incomplete_reasons=[],
                    )
                # 超时后绝不重试 session/new（第二次会造成意外重复会话）：
                # 会话可能已创建 -> intent unknown + 不重跑
                if business_key and self._acp_intent_unknown is not None:
                    try:
                        await _call_hook(
                            self._acp_intent_unknown, business_key,
                            "session/new: timed out",
                        )
                    except Exception:  # noqa: BLE001 - 标记不改失败语义
                        pass
                return self._error(
                    "transport_error", "acp_session_unresolved",
                    side_effects_unobserved=True,
                )
            session_id = str(new_detail.get("session_id") or "")
            if not session_id:
                return self._protocol_failed(
                    "session/new",
                    {**new_stage, "error": "empty session id"},
                    side_effects=False,
                    handle=handle, stages=stages, protocol=protocol,
                    conversation_id=conversation_id,
                    incomplete_reasons=[],
                )
            protocol.append("session/new")
            # 会话已创建（服务端持久副作用）：回填 external_id 并登记
            # session 保留意图（登记失败 fail-closed）
            if business_key and self._acp_intent_confirmed is not None:
                try:
                    await _call_hook(
                        self._acp_intent_confirmed,
                        business_key, session_id, conversation_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    return self._error(
                        "business_error", f"acp_intent_confirm_failed: {exc}",
                    )

            # 逐轮 prompt + 工具增量归属（每轮 prompt 返回即 run 已完成）
            steps: list[dict[str, Any]] = []
            last_answer = ""
            seen_run_ids: set[str] = set()
            step_run_ids: dict[str, str | None] = {}
            tool_calls_all: list[Any] = []
            tool_call_keys: set[str] = set()
            incomplete_reasons: list[str] = []
            import json as _json

            for turn in turns:
                step_id = str(turn.get("step_id"))
                prompt_detail, prompt_stage = await _stage(
                    "session/prompt",
                    lambda sid=session_id, msg=str(turn.get("message")): (
                        driver.prompt(sid, msg)
                    ),
                )
                stages.append(prompt_stage)
                if prompt_detail is None or str(
                    prompt_detail.get("stop_reason")
                ) == "refusal":
                    # prompt 失败/拒绝：会话已创建（id 在案，settle 保留
                    # 为证据），进程已停止 -> 不标 unknown；副作用可能
                    # 已发生 -> 不重跑目标。部分证据如实随错误返回。
                    if prompt_detail is None:
                        incomplete_reasons.append(
                            f"session/prompt step {step_id} "
                            + (
                                "timed out"
                                if prompt_stage.get("timed_out")
                                else f"failed: {prompt_stage.get('error')}"
                            )
                        )
                    else:
                        incomplete_reasons.append(
                            f"session/prompt step {step_id} refused"
                        )
                    evidence: dict[str, Any] = {
                        "status": "incomplete",
                        "reason": "; ".join(incomplete_reasons),
                        "protocol": list(protocol),
                        "acp": {
                            "source": "acp",
                            "conversation_id": conversation_id,
                            "session_id": session_id,
                            "process_identity": handle.process_identity,
                            "stages": stages,
                            "stderr": redact_bounded(
                                handle.stderr_text(),
                                limit=self._output_limit,
                                redactor=self._redactor,
                            ),
                        },
                        "tool_calls": tool_calls_all,
                        "steps": [
                            {
                                "step_id": step["step_id"],
                                "run_id": step_run_ids.get(str(step["step_id"])),
                                "answer": step["answer"],
                            }
                            for step in steps
                        ],
                    }
                    if prompt_detail is None:
                        return {
                            "status": "transport_error",
                            "error_type": "acp_protocol_failed",
                            "output": {"scenario_evidence": evidence},
                            "side_effects": [],
                            "retrievals": [],
                            "run_id": None,
                            "side_effects_unobserved": True,
                        }
                    return {
                        "status": "business_error",
                        "error_type": "acp_prompt_refused",
                        "output": {"scenario_evidence": evidence},
                        "side_effects": [],
                        "retrievals": [],
                        "run_id": None,
                    }
                answer = str(prompt_detail.get("text") or "")
                protocol.append("session/prompt")
                steps.append({
                    "step_id": step_id,
                    "stop_reason": prompt_detail.get("stop_reason"),
                    "answer": answer,
                })
                if answer:
                    last_answer = (
                        last_answer + "\n" + answer if last_answer else answer
                    )
                if self._tool_reader is None:
                    continue
                try:
                    calls = list(await self._tool_reader(session_id) or [])
                except Exception as exc:  # noqa: BLE001
                    incomplete_reasons.append(
                        f"tool evidence read failed: {exc}"
                    )
                    continue
                for call in calls:
                    if not isinstance(call, Mapping):
                        continue
                    key = _json.dumps(call, sort_keys=True, ensure_ascii=False)
                    if key not in tool_call_keys:
                        tool_call_keys.add(key)
                        tool_calls_all.append(call)
                new_run_ids = {
                    str(call.get("run_id"))
                    for call in calls
                    if isinstance(call, Mapping) and call.get("run_id")
                } - seen_run_ids
                seen_run_ids |= new_run_ids
                # 单轮应对应单 run；多 run 为非预期形态留空（fail-closed）
                step_run_ids[step_id] = (
                    next(iter(new_run_ids)) if len(new_run_ids) == 1 else None
                )

            evidence = {
                "status": "complete",
                "protocol": protocol,
                "acp": {
                    "source": "acp",
                    "conversation_id": conversation_id,
                    "session_id": session_id,
                    "process_identity": handle.process_identity,
                    "stages": stages,
                    "stderr": redact_bounded(
                        handle.stderr_text(), limit=self._output_limit,
                        redactor=self._redactor,
                    ),
                },
                "tool_calls": tool_calls_all,
            }
            if incomplete_reasons:
                evidence["status"] = "incomplete"
                evidence["reason"] = "; ".join(incomplete_reasons)
            evidence["steps"] = [
                {
                    "step_id": step["step_id"],
                    "run_id": step_run_ids.get(str(step["step_id"])),
                    "answer": step["answer"],
                }
                for step in steps
            ]
            return {
                "status": "succeeded",
                "output": {
                    "completion_reported": all(
                        step.get("answer") for step in steps
                    ),
                    "final_answer": last_answer,
                    "turns": [
                        {
                            "step_id": step["step_id"],
                            "final_answer": step["answer"],
                            "completion_reported": bool(step.get("answer")),
                            "run_id": step_run_ids.get(str(step["step_id"])),
                        }
                        for step in steps
                    ],
                    "scenario_evidence": evidence,
                    "session_id": session_id,
                    "model_response_version": None,
                },
                # 副作用证据由 scenario_evidence/正式端口承载，此处恒空
                "side_effects": [],
                "retrievals": [],
                "run_id": None,
            }
        finally:
            if handle is not None:
                await handle.kill()

    def _protocol_failed(
        self, stage: str, stage_entry: Mapping[str, Any], *,
        side_effects: bool, error_type: str = "acp_protocol_failed",
        handle=None, stages=None, protocol=None, conversation_id: str = "",
        incomplete_reasons: list[str] | None = None,
    ) -> dict[str, Any]:
        """session/new 之前的协议失败：无服务端持久副作用（进程由
        finally 停止），可安全重试（不标 side_effects_unobserved）。
        部分证据随错误返回（失败阶段/进程身份/stderr），不留诊断盲区。"""
        from app.infrastructure.evaluation.scenario_process import (
            redact_bounded,
        )

        reasons = list(incomplete_reasons or [])
        reasons.append(
            f"{stage} failed: "
            f"{stage_entry.get('error') or 'timed out'}"
        )
        evidence: dict[str, Any] = {
            "status": "incomplete",
            "reason": "; ".join(reasons),
            "protocol": list(protocol or []),
            "acp": {
                "source": "acp",
                "conversation_id": conversation_id,
                "session_id": None,
                "process_identity": (
                    handle.process_identity if handle is not None else None
                ),
                "stages": list(stages or []),
                "stderr": (
                    redact_bounded(
                        handle.stderr_text(), limit=self._output_limit,
                        redactor=self._redactor,
                    ) if handle is not None else ""
                ),
            },
            "tool_calls": [],
            "steps": [],
        }
        payload = self._error(
            "transport_error", error_type,
            side_effects_unobserved=side_effects,
        )
        payload["output"] = {"scenario_evidence": evidence}
        return payload

    @staticmethod
    def _error(
        status: str, error_type: str = "", *,
        side_effects_unobserved: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": status,
            "error_type": error_type,
            "output": {},
            "side_effects": [],
            "retrievals": [],
            "run_id": None,
        }
        if side_effects_unobserved:
            payload["side_effects_unobserved"] = True
        return payload

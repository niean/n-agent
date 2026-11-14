"""实验执行管理面；旧 experiments 登记接口保持不变。"""
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from app.domain.evaluation.errors import EvaluationConflictError, EvaluationNotFoundError
from app.domain.evaluation.execution import ExecutionCapacityError, ExecutionNotReadyError
from app.domain.evaluation.models import EvaluationValidationError
from app.interfaces.http.evaluation_routes import _err, _guarded, _json_ok, _read_json_body


async def _response(awaitable, *, accepted=False):
    try:
        result = await awaitable
        return JSONResponse(status_code=202, content=result, headers={"Cache-Control": "no-store"}) if accepted else _json_ok(result)
    except ExecutionNotReadyError as exc:
        return _err(503, "execution_not_ready", str(exc))
    except ExecutionCapacityError:
        return _err(503, "execution_capacity", "实验队列已满，请稍后重试")
    except EvaluationConflictError:
        return _err(409, "execution_conflict", "提交标识与已有请求不一致")
    except EvaluationNotFoundError:
        return _err(404, "not_found", "数据集、评分定义或执行记录不存在")
    except EvaluationValidationError:
        return _err(422, "execution_invalid", "实验参数、数据集版本或评分器不符合要求")


def register_evaluation_execution_routes(app: Any, *, service: Any, preflight: Any = None) -> None:
    @app.get("/chat/evaluations/executions/capabilities")
    @_guarded
    async def capabilities():
        return await _response(service.capabilities())

    @app.post("/chat/evaluations/executions/preflight")
    @_guarded
    async def preflight_check(request: Request):
        """只读预检（plan T5）：valid != runnable，无副作用，不登记。"""
        if preflight is None:
            return _err(503, "preflight_unavailable", "预检服务未接线")
        body, error = await _read_json_body(request)
        if error is not None:
            return error
        try:
            return _json_ok(await preflight.preflight(
                dataset_id=str(body.get("dataset_id") or ""),
                dataset_version=str(body.get("dataset_version") or ""),
                dataset_hash=str(body.get("dataset_hash") or ""),
                evaluator_set=body.get("evaluator_set") or [],
                replicates=int(body.get("replicates") or 1),
                case_ids=body.get("case_ids"),
            ))
        except EvaluationValidationError:
            return _err(422, "execution_invalid", "预检参数不符合要求")
        except EvaluationNotFoundError:
            return _err(404, "not_found", "数据集或版本不存在")

    @app.post("/chat/evaluations/executions")
    @_guarded
    async def submit(request: Request):
        body, error = await _read_json_body(request)
        if error is not None:
            return error
        return await _response(service.submit(body), accepted=True)

    @app.get("/chat/evaluations/executions")
    @_guarded
    async def list_executions(request: Request):
        try:
            limit = int(request.query_params.get("limit", "50"))
        except ValueError:
            return _err(422, "execution_invalid", "limit 必须为 1..100")
        return await _response(service.list(limit=limit, cursor=request.query_params.get("cursor")))

    @app.get("/chat/evaluations/executions/{execution_id}")
    @_guarded
    async def detail(execution_id: str):
        return await _response(service.get(execution_id))

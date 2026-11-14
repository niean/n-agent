"""Dashboard 实验执行控制：质量结果与执行状态相互独立。"""
from typing import Any, Mapping, Protocol

from app.domain.evaluation.models import EvaluationValidationError

MAX_QUEUED = 20
MAX_ITEMS = 100
MAX_REPLICATES = 10
LEASE_SECONDS = 60
TERMINAL_PHASES = frozenset({"completed", "failed", "interrupted"})
TRANSITIONS = {
    "queued": frozenset({"running"}),
    "running": frozenset({"scoring", "failed", "interrupted"}),
    "scoring": frozenset({"completed", "failed"}),
}

# ---------------------------------------------------------------------------
# 独立资源生命周期状态（plan T5，spec 6.3）：不属于执行 phase 词表，
# 不进入 TRANSITIONS / TERMINAL_PHASES——phase 表达"流程走到哪一步"，
# release/retention 表达"资源账目收敛到哪一步"，二者并行演进。
# ---------------------------------------------------------------------------
RELEASE_STATES: tuple[str, ...] = (
    "release_pending",
    "releasing",
    "released",
    "release_failed",
    "retention_registered",
)
RELEASE_TRANSITIONS: dict[str, frozenset[str]] = {
    "release_pending": frozenset({"releasing"}),
    "releasing": frozenset({"released", "release_failed", "retention_registered"}),
    # 释放失败保留可恢复账目：可重入重试，不是终态
    "release_failed": frozenset({"releasing"}),
    "released": frozenset(),
    "retention_registered": frozenset(),
}


def validate_release_transition(current: str, target: str) -> None:
    if target not in RELEASE_STATES:
        raise EvaluationValidationError(f"unknown release state: {target!r}")
    if target != current and target not in RELEASE_TRANSITIONS.get(current, frozenset()):
        raise EvaluationValidationError("illegal release state transition")


# ---------------------------------------------------------------------------
# 场景预算（plan T5，spec 6.3）：持久绝对 deadline，重启/重试不刷新时钟。
# task 600 秒从创建算起；评分总窗口 900 秒；资源释放独立预算；执行串行
# 上界 = 项数 × 单 case 上限，超过服务器项数上限整单拒绝。
# ---------------------------------------------------------------------------
SCENARIO_TASK_TIMEOUT_SECONDS = 600.0
SCENARIO_SCORING_WINDOW_SECONDS = 900.0
SCENARIO_RELEASE_WINDOW_SECONDS = 300.0


def scenario_run_budget_seconds(items: int) -> float:
    """执行阶段串行上界（项数 × 单 case 上限）。"""
    if items < 0:
        raise EvaluationValidationError("items must be >= 0")
    return items * SCENARIO_TASK_TIMEOUT_SECONDS


def scenario_total_budget_seconds(items: int) -> float:
    """总预算 = 执行串行上界 + 评分窗口 + 释放窗口。"""
    return (
        scenario_run_budget_seconds(items)
        + SCENARIO_SCORING_WINDOW_SECONDS
        + SCENARIO_RELEASE_WINDOW_SECONDS
    )


class ExecutionNotReadyError(Exception):
    pass


class ExecutionCapacityError(Exception):
    pass


def validate_transition(current: str, target: str) -> None:
    if target != current and target not in TRANSITIONS.get(current, ()):
        raise EvaluationValidationError("illegal execution phase transition")


class ExecutionStore(Protocol):
    async def submit(self, request: Mapping[str, Any], *, now: float) -> Mapping[str, Any]: ...
    async def get(self, execution_id: str) -> Mapping[str, Any]: ...
    async def find_request(self, request_key: str) -> Mapping[str, Any] | None: ...
    async def claim(self, *, now: float) -> Mapping[str, Any] | None: ...
    async def heartbeat(self, execution_id: str, token: str, *, now: float) -> None: ...
    async def update(self, execution_id: str, token: str, *, now: float, **changes: Any) -> Mapping[str, Any]: ...
    async def release_scoring(self, execution_id: str, token: str, *, now: float) -> None: ...
    async def list_page(self, *, limit: int = 50, cursor: str | None = None) -> Mapping[str, Any]: ...

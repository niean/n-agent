"""TurnPolicy -- the single decision-maker for the turn loop.

Pure Domain: no LangGraph, no asyncio, no time.now(). The Application
layer supplies elapsed/deadline facts via TurnEvaluationInput.

Decision table (in priority order):
1. cancelled -> terminal, CANCELLED
2. error -> terminal, ERROR
3. final_message present (no tool calls) -> terminal, STOP
4. iteration_count >= iteration_limit -> terminal, ITERATION_LIMIT
5. elapsed >= turn_timeout_seconds -> terminal, DEADLINE
6. budget_exhausted -> terminal, BUDGET_EXHAUSTED
7. pending_tool_calls -> execute_tools, NOT terminal
8. otherwise -> continue (call_llm), NOT terminal
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.domain.agent import EndReason


class TurnNextStep(str, Enum):
    """Stable next-step targets for the Application layer to map to graph nodes."""

    CALL_LLM = "call_llm"
    EXECUTE_TOOLS = "execute_tools"
    FINALIZE = "finalize"
    END = "end"


@dataclass(frozen=True)
class TurnDecision:
    """The decision output of TurnPolicy.evaluate."""

    next_step: TurnNextStep
    end_reason: EndReason | None
    terminal: bool
    reason: str


@dataclass(frozen=True)
class TurnEvaluationInput:
    """Domain projection of AgentState + runtime facts supplied by Application.

    The Application layer builds this from AgentState and external facts
    (elapsed time, budget status). TurnPolicy never calls time.now() or
    checks budget state directly -- it receives facts as input.
    """

    final_message: dict[str, Any] | None
    error: str | None
    pending_tool_calls: list
    iteration_count: int
    cancelled: bool
    elapsed_seconds: float
    turn_timeout_seconds: float | None
    iteration_limit: int
    budget_exhausted: bool


class TurnPolicy:
    """Pure-function turn-loop decision maker.

    Given a TurnEvaluationInput, returns a TurnDecision. No side effects,
    no I/O, no time calls. The Application layer is responsible for
    supplying accurate facts.
    """

    def evaluate(self, inp: TurnEvaluationInput) -> TurnDecision:
        # 1. Cancelled (interrupt/ACP cancel) -- highest priority
        if inp.cancelled:
            return TurnDecision(
                TurnNextStep.END, EndReason.CANCELLED, True, "cancelled",
            )

        # 2. Error (state.error set by call_llm or tools)
        if inp.error:
            return TurnDecision(
                TurnNextStep.END, EndReason.ERROR, True, "error",
            )

        # 3. Final message present with no tool calls -- normal stop
        if inp.final_message is not None and not inp.pending_tool_calls:
            return TurnDecision(
                TurnNextStep.END, EndReason.STOP, True, "final_message",
            )

        # 4. Iteration limit reached
        if inp.iteration_count >= inp.iteration_limit:
            return TurnDecision(
                TurnNextStep.FINALIZE, EndReason.ITERATION_LIMIT, True, "iteration_limit",
            )

        # 5. Deadline exceeded (wall-clock)
        if (
            inp.turn_timeout_seconds is not None
            and inp.elapsed_seconds >= inp.turn_timeout_seconds
        ):
            return TurnDecision(
                TurnNextStep.END, EndReason.DEADLINE, True, "deadline",
            )

        # 6. Budget exhausted (from T8 Budget deny)
        if inp.budget_exhausted:
            return TurnDecision(
                TurnNextStep.END, EndReason.BUDGET_EXHAUSTED, True, "budget_exhausted",
            )

        # 7. Pending tool calls -- route to tool execution
        if inp.pending_tool_calls:
            return TurnDecision(
                TurnNextStep.EXECUTE_TOOLS, None, False, "pending_tool_calls",
            )

        # 8. Otherwise -- continue to next LLM call
        return TurnDecision(
            TurnNextStep.CALL_LLM, None, False, "continue",
        )

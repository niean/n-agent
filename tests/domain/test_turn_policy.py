"""Tests for TurnPolicy -- the single decision-maker for the turn loop.

Decision table (in priority order):
1. cancelled -> terminal, CANCELLED
2. error -> terminal, ERROR
3. final_message present (no tool calls) -> terminal, STOP
4. iteration_count >= iteration_limit -> terminal, ITERATION_LIMIT
5. elapsed >= turn_timeout_seconds -> terminal, DEADLINE
6. budget_exhausted -> terminal, BUDGET_EXHAUSTED
7. pending_tool_calls -> execute_tools, NOT terminal
8. otherwise -> continue, NOT terminal
"""
from __future__ import annotations

import pytest

from app.domain.agent import EndReason
from app.domain.turn_policy import (
    TurnDecision,
    TurnEvaluationInput,
    TurnNextStep,
    TurnPolicy,
)


def _make_input(
    *,
    final_message: dict | None = None,
    error: str | None = None,
    pending_tool_calls: list | None = None,
    iteration_count: int = 0,
    cancelled: bool = False,
    elapsed_seconds: float = 0.0,
    turn_timeout_seconds: float | None = 900.0,
    iteration_limit: int = 10,
    budget_exhausted: bool = False,
) -> TurnEvaluationInput:
    return TurnEvaluationInput(
        final_message=final_message,
        error=error,
        pending_tool_calls=pending_tool_calls or [],
        iteration_count=iteration_count,
        cancelled=cancelled,
        elapsed_seconds=elapsed_seconds,
        turn_timeout_seconds=turn_timeout_seconds,
        iteration_limit=iteration_limit,
        budget_exhausted=budget_exhausted,
    )


class TestTurnPolicyCancelled:
    def test_cancelled_is_terminal_with_cancelled_reason(self):
        policy = TurnPolicy()
        inp = _make_input(cancelled=True)
        decision = policy.evaluate(inp)
        assert decision.terminal is True
        assert decision.end_reason is EndReason.CANCELLED
        assert decision.next_step is TurnNextStep.END

    def test_cancelled_takes_priority_over_error(self):
        policy = TurnPolicy()
        inp = _make_input(cancelled=True, error="something went wrong")
        decision = policy.evaluate(inp)
        assert decision.end_reason is EndReason.CANCELLED

    def test_cancelled_takes_priority_over_final_message(self):
        policy = TurnPolicy()
        inp = _make_input(cancelled=True, final_message={"role": "assistant", "content": "done"})
        decision = policy.evaluate(inp)
        assert decision.end_reason is EndReason.CANCELLED

    def test_cancelled_takes_priority_over_iteration_limit(self):
        policy = TurnPolicy()
        inp = _make_input(cancelled=True, iteration_count=100, iteration_limit=10)
        decision = policy.evaluate(inp)
        assert decision.end_reason is EndReason.CANCELLED

    def test_cancelled_takes_priority_over_deadline(self):
        policy = TurnPolicy()
        inp = _make_input(cancelled=True, elapsed_seconds=9999.0)
        decision = policy.evaluate(inp)
        assert decision.end_reason is EndReason.CANCELLED

    def test_cancelled_takes_priority_over_budget_exhausted(self):
        policy = TurnPolicy()
        inp = _make_input(cancelled=True, budget_exhausted=True)
        decision = policy.evaluate(inp)
        assert decision.end_reason is EndReason.CANCELLED


class TestTurnPolicyError:
    def test_error_is_terminal_with_error_reason(self):
        policy = TurnPolicy()
        inp = _make_input(error="LLM failed")
        decision = policy.evaluate(inp)
        assert decision.terminal is True
        assert decision.end_reason is EndReason.ERROR
        assert decision.next_step is TurnNextStep.END

    def test_error_takes_priority_over_final_message(self):
        policy = TurnPolicy()
        inp = _make_input(error="boom", final_message={"role": "assistant", "content": "done"})
        decision = policy.evaluate(inp)
        assert decision.end_reason is EndReason.ERROR

    def test_error_takes_priority_over_iteration_limit(self):
        policy = TurnPolicy()
        inp = _make_input(error="boom", iteration_count=100, iteration_limit=10)
        decision = policy.evaluate(inp)
        assert decision.end_reason is EndReason.ERROR

    def test_error_takes_priority_over_deadline(self):
        policy = TurnPolicy()
        inp = _make_input(error="boom", elapsed_seconds=9999.0)
        decision = policy.evaluate(inp)
        assert decision.end_reason is EndReason.ERROR

    def test_error_takes_priority_over_budget_exhausted(self):
        policy = TurnPolicy()
        inp = _make_input(error="boom", budget_exhausted=True)
        decision = policy.evaluate(inp)
        assert decision.end_reason is EndReason.ERROR


class TestTurnPolicyFinalMessage:
    def test_final_message_no_tools_is_terminal_stop(self):
        policy = TurnPolicy()
        inp = _make_input(final_message={"role": "assistant", "content": "answer"})
        decision = policy.evaluate(inp)
        assert decision.terminal is True
        assert decision.end_reason is EndReason.STOP
        assert decision.next_step is TurnNextStep.END

    def test_final_message_with_tool_calls_is_not_stop(self):
        """final_message present but pending_tool_calls also present -> not STOP."""
        policy = TurnPolicy()
        inp = _make_input(
            final_message={"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            pending_tool_calls=[{"id": "c1"}],
        )
        decision = policy.evaluate(inp)
        # Should NOT be terminal STOP -- falls through to iteration/tools
        assert decision.end_reason is not EndReason.STOP

    def test_final_message_takes_priority_over_iteration_limit(self):
        policy = TurnPolicy()
        inp = _make_input(
            final_message={"role": "assistant", "content": "done"},
            iteration_count=100,
            iteration_limit=10,
        )
        decision = policy.evaluate(inp)
        assert decision.end_reason is EndReason.STOP

    def test_final_message_takes_priority_over_deadline(self):
        policy = TurnPolicy()
        inp = _make_input(
            final_message={"role": "assistant", "content": "done"},
            elapsed_seconds=9999.0,
        )
        decision = policy.evaluate(inp)
        assert decision.end_reason is EndReason.STOP

    def test_final_message_takes_priority_over_budget_exhausted(self):
        policy = TurnPolicy()
        inp = _make_input(
            final_message={"role": "assistant", "content": "done"},
            budget_exhausted=True,
        )
        decision = policy.evaluate(inp)
        assert decision.end_reason is EndReason.STOP


class TestTurnPolicyIterationLimit:
    def test_iteration_limit_reached_is_terminal(self):
        policy = TurnPolicy()
        inp = _make_input(iteration_count=10, iteration_limit=10)
        decision = policy.evaluate(inp)
        assert decision.terminal is True
        assert decision.end_reason is EndReason.ITERATION_LIMIT
        assert decision.next_step is TurnNextStep.FINALIZE

    def test_iteration_below_limit_not_terminal(self):
        policy = TurnPolicy()
        inp = _make_input(iteration_count=5, iteration_limit=10)
        decision = policy.evaluate(inp)
        assert decision.terminal is False

    def test_iteration_limit_with_tool_calls_returns_iter_limit_not_tools(self):
        """When at iteration limit and LLM returned tool_calls, ITERATION_LIMIT wins."""
        policy = TurnPolicy()
        inp = _make_input(
            pending_tool_calls=[{"id": "c1"}],
            iteration_count=10,
            iteration_limit=10,
        )
        decision = policy.evaluate(inp)
        assert decision.end_reason is EndReason.ITERATION_LIMIT
        assert decision.terminal is True

    def test_iteration_limit_takes_priority_over_deadline(self):
        policy = TurnPolicy()
        inp = _make_input(
            iteration_count=10,
            iteration_limit=10,
            elapsed_seconds=9999.0,
        )
        decision = policy.evaluate(inp)
        assert decision.end_reason is EndReason.ITERATION_LIMIT

    def test_iteration_limit_takes_priority_over_budget(self):
        policy = TurnPolicy()
        inp = _make_input(
            iteration_count=10,
            iteration_limit=10,
            budget_exhausted=True,
        )
        decision = policy.evaluate(inp)
        assert decision.end_reason is EndReason.ITERATION_LIMIT


class TestTurnPolicyDeadline:
    def test_deadline_exceeded_is_terminal(self):
        policy = TurnPolicy()
        inp = _make_input(elapsed_seconds=900.0, turn_timeout_seconds=900.0)
        decision = policy.evaluate(inp)
        assert decision.terminal is True
        assert decision.end_reason is EndReason.DEADLINE
        assert decision.next_step is TurnNextStep.END

    def test_deadline_not_exceeded_not_terminal(self):
        policy = TurnPolicy()
        inp = _make_input(elapsed_seconds=100.0, turn_timeout_seconds=900.0)
        decision = policy.evaluate(inp)
        assert decision.terminal is False

    def test_no_deadline_disabled(self):
        """When turn_timeout_seconds is None, deadline check is skipped."""
        policy = TurnPolicy()
        inp = _make_input(elapsed_seconds=999999.0, turn_timeout_seconds=None)
        decision = policy.evaluate(inp)
        assert decision.terminal is False

    def test_deadline_takes_priority_over_budget(self):
        policy = TurnPolicy()
        inp = _make_input(
            elapsed_seconds=9999.0,
            turn_timeout_seconds=900.0,
            budget_exhausted=True,
        )
        decision = policy.evaluate(inp)
        assert decision.end_reason is EndReason.DEADLINE


class TestTurnPolicyBudgetExhausted:
    def test_budget_exhausted_is_terminal(self):
        policy = TurnPolicy()
        inp = _make_input(budget_exhausted=True)
        decision = policy.evaluate(inp)
        assert decision.terminal is True
        assert decision.end_reason is EndReason.BUDGET_EXHAUSTED
        assert decision.next_step is TurnNextStep.END


class TestTurnPolicyPendingTools:
    def test_pending_tool_calls_routes_to_execute_tools(self):
        policy = TurnPolicy()
        inp = _make_input(pending_tool_calls=[{"id": "c1"}])
        decision = policy.evaluate(inp)
        assert decision.terminal is False
        assert decision.next_step is TurnNextStep.EXECUTE_TOOLS
        assert decision.end_reason is None

    def test_pending_tool_calls_not_terminal(self):
        policy = TurnPolicy()
        inp = _make_input(
            pending_tool_calls=[{"id": "c1"}],
            iteration_count=3,
            iteration_limit=10,
        )
        decision = policy.evaluate(inp)
        assert decision.terminal is False


class TestTurnPolicyContinue:
    def test_no_conditions_routes_to_continue(self):
        policy = TurnPolicy()
        inp = _make_input()
        decision = policy.evaluate(inp)
        assert decision.terminal is False
        assert decision.next_step is TurnNextStep.CALL_LLM
        assert decision.end_reason is None

    def test_continue_with_elapsed_below_deadline(self):
        policy = TurnPolicy()
        inp = _make_input(iteration_count=3, iteration_limit=10, elapsed_seconds=50.0)
        decision = policy.evaluate(inp)
        assert decision.terminal is False
        assert decision.next_step is TurnNextStep.CALL_LLM


class TestTurnPolicyPure:
    def test_turn_policy_does_not_import_langgraph(self):
        import ast
        import pathlib

        src = pathlib.Path("app/domain/turn_policy.py").read_text()
        tree = ast.parse(src)
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
        forbidden = ("langgraph", "asyncio", "time", "pydantic", "fastapi", "openai")
        violations = [m for m in modules if m in forbidden or m.startswith(forbidden)]
        assert not violations, f"turn_policy.py imports forbidden modules: {violations}"

    def test_turn_decision_is_frozen(self):
        d = TurnDecision(
            next_step=TurnNextStep.END,
            end_reason=EndReason.STOP,
            terminal=True,
            reason="test",
        )
        with pytest.raises(Exception):
            d.next_step = TurnNextStep.CALL_LLM  # type: ignore[misc]

    def test_turn_evaluation_input_is_frozen(self):
        inp = TurnEvaluationInput(
            final_message=None,
            error=None,
            pending_tool_calls=[],
            iteration_count=0,
            cancelled=False,
            elapsed_seconds=0.0,
            turn_timeout_seconds=900.0,
            iteration_limit=10,
            budget_exhausted=False,
        )
        with pytest.raises(Exception):
            inp.cancelled = True  # type: ignore[misc]

    def test_policy_has_no_external_dependencies(self):
        """TurnPolicy.evaluate is a pure function -- same input, same output."""
        policy = TurnPolicy()
        inp = _make_input(final_message={"role": "assistant", "content": "hi"})
        d1 = policy.evaluate(inp)
        d2 = policy.evaluate(inp)
        assert d1 == d2

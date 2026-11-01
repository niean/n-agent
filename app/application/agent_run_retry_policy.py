"""Select retry relationships from predecessor facts, independently of storage."""
from app.domain.agent_run import RunRetryFacts


class AgentRunRetryPolicy:
    def resolve(self, execution_kind: str, facts: RunRetryFacts) -> str | None:
        if facts.member_runs:
            return facts.member_runs[0].run_id
        if execution_kind != "task_worker":
            return None
        if any(run.execution_kind == "task_worker" for run in facts.current_attempt_runs):
            return None
        return next((run.run_id for run in facts.earlier_attempt_runs
                     if run.execution_kind == "task_worker"), None)

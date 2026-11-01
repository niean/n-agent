import pytest

from app.application.agent_run_retry_policy import AgentRunRetryPolicy
from app.domain.agent_run import RunRetryFacts, RunPredecessor


@pytest.mark.asyncio
async def test_retry_policy_distinguishes_worker_continuation_and_new_attempt():
    policy = AgentRunRetryPolicy()
    previous = RunPredecessor("worker-before", "task_worker", 1)
    facts = RunRetryFacts(earlier_attempt_runs=(previous,))
    assert policy.resolve("task_worker", facts) == "worker-before"
    assert policy.resolve("task_judge", facts) is None
    continuation = RunRetryFacts(
        current_attempt_runs=(RunPredecessor("worker-now", "task_worker", 2),),
        earlier_attempt_runs=(previous,),
    )
    assert policy.resolve("task_worker", continuation) is None


@pytest.mark.asyncio
async def test_retry_policy_uses_member_predecessor_without_task_inference():
    facts = RunRetryFacts(member_runs=(RunPredecessor("member-previous", "delegation_worker"),))
    assert AgentRunRetryPolicy().resolve("delegation_worker", facts) == "member-previous"

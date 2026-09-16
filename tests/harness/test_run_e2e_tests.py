from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_ROOT = PROJECT_ROOT.parent / "harness-tpl"
RUNNER = (
    PROJECT_ROOT
    / ".harness/framework/skills/harness/run-e2e-tests/scripts/run-e2e.py"
)


def _run(command: str, timeout: int = 5) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RUNNER), command, str(timeout)],
        cwd=PROJECT_ROOT / "tests",
        capture_output=True,
        text=True,
        check=False,
    )


def test_runner_executes_from_repository_root() -> None:
    result = _run(f'test "$(pwd)" = "{PROJECT_ROOT}"')

    assert result.returncode == 0
    assert result.stdout == "e2e: executed\n"


def test_runner_preserves_nonzero_exit_code() -> None:
    result = _run("exit 7")

    assert result.returncode == 7
    assert "e2e: failed (exit_code=7)" in result.stderr


def test_runner_times_out_without_waiting_for_full_command() -> None:
    started = time.monotonic()
    result = _run("sleep 30", timeout=1)

    assert result.returncode == 124
    assert time.monotonic() - started < 8
    assert "e2e: timed_out (timeoutSeconds=1)" in result.stderr


def test_skill_uses_tests_e2e_config_and_shared_runner() -> None:
    skill = (
        PROJECT_ROOT
        / ".harness/framework/skills/harness/run-e2e-tests/SKILL.md"
    ).read_text(encoding="utf-8")

    assert "# Skill: 执行端到端测试" in skill
    assert "tests.e2e.enabled" in skill
    assert "tests.e2e.command" in skill
    assert "tests.e2e.timeoutSeconds" in skill
    assert "enabled=false" in skill
    assert "run-e2e-tests/scripts/run-e2e.py" in skill


def test_target_workflows_run_end_to_end_tests_inside_feedback_loop() -> None:
    expected_return_phases = {
        "iterate-feature.md": "回到 Phase 4",
        "refine-feature.md": "回到 Phase 2",
        "fix-bug.md": "回到 Phase 2",
    }

    for filename, return_phase in expected_return_phases.items():
        workflow = (
            PROJECT_ROOT / ".harness/framework/workflows" / filename
        ).read_text(encoding="utf-8")
        assert "Skill: 执行端到端测试" in workflow
        assert "run-e2e-tests/SKILL.md" in workflow
        assert return_phase in workflow
        assert "最多 3 轮反馈循环" in workflow


def test_template_contains_the_same_end_to_end_test_capability() -> None:
    project_skill = (
        PROJECT_ROOT / ".harness/framework/skills/harness/run-e2e-tests/SKILL.md"
    ).read_text(encoding="utf-8")
    template_skill = (
        TEMPLATE_ROOT / ".harness/framework/skills/harness/run-e2e-tests/SKILL.md"
    ).read_text(encoding="utf-8")
    project_runner = RUNNER.read_text(encoding="utf-8")
    template_runner = (
        TEMPLATE_ROOT
        / ".harness/framework/skills/harness/run-e2e-tests/scripts/run-e2e.py"
    ).read_text(encoding="utf-8")

    assert template_skill == project_skill
    assert template_runner == project_runner

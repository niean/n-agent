from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
THIRD_REVIEW_SKILL = (
    PROJECT_ROOT / ".harness/framework/skills/harness/third-review"
)


def _build_review_repo(
    tmp_path: Path,
    *,
    modify_target: bool,
    provider_output: str = "",
) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    skill = repo / ".harness/framework/skills/harness/third-review"
    target = repo / ".harness/specs/active/spec.md"
    shutil.copytree(THIRD_REVIEW_SKILL, skill)
    target.parent.mkdir(parents=True)
    target.write_text("# Spec\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    provider = skill / "providers/silent-failure.sh"
    provider.write_text(
        """#!/bin/sh
prompt_file=$(mktemp)
trap 'rm -f "$prompt_file"' EXIT
cat >"$prompt_file"
if [ "${FAKE_THIRD_REVIEW_MODIFY:-0}" = 1 ]; then
    target_file=$(sed -n 's/^TARGET_FILE: //p' "$prompt_file")
    printf '%s\n' 'provider edit' >>"$target_file"
fi
printf '%s' "${FAKE_THIRD_REVIEW_OUTPUT:-}"
printf '%s\n' 'failed to initialize provider client' >&2
exit 0
""",
        encoding="utf-8",
    )
    provider.chmod(0o755)

    env = os.environ.copy()
    env["HARNESS_THIRD_REVIEW_PROVIDER"] = "silent-failure"
    env["HARNESS_THIRD_REVIEW_TIMEOUT_SECONDS"] = "5"
    env["FAKE_THIRD_REVIEW_MODIFY"] = "1" if modify_target else "0"
    env["FAKE_THIRD_REVIEW_OUTPUT"] = provider_output
    return repo, env


@pytest.mark.parametrize("modify_target", [False, True])
def test_empty_provider_output_is_execution_failure(
    tmp_path: Path,
    modify_target: bool,
) -> None:
    repo, env = _build_review_repo(tmp_path, modify_target=modify_target)

    result = subprocess.run(
        [
            "sh",
            ".harness/framework/skills/harness/third-review/scripts/run-review.sh",
            "spec",
            ".harness/specs/active/spec.md",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "provider: provider produced no review output" in result.stderr
    assert "provider stderr: failed to initialize provider client" in result.stderr
    assert "状态: approved" not in result.stdout
    assert "状态: fixed" not in result.stdout


@pytest.mark.parametrize("modify_target", [False, True])
@pytest.mark.parametrize(
    "invalid_output",
    [
        "review completed, but this is not the five-field summary\n",
        (
            "状态: maybe\n"
            "修改数量: 0 项\n"
            "修改摘要: 无\n"
            "目标未达说明: 已覆盖审阅维度\n"
            "剩余风险: 无\n"
        ),
    ],
)
def test_nonempty_invalid_summary_warns_and_preserves_provider_output(
    tmp_path: Path,
    modify_target: bool,
    invalid_output: str,
) -> None:
    repo, env = _build_review_repo(
        tmp_path,
        modify_target=modify_target,
        provider_output=invalid_output,
    )

    result = subprocess.run(
        [
            "sh",
            ".harness/framework/skills/harness/third-review/scripts/run-review.sh",
            "spec",
            ".harness/specs/active/spec.md",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == invalid_output
    assert "warning: provider structured summary is invalid" in result.stderr
    assert "provider: provider emitted diagnostic output" in result.stderr


def test_valid_provider_summary_still_succeeds(tmp_path: Path) -> None:
    repo, env = _build_review_repo(tmp_path, modify_target=False)
    provider = (
        repo
        / ".harness/framework/skills/harness/third-review/providers/silent-failure.sh"
    )
    provider.write_text(
        """#!/bin/sh
cat >/dev/null
printf '%s\n' \
    '状态: approved' \
    '修改数量: 0 项' \
    '修改摘要: 无' \
    '目标未达说明: 未发现需修改问题；已覆盖需求、结构、门禁与验收维度' \
    '剩余风险: 无'
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "sh",
            ".harness/framework/skills/harness/third-review/scripts/run-review.sh",
            "spec",
            ".harness/specs/active/spec.md",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.startswith("状态: approved\n修改数量: 0 项\n")
    assert result.stderr == ""


def test_missing_provider_configuration_fails_validation(tmp_path: Path) -> None:
    repo, env = _build_review_repo(tmp_path, modify_target=False)
    env.pop("HARNESS_THIRD_REVIEW_PROVIDER")

    result = subprocess.run(
        [
            "sh",
            ".harness/framework/skills/harness/third-review/scripts/run-review.sh",
            "spec",
            ".harness/specs/active/spec.md",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "validation: provider is not configured" in result.stderr
    assert "状态: approved" not in result.stdout
    assert "状态: fixed" not in result.stdout


def test_timeout_and_foreground_invocation_contract() -> None:
    runner = (THIRD_REVIEW_SKILL / "scripts/run-review.sh").read_text(
        encoding="utf-8"
    )
    skill = (THIRD_REVIEW_SKILL / "SKILL.md").read_text(encoding="utf-8")

    assert "${HARNESS_THIRD_REVIEW_TIMEOUT_SECONDS:-900}" in runner
    assert "Invoke the runner in the foreground" in skill
    assert "must not append `&` or pipe the command" in skill
    assert "provider and watchdog as managed background children" in skill
    assert "third_review: disabled" in skill
    assert "get-config.sh thirdReview.enabled" in skill
    assert "Do not read or parse `.harness/harness.json` directly" in skill

    project = (PROJECT_ROOT / ".harness/PROJECT.md").read_text(encoding="utf-8")
    iterate_workflow = (
        PROJECT_ROOT / ".harness/framework/workflows/iterate-feature.md"
    ).read_text(encoding="utf-8")
    refine_workflow = (
        PROJECT_ROOT / ".harness/framework/workflows/refine-feature.md"
    ).read_text(encoding="utf-8")
    fix_workflow = (
        PROJECT_ROOT / ".harness/framework/workflows/fix-bug.md"
    ).read_text(encoding="utf-8")

    assert ".harness/harness.json" in skill
    assert ".harness/harness.json" in project
    assert "## 三方审阅\n\n- enabled:" not in project
    assert "third_review: disabled/executed/skipped/awaiting-skip-confirmation" in iterate_workflow
    for workflow in (iterate_workflow, refine_workflow, fix_workflow):
        assert "hooks.afterFinish.enabled" in workflow
        assert "hook: disabled/skipped/executed/failed" in workflow

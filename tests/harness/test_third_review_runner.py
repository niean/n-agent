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
CLAUDE_CODE_PROVIDER = THIRD_REVIEW_SKILL / "providers/claude-code.sh"


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
    retained = _single_review_result(repo)
    assert "validation: provider is not configured" in (
        retained / "stderr.txt"
    ).read_text(encoding="utf-8")
    metadata = (retained / "result.txt").read_text(encoding="utf-8")
    assert "outcome=failure" in metadata
    assert "failure_step=validation" in metadata


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


def _write_fake_claude(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """#!/bin/sh
printf '%s\\n' "$PWD" >"$FAKE_CLAUDE_CWD"
printf '%s\\n' "$@" >"$FAKE_CLAUDE_ARGS"
printf '%s\\n' "${CLAUDE_CODE_SIMPLE:-}" >"$FAKE_CLAUDE_SIMPLE"
cat >"$FAKE_CLAUDE_PROMPT"
printf '%s\\n' \\
    '状态: approved' \\
    '修改数量: 0 项' \\
    '修改摘要: 无' \\
    '目标未达说明: 已覆盖需求、结构、门禁与验收维度，未发现真实问题' \\
    '剩余风险: 无'
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _claude_provider_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["FAKE_CLAUDE_CWD"] = str(tmp_path / "cwd")
    env["FAKE_CLAUDE_ARGS"] = str(tmp_path / "args")
    env["FAKE_CLAUDE_SIMPLE"] = str(tmp_path / "simple")
    env["FAKE_CLAUDE_PROMPT"] = str(tmp_path / "prompt")
    return env


def _single_review_result(repo: Path) -> Path:
    result_root = repo / "locals/harness_tmp/third-review-results"
    results = [path for path in result_root.iterdir() if path.is_dir()]
    assert len(results) == 1
    return results[0]


def test_claude_code_provider_invokes_standalone_cli_with_direct_edit_tools(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / ".harness/specs/active/spec.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Spec\n", encoding="utf-8")
    claude = tmp_path / "bin/claude"
    _write_fake_claude(claude)
    env = _claude_provider_env(tmp_path)
    env["HARNESS_THIRD_REVIEW_CLAUDE_CODE_BIN"] = str(claude)
    env["HARNESS_THIRD_REVIEW_MODEL"] = "claude-sonnet-test"
    env["HARNESS_THIRD_REVIEW_TARGET_FILE"] = str(target)

    result = subprocess.run(
        ["sh", str(CLAUDE_CODE_PROVIDER), str(repo)],
        cwd=PROJECT_ROOT,
        env=env,
        input="review prompt\n",
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.startswith("状态: approved\n")
    assert (tmp_path / "cwd").read_text(encoding="utf-8").strip() == str(repo)
    prompt = (tmp_path / "prompt").read_text(encoding="utf-8")
    assert prompt == "review prompt\n"
    assert (tmp_path / "simple").read_text(encoding="utf-8").strip() == ""
    args = (tmp_path / "args").read_text(encoding="utf-8").splitlines()
    assert args == [
        "--print",
        "--output-format",
        "text",
        "--safe-mode",
        "--permission-mode",
        "acceptEdits",
        "--tools",
        "Read,Edit",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--no-chrome",
        "--effort",
        "low",
        "--model",
        "claude-sonnet-test",
    ]


def test_runner_dispatches_to_claude_code_provider(tmp_path: Path) -> None:
    repo, env = _build_review_repo(tmp_path, modify_target=False)
    claude = tmp_path / "bin/claude"
    _write_fake_claude(claude)
    env.update(_claude_provider_env(tmp_path))
    env["HARNESS_THIRD_REVIEW_PROVIDER"] = "claude-code"
    env["HARNESS_THIRD_REVIEW_CLAUDE_CODE_BIN"] = str(claude)

    result = subprocess.run(
        [
            "sh",
            ".harness/framework/skills/harness/third-review/scripts/run-review.sh",
            "spec",
            ".harness/specs/active/spec.md",
        ],
        cwd=repo,
        env=env,
        input="",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.startswith("状态: approved\n修改数量: 0 项\n")
    assert result.stderr == ""
    retained = _single_review_result(repo)
    assert (retained / "stdout.txt").read_text(encoding="utf-8") == result.stdout
    assert (retained / "stderr.txt").read_text(encoding="utf-8") == ""
    metadata = (retained / "result.txt").read_text(encoding="utf-8")
    assert "outcome=success" in metadata
    assert "exit_code=0" in metadata
    assert "failure_step=none" in metadata
    assert "provider=claude-code" in metadata


def test_runner_retains_provider_failure_details(tmp_path: Path) -> None:
    repo, env = _build_review_repo(tmp_path, modify_target=False)
    provider = (
        repo
        / ".harness/framework/skills/harness/third-review/providers/silent-failure.sh"
    )
    provider.write_text(
        "#!/bin/sh\ncat >/dev/null\nprintf '%s\\n' 'provider exploded' >&2\nexit 23\n",
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

    assert result.returncode == 23
    retained = _single_review_result(repo)
    assert (retained / "stdout.txt").read_text(encoding="utf-8") == ""
    assert "provider exploded" in (retained / "stderr.txt").read_text(
        encoding="utf-8"
    )
    metadata = (retained / "result.txt").read_text(encoding="utf-8")
    assert "outcome=failure" in metadata
    assert "failure_step=provider" in metadata
    assert "exit_code=23" in metadata


def test_runner_retains_timeout_details(tmp_path: Path) -> None:
    repo, env = _build_review_repo(tmp_path, modify_target=False)
    provider = (
        repo
        / ".harness/framework/skills/harness/third-review/providers/silent-failure.sh"
    )
    provider.write_text(
        "#!/bin/sh\ncat >/dev/null\nsleep 30\n",
        encoding="utf-8",
    )
    env["HARNESS_THIRD_REVIEW_TIMEOUT_SECONDS"] = "1"

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

    assert result.returncode == 124
    retained = _single_review_result(repo)
    assert "timed out after 1 seconds" in (retained / "stderr.txt").read_text(
        encoding="utf-8"
    )
    metadata = (retained / "result.txt").read_text(encoding="utf-8")
    assert "outcome=failure" in metadata
    assert "failure_step=provider" in metadata
    assert "exit_code=124" in metadata


def test_claude_code_provider_has_no_secondary_patch_protocol() -> None:
    scripts = {path.name for path in (THIRD_REVIEW_SKILL / "scripts").glob("*.sh")}

    assert scripts == {"run-review.sh"}


def test_claude_code_provider_allows_cli_to_edit_target_directly(
    tmp_path: Path,
) -> None:
    repo, env = _build_review_repo(tmp_path, modify_target=False)
    claude = tmp_path / "bin/claude"
    claude.parent.mkdir(parents=True)
    claude.write_text(
        """#!/bin/sh
cat >"$FAKE_CLAUDE_PROMPT"
printf '%s\\n' '# Spec' '' 'reviewed' >"$HARNESS_THIRD_REVIEW_TARGET_FILE"
printf '%s\\n' \\
    '状态: fixed' \\
    '修改数量: 1 项' \\
    '修改摘要: 补充审阅结论' \\
    '目标未达说明: 真实问题少于20项；已覆盖需求、结构、门禁与验收维度' \\
    '剩余风险: 无'
""",
        encoding="utf-8",
    )
    claude.chmod(0o755)
    env.update(_claude_provider_env(tmp_path))
    env["HARNESS_THIRD_REVIEW_PROVIDER"] = "claude-code"
    env["HARNESS_THIRD_REVIEW_CLAUDE_CODE_BIN"] = str(claude)

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
    assert result.stdout == (
        "状态: fixed\n"
        "修改数量: 1 项\n"
        "修改摘要: 补充审阅结论\n"
        "目标未达说明: 真实问题少于20项；已覆盖需求、结构、门禁与验收维度\n"
        "剩余风险: 无\n"
    )
    assert (
        repo / ".harness/specs/active/spec.md"
    ).read_text(encoding="utf-8") == "# Spec\n\nreviewed\n"
    prompt = (tmp_path / "prompt").read_text(encoding="utf-8")
    assert prompt.startswith("# Spec Third Review")
    assert "TARGET_FILE:" in prompt


def test_claude_code_provider_directly_rewrites_duplicate_content(
    tmp_path: Path,
) -> None:
    repo, env = _build_review_repo(tmp_path, modify_target=False)
    target = repo / ".harness/specs/active/spec.md"
    target.write_text("# Spec\n\n重复段落\n\n重复段落\n", encoding="utf-8")
    claude = tmp_path / "bin/claude"
    claude.parent.mkdir(parents=True)
    claude.write_text(
        """#!/bin/sh
cat >"$FAKE_CLAUDE_PROMPT"
printf '%s\\n' '# Spec' '' '重复段落' '' '第二处已修正' >"$HARNESS_THIRD_REVIEW_TARGET_FILE"
printf '%s\\n' \\
    '状态: fixed' \\
    '修改数量: 1 项' \\
    '修改摘要: 明确第二处内容' \\
    '目标未达说明: 已覆盖需求、结构、门禁与验收维度' \\
    '剩余风险: 无'
""",
        encoding="utf-8",
    )
    claude.chmod(0o755)
    env.update(_claude_provider_env(tmp_path))
    env["HARNESS_THIRD_REVIEW_PROVIDER"] = "claude-code"
    env["HARNESS_THIRD_REVIEW_CLAUDE_CODE_BIN"] = str(claude)

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

    assert result.returncode == 0, result.stderr
    assert target.read_text(encoding="utf-8") == (
        "# Spec\n\n重复段落\n\n第二处已修正\n"
    )


@pytest.mark.parametrize("resolution", ["override", "path"])
def test_claude_code_provider_rejects_vscode_extension_binary(
    tmp_path: Path,
    resolution: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    claude = tmp_path / ".vscode/extensions/anthropic.claude-code/bin/claude"
    _write_fake_claude(claude)
    env = _claude_provider_env(tmp_path)
    if resolution == "override":
        env["HARNESS_THIRD_REVIEW_CLAUDE_CODE_BIN"] = str(claude)
    else:
        env.pop("HARNESS_THIRD_REVIEW_CLAUDE_CODE_BIN", None)
        env["PATH"] = f"{claude.parent}:{env['PATH']}"

    result = subprocess.run(
        ["sh", str(CLAUDE_CODE_PROVIDER), str(repo)],
        cwd=PROJECT_ROOT,
        env=env,
        input="review prompt\n",
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 127
    assert "vscode extension claude binary is forbidden" in result.stderr
    assert not (tmp_path / "cwd").exists()


def test_claude_code_provider_rejects_symlink_into_vscode_extension(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    vscode_claude = (
        tmp_path / ".vscode/extensions/anthropic.claude-code/bin/claude"
    )
    _write_fake_claude(vscode_claude)
    claude_link = tmp_path / "bin/claude"
    claude_link.parent.mkdir()
    claude_link.symlink_to(vscode_claude)
    env = _claude_provider_env(tmp_path)
    env["HARNESS_THIRD_REVIEW_CLAUDE_CODE_BIN"] = str(claude_link)

    result = subprocess.run(
        ["sh", str(CLAUDE_CODE_PROVIDER), str(repo)],
        cwd=PROJECT_ROOT,
        env=env,
        input="review prompt\n",
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 127
    assert "vscode extension claude binary is forbidden" in result.stderr
    assert not (tmp_path / "cwd").exists()

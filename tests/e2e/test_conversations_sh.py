"""宿主单测：conversations.sh 宿主编排脚本。

通过 PATH 中的 docker stub 提供受控 inspect / exec，执行真实 shell 分支；
不 import Runner 的解析函数。每个测试自建独立 inspect fixture，不共享固定文件。
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_SOURCE = REPO_ROOT / "tests" / "e2e" / "conversations.sh"
CONTAINER_NAME = "n-agent-n-agent-1"
BASE_URL_DEFAULT = "http://127.0.0.1:8201"
SECRET_NAMES = [
    "N_AGENT_E2E_BROWSER_PASSWORD",
    "N_AGENT_E2E_JUDGE_BASE_URL",
    "N_AGENT_E2E_JUDGE_API_KEY",
    "N_AGENT_E2E_JUDGE_MODEL",
]

DOCKER_STUB = r"""#!/usr/bin/env python3
import json
import os
import pathlib
import sys

log_path = os.environ.get("DOCKER_STUB_LOG", "")


def record(line):
    if log_path:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


argv = sys.argv[1:]
if not argv:
    sys.exit(2)

if argv[0] == "inspect":
    record("inspect")
    fixture = os.environ.get("DOCKER_STUB_INSPECT", "")
    data = json.loads(pathlib.Path(fixture).read_text(encoding="utf-8"))
    fmt = argv[-1] if len(argv) > 1 else ""
    if "{{json .Mounts}}" in fmt:
        print(json.dumps(data["Mounts"]))
    else:
        print(data["Name"])
    sys.exit(0)

if argv[0] == "exec":
    env_names = []
    rest = argv[1:]
    while rest and rest[0] == "-e":
        env_names.append(rest[1])
        rest = rest[2:]
    if not rest or rest[0].startswith("-"):
        record("exec-bad-flags " + json.dumps(rest))
        sys.exit(2)
    container = rest[0]
    command = rest[1:]
    record("exec " + json.dumps({"env": env_names, "container": container,
                                 "argv": command}))
    if command and command[0] == "python":
        sys.exit(int(os.environ.get("DOCKER_STUB_EXEC_RC", "0")))
    if command and command[0] == "test" and len(command) == 3:
        checks = json.loads(os.environ.get("DOCKER_STUB_CHECKS", "{}"))
        ok = checks.get(command[2], {}).get(command[1], False)
        sys.exit(0 if ok else 1)
    sys.exit(2)

sys.exit(2)
"""

RESTART_STUB = """#!/usr/bin/env bash
echo "restart" >> "$DOCKER_STUB_LOG"
"""


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _mount(source, destination, rw=True):
    return {
        "Type": "bind",
        "Source": str(source),
        "Destination": destination,
        "Mode": "rw" if rw else "ro",
        "RW": rw,
        "Propagation": "rprivate",
    }


class Sandbox:
    """每次测试独立构建：含空格仓库、docker stub、独立 inspect fixture。"""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.bin_dir = tmp_path / "stub bin"
        self.bin_dir.mkdir(parents=True)
        _write_executable(self.bin_dir / "docker", DOCKER_STUB)
        self.log = tmp_path / "docker-stub.log"
        self._fixture_seq = 0

    def make_repo(self, repo: Path) -> Path:
        (repo / "tests" / "e2e" / "conversations").mkdir(parents=True)
        shutil.copy2(SCRIPT_SOURCE, repo / "tests" / "e2e" / "conversations.sh")
        (repo / "docker").mkdir(parents=True)
        _write_executable(repo / "docker" / "restart.sh", RESTART_STUB)
        return repo

    def inspect_fixture(self, mounts) -> Path:
        self._fixture_seq += 1
        fixture = self.tmp_path / f"inspect-{self._fixture_seq}.json"
        fixture.write_text(json.dumps(
            {"Name": CONTAINER_NAME, "Mounts": list(mounts)}), encoding="utf-8")
        return fixture

    def run(self, repo: Path, args, *, inspect_file, checks=None,
            rebuilt="1", exec_rc="0", extra_env=None):
        env = {
            "PATH": f"{self.bin_dir}:/usr/bin:/bin",
            "DOCKER_STUB_LOG": str(self.log),
            "DOCKER_STUB_INSPECT": str(inspect_file),
            "DOCKER_STUB_CHECKS": json.dumps(checks or {}),
            "DOCKER_STUB_EXEC_RC": exec_rc,
        }
        if rebuilt is not None:
            env["N_AGENT_E2E_REBUILT"] = rebuilt
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(repo / "tests" / "e2e" / "conversations.sh"), *args],
            env=env, capture_output=True, text=True, timeout=60)

    def log_lines(self):
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()

    def exec_records(self):
        records = []
        for line in self.log_lines():
            if line.startswith("exec "):
                records.append(json.loads(line[len("exec "):]))
        return records

    def runner_execs(self):
        """最终 Runner 调用（argv[0]==python），过滤容器内 test 核验。"""
        return [r for r in self.exec_records()
                if r["argv"] and r["argv"][0] == "python"]


def _writable_checks(*container_paths):
    checks = {}
    for path in container_paths:
        checks[str(path)] = {"-d": True, "-r": True, "-w": True}
    return checks


@pytest.fixture
def sandbox(tmp_path):
    return Sandbox(tmp_path)


class TestContainerPathResolution:
    """宿主路径 -> 容器路径转换：组件包含、最长匹配、权限校验。"""

    def test_space_containing_repo_succeeds_with_argv_array(self, sandbox, tmp_path):
        code_root = tmp_path / "code root"
        repo = sandbox.make_repo(code_root / "github.com" / "niean" / "my repo")
        (repo / "locals" / "e2e-reports").mkdir(parents=True)
        inspect_file = sandbox.inspect_fixture([_mount(code_root, "/workspace-code")])
        container_repo = "/workspace-code/github.com/niean/my repo"
        checks = _writable_checks(
            container_repo, f"{container_repo}/locals/e2e-reports")

        result = sandbox.run(repo, [], inspect_file=inspect_file, checks=checks)

        assert result.returncode == 0, result.stderr
        execs = sandbox.runner_execs()
        assert len(execs) == 1
        argv = execs[0]["argv"]
        # 含空格路径必须作为独立 argv 元素存在（数组传递，非字符串拼接）
        assert argv[0] == "python"
        assert argv[1] == f"{container_repo}/tests/e2e/conversations_runner.py"
        assert argv[2:] == [
            "--dataset-dir", f"{container_repo}/tests/e2e/conversations",
            "--report-dir", f"{container_repo}/locals/e2e-reports",
            "--base-url", BASE_URL_DEFAULT,
        ]
        assert execs[0]["env"] == []
        assert execs[0]["container"] == CONTAINER_NAME

    def test_sibling_directory_prefix_rejected(self, sandbox, tmp_path):
        code_root = tmp_path / "code root"
        code_root.mkdir()
        # 兄弟目录共享字符串前缀，但不是组件包含关系
        repo = sandbox.make_repo(tmp_path / "code root-evil" / "my repo")
        (repo / "locals" / "e2e-reports").mkdir(parents=True)
        inspect_file = sandbox.inspect_fixture([_mount(code_root, "/workspace-code")])

        result = sandbox.run(repo, [], inspect_file=inspect_file)

        assert result.returncode != 0
        assert sandbox.exec_records() == []

    def test_symlink_escape_rejected(self, sandbox, tmp_path):
        code_root = tmp_path / "code root"
        code_root.mkdir()
        outside = sandbox.make_repo(tmp_path / "outside base" / "my repo")
        (outside / "locals" / "e2e-reports").mkdir(parents=True)
        # 挂载树内的符号链接指向挂载外的真实仓库
        link = code_root / "linked repo"
        link.symlink_to(outside)
        inspect_file = sandbox.inspect_fixture([_mount(code_root, "/workspace-code")])

        result = subprocess.run(
            ["bash", str(link / "tests" / "e2e" / "conversations.sh")],
            env={
                "PATH": f"{sandbox.bin_dir}:/usr/bin:/bin",
                "DOCKER_STUB_LOG": str(sandbox.log),
                "DOCKER_STUB_INSPECT": str(inspect_file),
                "DOCKER_STUB_CHECKS": "{}",
                "N_AGENT_E2E_REBUILT": "1",
            },
            capture_output=True, text=True, timeout=60)

        assert result.returncode != 0
        assert sandbox.exec_records() == []

    def test_no_mount_rejected(self, sandbox, tmp_path):
        repo = sandbox.make_repo(tmp_path / "code root" / "my repo")
        (repo / "locals" / "e2e-reports").mkdir(parents=True)
        inspect_file = sandbox.inspect_fixture(
            [_mount(tmp_path / "unrelated root", "/elsewhere")])

        result = sandbox.run(repo, [], inspect_file=inspect_file)

        assert result.returncode != 0
        assert sandbox.exec_records() == []

    def test_nested_mount_shadowing_longest_wins(self, sandbox, tmp_path):
        code_root = tmp_path / "code root"
        repo = sandbox.make_repo(code_root / "my repo")
        reports = repo / "locals" / "e2e-reports"
        reports.mkdir(parents=True)
        # 嵌套挂载：repo/locals 单独挂载，遮蔽外层 /workspace-code
        inspect_file = sandbox.inspect_fixture([
            _mount(code_root, "/workspace-code"),
            _mount(repo / "locals", "/app-repo-locals"),
        ])
        container_repo = "/workspace-code/my repo"
        checks = _writable_checks(container_repo, "/app-repo-locals/e2e-reports")

        result = sandbox.run(repo, [], inspect_file=inspect_file, checks=checks)

        assert result.returncode == 0, result.stderr
        argv = sandbox.runner_execs()[0]["argv"]
        assert argv[1] == f"{container_repo}/tests/e2e/conversations_runner.py"
        # 报告目录必须走嵌套挂载（最长匹配），不能默认 <container_repo>/locals
        assert argv[argv.index("--report-dir") + 1] == "/app-repo-locals/e2e-reports"

    def test_manifest_under_separate_mount(self, sandbox, tmp_path):
        code_root = tmp_path / "code root"
        repo = sandbox.make_repo(code_root / "my repo")
        # 清单在报告根内，但报告根自身是嵌套挂载（与仓库不同 Mount）
        manifest = repo / "locals" / "e2e-reports" / "run-1" / "manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{}", encoding="utf-8")
        inspect_file = sandbox.inspect_fixture([
            _mount(code_root, "/workspace-code", rw=False),
            _mount(repo / "locals" / "e2e-reports", "/mnt manifests", rw=False),
        ])
        checks = _writable_checks(
            "/workspace-code/my repo", "/mnt manifests/run-1/manifest.json")

        result = sandbox.run(
            repo, ["--cleanup-only", str(manifest)],
            inspect_file=inspect_file, checks=checks)

        assert result.returncode == 0, result.stderr
        argv = sandbox.runner_execs()[0]["argv"]
        assert argv == [
            "python",
            "/workspace-code/my repo/tests/e2e/conversations_runner.py",
            "--cleanup-only", "/mnt manifests/run-1/manifest.json",
        ]

    def test_readonly_report_mount_rejected(self, sandbox, tmp_path):
        code_root = tmp_path / "code root"
        repo = sandbox.make_repo(code_root / "my repo")
        (repo / "locals" / "e2e-reports").mkdir(parents=True)
        inspect_file = sandbox.inspect_fixture([
            _mount(code_root, "/workspace-code"),
            _mount(repo / "locals", "/app-repo-locals", rw=False),
        ])
        # 即便容器内 test -w 通过，挂载只读标志也必须拒绝
        checks = _writable_checks(
            "/workspace-code/my repo", "/app-repo-locals/e2e-reports")

        result = sandbox.run(repo, [], inspect_file=inspect_file, checks=checks)

        assert result.returncode != 0
        assert sandbox.exec_records() == []

    def test_missing_report_dir_created_on_host_first(self, sandbox, tmp_path):
        code_root = tmp_path / "code root"
        repo = sandbox.make_repo(code_root / "my repo")
        (repo / "locals").mkdir(parents=True)
        inspect_file = sandbox.inspect_fixture([_mount(code_root, "/workspace-code")])
        container_repo = "/workspace-code/my repo"
        checks = _writable_checks(
            container_repo, f"{container_repo}/locals/e2e-reports")

        result = sandbox.run(repo, [], inspect_file=inspect_file, checks=checks)

        assert result.returncode == 0, result.stderr
        assert (repo / "locals" / "e2e-reports").is_dir()


class TestShellArguments:
    """参数解析、互斥、restart 时序、密钥注入与退出码透传。"""

    def _ok_repo(self, sandbox, tmp_path):
        code_root = tmp_path / "code root"
        repo = sandbox.make_repo(code_root / "my repo")
        (repo / "locals" / "e2e-reports").mkdir(parents=True)
        inspect_file = sandbox.inspect_fixture([_mount(code_root, "/workspace-code")])
        container_repo = "/workspace-code/my repo"
        checks = _writable_checks(
            container_repo, f"{container_repo}/locals/e2e-reports")
        return repo, inspect_file, checks, container_repo

    def test_replay_forwards_only_keep_base_url(self, sandbox, tmp_path):
        repo, inspect_file, checks, container_repo = self._ok_repo(sandbox, tmp_path)

        result = sandbox.run(
            repo,
            ["--only", "cli-weather-chat,task-artifact-chat", "--keep",
             "--base-url", "http://127.0.0.1:9999"],
            inspect_file=inspect_file, checks=checks)

        assert result.returncode == 0, result.stderr
        argv = sandbox.runner_execs()[0]["argv"]
        # 精确断言：基础参数之后按出现顺序透传
        assert argv[:7] == [
            "python",
            f"{container_repo}/tests/e2e/conversations_runner.py",
            "--dataset-dir", f"{container_repo}/tests/e2e/conversations",
            "--report-dir", f"{container_repo}/locals/e2e-reports",
            "--base-url",
        ]
        tail = argv[argv.index(f"{container_repo}/locals/e2e-reports") + 1:]
        assert tail == [
            "--base-url", "http://127.0.0.1:9999",
            "--only", "cli-weather-chat,task-artifact-chat",
            "--keep",
        ]

    def test_replay_forwards_max_retries(self, sandbox, tmp_path):
        repo, inspect_file, checks, _ = self._ok_repo(sandbox, tmp_path)

        result = sandbox.run(repo, ["--max-retries", "0"],
                             inspect_file=inspect_file, checks=checks)

        assert result.returncode == 0, result.stderr
        argv = sandbox.runner_execs()[0]["argv"]
        assert argv[-2:] == ["--max-retries", "0"]

    def test_max_retries_non_numeric_exit_2(self, sandbox, tmp_path):
        repo, inspect_file, checks, _ = self._ok_repo(sandbox, tmp_path)

        result = sandbox.run(repo, ["--max-retries", "abc"],
                             inspect_file=inspect_file, checks=checks)

        assert result.returncode == 2
        assert sandbox.exec_records() == []

    def test_replay_forwards_parallel(self, sandbox, tmp_path):
        repo, inspect_file, checks, _ = self._ok_repo(sandbox, tmp_path)

        result = sandbox.run(repo, ["--parallel", "3"],
                             inspect_file=inspect_file, checks=checks)

        assert result.returncode == 0, result.stderr
        argv = sandbox.runner_execs()[0]["argv"]
        assert argv[-2:] == ["--parallel", "3"]

    def test_parallel_non_numeric_exit_2(self, sandbox, tmp_path):
        repo, inspect_file, checks, _ = self._ok_repo(sandbox, tmp_path)

        result = sandbox.run(repo, ["--parallel", "abc"],
                             inspect_file=inspect_file, checks=checks)

        assert result.returncode == 2
        assert sandbox.exec_records() == []

    def test_parallel_zero_exit_2(self, sandbox, tmp_path):
        repo, inspect_file, checks, _ = self._ok_repo(sandbox, tmp_path)

        result = sandbox.run(repo, ["--parallel", "0"],
                             inspect_file=inspect_file, checks=checks)

        assert result.returncode == 2
        assert sandbox.exec_records() == []

    def test_cleanup_only_missing_value_exit_2(self, sandbox, tmp_path):
        repo, inspect_file, checks, _ = self._ok_repo(sandbox, tmp_path)

        result = sandbox.run(repo, ["--cleanup-only"],
                             inspect_file=inspect_file, checks=checks)

        assert result.returncode == 2
        assert sandbox.log_lines() == []

    @pytest.mark.parametrize("extra", [["--only", "a"], ["--keep"],
                                       ["--base-url", "http://x:1"],
                                       ["--max-retries", "1"],
                                       ["--parallel", "2"]])
    def test_cleanup_only_conflicts_exit_2(self, sandbox, tmp_path, extra):
        repo, inspect_file, checks, _ = self._ok_repo(sandbox, tmp_path)
        manifest = repo / "locals" / "e2e-reports" / "run-1" / "manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{}", encoding="utf-8")

        result = sandbox.run(repo, ["--cleanup-only", str(manifest), *extra],
                             inspect_file=inspect_file, checks=checks)

        assert result.returncode == 2
        assert sandbox.exec_records() == []

    def test_unknown_arg_exit_2(self, sandbox, tmp_path):
        repo, inspect_file, checks, _ = self._ok_repo(sandbox, tmp_path)

        result = sandbox.run(repo, ["--bogus"],
                             inspect_file=inspect_file, checks=checks)

        assert result.returncode == 2
        assert sandbox.log_lines() == []

    def test_cleanup_only_never_restarts_and_passthrough_rc(self, sandbox, tmp_path):
        repo, inspect_file, checks, container_repo = self._ok_repo(sandbox, tmp_path)
        manifest = repo / "locals" / "e2e-reports" / "run-1" / "manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{}", encoding="utf-8")
        checks.update(_writable_checks(
            f"{container_repo}/locals/e2e-reports/run-1/manifest.json"))
        # 密钥文件存在也不能在 cleanup-only 下加载（分流在密钥加载之前）
        (repo / "locals" / "e2e-secrets.env").write_text(
            "N_AGENT_E2E_JUDGE_API_KEY=top-secret-value\n", encoding="utf-8")

        result = sandbox.run(
            repo, ["--cleanup-only", str(manifest)],
            inspect_file=inspect_file, checks=checks,
            rebuilt=None, exec_rc="3")

        assert result.returncode == 3
        lines = sandbox.log_lines()
        assert "restart" not in lines
        execs = sandbox.runner_execs()
        assert len(execs) == 1
        assert execs[0]["argv"] == [
            "python",
            f"{container_repo}/tests/e2e/conversations_runner.py",
            "--cleanup-only",
            f"{container_repo}/locals/e2e-reports/run-1/manifest.json",
        ]
        assert execs[0]["env"] == []
        assert "top-secret-value" not in "\n".join(lines)

    def test_restart_runs_before_inspect_when_not_rebuilt(self, sandbox, tmp_path):
        repo, inspect_file, checks, _ = self._ok_repo(sandbox, tmp_path)

        result = sandbox.run(repo, [], inspect_file=inspect_file, checks=checks,
                             rebuilt=None)

        assert result.returncode == 0, result.stderr
        lines = sandbox.log_lines()
        assert lines[0] == "restart"
        assert lines[1] == "inspect"
        assert any(line.startswith("exec ") for line in lines[2:])

    def test_restart_skipped_when_rebuilt(self, sandbox, tmp_path):
        repo, inspect_file, checks, _ = self._ok_repo(sandbox, tmp_path)

        result = sandbox.run(repo, [], inspect_file=inspect_file, checks=checks,
                             rebuilt="1")

        assert result.returncode == 0, result.stderr
        assert "restart" not in sandbox.log_lines()
        assert "inspect" in sandbox.log_lines()

    def test_secrets_file_exports_names_only(self, sandbox, tmp_path):
        repo, inspect_file, checks, _ = self._ok_repo(sandbox, tmp_path)
        (repo / "locals" / "e2e-secrets.env").write_text(
            "\n".join([
                "N_AGENT_E2E_BROWSER_PASSWORD=top-secret-value",
                "N_AGENT_E2E_JUDGE_BASE_URL=http://judge.internal",
                "N_AGENT_E2E_JUDGE_API_KEY=another-secret",
                "N_AGENT_E2E_JUDGE_MODEL=judge-model-1",
                "UNRELATED_VAR=must-not-leak",
            ]) + "\n",
            encoding="utf-8")

        result = sandbox.run(repo, [], inspect_file=inspect_file, checks=checks)

        assert result.returncode == 0, result.stderr
        execs = sandbox.runner_execs()
        assert len(execs) == 1
        assert execs[0]["env"] == SECRET_NAMES
        log_text = "\n".join(sandbox.log_lines())
        assert "top-secret-value" not in log_text
        assert "another-secret" not in log_text
        assert "UNRELATED_VAR" not in log_text

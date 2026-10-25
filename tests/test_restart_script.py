import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
RESTART_SCRIPT = ROOT / "docker" / "restart.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _make_fake_restart_env(tmp_path):
    """Fake docker/curl/sleep/jq on PATH; returns (env, command_log)."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    stale_marker = tmp_path / "stale-container"
    stale_marker.touch()
    command_log = tmp_path / "docker-commands.log"

    _write_executable(
        fake_bin / "docker",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_DOCKER_COMMAND_LOG"

if [[ "$*" == "compose config --format json" ]]; then
  # host/terminal-host.py resolves the compose config before start/check;
  # answer with host terminal disabled so the bridge is a no-op here.
  printf '%s\n' '{"services":{"n-agent":{"environment":{"N_AGENT_HOST_TERMINAL_ENABLED":"false"},"volumes":[]}}}'
  exit 0
fi
if [[ "$*" == "compose down "* ]]; then
  exit 0
fi
if [[ "$*" == "compose rm "* ]]; then
  rm -f "$FAKE_DOCKER_STALE_MARKER"
  exit 0
fi
if [[ "$*" == "compose up "* ]]; then
  if [[ -e "$FAKE_DOCKER_STALE_MARKER" ]]; then
    echo 'Conflict. The container name "/n-agent-browser-1" is already in use' >&2
    exit 1
  fi
  exit 0
fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "curl",
        "#!/usr/bin/env bash\nprintf '{}\\n'\n",
    )
    _write_executable(fake_bin / "sleep", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(fake_bin / "jq", "#!/usr/bin/env bash\ncat\n")

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "FAKE_DOCKER_COMMAND_LOG": str(command_log),
            "FAKE_DOCKER_STALE_MARKER": str(stale_marker),
        }
    )
    return env, command_log


def _run_restart_script(tmp_path, extra_env):
    env, command_log = _make_fake_restart_env(tmp_path)
    env.update(extra_env)
    completed = subprocess.run(
        ["bash", str(RESTART_SCRIPT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return command_log.read_text(encoding="utf-8").splitlines()


def test_restart_removes_stale_compose_containers_before_up(tmp_path):
    commands = _run_restart_script(tmp_path, {})
    down_index = next(i for i, command in enumerate(commands) if command.startswith("compose down "))
    rm_index = next(i for i, command in enumerate(commands) if command.startswith("compose rm "))
    up_index = next(i for i, command in enumerate(commands) if command.startswith("compose up "))
    assert down_index < rm_index < up_index


@pytest.mark.parametrize(
    "stop_timeout, expected_timeout",
    [
        # 默认保持 1 秒（既有重建规则不受观测验收影响）
        (None, "1"),
        # 观测验收环境显式 10 秒，须贯穿到 compose down --timeout
        ("10", "10"),
    ],
)
def test_restart_propagates_compose_stop_timeout(tmp_path, stop_timeout, expected_timeout):
    """N_AGENT_COMPOSE_STOP_TIMEOUT 默认 1s 且逐值传入 compose down --timeout。"""
    extra_env = (
        {}
        if stop_timeout is None
        else {"N_AGENT_COMPOSE_STOP_TIMEOUT": stop_timeout}
    )
    if stop_timeout is None:
        os.environ.pop("N_AGENT_COMPOSE_STOP_TIMEOUT", None)
    commands = _run_restart_script(tmp_path, extra_env)
    down_command = next(
        command for command in commands if command.startswith("compose down ")
    )
    assert down_command == f"compose down --timeout {expected_timeout}"

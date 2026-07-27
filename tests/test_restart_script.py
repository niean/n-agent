import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
RESTART_SCRIPT = ROOT / "docker" / "restart.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_restart_removes_stale_compose_containers_before_up(tmp_path):
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
    completed = subprocess.run(
        ["bash", str(RESTART_SCRIPT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    commands = command_log.read_text(encoding="utf-8").splitlines()
    down_index = next(i for i, command in enumerate(commands) if command.startswith("compose down "))
    rm_index = next(i for i, command in enumerate(commands) if command.startswith("compose rm "))
    up_index = next(i for i, command in enumerate(commands) if command.startswith("compose up "))
    assert down_index < rm_index < up_index

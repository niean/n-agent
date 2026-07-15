from __future__ import annotations

import os
from pathlib import Path
import threading

import pytest

from app.infrastructure.host_terminal.policy_loader import (
    HostTerminalPolicyLoadError,
    HostTerminalPolicyLoader,
)


def _policy(version: str = "v1", *, extra: str = "") -> str:
    return f"""schema_version: 1
version: {version}
limits:
  default_timeout_seconds: 2
  max_timeout_seconds: 5
  max_stdout_bytes: 1024
  max_stderr_bytes: 1024
  max_args: 2
  max_arg_length: 32
  max_total_args_length: 64
  max_concurrency: 1
targets:
  - type: command
    rule_id: echo
    executable: /bin/echo
    args:
      - exact: hello
{extra}"""


def _write(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o600)


def test_loads_strict_policy_and_immutable_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    _write(path, _policy())
    loader = HostTerminalPolicyLoader(path)
    snapshot = loader.load()
    assert snapshot.version == "v1"
    assert len(snapshot.content_digest) == 64
    assert isinstance(snapshot.command_rules, tuple)


@pytest.mark.parametrize(
    "content,reason",
    [
        (_policy().replace("schema_version: 1", "schema_version: 2"), "host_policy_version_unsupported"),
        (_policy(extra="unknown: true\n"), "host_policy_schema_invalid"),
        (_policy().replace("version: v1", "version: v1\nversion: v2"), "host_policy_duplicate_field"),
        (_policy().replace("rule_id: echo", "rule_id: echo\n    surprise: nope"), "host_policy_schema_invalid"),
        ("{broken", "host_policy_yaml_invalid"),
    ],
)
def test_rejects_unknown_version_fields_duplicates_and_yaml(
    tmp_path: Path, content: str, reason: str
) -> None:
    path = tmp_path / "policy.yaml"
    _write(path, content)
    loader = HostTerminalPolicyLoader(path)
    assert loader.refresh() is False
    assert loader.snapshot is None
    assert loader.last_error_code == reason
    with pytest.raises(HostTerminalPolicyLoadError) as exc:
        loader.load()
    assert str(exc.value) == reason
    assert str(path) not in str(exc.value)
    assert content not in str(exc.value)


def test_invalid_refresh_retains_last_good_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    _write(path, _policy("v1"))
    loader = HostTerminalPolicyLoader(path)
    first = loader.load()
    _write(path, "invalid: [")
    assert loader.refresh() is False
    assert loader.snapshot is first
    assert loader.last_error_code == "host_policy_yaml_invalid"


def test_valid_refresh_atomically_replaces_snapshot_for_concurrent_readers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "policy.yaml"
    _write(path, _policy("v1"))
    loader = HostTerminalPolicyLoader(path)
    loader.load()
    observed: set[tuple[str, str]] = set()
    stop = threading.Event()

    def read() -> None:
        while not stop.is_set():
            snapshot = loader.snapshot
            assert snapshot is not None
            observed.add((snapshot.version, snapshot.command_rules[0].rule_id))

    thread = threading.Thread(target=read)
    thread.start()
    _write(path, _policy("v2").replace("rule_id: echo", "rule_id: echo-v2"))
    assert loader.refresh() is True
    stop.set()
    thread.join()
    assert observed <= {("v1", "echo"), ("v2", "echo-v2")}
    assert loader.snapshot is not None and loader.snapshot.version == "v2"


def test_rejects_insecure_mode_symlink_and_non_regular_file(tmp_path: Path) -> None:
    real = tmp_path / "real.yaml"
    _write(real, _policy())
    real.chmod(0o644)
    loader = HostTerminalPolicyLoader(real)
    assert loader.refresh() is False
    assert loader.last_error_code == "host_policy_file_unsafe"

    real.chmod(0o600)
    link = tmp_path / "link.yaml"
    link.symlink_to(real)
    assert HostTerminalPolicyLoader(link).refresh() is False
    assert HostTerminalPolicyLoader(tmp_path).refresh() is False


def test_rejects_special_permission_bits_and_post_open_metadata_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "policy.yaml"
    _write(path, _policy())
    path.chmod(0o4600)
    assert HostTerminalPolicyLoader(path).refresh() is False

    path.chmod(0o600)
    real_fstat = os.fstat

    def mismatched(fd: int) -> os.stat_result:
        values = list(real_fstat(fd))
        values[1] += 1  # st_ino
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", mismatched)
    loader = HostTerminalPolicyLoader(path)
    assert loader.refresh() is False
    assert loader.last_error_code == "host_policy_file_unsafe"


def test_rejects_file_not_owned_by_current_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "policy.yaml"
    _write(path, _policy())
    monkeypatch.setattr(os, "geteuid", lambda: path.stat().st_uid + 1)
    loader = HostTerminalPolicyLoader(path)
    assert loader.refresh() is False
    assert loader.last_error_code == "host_policy_file_unsafe"


def test_rejects_oversized_policy_before_yaml_parse(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_bytes(b"#" * (1_048_576 + 1))
    path.chmod(0o600)
    loader = HostTerminalPolicyLoader(path)
    assert loader.refresh() is False
    assert loader.last_error_code == "host_policy_file_too_large"

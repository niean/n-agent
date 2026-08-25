from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_ROOT = PROJECT_ROOT.parent / "harness-tpl"
CONFIG_SCRIPT = PROJECT_ROOT / ".harness/framework/scripts/get-config.sh"


def _load_config(root: Path) -> dict[str, object]:
    with (root / ".harness/harness.json").open(encoding="utf-8") as handle:
        return json.load(handle)


def test_project_harness_config_uses_runtime_values() -> None:
    config = _load_config(PROJECT_ROOT)

    assert config == {
        "version": 1,
        "thirdReview": {
            "enabled": True,
            "provider": "codex",
            "model": None,
            "timeoutSeconds": 900,
        },
        "hooks": {"afterFinish": {"enabled": True}},
    }


def test_template_harness_config_is_valid_and_disabled() -> None:
    config = _load_config(TEMPLATE_ROOT)

    assert config == {
        "version": 1,
        "thirdReview": {
            "enabled": False,
            "provider": "{{三方审阅 Provider 名称，已提供codex}}",
            "model": None,
            "timeoutSeconds": 900,
        },
        "hooks": {"afterFinish": {"enabled": False}},
    }


def test_harness_config_is_registered_in_framework_and_template_scan() -> None:
    framework = (PROJECT_ROOT / ".harness/framework/FRAMEWORK.md").read_text(
        encoding="utf-8"
    )
    project = (PROJECT_ROOT / ".harness/PROJECT.md").read_text(encoding="utf-8")
    extract_skill = (
        PROJECT_ROOT
        / ".harness/framework/skills/harness-ops/extract-harness-tpl/SKILL.md"
    ).read_text(encoding="utf-8")
    scan_reference = (
        PROJECT_ROOT
        / ".harness/framework/skills/harness-ops/extract-harness-tpl/references/scan-harness-tpl.md"
    ).read_text(encoding="utf-8")

    assert "Harness 运行配置" in framework
    assert "harness.json         -- Harness 机器可读运行配置" in project
    assert "`.harness/harness.json`" in extract_skill
    assert "`.harness/harness.json`" in scan_reference
    assert "scripts/get-config.sh" in framework


def test_template_uses_the_same_generic_config_script() -> None:
    template_script = TEMPLATE_ROOT / ".harness/framework/scripts/get-config.sh"

    assert template_script.read_text(encoding="utf-8") == CONFIG_SCRIPT.read_text(
        encoding="utf-8"
    )


def _run_config(
    root: Path,
    key: str,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", ".harness/framework/scripts/get-config.sh", key],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def _build_config_repo(tmp_path: Path, config: str | None) -> Path:
    root = tmp_path / "repo"
    script = root / ".harness/framework/scripts/get-config.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(CONFIG_SCRIPT, script)
    if config is not None:
        config_file = root / ".harness/harness.json"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(config, encoding="utf-8")
    return root


def test_config_script_reads_project_values() -> None:
    assert _run_config(PROJECT_ROOT, "thirdReview.enabled").stdout == "true\n"
    assert _run_config(PROJECT_ROOT, "thirdReview.provider").stdout == "codex\n"
    assert _run_config(PROJECT_ROOT, "thirdReview.model").stdout == "\n"
    assert _run_config(PROJECT_ROOT, "thirdReview.timeoutSeconds").stdout == "900\n"
    assert _run_config(PROJECT_ROOT, "hooks.afterFinish.enabled").stdout == "true\n"


def test_config_script_uses_defaults_when_file_is_missing(tmp_path: Path) -> None:
    root = _build_config_repo(tmp_path, None)

    assert _run_config(root, "thirdReview.enabled").stdout == "false\n"
    assert _run_config(root, "thirdReview.provider").stdout == "\n"
    assert _run_config(root, "thirdReview.model").stdout == "\n"
    assert _run_config(root, "thirdReview.timeoutSeconds").stdout == "900\n"
    assert _run_config(root, "hooks.afterFinish.enabled").stdout == "false\n"


def test_config_script_deep_merges_partial_config(tmp_path: Path) -> None:
    root = _build_config_repo(
        tmp_path,
        '{"version": 1, "thirdReview": {"provider": "codex"}}\n',
    )

    assert _run_config(root, "thirdReview.provider").stdout == "codex\n"
    assert _run_config(root, "thirdReview.enabled").stdout == "false\n"
    assert _run_config(root, "hooks.afterFinish.enabled").stdout == "false\n"


def test_config_script_rejects_invalid_json_and_types(tmp_path: Path) -> None:
    broken = _build_config_repo(tmp_path / "broken", "{not-json}\n")
    wrong_type = _build_config_repo(
        tmp_path / "wrong-type",
        '{"version": 1, "thirdReview": {"enabled": "true"}}\n',
    )

    broken_result = _run_config(broken, "thirdReview.enabled")
    type_result = _run_config(wrong_type, "thirdReview.enabled")

    assert broken_result.returncode == 65
    assert "invalid JSON" in broken_result.stderr
    assert type_result.returncode == 65
    assert "thirdReview.enabled must be boolean" in type_result.stderr


def test_config_script_rejects_unknown_key(tmp_path: Path) -> None:
    root = _build_config_repo(tmp_path, None)

    result = _run_config(root, "unknown.key")

    assert result.returncode == 64
    assert "unsupported config key" in result.stderr


@pytest.mark.parametrize("parser", ["jq", "python3", "node"])
def test_config_script_has_equivalent_supported_parser_adapters(
    parser: str,
    tmp_path: Path,
) -> None:
    if shutil.which(parser) is None:
        pytest.skip(f"{parser} is not installed")

    expected = {
        "thirdReview.enabled": "true\n",
        "thirdReview.provider": "codex\n",
        "thirdReview.model": "\n",
        "thirdReview.timeoutSeconds": "900\n",
        "hooks.afterFinish.enabled": "true\n",
    }
    for key, output in expected.items():
        result = _run_config(
            PROJECT_ROOT,
            key,
            env={"HARNESS_CONFIG_PARSER": parser},
        )
        assert result.returncode == 0
        assert result.stdout == output
        assert result.stderr == ""

    invalid = _build_config_repo(
        tmp_path / parser,
        '{"version": 1, "hooks": {"afterFinish": {"enabled": "true"}}}\n',
    )
    invalid_result = _run_config(
        invalid,
        "hooks.afterFinish.enabled",
        env={"HARNESS_CONFIG_PARSER": parser},
    )
    assert invalid_result.returncode == 65
    assert "hooks.afterFinish.enabled must be boolean" in invalid_result.stderr


def test_config_script_fails_explicitly_without_a_json_parser(tmp_path: Path) -> None:
    root = _build_config_repo(tmp_path, '{"version": 1}\n')

    result = _run_config(root, "thirdReview.enabled", env={"PATH": ""})

    assert result.returncode == 127
    assert "no supported JSON parser found" in result.stderr

"""T2：数据集离线转换 CLI 子命令测试（evaluate convert-e2e）。

覆盖 plan T2：
- CLI 只加载转换依赖，不初始化业务服务（build_evaluation_services 不被触发）；
- 子命令经 app/interfaces/cli/main.py 既有 commands 注册方式接线；
- 产物：manifest.json（通过 T12 权威加载回读）、素材文件、coverage.json。
"""
from __future__ import annotations

import json
from pathlib import Path

from app.interfaces.cli.commands import evaluate as evaluate_cmd
from app.interfaces.cli import main as _main_entry  # noqa: F401（包 re-export 的函数）
import importlib

cli_main = importlib.import_module("app.interfaces.cli.main")

ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = ROOT / "tests/e2e/conversations"


def _argv(out: Path, *extra: str) -> list[str]:
    return [
        "evaluate", "convert-e2e",
        "--source-dir", str(SOURCE_DIR),
        "--out", str(out),
        *extra,
    ]


def test_convert_e2e_subcommand_registered():
    args = cli_main.build_parser(plugin_commands=()).parse_args(
        _argv("unused")
    )
    assert args.evaluate_command == "convert-e2e"
    assert args.source_dir == str(SOURCE_DIR)


def test_convert_e2e_writes_manifest_and_coverage(tmp_path):
    out = tmp_path / "converted"
    exit_code = cli_main.main(_argv(out, "--dataset-version", "1"))
    assert exit_code == 0
    manifest_path = out / "manifest.json"
    coverage_path = out / "coverage.json"
    assert manifest_path.is_file()
    assert coverage_path.is_file()

    from app.application.evaluation_dataset import load_dataset_manifest

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "2"
    assert len(manifest["cases"]) == 12
    loaded = load_dataset_manifest(out / "dataset" / "agent-e2e-scenarios" / "1")
    assert loaded.dataset_id == "agent-e2e-scenarios"
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    assert coverage["summary"]["converted"] == 12


def test_convert_e2e_does_not_initialize_business_services(tmp_path, monkeypatch):
    def _boom():
        raise AssertionError("离线转换不得初始化业务服务")

    monkeypatch.setattr(evaluate_cmd, "_load_evaluation_runtime", _boom)
    args = cli_main.build_parser(plugin_commands=()).parse_args(_argv(tmp_path / "out2"))
    exit_code = evaluate_cmd.run(args)
    assert exit_code == 0


def test_convert_e2e_rejects_bad_source_dir(tmp_path):
    exit_code = cli_main.main(
        _argv(tmp_path / "out3", "--source-dir", str(tmp_path / "missing"))
    )
    assert exit_code == 2

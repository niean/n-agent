from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from app.domain.config_bundle import (
    BUNDLE_SCHEMA_VERSION,
    BundleSource,
    ConfigBundle,
    ImportAction,
    ImportItemReport,
    ImportOutcome,
    ImportReport,
)
from app.interfaces.cli.commands import config as config_cmd
from app.interfaces.cli.main import build_parser


def _make_settings():
    return SimpleNamespace(
        provider_base_url="http://x",
        provider_api_key="sk-secret",
        provider_model="m",
        sqlite_path="/tmp/x.db",
        workspace_root="/tmp",
        agent_iteration_limit=10,
        kb_enabled=False,
        kb_base_url="",
        mcp_connect_timeout_seconds=10,
        sandbox_enabled=False,
        sandbox_type="docker",
        feishu_app_id="",
        feishu_app_secret="feishu-secret",
        web_fetch_enabled=True,
    )


def _args(**kw):
    base = {
        "json": False,
        "form": False,
        "yaml": False,
        "section": None,
        "config_command": None,
        "format": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _bundle_payload(api_key: str | None = "sk-live") -> dict:
    """A complete, structurally valid bundle document (all ten sections)."""
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "created_at": "2026-09-10T00:00:00+00:00",
        "source": {
            "hostname": "source-host",
            "install_root": "/opt/n-agent",
            "code_root": "/opt/code",
        },
        "redacted": api_key is None,
        "sections": {
            "providers": [
                {
                    "name": "primary",
                    "provider_type": "openai-compatible",
                    "base_url": "https://api.example.com",
                    "model": "gpt-x",
                    "api_key": api_key,
                    "extra_headers": None,
                    "supports_vision": False,
                    "is_active": True,
                }
            ],
            "knowledge_bases": [],
            "mcp_sites": [],
            "external_memory_providers": [],
            "external_memory_global_config": None,
            "plugins": [],
            "skills": [],
            "scheduled_tasks": [],
            "gateway_home_targets": [],
            "task_config": None,
        },
    }


class _FakeBundleService:
    """Stands in for ConfigBundleService; both entry points are async, as on the
    real port. Records every import call so the CLI seam can be asserted."""

    def __init__(self, exit_code: int = 0):
        self.import_calls: list[dict] = []
        self.export_calls: list[dict] = []
        self._exit_code = exit_code

    async def export_bundle(self, *, redact_secrets: bool, source=None) -> ConfigBundle:
        self.export_calls.append({"redact_secrets": redact_secrets, "source": source})
        payload = _bundle_payload(api_key=None if redact_secrets else "sk-live")
        return ConfigBundle(
            schema_version=payload["schema_version"],
            created_at=payload["created_at"],
            source=source or BundleSource(hostname="", install_root="", code_root=""),
            redacted=redact_secrets,
            sections=payload["sections"],
        )

    async def import_bundle(
        self, bundle, *, mode, with_schedules: bool, dry_run: bool, pre_existing=None
    ) -> ImportReport:
        self.import_calls.append(
            {
                "bundle": bundle,
                "mode": mode,
                "with_schedules": with_schedules,
                "dry_run": dry_run,
                "pre_existing": pre_existing,
            }
        )
        items = [
            ImportItemReport(
                section="providers",
                natural_key="primary",
                outcome=ImportOutcome.CREATED,
                action=ImportAction.CREATED,
                reasons=[],
            )
        ]
        if self._exit_code:
            items.append(
                ImportItemReport(
                    section="knowledge_bases",
                    natural_key="kb",
                    outcome=ImportOutcome.FAILED,
                    action=ImportAction.NONE,
                    reasons=["boom"],
                )
            )
        return ImportReport(mode=mode, items=items)


def test_config_shows_provider_api_key_present_only(monkeypatch, capsys):
    monkeypatch.setattr(config_cmd, "_load_settings", lambda: _make_settings())
    rc = config_cmd.run(_args(json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["provider_api_key_present"] is True
    assert "sk-secret" not in json.dumps(data)
    assert "provider_api_key" not in data or data.get("provider_api_key") in (None, "")


def test_config_section_filter(monkeypatch, capsys):
    monkeypatch.setattr(config_cmd, "_load_settings", lambda: _make_settings())
    rc = config_cmd.run(_args(section="provider", json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert "provider_base_url" in data
    assert "provider_model" in data
    assert "sqlite_path" not in data


def test_config_json_output(monkeypatch, capsys):
    monkeypatch.setattr(config_cmd, "_load_settings", lambda: _make_settings())
    rc = config_cmd.run(_args(json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert "provider_api_key_present" in data
    assert "sk-secret" not in json.dumps(data)


def test_config_feishu_secret_redacted(monkeypatch, capsys):
    monkeypatch.setattr(config_cmd, "_load_settings", lambda: _make_settings())
    rc = config_cmd.run(_args(json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["feishu_app_secret_present"] is True
    assert "feishu-secret" not in json.dumps(data)


def test_config_table_output(monkeypatch, capsys):
    monkeypatch.setattr(config_cmd, "_load_settings", lambda: _make_settings())
    rc = config_cmd.run(_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "provider_base_url" in out
    assert "sk-secret" not in out


# ---------------------------------------------------------------------------
# T5: config export / config import
# ---------------------------------------------------------------------------


def test_bare_config_still_shows_redacted_settings(monkeypatch, capsys):
    monkeypatch.setattr(config_cmd, "_load_settings", lambda: _make_settings())
    assert config_cmd.run(_args(json=True, config_command=None)) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["provider_api_key_present"] is True
    assert "sk-secret" not in json.dumps(data)


def test_export_stdout_emits_single_json_document(monkeypatch, capsys):
    monkeypatch.setattr(config_cmd, "_load_config_bundle_service", lambda **kwargs: _FakeBundleService())
    rc = config_cmd.run(_args(config_command="export", stdout=True, out=None, redact_secrets=False))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert set(payload) == {"schema_version", "created_at", "source", "redacted", "sections"}
    assert len(payload["sections"]) == 10


def test_export_rejects_both_stdout_and_out():
    parser = build_parser(plugin_commands=[])
    with pytest.raises(SystemExit):
        parser.parse_args(["config", "export", "--stdout", "--out", "/tmp/a.json"])


def test_export_requires_an_output_destination():
    parser = build_parser(plugin_commands=[])
    with pytest.raises(SystemExit):
        parser.parse_args(["config", "export"])


def test_bare_config_still_parses_without_a_subcommand():
    parser = build_parser(plugin_commands=[])
    args = parser.parse_args(["config", "--section", "provider"])
    assert getattr(args, "config_command", None) is None
    assert args.section == "provider"


def test_import_rejects_invalid_mode_before_any_write(monkeypatch):
    svc = _FakeBundleService()
    monkeypatch.setattr(config_cmd, "_load_config_bundle_service", lambda **kwargs: svc)
    with pytest.raises(SystemExit):
        build_parser(plugin_commands=[]).parse_args(["config", "import", "--stdin", "--mode", "bogus"])
    assert svc.import_calls == []


def test_import_requires_an_input_source():
    parser = build_parser(plugin_commands=[])
    with pytest.raises(SystemExit):
        parser.parse_args(["config", "import"])


def test_import_rejects_both_stdin_and_file():
    parser = build_parser(plugin_commands=[])
    with pytest.raises(SystemExit):
        parser.parse_args(["config", "import", "--stdin", "--file", "/tmp/a.json"])


def test_export_out_file_is_written_0600(tmp_path, monkeypatch):
    monkeypatch.setattr(config_cmd, "_load_config_bundle_service", lambda **kwargs: _FakeBundleService())
    out = tmp_path / "bundle.json"
    assert config_cmd.run(_args(config_command="export", stdout=False, out=str(out),
                                redact_secrets=False)) == 0
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    assert json.loads(out.read_text(encoding="utf-8"))["schema_version"] == 1


def test_export_refuses_to_overwrite_existing_out_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config_cmd, "_load_config_bundle_service", lambda **kwargs: _FakeBundleService())
    out = tmp_path / "bundle.json"
    out.write_text("existing", encoding="utf-8")
    assert config_cmd.run(_args(config_command="export", stdout=False, out=str(out),
                                redact_secrets=False)) != 0
    assert out.read_text(encoding="utf-8") == "existing"


def test_export_out_mode_keeps_stdout_free_of_the_bundle(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config_cmd, "_load_config_bundle_service", lambda **kwargs: _FakeBundleService())
    out = tmp_path / "bundle.json"
    config_cmd.run(_args(config_command="export", stdout=False, out=str(out), redact_secrets=False))
    captured = capsys.readouterr()
    assert "sk-live" not in captured.out
    assert "sk-live" not in captured.err


def test_export_diagnostics_go_to_stderr_when_stdout_mode(monkeypatch, capsys):
    monkeypatch.setattr(config_cmd, "_load_config_bundle_service", lambda **kwargs: _FakeBundleService())
    config_cmd.run(_args(config_command="export", stdout=True, out=None, redact_secrets=True))
    captured = capsys.readouterr()
    json.loads(captured.out)
    assert captured.err


def test_export_uses_the_read_only_assembly(monkeypatch, capsys):
    seen: list[dict] = []

    def _factory(**kwargs):
        seen.append(kwargs)
        return _FakeBundleService()

    monkeypatch.setattr(config_cmd, "_load_config_bundle_service", _factory)
    config_cmd.run(_args(config_command="export", stdout=True, out=None, redact_secrets=False))
    capsys.readouterr()
    assert seen == [{"read_only": True}]


def test_export_source_metadata_comes_from_the_deployment_roots(monkeypatch, capsys):
    svc = _FakeBundleService()
    monkeypatch.setattr(config_cmd, "_load_config_bundle_service", lambda **kwargs: svc)
    monkeypatch.setenv("N_AGENT_INSTALL_ROOT", "/opt/install")
    monkeypatch.setenv("N_AGENT_CODE_ROOT", "/opt/code")
    config_cmd.run(_args(config_command="export", stdout=True, out=None, redact_secrets=False))
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"]["install_root"] == "/opt/install"
    assert payload["source"]["code_root"] == "/opt/code"
    assert payload["source"]["hostname"]


def test_import_reads_stdin_and_returns_report_exit_code(monkeypatch, capsys):
    svc = _FakeBundleService(exit_code=1)
    monkeypatch.setattr(config_cmd, "_load_config_bundle_service", lambda **kwargs: svc)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_bundle_payload())))
    assert config_cmd.run(_args(config_command="import", stdin=True, file=None,
                                mode="merge", with_schedules=False, dry_run=False)) == 1


def test_import_reads_a_file(tmp_path, monkeypatch, capsys):
    svc = _FakeBundleService()
    monkeypatch.setattr(config_cmd, "_load_config_bundle_service", lambda **kwargs: svc)
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(_bundle_payload()), encoding="utf-8")
    assert config_cmd.run(_args(config_command="import", stdin=False, file=str(path),
                                mode="overwrite", with_schedules=True, dry_run=False)) == 0
    call = svc.import_calls[0]
    assert str(call["mode"]) == "overwrite"
    assert call["with_schedules"] is True
    assert call["bundle"].sections["providers"][0]["name"] == "primary"


def test_import_report_is_redacted(monkeypatch, capsys):
    svc = _FakeBundleService()
    monkeypatch.setattr(config_cmd, "_load_config_bundle_service", lambda **kwargs: svc)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_bundle_payload(api_key="sk-live"))))
    config_cmd.run(_args(config_command="import", stdin=True, file=None,
                         mode="merge", with_schedules=False, dry_run=False))
    captured = capsys.readouterr()
    assert "sk-live" not in captured.out
    assert "sk-live" not in captured.err


def test_import_dry_run_uses_the_read_only_assembly(monkeypatch, capsys):
    seen: list[dict] = []

    def _factory(**kwargs):
        seen.append(kwargs)
        return _FakeBundleService()

    monkeypatch.setattr(config_cmd, "_load_config_bundle_service", _factory)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_bundle_payload())))
    config_cmd.run(_args(config_command="import", stdin=True, file=None, mode="merge",
                         with_schedules=False, dry_run=True))
    capsys.readouterr()
    assert seen == [{"read_only": True}]


def test_real_import_uses_the_write_assembly(monkeypatch, capsys):
    seen: list[dict] = []

    def _factory(**kwargs):
        seen.append(kwargs)
        return _FakeBundleService()

    monkeypatch.setattr(config_cmd, "_load_config_bundle_service", _factory)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_bundle_payload())))
    config_cmd.run(_args(config_command="import", stdin=True, file=None, mode="merge",
                         with_schedules=False, dry_run=False))
    capsys.readouterr()
    assert seen == [{"read_only": False}]


def test_import_passes_pre_existing_context_through(monkeypatch, capsys):
    svc = _FakeBundleService()
    monkeypatch.setattr(config_cmd, "_load_config_bundle_service", lambda **kwargs: svc)
    context = {"skills": ["a"], "plugins": ["p"]}
    payload = {"bundle": _bundle_payload(), "pre_existing": context}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    config_cmd.run(_args(config_command="import", stdin=True, file=None, mode="merge",
                         with_schedules=False, dry_run=False, internal_context=True))
    capsys.readouterr()
    assert svc.import_calls[0]["pre_existing"] == context


def test_import_without_internal_context_defaults_pre_existing_to_none(monkeypatch, capsys):
    svc = _FakeBundleService()
    monkeypatch.setattr(config_cmd, "_load_config_bundle_service", lambda **kwargs: svc)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_bundle_payload())))
    config_cmd.run(_args(config_command="import", stdin=True, file=None, mode="merge",
                         with_schedules=False, dry_run=False))
    capsys.readouterr()
    assert svc.import_calls[0]["pre_existing"] is None


def test_internal_context_requires_stdin(tmp_path, monkeypatch, capsys):
    svc = _FakeBundleService()
    monkeypatch.setattr(config_cmd, "_load_config_bundle_service", lambda **kwargs: svc)
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(_bundle_payload()), encoding="utf-8")
    rc = config_cmd.run(_args(config_command="import", stdin=False, file=str(path), mode="merge",
                              with_schedules=False, dry_run=False, internal_context=True))
    capsys.readouterr()
    assert rc == 2
    assert svc.import_calls == []


def test_internal_context_rejects_a_malformed_envelope(monkeypatch, capsys):
    svc = _FakeBundleService()
    monkeypatch.setattr(config_cmd, "_load_config_bundle_service", lambda **kwargs: svc)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_bundle_payload())))
    rc = config_cmd.run(_args(config_command="import", stdin=True, file=None, mode="merge",
                              with_schedules=False, dry_run=False, internal_context=True))
    capsys.readouterr()
    assert rc == 2
    assert svc.import_calls == []


def test_internal_context_rejects_unknown_natural_key_sections(monkeypatch, capsys):
    svc = _FakeBundleService()
    monkeypatch.setattr(config_cmd, "_load_config_bundle_service", lambda **kwargs: svc)
    payload = {"bundle": _bundle_payload(), "pre_existing": {"nope": ["a"]}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = config_cmd.run(_args(config_command="import", stdin=True, file=None, mode="merge",
                              with_schedules=False, dry_run=False, internal_context=True))
    capsys.readouterr()
    assert rc == 2
    assert svc.import_calls == []


def test_import_rejects_malformed_json(monkeypatch, capsys):
    svc = _FakeBundleService()
    monkeypatch.setattr(config_cmd, "_load_config_bundle_service", lambda **kwargs: svc)
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    rc = config_cmd.run(_args(config_command="import", stdin=True, file=None, mode="merge",
                              with_schedules=False, dry_run=False))
    capsys.readouterr()
    assert rc == 2
    assert svc.import_calls == []


def test_import_json_report_shape(capsys, monkeypatch):
    monkeypatch.setattr(config_cmd, "_load_config_bundle_service", lambda **kwargs: _FakeBundleService())
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_bundle_payload())))
    rc = config_cmd.run(_args(config_command="import", stdin=True, file=None,
                              mode="merge", with_schedules=False, dry_run=False, json=True))
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert out.count("\n") == 1 and out.endswith("\n")
    assert doc["mode"] == "merge" and doc["exit_code"] == rc
    assert set(doc["counts"]) == {"created", "updated", "skipped", "degraded", "failed"}
    for item in doc["items"]:
        assert set(item) == {"section", "natural_key", "outcome", "action", "reasons"}
        assert item["action"] in {"created", "updated", "none"}
        assert isinstance(item["outcome"], str)
    assert "sk-live" not in out


@pytest.mark.parametrize("argv_tail", [["--json"], ["--format", "json"]])
def test_import_json_shaped_formats_share_the_stdout_contract(argv_tail, capsys, monkeypatch):
    monkeypatch.setattr(config_cmd, "_load_config_bundle_service", lambda **kwargs: _FakeBundleService())
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_bundle_payload())))
    args = build_parser(plugin_commands=[]).parse_args(
        ["config", "import", "--stdin", "--mode", "merge", *argv_tail]
    )
    rc = config_cmd.run(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("\n") == 1 and out.endswith("\n")
    assert json.loads(out)["mode"] == "merge"


def test_import_format_table_renders_a_human_report(capsys, monkeypatch):
    monkeypatch.setattr(config_cmd, "_load_config_bundle_service", lambda **kwargs: _FakeBundleService())
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_bundle_payload())))
    args = build_parser(plugin_commands=[]).parse_args(
        ["config", "import", "--stdin", "--mode", "merge", "--format", "table"]
    )
    assert config_cmd.run(args) == 0
    out = capsys.readouterr().out
    assert "providers" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


@pytest.mark.parametrize(
    "conflict",
    [
        ["--format", "json", "--json"],
        ["--format", "table", "--json"],
        ["--format", "json", "--yaml"],
        ["--format", "table", "--form"],
    ],
)
def test_import_rejects_conflicting_format_arguments(conflict):
    parser = build_parser(plugin_commands=[])
    with pytest.raises(SystemExit):
        parser.parse_args(["config", "import", "--stdin", *conflict])


def test_import_rejects_unknown_format_choice():
    parser = build_parser(plugin_commands=[])
    with pytest.raises(SystemExit):
        parser.parse_args(["config", "import", "--stdin", "--format", "xml"])

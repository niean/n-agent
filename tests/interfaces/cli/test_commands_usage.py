from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.interfaces.cli.commands import usage
from app.interfaces.cli.main import build_parser


class _FakeUsageService:
    def __init__(self):
        self.session_id_arg: str | None = None
        self.stats_returned = False
        self.records_returned = False
        self.compressions_returned = False

    async def get_session_stats(self, session_id):
        self.session_id_arg = session_id
        self.stats_returned = True
        return SimpleNamespace(
            session_id=session_id,
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=10,
            cache_write_tokens=5,
            reasoning_tokens=0,
            total_tokens=165,
            api_call_count=2,
            estimated_cost_usd="0.0123",
            cost_status="estimated",
        )

    async def list_records(self, session_id, limit=50):
        self.records_returned = True
        return [
            SimpleNamespace(
                id=1, session_id=session_id, model="gpt-4o", provider="openai",
                input_tokens=100, output_tokens=50, cache_read_tokens=10, cache_write_tokens=5,
                reasoning_tokens=0, total_tokens=165, estimated_cost_usd="0.0123",
                cost_status="estimated", latency_ms=200, created_at="2026-07-11T10:00:00Z",
            ),
        ]

    async def list_compressions(self, session_id):
        self.compressions_returned = True
        return [
            SimpleNamespace(
                id=1, session_id=session_id, before_tokens=5000, after_tokens=2000,
                tokens_saved=3000, compression_ratio=0.4, created_at="2026-07-11T10:00:00Z",
            ),
        ]


def _args(**kw):
    base = {"session_id": None, "json": False, "form": False, "yaml": False}
    base.update(kw)
    return SimpleNamespace(**base)


def test_usage_command_help():
    parser = build_parser()
    # Verify the usage subcommand exists and accepts --help (argparse prints help and exits 0)
    import pytest as _pytest
    with _pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["usage", "--help"])
    assert excinfo.value.code == 0


def test_usage_command_with_session_renders_stats(monkeypatch, capsys):
    fake = _FakeUsageService()
    monkeypatch.setattr(usage, "_load_usage_service", lambda: fake)
    rc = usage.run(_args(session_id="sess-1"))
    assert rc == 0
    assert fake.stats_returned
    assert fake.records_returned
    assert fake.compressions_returned
    out = capsys.readouterr().out
    assert "sess-1" in out
    assert "165" in out
    assert "gpt-4o" in out


def test_usage_command_json_output(monkeypatch, capsys):
    fake = _FakeUsageService()
    monkeypatch.setattr(usage, "_load_usage_service", lambda: fake)
    rc = usage.run(_args(session_id="sess-1", json=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert '"session_id": "sess-1"' in out
    assert '"total_tokens": 165' in out


def test_usage_command_no_session_runs_without_error(monkeypatch, capsys):
    fake = _FakeUsageService()
    monkeypatch.setattr(usage, "_load_usage_service", lambda: fake)
    rc = usage.run(_args())
    assert rc == 0

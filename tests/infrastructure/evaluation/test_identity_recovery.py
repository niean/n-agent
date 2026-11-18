"""Manual recovery uses frozen evidence and never rewrites the source ledger."""
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.infrastructure.evaluation.identity_recovery import apply_plan, plan_recovery, main
from app.infrastructure.evaluation.store import SQLiteEvaluationStore
from tests.infrastructure.evaluation.test_evaluation_store import _sample_payload, _auto_result_payload


@pytest.fixture
def env(tmp_path):
    evaluation = tmp_path / "evaluation.db"
    business = tmp_path / "business.db"
    snapshots = tmp_path / "evidence"
    (snapshots / "snapshots").mkdir(parents=True)
    store = SQLiteEvaluationStore(str(evaluation))
    with sqlite3.connect(business) as conn:
        conn.executescript("CREATE TABLE agent_runs(id TEXT, session_id TEXT);"
                           "CREATE TABLE tasks(id TEXT, origin_session_id TEXT);")
        conn.executemany("INSERT INTO agent_runs VALUES(?,?)", [("run-1", "session-1"), ("run-2", "session-1"), ("run-other", "other")])
        conn.execute("INSERT INTO tasks VALUES('task-1','task-origin')")
    return evaluation, business, snapshots, store


async def seed(env, content, *, target=None, ref=None, hash_override=None):
    evaluation, _, snapshots, store = env
    body = json.dumps(content).encode()
    sha = hashlib.sha256(body).hexdigest()
    (snapshots / "snapshots" / f"{sha}.json").write_bytes(body)
    payload = _sample_payload()
    payload.update(producer_event_id=sha, target=target or {"service": "n-agent", "kind": "agent_run", "subject_id": "synthetic"}, snapshot_ref=ref or f"snapshots/{sha}.json", snapshot_hash=hash_override or sha)
    sample_id = await store.save_sample(payload)
    # Minimal experiment membership is sufficient for the planner's query.
    with sqlite3.connect(evaluation) as conn:
        conn.execute("INSERT INTO evaluation_jobs(job_id,sample_id,definition_id,definition_version,experiment_id,replicate,requested_by,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (sha, sample_id, "d", "v1", "experiment", 1, "manual", "queued", "2026-01-01", "2026-01-01"))
    return sample_id


@pytest.mark.parametrize("content,session,run", [
    ({"business_run_id": "run-1"}, "session-1", "run-1"),
    ({"business_session_id": "frozen-session"}, "frozen-session", None),
    ({"scenario_evidence": {"cli": {"session_id": "session-1"}, "steps": [{"run_id": "run-1"}, {"run_id": "run-2"}]}}, "session-1", None),
    ({"scenario_evidence": {"acp": {"session_id": "session-1"}, "conversation_id": "not-a-session", "steps": [{"run_id": "run-1"}]}}, "session-1", "run-1"),
    ({"scenario_evidence": {"tasks": [{"task_id": "task-1", "execution_session_id": "worker"}]}}, "task-origin", None),
])
async def test_planner_identity_paths(env, content, session, run):
    sample = await seed(env, content)
    plan = plan_recovery(*env[:3])
    assert plan["summary"]["planned"] == 1
    entry = plan["items"][0]
    assert (entry["sample_id"], entry["session_id"], entry["run_id"]) == (sample, session, run)
    assert entry["evidence"]["proofs"]


@pytest.mark.parametrize("content,ref,sha", [
    ({"business_run_id": "run-1", "business_session_id": "other"}, None, None),
    ({"scenario_evidence": {"cli": {"session_id": "other"}, "steps": [{"run_id": "run-1"}]}}, None, None),
    ({"scenario_evidence": {"conversation_id": "not-proof"}}, None, None),
    ({"business_run_id": "missing"}, None, None),
    ({"business_run_id": "run-1"}, "../escape.json", None),
    ({"business_run_id": "run-1"}, None, "a" * 64),
])
async def test_planner_rejects_ambiguous_or_invalid_proof(env, content, ref, sha):
    await seed(env, content, ref=ref, hash_override=sha)
    plan = plan_recovery(*env[:3])
    assert plan["summary"]["planned"] == 0
    assert plan["items"][0]["status"] == "rejected"


async def test_missing_snapshot_and_existing_identity_are_not_recovered(env):
    await seed(env, {"business_run_id": "run-1"})
    for path in (env[2] / "snapshots").iterdir():
        path.unlink()
    assert plan_recovery(*env[:3])["items"][0]["status"] == "rejected"


async def test_plan_is_readonly_apply_backups_and_preserves_original_rows(env, tmp_path):
    sample_id = await seed(env, {"business_run_id": "run-1"})
    before = env[0].read_bytes()
    plan = plan_recovery(*env[:3])
    assert env[0].read_bytes() == before
    with sqlite3.connect(env[0]) as conn:
        original = conn.execute("SELECT * FROM evaluation_samples").fetchall()
    report = tmp_path / "audit.json"
    await apply_plan(plan, report)
    saved = json.loads(report.read_text())
    assert saved["summary"]["applied"] == 1
    assert saved["backup_path"]
    assert Path(saved["backup_path"]).stat().st_mode & 0o777 == 0o600
    assert report.stat().st_mode & 0o777 == 0o600
    assert (await env[3].load_sample(sample_id))["target"]["session_id"] == "session-1"
    with sqlite3.connect(env[0]) as conn:
        assert conn.execute("SELECT * FROM evaluation_samples").fetchall() == original
    assert plan_recovery(*env[:3])["items"][0]["status"] == "skipped"


async def test_apply_rechecks_original_and_tombstones(env, tmp_path):
    sample_id = await seed(env, {"business_run_id": "run-1"})
    plan = plan_recovery(*env[:3])
    with sqlite3.connect(env[0]) as conn:
        conn.execute("UPDATE evaluation_samples SET payload_hash='changed' WHERE sample_id=?", (sample_id,))
    await apply_plan(plan, tmp_path / "audit.json")
    assert plan["summary"]["applied"] == 0
    assert plan["items"][0]["status"] == "rejected"


async def test_sample_and_result_tombstones_prevent_planning(env):
    sample_id = await seed(env, {"business_run_id": "run-1"})
    await env[3].record_deletion_tombstone({"subject_kind": "sample", "subject_id": sample_id})
    assert plan_recovery(*env[:3])["items"][0]["status"] == "skipped"


async def test_snapshot_content_tampering_and_symlink_are_rejected(env):
    await seed(env, {"business_run_id": "run-1"})
    path = next((env[2] / "snapshots").iterdir())
    original = path.read_bytes()
    path.write_bytes(b"{}")
    assert plan_recovery(*env[:3])["items"][0]["status"] == "rejected"
    other = env[2] / "elsewhere.json"
    other.write_bytes(original)
    path.unlink()
    path.symlink_to(other)
    assert plan_recovery(*env[:3])["items"][0]["status"] == "rejected"


async def test_apply_rechecks_deletion_gate_and_preserves_existing_identity(env, tmp_path):
    sample_id = await seed(env, {"business_run_id": "run-1"})
    plan = plan_recovery(*env[:3])
    await env[3].record_deletion_tombstone({"subject_kind": "session", "subject_id": "session-1"})
    await apply_plan(plan, tmp_path / "audit.json")
    assert plan["summary"]["applied"] == 0
    assert "session_id" not in (await env[3].load_sample(sample_id))["target"]


async def test_report_cannot_overwrite_source_database(env):
    await seed(env, {"business_run_id": "run-1"})
    plan = plan_recovery(*env[:3])
    before = env[0].read_bytes()
    with pytest.raises(ValueError, match="overwrite"):
        await apply_plan(plan, env[0])
    assert env[0].read_bytes() == before


async def test_result_tombstone_excludes_linked_experimental_sample(env):
    sample_id = await seed(env, {"business_run_id": "run-1"})
    with sqlite3.connect(env[0]) as conn:
        job_id = conn.execute("SELECT job_id FROM evaluation_jobs WHERE sample_id=?", (sample_id,)).fetchone()[0]
    result_id = await env[3].save_result(_auto_result_payload(job_id))
    await env[3].record_deletion_tombstone({"subject_kind": "result", "subject_id": result_id})
    assert plan_recovery(*env[:3])["items"][0]["status"] == "skipped"


async def test_existing_original_identity_is_not_overwritten(env):
    sample_id = await seed(env, {"business_run_id": "run-1"}, target={"session_id": "other"})
    item = plan_recovery(*env[:3])["items"][0]
    assert item["status"] == "rejected"
    assert (await env[3].load_sample(sample_id))["target"]["session_id"] == "other"


async def test_cli_reports_rejections_with_nonzero_exit(env, tmp_path, monkeypatch, capsys):
    await seed(env, {"business_run_id": "missing"})
    original_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda path: str(path) == "/.dockerenv" or original_exists(path))
    monkeypatch.setattr("app.config.Settings", lambda: SimpleNamespace(
        evaluation_ledger_path=env[0], sqlite_path=env[1], evaluation_snapshots_root=env[2]))
    report = tmp_path / "cli-audit.json"
    assert main(["--report", str(report)]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["summary"]["rejected"] == 1
    assert json.loads(report.read_text())["summary"]["rejected"] == 1

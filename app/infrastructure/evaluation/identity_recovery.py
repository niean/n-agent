"""Explicit, audited recovery of historical experiment business identities.

Run inside Docker: python -m app.infrastructure.evaluation.identity_recovery
--report /app/locals/identity-recovery.json [--apply]. The default only plans.
Frozen samples, results and evidence bytes are never rewritten.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any
import uuid


_REF = re.compile(r"snapshots/([0-9a-f]{64})\.json\Z")
_ORIGINAL = ("target_json", "snapshot_ref", "snapshot_hash", "payload_hash")


@contextmanager
def _readonly(path: Path):
    conn = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def _table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _snapshot(root: Path, row: dict[str, Any]) -> dict[str, Any]:
    matched = _REF.fullmatch(row["snapshot_ref"] or "")
    if matched is None or row["snapshot_hash"] != matched[1]:
        raise ValueError("invalid snapshot reference or registered hash")
    path = root.absolute() / row["snapshot_ref"]
    # Reject symbolic links in every component, including the configured root.
    for component in (path, *path.parents):
        mode = component.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError("snapshot path contains symlink")
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("snapshot is not a regular file")
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != row["snapshot_hash"]:
        raise ValueError("snapshot content hash mismatch")
    content = json.loads(body)
    if not isinstance(content, dict):
        raise ValueError("snapshot is not an object")
    return content


def _revoked(conn: sqlite3.Connection, sample_id: str, session_id=None, run_id=None) -> bool:
    if not _table(conn, "evaluation_deletion_tombstones"):
        return False
    subjects = {("sample", sample_id)}
    if session_id:
        subjects.add(("session", session_id))
    if run_id:
        subjects.add(("agent_run", run_id))
    for row in conn.execute("SELECT subject_kind, subject_id FROM evaluation_deletion_tombstones"):
        if (row["subject_kind"], row["subject_id"]) in subjects:
            return True
    if conn.execute("SELECT 1 FROM evaluation_results r JOIN evaluation_jobs j ON r.job_id=j.job_id JOIN evaluation_deletion_tombstones t ON t.subject_kind='result' AND t.subject_id=r.result_id WHERE j.sample_id=? LIMIT 1", (sample_id,)).fetchone():
        return True
    return conn.execute("SELECT 1 FROM evaluation_evidence_targets e JOIN evaluation_deletion_tombstones t ON t.subject_kind=e.subject_kind AND t.subject_id=e.subject_id AND (t.subject_revision='' OR t.subject_revision=e.subject_revision) WHERE e.sample_id=? LIMIT 1", (sample_id,)).fetchone() is not None


def _resolve_identity(content: dict[str, Any], business: sqlite3.Connection) -> tuple[str, str | None, list[dict[str, Any]]]:
    sessions: set[str] = set()
    runs: set[str] = set()
    proofs: list[dict[str, Any]] = []

    def add_session(value, source, **details):
        value = _text(value)
        if value:
            sessions.add(value)
            proofs.append({"source": source, "session_id": value, **details})

    def add_run(value, source):
        run_id = _text(value)
        if not run_id:
            return
        if not _table(business, "agent_runs"):
            return
        row = business.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        if row is not None:
            if "business_owner_deleted_at" in row.keys() and row["business_owner_deleted_at"] is not None:
                raise ValueError("business run owner deleted")
            add_session(row["session_id"], source, run_id=run_id, table="agent_runs")
            runs.add(run_id)

    add_session(content.get("business_session_id"), "snapshot.business_session_id")
    add_run(content.get("business_run_id"), "snapshot.business_run_id")
    scenario = content.get("scenario_evidence") or {}
    if not isinstance(scenario, dict):
        raise ValueError("scenario_evidence is not an object")
    for channel in ("cli", "acp"):
        evidence = scenario.get(channel) or {}
        if not isinstance(evidence, dict):
            raise ValueError(f"{channel} evidence is not an object")
        add_session(evidence.get("session_id"), f"snapshot.scenario_evidence.{channel}.session_id")
    for step in scenario.get("steps") or []:
        if isinstance(step, dict):
            add_run(step.get("run_id"), "snapshot.scenario_evidence.steps.run_id")
    for task in scenario.get("tasks") or []:
        task_id = _text(task.get("task_id")) if isinstance(task, dict) else None
        if task_id and _table(business, "tasks"):
            row = business.execute("SELECT origin_session_id FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is not None:
                add_session(row["origin_session_id"], "snapshot.scenario_evidence.tasks.task_id", task_id=task_id, table="tasks")
    if len(sessions) != 1:
        raise ValueError("conflicting source sessions" if sessions else "no proven source session")
    return next(iter(sessions)), next(iter(runs)) if len(runs) == 1 else None, proofs


def _summarize(plan):
    counts = Counter(item["status"] for item in plan["items"])
    plan["summary"] = {key: counts[key] for key in ("planned", "applied", "rejected", "skipped")}
    plan["source_counts"] = dict(Counter(
        proof["source"] for item in plan["items"]
        for proof in item.get("evidence", {}).get("proofs", [])
    ))


def plan_recovery(evaluation_path, business_path, snapshots_root, *, sample_id=None) -> dict[str, Any]:
    """Read-only plan for missing experimental identities; no store constructor."""
    evaluation_path, business_path, root = map(Path, (evaluation_path, business_path, snapshots_root))
    plan: dict[str, Any] = {"version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                           "evaluation_path": str(evaluation_path.absolute()), "business_path": str(business_path.absolute()),
                           "snapshots_root": str(root.absolute()), "items": []}
    with _readonly(evaluation_path) as evaluation, _readonly(business_path) as business:
        query = "SELECT DISTINCT s.* FROM evaluation_samples s JOIN evaluation_jobs j ON j.sample_id=s.sample_id WHERE j.experiment_id IS NOT NULL"
        rows = evaluation.execute(query + (" AND s.sample_id=?" if sample_id else "") + " ORDER BY s.sample_id", (sample_id,) if sample_id else ()).fetchall()
        plan["experimental_samples"] = len(rows)
        plan["experimental_results"] = evaluation.execute("SELECT COUNT(*) FROM evaluation_results r JOIN evaluation_jobs j ON r.job_id=j.job_id WHERE j.experiment_id IS NOT NULL").fetchone()[0]
        for raw in rows:
            row = dict(raw)
            item: dict[str, Any] = {"sample_id": row["sample_id"], "original": {key: row[key] for key in _ORIGINAL}, "status": "rejected"}
            plan["items"].append(item)
            try:
                target = json.loads(row["target_json"])
                if not isinstance(target, dict):
                    raise ValueError("invalid original target")
                identity = evaluation.execute("SELECT * FROM evaluation_sample_identities WHERE sample_id=?", (row["sample_id"],)).fetchone() if _table(evaluation, "evaluation_sample_identities") else None
                if identity is not None or (target.get("session_id") and target.get("run_id")):
                    item.update(status="skipped", reason="identity already present")
                    continue
                if _revoked(evaluation, row["sample_id"]):
                    item.update(status="skipped", reason="deletion tombstone")
                    continue
                session_id, run_id, proofs = _resolve_identity(_snapshot(root, row), business)
                for key, value in (("session_id", session_id), ("run_id", run_id)):
                    if target.get(key) and value and target[key] != value:
                        raise ValueError(f"original {key} conflicts with proof")
                run_id = run_id or target.get("run_id")
                if _revoked(evaluation, row["sample_id"], session_id, run_id):
                    item.update(status="skipped", reason="identity deletion tombstone")
                    continue
                if target.get("session_id") == session_id and (not run_id or target.get("run_id") == run_id):
                    item.update(status="skipped", reason="no missing proven identity")
                    continue
                item.update(status="planned", session_id=session_id, run_id=run_id,
                            source="manual_snapshot_identity_recovery", evidence={"snapshot_ref": row["snapshot_ref"], "snapshot_hash": row["snapshot_hash"], "proofs": proofs})
            except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
                item["reason"] = str(exc)
    _summarize(plan)
    return plan


def _write_report(path: Path, plan):
    protected = (Path(plan[key]).resolve() for key in ("evaluation_path", "business_path"))
    if path.resolve() in protected or path.resolve().is_relative_to(Path(plan["snapshots_root"]).resolve()):
        raise ValueError("audit report must not overwrite source databases or snapshots")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    with os.fdopen(os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as output:
        output.write(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


async def apply_plan(plan: dict[str, Any], report_path) -> dict[str, Any]:
    """Persist audit first, back up before DDL/writes, then revalidate each item."""
    report_path = Path(report_path)
    _write_report(report_path, plan)
    pending = [item for item in plan["items"] if item["status"] == "planned"]
    if not pending:
        return plan
    evaluation_path = Path(plan["evaluation_path"])
    backup_path = report_path.with_name(report_path.name + f".{uuid.uuid4().hex}.backup.sqlite3")
    os.close(os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    with _readonly(evaluation_path) as source, sqlite3.connect(backup_path) as destination:
        source.backup(destination)
    plan["backup_path"] = str(backup_path.absolute())
    _write_report(report_path, plan)
    from app.infrastructure.evaluation.store import SQLiteEvaluationStore
    store = SQLiteEvaluationStore(str(evaluation_path))
    for item in pending:
        try:
            # Samples are immutable apart from controlled evidence revocation;
            # the store also checks deletion gates atomically with its insert.
            fresh = plan_recovery(evaluation_path, plan["business_path"], plan["snapshots_root"], sample_id=item["sample_id"])
            current = next((entry for entry in fresh["items"] if entry["sample_id"] == item["sample_id"]), None)
            if current is None or any(current.get(key) != item.get(key) for key in ("original", "session_id", "run_id", "evidence", "status")):
                raise ValueError("source identity or immutable sample changed since planning")
            await store.save_sample_identity(item["sample_id"], item["session_id"], item["run_id"], item["source"], item["evidence"])
            item["status"] = "applied"
        except Exception as exc:
            item.update(status="rejected", reason=str(exc))
        _summarize(plan)
        _write_report(report_path, plan)
    return plan


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not (Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()):
        parser.error("identity recovery must run inside Docker")
    from app.config import Settings
    settings = Settings()
    plan = plan_recovery(settings.evaluation_ledger_path, settings.sqlite_path, settings.evaluation_snapshots_root)
    _write_report(Path(args.report), plan)
    if args.apply:
        asyncio.run(apply_plan(plan, args.report))
    print(json.dumps({"report": args.report, "backup_path": plan.get("backup_path"),
                      "summary": plan["summary"], "source_counts": plan["source_counts"],
                      "experimental_samples": plan["experimental_samples"],
                      "experimental_results": plan["experimental_results"]}, ensure_ascii=False))
    return 1 if plan["summary"]["rejected"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

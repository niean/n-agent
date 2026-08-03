"""T14: main.py Artifact subsystem wiring + gating tests.

Covers:
  - artifacts_enabled=True -> artifact_service non-None; registry/store/policy/
    service constructed; the 2 write-through callbacks injected into TaskService
    and TaskRunService; create_dashboard_router receives artifact_service;
    register_published_artifact_routes registered at app root (GET /p/{id}
    returns 404 for unknown, not 404-for-route-missing); lifespan runs backfill.
  - artifacts_enabled=False -> artifact_service is None; NO callback injected
    into TaskService/TaskRunService; NO backfill; /artifacts, /chat/artifacts*,
    /p/* all NOT registered (404).
  - Registry/schema init failure -> fail-fast (propagates, NOT silent degrade).
  - Relative artifacts_root resolved via workspace_root (not cwd drift).
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.application.artifact_service import ArtifactService
from app.config import Settings
from app.domain.task import TaskAttachment
from app.main import ApplicationServices, build_application_services, create_app


def _settings(tmp_path: Path, **updates) -> Settings:
    values = dict(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        skills_root=str(tmp_path / "skills"),
        plugins_root=str(tmp_path / "plugins"),
        scheduler_enabled=False,
        feishu_enabled=False,
        sandbox_enabled=False,
        task_enabled=False,
        artifacts_enabled=False,
        # Override the default /app/locals/artifacts (absolute, does not exist
        # on the test host) with a tmp_path-based root.
        artifacts_root=str(tmp_path / "artifacts"),
    )
    values.update(updates)
    return Settings(**values)


# ---------------------------------------------------------------------------
# ApplicationServices dataclass field
# ---------------------------------------------------------------------------


def test_artifact_service_is_dataclass_field():
    """ApplicationServices must have an artifact_service field."""
    import dataclasses
    fields = {f.name: f for f in dataclasses.fields(ApplicationServices)}
    assert "artifact_service" in fields
    assert fields["artifact_service"].default is None


# ---------------------------------------------------------------------------
# Enabled: artifact_service constructed
# ---------------------------------------------------------------------------


def test_artifact_service_non_none_when_enabled(tmp_path: Path):
    services = build_application_services(
        _settings(tmp_path, artifacts_enabled=True)
    )
    assert services.artifact_service is not None
    assert isinstance(services.artifact_service, ArtifactService)


def test_artifact_service_none_when_disabled(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))
    assert services.artifact_service is None


def test_artifact_service_reuses_information_flow_and_audit(tmp_path: Path):
    """ArtifactService must reuse the existing InformationFlowService and
    PolicyAuditService instances (not construct new ones)."""
    services = build_application_services(
        _settings(tmp_path, artifacts_enabled=True)
    )
    assert services.artifact_service is not None
    # The ArtifactService stores them as _flow and _audit
    assert services.artifact_service._flow is not None
    assert services.artifact_service._audit is not None


# ---------------------------------------------------------------------------
# Callback injection (both task_enabled AND artifacts_enabled)
# ---------------------------------------------------------------------------


def test_callbacks_injected_when_both_enabled(tmp_path: Path):
    """When both task_enabled and artifacts_enabled, the callbacks are
    injected into TaskService and TaskRunService."""
    services = build_application_services(
        _settings(tmp_path, task_enabled=True, artifacts_enabled=True)
    )
    assert services.task_service is not None
    assert services.task_run_service is not None
    assert services.artifact_service is not None
    # TaskService has the attachment callback
    assert services.task_service._artifact_register_callback is not None
    # TaskService has the artifact delete callback (cascade on task delete)
    assert services.task_service._artifact_delete_callback is not None
    # ArtifactService has the task_exists callback (orphan backfill on startup)
    assert services.artifact_service._task_exists_callback is not None
    # ArtifactService has the task_attachment_delete callback (cascade-delete
    # the source TaskAttachment when a task_attachment artifact is deleted from
    # the workbench, keeping the task detail page in sync).
    assert services.artifact_service._task_attachment_delete_callback is not None
    # TaskRunService has the task-artifact callback
    assert services.task_run_service.artifact_register_callback is not None


def test_no_callbacks_when_artifacts_disabled(tmp_path: Path):
    """When artifacts_enabled=False (but task_enabled=True), NO callback
    is injected into TaskService/TaskRunService."""
    services = build_application_services(
        _settings(tmp_path, task_enabled=True, artifacts_enabled=False)
    )
    assert services.task_service is not None
    assert services.task_run_service is not None
    assert services.artifact_service is None
    assert services.task_service._artifact_register_callback is None
    assert services.task_service._artifact_delete_callback is None
    assert services.task_run_service.artifact_register_callback is None


def test_attachment_callback_registers_artifact(tmp_path: Path):
    """Regression: the injected TaskService callback must accept a real
    TaskAttachment (which has .id) and register an artifact -- not raise
    AttributeError by passing it to register_from_attachment (which expects
    an ArtifactAttachmentSource with .attachment_id). The composition root
    must adapt the type."""
    attachments_root = tmp_path / "task-attachments"
    attachments_root.mkdir(parents=True, exist_ok=True)
    services = build_application_services(
        _settings(
            tmp_path,
            task_enabled=True,
            artifacts_enabled=True,
            task_attachments_root=str(attachments_root),
        )
    )
    task_service = services.task_service
    artifact_service = services.artifact_service
    assert task_service._artifact_register_callback is not None

    task_id = "t-regression-1"
    stored_name = "stored-1.txt"
    content = b"regression attachment content"
    # Seed the attachment file where the content store expects it.
    (attachments_root / task_id).mkdir(parents=True, exist_ok=True)
    (attachments_root / task_id / stored_name).write_bytes(content)

    att = TaskAttachment(
        id="ta-regression-1",
        task_id=task_id,
        filename="file-1.txt",
        stored_name=stored_name,
        content_type="text/plain",
        size=len(content),
        checksum="sha256:" + hashlib.sha256(content).hexdigest(),
        uploaded_by="test",
    )
    # Must not raise AttributeError (the regression).
    asyncio.run(task_service._artifact_register_callback(att))

    # The artifact must be registered (source_ref == attachment_id).
    page = asyncio.run(artifact_service.list_artifacts(limit=50))
    source_refs = [a.source_ref for a in page.items]
    assert "ta-regression-1" in source_refs


def test_delete_task_cascades_artifacts_out_of_list(tmp_path: Path):
    """Regression: deleting a task must remove its artifacts from the artifact
    list. Previously artifacts lived in a separate DB (registered one-way via
    artifact_register_callback) and survived task deletion, so a deleted task's
    artifacts kept showing in the list (e.g. e2e-art-*-att2.txt)."""
    attachments_root = tmp_path / "task-attachments"
    services = build_application_services(
        _settings(
            tmp_path,
            task_enabled=True,
            artifacts_enabled=True,
            task_attachments_root=str(attachments_root),
        )
    )
    task_service = services.task_service
    artifact_service = services.artifact_service

    task = asyncio.run(task_service.create_task(title="cascade", created_by="u"))
    asyncio.run(
        task_service.upload_attachment(
            task.id, "report.md", b"# Hello", "text/markdown", "alice",
        )
    )

    # Artifact registered against the task and visible in the list.
    page = asyncio.run(artifact_service.list_artifacts(limit=50))
    assert any(a.source_context_ref == task.id for a in page.items)

    # Delete the task -> its artifacts must no longer appear in the list.
    asyncio.run(task_service.delete_task(task.id))
    page = asyncio.run(artifact_service.list_artifacts(limit=50))
    assert not any(a.source_context_ref == task.id for a in page.items)


def test_delete_artifact_cascades_to_task_attachment(tmp_path: Path):
    """Regression: deleting a task_attachment artifact from the workbench must
    also delete the underlying TaskAttachment (record + file), otherwise the
    task detail page still shows the attachment -- the user-reported sync bug.
    Mirrors test_delete_task_cascades_artifacts_out_of_list in the reverse
    direction (artifact delete -> task attachment delete)."""
    attachments_root = tmp_path / "task-attachments"
    services = build_application_services(
        _settings(
            tmp_path,
            task_enabled=True,
            artifacts_enabled=True,
            task_attachments_root=str(attachments_root),
        )
    )
    task_service = services.task_service
    artifact_service = services.artifact_service

    task = asyncio.run(task_service.create_task(title="cascade", created_by="u"))
    att = asyncio.run(
        task_service.upload_attachment(
            task.id, "report.md", b"# Hello", "text/markdown", "alice",
        )
    )

    # Artifact registered against the attachment and visible in the list.
    page = asyncio.run(artifact_service.list_artifacts(limit=50))
    art = next(a for a in page.items if a.source_context_ref == task.id)
    assert art.source_kind.value == "task_attachment"
    # Attachment file exists on disk.
    assert (attachments_root / task.id / att.stored_name).exists()

    # Delete the artifact from the workbench -> the task attachment must be gone.
    asyncio.run(artifact_service.delete_artifact(art.id))
    assert asyncio.run(task_service.get_attachment(att.id)) is None
    # And the attachment file removed.
    assert not (attachments_root / task.id / att.stored_name).exists()



# ---------------------------------------------------------------------------
# Route registration: /p/* (published, app root)
# ---------------------------------------------------------------------------


def test_published_routes_registered_when_enabled(tmp_path: Path):
    """GET /p/{id} returns 404 for unknown publish_id -- the route exists
    (handler-set CSP header present), not 404-for-route-missing."""
    app = create_app(_settings(tmp_path, artifacts_enabled=True))
    with TestClient(app) as client:
        r = client.get("/p/AAAAAAAAAAAAAAAAAAAAAAAAAA")
        assert r.status_code == 404
        # Route exists: handler-set security headers present
        header_names = {k.lower() for k in r.headers.keys()}
        assert "content-security-policy" in header_names


def test_published_routes_not_registered_when_disabled(tmp_path: Path):
    """When artifacts_enabled=False, /p/* is NOT registered (404 without
    handler-set security headers)."""
    app = create_app(_settings(tmp_path, artifacts_enabled=False))
    with TestClient(app) as client:
        r = client.get("/p/AAAAAAAAAAAAAAAAAAAAAAAAAA")
        assert r.status_code == 404
        # Route missing: no handler-set CSP header
        header_names = {k.lower() for k in r.headers.keys()}
        assert "content-security-policy" not in header_names


# ---------------------------------------------------------------------------
# Route registration: /artifacts shell + /chat/artifacts* (dashboard)
# ---------------------------------------------------------------------------


def test_dashboard_artifact_routes_registered_when_enabled(tmp_path: Path):
    """Dashboard /artifacts shell and /chat/artifacts API routes are
    registered when artifact_service is wired."""
    app = create_app(_settings(tmp_path, artifacts_enabled=True))
    with TestClient(app) as client:
        # /artifacts shell route (returns index.html)
        r = client.get("/artifacts")
        assert r.status_code != 404
        # /chat/artifacts API route (list endpoint)
        r = client.get("/chat/artifacts")
        assert r.status_code != 404


def test_dashboard_artifact_routes_not_registered_when_disabled(tmp_path: Path):
    """When artifacts_enabled=False, /artifacts and /chat/artifacts* are
    NOT registered (404)."""
    app = create_app(_settings(tmp_path, artifacts_enabled=False))
    with TestClient(app) as client:
        r = client.get("/artifacts")
        assert r.status_code == 404
        r = client.get("/chat/artifacts")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Fail-fast on registry/schema init
# ---------------------------------------------------------------------------


def test_registry_init_failure_propagates(tmp_path: Path, monkeypatch):
    """Registry/schema init failure MUST propagate (fail-fast, NOT silent
    degrade). Matches the task_enabled fail-fast pattern."""
    from app.infrastructure.registry.sqlite_artifact_registry import (
        SQLiteArtifactRegistry,
    )

    def boom(self, db_path: str):
        raise RuntimeError("schema init failed")

    monkeypatch.setattr(SQLiteArtifactRegistry, "__init__", boom)
    with pytest.raises(RuntimeError, match="schema init failed"):
        build_application_services(
            _settings(tmp_path, artifacts_enabled=True)
        )


# ---------------------------------------------------------------------------
# Relative artifacts_root resolution
# ---------------------------------------------------------------------------


def test_relative_artifacts_root_resolved_via_workspace(tmp_path: Path):
    """Relative artifacts_root is resolved relative to workspace_root, not
    cwd drift."""
    services = build_application_services(
        _settings(
            tmp_path,
            artifacts_enabled=True,
            artifacts_root=Path("locals/my-artifacts"),
        )
    )
    assert services.artifact_service is not None
    content_store = services.artifact_service._content_store
    expected_root = (tmp_path / "locals" / "my-artifacts").resolve()
    assert content_store._root == expected_root


# ---------------------------------------------------------------------------
# Lifespan backfill
# ---------------------------------------------------------------------------


def test_lifespan_runs_backfill_when_enabled(tmp_path: Path, monkeypatch):
    """Lifespan runs backfill_attachments when artifact_service is not None."""
    backfill_called = {"value": False}
    original_backfill = ArtifactService.backfill_attachments

    async def tracking_backfill(self, *, batch_size: int = 100):
        backfill_called["value"] = True
        return await original_backfill(self, batch_size=batch_size)

    monkeypatch.setattr(
        ArtifactService, "backfill_attachments", tracking_backfill
    )
    app = create_app(_settings(tmp_path, artifacts_enabled=True))
    with TestClient(app) as client:
        assert backfill_called["value"]


def test_lifespan_skips_backfill_when_disabled(tmp_path: Path, monkeypatch):
    """Lifespan does NOT run backfill when artifact_service is None."""
    backfill_called = {"value": False}

    async def tracking_backfill(self, *, batch_size: int = 100):
        backfill_called["value"] = True
        return {"processed": 0, "created": 0, "skipped": 0, "failed": 0}

    monkeypatch.setattr(
        ArtifactService, "backfill_attachments", tracking_backfill
    )
    app = create_app(_settings(tmp_path, artifacts_enabled=False))
    with TestClient(app) as client:
        assert not backfill_called["value"]

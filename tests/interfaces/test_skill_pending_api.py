"""Tests for the skill pending approval + pin/usage API endpoints on the dashboard router."""
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.skill_service import SkillScanReport, SkillScanWarning
from app.domain.skill import (
    Skill,
    SkillFrontmatter,
    SkillManageResult,
    SkillNotFoundError,
    SkillPendingWrite,
    SkillReadiness,
    SkillUsage,
    SkillWriteAction,
    SkillWriteOrigin,
)
from app.interfaces.http.dashboard import create_dashboard_router


def _skill(name, enabled=True, readiness=SkillReadiness.AVAILABLE):
    fm = SkillFrontmatter(
        name=name, description="d", version="", platforms=["linux"], tags=[],
        related_skills=[], author="", license="", setup_help=None,
        required_env_vars=[], raw={"name": name},
    )
    return Skill(
        id=f"id-{name}", name=name, relative_path=f"{name}/SKILL.md",
        description="d", platforms=["linux"], frontmatter=fm,
        enabled=enabled, readiness=readiness, last_scan_status="ok",
        last_scan_error=None, last_seen_at=None,
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )


def _pending(
    pending_id="p1", skill_name="demo", action=SkillWriteAction.CREATE,
    state="pending", summary="test summary", diff="--- a\n+++ b\n",
):
    return SkillPendingWrite(
        pending_id=pending_id, action=action, skill_name=skill_name,
        origin=SkillWriteOrigin.FOREGROUND, summary=summary, diff=diff,
        payload={"action": "create", "name": skill_name, "content": "body"},
        state=state, error=None,
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )


def _usage(name="demo", pinned=False, use_count=0):
    return SkillUsage(
        created_by="foreground", use_count=use_count, view_count=0, patch_count=0,
        created_at=datetime.now(timezone.utc), last_used_at=None,
        last_viewed=None, last_patched_at=None,
        state="active", pinned=pinned, archived_at=None,
    )


class _FakeSkillService:
    def __init__(self):
        self.skills = {"demo": _skill("demo")}
        self._approve_result = None

    async def list_skills(self, include_disabled=True):
        return list(self.skills.values())

    async def get(self, name):
        s = self.skills.get(name)
        if s is None:
            raise SkillNotFoundError(name)
        return s

    async def render_view(self, name, session_id=""):
        return {"success": True, "name": name, "content": "BODY", "linked_files": {}}

    async def create_skill(self, payload):
        self.skills[payload.name] = _skill(
            payload.name, enabled=payload.enabled, readiness=payload.readiness,
        )
        return self.skills[payload.name]

    async def update_skill(self, name, payload):
        if name not in self.skills:
            raise SkillNotFoundError(name)
        self.skills[name] = _skill(
            payload.name, enabled=payload.enabled, readiness=payload.readiness,
        )
        return self.skills[name]

    async def delete_skill(self, name):
        if name not in self.skills:
            raise SkillNotFoundError(name)
        del self.skills[name]

    async def set_enabled(self, name, enabled):
        if name not in self.skills:
            raise SkillNotFoundError(name)
        from dataclasses import replace
        self.skills[name] = replace(self.skills[name], enabled=enabled)
        return self.skills[name]

    async def scan_now(self):
        return SkillScanReport(
            skills_count=1,
            warnings=[SkillScanWarning("dup/SKILL.md", "duplicate_name", first_path="x")],
        )

    async def approve_pending(self, pending_id):
        if self._approve_result is not None:
            return self._approve_result
        return SkillManageResult(
            success=False, staged=False, pending_id=pending_id,
            skill_name="", action=SkillWriteAction.CREATE,
            summary="", diff=None, error="pending_not_found_or_taken",
        )


class _FakeSkillPendingStore:
    def __init__(self):
        self.items = {}

    async def list(self):
        return list(self.items.values())

    async def get(self, pending_id):
        return self.items.get(pending_id)

    async def reject(self, pending_id):
        if pending_id in self.items:
            del self.items[pending_id]
            return True
        return False

    async def approve_take(self, pending_id):
        return self.items.get(pending_id)

    async def clear(self, pending_id):
        self.items.pop(pending_id, None)


class _FakeSkillUsageStore:
    def __init__(self):
        self.usages = {}
        self.pinned = {}

    async def get(self, name):
        return self.usages.get(name)

    async def set_pinned(self, name, pinned):
        self.pinned[name] = pinned

    async def upsert(self, name, usage):
        self.usages[name] = usage
        return usage


def _build_app(
    skill_service=None, skill_pending_store=None, skill_usage_store=None,
):
    class _Empty:
        async def list_sessions(self): return []
        async def get_session_detail(self, sid):
            return {"session": None, "messages": [], "summary": None, "task_state": None}
        async def list_tool_calls(self, sid): return []

    class _Tools:
        def list_definitions(self): return []

    class _Models:
        async def list_models(self): return []

        @property
        def default_model(self):
            return "n-agent"

    app = FastAPI()
    app.include_router(create_dashboard_router(
        _Empty(), _Tools(), _Models(), lambda: {},
        skill_service=skill_service or _FakeSkillService(),
        skill_pending_store=skill_pending_store or _FakeSkillPendingStore(),
        skill_usage_store=skill_usage_store or _FakeSkillUsageStore(),
    ))
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /chat/skills/pending
# ---------------------------------------------------------------------------

def test_list_pending_empty():
    client = _build_app()
    resp = client.get("/chat/skills/pending")
    assert resp.status_code == 200
    assert resp.json()["pending"] == []


def test_list_pending_with_items():
    store = _FakeSkillPendingStore()
    store.items["p1"] = _pending("p1", "demo")
    client = _build_app(skill_pending_store=store)
    resp = client.get("/chat/skills/pending")
    assert resp.status_code == 200
    items = resp.json()["pending"]
    assert len(items) == 1
    assert items[0]["pending_id"] == "p1"
    assert items[0]["skill_name"] == "demo"
    assert items[0]["action"] == "create"
    assert items[0]["origin"] == "foreground"
    assert items[0]["summary"] == "test summary"
    assert items[0]["state"] == "pending"


# ---------------------------------------------------------------------------
# GET /chat/skills/pending/{pending_id}/diff
# ---------------------------------------------------------------------------

def test_get_pending_diff():
    store = _FakeSkillPendingStore()
    store.items["p1"] = _pending("p1", "demo", diff="diff content")
    client = _build_app(skill_pending_store=store)
    resp = client.get("/chat/skills/pending/p1/diff")
    assert resp.status_code == 200
    body = resp.json()
    assert body["diff"] == "diff content"
    assert body["summary"] == "test summary"


def test_get_pending_diff_not_found():
    client = _build_app()
    resp = client.get("/chat/skills/pending/missing/diff")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "skill_pending_not_found"


# ---------------------------------------------------------------------------
# POST /chat/skills/pending/{pending_id}/approve
# ---------------------------------------------------------------------------

def test_approve_pending():
    service = _FakeSkillService()
    service._approve_result = SkillManageResult(
        success=True, staged=False, pending_id="p1",
        skill_name="demo", action=SkillWriteAction.CREATE,
        summary="create demo", diff=None, error=None,
    )
    client = _build_app(skill_service=service)
    resp = client.post("/chat/skills/pending/p1/approve")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["skill_name"] == "demo"


def test_approve_pending_not_found():
    client = _build_app()
    resp = client.post("/chat/skills/pending/missing/approve")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "skill_pending_not_found"


# ---------------------------------------------------------------------------
# POST /chat/skills/pending/{pending_id}/reject
# ---------------------------------------------------------------------------

def test_reject_pending():
    store = _FakeSkillPendingStore()
    store.items["p1"] = _pending("p1")
    client = _build_app(skill_pending_store=store)
    resp = client.post("/chat/skills/pending/p1/reject")
    assert resp.status_code == 200
    assert resp.json()["rejected"] is True
    assert "p1" not in store.items


def test_reject_pending_not_found():
    client = _build_app()
    resp = client.post("/chat/skills/pending/missing/reject")
    assert resp.status_code == 200
    assert resp.json()["rejected"] is False


# ---------------------------------------------------------------------------
# POST /chat/skills/pending/approve-all
# ---------------------------------------------------------------------------

def test_approve_all_pending():
    store = _FakeSkillPendingStore()
    store.items["p1"] = _pending("p1", "demo1")
    store.items["p2"] = _pending("p2", "demo2")
    service = _FakeSkillService()
    service._approve_result = SkillManageResult(
        success=True, staged=False, pending_id="",
        skill_name="", action=SkillWriteAction.CREATE,
        summary="", diff=None, error=None,
    )
    client = _build_app(skill_service=service, skill_pending_store=store)
    resp = client.post("/chat/skills/pending/approve-all")
    assert resp.status_code == 200
    assert resp.json()["approved"] == 2


def test_approve_all_pending_empty():
    client = _build_app()
    resp = client.post("/chat/skills/pending/approve-all")
    assert resp.status_code == 200
    assert resp.json()["approved"] == 0


# ---------------------------------------------------------------------------
# POST /chat/skills/pending/reject-all
# ---------------------------------------------------------------------------

def test_reject_all_pending():
    store = _FakeSkillPendingStore()
    store.items["p1"] = _pending("p1", "demo1")
    store.items["p2"] = _pending("p2", "demo2")
    client = _build_app(skill_pending_store=store)
    resp = client.post("/chat/skills/pending/reject-all")
    assert resp.status_code == 200
    assert resp.json()["rejected"] == 2
    assert len(store.items) == 0


def test_reject_all_pending_empty():
    client = _build_app()
    resp = client.post("/chat/skills/pending/reject-all")
    assert resp.status_code == 200
    assert resp.json()["rejected"] == 0


# ---------------------------------------------------------------------------
# PATCH /chat/skills/{name}/pin
# ---------------------------------------------------------------------------

def test_pin_skill():
    client = _build_app()
    resp = client.patch("/chat/skills/demo/pin", json={"pinned": True})
    assert resp.status_code == 200
    assert resp.json()["pinned"] is True


def test_unpin_skill():
    client = _build_app()
    resp = client.patch("/chat/skills/demo/pin", json={"pinned": False})
    assert resp.status_code == 200
    assert resp.json()["pinned"] is False


def test_pin_skill_not_found():
    client = _build_app()
    resp = client.patch("/chat/skills/missing/pin", json={"pinned": True})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "skill_not_found"


# ---------------------------------------------------------------------------
# GET /chat/skills/usage
# ---------------------------------------------------------------------------

def test_get_skill_usage():
    usage_store = _FakeSkillUsageStore()
    usage_store.usages["demo"] = _usage("demo", pinned=True, use_count=5)
    client = _build_app(skill_usage_store=usage_store)
    resp = client.get("/chat/skills/usage")
    assert resp.status_code == 200
    items = resp.json()["usage"]
    assert len(items) == 1
    assert items[0]["name"] == "demo"
    assert items[0]["use_count"] == 5
    assert items[0]["pinned"] is True


def test_get_skill_usage_empty():
    client = _build_app()
    resp = client.get("/chat/skills/usage")
    assert resp.status_code == 200
    assert resp.json()["usage"] == []


# ---------------------------------------------------------------------------
# Route ordering: literal routes must not be shadowed by {name} catch-all
# ---------------------------------------------------------------------------

def test_pending_routes_not_shadowed_by_catchall():
    """GET /chat/skills/pending and /chat/skills/usage must not be caught by {name}."""
    client = _build_app()
    resp = client.get("/chat/skills/pending")
    assert resp.status_code == 200
    assert "pending" in resp.json()
    resp = client.get("/chat/skills/usage")
    assert resp.status_code == 200
    assert "usage" in resp.json()


def test_existing_skill_detail_still_works():
    """GET /chat/skills/{name} must still work for non-literal names."""
    client = _build_app()
    resp = client.get("/chat/skills/demo")
    assert resp.status_code == 200
    assert resp.json()["skill"]["name"] == "demo"

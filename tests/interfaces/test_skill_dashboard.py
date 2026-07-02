import pytest
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.skill_service import SkillScanReport, SkillScanWarning
from app.domain.skill import Skill, SkillFrontmatter, SkillNotFoundError, SkillReadiness
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


class _FakeSkillService:
    def __init__(self):
        self.skills = {"alpha": _skill("alpha")}

    async def list_skills(self, include_disabled=True):
        return list(self.skills.values())

    async def get(self, name):
        s = self.skills.get(name)
        if s is None:
            raise SkillNotFoundError(name)
        return s

    async def render_view(self, name, session_id=""):
        if name not in self.skills:
            return {"success": False, "error": "skill not found", "available": []}
        return {
            "success": True,
            "name": name,
            "content": "BODY",
            "description": "d",
            "readiness": "available",
            "linked_files": {},
        }

    async def create_skill(self, payload):
        self.skills[payload.name] = _skill(
            payload.name,
            enabled=payload.enabled,
            readiness=payload.readiness,
        )
        return self.skills[payload.name]

    async def update_skill(self, name, payload):
        if name not in self.skills:
            raise SkillNotFoundError(name)
        self.skills[name] = _skill(
            payload.name,
            enabled=payload.enabled,
            readiness=payload.readiness,
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
            skills_count=2,
            warnings=[SkillScanWarning("dup/SKILL.md", "duplicate_name", first_path="x")],
        )


def _build_app(skill_service):
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
        _Empty(), _Tools(), _Models(), lambda: {}, skill_service=skill_service,
    ))
    return TestClient(app)


def test_list_skills():
    client = _build_app(_FakeSkillService())
    res = client.get("/chat/skills")
    assert res.status_code == 200
    assert any(item["name"] == "alpha" for item in res.json()["skills"])


def test_get_skill_detail():
    client = _build_app(_FakeSkillService())
    res = client.get("/chat/skills/alpha")
    assert res.status_code == 200
    body = res.json()
    assert body["skill"]["name"] == "alpha"
    assert body["content"] == "BODY"


def test_get_skill_not_found():
    client = _build_app(_FakeSkillService())
    res = client.get("/chat/skills/missing")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "skill_not_found"


def test_patch_skill_enabled():
    client = _build_app(_FakeSkillService())
    res = client.patch("/chat/skills/alpha", json={"enabled": False})
    assert res.status_code == 200
    assert res.json()["enabled"] is False


def test_create_update_delete_skill_metadata():
    client = _build_app(_FakeSkillService())
    created = client.post(
        "/chat/skills",
        json={"name": "beta", "relative_path": "beta/SKILL.md", "description": "Beta"},
    )
    assert created.status_code == 200
    assert created.json()["name"] == "beta"

    patched = client.patch(
        "/chat/skills/beta",
        json={
            "name": "beta",
            "relative_path": "beta/SKILL.md",
            "description": "Beta 2",
            "enabled": False,
            "readiness": "setup_needed",
        },
    )
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False
    assert patched.json()["readiness"] == "setup_needed"

    deleted = client.delete("/chat/skills/beta")
    assert deleted.status_code == 204
    assert client.get("/chat/skills/beta").status_code == 404


def test_refresh_skills_returns_warnings():
    client = _build_app(_FakeSkillService())
    res = client.post("/chat/skills/refresh")
    assert res.status_code == 200
    body = res.json()
    assert body["skills_count"] == 2
    assert body["warnings"][0]["reason"] == "duplicate_name"

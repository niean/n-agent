import pytest
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.skill_service import SkillScanReport, SkillScanWarning
from app.domain.skill import Skill, SkillFrontmatter, SkillNotFoundError, SkillReadiness
from app.interfaces.http.dashboard import create_dashboard_router


def _skill(name, enabled=True, readiness=SkillReadiness.AVAILABLE, last_scan_status="ok", last_scan_error=None, chat_selectable=True):
    fm = SkillFrontmatter(
        name=name, description="d", version="", platforms=["linux"], tags=[],
        related_skills=[], author="", license="", setup_help=None,
        required_env_vars=[], raw={"name": name},
    )
    return Skill(
        id=f"id-{name}", name=name, relative_path=f"{name}/SKILL.md",
        description="d", platforms=["linux"], frontmatter=fm,
        enabled=enabled, readiness=readiness, last_scan_status=last_scan_status,
        last_scan_error=last_scan_error, last_seen_at=None,
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
        chat_selectable=chat_selectable,
    )


class _FakeSkillService:
    def __init__(self, skills=None):
        self.skills = skills if skills is not None else {"alpha": _skill("alpha")}

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

    async def set_chat_selectable(self, name, value):
        if name not in self.skills:
            raise SkillNotFoundError(name)
        from dataclasses import replace
        self.skills[name] = replace(self.skills[name], chat_selectable=bool(value))
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


def test_skill_dict_format_status_valid():
    client = _build_app(_FakeSkillService())
    item = next(i for i in client.get("/chat/skills").json()["skills"] if i["name"] == "alpha")
    assert item["format_status"] == "valid"
    assert item["format_messages"] == []


def test_skill_dict_format_status_warning_for_format_warning():
    svc = _FakeSkillService(skills={
        "fw": _skill("fw", last_scan_status="warning", last_scan_error="format_warning"),
    })
    item = next(i for i in _build_app(svc).get("/chat/skills").json()["skills"] if i["name"] == "fw")
    assert item["format_status"] == "warning"
    assert item["format_messages"] == ["format_warning"]


def test_skill_dict_format_status_warning_for_injection_warning():
    svc = _FakeSkillService(skills={
        "inj": _skill("inj", last_scan_status="warning", last_scan_error="injection_warning"),
    })
    item = next(i for i in _build_app(svc).get("/chat/skills").json()["skills"] if i["name"] == "inj")
    assert item["format_status"] == "warning"
    assert item["format_messages"] == ["injection_warning"]


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


def test_patch_skill_chat_selectable_true():
    client = _build_app(_FakeSkillService())
    res = client.patch("/chat/skills/alpha", json={"chat_selectable": True})
    assert res.status_code == 200
    assert res.json()["chat_selectable"] is True


def test_patch_skill_chat_selectable_false():
    client = _build_app(_FakeSkillService())
    res = client.patch("/chat/skills/alpha", json={"chat_selectable": False})
    assert res.status_code == 200
    assert res.json()["chat_selectable"] is False


def test_patch_skill_chat_selectable_422_when_not_bool():
    client = _build_app(_FakeSkillService())
    res = client.patch("/chat/skills/alpha", json={"chat_selectable": "yes"})
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "skill_invalid"


def test_patch_skill_chat_selectable_404_when_missing():
    client = _build_app(_FakeSkillService())
    res = client.patch("/chat/skills/ghost", json={"chat_selectable": False})
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "skill_not_found"


def test_patch_skill_with_extra_keys_falls_through_to_full_update():
    """`{chat_selectable, enabled}` must NOT match the single-field branch and should
    continue to the full SkillInput update path (regression guard)."""
    svc = _FakeSkillService()
    client = _build_app(svc)
    res = client.patch(
        "/chat/skills/alpha",
        json={"chat_selectable": False, "enabled": False},
    )
    # update_skill on the fake always sets chat_selectable=True (default). The
    # router reaches update_skill, not set_chat_selectable, and the value goes
    # through SkillInput(enabled=False) -> fake's _skill(enabled=False). The
    # stored chat_selectable is the default (True), proving we did not call
    # the dedicated endpoint.
    assert res.status_code == 200
    assert res.json()["chat_selectable"] is True
    assert res.json()["enabled"] is False


def test_list_skills_includes_chat_selectable():
    client = _build_app(_FakeSkillService())
    item = next(i for i in client.get("/chat/skills").json()["skills"] if i["name"] == "alpha")
    assert "chat_selectable" in item
    assert item["chat_selectable"] is True


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

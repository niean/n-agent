from __future__ import annotations

from types import SimpleNamespace

from app.interfaces.cli import main
from app.interfaces.cli.commands import skill


def _make_args(**kw):
    defaults = {
        "skill_command": None,
        "name": None,
        "pending_id": None,
        "json": False,
        "form": False,
        "yaml": False,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _enum(value):
    """Build a tiny enum-like object with a .value attribute."""
    return SimpleNamespace(value=value)


def _pending(
    pending_id="pid1",
    action="create",
    skill_name="demo",
    origin="foreground",
    summary="create demo",
    state="pending",
    diff="--- old\n+++ new\n@@ -1 +1 @@\n-old\n+new\n",
    created_at=None,
):
    return SimpleNamespace(
        pending_id=pending_id,
        action=_enum(action) if isinstance(action, str) else action,
        skill_name=skill_name,
        origin=_enum(origin) if isinstance(origin, str) else origin,
        summary=summary,
        diff=diff,
        state=state,
        created_at=created_at,
    )


def _manage_result(
    success=True,
    pending_id="pid1",
    skill_name="demo",
    action="create",
    summary="create demo",
    error=None,
):
    return SimpleNamespace(
        success=success,
        staged=False,
        pending_id=pending_id,
        skill_name=skill_name,
        action=_enum(action) if isinstance(action, str) else action,
        summary=summary,
        diff="--- old\n+++ new\n",
        error=error,
    )


def _usage(
    name="demo",
    created_by="foreground",
    state="active",
    pinned=False,
    use_count=0,
    view_count=0,
    patch_count=0,
):
    return (name, SimpleNamespace(
        created_by=created_by,
        state=state,
        pinned=pinned,
        use_count=use_count,
        view_count=view_count,
        patch_count=patch_count,
    ))


class _FakeSkillService:
    """Fake service supporting legacy list/view and new pending/usage ops."""

    def __init__(self):
        self.approved: list[str] = []
        self.rejected: list[str] = []
        self.approved_all = False
        self.rejected_all = False
        self.pinned: list[tuple[str, bool]] = []
        self._pending_list = [_pending()]
        self._usage_list = [_usage()]

    # -- legacy --
    async def list_skills(self, include_disabled=True):
        return []

    async def render_view(self, name, session_id=""):
        return {"success": True, "content": ""}

    # -- pending --
    async def list_pending(self):
        return list(self._pending_list)

    async def get_pending(self, pending_id):
        for pw in self._pending_list:
            if pw.pending_id == pending_id:
                return pw
        return None

    async def approve_pending(self, pending_id):
        self.approved.append(pending_id)
        return _manage_result(pending_id=pending_id)

    async def reject_pending(self, pending_id):
        self.rejected.append(pending_id)
        return True

    async def approve_all_pending(self):
        self.approved_all = True
        return 1

    async def reject_all_pending(self):
        self.rejected_all = True
        return 1

    # -- usage --
    async def list_usage(self):
        return list(self._usage_list)

    async def set_pinned(self, name, pinned):
        self.pinned.append((name, pinned))


# ------------------------------------------------------------------
# help test (exercises argparse registration via main())
# ------------------------------------------------------------------

def test_skill_pending_subcommand_help():
    rc = main(["skill", "pending", "--help"])
    assert rc == 0


def test_skill_approve_subcommand_help():
    rc = main(["skill", "approve", "--help"])
    assert rc == 0


# ------------------------------------------------------------------
# pending list
# ------------------------------------------------------------------

def test_skill_pending_list_returns_zero(monkeypatch):
    fake = _FakeSkillService()
    monkeypatch.setattr(skill, "_load_skill_service", lambda: fake)
    args = _make_args(skill_command="pending")
    rc = skill.run(args)
    assert rc == 0


# ------------------------------------------------------------------
# diff
# ------------------------------------------------------------------

def test_skill_diff_returns_zero(monkeypatch):
    fake = _FakeSkillService()
    monkeypatch.setattr(skill, "_load_skill_service", lambda: fake)
    args = _make_args(skill_command="diff", pending_id="pid1")
    rc = skill.run(args)
    assert rc == 0


def test_skill_diff_not_found_returns_one(monkeypatch):
    fake = _FakeSkillService()
    monkeypatch.setattr(skill, "_load_skill_service", lambda: fake)
    args = _make_args(skill_command="diff", pending_id="missing")
    rc = skill.run(args)
    assert rc == 1


# ------------------------------------------------------------------
# approve
# ------------------------------------------------------------------

def test_skill_approve_calls_service(monkeypatch):
    fake = _FakeSkillService()
    monkeypatch.setattr(skill, "_load_skill_service", lambda: fake)
    args = _make_args(skill_command="approve", pending_id="pid1")
    rc = skill.run(args)
    assert rc == 0
    assert fake.approved == ["pid1"]


def test_skill_approve_failure_returns_one(monkeypatch):
    class _FailFake(_FakeSkillService):
        async def approve_pending(self, pending_id):
            self.approved.append(pending_id)
            return _manage_result(
                success=False, pending_id=pending_id,
                error="pending_not_found_or_taken",
            )

    fake = _FailFake()
    monkeypatch.setattr(skill, "_load_skill_service", lambda: fake)
    args = _make_args(skill_command="approve", pending_id="pid1")
    rc = skill.run(args)
    assert rc == 1


# ------------------------------------------------------------------
# reject
# ------------------------------------------------------------------

def test_skill_reject_calls_service(monkeypatch):
    fake = _FakeSkillService()
    monkeypatch.setattr(skill, "_load_skill_service", lambda: fake)
    args = _make_args(skill_command="reject", pending_id="pid1")
    rc = skill.run(args)
    assert rc == 0
    assert fake.rejected == ["pid1"]


# ------------------------------------------------------------------
# approve-all
# ------------------------------------------------------------------

def test_skill_approve_all_calls_service(monkeypatch):
    fake = _FakeSkillService()
    monkeypatch.setattr(skill, "_load_skill_service", lambda: fake)
    args = _make_args(skill_command="approve-all")
    rc = skill.run(args)
    assert rc == 0
    assert fake.approved_all


# ------------------------------------------------------------------
# reject-all
# ------------------------------------------------------------------

def test_skill_reject_all_calls_service(monkeypatch):
    fake = _FakeSkillService()
    monkeypatch.setattr(skill, "_load_skill_service", lambda: fake)
    args = _make_args(skill_command="reject-all")
    rc = skill.run(args)
    assert rc == 0
    assert fake.rejected_all


# ------------------------------------------------------------------
# pin / unpin
# ------------------------------------------------------------------

def test_skill_pin_calls_service(monkeypatch):
    fake = _FakeSkillService()
    monkeypatch.setattr(skill, "_load_skill_service", lambda: fake)
    args = _make_args(skill_command="pin", name="demo")
    rc = skill.run(args)
    assert rc == 0
    assert fake.pinned == [("demo", True)]


def test_skill_unpin_calls_service(monkeypatch):
    fake = _FakeSkillService()
    monkeypatch.setattr(skill, "_load_skill_service", lambda: fake)
    args = _make_args(skill_command="unpin", name="demo")
    rc = skill.run(args)
    assert rc == 0
    assert fake.pinned == [("demo", False)]


# ------------------------------------------------------------------
# usage
# ------------------------------------------------------------------

def test_skill_usage_returns_zero(monkeypatch):
    fake = _FakeSkillService()
    monkeypatch.setattr(skill, "_load_skill_service", lambda: fake)
    args = _make_args(skill_command="usage")
    rc = skill.run(args)
    assert rc == 0

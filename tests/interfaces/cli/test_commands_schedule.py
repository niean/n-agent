from __future__ import annotations

import json
from types import SimpleNamespace

from app.interfaces.cli.commands import schedule as schedule_cmd


class _FakeSchedule:
    def __init__(self):
        self.created_input = None
        self.updated: list[tuple[str, dict]] = []
        self.paused: list[str] = []
        self.resumed: list[str] = []
        self.run_now_called: list[str] = []
        self.deleted: list[str] = []
        self.delete_result = True
        self.executions_called: list[tuple[str, int]] = []

    async def list(self):
        from app.domain.schedule import ScheduledTaskStatus, ScheduleExpression, ScheduleTimezone, DeliveryTarget, DeliveryTargetType
        from datetime import datetime, timezone
        return [SimpleNamespace(
            id="t1", name="T1", prompt="p",
            schedule=ScheduleExpression("*/5 * * * *"),
            timezone=ScheduleTimezone("Asia/Shanghai"),
            session_id="sess-1",
            origin={},
            delivery_target=DeliveryTarget(DeliveryTargetType.DASHBOARD),
            next_run_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            status=ScheduledTaskStatus.ACTIVE,
            enabled=True,
        )]

    async def get(self, tid):
        if tid == "missing":
            from app.application.schedule_service import ScheduledTaskNotFoundError
            raise ScheduledTaskNotFoundError(tid)
        items = await self.list()
        return items[0]

    async def create(self, payload):
        self.created_input = payload
        return SimpleNamespace(id="t2", name=payload.name, prompt=payload.prompt)

    async def update(self, tid, payload):
        self.updated.append((tid, payload.__dict__))
        return SimpleNamespace(id=tid, name=payload.name or "T1")

    async def pause(self, tid):
        self.paused.append(tid)
        return SimpleNamespace(id=tid)

    async def resume(self, tid):
        self.resumed.append(tid)
        return SimpleNamespace(id=tid)

    async def run_now(self, tid):
        self.run_now_called.append(tid)
        return {"status": "triggered", "claim_id": "c1"}

    async def delete(self, tid):
        self.deleted.append(tid)
        return self.delete_result

    async def list_executions(self, tid, limit=10):
        self.executions_called.append((tid, limit))
        return [
            SimpleNamespace(
                id="e1",
                task_id=tid,
                session_id="s1",
                claim_id="c1",
                lease_owner="o1",
                status=SimpleNamespace(value="succeeded"),
                claimed_next_run_at=None,
                started_at=None,
                completed_at=None,
                output="ok",
                error=None,
                delivery_status=None,
                delivery_error=None,
                created_at=None,
            )
        ]


def _args(**kw):
    base = {"schedule_command": None, "json": False, "form": False, "yaml": False, "id": None, "name": None,
            "cron": None, "prompt": None, "timezone": None,
            "delivery_target": None, "limit": None,
            "no_wait": False, "timeout": None}
    base.update(kw)
    return SimpleNamespace(**base)


def test_schedule_list(monkeypatch, capsys):
    fake = _FakeSchedule()
    monkeypatch.setattr(schedule_cmd, "_load_schedule_service", lambda: fake)
    rc = schedule_cmd.run(_args(schedule_command="list"))
    assert rc == 0
    assert "t1" in capsys.readouterr().out


def test_schedule_create(monkeypatch, capsys):
    fake = _FakeSchedule()
    monkeypatch.setattr(schedule_cmd, "_load_schedule_service", lambda: fake)
    rc = schedule_cmd.run(_args(schedule_command="create", name="N", cron="*/5 * * * *", prompt="hi"))
    assert rc == 0
    assert fake.created_input.name == "N"
    assert fake.created_input.cron_expression == "*/5 * * * *"
    # 不携带 gateway.platform trusted_metadata
    assert "gateway" not in fake.created_input.origin
    assert "platform" not in fake.created_input.origin


def test_schedule_create_no_origin_no_session_id(monkeypatch, capsys):
    fake = _FakeSchedule()
    monkeypatch.setattr(schedule_cmd, "_load_schedule_service", lambda: fake)
    rc = schedule_cmd.run(_args(schedule_command="create", name="N", cron="*/5 * * * *", prompt="hi"))
    assert rc == 0
    # origin 用默认空 dict，session_id 不传（service 自建）
    assert fake.created_input.origin == {}
    assert fake.created_input.session_id is None


def test_schedule_delete_false_returns_error(monkeypatch, capsys):
    fake = _FakeSchedule()
    fake.delete_result = False
    monkeypatch.setattr(schedule_cmd, "_load_schedule_service", lambda: fake)
    rc = schedule_cmd.run(_args(schedule_command="delete", id="t1"))
    assert rc == 1


def test_schedule_delete_true(monkeypatch, capsys):
    fake = _FakeSchedule()
    monkeypatch.setattr(schedule_cmd, "_load_schedule_service", lambda: fake)
    rc = schedule_cmd.run(_args(schedule_command="delete", id="t1"))
    assert rc == 0
    assert fake.deleted == ["t1"]


def test_schedule_executions_limit_too_low_returns_2(monkeypatch, capsys):
    fake = _FakeSchedule()
    monkeypatch.setattr(schedule_cmd, "_load_schedule_service", lambda: fake)
    rc = schedule_cmd.run(_args(schedule_command="executions", id="t1", limit=0))
    assert rc == 2


def test_schedule_executions_limit_too_high_returns_2(monkeypatch, capsys):
    fake = _FakeSchedule()
    monkeypatch.setattr(schedule_cmd, "_load_schedule_service", lambda: fake)
    rc = schedule_cmd.run(_args(schedule_command="executions", id="t1", limit=100))
    assert rc == 2


def test_schedule_executions_valid_limit(monkeypatch, capsys):
    fake = _FakeSchedule()
    monkeypatch.setattr(schedule_cmd, "_load_schedule_service", lambda: fake)
    rc = schedule_cmd.run(_args(schedule_command="executions", id="t1", limit=20))
    assert rc == 0
    assert fake.executions_called == [("t1", 20)]


def test_schedule_pause_resume_run(monkeypatch, capsys):
    fake = _FakeSchedule()
    monkeypatch.setattr(schedule_cmd, "_load_schedule_service", lambda: fake)
    assert schedule_cmd.run(_args(schedule_command="pause", id="t1")) == 0
    assert schedule_cmd.run(_args(schedule_command="resume", id="t1")) == 0
    assert schedule_cmd.run(_args(schedule_command="run", id="t1", no_wait=True)) == 0
    assert fake.paused == ["t1"]
    assert fake.resumed == ["t1"]
    assert fake.run_now_called == ["t1"]


def test_schedule_run_waits_for_completion(monkeypatch, capsys):
    fake = _FakeSchedule()
    monkeypatch.setattr(schedule_cmd, "_load_schedule_service", lambda: fake)
    rc = schedule_cmd.run(_args(schedule_command="run", id="t1", timeout=5))
    assert rc == 0
    out = capsys.readouterr().out
    assert "succeeded" in out
    assert fake.run_now_called == ["t1"]
    assert len(fake.executions_called) >= 1


def test_schedule_run_no_wait_skips_polling(monkeypatch, capsys):
    fake = _FakeSchedule()
    monkeypatch.setattr(schedule_cmd, "_load_schedule_service", lambda: fake)
    rc = schedule_cmd.run(_args(schedule_command="run", id="t1", no_wait=True))
    assert rc == 0
    assert fake.executions_called == []


def test_schedule_run_not_claimed_returns_1(monkeypatch, capsys):
    fake = _FakeSchedule()
    fake.run_now_result = {"status": "not_claimed"}

    async def _run_now(tid):
        fake.run_now_called.append(tid)
        return fake.run_now_result

    fake.run_now = _run_now
    monkeypatch.setattr(schedule_cmd, "_load_schedule_service", lambda: fake)
    rc = schedule_cmd.run(_args(schedule_command="run", id="t1", no_wait=True))
    assert rc == 1
    assert "not_claimed" in capsys.readouterr().out

from __future__ import annotations

import json
from types import SimpleNamespace

from app.interfaces.cli.commands import knowledge


class _FakeKB:
    def __init__(self):
        self.created = None
        self.probed: list[str] = []
        self.updated: list[tuple[str, dict]] = []
        self.deleted: list[str] = []

    async def list_bases(self):
        return [SimpleNamespace(
            id="kb1", name="K1", base_type="n_kb", base_url="http://x",
            dataset_id="d1", enabled=True, api_key_present=True,
            last_probe_status="ok", last_probe_error="",
            default_top_k=None, default_min_score=None,
        )]

    async def get_base(self, kid):
        if kid == "missing":
            from app.domain.knowledge import KnowledgeBaseNotFoundError
            raise KnowledgeBaseNotFoundError(kid)
        return SimpleNamespace(
            id=kid, name="K1", base_type="n_kb", base_url="http://x",
            dataset_id="d1", enabled=True, api_key_present=True,
            last_probe_status="ok", last_probe_error="",
            default_top_k=None, default_min_score=None,
        )

    async def create_base(self, payload):
        self.created = payload
        return SimpleNamespace(
            id=payload.id, name=payload.name, base_type=payload.base_type,
            base_url=payload.base_url, dataset_id=payload.dataset_id,
            enabled=payload.enabled, api_key_present=bool(payload.api_key),
            last_probe_status="", last_probe_error="",
            default_top_k=payload.default_top_k, default_min_score=payload.default_min_score,
        )

    async def update_base(self, kid, payload):
        self.updated.append((kid, payload.__dict__))
        return SimpleNamespace(
            id=kid, name=payload.name or "K1", base_type="n_kb", base_url="http://x",
            dataset_id="d1", enabled=True, api_key_present=True,
            last_probe_status="ok", last_probe_error="",
            default_top_k=None, default_min_score=None,
        )

    async def delete_base(self, kid):
        self.deleted.append(kid)
        return None

    async def probe_base(self, kid):
        self.probed.append(kid)
        return None


def _args(**kw):
    base = {"knowledge_command": None, "json": False, "form": False, "yaml": False, "id": None, "name": None,
            "description": None, "base_type": None, "base_url": None,
            "dataset_id": None, "api_key": None, "enabled": True,
            "default_top_k": None, "default_min_score": None,
            "clear_default_top_k": False, "clear_default_min_score": False}
    base.update(kw)
    return SimpleNamespace(**base)


def test_kb_list(monkeypatch, capsys):
    fake = _FakeKB()
    monkeypatch.setattr(knowledge, "_load_knowledge_service", lambda: fake)
    rc = knowledge.run(_args(knowledge_command="list"))
    assert rc == 0
    assert "kb1" in capsys.readouterr().out


def test_kb_create_requires_id_description_dataset(monkeypatch, capsys):
    fake = _FakeKB()
    monkeypatch.setattr(knowledge, "_load_knowledge_service", lambda: fake)
    rc = knowledge.run(_args(knowledge_command="create", name="K", base_type="n_kb",
                             base_url="http://x"))
    assert rc == 2


def test_kb_create_full(monkeypatch, capsys):
    fake = _FakeKB()
    monkeypatch.setattr(knowledge, "_load_knowledge_service", lambda: fake)
    rc = knowledge.run(_args(knowledge_command="create", id="kb2", name="K",
                             description="d", base_type="n_kb",
                             base_url="http://x", dataset_id="d1", api_key="k"))
    assert rc == 0
    assert fake.created.id == "kb2"
    assert fake.created.description == "d"
    assert fake.created.dataset_id == "d1"
    assert fake.created.enabled is True


def test_kb_create_disabled_flag(monkeypatch, capsys):
    fake = _FakeKB()
    monkeypatch.setattr(knowledge, "_load_knowledge_service", lambda: fake)
    rc = knowledge.run(_args(knowledge_command="create", id="kb2", name="K",
                             description="d", base_type="n_kb",
                             base_url="http://x", dataset_id="d1", enabled=False))
    assert rc == 0
    assert fake.created.enabled is False


def test_kb_probe_persisted(monkeypatch, capsys):
    fake = _FakeKB()
    monkeypatch.setattr(knowledge, "_load_knowledge_service", lambda: fake)
    rc = knowledge.run(_args(knowledge_command="probe", id="kb1"))
    assert rc == 0
    assert fake.probed == ["kb1"]


def test_kb_list_json_no_secret(monkeypatch, capsys):
    fake = _FakeKB()
    monkeypatch.setattr(knowledge, "_load_knowledge_service", lambda: fake)
    rc = knowledge.run(_args(knowledge_command="list", json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert "api_key" not in data[0]
    assert data[0]["api_key_present"] is True


def test_kb_update_clear_default_top_k(monkeypatch, capsys):
    fake = _FakeKB()
    monkeypatch.setattr(knowledge, "_load_knowledge_service", lambda: fake)
    rc = knowledge.run(_args(knowledge_command="update", id="kb1",
                             clear_default_top_k=True))
    assert rc == 0
    assert fake.updated[-1][1]["clear_default_top_k"] is True


def test_kb_get_not_found(monkeypatch, capsys):
    fake = _FakeKB()
    monkeypatch.setattr(knowledge, "_load_knowledge_service", lambda: fake)
    rc = knowledge.run(_args(knowledge_command="get", id="missing"))
    assert rc == 1


def test_kb_delete(monkeypatch, capsys):
    fake = _FakeKB()
    monkeypatch.setattr(knowledge, "_load_knowledge_service", lambda: fake)
    rc = knowledge.run(_args(knowledge_command="delete", id="kb1"))
    assert rc == 0
    assert fake.deleted == ["kb1"]

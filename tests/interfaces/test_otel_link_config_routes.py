"""OTel link config Dashboard API tests -- GET/PATCH /chat/observations/otel/links.

Covers (plan T5): GET no-row env base + version=0 + null metadata; DTO exact
shape; PATCH success round-trip; the full 422 parameter matrix (non-object
body, malformed JSON, missing/bool/float/str/null expected_version, empty
patch, unknown field, forged updated_by, non-str non-null values); null
clears an override; empty string disables a link; CAS 409; store failure
500; every response Cache-Control: no-store; error bodies never echo
submitted URL secrets.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.otel_link_config_service import OtelLinkConfigService
from app.domain.otel_link_config import (
    OTEL_LINK_CONFIG_FIELDS,
    OtelLinkConfigConflictError,
    OtelLinkConfigStoreError,
)
from app.infrastructure.registry.sqlite_otel_link_config_store import (
    SqliteOtelLinkConfigStore,
)
from app.interfaces.http.otel_link_config_routes import register_otel_link_config_routes

PATH = "/chat/observations/otel/links"

_ENV = {
    "metric_base_url": "https://grafana.example.com/d/metrics",
    "log_base_url": "https://grafana.example.com/d/logs",
    "trace_base_url": "https://jaeger.example.com",
    "profile_base_url": "https://grafana.example.com/d/profile",
}


def _app(tmp_path):
    store = SqliteOtelLinkConfigStore(str(tmp_path / "sessions.db"))
    svc = OtelLinkConfigService(store, **_ENV)
    app = FastAPI()
    register_otel_link_config_routes(app.router, svc)
    return app, svc


def _stub_app(service):
    app = FastAPI()
    register_otel_link_config_routes(app.router, service)
    return app


class _FailingService:
    """Both management paths fail with StoreError."""

    async def get_resolved(self):
        raise OtelLinkConfigStoreError("db corrupt")

    async def update(self, partial, expected_version, updated_by):
        raise OtelLinkConfigStoreError("db corrupt")


class _UnexpectedErrorService:
    """Both management paths raise a non-domain exception."""

    async def get_resolved(self):
        raise RuntimeError("boom-with-internal-detail")

    async def update(self, partial, expected_version, updated_by):
        raise RuntimeError("boom-with-internal-detail")


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------


def test_get_no_row_returns_env_base_version_zero(tmp_path):
    app, _ = _app(tmp_path)
    r = TestClient(app).get(PATH)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    data = r.json()
    assert set(data) == {"config", "version", "overridden_fields", "updated_at", "updated_by"}
    assert set(data["config"]) == set(OTEL_LINK_CONFIG_FIELDS)
    assert data["config"] == _ENV
    assert data["version"] == 0
    assert data["overridden_fields"] == []
    assert data["updated_at"] is None
    assert data["updated_by"] is None


def test_get_store_error_500(tmp_path):
    app = _stub_app(_FailingService())
    r = TestClient(app, raise_server_exceptions=False).get(PATH)
    assert r.status_code == 500
    assert r.headers["cache-control"] == "no-store"
    assert r.json()["error"]["code"] == "otel_link_config_load_failed"
    assert "corrupt" not in r.text


def test_get_unexpected_error_500_no_store():
    app = _stub_app(_UnexpectedErrorService())
    r = TestClient(app, raise_server_exceptions=False).get(PATH)
    assert r.status_code == 500
    assert r.headers["cache-control"] == "no-store"
    assert r.json()["error"]["code"] == "otel_link_config_load_failed"
    assert "boom-with-internal-detail" not in r.text


# ---------------------------------------------------------------------------
# PATCH success
# ---------------------------------------------------------------------------


def test_patch_first_write_success_round_trip(tmp_path):
    app, _ = _app(tmp_path)
    client = TestClient(app)
    r = client.patch(PATH, json={
        "expected_version": 0,
        "trace_base_url": "https://jaeger.internal.example.com",
    })
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    data = r.json()
    assert data["version"] == 1
    assert data["config"]["trace_base_url"] == "https://jaeger.internal.example.com"
    assert data["config"]["metric_base_url"] == _ENV["metric_base_url"]  # untouched
    assert data["overridden_fields"] == ["trace_base_url"]
    assert data["updated_by"] == "dashboard-local"
    assert data["updated_at"] is not None

    g = client.get(PATH)
    assert g.status_code == 200
    assert g.json() == data


def test_patch_null_clears_override(tmp_path):
    app, _ = _app(tmp_path)
    client = TestClient(app)
    client.patch(PATH, json={"expected_version": 0, "trace_base_url": "https://jaeger2.example.com"})
    r = client.patch(PATH, json={"expected_version": 1, "trace_base_url": None})
    assert r.status_code == 200
    data = r.json()
    assert data["version"] == 2
    assert data["config"]["trace_base_url"] == _ENV["trace_base_url"]
    assert data["overridden_fields"] == []


def test_patch_empty_string_disables_link(tmp_path):
    app, _ = _app(tmp_path)
    r = TestClient(app).patch(PATH, json={"expected_version": 0, "log_base_url": ""})
    assert r.status_code == 200
    data = r.json()
    assert data["config"]["log_base_url"] == ""
    assert "log_base_url" in data["overridden_fields"]


def test_patch_conflict_409(tmp_path):
    app, _ = _app(tmp_path)
    client = TestClient(app)
    client.patch(PATH, json={"expected_version": 0, "trace_base_url": "https://jaeger2.example.com"})
    r = client.patch(PATH, json={"expected_version": 0, "trace_base_url": "https://jaeger3.example.com"})
    assert r.status_code == 409
    assert r.headers["cache-control"] == "no-store"
    assert r.json()["error"]["code"] == "otel_link_config_conflict"


def test_patch_store_error_500(tmp_path):
    app = _stub_app(_FailingService())
    r = TestClient(app, raise_server_exceptions=False).patch(
        PATH, json={"expected_version": 0, "trace_base_url": "https://jaeger2.example.com"}
    )
    assert r.status_code == 500
    assert r.headers["cache-control"] == "no-store"
    assert r.json()["error"]["code"] == "otel_link_config_load_failed"


def test_patch_unexpected_error_500_no_store():
    app = _stub_app(_UnexpectedErrorService())
    r = TestClient(app, raise_server_exceptions=False).patch(
        PATH, json={"expected_version": 0, "trace_base_url": "https://jaeger2.example.com"}
    )
    assert r.status_code == 500
    assert r.headers["cache-control"] == "no-store"
    assert r.json()["error"]["code"] == "otel_link_config_load_failed"
    assert "boom-with-internal-detail" not in r.text


# ---------------------------------------------------------------------------
# PATCH 422 parameter matrix
# ---------------------------------------------------------------------------


def _assert_422(resp):
    assert resp.status_code == 422
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert body["error"]["code"] == "otel_link_config_invalid"
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


def test_patch_non_object_body_422(tmp_path):
    app, _ = _app(tmp_path)
    client = TestClient(app)
    _assert_422(client.patch(PATH, json=[1, 2, 3]))
    _assert_422(client.patch(PATH, json="just-a-string"))
    _assert_422(client.patch(PATH, json=42))


def test_patch_malformed_json_422(tmp_path):
    app, _ = _app(tmp_path)
    r = TestClient(app).patch(
        PATH, content=b"{not valid json", headers={"Content-Type": "application/json"}
    )
    _assert_422(r)


@pytest.mark.parametrize("expected_version", [True, False, 1.5, "0", None])
def test_patch_bad_expected_version_422(tmp_path, expected_version):
    app, _ = _app(tmp_path)
    r = TestClient(app).patch(PATH, json={
        "expected_version": expected_version, "trace_base_url": "https://jaeger2.example.com"
    })
    _assert_422(r)


def test_patch_missing_expected_version_422(tmp_path):
    app, _ = _app(tmp_path)
    r = TestClient(app).patch(PATH, json={"trace_base_url": "https://jaeger2.example.com"})
    _assert_422(r)


def test_patch_negative_expected_version_422(tmp_path):
    app, _ = _app(tmp_path)
    r = TestClient(app).patch(PATH, json={
        "expected_version": -1, "trace_base_url": "https://jaeger2.example.com"
    })
    _assert_422(r)


def test_patch_empty_patch_422(tmp_path):
    app, _ = _app(tmp_path)
    r = TestClient(app).patch(PATH, json={"expected_version": 0})
    _assert_422(r)


def test_patch_unknown_field_422(tmp_path):
    app, _ = _app(tmp_path)
    r = TestClient(app).patch(PATH, json={"expected_version": 0, "evil_field": "x"})
    _assert_422(r)
    # attacker key must not be echoed back
    assert "evil_field" not in r.text


def test_patch_rejects_body_updated_by(tmp_path):
    app, _ = _app(tmp_path)
    r = TestClient(app).patch(PATH, json={
        "expected_version": 0,
        "trace_base_url": "https://jaeger2.example.com",
        "updated_by": "attacker",
    })
    _assert_422(r)


@pytest.mark.parametrize("value", [123, 1.5, True, ["https://x.example.com"], {"u": 1}])
def test_patch_non_str_non_null_value_422(tmp_path, value):
    app, _ = _app(tmp_path)
    r = TestClient(app).patch(PATH, json={"expected_version": 0, "trace_base_url": value})
    _assert_422(r)


def test_patch_invalid_url_422_and_no_secret_echo(tmp_path):
    app, _ = _app(tmp_path)
    secret_url = "https://evil.example.com/path?api_key=SECRET-TOKEN-123#frag"
    r = TestClient(app).patch(PATH, json={"expected_version": 0, "metric_base_url": secret_url})
    _assert_422(r)
    assert "SECRET-TOKEN-123" not in r.text
    assert secret_url not in r.text
    # message carries the safe field name
    assert "metric_base_url" in r.json()["error"]["message"]


def test_patch_invalid_url_not_persisted(tmp_path):
    app, _ = _app(tmp_path)
    client = TestClient(app)
    client.patch(PATH, json={"expected_version": 0, "metric_base_url": "ftp://bad"})
    g = client.get(PATH)
    assert g.json()["version"] == 0
    assert g.json()["config"]["metric_base_url"] == _ENV["metric_base_url"]

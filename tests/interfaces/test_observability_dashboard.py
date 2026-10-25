"""T8 observability dashboard self-check: API + page + profiler wiring tests.

Covers (plan T8 S1):
- ``GET /chat/observations/otel`` returns the injected snapshot as a
  read-only diagnostic (HTTP 200, ``Cache-Control: no-store``) with the
  contract fields (status/mode/instance_id/sample ratio/signal states/
  instrument list/restricted spans/delivery/semconv version);
- sanitization: top-level whitelist (no full config), string caps, no
  credentials/raw exceptions; exporter endpoint reduced to host:port;
- external links built from the injected base_urls only: http/https
  only, no userinfo, configured subpath preserved, service/time params
  URL-encoded, hidden when unconfigured;
- the self-check route is excluded from automatic spans (T4 contract);
- ``/observations/otel`` page shell + static frontend contracts
  (independent routeConfig entry, observations subdomain scope,
  textContent-only rendering, app.js renderer dispatch);
- composition-root profiler wiring: profiling_enabled starts the PID
  sampler after construction and registers it with the handle, shared
  by the HTTP lifespan and application_runtime; disabled wires nothing.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.interfaces.http.dashboard import STATIC_DIR, create_dashboard_router

REPO_ROOT = Path(__file__).resolve().parents[2]
NAV_JS = STATIC_DIR / "management-navigation.js"
APP_JS = STATIC_DIR / "app.js"
OTEL_JS = STATIC_DIR / "observations-otel.js"
INDEX_HTML = STATIC_DIR / "index.html"


def _fake_snapshot() -> dict:
    return {
        "status": "healthy",
        "mode": "otlp",
        "instance_id": "inst-abc123",
        "sample_ratio": 0.25,
        "semconv_version": "1.37.0 (genai draft)",
        "instruments": [
            {
                "name": "gen_ai.client.token.usage",
                "kind": "histogram",
                "unit": "{token}",
                "allowed_dimensions": ["gen_ai.operation.name"],
            }
        ],
        "signals": {
            "traces": {
                "status": "healthy",
                "delivery": "succeeded",
                "last_success": "2026-10-25T00:00:00+00:00",
                "last_failure": None,
                "failure_count": 2,
                "current_error": None,
            },
            "metrics": {
                "status": "healthy",
                "delivery": "unknown",
                "last_success": None,
                "last_failure": None,
                "failure_count": 0,
                "current_error": None,
            },
            "logs": {
                "status": "healthy",
                "delivery": "unknown",
                "last_success": None,
                "last_failure": None,
                "failure_count": 0,
                "current_error": None,
            },
            "profile": {
                "status": "disabled",
                "delivery": "disabled",
                "last_success": None,
                "last_failure": None,
                "failure_count": 0,
                "current_error": None,
            },
        },
        "spans": [
            {
                "name": "chat {model}",
                "trace_id": "a" * 32,
                "span_id": "b" * 16,
                "duration_ms": 12.5,
                "status": "OK",
                "attributes": {"gen_ai.request.model": "gpt-4o"},
            }
        ],
        "dropped_after_deadline": 0,
        # Must be dropped by the API whitelist: never echo unknown snapshot
        # keys (a hostile/buggy snapshot could carry full config).
        "full_config": {"provider_api_key": "sk-super-secret"},
        "credential_token": "credential-token-xyz",
    }


def _build_client(
    snapshot=None,
    *,
    grafana_metrics: str = "",
    grafana_logs: str = "",
    grafana_profile: str = "",
    jaeger: str = "",
    settings=None,
    service_name: str = "n-agent",
    otel_link_config_service=None,
) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_dashboard_router(
            SimpleNamespace(),  # session_service (unused on these routes)
            SimpleNamespace(),  # tool_service
            SimpleNamespace(),  # model_service
            lambda: {},  # health_provider
            observability_snapshot=snapshot,
            otel_grafana_metrics_base_url=grafana_metrics,
            otel_grafana_logs_base_url=grafana_logs,
            otel_grafana_profile_base_url=grafana_profile,
            otel_jaeger_base_url=jaeger,
            otel_service_name=service_name,
            settings=settings,
            otel_link_config_service=otel_link_config_service,
        )
    )
    return TestClient(app)


def _iter_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _iter_strings(k)
            yield from _iter_strings(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)


# ---------------------------------------------------------------------------
# API contract
# ---------------------------------------------------------------------------


def test_otel_api_returns_snapshot_contract():
    client = _build_client(snapshot=_fake_snapshot)
    r = client.get("/chat/observations/otel")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    data = r.json()
    assert data["status"] == "healthy"
    assert data["mode"] == "otlp"
    assert data["instance_id"] == "inst-abc123"
    assert data["sample_ratio"] == 0.25
    assert data["semconv_version"] == "1.37.0 (genai draft)"
    assert isinstance(data["instruments"], list) and data["instruments"]
    assert data["instruments"][0]["name"] == "gen_ai.client.token.usage"
    assert isinstance(data["signals"], dict)
    assert set(data["signals"]) == {"traces", "metrics", "logs", "profile"}
    traces = data["signals"]["traces"]
    assert traces["delivery"] == "succeeded"
    assert traces["failure_count"] == 2
    assert traces["last_success"] == "2026-10-25T00:00:00+00:00"
    assert data["signals"]["profile"]["delivery"] == "disabled"
    assert isinstance(data["spans"], list) and data["spans"]
    assert data["spans"][0]["name"] == "chat {model}"
    assert "dropped_after_deadline" in data


def test_otel_api_is_read_only_get_only():
    client = _build_client(snapshot=_fake_snapshot)
    assert client.post("/chat/observations/otel").status_code in (405, 400)
    assert client.delete("/chat/observations/otel").status_code == 405


def test_otel_api_sanitizes_unknown_keys_and_long_strings():
    long_error = "boom-" + "x" * 500
    snapshot = _fake_snapshot()
    snapshot["signals"]["traces"]["current_error"] = long_error
    snapshot["spans"][0]["attributes"]["gen_ai.request.model"] = "m" * 500
    client = _build_client(snapshot=lambda: snapshot)
    r = client.get("/chat/observations/otel")
    assert r.status_code == 200
    data = r.json()
    # Top-level whitelist: unknown snapshot keys never reach the response.
    assert "full_config" not in data
    assert "credential_token" not in data
    assert "sk-super-secret" not in r.text
    assert "credential-token-xyz" not in r.text
    # All strings are capped at 256 chars (no raw exceptions / payloads).
    assert all(len(s) <= 256 for s in _iter_strings(data))
    traces = data["signals"]["traces"]
    assert len(traces["current_error"]) == 256


def test_otel_api_read_failure_reports_degraded_without_raw_error():
    def broken_snapshot():
        raise RuntimeError("secret internals exploded")

    client = _build_client(snapshot=broken_snapshot)
    r = client.get("/chat/observations/otel")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "degraded"
    assert "secret internals" not in r.text


def test_otel_api_without_snapshot_reports_disabled():
    client = _build_client()
    r = client.get("/chat/observations/otel")
    assert r.status_code == 200
    assert r.json()["status"] == "disabled"
    assert r.headers["cache-control"] == "no-store"


def test_otel_api_exporter_endpoint_reduced_to_host_port():
    # Settings validation forbids credentials, but the sanitizer must not
    # trust that: strip scheme/userinfo/path/query and keep host:port only.
    endpoint_settings = SimpleNamespace(
        otel_exporter_otlp_endpoint="https://user:secret@collector.internal:4317/path?q=1"
    )
    client = _build_client(snapshot=_fake_snapshot, settings=endpoint_settings)
    data = client.get("/chat/observations/otel").json()
    assert data["exporter_endpoint"] == "collector.internal:4317"
    assert "secret" not in json.dumps(data)
    assert "user" not in data["exporter_endpoint"]

    # IPv6 hosts render unambiguously with brackets.
    ipv6_settings = SimpleNamespace(otel_exporter_otlp_endpoint="http://[::1]:4318")
    client = _build_client(snapshot=_fake_snapshot, settings=ipv6_settings)
    assert (
        client.get("/chat/observations/otel").json()["exporter_endpoint"]
        == "[::1]:4318"
    )

    empty_settings = SimpleNamespace(otel_exporter_otlp_endpoint="")
    client = _build_client(snapshot=_fake_snapshot, settings=empty_settings)
    assert client.get("/chat/observations/otel").json()["exporter_endpoint"] is None


def test_otel_api_replaces_non_finite_floats_to_keep_json_strict():
    snapshot = _fake_snapshot()
    snapshot["sample_ratio"] = float("nan")
    snapshot["signals"]["traces"]["failure_count"] = float("inf")
    client = _build_client(snapshot=lambda: snapshot)
    # Starlette's JSONResponse uses allow_nan=False: a non-finite float
    # surviving the sanitizer would 500 the read-only route.
    r = client.get("/chat/observations/otel")
    assert r.status_code == 200
    data = r.json()
    assert data["sample_ratio"] is None
    assert data["signals"]["traces"]["failure_count"] is None


def test_otel_api_links_preserve_subpath_and_encode_params():
    metrics = "https://grafana.example.com/d/nagent-metrics?orgId=1"
    client = _build_client(
        snapshot=_fake_snapshot,
        grafana_metrics=metrics,
        grafana_logs="https://grafana.example.com/d/nagent-logs",
        grafana_profile="https://grafana.example.com/d/nagent-profile",
        jaeger="https://jaeger.example.com/jaeger/search",
    )
    data = client.get("/chat/observations/otel").json()
    links = data["links"]
    assert set(links) == {"Metric", "Log", "Trace", "Profile"}
    g = links["Metric"]
    # Configured subpath + existing query preserved.
    assert g.startswith("https://grafana.example.com/d/nagent-metrics?orgId=1")
    # service/time appended and URL-encoded (no raw spaces/colons).
    assert "&service=n-agent&time=" in g
    tail = g.split("&time=", 1)[1]
    assert " " not in tail and ":" not in tail
    assert links["Log"].startswith("https://grafana.example.com/d/nagent-logs?")
    assert links["Profile"].startswith("https://grafana.example.com/d/nagent-profile?")
    assert links["Trace"].startswith("https://jaeger.example.com/jaeger/search?")
    assert "service=n-agent" in links["Trace"]


def test_otel_api_link_service_param_comes_from_injection_only():
    # The service label is not duplicated in interfaces: without the
    # injected name the service param is omitted (time still present).
    client = _build_client(
        snapshot=_fake_snapshot,
        grafana_metrics="https://grafana.example.com/",
        service_name="",
    )
    links = client.get("/chat/observations/otel").json()["links"]
    assert "service=" not in links["Metric"]
    assert "time=" in links["Metric"]


def test_otel_api_links_hidden_or_rejected():
    client = _build_client(
        snapshot=_fake_snapshot,
        grafana_metrics="",
        grafana_logs="",
        grafana_profile="",
        jaeger="https://user:secret@jaeger.example.com/",
    )
    r = client.get("/chat/observations/otel")
    data = r.json()
    assert data["links"] == {}
    assert "user:secret" not in r.text


# ---------------------------------------------------------------------------
# OTel link config service merge (T5)
# ---------------------------------------------------------------------------


class _FakeOtelLinkConfigService:
    """In-memory OtelLinkConfigProvider stub: current() returns a fixed
    already-merged config (merge semantics belong to the service)."""

    def __init__(self, config):
        self._config = config

    async def current(self):
        return self._config


def _fake_link_service(**overrides):
    from app.domain.otel_link_config import OtelLinkConfig

    return _FakeOtelLinkConfigService(OtelLinkConfig(**overrides))


def test_otel_api_links_come_from_injected_service_when_present():
    # Env params are set but the service (merged env+DB) wins; dashboard
    # must not merge again -- it consumes current() verbatim.
    client = _build_client(
        snapshot=_fake_snapshot,
        grafana_metrics="https://env.example.com/metrics",
        grafana_logs="https://env.example.com/logs",
        grafana_profile="https://env.example.com/profile",
        jaeger="https://env.example.com/jaeger",
        otel_link_config_service=_fake_link_service(
            metric_base_url="https://db.example.com/grafana/d/metrics",
            log_base_url="https://db.example.com/grafana/d/logs",
            trace_base_url="https://db.example.com/jaeger/search",
            profile_base_url="https://db.example.com/grafana/d/profile",
        ),
    )
    data = client.get("/chat/observations/otel").json()
    links = data["links"]
    assert set(links) == {"Metric", "Log", "Trace", "Profile"}
    assert links["Metric"].startswith("https://db.example.com/grafana/d/metrics?")
    assert links["Log"].startswith("https://db.example.com/grafana/d/logs?")
    assert links["Trace"].startswith("https://db.example.com/jaeger/search?")
    assert links["Profile"].startswith("https://db.example.com/grafana/d/profile?")
    assert "env.example.com" not in json.dumps(links)
    # runtime surface still appends service/time server-side
    assert "service=n-agent" in links["Metric"]
    assert "time=" in links["Metric"]


def test_otel_api_links_empty_string_field_produces_no_link():
    client = _build_client(
        snapshot=_fake_snapshot,
        grafana_metrics="https://env.example.com/metrics",
        jaeger="https://env.example.com/jaeger",
        otel_link_config_service=_fake_link_service(
            metric_base_url="",  # DB override disables the Metric link
            log_base_url="",
            trace_base_url="https://db.example.com/jaeger",
            profile_base_url="",
        ),
    )
    data = client.get("/chat/observations/otel").json()
    assert set(data["links"]) == {"Trace"}
    assert data["links"]["Trace"].startswith("https://db.example.com/jaeger?")


class _RaisingOtelLinkConfigService:
    async def current(self):
        raise RuntimeError("boom")


def test_otel_api_links_fall_back_to_env_when_service_current_raises():
    client = _build_client(
        snapshot=_fake_snapshot,
        grafana_metrics="https://env.example.com/metrics",
        jaeger="https://env.example.com/jaeger",
        otel_link_config_service=_RaisingOtelLinkConfigService(),
    )
    r = client.get("/chat/observations/otel")
    assert r.status_code == 200
    links = r.json()["links"]
    assert links["Metric"].startswith("https://env.example.com/metrics?")
    assert links["Trace"].startswith("https://env.example.com/jaeger?")
    assert "boom" not in r.text


def test_otel_api_links_without_service_keep_env_behavior():
    # otel_link_config_service=None: links come from injected env params.
    client = _build_client(
        snapshot=_fake_snapshot,
        grafana_metrics="https://env.example.com/metrics",
        otel_link_config_service=None,
    )
    links = client.get("/chat/observations/otel").json()["links"]
    assert links["Metric"].startswith("https://env.example.com/metrics?")


# ---------------------------------------------------------------------------
# Self-check route is never auto-instrumented (T4 exclusion contract)
# ---------------------------------------------------------------------------


def test_self_check_route_excluded_from_auto_spans():
    from app.infrastructure.observability.instrumentors import EXCLUDED_HTTP_PATHS

    assert "/chat/observations/otel" in EXCLUDED_HTTP_PATHS


def test_dashboard_module_never_imports_opentelemetry():
    source = (STATIC_DIR.parent / "dashboard.py").read_text(encoding="utf-8")
    assert "import opentelemetry" not in source
    assert "from opentelemetry" not in source


# ---------------------------------------------------------------------------
# Page shell + static frontend contracts
# ---------------------------------------------------------------------------


def test_observations_otel_page_shell_route():
    client = _build_client(snapshot=_fake_snapshot)
    r = client.get("/observations/otel")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert 'id="app-sidebar"' in r.text
    # The shell is the shared index.html (page shell contract).
    assert 'id="tab-observations-otel"' in r.text


def test_index_html_wires_page_shell_sidebar_and_script():
    html = INDEX_HTML.read_text(encoding="utf-8")
    # page shell container
    assert 'id="tab-observations-otel"' in html
    # sidebar child entry under the observations submenu
    assert 'href="/observations/otel"' in html
    assert 'data-tab="observations-otel"' in html
    # module script loaded before app.js
    assert 'src="/static/observations-otel.js"' in html
    assert html.index("observations-otel.js") < html.index("/static/app.js")


def test_observations_otel_js_node_syntax_check():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    result = subprocess.run(
        [node, "--check", str(OTEL_JS)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_observations_otel_js_renders_text_content_only():
    source = OTEL_JS.read_text(encoding="utf-8")
    assert "innerHTML" not in source
    assert "outerHTML" not in source
    assert "insertAdjacentHTML" not in source
    assert "document.write" not in source


# --- node harness: navigation entry + app.js dispatch + module rendering ----

_HARNESS_JS = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');

const REPO = process.argv[2];
const read = (p) => fs.readFileSync(REPO + p, 'utf8');

let failures = 0;
function ok(cond, msg) { if (!cond) { failures++; console.error('FAIL: ' + msg); } }

// --- shared DOM mock --------------------------------------------------------
const allEls = [];
const byId = {};
function makeEl(tag) {
  const el = {
    tag: tag || 'div',
    _cls: new Set(),
    dataset: {},
    _attrs: {},
    style: {},
    hidden: false,
    _listeners: {},
    _kids: [],
    _text: '',
    value: '',
    _id: null,
    set textContent(v) { this._text = (v == null) ? '' : String(v); },
    get textContent() { return this._text; },
    set id(v) { this._id = v; if (v != null) byId[v] = this; },
    get id() { return this._id; },
    get classList() {
      const self = this;
      return {
        add(...cs) { cs.forEach((c) => self._cls.add(c)); },
        remove(...cs) { cs.forEach((c) => self._cls.delete(c)); },
        toggle(c, force) { if (force === false) { self._cls.delete(c); return; } self._cls.add(c); },
        contains(c) { return self._cls.has(c); },
      };
    },
    appendChild(c) { this._kids.push(c); c._parent = this; return c; },
    replaceChildren() { this._kids.forEach((k) => { k._parent = null; }); this._kids = []; },
    remove() {
      if (this._parent) {
        const i = this._parent._kids.indexOf(this);
        if (i >= 0) this._parent._kids.splice(i, 1);
        this._parent = null;
      }
      this._removed = true;
    },
    addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); },
    setAttribute(k, v) { this._attrs[k] = String(v); },
    getAttribute(k) { return (k in this._attrs) ? this._attrs[k] : null; },
  };
  allEls.push(el);
  return el;
}
function descendants(el) {
  const out = [];
  (function walk(n) { if (!n || !n._kids) return; for (const k of n._kids) { out.push(k); walk(k); } })(el);
  return out;
}
function liveQuery(root, pred) { return descendants(root).filter(pred); }

const container = makeEl('div');
container._cls.add('tab-content');
container.id = 'tab-observations-otel';
container._cls.add('active');

const doc = {
  createElement(tag) { return makeEl(tag); },
  createTextNode(t) { const el = makeEl('#text'); el._text = String(t); return el; },
  getElementById(id) { return byId[id] || null; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
};

// ===========================================================================
// Part 1: management-navigation.js route contract for /observations/otel
// ===========================================================================
{
  const win = { NAGENT: {}, location: { pathname: '/' } };
  const ctx = { window: win, document: doc, console, URL, localStorage: { getItem() { return null; }, setItem() {} } };
  vm.createContext(ctx);
  vm.runInContext(read('/app/interfaces/http/static/management-navigation.js'), ctx);
  const nav = win.NAGENT.navigation;

  const state = nav.resolveRoute('/observations/otel');
  ok(state.route !== null, '/observations/otel has an independent routeConfig entry');
  ok(state.activeTab === 'observations-otel', 'activeTab === observations-otel (got ' + state.activeTab + ')');
  ok(state.renderTab === 'observations-otel', 'renderTab === observations-otel (got ' + state.renderTab + ')');
  ok(state.sidebarTab === 'observations-otel', 'sidebarTab === observations-otel (got ' + state.sidebarTab + ')');
  ok(state.currentSubdomain === 'observations-otel', 'private observations subdomain scope (got ' + state.currentSubdomain + ')');
  if (state.route) {
    ok(state.route.scope === 'observations-otel', 'route.scope === observations-otel');
    ok(state.route.topnavParent === 'observations-otel', 'route.topnavParent === observations-otel');
    ok(state.route.paths.length === 1 && state.route.paths[0] === '/observations/otel', 'route.paths is the single canonical path');
  }
  ok(nav.tabNames.indexOf('observations-otel') !== -1, 'observations-otel is a registered tab (sidebar parent child)');
  ok(nav.pathByTab['observations-otel'] === '/observations/otel', 'pathByTab maps observations-otel');
  const items = nav.topnavConfig['observations-otel'];
  ok(Array.isArray(items) && items.length === 1, 'observations-otel has its own single-item topnav');
  ok(items && items[0].tab === 'observations-otel' && items[0].path === '/observations/otel', 'observations-otel topnav item owns its tab/path');
}

// ===========================================================================
// Part 2 + 3 run sequentially in one async main; exit code reflects failures.
// ===========================================================================
let fetchCalls = 0;
(async function main() {
  // --- Part 2: app.js renderer dispatch for the observations-otel tab ------
  {
    const initCalls = [];
    const refreshCalls = [];
    const win = {
      NAGENT: {
        observationsOtel: {
          init() { initCalls.push('init'); },
          refresh() { refreshCalls.push('refresh'); },
        },
        navigation: { topnavConfig: { 'observations-otel': [{ tab: 'observations-otel', path: '/observations/otel' }] }, initNavigation() {} },
      },
      location: { pathname: '/observations/otel' },
    };
    const ctx = { window: win, document: doc, console };
    ctx.document.readyState = 'complete';
    ctx.document.body = makeEl('body');
    vm.createContext(ctx);
    vm.runInContext(read('/app/interfaces/http/static/app.js'), ctx);
    const state = {
      activeTab: 'observations-otel', renderTab: 'observations-otel',
      sidebarTab: 'observations-otel', currentSubdomain: 'observations-otel', route: null,
    };
    await win.NAGENT.app.onTabActivated(state);
    ok(initCalls.length === 1, 'app.js dispatches init to NAGENT.observationsOtel (got ' + initCalls.length + ')');
    await win.NAGENT.app.onTabActivated(state);
    ok(refreshCalls.length === 1, 'second activation refreshes NAGENT.observationsOtel (got ' + refreshCalls.length + ')');
  }

  // --- Part 3: observations-otel.js rendering + lifecycle (textContent-only)
  const snapshotPayload = {
    status: 'degraded',
    mode: 'otlp',
    instance_id: 'inst-xyz',
    sample_ratio: 1,
    semconv_version: '1.37.0 (genai draft)',
    exporter_endpoint: 'collector.internal:4317',
    instruments: [{ name: 'gen_ai.client.inference.duration', kind: 'histogram', unit: 's' }],
    signals: {
      traces: { status: 'healthy', delivery: 'succeeded', failure_count: 0, last_success: 'T1', last_failure: null, current_error: null },
      profile: { status: 'degraded', delivery: 'failed', failure_count: 3, last_success: null, last_failure: 'T2', current_error: 'upload_failed' },
    },
    spans: [{ name: 'execute_tool {tool}', duration_ms: 3.2, status: 'OK' }],
    dropped_after_deadline: 0,
    links: { Metric: 'https://grafana.example.com/d/nagent-metrics?service=n-agent' },
    notes: { queue_drops: 'not exposed by SDK batch processors' },
  };
  let fetchCalls = 0;
  const ui = {
    byId: (id) => byId[id] || null,
    el: (tag, cls) => { const n = makeEl(tag); if (cls) String(cls).split(/\s+/).filter(Boolean).forEach((c) => n._cls.add(c)); return n; },
    renderLoading: (parent, msg) => { const d = makeEl('div'); d.textContent = msg || '加载中...'; parent.appendChild(d); },
    renderError: (parent, msg) => { const d = makeEl('div'); d.textContent = msg || '加载失败'; parent.appendChild(d); },
  };
  const win = {
    NAGENT: { ui: ui, api: { fetchJson(url) { fetchCalls++; ok(url === '/chat/observations/otel', 'module fetches the self-check API (got ' + url + ')'); return Promise.resolve(snapshotPayload); } } },
    location: { pathname: '/observations/otel' },
  };
  const ctx = { window: win, document: doc, console, URLSearchParams };
  vm.createContext(ctx);
  vm.runInContext(read('/app/interfaces/http/static/observations.js'), ctx);
  vm.runInContext(read('/app/interfaces/http/static/observations-otel.js'), ctx);
  const mod = win.NAGENT.observationsOtel;
  ok(mod && typeof mod.init === 'function' && typeof mod.refresh === 'function' && typeof mod.deactivate === 'function',
    'observations-otel.js exports init/refresh/deactivate');
  await mod.init();
  ok(fetchCalls === 1, 'init fetches the API exactly once');
  const text = descendants(container).map((el) => el._text).join('\n');
  ok(text.indexOf('degraded') !== -1, 'status rendered via textContent');
  ok(text.indexOf('inst-xyz') !== -1, 'instance_id rendered');
  ok(text.indexOf('upload_failed') !== -1, 'per-signal safe error code rendered');
  ok(text.indexOf('execute_tool {tool}') !== -1, 'restricted span summaries rendered');
  ok(text.indexOf('not exposed by SDK batch processors') !== -1, 'queue-drop limitation note rendered');
  const anchors = liveQuery(container, (el) => el.tag === 'a' && el._attrs.href);
  ok(anchors.length === 1, 'server link rendered as a single anchor');
  ok(anchors[0] && anchors[0]._attrs.target === '_blank', 'external link opens in a new window');
  const rel = anchors[0] ? String(anchors[0]._attrs.rel || '') : '';
  ok(rel.indexOf('noopener') !== -1 && rel.indexOf('noreferrer') !== -1, 'external link uses noopener noreferrer');
  // textContent-only: every rendered value went through textContent nodes
  // (the mock records them as _text; there is no HTML channel in the mock).
  ok(descendants(container).every((el) => el._attrs.innerHTML === undefined),
    'no innerHTML attribute ever set');
  // refresh re-fetches
  await mod.refresh();
  ok(fetchCalls === 2, 'refresh re-fetches the snapshot');
  snapshotPayload.spans = Array.from({ length: 21 }, (_, i) => ({ name: 'span-' + i, status: 'OK' }));
  await mod.refresh();
  const spanPanel = () => liveQuery(container, (el) => el.tag === 'section').find(
    (section) => descendants(section).some((el) => el._text === '最近执行片段(span)')
  );
  const spanRows = () => liveQuery(spanPanel(), (el) => el.tag === 'tbody')[0]._kids;
  const pageButton = (label) => liveQuery(spanPanel(), (el) => el.tag === 'button' && el._text === label)[0];
  const clickPage = (label) => pageButton(label)._listeners.click.forEach((fn) => fn());
  ok(spanRows().length === 10 && spanRows()[0]._kids[0]._text === 'span-0', 'span first page has ten rows');
  ok(pageButton('<').disabled, 'span first page disables previous');
  clickPage('2');
  ok(spanRows().length === 10 && spanRows()[0]._kids[0]._text === 'span-10', 'span second page has next ten rows');
  clickPage('>');
  ok(spanRows().length === 1 && spanRows()[0]._kids[0]._text === 'span-20', 'span last page has remaining row');
  ok(pageButton('>').disabled, 'span last page disables next');
  clickPage('<');
  ok(spanRows()[0]._kids[0]._text === 'span-10', 'span previous page works');
  ok(fetchCalls === 3, 'span paging does not refetch');
  await mod.refresh();
  ok(spanRows().length === 10 && spanRows()[0]._kids[0]._text === 'span-0', 'snapshot refresh resets span page');
  mod.deactivate();

  // =========================================================================
  // Part 4: link config modal (T7)
  // =========================================================================
  {
    const defer = () => { let res; const p = new Promise((r) => { res = r; }); return { p, res }; };
    const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
    const waitFor = async (cond) => { for (let i = 0; i < 80 && !cond(); i++) await Promise.resolve(); };

    const modalContainer = makeEl('div');
    modalContainer._cls.add('tab-content');
    modalContainer.id = 'tab-observations-otel';
    modalContainer._cls.add('active');
    doc.body = makeEl('body');

    const configDto = {
      config: {
        metric_base_url: 'https://env.example.com/m',
        log_base_url: 'https://env.example.com/l',
        trace_base_url: '',
        profile_base_url: 'https://db.example.com/p',
      },
      version: 3,
      overridden_fields: ['trace_base_url', 'profile_base_url'],
      updated_at: '2026-10-27T00:00:00+00:00',
      updated_by: 'dashboard-local',
    };
    const okResponse = (body) => ({ status: 200, body });
    const errResponse = (status, code, message) => ({ status, body: { error: { code, message } } });

    const fetchCalls = [];
    let fetchQueue = [];
    const fetchStub = (url, options) => {
      fetchCalls.push({ url, options: options || {} });
      const next = fetchQueue.shift() || okResponse(configDto);
      const respond = (r) => ({
        ok: r.status >= 200 && r.status < 300,
        status: r.status,
        json: () => Promise.resolve(r.body),
      });
      if (next && next.defer) return next.defer.p.then(respond);
      if (next && next.badJson) {
        return Promise.resolve({ ok: next.status < 300, status: next.status,
          json: () => Promise.reject(new Error('bad json')) });
      }
      return Promise.resolve(next).then(respond);
    };
    const patchCalls = () => fetchCalls.filter((c) => c.options.method === 'PATCH');

    const alerts = [];
    let confirmResult = true;
    let confirmImpl = null;
    const snapshotCalls = [];
    const win4 = {
      NAGENT: {
        ui: ui,
        api: { fetchJson(url) { const d = defer(); snapshotCalls.push(d); return d.p; } },
        modal: {
          alert(msg) { alerts.push(String(msg)); return Promise.resolve(); },
          confirm(msg) { alerts.push('confirm:' + String(msg)); return confirmImpl ? confirmImpl() : Promise.resolve(confirmResult); },
        },
      },
      location: { pathname: '/observations/otel' },
    };
    const ctx4 = { window: win4, document: doc, console, URLSearchParams, fetch: fetchStub };
    vm.createContext(ctx4);
    vm.runInContext(read('/app/interfaces/http/static/observations-otel.js'), ctx4);
    const mod4 = win4.NAGENT.observationsOtel;

    const findBackdrop = () => liveQuery(doc.body, (el) => el._cls.has('modal-backdrop'))[0] || null;
    const findButton = (root, text) => liveQuery(root, (el) => el.tag === 'button' && el._text === text)[0] || null;
    const inputsOf = (bd) => liveQuery(bd, (el) => el.tag === 'input');
    const clickEl = (el) => (el._listeners.click || []).forEach((fn) => fn({ target: el }));
    const fireInput = (el) => (el._listeners.input || []).forEach((fn) => fn({ target: el }));
    const submitForm = (bd) => {
      const form = liveQuery(bd, (el) => el.tag === 'form')[0];
      (form._listeners.submit || []).forEach((fn) => fn({ preventDefault() {} }));
    };
    const bodyText = (bd) => descendants(bd).map((el) => el._text).join('\n');
    const basePayload = { links: {}, signals: {}, instruments: [], spans: [], notes: {} };
    const openModal = async (getResponse) => {
      fetchQueue.push(getResponse || okResponse(configDto));
      await waitFor(() => findButton(modalContainer, '配置链接'));
      const btn = findButton(modalContainer, '配置链接');
      ok(btn, '配置链接 button available for open');
      if (!btn) return null;
      clickEl(btn);
      await flush();
      return findBackdrop();
    };

    // A: entry button renders even with an empty link list
    const initP = mod4.init();
    snapshotCalls[0].res(basePayload);
    await initP;
    const configBtn = findButton(modalContainer, '配置链接');
    ok(configBtn, '配置链接 entry renders even when links list is empty');
    ok(configBtn && configBtn._cls.has('btn'), 'entry uses the standard .btn class');
    ok(configBtn && configBtn._parent && configBtn._parent._cls.has('panel-header'), 'entry sits in the panel header row');

    // B: open -> GET in flight disables save; success prefills inputs + states
    const slowGetB = defer();
    fetchQueue.push({ defer: slowGetB });
    clickEl(findButton(modalContainer, '配置链接'));
    await flush();
    let bd = findBackdrop();
    ok(bd, 'clicking 配置链接 opens the modal');
    ok(bodyText(bd).indexOf('配置外部链接') !== -1, 'modal title rendered');
    ok(bodyText(bd).indexOf('修改后留空表示跟随环境变量默认配置，未修改字段保持原配置') !== -1, 'muted hint rendered');
    ok(fetchCalls.length === 1 && fetchCalls[0].url === '/chat/observations/otel/links', 'open issues GET on the admin API');
    ok(findButton(bd, '保存').disabled === true, 'save disabled while loading');
    ok(findButton(bd, '恢复默认').disabled === true, 'restore disabled while loading');
    const dialogEl = bd._kids[0];
    ok(dialogEl && dialogEl.tag === 'section' && dialogEl.className === 'modal-dialog', 'dialog is section.modal-dialog');
    ok(dialogEl && dialogEl._attrs.role === 'dialog' && dialogEl._attrs['aria-modal'] === 'true', 'dialog carries role/aria-modal');
    const formEl = dialogEl && dialogEl._kids[0];
    ok(formEl && formEl.tag === 'form' && formEl.className === 'providers-form', 'form uses providers-form inside the dialog');
    const closeX = findButton(bd, '×');
    ok(closeX && closeX._cls.has('modal-close'), 'header has a modal-close button');
    const actionsEl = liveQuery(bd, (el) => el._cls.has('providers-form__actions'))[0];
    ok(actionsEl, 'footer uses providers-form__actions');
    const actionTexts = actionsEl ? actionsEl._kids.map((k) => k._text) : [];
    ok(actionTexts.join('|') === '恢复默认|取消|保存', 'footer order: restore(left)/cancel/save (got ' + actionTexts.join(',') + ')');
    ok(actionsEl && actionsEl._kids[0].style.marginRight === 'auto', 'restore pinned left via marginRight:auto');
    ok(actionsEl && actionsEl._kids[2]._cls.has('btn--primary'), 'save uses btn--primary');
    slowGetB.res(okResponse(configDto));
    await flush();
    const inputs = inputsOf(bd);
    ok(inputs.length === 4, 'four url inputs rendered');
    ok(inputs[0].value === 'https://env.example.com/m', 'metric input prefilled from resolved config');
    ok(inputs[2].value === '', 'disabled trace field prefilled empty');
    const text1 = bodyText(bd);
    ok(text1.indexOf('跟随环境') !== -1, 'env-following state label rendered');
    ok(text1.indexOf('已禁用') !== -1, 'disabled state label rendered');
    ok(text1.indexOf('自定义') !== -1, 'custom state label rendered');
    const followBtns = liveQuery(bd, (el) => el.tag === 'button' && el._text === '跟随环境');
    ok(followBtns.length === 4, 'every row owns a 跟随环境 marker button');
    ok(followBtns.filter((b) => !b.hidden).length === 1, 'only the disabled row exposes 跟随环境');
    ok(findButton(bd, '保存').disabled === false, 'save enabled after load');
    clickEl(findButton(bd, '取消'));
    await flush();
    ok(!findBackdrop(), 'cancel closes the modal');

    // C: save without edits sends no PATCH and just closes
    bd = await openModal();
    submitForm(bd);
    await flush();
    ok(patchCalls().length === 0, 'no-edit save sends no PATCH');
    ok(!findBackdrop(), 'no-edit save closes the modal');

    // D: editing one field sends only that field + expected_version
    bd = await openModal();
    inputsOf(bd)[0].value = '  https://custom.example.com/m  ';
    fetchQueue.push(okResponse(configDto));
    submitForm(bd);
    await flush();
    ok(patchCalls().length === 1, 'edited save sends exactly one PATCH');
    const patch1 = JSON.parse(patchCalls()[0].options.body);
    ok(patch1.expected_version === 3, 'PATCH carries the opened version');
    ok(patch1.metric_base_url === 'https://custom.example.com/m', 'edited value trimmed and sent');
    ok(!('log_base_url' in patch1) && !('trace_base_url' in patch1) && !('profile_base_url' in patch1),
      'unedited fields are not sent (env defaults stay unfrozen)');
    await waitFor(() => !findBackdrop());
    ok(!findBackdrop(), 'successful save closes the modal');
    await waitFor(() => snapshotCalls.length === 2);
    ok(snapshotCalls.length === 2, 'successful save forces a panel refresh');
    ok(alerts.some((m) => m.indexOf('已保存') !== -1 || m.indexOf('保存') !== -1), 'success alert via shared modal');
    snapshotCalls[1].res(basePayload);
    await flush();

    // E: clearing an edited field sends null
    fetchCalls.length = 0;
    bd = await openModal();
    inputsOf(bd)[3].value = '   ';
    fetchQueue.push(okResponse(configDto));
    submitForm(bd);
    await flush();
    const patch2 = JSON.parse(patchCalls()[0].options.body);
    ok(patchCalls().length === 1 && patch2.profile_base_url === null, 'cleared field sends null');
    ok(!('metric_base_url' in patch2), 'other fields untouched');
    snapshotCalls[snapshotCalls.length - 1].res(basePayload);
    await waitFor(() => findButton(modalContainer, '配置链接'));

    // F: disabled-field 跟随环境 button marks a pending null
    fetchCalls.length = 0;
    bd = await openModal();
    const followBtn2 = liveQuery(bd, (el) => el.tag === 'button' && el._text === '跟随环境' && !el.hidden)[0];
    clickEl(followBtn2);
    fetchQueue.push(okResponse(configDto));
    submitForm(bd);
    await flush();
    const patch3 = JSON.parse(patchCalls()[0].options.body);
    ok(patchCalls().length === 1 && patch3.trace_base_url === null, 'follow-env on a disabled field sends null');
    snapshotCalls[snapshotCalls.length - 1].res(basePayload);
    await waitFor(() => findButton(modalContainer, '配置链接'));

    // G: 409 keeps inputs and never auto-retries
    fetchCalls.length = 0;
    bd = await openModal();
    inputsOf(bd)[0].value = 'https://custom.example.com/m';
    fetchQueue.push(errResponse(409, 'otel_link_config_conflict', 'version conflict'));
    submitForm(bd);
    await flush();
    ok(patchCalls().length === 1, '409 is not auto-retried');
    ok(findBackdrop(), '409 keeps the modal open');
    ok(inputsOf(bd)[0].value === 'https://custom.example.com/m', '409 preserves user input');
    ok(alerts.some((m) => m.indexOf('重新打开弹窗') !== -1), '409 alert asks to reopen the modal');
    clickEl(findButton(bd, '取消'));
    await flush();

    // H: restore-default cancel writes nothing
    fetchCalls.length = 0;
    confirmResult = false;
    bd = await openModal();
    clickEl(findButton(bd, '恢复默认'));
    await flush();
    ok(patchCalls().length === 0, 'cancelled restore sends no PATCH');
    ok(findBackdrop(), 'cancelled restore keeps the modal open');
    ok(findButton(bd, '保存').disabled === false, 'cancel releases the submit lock');

    // I: confirmed restore PATCHes every field null once
    confirmResult = true;
    fetchQueue.push(okResponse(configDto));
    clickEl(findButton(bd, '恢复默认'));
    await flush();
    ok(patchCalls().length === 1, 'confirmed restore sends one PATCH');
    const patch4 = JSON.parse(patchCalls()[0].options.body);
    ok(patch4.expected_version === 3 && patch4.metric_base_url === null && patch4.log_base_url === null
      && patch4.trace_base_url === null && patch4.profile_base_url === null,
      'restore sends null for all four fields');
    ok(!findBackdrop(), 'successful restore closes the modal');
    snapshotCalls[snapshotCalls.length - 1].res(basePayload);
    await waitFor(() => findButton(modalContainer, '配置链接'));

    // J: double submit during a slow PATCH sends exactly once
    fetchCalls.length = 0;
    bd = await openModal();
    inputsOf(bd)[0].value = 'https://slow.example.com/m';
    const slowPatch = defer();
    fetchQueue.push({ defer: slowPatch });
    submitForm(bd);
    submitForm(bd);
    ok(patchCalls().length === 1, 'repeated submit during flight sends one PATCH');
    ok(findButton(bd, '保存').disabled === true, 'write buttons disabled while submitting');
    slowPatch.res(okResponse(configDto));
    await flush();
    ok(patchCalls().length === 1, 'still one PATCH after resolution');
    snapshotCalls[snapshotCalls.length - 1].res(basePayload);
    await waitFor(() => findButton(modalContainer, '配置链接'));

    // K: forced refresh discards the superseded in-flight snapshot
    bd = await openModal();
    const staleP = mod4.refresh();
    await flush();
    inputsOf(bd)[0].value = 'https://force.example.com/m';
    fetchQueue.push(okResponse(configDto));
    submitForm(bd);
    await waitFor(() => !findBackdrop());
    const staleIdx = snapshotCalls.length - 2;
    ok(staleIdx >= 1, 'a stale snapshot request preceded the forced reload');
    snapshotCalls[staleIdx].res({ links: {}, signals: {}, instruments: [], spans: [], notes: { stale: 'STALE_MARKER' } });
    await staleP;
    await flush();
    ok(descendants(modalContainer).map((el) => el._text).join('\n').indexOf('STALE_MARKER') === -1,
      'forced refresh discards the superseded snapshot');
    snapshotCalls[snapshotCalls.length - 1].res(basePayload);
    await waitFor(() => findButton(modalContainer, '配置链接'));

    // L: deactivate invalidates a pending GET and removes the modal
    const slowGet = defer();
    fetchQueue.push({ defer: slowGet });
    clickEl(findButton(modalContainer, '配置链接'));
    await flush();
    ok(findBackdrop(), 'modal open before deactivate');
    mod4.deactivate();
    ok(!findBackdrop(), 'deactivate removes the config modal');
    slowGet.res(okResponse(configDto));
    await flush();
    ok(!findBackdrop(), 'late GET after deactivate does not reopen or render');

    // M: GET 404 disables the entry and explains unavailability
    const configBtn2 = findButton(modalContainer, '配置链接');
    fetchQueue.push({ status: 404, body: { error: { code: 'not_found', message: 'not found' } } });
    clickEl(configBtn2);
    await flush();
    const bd404 = findBackdrop();
    ok(bd404 && bodyText(bd404).indexOf('不可用') !== -1, '404 explains the config service is unavailable');
    ok(configBtn2.disabled === true, '404 disables the config entry');
    ok(findButton(bd404, '保存').disabled === true, '404 keeps save disabled');
    clickEl(findButton(bd404, '取消'));
    await flush();

    // M2: tab re-init resets the 404-disabled entry
    const initP2 = mod4.init();
    snapshotCalls[snapshotCalls.length - 1].res(basePayload);
    await initP2;
    const configBtn3 = findButton(modalContainer, '配置链接');
    ok(configBtn3 && configBtn3.disabled === false, 'tab re-init resets the 404-disabled entry');

    // N: other GET errors show in-modal retry only
    fetchQueue.push({ status: 500, body: { error: { code: 'otel_link_config_load_failed', message: 'load failed' } } });
    clickEl(configBtn3);
    await flush();
    const bd500 = findBackdrop();
    ok(bd500 && bodyText(bd500).indexOf('加载配置失败') !== -1, 'GET failure shows in-modal error');
    ok(findButton(bd500, '重试'), 'GET failure offers a retry button');
    fetchQueue.push(okResponse(configDto));
    clickEl(findButton(bd500, '重试'));
    await flush();
    ok(inputsOf(bd500).length === 4, 'retry reloads the form');
    clickEl(findButton(bd500, '取消'));
    await flush();

    // O: typing a new url after marking follow-env sends the value, not null
    fetchCalls.length = 0;
    bd = await openModal();
    const followBtnO = liveQuery(bd, (el) => el.tag === 'button' && el._text === '跟随环境' && !el.hidden)[0];
    clickEl(followBtnO);
    const traceInput = inputsOf(bd)[2];
    traceInput.value = 'https://new.example.com/t';
    fireInput(traceInput);
    const stateLabels = liveQuery(bd, (el) => el._cls.has('otel-link-config__state'));
    ok(stateLabels[2]._text === '自定义', 'state label follows the typed input');
    fetchQueue.push(okResponse(configDto));
    submitForm(bd);
    await waitFor(() => !findBackdrop());
    const patch5 = JSON.parse(patchCalls()[0].options.body);
    ok(patchCalls().length === 1 && patch5.trace_base_url === 'https://new.example.com/t',
      'typed value wins over the pending follow-env null');
    snapshotCalls[snapshotCalls.length - 1].res(basePayload);
    await waitFor(() => findButton(modalContainer, '配置链接'));

    // P: 2xx with an unparseable body is a load failure, not an empty config
    fetchCalls.length = 0;
    bd = await openModal({ status: 200, badJson: true });
    ok(bd && bodyText(bd).indexOf('加载配置失败') !== -1, 'malformed 2xx body shows in-modal error');
    ok(findButton(bd, '重试'), 'malformed 2xx body offers a retry');
    ok(findButton(bd, '保存').disabled === true, 'malformed 2xx body never enables save');
    ok(inputsOf(bd).length === 0, 'malformed 2xx body does not render an empty form');
    clickEl(findButton(bd, '取消'));
    await flush();

    // Q: 422 shows the server message via shared alert and keeps the input
    fetchCalls.length = 0;
    bd = await openModal();
    inputsOf(bd)[0].value = 'ftp://bad';
    fetchQueue.push(errResponse(422, 'otel_link_config_invalid', 'metric_base_url: 必须是合法 http/https URL'));
    submitForm(bd);
    await flush();
    ok(patchCalls().length === 1, '422 issued the PATCH once');
    ok(findBackdrop(), '422 keeps the modal open');
    ok(inputsOf(bd)[0].value === 'ftp://bad', '422 preserves user input');
    ok(alerts.some((m) => m.indexOf('metric_base_url: 必须是合法 http/https URL') !== -1),
      '422 surfaces the server message via shared alert');
    ok(findButton(bd, '保存').disabled === false, '422 re-enables save');
    clickEl(findButton(bd, '取消'));
    await flush();

    // R: the restore confirm wait holds the submit lock
    fetchCalls.length = 0;
    bd = await openModal();
    const pendingConfirm = defer();
    confirmImpl = () => pendingConfirm.p;
    clickEl(findButton(bd, '恢复默认'));
    await flush();
    ok(findButton(bd, '保存').disabled === true, 'confirm wait disables save');
    ok(findButton(bd, '恢复默认').disabled === true, 'confirm wait disables restore');
    submitForm(bd);
    clickEl(findButton(bd, '恢复默认'));
    await flush();
    ok(patchCalls().length === 0, 'no PATCH while the confirm is pending');
    fetchQueue.push(okResponse(configDto));
    pendingConfirm.res(true);
    await waitFor(() => !findBackdrop());
    ok(patchCalls().length === 1, 'exactly one PATCH after the confirm resolves');
    const patchR = JSON.parse(patchCalls()[0].options.body);
    ok(patchR.metric_base_url === null && patchR.log_base_url === null
      && patchR.trace_base_url === null && patchR.profile_base_url === null,
      'the single restore PATCH nulls every field');
    confirmImpl = null;
    snapshotCalls[snapshotCalls.length - 1].res(basePayload);
    await waitFor(() => findButton(modalContainer, '配置链接'));

    // S: closing the modal invalidates an in-flight GET
    const slowGetS = defer();
    fetchQueue.push({ defer: slowGetS });
    clickEl(findButton(modalContainer, '配置链接'));
    await flush();
    ok(findBackdrop(), 'modal open before cancel');
    clickEl(findButton(findBackdrop(), '取消'));
    await flush();
    ok(!findBackdrop(), 'cancel closes the modal during the GET');
    slowGetS.res(okResponse(configDto));
    await flush();
    ok(!findBackdrop(), 'late GET after close does not reopen the modal');
    ok(liveQuery(doc.body, (el) => el.tag === 'input').length === 0, 'late GET after close renders nothing');

    // T: closing the modal invalidates an in-flight PATCH success
    fetchCalls.length = 0;
    bd = await openModal();
    inputsOf(bd)[0].value = 'https://late.example.com/m';
    const slowPatchT = defer();
    fetchQueue.push({ defer: slowPatchT });
    submitForm(bd);
    await flush();
    ok(patchCalls().length === 1, 'PATCH issued before close');
    const alertsBefore = alerts.length;
    const snapshotsBefore = snapshotCalls.length;
    clickEl(findButton(bd, '取消'));
    await flush();
    slowPatchT.res(okResponse(configDto));
    await flush();
    ok(alerts.length === alertsBefore, 'late PATCH success after close shows no alert');
    ok(snapshotCalls.length === snapshotsBefore, 'late PATCH success after close triggers no refresh');
  }
})().then(function () {
  if (failures) {
    console.error('otel dashboard harness: ' + failures + ' failure(s)');
    process.exit(1);
  }
  console.log('OK otel dashboard harness passed');
  process.exit(0);
}, function (err) {
  console.error('HARNESS ERROR: ' + (err && err.stack || err));
  process.exit(2);
});
"""

# element contract the harness relies on: mock elements never accept HTML
_HARNESS_JS = "/* otel dashboard harness */\n" + _HARNESS_JS


def _run_node_harness() -> subprocess.CompletedProcess:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    with tempfile.TemporaryDirectory() as tmp:
        script_path = Path(tmp) / "otel_dashboard_harness.js"
        script_path.write_text(_HARNESS_JS, encoding="utf-8")
        return subprocess.run(
            [node, str(script_path), str(REPO_ROOT) + "/"],
            capture_output=True,
            text=True,
            check=False,
        )


def test_navigation_app_and_module_frontend_harness():
    result = _run_node_harness()
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "OK otel dashboard harness passed" in result.stdout
    assert "FAIL:" not in output
    assert "HARNESS ERROR" not in output


# ---------------------------------------------------------------------------
# Composition-root profiler wiring (main.py)
# ---------------------------------------------------------------------------


def _wire_settings(tmp_path: Path, *, profiling_enabled: bool) -> Settings:
    skills_root = tmp_path / "skills"
    skills_root.mkdir(exist_ok=True)
    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir(exist_ok=True)
    return Settings(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        skills_root=str(skills_root),
        plugins_root=str(plugins_root),
        scheduler_enabled=False,
        feishu_enabled=False,
        artifacts_enabled=False,
        otel_enabled=False,
        profiling_enabled=profiling_enabled,
        profiling_pyroscope_endpoint=(
            "http://127.0.0.1:4040" if profiling_enabled else ""
        ),
    )


class FakeProfiler:
    def __init__(self, *, on_status=None, start_result=True, start_events=None):
        self.on_status = on_status
        self._start_result = start_result
        self._start_events = start_events
        self.registered_handles = []
        self.start_calls = 0

    def register_with_handle(self, handle):
        self.registered_handles.append(handle)
        return True

    def start(self):
        self.start_calls += 1
        for event in self._start_events or []:
            if self.on_status is not None:
                self.on_status(*event)
        return self._start_result


@pytest.fixture
def fake_observability(monkeypatch):
    """Replace the SDK setup with a fake handle; no real OTel state.

    The fake handle mirrors the real profile-signal state machine: an
    enabled profile signal starts degraded with ``profiler_not_configured``
    and exposes the same status bridge methods the composition root uses."""
    import app.infrastructure.observability.setup as setup

    state = SimpleNamespace(handle=None, configure_calls=0)

    class FakeHandle:
        instance_id = "inst-fake"

        def __init__(self):
            self.shutdown_calls = 0
            self.attached_marks = 0
            self.profiler_events = []
            self.profile = {
                "enabled": True,
                "status": "degraded",
                "delivery": "unknown",
                "failure_count": 0,
                "current_error": "profiler_not_configured",
            }

        def profiler_on_status(self):
            def _bridge(status, code):
                self.profiler_events.append((status, code))
                if status == "healthy":
                    self.profile.update(
                        status="healthy", delivery="succeeded", current_error=None
                    )
                elif status == "degraded":
                    self.profile["failure_count"] += 1
                    self.profile.update(
                        status="degraded",
                        delivery="failed",
                        current_error=code or "profiler_failed",
                    )

            return _bridge

        def mark_profiler_attached(self):
            self.attached_marks += 1
            if self.profile["current_error"] == "profiler_not_configured":
                self.profile.update(current_error=None, status="healthy")

        def snapshot(self):
            overall = (
                "degraded" if self.profile["status"] == "degraded" else "healthy"
            )
            return {
                "status": overall,
                "mode": "local_only",
                "instance_id": self.instance_id,
                "signals": {"profile": dict(self.profile)},
                "spans": [],
                "instruments": [],
            }

        def shutdown(self):
            self.shutdown_calls += 1

    def fake_configure(settings, **_kwargs):
        state.configure_calls += 1
        if state.handle is None:
            state.handle = FakeHandle()
        return state.handle

    monkeypatch.setattr(setup, "configure_observability", fake_configure)
    return state


@pytest.fixture
def fake_profiler_factory(monkeypatch):
    import app.infrastructure.observability.profiling as profiling

    state = SimpleNamespace(
        calls=[], profiler=None, start_result=True, start_events=None
    )

    def fake_get_or_create(settings, *, instance_id=None, **overrides):
        profiler = FakeProfiler(
            on_status=overrides.get("on_status"),
            start_result=state.start_result,
            start_events=state.start_events,
        )
        state.profiler = profiler
        state.calls.append(
            {
                "settings": settings,
                "instance_id": instance_id,
                "on_status": overrides.get("on_status"),
            }
        )
        return profiler

    monkeypatch.setattr(profiling, "get_or_create_profiler", fake_get_or_create)
    return state


def test_build_wires_profiler_when_enabled(tmp_path, fake_observability, fake_profiler_factory):
    from app.main import build_application_services

    services = build_application_services(_wire_settings(tmp_path, profiling_enabled=True))

    assert len(fake_profiler_factory.calls) == 1
    call = fake_profiler_factory.calls[0]
    assert call["settings"].profiling_enabled is True
    # profile signal instance label matches the handle instance id
    assert call["instance_id"] == fake_observability.handle.instance_id
    # the sampler receives the handle's status bridge
    assert callable(call["on_status"])
    profiler = fake_profiler_factory.profiler
    assert profiler.start_calls == 1
    assert profiler.registered_handles == [services.observability_handle]
    handle = fake_observability.handle
    # successful start clears the assembly-time profiler_not_configured
    # fault: overall healthy, delivery still unknown (no ingest yet)
    assert handle.attached_marks == 1
    snapshot = handle.snapshot()
    assert snapshot["status"] == "healthy"
    assert snapshot["signals"]["profile"]["status"] == "healthy"
    assert snapshot["signals"]["profile"]["delivery"] == "unknown"
    assert snapshot["signals"]["profile"]["current_error"] is None
    # a successful window bridges into delivery=succeeded via on_status
    profiler.on_status("healthy", None)
    snapshot = handle.snapshot()
    assert snapshot["signals"]["profile"]["delivery"] == "succeeded"


def test_build_start_failure_degrades_profile_with_sampler_code(
    tmp_path, fake_observability, fake_profiler_factory
):
    from app.main import build_application_services

    fake_profiler_factory.start_result = False
    fake_profiler_factory.start_events = [("degraded", "pyspy_missing")]

    build_application_services(_wire_settings(tmp_path, profiling_enabled=True))

    handle = fake_observability.handle
    snapshot = handle.snapshot()
    assert snapshot["status"] == "degraded"
    profile = snapshot["signals"]["profile"]
    assert profile["status"] == "degraded"
    assert profile["current_error"] == "pyspy_missing"
    assert profile["failure_count"] == 1


def test_build_surfaces_three_strikes_sampler_disabled(
    tmp_path, fake_observability, fake_profiler_factory
):
    from app.main import build_application_services

    build_application_services(_wire_settings(tmp_path, profiling_enabled=True))

    handle = fake_observability.handle
    # healthy start first...
    assert handle.snapshot()["status"] == "healthy"
    # ...then the sampler's 3-strikes disablement must degrade the signal
    fake_profiler_factory.profiler.on_status("degraded", "sampler_disabled")
    snapshot = handle.snapshot()
    assert snapshot["status"] == "degraded"
    profile = snapshot["signals"]["profile"]
    assert profile["current_error"] == "sampler_disabled"
    assert profile["failure_count"] == 1
    assert profile["delivery"] == "failed"


def test_build_skips_profiler_when_disabled(tmp_path, fake_observability, fake_profiler_factory):
    from app.main import build_application_services

    build_application_services(_wire_settings(tmp_path, profiling_enabled=False))

    assert fake_profiler_factory.calls == []
    assert fake_profiler_factory.profiler is None


def test_application_runtime_shares_profiler_wiring(
    tmp_path, fake_observability, fake_profiler_factory
):
    import app.main as main_module

    settings = _wire_settings(tmp_path, profiling_enabled=True)
    with main_module.application_runtime(settings):
        assert len(fake_profiler_factory.calls) == 1
        assert fake_profiler_factory.profiler.start_calls == 1
        assert fake_profiler_factory.profiler.registered_handles == [
            fake_observability.handle
        ]
    assert fake_observability.handle.shutdown_calls == 1


def test_create_app_lifespan_shares_profiler_wiring(
    tmp_path, fake_observability, fake_profiler_factory, monkeypatch
):
    import app.main as main_module

    monkeypatch.setattr(
        main_module, "_configure_logging", lambda: None
    )
    app = main_module.create_app(_wire_settings(tmp_path, profiling_enabled=True))
    # wiring happened exactly once during service construction
    assert len(fake_profiler_factory.calls) == 1
    assert fake_profiler_factory.profiler.start_calls == 1
    profiler = fake_profiler_factory.profiler
    assert profiler.registered_handles == [fake_observability.handle]

    import asyncio

    async def _run_lifespan():
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(_run_lifespan())
    # no duplicate wiring during lifespan; handle shut down exactly once
    assert len(fake_profiler_factory.calls) == 1
    assert profiler.start_calls == 1
    assert fake_observability.handle.shutdown_calls == 1


def test_main_injects_snapshot_and_base_urls_into_dashboard_router(
    tmp_path, fake_observability, fake_profiler_factory, monkeypatch
):
    """create_app passes the handle snapshot + three base_urls to the router."""
    import app.main as main_module

    captured = {}
    original = main_module.create_dashboard_router

    def spy(*args, **kwargs):
        captured["snapshot"] = kwargs.get("observability_snapshot")
        captured["grafana_metrics"] = kwargs.get("otel_grafana_metrics_base_url")
        captured["grafana_logs"] = kwargs.get("otel_grafana_logs_base_url")
        captured["grafana_profile"] = kwargs.get("otel_grafana_profile_base_url")
        captured["jaeger"] = kwargs.get("otel_jaeger_base_url")
        captured["service_name"] = kwargs.get("otel_service_name")
        return original(*args, **kwargs)

    monkeypatch.setattr(main_module, "create_dashboard_router", spy)
    monkeypatch.setattr(main_module, "_configure_logging", lambda: None)

    settings = _wire_settings(tmp_path, profiling_enabled=False)
    settings = settings.model_copy(
        update={
            "otel_grafana_metrics_base_url": "https://grafana.example.com/d/nagent-metrics",
            "otel_grafana_logs_base_url": "https://grafana.example.com/d/nagent-logs",
            "otel_grafana_profile_base_url": "https://grafana.example.com/d/nagent-profile",
            "otel_jaeger_base_url": "https://jaeger.example.com",
        }
    )
    main_module.create_app(settings)

    assert captured["snapshot"] is not None
    # the injected callable is the handle's snapshot bound method
    assert captured["snapshot"].__self__ is fake_observability.handle
    assert captured["grafana_metrics"] == "https://grafana.example.com/d/nagent-metrics"
    assert captured["grafana_logs"] == "https://grafana.example.com/d/nagent-logs"
    assert captured["grafana_profile"] == "https://grafana.example.com/d/nagent-profile"
    assert captured["jaeger"] == "https://jaeger.example.com"
    # service label injected from setup.SERVICE_NAME (single source)
    assert captured["service_name"] == "n-agent"


def test_trace_explore_link_uses_tempo_and_service_query():
    from urllib.parse import parse_qs, urlparse
    client = _build_client(snapshot=_fake_snapshot,
                           jaeger="https://grafana.example.com/grafana/explore")
    link = client.get("/chat/observations/otel").json()["links"]["Trace"]
    assert urlparse(link).path == "/grafana/explore"
    params = parse_qs(urlparse(link).query)
    panes = json.loads(params["panes"][0])
    pane = panes["trace"]
    assert pane["datasource"] == "nagent-tempo"
    assert pane["queries"][0]["query"] == '{resource.service.name="n-agent"}'
    assert pane["range"] == {"from": "now-1h", "to": "now"}

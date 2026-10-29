#!/usr/bin/env bash
# OTel observability E2E smoke (T10 of plan-261024-otel-observability).
#
# Runs entirely against the Docker deployment (host only orchestrates):
#   - business traffic via http://127.0.0.1:8201 (published n-agent port)
#   - Tempo /api/search and /api/traces/{id} via loopback 127.0.0.1:3200;
#     OTLP JSON normalization is handled by tempo_query.py.
#   - Prometheus / Pyroscope / Grafana: Prometheus has NO host port and is
#     queried from inside the independent observability project;
#     Grafana/Pyroscope are queried on their 127.0.0.1 host bindings
#
# Self-gating (plan item 23):
#   - the app switches are read from the running container:
#       N_AGENT_OTEL_ENABLED      -> full OTel assertion set
#       N_AGENT_PROFILING_ENABLED -> Profile (Pyroscope) assertion set
#   - with both switches off the script asserts the disabled behavior of
#     the self-check API and exits 0, so `run.sh all` stays green for
#     users without the shared observability stack.
#
# Deterministic fixture: a fixed fake OpenAI-compatible LLM server runs
# INSIDE the n-agent container (127.0.0.1:9999) and is wired through the
# real provider factory via the provider registry API
# (POST /chat/providers -> POST /chat/providers/{id}/activate -> swap).
# It deterministically answers the first round with a `calculator` tool
# call and the second round with a final message (LLM -> tool -> LLM),
# with fixed usage numbers, so metric deltas are exact. No real API.
# The original active provider is restored and the fake row deleted.
#
# Environment overlay (ratio=0 / budget denial / compression) temporarily
# rewrites docker/.env from a backup, recreates only the n-agent
# container, and restores everything afterwards.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DOCKER_DIR="$REPO_ROOT/docker"

BASE_URL="${N_AGENT_OTEL_SMOKE_BASE_URL:-http://127.0.0.1:8201}"
TEMPO_URL="http://127.0.0.1:3200"
PYROSCOPE_URL="http://127.0.0.1:4040"
GRAFANA_URL="http://127.0.0.1:13000"
DASH_UID="nagent-overview"

ENV_FILE="$DOCKER_DIR/.env"
ENV_BACKUP="$DOCKER_DIR/.env.otel-smoke.bak"

TEMPO_POLL_ATTEMPTS=60        # bounded polling: 60 x 1s = 60s per spec
PROM_POLL_ATTEMPTS=60
HEALTH_ATTEMPTS=90
STOP_TIMEOUT="${N_AGENT_COMPOSE_STOP_TIMEOUT:-10}"

RUN_TAG="otel-smoke-$(date +%s)"
# Credential-format sentinel: sanitized detail text is allowed, credentials are not.
SENTINEL="E2EOTELSENTINEL-$(date +%s)-$$"
FAKE_PORT=9999
FAKE_LLM_PATH="/tmp/fake_llm_otel_smoke.py"
FAKE_PROVIDER_NAME="otel-smoke-$RUN_TAG"
FAKE_MODEL="gpt-4o-mini"

SESSION_GOOD="otel-smoke-good-$RUN_TAG"
SESSION_FAIL="otel-smoke-fail-$RUN_TAG"
SESSION_CANCEL="otel-smoke-cancel-$RUN_TAG"
SESSION_OVERLAY="otel-smoke-overlay-$RUN_TAG"
SESSION_ZEROCALL="otel-smoke-zerocall-$RUN_TAG"
# valid 1x1 PNG data URL (validate_image_url accepts it; the fake provider
# has no vision capability, so an image turn is denied before provider.chat)
ONE_PIXEL_PNG="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="

OTEL_ENABLED=""
PROFILING_ENABLED=""
INSTANCE_ID=""

TMP_DIR="$(mktemp -d /tmp/otel-smoke-XXXXXX)"
FAKE_PROVIDER_ID=""
ORIG_PROVIDER_ID=""
FAKE_STARTED=0
ENV_DIRTY=0
CHAT_FINISH=""

# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------

compose() { (cd "$DOCKER_DIR" && docker compose "$@"); }
obs_compose() { sh "$DOCKER_DIR/observability/restart.sh" compose "$@"; }

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

http() { # http METHOD URL [curl args...] -> HTTP_STATUS / HTTP_BODY
  local method="$1" url="$2"
  shift 2
  local body_file="$TMP_DIR/resp-body.txt"
  HTTP_STATUS=$(curl -sS -o "$body_file" -w "%{http_code}" -X "$method" "$@" "$url" 2>/dev/null) || true
  HTTP_BODY="$(cat "$body_file" 2>/dev/null || true)"
}

assert_status() {
  [ "$HTTP_STATUS" = "$1" ] || fail "$2: expected HTTP $1, got $HTTP_STATUS body=$HTTP_BODY"
}

assert_contains() {
  echo "$HTTP_BODY" | grep -qF -- "$2" || fail "$1: body does not contain '$2'"
}

assert_not_contains() {
  if echo "$HTTP_BODY" | grep -qF -- "$2"; then
    fail "$1: body must NOT contain '$2'"
  fi
}

assert_no_sentinel() { # assert_no_sentinel <file> <outlet-label>
  if grep -qF -- "$SENTINEL" "$1" 2>/dev/null; then
    fail "sentinel leaked via $2"
  fi
}

json_field() { # json_field <jq-expr> (reads HTTP_BODY)
  echo "$HTTP_BODY" | jq -r "$1" 2>/dev/null
}

assert_cache_control_no_store() { # assert_cache_control_no_store <url> <label>
  local headers
  headers="$(curl -sS -D - -o /dev/null -X GET "$1" 2>/dev/null || true)"
  echo "$headers" | grep -qi 'cache-control: *no-store' \
    || fail "$2: Cache-Control no-store missing"
}

now_ms() { python3 -c 'import time; print(int(time.time() * 1000))'; }

wait_health() { # wait_health <attempts>
  local i
  for i in $(seq 1 "$1"); do
    if curl -fsS --connect-timeout 1 --max-time 3 "$BASE_URL/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  fail "n-agent container did not become healthy within $1 attempts"
}

app_env() { # app_env <VAR> -> value from the running container
  compose exec -T n-agent printenv "$1" 2>/dev/null | tr -d '\r\n'
}

recreate_nagent() { # recreate only n-agent to apply docker/.env changes
  echo "[otel-smoke] recreating n-agent container (stop timeout ${STOP_TIMEOUT}s)"
  (cd "$DOCKER_DIR" && N_AGENT_COMPOSE_STOP_TIMEOUT="$STOP_TIMEOUT" \
    docker compose up -d --force-recreate --no-deps n-agent) \
    || fail "n-agent container recreation failed"
  ENV_DIRTY=1
  wait_health "$HEALTH_ATTEMPTS"
}

# ---------------------------------------------------------------------------
# Prometheus helpers (queried from inside the compose network)
# ---------------------------------------------------------------------------

prom_query_raw() { # prom_query_raw <promql> -> raw JSON on stdout
  local url
  url="$(python3 -c 'import sys, urllib.parse; print("http://localhost:9090/api/v1/query?" + urllib.parse.urlencode({"query":sys.argv[1]}))' "$1")"
  obs_compose exec -T prometheus wget -q -T 15 -O - "$url"
}

prom_sum() { # prom_sum <promql> -> float (sum over series; 0 when absent)
  local raw
  if ! raw="$(prom_query_raw "$1")"; then
    echo "0"
    return
  fi
  PROM_PY_INPUT="$raw" python3 - <<'PY'
import json
import os

data = json.loads(os.environ["PROM_PY_INPUT"])
if data.get("status") != "success":
    print("0")
    raise SystemExit(0)
total = 0.0
for series in data.get("data", {}).get("result", []):
    try:
        total += float(series.get("value", [0, "0"])[1])
    except (TypeError, ValueError, IndexError):
        continue
print(total)
PY
}

prom_query_ok() { # prom_query_ok <promql> -> "success"/"error"
  local raw
  raw="$(prom_query_raw "$1")" || { echo "error"; return; }
  echo "$raw" | grep -q '"status":"success"' && echo "success" || echo "error"
}

prom_series_exists() { # prom_series_exists <selector> -> 0/1
  local raw
  raw="$(prom_query_raw "$1")" || return 1
  echo "$raw" | grep -q '"result":\[{' && return 0 || return 1
}

wait_prom_delta() { # wait_prom_delta <promql> <baseline> <min_delta> [attempts]
  local promql="$1" baseline="$2" min_delta="$3" attempts="${4:-$PROM_POLL_ATTEMPTS}"
  local i current
  current="0"
  for i in $(seq 1 "$attempts"); do
    current="$(prom_sum "$promql")"
    if python3 -c "
import sys
sys.exit(0 if float('''$current''') >= float('''$baseline''') + float('''$min_delta''') else 1)
"; then
      echo "[otel-smoke] prom delta ok: $promql baseline=$baseline now=$current"
      return 0
    fi
    sleep 1
  done
  fail "Prometheus increment not observed within ${attempts}s: $promql (baseline=$baseline last=$current)"
}

prom_stable_value() { # prom_stable_value <promql> [attempts] -> stabilized value
  local promql="$1" attempts="${2:-$PROM_POLL_ATTEMPTS}" i prev cur
  cur="$(prom_sum "$promql")"
  prev="$cur"
  for i in $(seq 1 "$attempts"); do
    sleep 2
    cur="$(prom_sum "$promql")"
    if [ "$cur" = "$prev" ]; then
      echo "$cur"
      return 0
    fi
    prev="$cur"
  done
  fail "metric never stabilized within $((attempts * 2))s: $promql (last=$cur)"
}

prom_assert_no_delta() { # prom_assert_no_delta <promql> <baseline> [attempts]
  local promql="$1" baseline="$2" attempts="${3:-10}" i cur
  for i in $(seq 1 "$attempts"); do
    cur="$(prom_sum "$promql")"
    [ "$cur" = "$baseline" ] \
      || fail "metric moved without any LLM invocation: $promql (baseline=$baseline now=$cur)"
    sleep 1
  done
}

# ---------------------------------------------------------------------------
# Tempo helpers (search + trace-by-id over the published loopback API)
# ---------------------------------------------------------------------------

tempo_search() { # tempo_search <startTimeMin> <startTimeMax> -> normalized OTLP JSON
  python3 "$SCRIPT_DIR/tempo_query.py" "$TEMPO_URL" "$1" "$2" "${3:-5}"
}

# Shared normalized OTLP JSON parser (written once per run).
install_tempo_parser() {
  cat > "$TMP_DIR/tempo_parse.py" <<'PYEOF'
"""Parse normalized Tempo OTLP-JSON responses into trace groups."""
import json
import sys


def _attr_map(span):
    out = {}
    for attr in span.get("attributes") or []:
        value = attr.get("value") or {}
        for kind in ("stringValue", "intValue", "boolValue", "doubleValue"):
            if kind in value:
                out[attr.get("key")] = value[kind]
                break
    return out


def _iter_spans(data):
    result = data.get("result") or {}
    resource_spans = result.get("resourceSpans")
    if resource_spans is None:
        # tolerate a plain traces list shape just in case
        for trace in data.get("traces") or []:
            for span in trace.get("spans") or []:
                yield span
        return
    for rs in resource_spans:
        for ss in rs.get("scopeSpans") or []:
            for span in ss.get("spans") or []:
                yield span


def group_traces(data):
    traces = {}
    for span in _iter_spans(data):
        traces.setdefault(span.get("traceId", ""), []).append(span)
    return traces


def _project(span):
    return {
        "name": span.get("name", ""),
        "spanId": span.get("spanId", ""),
        "parentSpanId": span.get("parentSpanId", ""),
        "traceId": span.get("traceId", ""),
        "attrs": _attr_map(span),
    }


if __name__ == "__main__":
    mode = sys.argv[1]
    with open(sys.argv[2]) as handle:
        data = json.load(handle)
    if mode == "count-traces":
        print(len(group_traces(data)))
    elif mode == "find-session":
        session = sys.argv[3]
        for trace_id, spans in group_traces(data).items():
            if any(
                _attr_map(s).get("gen_ai.conversation.id") == session
                and s.get("name", "").startswith("invoke_agent ")
                for s in spans
            ):
                print(json.dumps({
                    "traceId": trace_id,
                    "spans": [_project(s) for s in spans],
                }))
                raise SystemExit(0)
        raise SystemExit(1)
    elif mode == "check-good":
        with open(sys.argv[2]) as handle:
            trace = json.load(handle)
        spans = trace["spans"]

        def find(prefix):
            return [s for s in spans if s["name"].startswith(prefix)]

        roots = find("invoke_agent n-agent")
        assert roots, "missing invoke_agent root span"
        root = roots[0]
        http = next((s for s in spans if s["spanId"] == root["parentSpanId"]), None)
        assert http and http["name"] in ("POST /chat/completions", "POST /v1/chat/completions"), (
            "invoke_agent must be a child of its HTTP conversation ingress"
        )
        chats = find("chat ")
        assert chats, "missing chat span(s)"
        tools = find("execute_tool ")
        assert tools, "missing execute_tool span"
        tool = tools[0]
        assert tool["parentSpanId"] == root["spanId"], (
            "execute_tool must be a direct child of invoke_agent"
        )
        chat = next(c for c in chats if c["parentSpanId"] == root["spanId"])
        assert chat["attrs"].get("gen_ai.request.model") == "gpt-4o-mini", (
            chat["attrs"]
        )
        assert chat["attrs"].get("gen_ai.provider.name") == "openai"
        assert chat["attrs"].get("gen_ai.operation.name") == "chat"
        assert tool["attrs"].get("gen_ai.tool.name") == "calculator"
        assert tool["attrs"].get("nagent.policy_outcome") == "allow", tool["attrs"]
        assert tool["attrs"].get("gen_ai.tool.type") == "function"
        assert root["attrs"].get("gen_ai.operation.name") == "invoke_agent"
        assert root["attrs"].get("gen_ai.agent.name") == "n-agent"
        # chat and execute_tool are siblings of the same run
        assert all(s["traceId"] == root["traceId"] for s in spans)
        print(
            "tempo hierarchy ok: trace=%s spans=%d"
            % (root["traceId"], len(spans))
        )
    elif mode == "check-failure":
        with open(sys.argv[2]) as handle:
            trace = json.load(handle)
        spans = trace["spans"]
        error_spans = [
            s for s in spans if s["attrs"].get("error.type") == "APIStatusError"
        ]
        assert error_spans, (
            "expected a span with safe error.type=APIStatusError, got: %s"
            % [(s["name"], s["attrs"]) for s in spans]
        )
        for span in spans:
            for key in ("exception.message", "exception.stacktrace", "error.message"):
                assert key not in span["attrs"], (span["name"], key)
        print("failure trace ok: error.type=APIStatusError recorded")
    else:
        raise SystemExit("unknown mode: %s" % mode)
PYEOF
}

tempo_find_trace() { # tempo_find_trace <session_id> <startTimeMin> [attempts]
  local session="$1" t0="$2" attempts="${3:-$TEMPO_POLL_ATTEMPTS}"
  local i remaining deadline=$((SECONDS + TEMPO_POLL_ATTEMPTS))
  for i in $(seq 1 "$attempts"); do
    remaining=$((deadline - SECONDS))
    [ "$remaining" -gt 0 ] || break
    if ! tempo_search "$t0" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$remaining" > "$TMP_DIR/tempo.json"; then
      sleep 1
      continue
    fi
    if TRACE_OUT="$(python3 "$TMP_DIR/tempo_parse.py" find-session "$TMP_DIR/tempo.json" "$session")"; then
      echo "$TRACE_OUT"
      return 0
    fi
    sleep 1
  done
  return 1
}

# ---------------------------------------------------------------------------
# deterministic fake OpenAI-compatible provider (runs in the n-agent box)
# ---------------------------------------------------------------------------

install_fake_llm() {
  compose exec -T n-agent sh -c "cat > $FAKE_LLM_PATH" <<'PYEOF'
"""Deterministic OpenAI-compatible fake LLM for the observability smoke.

Round 1 (no tool message in history): tool_call for `calculator`
{"expression": "1+1"}, usage prompt=10 completion=5.
Round 2 (a tool message exists): final content, usage prompt=40
completion=20. A user message containing FORCE-LLM-500 makes the server
return HTTP 500 (failure path). Fully deterministic; no external I/O.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9999

TOOL_NAME = "calculator"
TOOL_ARGS = "{\"expression\": \"1+1\"}"
FINAL_TEXT = "TOOL-ROUND-COMPLETE answer=2"
USAGE_TOOL = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
USAGE_FINAL = {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60}


def _wants_error(messages):
    return any(
        isinstance(m.get("content"), str) and "FORCE-LLM-500" in m.get("content")
        for m in messages
    )


def _has_tool_result(messages):
    return any(m.get("role") == "tool" for m in messages)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/health":
            body = b'{"status": "ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def _base(self, model, message, finish_reason, usage):
        return {
            "id": "chatcmpl-otel-smoke",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }],
            "usage": dict(usage),
        }

    def _tool_call_completion(self, model):
        return self._base(
            model,
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call-otel-smoke",
                    "type": "function",
                    "function": {"name": TOOL_NAME, "arguments": TOOL_ARGS},
                }],
            },
            "tool_calls",
            USAGE_TOOL,
        )

    def _final_completion(self, model):
        return self._base(
            model,
            {"role": "assistant", "content": FINAL_TEXT},
            "stop",
            USAGE_FINAL,
        )

    def _sse(self, chunks):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for chunk in chunks:
            data = ("data: " + json.dumps(chunk) + "\n\n").encode()
            self.wfile.write(("%x\r\n" % len(data)).encode() + data + b"\r\n")
            self.wfile.flush()
        done = b"data: [DONE]\n\n"
        self.wfile.write(("%x\r\n" % len(done)).encode() + done + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self.send_error(400)
            return
        messages = payload.get("messages", [])
        model = payload.get("model", "unknown")
        stream = bool(payload.get("stream"))
        if _wants_error(messages):
            body = json.dumps({
                "error": {"message": "otel-smoke forced failure", "type": "server_error"}
            }).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # "tools" is a top-level request field in the OpenAI schema
        if _has_tool_result(messages) or not payload.get("tools"):
            completion = self._final_completion(model)
        else:
            completion = self._tool_call_completion(model)
        if not stream:
            body = json.dumps(completion).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        choice = completion["choices"][0]
        usage_chunk = {
            "id": "chatcmpl-otel-smoke",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [],
            "usage": dict(completion["usage"]),
        }

        def chunk(delta, finish=None):
            return {
                "id": "chatcmpl-otel-smoke",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }

        if choice["message"].get("tool_calls"):
            chunks = [
                chunk({"role": "assistant", "content": None}),
                chunk({"tool_calls": [{
                    "index": 0,
                    "id": "call-otel-smoke",
                    "type": "function",
                    "function": {"name": TOOL_NAME, "arguments": TOOL_ARGS},
                }]}),
                chunk({}, finish="tool_calls"),
                usage_chunk,
            ]
        else:
            chunks = [
                chunk({"role": "assistant", "content": None}),
                chunk({"content": FINAL_TEXT}),
                chunk({}, finish="stop"),
                usage_chunk,
            ]
        self._sse(chunks)


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
PYEOF
}

start_fake_llm() {
  install_fake_llm
  compose exec -d n-agent python3 "$FAKE_LLM_PATH" "$FAKE_PORT" \
    || fail "fake LLM server failed to start"
  FAKE_STARTED=1
  local i
  for i in $(seq 1 20); do
    if compose exec -T n-agent python3 -c "
import urllib.request
urllib.request.urlopen('http://127.0.0.1:$FAKE_PORT/health', timeout=2).read()
" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  fail "fake LLM server did not become reachable"
}

stop_fake_llm() {
  [ "$FAKE_STARTED" = "1" ] || return 0
  compose exec -T n-agent pkill -f "$FAKE_LLM_PATH" >/dev/null 2>&1 || true
  FAKE_STARTED=0
}

# ---------------------------------------------------------------------------
# provider fixture (via the real provider factory path) + restore
# ---------------------------------------------------------------------------

record_original_provider() {
  http GET "$BASE_URL/chat/providers"
  assert_status 200 "list providers"
  # The active provider may be a leftover smoke fixture from an earlier
  # crashed run (its cleanup could not reach the API): never treat our own
  # otel-smoke-* fixtures as the original to restore.
  ORIG_PROVIDER_ID="$(json_field '[.[] | select(.is_active and (.name | startswith("otel-smoke-") | not))][0].id // ""')"
  echo "[otel-smoke] original active provider: ${ORIG_PROVIDER_ID:-<none>}"
}

install_fake_provider() {
  http POST "$BASE_URL/chat/providers" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg name "$FAKE_PROVIDER_NAME" --arg model "$FAKE_MODEL" \
      '{name:$name, base_url:"http://127.0.0.1:'"$FAKE_PORT"'/v1", model:$model, api_key:"otel-smoke", provider_type:"openai-compatible"}')"
  assert_status 200 "create fake provider"
  FAKE_PROVIDER_ID="$(json_field '.id')"
  [ -n "$FAKE_PROVIDER_ID" ] && [ "$FAKE_PROVIDER_ID" != "null" ] \
    || fail "fake provider id missing"
  http POST "$BASE_URL/chat/providers/$FAKE_PROVIDER_ID/activate"
  assert_status 200 "activate fake provider"
}

restore_provider() {
  [ -n "$FAKE_PROVIDER_ID" ] || return 0
  if [ -n "$ORIG_PROVIDER_ID" ]; then
    # re-activate BEFORE retry loops: a crashed earlier recreate may have
    # left the API unreachable, so retry while the app comes back
    local i
    for i in $(seq 1 30); do
      http POST "$BASE_URL/chat/providers/$ORIG_PROVIDER_ID/activate"
      [ "$HTTP_STATUS" = "200" ] && break
      sleep 2
    done
    echo "[otel-smoke] re-activated original provider $ORIG_PROVIDER_ID"
  else
    echo "WARN: no original registry provider; fake provider stays active until next restart" >&2
  fi
  local j
  for j in $(seq 1 10); do
    http DELETE "$BASE_URL/chat/providers/$FAKE_PROVIDER_ID"
    { [ "$HTTP_STATUS" = "204" ] || [ "$HTTP_STATUS" = "404" ]; } && break
    sleep 2
  done
  FAKE_PROVIDER_ID=""
}

# ---------------------------------------------------------------------------
# env overlay (ratio=0 + budget denial + compression trigger) + restore
# ---------------------------------------------------------------------------

apply_env_overlay() {
  [ -f "$ENV_FILE" ] || fail "docker/.env not found (overlay phase requires it)"
  cp "$ENV_FILE" "$ENV_BACKUP"
  ENV_DIRTY=1
  python3 - "$ENV_FILE" <<'PY'
import sys

path = sys.argv[1]
overrides = {
    "N_AGENT_OTEL_TRACES_SAMPLE_RATIO": "0",
    "N_AGENT_BUDGET_MAX_LLM_CALLS": "1",
    "N_AGENT_CONTEXT_LENGTH": "1024",
    "N_AGENT_CONTEXT_COMPRESSION_THRESHOLD": "0.01",
    # validation requires target_ratio < threshold (reject-to-start rule)
    "N_AGENT_CONTEXT_COMPRESSION_TARGET_RATIO": "0.001",
}
with open(path, encoding="utf-8") as handle:
    lines = handle.readlines()
kept = [
    line for line in lines
    if line.split("=", 1)[0].strip() not in overrides
]
kept.extend(f"{key}={value}\n" for key, value in overrides.items())
with open(path, "w", encoding="utf-8") as handle:
    handle.writelines(kept)
PY
}

restore_env() {
  [ "$ENV_DIRTY" = "1" ] || return 0
  if [ -f "$ENV_BACKUP" ]; then
    mv "$ENV_BACKUP" "$ENV_FILE"
    echo "[otel-smoke] restored docker/.env"
  fi
  ENV_DIRTY=0
}

# ---------------------------------------------------------------------------
# chat driver (SSE)
# ---------------------------------------------------------------------------

create_session() { # create_session <session_id>
  http POST "$BASE_URL/chat/sessions?session_id=$1"
  assert_status 200 "create session $1"
}

delete_session() {
  http DELETE "$BASE_URL/chat/sessions/$1" || true
}

# chat_turn <session> <content> [--max-time S] -> writes the SSE transcript
# to $TMP_DIR/sse.txt and sets CHAT_FINISH (stop/tool_calls/error).
chat_turn() {
  local session="$1" content="$2" maxtime="${3:-60}"
  local payload
  payload="$(jq -n --arg c "$content" \
    --arg model "$FAKE_MODEL" \
    '{model:$model, messages:[{role:"user", content:$c}], stream:true}')"
  curl -sS --max-time "$maxtime" -N \
    -X POST "$BASE_URL/chat/completions" \
    -H "Content-Type: application/json" \
    -H "X-Session-ID: $session" \
    -H "X-E2E-Sentinel: $SENTINEL" \
    -d "$payload" > "$TMP_DIR/sse.txt" 2>/dev/null || true
  # answer pending tool approvals, then reduce the transcript to a finish reason
  local confirmation
  confirmation="$(grep -o '"confirmation_id": *"[^"]*"' "$TMP_DIR/sse.txt" | head -1 | sed 's/.*: *"//; s/"$//')"
  if [ -n "$confirmation" ]; then
    echo "[otel-smoke] approving tool confirmation $confirmation"
    http POST "$BASE_URL/chat/tool-approvals/$confirmation" \
      -H "Content-Type: application/json" \
      -H "X-Session-ID: $session" \
      -d '{"choice":"once"}' || true
  fi
  CHAT_FINISH="$(SSE_FILE="$TMP_DIR/sse.txt" python3 - <<'PY'
import json
import os

finish = "error"
with open(os.environ["SSE_FILE"]) as handle:
    for line in handle:
        line = line.strip()
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        try:
            chunk = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        if chunk.get("object") == "n-agent.tool_approval":
            continue
        for choice in chunk.get("choices", []):
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
print(finish)
PY
)"
}

utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

instance_label() { # PromQL label matcher for this app instance
  echo "service_instance_id=\"$INSTANCE_ID\""
}

# ---------------------------------------------------------------------------
# disabled path (self-gating)
# ---------------------------------------------------------------------------

run_disabled_checks() {
  echo "[otel-smoke] observability disabled: asserting disabled behavior"
  http GET "$BASE_URL/chat/observations/otel"
  assert_status 200 "self-check API (disabled)"
  assert_cache_control_no_store "$BASE_URL/chat/observations/otel" "self-check API (disabled)"
  local status mode
  status="$(json_field '.status')"
  mode="$(json_field '.mode')"
  # status contract: both switches closed -> disabled; profiling-only mode
  # may report healthy/degraded, but the OTel signals stay disabled.
  if [ "$PROFILING_ENABLED" != "true" ]; then
    [ "$status" = "disabled" ] || fail "disabled environment reported status=$status"
  fi
  [ "$mode" = "disabled" ] || fail "disabled environment reported mode=$mode"
  local signal delivery
  for signal in traces metrics logs; do
    delivery="$(json_field ".signals.$signal.delivery")"
    [ "$delivery" = "disabled" ] || fail "signal $signal delivery=$delivery (expected disabled)"
  done
  local span_count
  span_count="$(json_field '.spans | length')"
  [ "$span_count" = "0" ] || fail "disabled snapshot must carry no spans (got $span_count)"
  # the instrument list is still exposed (semconv version identifier)
  assert_contains "semconv version exposed" "semantic-conventions-genai"
  echo "[otel-smoke] PASS (disabled)"
  return 0
}

# ---------------------------------------------------------------------------
# OTel enabled path
# ---------------------------------------------------------------------------

check_execution_summaries() {
  local trace_file="$1" since="$2"
  python3 "$SCRIPT_DIR/check_execution_summary.py" stderr "$trace_file" "$TMP_DIR/nagent-stderr.log" \
    || fail "stderr execution summaries differ from Tempo spans"
  python3 "$SCRIPT_DIR/check_execution_summary.py" fetch "$TRACE_ID" "$since" "$DOCKER_DIR/observability/restart.sh" > "$TMP_DIR/loki-summary.json" \
    || fail "Loki completion delivery failed"
  assert_no_sentinel "$TMP_DIR/loki-summary.json" "Loki completion logs"
  python3 "$SCRIPT_DIR/check_execution_summary.py" loki "$trace_file" "$TMP_DIR/loki-summary.json" \
    || fail "Loki summaries differ from Tempo spans or contain indexed identifiers"
}

run_otel_checks() {
  local t_turn t_fail t_cancel

  install_tempo_parser
  echo "[otel-smoke] Section 0: self-check preflight"
  http GET "$BASE_URL/chat/observations/otel"
  assert_status 200 "self-check API"
  INSTANCE_ID="$(json_field '.instance_id')"
  [ -n "$INSTANCE_ID" ] && [ "$INSTANCE_ID" != "null" ] || fail "instance_id missing"
  local status
  status="$(json_field '.status')"
  [ "$status" = "healthy" ] || [ "$status" = "degraded" ] || fail "self-check status=$status"
  echo "[otel-smoke] instance_id=$INSTANCE_ID status=$status"

  echo "[otel-smoke] Section 1: deterministic provider fixture"
  start_fake_llm
  record_original_provider
  install_fake_provider

  # baseline before the measured turn (isolated per instance, item 24)
  local base_turns base_in_tok base_out_tok base_inf_dur base_tool_dur
  local base_agent_dur base_inf_calls_sum base_inf_calls_cnt base_tool_calls_sum
  base_turns="$(prom_sum "sum(nagent_turns_total{$(instance_label)})")"
  base_in_tok="$(prom_sum "sum(gen_ai_client_inference_usage_input_tokens_total{$(instance_label)})")"
  base_out_tok="$(prom_sum "sum(gen_ai_client_inference_usage_output_tokens_total{$(instance_label)})")"
  base_inf_dur="$(prom_sum "sum(gen_ai_client_inference_duration_seconds_count{$(instance_label)})")"
  base_tool_dur="$(prom_sum "sum(gen_ai_execute_tool_duration_seconds_count{$(instance_label)})")"
  base_agent_dur="$(prom_sum "sum(gen_ai_invoke_agent_duration_seconds_count{$(instance_label)})")"
  base_inf_calls_sum="$(prom_sum "sum(gen_ai_invoke_agent_inference_calls_sum{$(instance_label)})")"
  base_inf_calls_cnt="$(prom_sum "sum(gen_ai_invoke_agent_inference_calls_count{$(instance_label)})")"
  base_tool_calls_sum="$(prom_sum "sum(gen_ai_invoke_agent_tool_calls_sum{$(instance_label)})")"
  echo "[otel-smoke] baseline: turns=$base_turns in=$base_in_tok out=$base_out_tok inf_dur=$base_inf_dur"

  echo "[otel-smoke] Section 2: good turn (LLM -> calculator tool -> LLM)"
  create_session "$SESSION_GOOD"
  t_turn="$(utc_now)"
  chat_turn "$SESSION_GOOD" "api_key=$SENTINEL use the calculator tool for 1+1 then answer" 90
  [ "$CHAT_FINISH" = "stop" ] || fail "good turn did not finish cleanly (finish=$CHAT_FINISH)"

  echo "[otel-smoke] Section 3: Tempo trace hierarchy (60s bounded poll)"
  if ! TRACE_JSON="$(tempo_find_trace "$SESSION_GOOD" "$t_turn")"; then
    fail "invoke_agent trace for $SESSION_GOOD not found in Tempo within 60s"
  fi
  echo "$TRACE_JSON" > "$TMP_DIR/trace-good.json"
  assert_no_sentinel "$TMP_DIR/trace-good.json" "Tempo trace payload"
  TRACE_ID="$(python3 -c "
import json, sys
trace = json.load(open('$TMP_DIR/trace-good.json'))
print(trace['traceId'])
")"
  [ -n "$TRACE_ID" ] || fail "invoke_agent traceId missing"
  python3 "$TMP_DIR/tempo_parse.py" check-good "$TMP_DIR/trace-good.json" \
    || fail "Tempo good trace hierarchy invalid"
  echo "[otel-smoke] Section 3 ok"

  echo "[otel-smoke] Section 4: Prometheus GenAI metric increments"
  wait_prom_delta "sum(nagent_turns_total{$(instance_label)})" "$base_turns" 1
  wait_prom_delta "sum(gen_ai_client_inference_usage_input_tokens_total{$(instance_label)})" "$base_in_tok" 50
  wait_prom_delta "sum(gen_ai_client_inference_usage_output_tokens_total{$(instance_label)})" "$base_out_tok" 25
  wait_prom_delta "sum(gen_ai_client_inference_duration_seconds_count{$(instance_label)})" "$base_inf_dur" 2
  wait_prom_delta "sum(gen_ai_execute_tool_duration_seconds_count{$(instance_label)})" "$base_tool_dur" 1
  wait_prom_delta "sum(gen_ai_invoke_agent_duration_seconds_count{$(instance_label)})" "$base_agent_dur" 1
  wait_prom_delta "sum(gen_ai_invoke_agent_inference_calls_sum{$(instance_label)})" "$base_inf_calls_sum" 2
  wait_prom_delta "sum(gen_ai_invoke_agent_inference_calls_count{$(instance_label)})" "$base_inf_calls_cnt" 1
  wait_prom_delta "sum(gen_ai_invoke_agent_tool_calls_sum{$(instance_label)})" "$base_tool_calls_sum" 1
  # every locked histogram exposes bucket/sum/count series (T9 mapping)
  local hist counter
  for hist in \
    gen_ai_client_inference_duration_seconds \
    gen_ai_execute_tool_duration_seconds \
    gen_ai_invoke_agent_duration_seconds \
    gen_ai_invoke_agent_inference_calls \
    gen_ai_invoke_agent_tool_calls; do
    prom_series_exists "${hist}_bucket" \
      || fail "histogram _bucket series missing for $hist"
  done
  for counter in \
    gen_ai_client_inference_usage_input_tokens_total \
    gen_ai_client_inference_usage_output_tokens_total \
    nagent_turns_total; do
    prom_series_exists "$counter" || fail "counter series missing: $counter"
  done
  echo "[otel-smoke] checking Prometheus exemplar -> Tempo Trace -> Loki"
  local exemplar_attempt exemplar_ok=0
  for exemplar_attempt in $(seq 1 "$PROM_POLL_ATTEMPTS"); do
    local exemplar_url exemplar_json
    exemplar_url="$(python3 -c 'import time, urllib.parse; print("http://localhost:9090/api/v1/query_exemplars?" + urllib.parse.urlencode({"query": "gen_ai_invoke_agent_duration_seconds_bucket{service_name=\"n-agent\"}", "start": time.time()-600, "end": time.time()}))')"
    if exemplar_json="$(obs_compose exec -T prometheus wget -q -T 5 -O - "$exemplar_url")" \
      && python3 -c 'import json,sys; p=json.loads(sys.argv[1]); assert p["status"] == "success"; assert any(e.get("labels",{}).get("trace_id")==sys.argv[2] for s in p["data"] for e in s.get("exemplars",[]))' "$exemplar_json" "$TRACE_ID"
    then
      exemplar_ok=1
      break
    fi
    sleep 1
  done
  [ "$exemplar_ok" = "1" ] || fail "Prometheus exemplar missing for Tempo trace"
  echo "[otel-smoke] Section 4 ok"

  echo "[otel-smoke] Section 5: log delivery + trace correlation"
  assert_no_sentinel "$TMP_DIR/sse.txt" "chat SSE response"
  local i log_ok=1
  for i in $(seq 1 30); do
    compose logs n-agent --since "$t_turn" > "$TMP_DIR/nagent-stderr.log" 2>/dev/null || true
    if grep -q "llm_api_call" "$TMP_DIR/nagent-stderr.log" \
      && LOG_TRACE_FILE="$TMP_DIR/nagent-stderr.log" EXPECTED_TRACE="$TRACE_ID" python3 - <<'PY'
import json
import os
import sys

expected = os.environ["EXPECTED_TRACE"]
for line in open(os.environ["LOG_TRACE_FILE"]):
    line = line.strip()
    if '"llm_api_call"' not in line:
        continue
    # strip the "n-agent-1  | " docker compose log prefix
    brace = line.find("{")
    if brace < 0:
        continue
    try:
        payload = json.loads(line[brace:])
    except json.JSONDecodeError:
        continue
    if payload.get("event") == "llm_api_call" and payload.get("trace_id") == expected:
        sys.exit(0)
sys.exit(1)
PY
    then
      log_ok=0
      break
    fi
    sleep 2
  done
  [ "$log_ok" = "0" ] || fail "stderr JSON log with event=llm_api_call and matching trace_id not found"
  assert_no_sentinel "$TMP_DIR/nagent-stderr.log" "application stderr JSON logs"
  grep -q '"trace_id":null' "$TMP_DIR/nagent-stderr.log" \
    || echo "WARN: no active-span-free log line (trace_id null) in the sampled window" >&2
  # collector stdout keeps the structured body + correlation fields
  local collector_ok=1
  for i in $(seq 1 30); do
    obs_compose logs otel-collector --since "$t_turn" > "$TMP_DIR/collector.log" 2>/dev/null || true
    if grep -q "llm_api_call" "$TMP_DIR/collector.log" \
      && grep -qF "$TRACE_ID" "$TMP_DIR/collector.log"; then
      collector_ok=0
      break
    fi
    sleep 2
  done
  [ "$collector_ok" = "0" ] || fail "collector debug exporter did not deliver llm_api_call log with trace_id"
  assert_no_sentinel "$TMP_DIR/collector.log" "collector log delivery"
  check_execution_summaries "$TMP_DIR/trace-good.json" "$t_turn"
  echo "[otel-smoke] Section 5 ok"
  if [ "${N_AGENT_OTEL_SMOKE_SUMMARY_ONLY:-false}" = "true" ]; then
    return 0
  fi

  echo "[otel-smoke] Section 6: self-check healthy + ring buffer + no leak"
  local selfcheck_ok=1
  for i in $(seq 1 30); do
    http GET "$BASE_URL/chat/observations/otel"
    if [ "$(json_field '.status')" = "healthy" ] \
      && [ "$(json_field '.signals.traces.delivery')" = "succeeded" ] \
      && [ "$(json_field '.signals.metrics.delivery')" = "succeeded" ]; then
      selfcheck_ok=0
      break
    fi
    sleep 2
  done
  [ "$selfcheck_ok" = "0" ] || fail "self-check did not reach healthy with succeeded delivery"
  assert_cache_control_no_store "$BASE_URL/chat/observations/otel" "self-check API"
  assert_contains "self-check semconv version" "semantic-conventions-genai"
  assert_contains "self-check ring buffer holds invoke_agent" "invoke_agent"
  local endpoint_field
  endpoint_field="$(json_field '.exporter_endpoint')"
  case "$endpoint_field" in
    *"://"*) fail "exporter_endpoint is not sanitized: $endpoint_field" ;;
  esac
  http GET "$BASE_URL/chat/observations/otel"
  echo "$HTTP_BODY" > "$TMP_DIR/selfcheck.json"
  assert_no_sentinel "$TMP_DIR/selfcheck.json" "self-check API payload"
  echo "[otel-smoke] Section 6 ok"

  echo "[otel-smoke] Section 7: failure turn (forced provider 500)"
  create_session "$SESSION_FAIL"
  local base_err_dur
  base_err_dur="$(prom_sum "sum(gen_ai_client_inference_duration_seconds_count{$(instance_label),error_type!=''})")"
  t_fail="$(utc_now)"
  chat_turn "$SESSION_FAIL" "FORCE-LLM-500 api_key=${SENTINEL}" 90
  if ! TRACE_JSON="$(tempo_find_trace "$SESSION_FAIL" "$t_fail")"; then
    fail "failure trace for $SESSION_FAIL not found in Tempo"
  fi
  echo "$TRACE_JSON" > "$TMP_DIR/trace-fail.json"
  assert_no_sentinel "$TMP_DIR/trace-fail.json" "failure Tempo trace"
  python3 "$TMP_DIR/tempo_parse.py" check-failure "$TMP_DIR/trace-fail.json"
  wait_prom_delta "sum(gen_ai_client_inference_duration_seconds_count{$(instance_label),error_type!=''})" "$base_err_dur" 1
  echo "[otel-smoke] Section 7 ok"

  echo "[otel-smoke] Section 8: disconnect/cancel turn (span closes with the run)"
  create_session "$SESSION_CANCEL"
  t_cancel="$(utc_now)"
  # disconnect immediately: the detached run must still finish and close
  chat_turn "$SESSION_CANCEL" "api_key=$SENTINEL disconnect probe" 2
  if ! TRACE_JSON="$(tempo_find_trace "$SESSION_CANCEL" "$t_cancel")"; then
    fail "cancel-path trace for $SESSION_CANCEL never closed in Tempo"
  fi
  echo "[otel-smoke] Section 8 ok (invoke_agent span closed after disconnect)"

  echo "[otel-smoke] Section 9: Grafana dashboard + p95 PromQL"
  http GET "$GRAFANA_URL/api/dashboards/uid/$DASH_UID"
  assert_status 200 "Grafana dashboard $DASH_UID"
  [ "$(json_field '.dashboard.uid')" = "$DASH_UID" ] \
    || fail "dashboard uid mismatch: $(json_field '.dashboard.uid')"
  assert_contains "dashboard p95 query" "histogram_quantile"
  local p95_status
  p95_status="$(prom_query_ok "histogram_quantile(0.95, sum by (le) (rate(gen_ai_client_inference_duration_seconds_bucket[5m])))")"
  [ "$p95_status" = "success" ] || fail "p95 PromQL did not execute successfully"
  echo "[otel-smoke] Section 9 ok"

  echo "[otel-smoke] Section 9b: zero-LLM-call run (no gen_ai usage delta)"
  # Vision-deny path: the fixture provider declares no vision capability, so
  # an image turn is rejected before provider.chat -- a completed run with
  # zero LLM invocations (plan item 24). Token counters and the inference
  # duration series must not move at all; per-run histograms may still
  # record a 0 observation (plan item 11), so they are not asserted here.
  create_session "$SESSION_ZEROCALL"
  local z_base_in z_base_out z_base_inf z_reply
  z_base_in="$(prom_stable_value "sum(gen_ai_client_inference_usage_input_tokens_total{$(instance_label)})")"
  z_base_out="$(prom_stable_value "sum(gen_ai_client_inference_usage_output_tokens_total{$(instance_label)})")"
  z_base_inf="$(prom_stable_value "sum(gen_ai_client_inference_duration_seconds_count{$(instance_label)})")"
  curl -sS --max-time 60 -N -X POST "$BASE_URL/chat/completions" \
    -H "Content-Type: application/json" \
    -H "X-Session-ID: $SESSION_ZEROCALL" \
    -H "X-E2E-Sentinel: $SENTINEL" \
    -d "$(jq -n --arg model "$FAKE_MODEL" --arg img "$ONE_PIXEL_PNG" \
      '{model:$model, stream:true,
        messages:[{role:"user", content:[
          {type:"text", text:"zero-call vision probe"},
          {type:"image_url", image_url:{url:$img}}]}]}')" \
    > "$TMP_DIR/sse-zero.txt" 2>/dev/null || true
  z_reply="$(grep -o "不支持图片输入" "$TMP_DIR/sse-zero.txt" | head -1)"
  [ -n "$z_reply" ] || fail "zero-call probe did not take the vision-deny path"
  assert_no_sentinel "$TMP_DIR/sse-zero.txt" "zero-call SSE response"
  prom_assert_no_delta "sum(gen_ai_client_inference_usage_input_tokens_total{$(instance_label)})" "$z_base_in"
  prom_assert_no_delta "sum(gen_ai_client_inference_usage_output_tokens_total{$(instance_label)})" "$z_base_out"
  prom_assert_no_delta "sum(gen_ai_client_inference_duration_seconds_count{$(instance_label)})" "$z_base_inf"
  echo "[otel-smoke] Section 9b ok (zero-call run left gen_ai usage untouched)"

  echo "[otel-smoke] Section 10: ratio=0 + budget denial + compression (env overlay)"
  apply_env_overlay
  recreate_nagent
  start_fake_llm
  # new process -> new instance id
  http GET "$BASE_URL/chat/observations/otel"
  INSTANCE_ID="$(json_field '.instance_id')"
  [ -n "$INSTANCE_ID" ] && [ "$INSTANCE_ID" != "null" ] || fail "instance_id missing after recreate"
  local o_turns o_denied o_comp o_in_tok
  o_turns="$(prom_sum "sum(nagent_turns_total{$(instance_label)})")"
  o_denied="$(prom_sum "sum(nagent_budget_denied_total{$(instance_label)})")"
  o_comp="$(prom_sum "sum(nagent_context_compressions_total{$(instance_label)})")"
  o_in_tok="$(prom_sum "sum(gen_ai_client_inference_usage_input_tokens_total{$(instance_label)})")"
  create_session "$SESSION_OVERLAY"
  local t0_overlay t1_overlay
  t0_overlay="$(utc_now)"
  chat_turn "$SESSION_OVERLAY" "api_key=$SENTINEL overlay run" 90
  wait_prom_delta "sum(nagent_turns_total{$(instance_label)})" "$o_turns" 1
  wait_prom_delta "sum(nagent_budget_denied_total{$(instance_label)})" "$o_denied" 1
  wait_prom_delta "sum(nagent_context_compressions_total{$(instance_label)})" "$o_comp" 1
  wait_prom_delta "sum(gen_ai_client_inference_usage_input_tokens_total{$(instance_label)})" "$o_in_tok" 1
  # ratio=0 with no remote parent: no sampled local root spans at all.
  # Traces share the batch pipeline with metrics: re-check the fixed window
  # over ~30s (6 attempts x 5s) so a late-exported trace batch cannot slip
  # past a single query; Tempo data is append-only, so any non-zero count
  # is an immediate failure.
  t1_overlay="$(utc_now)"
  local traces_in_window ratio0_attempt
  for ratio0_attempt in $(seq 1 6); do
    tempo_search "$t0_overlay" "$t1_overlay" > "$TMP_DIR/tempo-ratio0.json" || fail "Tempo ratio=0 query failed"
    traces_in_window="$(python3 "$TMP_DIR/tempo_parse.py" count-traces "$TMP_DIR/tempo-ratio0.json")"
    [ "$traces_in_window" = "0" ] || fail "ratio=0 produced $traces_in_window sampled traces (expected none, attempt $ratio0_attempt)"
    [ "$ratio0_attempt" -eq 6 ] || sleep 5
  done
  echo "[otel-smoke] Section 10 ok (metrics grew, zero sampled spans)"
  restore_env
  recreate_nagent
  echo "[otel-smoke] Section 10 env restored"

  # provider cleanup happens in the global cleanup trap
}

# ---------------------------------------------------------------------------
# Profile branch (Pyroscope; requires the profiling compose override)
# ---------------------------------------------------------------------------

pyroscope_render() { # pyroscope_render <from_ms> <until_ms> -> body
  local query='process_cpu:cpu:nanoseconds:cpu:nanoseconds{service_name="n-agent"}'
  curl -fsS -G "$PYROSCOPE_URL/pyroscope/render" \
    --data-urlencode "query=$query" \
    --data-urlencode "from=$1" \
    --data-urlencode "until=$2" 2>/dev/null || echo ""
}

run_profile_checks() {
  echo "[otel-smoke] Section P: profiling two consecutive windows"
  local interval wait_seconds
  interval="$(app_env N_AGENT_PROFILING_INTERVAL_SECONDS)"
  interval="${interval:-60}"
  case "$interval" in ''|*[!0-9]*) interval=60 ;; esac
  wait_seconds=$((interval * 2 + 15))
  echo "[otel-smoke] waiting ${wait_seconds}s for two profiling windows (interval=${interval}s)"
  sleep "$wait_seconds"
  local now until1 from1 until2 from2
  now="$(now_ms)"
  until1=$((now - interval * 1000))
  from1=$((until1 - interval * 1000))
  until2="$now"
  from2="$until1"
  local render1 render2
  render1="$(pyroscope_render "$from1" "$until1")"
  render2="$(pyroscope_render "$from2" "$until2")"
  [ -n "$render1" ] || fail "Pyroscope window 1 returned no data"
  [ -n "$render2" ] || fail "Pyroscope window 2 returned no data"
  echo "$render1" > "$TMP_DIR/profile1.json"
  echo "$render2" > "$TMP_DIR/profile2.json"
  assert_no_sentinel "$TMP_DIR/profile1.json" "Pyroscope window 1"
  assert_no_sentinel "$TMP_DIR/profile2.json" "Pyroscope window 2"
  echo "[otel-smoke] Section P ok (two consecutive non-empty windows)"
}

# ---------------------------------------------------------------------------
# persistence: observability container restart keeps volume data queryable
# ---------------------------------------------------------------------------

run_persistence_checks() {
  echo "[otel-smoke] Section X: observability container restart persistence"
  local before prom_id pyro_id
  # global sum across instances: history from earlier runs must survive
  before="$(prom_sum "sum(nagent_turns_total)")"
  prom_id="$(obs_compose ps -q prometheus)"
  pyro_id="$(obs_compose ps -q pyroscope)"
  [ -n "$prom_id" ] || fail "prometheus container not running (shared observability deployment required)"
  [ -n "$pyro_id" ] || fail "pyroscope container not running"
  local before_ms
  before_ms="$(now_ms)"
  docker restart "$prom_id" "$pyro_id" >/dev/null || fail "observability container restart failed"
  local i ready=1
  for i in $(seq 1 60); do
    if obs_compose exec -T prometheus wget -q -O - "http://localhost:9090/-/ready" >/dev/null 2>&1; then
      ready=0
      break
    fi
    sleep 1
  done
  [ "$ready" = "0" ] || fail "prometheus not ready after restart"
  local after
  after="$(prom_sum "sum(nagent_turns_total)")"
  python3 -c "
import sys
sys.exit(0 if float('''$after''') >= float('''$before''') > 0 else 1)
" || fail "Prometheus history lost after restart (before=$before after=$after)"
  # Pyroscope needs a short warm-up after restart (WAL replay); bounded retry
  local render="" i
  for i in $(seq 1 30); do
    render="$(pyroscope_render "$((before_ms - 300000))" "$(now_ms)")"
    [ -n "$render" ] && break
    sleep 2
  done
  [ -n "$render" ] || fail "Pyroscope profile data not queryable after restart"
  echo "[otel-smoke] Section X ok (Prometheus history + Pyroscope profile survived restart)"
}

# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------

cleanup() {
  set +e
  restore_provider
  stop_fake_llm
  for session in "$SESSION_GOOD" "$SESSION_FAIL" "$SESSION_CANCEL" "$SESSION_ZEROCALL" "$SESSION_OVERLAY"; do
    delete_session "$session"
  done
  if [ "$ENV_DIRTY" = "1" ]; then
    restore_env
    recreate_nagent
  fi
  rm -rf "$TMP_DIR" 2>/dev/null || true
  set -e
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

command -v jq >/dev/null 2>&1 || fail "otel smoke requires host jq"
command -v python3 >/dev/null 2>&1 || fail "otel smoke requires host python3"

wait_health "$HEALTH_ATTEMPTS"
OTEL_ENABLED="$(app_env N_AGENT_OTEL_ENABLED)"
PROFILING_ENABLED="$(app_env N_AGENT_PROFILING_ENABLED)"

echo "[otel-smoke] N_AGENT_OTEL_ENABLED=$OTEL_ENABLED N_AGENT_PROFILING_ENABLED=$PROFILING_ENABLED"

if [ "$OTEL_ENABLED" != "true" ] && [ "$PROFILING_ENABLED" != "true" ]; then
  run_disabled_checks
  exit 0
fi

if [ "$OTEL_ENABLED" != "true" ]; then
  run_disabled_checks
else
  run_otel_checks
  if [ "${N_AGENT_OTEL_SMOKE_SUMMARY_ONLY:-false}" = "true" ]; then
    echo "[otel-smoke] execution summary PASS"
    exit 0
  fi
  run_persistence_checks
fi

if [ "$PROFILING_ENABLED" = "true" ]; then
  run_profile_checks
fi

echo "[otel-smoke] PASS"

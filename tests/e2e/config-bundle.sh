#!/usr/bin/env bash

# E2E for the configuration migration bundle (plan T9).
#
# Runs against the already-running Docker container (run.sh restarts it once
# before the suites). Every database this suite touches is a throwaway file
# under /tmp inside the container, selected per `docker exec` with
# N_AGENT_SQLITE_PATH (app/config.py: env_prefix N_AGENT_): the live
# deployment database at /app/locals/sessions.db is never written by this
# suite. The host side only ever runs docker/config-import.sh and
# docker/install.sh in --dry-run, so docker/.env and <install-root> stay
# untouched as well.
#
# Coverage: canary source database -> export (section whitelist, runtime-field
# exclusion, four-state secrets) -> dry run into an empty target -> real merge
# import -> idempotent rerun -> degraded rerun -> overwrite secret semantics ->
# the host bundle chain through both entry points.
#
# Not covered (needs a second Compose project and a second host port, which
# would resolve to the live deployment): a genuine no-network cold start of a
# second service instance. The importer's placeholder-network branch is only
# observed through the running deployment.
#
# No plaintext secret ever leaves the container: fixture keys are generated
# in-container with secrets.token_hex and every assertion about them compares
# sha256 digests and prints booleans only.

(
  set -eu
  set -o pipefail

  REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  RUN_TAG="e2e-cb-$(date +%s)-$$"
  CDIR="/tmp/config-bundle-$RUN_TAG"
  HOST_TMP=""
  CONTAINER=""

  cleanup() {
    if [ -n "$CONTAINER" ]; then
      docker exec "$CONTAINER" rm -rf "$CDIR" >/dev/null 2>&1 || true
    fi
    if [ -n "$HOST_TMP" ]; then
      rm -rf "$HOST_TMP" 2>/dev/null || true
    fi
  }

  trap cleanup EXIT
  trap 'exit 1' HUP INT TERM

  fail() {
    echo "FAIL: $*" >&2
    exit 1
  }

  # Assert that a captured text contains a needle, and never accept an empty
  # haystack (an empty capture would make grep -F trivially fail, but an empty
  # needle would make it trivially pass).
  assert_contains() {
    local haystack="$1"
    local needle="$2"
    local label="$3"
    [ -n "$needle" ] || fail "$label: the expected marker is empty (vacuous assertion)"
    [ -n "$haystack" ] || fail "$label: nothing was captured"
    printf '%s\n' "$haystack" | grep -qF -- "$needle" ||
      fail "$label: output does not contain '$needle'
--- output ---
$haystack"
  }

  assert_not_contains() {
    local haystack="$1"
    local needle="$2"
    local label="$3"
    [ -n "$needle" ] || fail "$label: the forbidden marker is empty (vacuous assertion)"
    [ -n "$haystack" ] || fail "$label: nothing was captured"
    printf '%s\n' "$haystack" | grep -qF -- "$needle" &&
      fail "$label: output must not contain '$needle'
--- output ---
$haystack"
    return 0
  }

  # Run python inside the container with $CDIR exported. Requires -i: the
  # heredoc arrives on the container's stdin.
  cpy() {
    docker exec -i -e CDIR="$CDIR" -e RUN_TAG="$RUN_TAG" "$CONTAINER" python3 -
  }

  # Run the in-container CLI against one of this suite's throwaway databases.
  ncli() {
    local db="$1"
    shift
    docker exec -e N_AGENT_SQLITE_PATH="$CDIR/$db" "$CONTAINER" n-agent "$@"
  }

  # ---------------------------------------------------------------------------
  # Section 0: fixed compose context -> container id
  # ---------------------------------------------------------------------------

  echo "[ConfigBundle E2E] 0. resolve the container through the fixed compose context"

  command -v python3 >/dev/null 2>&1 ||
    fail "this suite needs host python3 (docker/config-import.sh needs it too)"

  COMPOSE_ARGS=(
    --project-directory "$REPO_ROOT/docker"
    --env-file "$REPO_ROOT/docker/.env"
    -f "$REPO_ROOT/docker/docker-compose.yml"
  )
  CONTAINER="$(docker compose "${COMPOSE_ARGS[@]}" ps -q n-agent)"
  [ -n "$CONTAINER" ] || fail "the n-agent service is not running (run.sh restarts it first)"
  docker exec "$CONTAINER" true >/dev/null 2>&1 ||
    fail "cannot exec into the resolved container $CONTAINER"

  HOST_TMP="$(mktemp -d "/tmp/e2e-config-bundle-XXXXXX")"
  docker exec "$CONTAINER" mkdir -p "$CDIR/target" "$CDIR/probe"

  # The two host files that must be provably untouched by this suite.
  DOCKER_ENV="$REPO_ROOT/docker/.env"
  DOCKER_ENV_SHA_BEFORE="$(shasum -a 256 "$DOCKER_ENV" | awk '{print $1}')"
  [ -n "$DOCKER_ENV_SHA_BEFORE" ] || fail "cannot checksum $DOCKER_ENV"

  # ---------------------------------------------------------------------------
  # Section 1: a canary source database, built without the app bootstrap
  # ---------------------------------------------------------------------------

  echo "[ConfigBundle E2E] 1. build the canary source database"

  cpy <<'PYEOF'
import json
import os
import secrets

cdir = os.environ["CDIR"]
tag = os.environ["RUN_TAG"]

def key() -> str:
    # Generated here and never printed: the suite only ever compares digests.
    return secrets.token_hex(16)

canary = {
    "schema_version": 1,
    "created_at": "2026-09-10T00:00:00+00:00",
    "source": {
        "hostname": f"source-{tag}",
        "install_root": "/canary/install",
        "code_root": "/canary/code",
    },
    "redacted": False,
    "sections": {
        "providers": [
            {"name": f"{tag}-p1", "provider_type": "openai",
             "base_url": "http://p1.invalid/v1", "model": "m1", "api_key": key(),
             "extra_headers": None, "supports_vision": False, "is_active": True},
            {"name": f"{tag}-p2", "provider_type": "openai",
             "base_url": "http://p2.invalid/v1", "model": "m2", "api_key": key(),
             "extra_headers": None, "supports_vision": False, "is_active": False},
            {"name": f"{tag}-p3", "provider_type": "openai",
             "base_url": "http://p3.invalid/v1", "model": "m3", "api_key": key(),
             "extra_headers": None, "supports_vision": True, "is_active": False},
        ],
        "knowledge_bases": [
            {"id": f"kb-{tag}", "name": f"{tag}-kb", "description": "canary",
             "base_type": "n_kb", "base_url": "http://kb.invalid", "dataset_id": "ds1",
             "api_key": key(), "enabled": True, "default_top_k": 5,
             "default_min_score": 0.5},
        ],
        "mcp_sites": [
            {"name": f"{tag}-mcp", "transport_type": "streamable_http",
             "url": "http://mcp.invalid/mcp", "command": "", "args": [],
             "env": {"CANARY_TOKEN": key()}, "enabled": False},
        ],
        "external_memory_providers": [
            {"name": f"{tag}-mem", "provider_type": "mem0",
             "base_url": "http://mem.invalid", "api_key": key(), "enabled": False,
             "extra_config": {"note": "canary"}},
        ],
        # "builtin" always resolves, so this section is a clean created ->
        # skipped pair rather than a dry-run-dependent degradation.
        "external_memory_global_config": {"enabled_providers": ["builtin"]},
        "plugins": [],
        "skills": [],
        "scheduled_tasks": [
            {"name": f"{tag}-task", "prompt": "canary prompt",
             "cron_expression": "0 3 * * *", "timezone": "Asia/Shanghai",
             "enabled": True, "delivery_target": "dashboard", "origin": {},
             "delivery_context": {},
             "execution_policy": {"mode": "unattended",
                                  "tool_exposure_policy": "safe_only",
                                  "allow_confirm_tools": False, "allowed_tools": []},
             "status": "active"},
        ],
        "gateway_home_targets": [
            {"platform": "feishu", "receive_id": f"oc_{tag}",
             "receive_id_type": "chat_id", "thread_id": "",
             "display_name": f"{tag} home"},
        ],
        "task_config": {"overrides": {"task_failure_limit": 7}},
    },
}

empty = {
    "schema_version": 1,
    "created_at": "2026-09-10T00:00:00+00:00",
    "source": {"hostname": "empty", "install_root": "/e", "code_root": "/c"},
    "redacted": False,
    "sections": {
        "providers": [], "knowledge_bases": [], "mcp_sites": [],
        "external_memory_providers": [], "external_memory_global_config": None,
        "plugins": [], "skills": [], "scheduled_tasks": [],
        "gateway_home_targets": [], "task_config": None,
    },
}

with open(f"{cdir}/canary.json", "w", encoding="utf-8") as handle:
    json.dump(canary, handle, ensure_ascii=False)
with open(f"{cdir}/empty.json", "w", encoding="utf-8") as handle:
    json.dump(empty, handle, ensure_ascii=False)
print("OK fixtures written")
PYEOF

  # The source database does not exist yet: a real (non dry-run) import is the
  # only thing that creates its schema, and it must report every canary record
  # as created. This is a handcrafted fixture, NOT a re-import of an export of
  # the same database.
  seed_report="$(ncli source.db config import --file "$CDIR/canary.json" --json --mode merge --with-schedules)"
  printf '%s\n' "$seed_report" > "$HOST_TMP/seed-report.json"
  python3 - "$HOST_TMP/seed-report.json" <<'PYEOF'
import json
import sys

report = json.load(open(sys.argv[1], encoding="utf-8"))
counts = report["counts"]
items = report["items"]
if len(items) != 10:
    raise SystemExit(f"FAIL: expected 10 seed items, got {len(items)}")
if counts["created"] != 9 or counts["updated"] != 1:
    raise SystemExit(f"FAIL: unexpected seed counts: {counts}")
if counts["failed"] or counts["degraded"] or counts["skipped"]:
    raise SystemExit(f"FAIL: the canary seed must be clean: {counts}")
if report["exit_code"] != 0:
    raise SystemExit(f"FAIL: seed exit_code={report['exit_code']}")
print("OK seed created 9 + updated 1 across 10 sections")
PYEOF

  # ---------------------------------------------------------------------------
  # Section 2: export -- section whitelist, runtime fields, four-state secrets
  # ---------------------------------------------------------------------------

  echo "[ConfigBundle E2E] 2. export the source: whitelist, runtime fields, secrets"

  ncli source.db config export --out "$CDIR/full.json" 2>/dev/null
  ncli source.db config export --out "$CDIR/redacted.json" --redact-secrets 2>/dev/null

  cpy <<'PYEOF'
import json
import os
import sqlite3

cdir = os.environ["CDIR"]
tag = os.environ["RUN_TAG"]
full = json.load(open(f"{cdir}/full.json", encoding="utf-8"))
red = json.load(open(f"{cdir}/redacted.json", encoding="utf-8"))
problems = []

# -- one document, fixed envelope -----------------------------------------
if sorted(full) != ["created_at", "redacted", "schema_version", "sections", "source"]:
    problems.append(f"envelope keys: {sorted(full)}")
if full["schema_version"] != 1:
    problems.append(f"schema_version: {full['schema_version']}")
if full["redacted"] is not False or red["redacted"] is not True:
    problems.append("the redacted flag does not follow --redact-secrets")

# -- the 10-section whitelist ---------------------------------------------
EXPECTED_SECTIONS = [
    "external_memory_global_config", "external_memory_providers",
    "gateway_home_targets", "knowledge_bases", "mcp_sites", "plugins",
    "providers", "scheduled_tasks", "skills", "task_config",
]
if sorted(full["sections"]) != EXPECTED_SECTIONS:
    problems.append(f"sections: {sorted(full['sections'])}")

# -- per-section field whitelist. Anything a table carries but this set does
#    not is a runtime field and must not travel.
EXPECTED_FIELDS = {
    "providers": ["api_key", "base_url", "extra_headers", "is_active", "model",
                  "name", "provider_type", "supports_vision"],
    "knowledge_bases": ["api_key", "base_type", "base_url", "dataset_id",
                        "default_min_score", "default_top_k", "description",
                        "enabled", "id", "name"],
    "mcp_sites": ["args", "command", "enabled", "env", "name", "transport_type", "url"],
    "external_memory_providers": ["api_key", "base_url", "enabled", "extra_config",
                                  "name", "provider_type"],
    "scheduled_tasks": ["cron_expression", "delivery_context", "delivery_target",
                        "enabled", "execution_policy", "name", "origin", "prompt",
                        "status", "timezone"],
    "gateway_home_targets": ["display_name", "platform", "receive_id",
                             "receive_id_type", "thread_id"],
}
for section, expected in EXPECTED_FIELDS.items():
    rows = full["sections"][section]
    if not rows:
        problems.append(f"{section}: the canary export must not be empty")
        continue
    for row in rows:
        if sorted(row) != expected:
            problems.append(f"{section} row keys: {sorted(row)}")

# -- runtime-only columns really exist in the tables, and really are absent
#    from the export. Reading the columns from the database is what keeps this
#    assertion honest: a renamed column cannot silently empty the check.
con = sqlite3.connect(f"file:{cdir}/source.db?mode=ro", uri=True)
TABLE_OF = {
    "providers": "providers",
    "knowledge_bases": "knowledge_bases",
    "mcp_sites": "mcp_sites",
    "scheduled_tasks": "scheduled_tasks",
    "gateway_home_targets": "gateway_home_targets",
}
runtime_seen = 0
for section, table in TABLE_OF.items():
    columns = {row[1] for row in con.execute(f"pragma table_info({table})")}
    if not columns:
        problems.append(f"{table}: no such table in the source database")
        continue
    exported = set(EXPECTED_FIELDS[section])
    runtime = {c for c in columns if c in {
        "id", "created_at", "updated_at", "session_id", "next_run_at",
        "last_run_at", "last_probe_status", "last_probe_error", "last_probed_at",
        "last_error", "probe_status", "probed_at", "tools_json",
    }}
    if section == "knowledge_bases":
        runtime.discard("id")  # id is the natural key of this section
    if not runtime:
        problems.append(f"{table}: expected at least one runtime column")
        continue
    runtime_seen += len(runtime)
    leaked = runtime & exported
    if leaked:
        problems.append(f"{table}: runtime columns leaked into the export: {sorted(leaked)}")
if runtime_seen < 5:
    problems.append(f"only {runtime_seen} runtime columns were checked (vacuous)")

# -- singleton shapes ------------------------------------------------------
if full["sections"]["external_memory_global_config"] != {"enabled_providers": ["builtin"]}:
    problems.append(f"emgc: {full['sections']['external_memory_global_config']}")
if full["sections"]["task_config"] != {"overrides": {"task_failure_limit": 7}}:
    problems.append(f"task_config: {full['sections']['task_config']}")

# -- secrets: three of the four states are observable on the export side.
#    non-empty = carried, "" = the source really has no secret, null = redacted.
#    (absent = unchanged is an import-side state; section 6 exercises it.)
plain_keys = [p["api_key"] for p in full["sections"]["providers"]]
if not all(isinstance(k, str) and len(k) == 32 for k in plain_keys):
    problems.append("a full export must carry every provider secret verbatim")
if full["sections"]["mcp_sites"][0]["env"].get("CANARY_TOKEN", "") == "":
    problems.append("a full export must carry the MCP env map")
for row in red["sections"]["providers"]:
    if row["api_key"] is not None:
        problems.append("a redacted export must null every provider api_key")
if red["sections"]["knowledge_bases"][0]["api_key"] is not None:
    problems.append("a redacted export must null the knowledge base api_key")
if red["sections"]["mcp_sites"][0]["env"] is not None:
    problems.append("a redacted export must null the whole MCP env map")
if red["sections"]["external_memory_providers"][0]["api_key"] is not None:
    problems.append("a redacted export must null the external memory api_key")
# "" (explicitly no secret) is distinct from null: the external memory provider
# in the target created without a secret proves it in section 4; here we prove
# the redacted document never smuggles a plaintext value.
redacted_text = open(f"{cdir}/redacted.json", encoding="utf-8").read()
for value in plain_keys + [full["sections"]["mcp_sites"][0]["env"]["CANARY_TOKEN"]]:
    if value and value in redacted_text:
        problems.append("a plaintext secret survived into the redacted export")
        break

if problems:
    raise SystemExit("FAIL: export contract violated:\n  " + "\n  ".join(problems))
print(f"OK export: 10 sections, {runtime_seen} runtime columns excluded, secrets redacted")
PYEOF

  # ---------------------------------------------------------------------------
  # Section 3: dry run into an empty target -- zero side effects
  # ---------------------------------------------------------------------------

  echo "[ConfigBundle E2E] 3. dry run into an empty target leaves no trace"

  # An empty bundle is what creates the target schema, so the dry run below
  # runs against a real, empty, already-created database.
  empty_report="$(ncli target/target.db config import --file "$CDIR/empty.json" --json)"
  assert_contains "$empty_report" '"created": 0' "empty seed writes nothing"

  before_ls="$(docker exec "$CONTAINER" sh -c "cd '$CDIR/target' && ls -1a | sort")"
  before_sha="$(docker exec "$CONTAINER" sh -c "sha256sum '$CDIR/target/target.db'" | awk '{print $1}')"
  [ -n "$before_sha" ] || fail "cannot checksum the target database"
  assert_contains "$before_ls" "target.db" "the target database exists before the dry run"

  dry_report="$(ncli target/target.db config import --file "$CDIR/full.json" --json --dry-run)"
  printf '%s\n' "$dry_report" > "$HOST_TMP/dry-report.json"
  python3 - "$HOST_TMP/dry-report.json" <<'PYEOF'
import json
import sys

report = json.load(open(sys.argv[1], encoding="utf-8"))
counts = report["counts"]
if counts["created"] != 9 or counts["updated"] != 1:
    raise SystemExit(f"FAIL: the dry run must preview the full apply, got {counts}")
if counts["failed"]:
    raise SystemExit(f"FAIL: the dry run reported failures: {counts}")
print(f"OK dry run previewed {counts['created']} created + {counts['updated']} updated")
PYEOF

  after_ls="$(docker exec "$CONTAINER" sh -c "cd '$CDIR/target' && ls -1a | sort")"
  after_sha="$(docker exec "$CONTAINER" sh -c "sha256sum '$CDIR/target/target.db'" | awk '{print $1}')"
  [ "$before_sha" = "$after_sha" ] ||
    fail "the dry run modified the target database ($before_sha -> $after_sha)"
  [ "$before_ls" = "$after_ls" ] ||
    fail "the dry run left files behind:
--- before ---
$before_ls
--- after ---
$after_ls"

  # A dry run against a database that does not exist must refuse, and must not
  # create the file, the directory, or any schema on the way to refusing.
  set +e
  missing_out="$(ncli probe/missing.db config import --file "$CDIR/full.json" --json --dry-run 2>&1)"
  missing_rc=$?
  set -e
  [ "$missing_rc" -ne 0 ] ||
    fail "a dry run against a missing database must fail, got rc=0: $missing_out"
  probe_ls="$(docker exec "$CONTAINER" sh -c "cd '$CDIR/probe' && ls -1a | sort")"
  assert_not_contains "$probe_ls" "missing.db" "a dry run must not create the database"
  echo "[ConfigBundle E2E]    dry run: db unchanged ($before_sha), no schema on a missing path (rc=$missing_rc)"

  # ---------------------------------------------------------------------------
  # Section 4: the real import
  # ---------------------------------------------------------------------------

  echo "[ConfigBundle E2E] 4. real merge import into the target"

  # No --with-schedules: a migrated schedule must land disabled and paused.
  real_report="$(ncli target/target.db config import --file "$CDIR/full.json" --json --mode merge)"
  printf '%s\n' "$real_report" > "$HOST_TMP/real-report.json"
  python3 - "$HOST_TMP/real-report.json" <<'PYEOF'
import json
import sys

report = json.load(open(sys.argv[1], encoding="utf-8"))
counts = report["counts"]
created = [i["natural_key"] for i in report["items"] if i["outcome"] == "created"]
if counts["created"] != 9 or counts["updated"] != 1 or counts["failed"]:
    raise SystemExit(f"FAIL: unexpected import counts: {counts}")
if len(created) != 9:
    raise SystemExit(f"FAIL: expected 9 created records, got {created}")
for item in report["items"]:
    if item["outcome"] in ("created", "updated") and item["action"] == "none":
        raise SystemExit(f"FAIL: {item['section']} claims {item['outcome']} without writing")
print(f"OK real import: {counts}")
PYEOF

  cpy <<'PYEOF'
import os
import sqlite3

cdir = os.environ["CDIR"]
tag = os.environ["RUN_TAG"]
con = sqlite3.connect(f"file:{cdir}/target/target.db?mode=ro", uri=True)
problems = []

providers = con.execute("select name from providers order by name").fetchall()
if len(providers) != 3:
    problems.append(f"providers: {providers}")

tasks = con.execute(
    "select name, enabled, status, session_id from scheduled_tasks").fetchall()
if len(tasks) != 1:
    problems.append(f"scheduled_tasks: {tasks}")
else:
    name, enabled, status, session_id = tasks[0]
    if name != f"{tag}-task":
        problems.append(f"scheduled task name: {name}")
    # The source task was enabled/active; without --with-schedules it must
    # arrive disabled and paused so nothing fires on the new machine.
    if enabled != 0 or status != "paused":
        problems.append(f"a migrated schedule must be disabled+paused, got {enabled}/{status}")
    if not session_id:
        problems.append("the migrated schedule has no session")
    else:
        sessions = con.execute(
            "select id, source from sessions").fetchall()
        if sessions != [(session_id, "schedule")]:
            problems.append(f"sessions: {sessions}")
        history = con.execute(
            "select count(*) from messages where session_id = ?", (session_id,)
        ).fetchone()[0]
        if history != 0:
            problems.append(f"the migrated session carries {history} messages")

# Existence of plugins and skills is owned by the target machine's scan; the
# import must never invent rows, and must never run a scan of its own.
for table in ("plugins", "skills"):
    count = con.execute(f"select count(*) from {table}").fetchone()[0]
    if count != 0:
        problems.append(f"{table}: import created {count} rows without a scan")

# The single write the import is allowed to make outside the config tables.
enabled_set = con.execute(
    "select * from external_memory_global_config").fetchall()
if not enabled_set:
    problems.append("external_memory_global_config was not written")

if problems:
    raise SystemExit("FAIL: target state after import:\n  " + "\n  ".join(problems))
print("OK target: 3 providers, 1 disabled+paused schedule, 1 empty session, "
      "0 plugin rows, 0 skill rows")
PYEOF

  # ---------------------------------------------------------------------------
  # Section 5: idempotent rerun, then a degraded rerun that still writes nothing
  # ---------------------------------------------------------------------------

  echo "[ConfigBundle E2E] 5. rerun merge: complete fixture skipped, degraded fixture writes nothing"

  stable_sha="$(docker exec "$CONTAINER" sh -c "sha256sum '$CDIR/target/target.db'" | awk '{print $1}')"
  [ -n "$stable_sha" ] || fail "cannot checksum the target database"

  repeat_report="$(ncli target/target.db config import --file "$CDIR/full.json" --json --mode merge)"
  printf '%s\n' "$repeat_report" > "$HOST_TMP/repeat-report.json"
  python3 - "$HOST_TMP/repeat-report.json" <<'PYEOF'
import json
import sys

report = json.load(open(sys.argv[1], encoding="utf-8"))
counts = report["counts"]
items = report["items"]
if len(items) != 10:
    raise SystemExit(f"FAIL: expected 10 items on the rerun, got {len(items)}")
if counts["skipped"] != 10:
    raise SystemExit(f"FAIL: a complete non-degrading fixture must be entirely skipped: {counts}")
for name in ("created", "updated", "degraded", "failed"):
    if counts[name]:
        raise SystemExit(f"FAIL: rerun reported {name}={counts[name]}")
for item in items:
    if item["action"] != "none":
        raise SystemExit(f"FAIL: {item['section']} wrote on an idempotent rerun")
print("OK idempotent rerun: 10/10 skipped, action=none")
PYEOF

  # A separate fixture: redacted secrets plus a plugin and a skill this machine
  # does not have. Degraded is allowed here, writing is not.
  cpy <<'PYEOF'
import json
import os

cdir = os.environ["CDIR"]
tag = os.environ["RUN_TAG"]
bundle = json.load(open(f"{cdir}/redacted.json", encoding="utf-8"))
bundle["sections"]["plugins"] = [{
    "key": f"{tag}-ghost", "name": "ghost", "version": "9.9.9", "kind": "python",
    "enabled": True, "config_json": {}, "plugin_secrets": None,
}]
bundle["sections"]["skills"] = [{
    "name": f"{tag}-ghost-skill", "enabled": True, "chat_selectable": True,
}]
with open(f"{cdir}/degraded.json", "w", encoding="utf-8") as handle:
    json.dump(bundle, handle, ensure_ascii=False)
print("OK degraded fixture written")
PYEOF

  # --stdin needs docker exec -i, otherwise the container's stdin is empty.
  degraded_report="$(
    docker exec "$CONTAINER" cat "$CDIR/degraded.json" |
      docker exec -i -e N_AGENT_SQLITE_PATH="$CDIR/target/target.db" "$CONTAINER" \
        n-agent config import --stdin --json --mode merge
  )"
  printf '%s\n' "$degraded_report" > "$HOST_TMP/degraded-report.json"
  python3 - "$HOST_TMP/degraded-report.json" <<'PYEOF'
import json
import sys

report = json.load(open(sys.argv[1], encoding="utf-8"))
counts = report["counts"]
items = report["items"]
if counts["degraded"] < 2:
    raise SystemExit(f"FAIL: the missing-dependency fixture must degrade: {counts}")
if counts["created"] or counts["updated"] or counts["failed"]:
    raise SystemExit(f"FAIL: a degraded rerun must not write: {counts}")
wrote = [f"{i['section']}/{i['natural_key']}" for i in items if i["action"] != "none"]
if wrote:
    raise SystemExit(f"FAIL: these items wrote on a degraded rerun: {wrote}")
sections = {i["section"] for i in items if i["outcome"] == "degraded"}
if not {"plugins", "skills"} <= sections:
    raise SystemExit(f"FAIL: expected the ghost plugin and skill to degrade, got {sections}")
print(f"OK degraded rerun: degraded={counts['degraded']}, every action=none")
PYEOF

  after_reruns_sha="$(docker exec "$CONTAINER" sh -c "sha256sum '$CDIR/target/target.db'" | awk '{print $1}')"
  [ "$stable_sha" = "$after_reruns_sha" ] ||
    fail "the two reruns changed the target database ($stable_sha -> $after_reruns_sha)"

  # ---------------------------------------------------------------------------
  # Section 6: overwrite mode and the four secret states
  # ---------------------------------------------------------------------------

  echo "[ConfigBundle E2E] 6. overwrite: fields updated, secret set/cleared/kept/untouched"

  cpy <<'PYEOF'
import hashlib
import json
import os
import secrets
import sqlite3

cdir = os.environ["CDIR"]
tag = os.environ["RUN_TAG"]

con = sqlite3.connect(f"file:{cdir}/target/target.db?mode=ro", uri=True)
before = {name: (url, model, key) for name, url, model, key in con.execute(
    "select name, base_url, model, api_key from providers")}
kb_before = con.execute("select api_key from knowledge_bases").fetchone()[0]
if len(before) != 3 or not all(v[2] for v in before.values()):
    raise SystemExit(f"FAIL: the three canary providers must all carry a secret before overwrite")
if not kb_before:
    raise SystemExit("FAIL: the knowledge base must carry a secret before overwrite")

bundle = json.load(open(f"{cdir}/full.json", encoding="utf-8"))
replacement = secrets.token_hex(16)
for row in bundle["sections"]["providers"]:
    if row["name"].endswith("-p1"):
        row["base_url"] = "http://p1-new.invalid/v1"
        row["model"] = "m1-new"
        row["api_key"] = replacement          # non-empty -> overwrite
    elif row["name"].endswith("-p2"):
        row["api_key"] = ""                   # "" -> clear
    elif row["name"].endswith("-p3"):
        row["api_key"] = None                 # null -> redacted, keep target
for row in bundle["sections"]["knowledge_bases"]:
    row.pop("api_key", None)                  # absent -> unchanged

state = {
    "replacement_sha": hashlib.sha256(replacement.encode()).hexdigest(),
    "p3_sha": hashlib.sha256(before[f"{tag}-p3"][2].encode()).hexdigest(),
    "kb_sha": hashlib.sha256(kb_before.encode()).hexdigest(),
    "p1_url_before": before[f"{tag}-p1"][0],
    "p1_model_before": before[f"{tag}-p1"][1],
}
with open(f"{cdir}/overwrite.json", "w", encoding="utf-8") as handle:
    json.dump(bundle, handle, ensure_ascii=False)
with open(f"{cdir}/overwrite-state.json", "w", encoding="utf-8") as handle:
    json.dump(state, handle)
print("OK overwrite fixture written (four secret states)")
PYEOF

  overwrite_report="$(ncli target/target.db config import --file "$CDIR/overwrite.json" --json --mode overwrite)"
  printf '%s\n' "$overwrite_report" > "$HOST_TMP/overwrite-report.json"
  python3 - "$HOST_TMP/overwrite-report.json" <<'PYEOF'
import json
import sys

report = json.load(open(sys.argv[1], encoding="utf-8"))
counts = report["counts"]
if counts["updated"] != 2 or counts["failed"]:
    raise SystemExit(f"FAIL: overwrite should update exactly p1 and p2: {counts}")
redacted = [i for i in report["items"]
            if any("redacted" in r for r in i["reasons"]) and i["action"] == "none"]
if not redacted:
    raise SystemExit("FAIL: the null secret must be reported as kept without a write")
print(f"OK overwrite report: {counts}, {len(redacted)} redacted item kept")
PYEOF

  cpy <<'PYEOF'
import hashlib
import json
import os
import sqlite3

cdir = os.environ["CDIR"]
tag = os.environ["RUN_TAG"]
state = json.load(open(f"{cdir}/overwrite-state.json", encoding="utf-8"))
con = sqlite3.connect(f"file:{cdir}/target/target.db?mode=ro", uri=True)
rows = {name: (url, model, key) for name, url, model, key in con.execute(
    "select name, base_url, model, api_key from providers")}
kb_after = con.execute("select api_key from knowledge_bases").fetchone()[0]

def sha(value):
    return hashlib.sha256(value.encode()).hexdigest() if value else None

problems = []
p1_url, p1_model, p1_key = rows[f"{tag}-p1"]
if p1_url == state["p1_url_before"] or p1_url != "http://p1-new.invalid/v1":
    problems.append(f"p1 base_url was not updated: {p1_url}")
if p1_model == state["p1_model_before"] or p1_model != "m1-new":
    problems.append(f"p1 model was not updated: {p1_model}")
if sha(p1_key) != state["replacement_sha"]:
    problems.append("p1 secret was not overwritten with the new value")

if rows[f"{tag}-p2"][2] not in (None, ""):
    problems.append("p2 secret was not cleared by the empty string")

if sha(rows[f"{tag}-p3"][2]) != state["p3_sha"]:
    problems.append("p3 secret was not preserved through the null (redacted) state")

if sha(kb_after) != state["kb_sha"]:
    problems.append("the knowledge base secret changed although the field was absent")

if problems:
    raise SystemExit("FAIL: secret four-state semantics:\n  " + "\n  ".join(problems))
print("OK secrets: non-empty overwrote, \"\" cleared, null kept, absent unchanged")
PYEOF

  # ---------------------------------------------------------------------------
  # Section 7: the host bundle chain (plan T7 S13) through both entry points
  # ---------------------------------------------------------------------------

  echo "[ConfigBundle E2E] 7. host bundle chain: config-import.sh and install.sh agree"

  STAGE="$HOST_TMP/stage"
  SCRATCH="$HOST_TMP/scratch"
  mkdir -p "$STAGE/db" "$STAGE/env" "$SCRATCH/install" "$SCRATCH/code"

  # The redacted export is what travels: no plaintext secret ever reaches the
  # host filesystem.
  docker exec "$CONTAINER" cat "$CDIR/redacted.json" > "$STAGE/db/config.json"
  [ -s "$STAGE/db/config.json" ] || fail "could not copy the redacted export out of the container"
  cat > "$STAGE/env/docker.env" <<EOF
N_AGENT_INSTALL_ROOT=/canary/install
N_AGENT_CODE_ROOT=/canary/code
N_AGENT_PROVIDER_API_KEY=
N_AGENT_AGENT_ITERATION_LIMIT=7
EOF
  cat > "$STAGE/env/install-root.env" <<EOF
CBE2E_CANARY=/canary/code/sub
EOF
  # A bundled install.sh must be reported as ignored, never executed.
  cp "$REPO_ROOT/docker/install.sh" "$STAGE/install.sh"

  python3 "$REPO_ROOT/docker/config_bundle_files.py" manifest "$STAGE" \
    --hostname "source-$RUN_TAG" --install-root /canary/install \
    --code-root /canary/code --redacted true
  BUNDLE="$HOST_TMP/n-agent-config-$RUN_TAG.tar.gz"
  ( cd "$STAGE" && COPYFILE_DISABLE=1 tar czf "$BUNDLE" . )
  [ -s "$BUNDLE" ] || fail "the fixture bundle was not created"

  # 7a. Preflight: the archive is validated before anything is resolved.
  preflight_out="$(sh "$REPO_ROOT/docker/config-import.sh" "$BUNDLE" --preflight-only)"
  assert_contains "$preflight_out" "preflight: ok" "preflight"
  assert_contains "$preflight_out" "origin:   hostname=source-$RUN_TAG" "preflight origin"
  assert_contains "$preflight_out" "redacted: yes" "preflight redaction"
  assert_contains "$preflight_out" "ignored:  install.sh" "preflight refuses to run a bundled script"

  # 7b/7c. The same dry run through both entry points must produce the same
  # host plan. --host-only keeps this pair off the shared deployment entirely.
  import_out="$(sh "$REPO_ROOT/docker/config-import.sh" "$BUNDLE" \
    --install-root "$SCRATCH/install" --code-root "$SCRATCH/code" --dry-run --host-only)"
  install_out="$(sh "$REPO_ROOT/docker/install.sh" "$BUNDLE" --repo "$REPO_ROOT" \
    --install-root "$SCRATCH/install" --code-root "$SCRATCH/code" --dry-run --host-only)"
  assert_contains "$import_out" "host files (merge, dry run):" "config-import.sh host plan"
  assert_contains "$import_out" "install-root: $SCRATCH/install" "the scratch install root is used"
  assert_contains "$import_out" "created <install-root>/.env" "the install-root dotenv is planned"
  assert_contains "$import_out" "created locals/host-terminal.token" "a token is planned"
  assert_contains "$import_out" "install.sh in the bundle was ignored" "the bundled script is ignored"
  assert_contains "$import_out" "was stripped by --no-secrets" "the redacted env key degrades"
  [ "$import_out" = "$install_out" ] ||
    fail "the two entry points disagree:
--- config-import.sh ---
$import_out
--- install.sh ---
$install_out"

  # A dry run writes nothing: the scratch roots must still be the two empty
  # directories this suite created.
  scratch_tree="$(cd "$SCRATCH" && find . | sort | tr '\n' ' ')"
  [ "$scratch_tree" = ". ./code ./install " ] ||
    fail "the dry run wrote into the scratch roots: $scratch_tree"

  # 7d. The full dry run, which is the only path that reaches docker. It is
  # read-only by construction (config import --dry-run opens SQLite mode=ro)
  # and must leave no canary record behind in the live deployment.
  full_out="$(sh "$REPO_ROOT/docker/config-import.sh" "$BUNDLE" \
    --install-root "$SCRATCH/install" --code-root "$SCRATCH/code" --dry-run)"
  assert_contains "$full_out" "host files (merge, dry run):" "full dry run host segment"
  assert_contains "$full_out" "database (merge):" "full dry run database segment"
  assert_contains "$full_out" "  counts: created=" "full dry run counts"
  assert_contains "$full_out" "scheduled_tasks/$RUN_TAG-task" "the canary record reached the database preview"
  assert_contains "$full_out" "service:  not restarted (nothing was written)" "a dry run never restarts"

  live_leak="$(docker exec -e RUN_TAG="$RUN_TAG" "$CONTAINER" python3 -c '
import os, sqlite3
tag = os.environ["RUN_TAG"]
con = sqlite3.connect("file:/app/locals/sessions.db?mode=ro", uri=True)
found = []
for table, column in (("providers", "name"), ("knowledge_bases", "id"),
                      ("mcp_sites", "name"), ("scheduled_tasks", "name")):
    found += [r[0] for r in con.execute(
        f"select {column} from {table} where {column} like ?", (f"%{tag}%",))]
print("LEAK " + ",".join(found) if found else "CLEAN")
')"
  [ "$live_leak" = "CLEAN" ] ||
    fail "the dry run wrote canary records into the live deployment: $live_leak"

  DOCKER_ENV_SHA_AFTER="$(shasum -a 256 "$DOCKER_ENV" | awk '{print $1}')"
  [ "$DOCKER_ENV_SHA_BEFORE" = "$DOCKER_ENV_SHA_AFTER" ] ||
    fail "docker/.env changed during the suite ($DOCKER_ENV_SHA_BEFORE -> $DOCKER_ENV_SHA_AFTER)"
  echo "[ConfigBundle E2E]    docker/.env unchanged, live deployment carries no $RUN_TAG record"

  echo "[ConfigBundle E2E] PASS"
)

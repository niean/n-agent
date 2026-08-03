#!/usr/bin/env bash

# E2E for the Artifact workbench feature (Task 16).
# Runs against the Docker container (http://127.0.0.1:8201).
# Exercises: setup/backfill, preview kinds, malicious content, update,
# export, publish lifecycle, size limits, invalid paths, gating/config.
#
# Disabled-mode (artifacts_enabled=False) E2E is a follow-up: it would
# verify that /artifacts, /chat/artifacts*, and /p/* all return 404 when
# the subsystem is disabled. The current run.sh pattern starts a single
# container with default config (enabled); a disabled-mode run requires a
# separate N_AGENT_ARTIFACTS_ENABLED=false invocation.

(
  set -eu
  set -o pipefail

  BASE_URL="http://127.0.0.1:8201"
  CONTAINER="n-agent-n-agent-1"
  RUN_TAG="e2e-art-$(date +%s)-$$"

  # Require jq for JSON extraction.
  if ! command -v jq >/dev/null 2>&1; then
    echo "Artifact E2E requires host jq for JSON parsing" >&2
    exit 2
  fi

  # Temp directory for fixture files.
  TMP_DIR="$(mktemp -d /tmp/e2e-artifacts-XXXXXX)"
  CLEANUP_ARTIFACT_IDS=""
  CLEANUP_TASK_IDS=""

  cleanup() {
    # Delete artifacts created during the run.
    for id in $CLEANUP_ARTIFACT_IDS; do
      curl -fsS -X DELETE "$BASE_URL/chat/artifacts/$id" >/dev/null 2>&1 || true
    done
    # Delete tasks created during the run.
    for id in $CLEANUP_TASK_IDS; do
      docker exec "$CONTAINER" n-agent task delete "$id" --json >/dev/null 2>&1 || true
    done
    # Remove temp files.
    rm -rf "$TMP_DIR" 2>/dev/null || true
  }

  trap cleanup EXIT
  trap 'exit 1' HUP INT TERM

  # ---------------------------------------------------------------------------
  # Helpers
  # ---------------------------------------------------------------------------

  # Perform an HTTP request and capture status + body.
  # Sets HTTP_STATUS and HTTP_BODY.
  http() {
    local method="$1"
    local url="$2"
    shift 2
    local tmp_body="$TMP_DIR/resp-body.txt"
    HTTP_STATUS=$(curl -sS -o "$tmp_body" -w "%{http_code}" -X "$method" "$@" "$url" 2>/dev/null) || true
    HTTP_BODY="$(cat "$tmp_body")"
  }

  # Assert HTTP_STATUS equals expected value.
  assert_status() {
    local expected="$1"
    local label="$2"
    if [ "$HTTP_STATUS" != "$expected" ]; then
      echo "FAIL: $label: expected HTTP $expected, got $HTTP_STATUS" >&2
      echo "  response: $HTTP_BODY" >&2
      exit 1
    fi
  }

  # Assert HTTP_BODY contains a string.
  assert_body_contains() {
    local needle="$1"
    local label="$2"
    if ! echo "$HTTP_BODY" | grep -qF -- "$needle"; then
      echo "FAIL: $label: body does not contain '$needle'" >&2
      echo "  body: $HTTP_BODY" >&2
      exit 1
    fi
  }

  # Assert HTTP_BODY does NOT contain a string.
  assert_body_not_contains() {
    local needle="$1"
    local label="$2"
    if echo "$HTTP_BODY" | grep -qF -- "$needle"; then
      echo "FAIL: $label: body should NOT contain '$needle'" >&2
      echo "  body: $HTTP_BODY" >&2
      exit 1
    fi
  }

  # Extract a JSON field via jq; echoes the value (without quotes).
  json_field() {
    local field="$1"
    echo "$HTTP_BODY" | jq -r "$field" 2>/dev/null
  }

  # Create a manual artifact via JSON API; sets HTTP_STATUS/HTTP_BODY.
  create_artifact_json() {
    local name="$1"
    local kind="$2"
    local content="$3"
    local payload
    payload=$(jq -n \
      --arg name "$name" \
      --arg kind "$kind" \
      --arg content "$content" \
      '{name:$name, kind:$kind, content:$content}')
    http POST "$BASE_URL/chat/artifacts" \
      -H "Content-Type: application/json" \
      -d "$payload"
  }

  # Create a manual artifact via multipart (file upload); sets HTTP_STATUS/HTTP_BODY.
  create_artifact_file() {
    local name="$1"
    local kind="$2"
    local file_path="$3"
    local mime="${4:-application/octet-stream}"
    http POST "$BASE_URL/chat/artifacts" \
      -F "name=$name" \
      -F "kind=$kind" \
      -F "file=@$file_path;type=$mime"
  }

  # Track an artifact ID for cleanup.
  track_artifact() {
    local id="$1"
    CLEANUP_ARTIFACT_IDS="$CLEANUP_ARTIFACT_IDS $id"
  }

  # Track a task ID for cleanup.
  track_task() {
    local id="$1"
    CLEANUP_TASK_IDS="$CLEANUP_TASK_IDS $id"
  }

  # Count artifacts in list response whose name contains RUN_TAG.
  count_tagged_artifacts() {
    http GET "$BASE_URL/chat/artifacts?limit=500"
    assert_status 200 "list artifacts"
    echo "$HTTP_BODY" | jq --arg tag "$RUN_TAG" \
      '[.items[] | select(.name | contains($tag))] | length'
  }

  # Wait for the container health endpoint to return 200.
  wait_health() {
    local attempts=30
    for i in $(seq 1 "$attempts"); do
      if curl -fsS --connect-timeout 1 --max-time 2 "$BASE_URL/health" >/dev/null 2>&1; then
        return 0
      fi
      sleep 1
    done
    echo "FAIL: container did not become healthy within $attempts seconds" >&2
    return 1
  }

  # ---------------------------------------------------------------------------
  # Section 1: Setup -- 2 TaskAttachments + 2 TaskArtifacts + 1 manual = 5
  # ---------------------------------------------------------------------------

  echo "[Artifact E2E] Section 1: Setup"

  # 1a. Create a task.
  echo "[Artifact E2E] 1a. create task"
  http POST "$BASE_URL/chat/tasks" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg t "${RUN_TAG}-task" '{title:$t,body:"e2e",created_by:"e2e"}')"
  assert_status 200 "create task"
  TASK_ID=$(json_field '.id')
  if [ -z "$TASK_ID" ]; then
    echo "FAIL: could not extract task id" >&2
    exit 1
  fi
  track_task "$TASK_ID"

  # 1b. Upload 2 TaskAttachments (auto-registers 2 artifacts via callback).
  # Filenames include RUN_TAG so count_tagged_artifacts (name contains tag)
  # counts them alongside the task artifacts and manual artifact.
  echo "[Artifact E2E] 1b. upload 2 attachments"
  echo "attachment content 1" > "$TMP_DIR/${RUN_TAG}-att1.txt"
  echo "attachment content 2" > "$TMP_DIR/${RUN_TAG}-att2.txt"
  for i in 1 2; do
    http POST "$BASE_URL/chat/tasks/$TASK_ID/attachments" \
      -F "file=@$TMP_DIR/${RUN_TAG}-att${i}.txt;type=text/plain" \
      -F "uploaded_by=e2e"
    assert_status 200 "upload attachment $i"
  done

  # 1c. Create 2 TaskArtifacts by writing workspace files and registering
  # directly in the registry (simulates task-run artifact registration).
  echo "[Artifact E2E] 1c. register 2 task artifacts"
  docker exec -i -e RUN_TAG="$RUN_TAG" -e TASK_ID="$TASK_ID" "$CONTAINER" python3 - <<'PYEOF'
import asyncio
import hashlib
import os
import secrets
from pathlib import Path

from app.domain.artifact import (
    Artifact, ArtifactKind, ArtifactSource, ArtifactStatus,
)
from app.infrastructure.registry.sqlite_artifact_registry import SQLiteArtifactRegistry

async def main():
    run_tag = os.environ["RUN_TAG"]
    task_id = os.environ["TASK_ID"]
    registry = SQLiteArtifactRegistry("/app/locals/sessions.db")
    workspace = Path("/workspace")
    for i in (1, 2):
        name = f"{run_tag}-taskart-{i}.txt"
        content = f"task artifact content {i}"
        (workspace / name).write_text(content)
        data = content.encode("utf-8")
        checksum = "sha256:" + hashlib.sha256(data).hexdigest()
        # source_context_ref must be the real task_id: orphan backfill
        # (backfill_orphaned_task_artifacts) deletes task-sourced artifacts
        # whose task no longer exists, checking source_context_ref == task_id.
        source_ref = f"task:{task_id}:run:1:artifact:{i}"
        artifact = Artifact(
            id=secrets.token_urlsafe(16),
            name=name,
            kind=ArtifactKind.TEXT,
            mime="text/plain",
            content_ref=f"workspace:{name}",
            inline_content=None,
            size=len(data),
            checksum=checksum,
            source_kind=ArtifactSource.TASK_ARTIFACT,
            source_ref=source_ref,
            source_context_ref=task_id,
            summary="",
            created_by="e2e",
        )
        await registry.create_artifact(artifact)

asyncio.run(main())
PYEOF

  # 1d. Manually create 1 Artifact.
  echo "[Artifact E2E] 1d. create 1 manual artifact"
  create_artifact_json "${RUN_TAG}-manual-1" "markdown" "# Manual artifact"
  assert_status 201 "create manual artifact"
  MANUAL_ID=$(json_field '.id')
  track_artifact "$MANUAL_ID"

  # 1e. Assert list has at least 5 tagged items.
  echo "[Artifact E2E] 1e. assert 5 tagged items"
  TAGGED_COUNT=$(count_tagged_artifacts)
  if [ "$TAGGED_COUNT" -lt 5 ]; then
    echo "FAIL: expected >= 5 tagged artifacts, got $TAGGED_COUNT" >&2
    exit 1
  fi
  echo "[Artifact E2E] 1e. ok ($TAGGED_COUNT tagged artifacts)"

  # ---------------------------------------------------------------------------
  # Section 2: Backfill idempotency
  # ---------------------------------------------------------------------------

  echo "[Artifact E2E] Section 2: Backfill idempotency"

  # 2a. Record count before restart.
  BEFORE_COUNT=$(count_tagged_artifacts)
  echo "[Artifact E2E] 2a. before restart: $BEFORE_COUNT tagged artifacts"

  # 2b. Restart the n-agent container (triggers backfill on startup).
  echo "[Artifact E2E] 2b. restart container"
  docker restart "$CONTAINER" >/dev/null
  wait_health

  # 2c. Assert count unchanged after restart (backfill is idempotent).
  AFTER_COUNT=$(count_tagged_artifacts)
  echo "[Artifact E2E] 2c. after restart: $AFTER_COUNT tagged artifacts"
  if [ "$AFTER_COUNT" -ne "$BEFORE_COUNT" ]; then
    echo "FAIL: backfill changed artifact count: $BEFORE_COUNT -> $AFTER_COUNT" >&2
    exit 1
  fi
  echo "[Artifact E2E] 2c. ok (idempotent)"

  # ---------------------------------------------------------------------------
  # Section 3: Preview kinds (8 kinds)
  # ---------------------------------------------------------------------------

  echo "[Artifact E2E] Section 3: Preview kinds (8 kinds)"

  # 3a. Markdown
  create_artifact_json "${RUN_TAG}-preview-md" "markdown" "# Markdown preview"
  assert_status 201 "create markdown"
  MD_ID=$(json_field '.id'); track_artifact "$MD_ID"
  http GET "$BASE_URL/chat/artifacts/$MD_ID/content"
  assert_status 200 "get markdown content"
  assert_body_contains "# Markdown preview" "markdown content"

  # 3b. Code
  create_artifact_json "${RUN_TAG}-preview-code" "code" "print('hello')"
  assert_status 201 "create code"
  CODE_ID=$(json_field '.id'); track_artifact "$CODE_ID"
  http GET "$BASE_URL/chat/artifacts/$CODE_ID/content"
  assert_status 200 "get code content"
  assert_body_contains "print('hello')" "code content"

  # 3c. HTML
  create_artifact_json "${RUN_TAG}-preview-html" "html" "<p>HTML preview</p>"
  assert_status 201 "create html"
  HTML_ID=$(json_field '.id'); track_artifact "$HTML_ID"
  http GET "$BASE_URL/chat/artifacts/$HTML_ID/content"
  assert_status 200 "get html content"

  # 3d. Image (binary upload)
  echo 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg==' \
    | base64 -d > "$TMP_DIR/pixel.png"
  create_artifact_file "${RUN_TAG}-preview-image" "image" "$TMP_DIR/pixel.png" "image/png"
  assert_status 201 "create image"
  IMG_ID=$(json_field '.id'); track_artifact "$IMG_ID"
  http GET "$BASE_URL/chat/artifacts/$IMG_ID/content"
  assert_status 200 "get image content"

  # 3e. CSV
  create_artifact_json "${RUN_TAG}-preview-csv" "csv" "a,b,c"$'\n'"1,2,3"
  assert_status 201 "create csv"
  CSV_ID=$(json_field '.id'); track_artifact "$CSV_ID"
  http GET "$BASE_URL/chat/artifacts/$CSV_ID/content"
  assert_status 200 "get csv content"
  assert_body_contains "a,b,c" "csv content"

  # 3f. JSON
  create_artifact_json "${RUN_TAG}-preview-json" "json" '{"key":"value"}'
  assert_status 201 "create json"
  JSON_ID=$(json_field '.id'); track_artifact "$JSON_ID"
  http GET "$BASE_URL/chat/artifacts/$JSON_ID/content"
  assert_status 200 "get json content"
  assert_body_contains '"key":"value"' "json content"

  # 3g. Text
  create_artifact_json "${RUN_TAG}-preview-text" "text" "plain text preview"
  assert_status 201 "create text"
  TEXT_ID=$(json_field '.id'); track_artifact "$TEXT_ID"
  http GET "$BASE_URL/chat/artifacts/$TEXT_ID/content"
  assert_status 200 "get text content"
  assert_body_contains "plain text preview" "text content"

  # 3h. PDF (binary upload)
  printf '%%PDF-1.1\n1 0 obj<<>>endobj\ntrailer<<>>\n%%%%EOF\n' > "$TMP_DIR/min.pdf"
  create_artifact_file "${RUN_TAG}-preview-pdf" "pdf" "$TMP_DIR/min.pdf" "application/pdf"
  assert_status 201 "create pdf"
  PDF_ID=$(json_field '.id'); track_artifact "$PDF_ID"
  http GET "$BASE_URL/chat/artifacts/$PDF_ID/content"
  assert_status 200 "get pdf content"

  echo "[Artifact E2E] Section 3: ok (8 preview kinds)"

  # ---------------------------------------------------------------------------
  # Section 4: Malicious HTML/Markdown (no script execution)
  # ---------------------------------------------------------------------------

  echo "[Artifact E2E] Section 4: Malicious HTML/Markdown"

  # 4a. HTML with <script> -- content endpoint uses Content-Disposition: attachment
  # (raw HTML must not render same-origin).
  create_artifact_json "${RUN_TAG}-malicious-html" "html" \
    '<script>alert(1)</script><p>safe</p>'
  assert_status 201 "create malicious html"
  MAL_HTML_ID=$(json_field '.id'); track_artifact "$MAL_HTML_ID"
  http GET "$BASE_URL/chat/artifacts/$MAL_HTML_ID/content"
  assert_status 200 "get malicious html content"
  # Content-Disposition must be attachment (not inline) for HTML.
  # Re-fetch headers to verify (use -D to dump headers from a GET request).
  HDR=$(curl -sS -D - -o /dev/null "$BASE_URL/chat/artifacts/$MAL_HTML_ID/content" 2>/dev/null)
  if ! echo "$HDR" | grep -qi 'Content-Disposition:.*attachment'; then
    echo "FAIL: HTML content should use Content-Disposition: attachment" >&2
    echo "  headers: $HDR" >&2
    exit 1
  fi
  echo "[Artifact E2E] 4a. ok (html content uses attachment disposition)"

  # 4b. Markdown with <script> -- html export must not contain a live <script> tag.
  create_artifact_json "${RUN_TAG}-malicious-md" "markdown" \
    '<script>alert("xss")</script>'$'\n\n''Safe text.'
  assert_status 201 "create malicious markdown"
  MAL_MD_ID=$(json_field '.id'); track_artifact "$MAL_MD_ID"
  http GET "$BASE_URL/chat/artifacts/$MAL_MD_ID/export?format=html"
  assert_status 200 "export malicious markdown as html"
  # The sanitizer escapes disallowed tags to visible text, so <script> must
  # not appear as a live tag.  The escaped form &lt;script&gt; is acceptable.
  assert_body_not_contains "<script>" "html export has no live script tag"
  echo "[Artifact E2E] 4b. ok (markdown html export has no live script tag)"

  # ---------------------------------------------------------------------------
  # Section 5: Text/binary update (original attachment unchanged)
  # ---------------------------------------------------------------------------

  echo "[Artifact E2E] Section 5: Text update (original attachment unchanged)"

  # 5a. Upload an attachment with known content.
  echo "original attachment content" > "$TMP_DIR/update-att.txt"
  http POST "$BASE_URL/chat/tasks/$TASK_ID/attachments" \
    -F "file=@$TMP_DIR/update-att.txt;type=text/plain" \
    -F "uploaded_by=e2e"
  assert_status 200 "upload attachment for update test"
  UPDATE_ATT_ID=$(json_field '.id')

  # 5b. Find the auto-registered artifact (source_kind=task_attachment, matching task_id).
  http GET "$BASE_URL/chat/artifacts?source_kind=task_attachment&limit=500"
  assert_status 200 "list task_attachment artifacts"
  UPDATE_ART_ID=$(echo "$HTTP_BODY" | jq -r --arg tid "$TASK_ID" \
    '.items[] | select(.source_context_ref == $tid) | select(.name | contains("update-att")) | .id' | head -1)
  if [ -z "$UPDATE_ART_ID" ]; then
    echo "FAIL: could not find auto-registered artifact for attachment" >&2
    exit 1
  fi
  track_artifact "$UPDATE_ART_ID"

  # 5c. Verify original content.
  http GET "$BASE_URL/chat/artifacts/$UPDATE_ART_ID/content"
  assert_status 200 "get original artifact content"
  assert_body_contains "original attachment content" "original content"

  # 5d. PATCH the artifact content (materializes to owned storage).
  http PATCH "$BASE_URL/chat/artifacts/$UPDATE_ART_ID" \
    -H "Content-Type: application/json" \
    -d "$(jq -n '{content:"updated derived content"}')"
  assert_status 200 "patch artifact content"

  # 5e. Verify artifact content is updated.
  http GET "$BASE_URL/chat/artifacts/$UPDATE_ART_ID/content"
  assert_status 200 "get updated artifact content"
  assert_body_contains "updated derived content" "updated content"
  assert_body_not_contains "original attachment content" "old content gone"

  # 5f. Verify original attachment is unchanged.
  http GET "$BASE_URL/chat/tasks/attachments/$UPDATE_ATT_ID"
  assert_status 200 "download original attachment"
  assert_body_contains "original attachment content" "original attachment unchanged"
  echo "[Artifact E2E] 5. ok (original attachment unchanged after derived edit)"

  # ---------------------------------------------------------------------------
  # Section 6: Export (original/html, html rejected for non-markdown)
  # ---------------------------------------------------------------------------

  echo "[Artifact E2E] Section 6: Export"

  # 6a. Markdown: original export.
  http GET "$BASE_URL/chat/artifacts/$MD_ID/export?format=original"
  assert_status 200 "markdown original export"
  assert_body_contains "# Markdown preview" "markdown original export content"

  # 6b. Markdown: html export.
  http GET "$BASE_URL/chat/artifacts/$MD_ID/export?format=html"
  assert_status 200 "markdown html export"

  # 6c. Code: html export -> 422 (only markdown/document support html export).
  http GET "$BASE_URL/chat/artifacts/$CODE_ID/export?format=html"
  assert_status 422 "code html export rejected"
  echo "[Artifact E2E] 6. ok (export original/html, non-markdown html rejected)"

  # ---------------------------------------------------------------------------
  # Section 7: Publish/reuse/replacement/revoke
  # ---------------------------------------------------------------------------

  echo "[Artifact E2E] Section 7: Publish lifecycle"

  # 7a. Publish a markdown artifact.
  create_artifact_json "${RUN_TAG}-pub" "markdown" "## Publishable content"
  assert_status 201 "create publishable artifact"
  PUB_ART_ID=$(json_field '.id'); track_artifact "$PUB_ART_ID"
  http POST "$BASE_URL/chat/artifacts/$PUB_ART_ID/publish"
  assert_status 200 "publish (first)"
  PUB_ID_1=$(json_field '.publish_id')
  PUB_REUSED_1=$(json_field '.reused')
  PUB_SHARE_URL=$(json_field '.share_url')
  if [ "$PUB_REUSED_1" != "false" ]; then
    echo "FAIL: first publish should have reused=false, got $PUB_REUSED_1" >&2
    exit 1
  fi
  if [ -z "$PUB_ID_1" ]; then
    echo "FAIL: publish_id is empty" >&2
    exit 1
  fi
  echo "[Artifact E2E] 7a. ok (publish_id=$PUB_ID_1)"

  # 7b. Publish again -- same checksum -> reuse.
  http POST "$BASE_URL/chat/artifacts/$PUB_ART_ID/publish"
  assert_status 200 "publish (reuse)"
  PUB_ID_2=$(json_field '.publish_id')
  PUB_REUSED_2=$(json_field '.reused')
  if [ "$PUB_ID_2" != "$PUB_ID_1" ]; then
    echo "FAIL: reuse should return same publish_id ($PUB_ID_1), got $PUB_ID_2" >&2
    exit 1
  fi
  if [ "$PUB_REUSED_2" != "true" ]; then
    echo "FAIL: second publish should have reused=true, got $PUB_REUSED_2" >&2
    exit 1
  fi
  echo "[Artifact E2E] 7b. ok (reuse same publish_id)"

  # 7c. Edit artifact content (new checksum).
  http PATCH "$BASE_URL/chat/artifacts/$PUB_ART_ID" \
    -H "Content-Type: application/json" \
    -d "$(jq -n '{content:"## Updated publishable content"}')"
  assert_status 200 "edit published artifact"

  # 7d. Publish again -- different checksum -> replacement (new publish_id).
  http POST "$BASE_URL/chat/artifacts/$PUB_ART_ID/publish"
  assert_status 200 "publish (replacement)"
  PUB_ID_3=$(json_field '.publish_id')
  PUB_REUSED_3=$(json_field '.reused')
  if [ "$PUB_ID_3" = "$PUB_ID_1" ]; then
    echo "FAIL: replacement should return new publish_id, got same as $PUB_ID_1" >&2
    exit 1
  fi
  if [ "$PUB_REUSED_3" != "false" ]; then
    echo "FAIL: replacement publish should have reused=false, got $PUB_REUSED_3" >&2
    exit 1
  fi
  echo "[Artifact E2E] 7d. ok (replacement new publish_id=$PUB_ID_3)"

  # 7e. Old publish_id -> 410 (revoked by replacement).
  http GET "$BASE_URL/p/$PUB_ID_1"
  assert_status 410 "old publish page is 410"
  http GET "$BASE_URL/p/$PUB_ID_1/content"
  assert_status 410 "old publish content is 410"
  echo "[Artifact E2E] 7e. ok (old publish 410)"

  # 7f. New publish_id -> 200.
  http GET "$BASE_URL/p/$PUB_ID_3"
  assert_status 200 "new publish page is 200"

  # 7g. Revoke publish -> both endpoints 410.
  http DELETE "$BASE_URL/chat/artifacts/$PUB_ART_ID/publish"
  assert_status 200 "revoke publish"
  http GET "$BASE_URL/p/$PUB_ID_3"
  assert_status 410 "revoked publish page is 410"
  http GET "$BASE_URL/p/$PUB_ID_3/content"
  assert_status 410 "revoked publish content is 410"
  echo "[Artifact E2E] 7g. ok (revoke -> both 410)"

  # ---------------------------------------------------------------------------
  # Section 8: Source-delete-snapshot (published link survives source deletion)
  # ---------------------------------------------------------------------------

  echo "[Artifact E2E] Section 8: Source-delete-snapshot"

  # 8a. Create + publish.
  create_artifact_json "${RUN_TAG}-snapshot" "markdown" "Snapshot test content"
  assert_status 201 "create snapshot artifact"
  SNAP_ART_ID=$(json_field '.id')
  http POST "$BASE_URL/chat/artifacts/$SNAP_ART_ID/publish"
  assert_status 200 "publish snapshot artifact"
  SNAP_PUB_ID=$(json_field '.publish_id')

  # 8b. Delete the source artifact.
  http DELETE "$BASE_URL/chat/artifacts/$SNAP_ART_ID"
  assert_status 204 "delete snapshot source artifact"

  # 8c. Published page + content still return 200 (snapshot persists).
  http GET "$BASE_URL/p/$SNAP_PUB_ID"
  assert_status 200 "published page survives source deletion"
  http GET "$BASE_URL/p/$SNAP_PUB_ID/content"
  assert_status 200 "published content survives source deletion"
  assert_body_contains "Snapshot test content" "snapshot content intact"
  echo "[Artifact E2E] 8. ok (snapshot persists after source delete)"

  # ---------------------------------------------------------------------------
  # Section 9: Size limits (inline/create/publish)
  # ---------------------------------------------------------------------------

  echo "[Artifact E2E] Section 9: Size limits"

  # 9a. Inline content > 256 KB -> 413.
  LARGE_INLINE=$(head -c 262145 /dev/zero | tr '\0' 'a')
  http POST "$BASE_URL/chat/artifacts" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg c "$LARGE_INLINE" --arg n "${RUN_TAG}-large-inline" \
      '{name:$n, kind:"text", content:$c}')"
  assert_status 413 "inline over 256KB rejected"
  echo "[Artifact E2E] 9a. ok (inline over-limit -> 413)"

  # 9b. File upload > 20 MB -> 413.
  dd if=/dev/zero of="$TMP_DIR/over-max.txt" bs=1048576 count=21 2>/dev/null
  http POST "$BASE_URL/chat/artifacts" \
    -F "name=${RUN_TAG}-over-max" \
    -F "kind=text" \
    -F "file=@$TMP_DIR/over-max.txt;type=text/plain"
  assert_status 413 "file over 20MB rejected"
  echo "[Artifact E2E] 9b. ok (file over-limit -> 413)"

  # 9c. Publish artifact > 10 MB -> 422 (publish_blocked: size_over_limit).
  dd if=/dev/zero of="$TMP_DIR/over-publish.txt" bs=1048576 count=11 2>/dev/null
  create_artifact_file "${RUN_TAG}-over-publish" "text" "$TMP_DIR/over-publish.txt" "text/plain"
  assert_status 201 "create 11MB artifact"
  OVER_PUB_ID=$(json_field '.id'); track_artifact "$OVER_PUB_ID"
  http POST "$BASE_URL/chat/artifacts/$OVER_PUB_ID/publish"
  assert_status 422 "publish over 10MB blocked"
  echo "[Artifact E2E] 9c. ok (publish over-limit -> 422)"

  # ---------------------------------------------------------------------------
  # Section 10: Invalid paths (publish_id validation, path traversal)
  # ---------------------------------------------------------------------------

  echo "[Artifact E2E] Section 10: Invalid paths"

  # 10a. Short publish_id -> 404.
  http GET "$BASE_URL/p/short"
  assert_status 404 "short publish_id -> 404"

  # 10b. Path traversal in publish_id -> 404 (regex rejects).
  http GET "$BASE_URL/p/..%2F..%2Fetc%2Fpasswd"
  assert_status 404 "path traversal publish_id -> 404"

  # 10c. Invalid characters in publish_id -> 404.
  http GET "$BASE_URL/p/invalid;chars;here"
  assert_status 404 "invalid chars publish_id -> 404"

  # 10d. Non-existent artifact_id -> 404.
  http GET "$BASE_URL/chat/artifacts/nonexistent-id-12345"
  assert_status 404 "non-existent artifact -> 404"
  echo "[Artifact E2E] 10. ok (invalid paths rejected)"

  # ---------------------------------------------------------------------------
  # Section 11: Gating/config (enabled mode)
  # ---------------------------------------------------------------------------

  echo "[Artifact E2E] Section 11: Gating/config"

  # 11a. /artifacts page -> 200.
  http GET "$BASE_URL/artifacts"
  assert_status 200 "artifacts page"
  assert_body_contains 'data-tab="artifacts"' "artifacts menu item present"
  echo "[Artifact E2E] 11a. ok (/artifacts page + menu)"

  # 11b. /chat/artifacts API -> 200.
  http GET "$BASE_URL/chat/artifacts?limit=1"
  assert_status 200 "artifacts API"
  echo "[Artifact E2E] 11b. ok (/chat/artifacts API)"

  # 11c. /p/{publish_id} public route -> 200 (use a fresh publish).
  create_artifact_json "${RUN_TAG}-gate" "markdown" "Gating config test"
  assert_status 201 "create gating artifact"
  GATE_ART_ID=$(json_field '.id'); track_artifact "$GATE_ART_ID"
  http POST "$BASE_URL/chat/artifacts/$GATE_ART_ID/publish"
  assert_status 200 "publish gating artifact"
  GATE_PUB_ID=$(json_field '.publish_id')
  GATE_SHARE_URL=$(json_field '.share_url')
  http GET "$BASE_URL/p/$GATE_PUB_ID"
  assert_status 200 "public route"
  echo "[Artifact E2E] 11c. ok (/p/* public route)"

  # 11d. Relative share_url when published_base_url empty (default).
  # The share_url from the publish response should start with "/p/".
  case "$GATE_SHARE_URL" in
    /p/*) echo "[Artifact E2E] 11d. ok (relative share_url: $GATE_SHARE_URL)" ;;
    *)
      echo "FAIL: share_url should be relative (/p/...), got: $GATE_SHARE_URL" >&2
      exit 1
      ;;
  esac

  # 11e. Host spoof does not change share_url.
  # Publish with a spoofed Host header; share_url must still be relative.
  create_artifact_json "${RUN_TAG}-hostspoof" "markdown" "Host spoof test"
  assert_status 201 "create host-spoof artifact"
  SPOOF_ART_ID=$(json_field '.id'); track_artifact "$SPOOF_ART_ID"
  HTTP_STATUS=$(curl -sS -o "$TMP_DIR/spoof-resp.txt" -w "%{http_code}" \
    -X POST -H "Host: evil.example.com" \
    "$BASE_URL/chat/artifacts/$SPOOF_ART_ID/publish" 2>/dev/null) || true
  HTTP_BODY="$(cat "$TMP_DIR/spoof-resp.txt")"
  assert_status 200 "publish with spoofed host"
  SPOOF_SHARE_URL=$(echo "$HTTP_BODY" | jq -r '.share_url')
  case "$SPOOF_SHARE_URL" in
    /p/*) echo "[Artifact E2E] 11e. ok (Host spoof did not change share_url)" ;;
    *)
      echo "FAIL: Host spoof changed share_url to: $SPOOF_SHARE_URL" >&2
      exit 1
      ;;
  esac

  # 11f. Callbacks/backfill are wired (attachments auto-registered in 1b).
  # Verify the 2 attachment artifacts exist with source_kind=task_attachment.
  http GET "$BASE_URL/chat/artifacts?source_kind=task_attachment&limit=500"
  assert_status 200 "list task_attachment artifacts"
  ATT_ART_COUNT=$(echo "$HTTP_BODY" | jq --arg tag "$RUN_TAG" \
    '[.items[] | select(.name | contains($tag))] | length')
  if [ "$ATT_ART_COUNT" -lt 2 ]; then
    echo "FAIL: expected >= 2 task_attachment artifacts, got $ATT_ART_COUNT" >&2
    exit 1
  fi
  echo "[Artifact E2E] 11f. ok ($ATT_ART_COUNT attachment artifacts auto-registered)"

  echo "[Artifact E2E] PASS"
)

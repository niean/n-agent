#!/usr/bin/env bash
# Host side only orchestrates Docker; all probes execute inside the test container.
source "$(dirname "${BASH_SOURCE[0]}")/environment.sh"
set -uo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
run_id="${LANGFUSE_SMOKE_RUN_ID:-langfuse-smoke-$(date +%Y%m%d-%H%M%S)-$$}"
report="$root/locals/e2e-reports/$run_id"
mkdir -p "$report"
if [ -z "${N_AGENT_E2E_ENVIRONMENT:-}" ]; then
  export LANGFUSE_SMOKE_RUN_ID="$run_id"
  mkdir -m 700 -p "$root/locals/harness_tmp/$run_id"
  export N_AGENT_E2E_RESTORE_STATUS_FILE="$root/locals/harness_tmp/$run_id/environment-restoration.txt"
  # An interrupted run cannot inherit a previous PASS restoration marker.
  (umask 077; printf 'FAIL\n' > "$N_AGENT_E2E_RESTORE_STATUS_FILE")
  bash "$(dirname "$0")/environment.sh" default "$0" "$@"
  status=$?
  restored=FAIL
  [ "$(cat "$N_AGENT_E2E_RESTORE_STATUS_FILE" 2>/dev/null)" != PASS ] || restored=PASS
  inner_cleanup=FAIL
  [ "$(cat "$root/locals/harness_tmp/$run_id/cleanup-status.txt" 2>/dev/null)" != PASS ] || inner_cleanup=PASS
  image="$(docker inspect n-agent-n-agent-1 --format '{{.Config.Image}}')"
  docker run --rm --network n-agent_default --entrypoint python \
    -e LANGFUSE_APP_URL=http://n-agent-n-agent-1:8201 \
    -v "$root/tests/e2e:/tests:ro" -v "$report:/reports" \
    -v "$root/docker/langfuse/.env:/credentials:ro" \
    "$image" /tests/langfuse_smoke.py /reports "$run_id" environment-cleanup "$restored" "$inner_cleanup" || status=1
  [ "$restored" = PASS ] || status=1
  [ "$inner_cleanup" = PASS ] || status=1
  exit "$status"
fi
e2e_environment default "$0" "$@"
tmp="$root/locals/harness_tmp/$run_id"
mkdir -m 700 -p "$tmp"
name="e2e-$run_id"
slow="$name-slow"
web_stopped=0
collector_changed=0
container_created=0
slow_created=0
(umask 077; printf 'FAIL\n' > "$tmp/cleanup-status.txt")
result=0
probe() { docker exec "$name" python /tests/langfuse_smoke.py /reports "$run_id" "$@"; }
# This helper is infrastructure-only Docker Compose orchestration. It never
# imports or runs N-Agent application code on the host, and never echoes secrets.
cat > "$tmp/collector.py" <<'PY'
import importlib.util,json,sys
from pathlib import Path
root=Path(sys.argv[1]); mode=sys.argv[2]; slow=sys.argv[3]
spec=importlib.util.spec_from_file_location('langfuse_smoke_stack',root/'docker/observability/manage-stack.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
stack=m.Stack(root/'docker/observability',progress=False)
selected=stack.langfuse()
if mode!='restore':
    cfg=json.loads(selected['config'].read_text())
    if mode=='auth':
        # One shared credential: a rejected Basic breaks both Langfuse pipelines.
        for exporter in ('otlphttp/langfuse','otlphttp/langfuse-kb'):
            cfg['exporters'][exporter]['headers']['Authorization']='Basic ZTJlLWJhZDplMmUtYmFk'
    else:
        target='otlphttp/langfuse-kb' if mode=='slow-kb' else 'otlphttp/langfuse'
        cfg['exporters'][target]['traces_endpoint']='http://'+slow+':9988/v1/traces'
    mod=stack.langfuse_module()
    path=mod.publish_json(root/'locals/harness_tmp/langfuse-collector',cfg)
    override=json.loads(selected['override'].read_text())
    for volume in override['services']['otel-collector']['volumes']:
        if volume['target']=='/etc/otelcol-contrib/langfuse.json': volume['source']=str(path)
    stack._langfuse={**selected,'override':mod.publish_json(root/'locals/harness_tmp/langfuse-collector',override)}
stack.compose('up','-d','--no-deps','--force-recreate','--wait','otel-collector')
PY
restore() {
  local status=$? cleanup=PASS
  trap - EXIT
  if [ "$web_stopped" = 1 ]; then
    sh "$root/docker/langfuse/restart.sh" start >"$tmp/web-restore.log" 2>&1 || cleanup=FAIL
  fi
  if [ "$collector_changed" = 1 ]; then
    python3 "$tmp/collector.py" "$root" restore "$slow" >"$tmp/collector-restore.log" 2>&1 || cleanup=FAIL
  fi
  if [ "$slow_created" = 1 ]; then
    docker rm -f "$slow" >/dev/null 2>&1 || cleanup=FAIL
  fi
  if [ "$container_created" = 1 ]; then
    probe sessions-cleanup || cleanup=FAIL
    probe audit || cleanup=FAIL
    probe cleanup "$cleanup" || cleanup=FAIL
    probe summary || true
    if ! docker rm -f "$name" >/dev/null 2>&1; then
      cleanup=FAIL
      probe cleanup FAIL || true
    fi
  fi
  (umask 077; printf '%s\n' "$cleanup" > "$tmp/cleanup-status.txt") || status=1
  echo "RESULT_FILE $report/result.md"
  [ "$cleanup" = PASS ] || status=1
  exit "$status"
}
trap restore EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
image="$(docker inspect n-agent-n-agent-1 --format '{{.Config.Image}}')"
docker create --name "$name" --network n-telemetry --entrypoint sleep \
  -e LANGFUSE_APP_URL=http://n-agent-n-agent-1:8201 \
  -v "$root/tests/e2e:/tests:ro" -v "$report:/reports" \
  -v "$root/docker/langfuse/.env:/credentials:ro" \
  -v "$root/locals/harness_tmp:/history:ro" "$image" infinity > /dev/null || exit 1
container_created=1
docker network connect n-observability_default "$name" || exit 1
docker network connect n-agent_default "$name" || exit 1
docker start "$name" >/dev/null || exit 1
for action in synthetic kb-synthetic business sampling concurrent history; do probe "$action" || result=1; done
web_stopped=1
sh "$root/docker/langfuse/restart.sh" compose stop langfuse-web >"$tmp/web-stop.log" 2>&1 || exit 1
probe fault-web || result=1
probe fault-web-kb || result=1
sh "$root/docker/langfuse/restart.sh" start >"$tmp/web-start.log" 2>&1 || exit 1
web_stopped=0
probe business || result=1
probe kb-recovered || result=1
collector_changed=1
python3 "$tmp/collector.py" "$root" auth "$slow" >"$tmp/auth.log" 2>&1 || exit 1
probe fault-auth || result=1
python3 "$tmp/collector.py" "$root" restore "$slow" >"$tmp/auth-restore.log" 2>&1 || exit 1
collector_changed=0
probe business || result=1
slow_created=1
docker run -d --name "$slow" --network n-telemetry --entrypoint python \
  -v "$root/tests/e2e:/tests:ro" "$image" /tests/langfuse_smoke.py serve > /dev/null || exit 1
collector_changed=1
python3 "$tmp/collector.py" "$root" slow "$slow" >"$tmp/slow.log" 2>&1 || exit 1
probe fault-slow || result=1
probe saturation || result=1
probe fault-queue || result=1
python3 "$tmp/collector.py" "$root" restore "$slow" >"$tmp/slow-restore.log" 2>&1 || exit 1
collector_changed=0
probe business || result=1
collector_changed=1
python3 "$tmp/collector.py" "$root" slow-kb "$slow" >"$tmp/slow-kb.log" 2>&1 || exit 1
probe fault-queue-kb || result=1
python3 "$tmp/collector.py" "$root" restore "$slow" >"$tmp/slow-kb-restore.log" 2>&1 || exit 1
collector_changed=0
probe business || result=1
sh "$root/docker/langfuse/restart.sh" compose restart langfuse-web >"$tmp/web-restart.log" 2>&1 || exit 1
sh "$root/docker/langfuse/restart.sh" start >>"$tmp/web-restart.log" 2>&1 || exit 1
probe persisted || result=1
probe history || result=1
probe orchestration PASS || result=1
exit "$result"

#!/usr/bin/env bash
# Host orchestrates only; the model and all backend probes run in Docker.
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
report="$1"
run_id="$2"
app_container="$3"
name="e2e-comparison-$run_id"
created=0
started=0
published=0
report_tool="$root/tests/e2e/result_report.py"
cleanup() {
  local result=$?
  trap - EXIT
  if [ "$started" = 1 ]; then
    # Stop any interrupted exec first: it must not race cleanup and recreate
    # its session. Restart only this disposable probe, then replay its manifest.
    if docker stop --time 1 "$name" >/dev/null 2>&1 \
      && docker start "$name" >/dev/null 2>&1 \
      && docker exec "$name" python /tests/otel_langfuse_comparison.py --cleanup-only /reports "$run_id"; then
      python3 "$report_tool" "$report" comparison comparison/comparison.json || result=1
    else
      python3 "$report_tool" "$report" comparison-error "对账会话清理或删除后回读失败" || true
      result=1
    fi
  fi
  if [ "$created" = 1 ] && ! docker rm -f "$name" >/dev/null 2>&1; then
    python3 "$report_tool" "$report" comparison-error "对账探针容器清理失败" || true
    result=1
  fi
  if [ "$result" -ne 0 ] && [ "$published" = 0 ]; then
    python3 "$report_tool" "$report" comparison-error "对账探针执行中断或初始化失败，详见运行日志" || true
  fi
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

# Absence is an explicit SKIP, never a fabricated successful comparison.
if [ ! -f "$root/docker/langfuse/.env" ]; then
  python3 "$report_tool" "$report" comparison-skip "Langfuse未配置，未执行数值对比"
  exit 0
fi
collector_args="$(docker inspect n-observability-otel-collector-1 --format '{{json .Config.Cmd}}')" || {
  python3 "$report_tool" "$report" comparison-error "无法检查Collector的Langfuse分支"
  exit 1
}
if [[ "$collector_args" != *"/etc/otelcol-contrib/langfuse.json"* ]]; then
  python3 "$report_tool" "$report" comparison-skip "Collector未启用Langfuse分支，未执行数值对比"
  exit 0
fi

mkdir -p "$report/comparison"
image="$(docker inspect "$app_container" --format '{{.Config.Image}}')"
app_name="$(docker inspect "$app_container" --format '{{.Name}}' | tr -d /)"
project="$(docker inspect "$app_container" --format '{{index .Config.Labels "com.docker.compose.project"}}')"
docker create --name "$name" --network n-telemetry --entrypoint sleep \
  -e "LANGFUSE_APP_URL=http://$app_name:8201" \
  -v "$root/tests/e2e:/tests:ro" -v "$report/comparison:/reports" \
  -v "$root/docker/langfuse/.env:/credentials:ro" "$image" infinity >/dev/null
created=1
docker network connect "${project}_default" "$name"
docker network connect n-observability_default "$name"
docker start "$name" >/dev/null
started=1
result=0
docker exec "$name" python /tests/otel_langfuse_comparison.py /reports "$run_id" || result=$?
if [ -f "$report/comparison/comparison.json" ]; then
  python3 "$report_tool" "$report" comparison comparison/comparison.json
  published=1
else
  python3 "$report_tool" "$report" comparison-error "对账探针未生成结果，详见运行日志"
  published=1
  result=1
fi
exit "$result"

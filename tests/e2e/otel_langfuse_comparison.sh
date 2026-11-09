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
BASE_URL="${N_AGENT_OTEL_SMOKE_BASE_URL:-http://127.0.0.1:8201}"

# Manual-only entry: keep a standalone report, separate from the E2E suite.
python3 "$report_tool" "$report" init-comparison "$run_id"

# ---------------------------------------------------------------------------
# T8 成本探测部署编排（协调者授权的 T5 遗留接线收尾，仅 E2E 运行期生效）。
# 两服务注入同一测试价格目录（内容版本+JCS 哈希见
# tests/e2e/fixtures/model-pricing-e2e.json），n-kb 经 compose override 注入
# （不修改 n-kb 仓任何文件）；n-agent 经运行期 override 注入测试目录、受信计价
# 身份映射（指向确定性 stub Provider）并在窗口后还原。默认部署不受影响。
# ---------------------------------------------------------------------------
COST_CATALOG_JCS_SHA256="1536ca56d137af4f903d3ca43b589e986cfddfb3395a2e0a357d537bdb40c758"
COST_PROBE_MODEL="e2e-cost-model"
FAKE_LLM_PORT=9999
FAKE_PROVIDER_NAME="e2e-cost-probe"
nkb_docker="$root/../n-kb/docker"
cost_dir=""
fake_provider_id=""
orig_provider_id=""
fake_llm_started=0
nkb_test_pricing=0
nagent_test_pricing=0

# 继承 environment.sh 经 COMPOSE_FILE（绝对路径、冒号分隔）传入的 override 链；
# compose 的 -f 一旦出现即覆盖 COMPOSE_FILE，必须展开为重复 -f 才不丢失前链。
# 独立运行（无 COMPOSE_FILE）时回退到基础 compose 文件。
# ${arr[@]+...} 写法兼容 macOS bash 3.2 的空数组 + set -u。
base_compose_args=()
if [ -n "${COMPOSE_FILE:-}" ]; then
  IFS=':' read -r -a file_list <<< "$COMPOSE_FILE"
  for file in "${file_list[@]}"; do
    [ -n "$file" ] && base_compose_args+=(-f "$file")
  done
else
  base_compose_args=(-f "$root/docker/docker-compose.yml")
fi

# n-kb 就绪等待：自检接口可达即认为检索服务可用（ recreate 后 T6 代码生效）。
wait_nkb_ready() {
  local container="$1" i
  for i in $(seq 1 30); do
    if docker exec "$container" python3 -c "
import urllib.request
urllib.request.urlopen('http://127.0.0.1:8212/observations/otel', timeout=3).read()
" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "n-kb self-check endpoint did not become ready" >&2
  return 1
}

restore_cost_environment() {
  local status=0 code i
  if [ "$fake_llm_started" = 1 ]; then
    docker exec "$app_name" pkill -f "fake_llm_cost.py" >/dev/null 2>&1 || true
    fake_llm_started=0
  fi
  if [ -n "$fake_provider_id" ]; then
    if [ -n "$orig_provider_id" ]; then
      # 先恢复原激活 Provider，再删除 stub（激活中的 Provider 不可删除）。
      curl -fsS -X POST "$BASE_URL/chat/providers/$orig_provider_id/activate" >/dev/null 2>&1 \
        || status=1
    fi
    for i in 1 2 3 4 5; do
      code="$(curl -s -o /dev/null -w '%{http_code}' \
        -X DELETE "$BASE_URL/chat/providers/$fake_provider_id")" || code=000
      { [ "$code" = "204" ] || [ "$code" = "404" ]; } && break
      sleep 2
    done
    case "$code" in 204|404) ;; *) status=1 ;; esac
    fake_provider_id=""
  fi
  if [ "$nagent_test_pricing" = 1 ]; then
    (cd "$root/docker" && docker compose \
      ${base_compose_args[@]+"${base_compose_args[@]}"} \
      up -d --force-recreate --no-deps --wait n-agent) || status=1
    nagent_test_pricing=0
  fi
  if [ "$nkb_test_pricing" = 1 ]; then
    # -f 必须显式给出：本 shell 继承了 environment.sh 导出的 COMPOSE_FILE
    # （指向 n-agent override 链），裸 compose 会解析出 "no such service: n-kb"。
    # 容器 id 必须在 recreate 之后再解析：recreate 会更换容器 id。
    if (cd "$nkb_docker" && docker compose -f docker-compose.yml \
        up -d --no-deps --force-recreate n-kb) \
      && nkb_container="$(docker compose -f "$nkb_docker/docker-compose.yml" ps -q n-kb)" \
      && [ -n "$nkb_container" ] \
      && wait_nkb_ready "$nkb_container"; then
      :
    else
      status=1
    fi
    nkb_test_pricing=0
  fi
  if [ -n "$cost_dir" ]; then
    rm -rf "$cost_dir"
    cost_dir=""
  fi
  return $status
}

cleanup() {
  local result=$?
  trap - EXIT
  # 成本探测部署必须还原（先还原再清理探针；还原失败置失败并继续清理）。
  if ! restore_cost_environment; then
    python3 "$report_tool" "$report" comparison-error "成本探测部署还原失败，详见运行日志" || true
    result=1
  fi
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
# T8 隔离探针价格目录：与两服务注入的测试目录同文件（内容版本+哈希见
# tests/e2e/fixtures/model-pricing-e2e.json），探针据此重算本地 Decimal 预期。
probe_catalog="$root/tests/e2e/fixtures/model-pricing-e2e.json"
if [ ! -f "$probe_catalog" ]; then
  python3 "$report_tool" "$report" comparison-error "缺少E2E测试价格目录fixture" || true
  exit 1
fi

# --- Phase C：部署成本探测环境（失败即中止，绝不以默认部署跑成本断言） ---
deploy_cost_environment() {
  # 1) n-kb 注入测试价格目录（override 来自本仓 fixtures，不编辑 n-kb 仓）；
  #    --build 携带 n-kb 侧成本采集代码。
  # 评审修复（第三轮）：还原责任必须在首次环境改动之前登记——compose
  # 重建容器后 start/--wait 失败时函数提前返回，若标志在成功后才置位，
  # cleanup 会跳过还原，遗留测试目录挂载、身份映射或半启动容器。
  if [ ! -f "$nkb_docker/docker-compose.yml" ]; then
    echo "n-kb compose not found: $nkb_docker/docker-compose.yml" >&2
    return 1
  fi
  nkb_test_pricing=1
  (cd "$nkb_docker" && E2E_PRICING_CATALOG_HOST_PATH="$probe_catalog" \
    docker compose -f docker-compose.yml \
      -f "$root/tests/e2e/fixtures/nkb-pricing-override.yml" \
      up -d --build --no-deps n-kb) || return 1
  nkb_container="$(docker compose -f "$nkb_docker/docker-compose.yml" ps -q n-kb)"
  [ -n "$nkb_container" ] || return 1
  wait_nkb_ready "$nkb_container" || return 1
  docker exec "$nkb_container" sh -c 'test "$(printenv N_KB_PRICING_CATALOG_PATH)" = "/etc/n-kb/model-pricing-e2e.json" && test -f /etc/n-kb/model-pricing-e2e.json' \
    || return 1
  # Ollama 空闲卸载 bge-m3 后冷加载（实测 16s+）叠加单核推理时延（稳态 8-15s）
  # 可超 N-KB 30s 嵌入超时并使 /retrieval/search 返回 502（attempt-10 根因，
  # 环境时序噪声而非语义缺陷）。部署窗口内 keep_alive 预载驻留 30 分钟，
  # 探针全程模型温热；失败即失败，不静默进入探针。
  docker exec "$nkb_container" python3 -c "
import json, urllib.request
req = urllib.request.Request('http://ollama:11434/api/embed',
    data=json.dumps({'model': 'bge-m3', 'input': ['e2e keep-alive preload'],
                     'keep_alive': '30m'}).encode(),
    headers={'Content-Type': 'application/json'}, method='POST')
urllib.request.urlopen(req, timeout=120).read()
" || return 1
  # 2) 记录当前激活 Provider；创建确定性 stub Provider（SQLite 持久化）。
  orig_provider_id="$(curl -fsS "$BASE_URL/chat/providers" | python3 -c '
import json, sys
rows = json.load(sys.stdin)
active = [r["id"] for r in rows
          if r.get("is_active") and not str(r.get("name", "")).startswith("e2e-cost-probe")]
print(active[0] if active else "")')" || return 1
  fake_provider_id="$(curl -fsS -X POST "$BASE_URL/chat/providers" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"$FAKE_PROVIDER_NAME\",\"base_url\":\"http://127.0.0.1:$FAKE_LLM_PORT/v1\",\"model\":\"$COST_PROBE_MODEL\",\"api_key\":\"e2e-cost-probe\",\"provider_type\":\"openai-compatible\",\"supports_vision\":false}" \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["id"])')" || return 1
  [ -n "$fake_provider_id" ] || return 1
  # 3) 运行期 override：测试目录挂载 + 目录环境 + 受信计价身份映射
  #    （requested_model 为 stub Provider 的模型名；占位符 N-Agent 解析后命中）。
  # 还原责任先于首次环境改动登记（--wait 失败时 trap cleanup 仍会还原）。
  cost_dir="$(mktemp -d "${TMPDIR:-/tmp}/e2e-cost-override.XXXXXX")"
  python3 - "$cost_dir/cost-override.json" "$fake_provider_id" "$probe_catalog" \
    "$COST_CATALOG_JCS_SHA256" <<'PYEOF'
import json, sys
path, provider_id, catalog, sha = sys.argv[1:5]
identity = json.dumps([
    {"provider_id": provider_id, "requested_model": "e2e-cost-model",
     "billing_provider": "openai", "billing_model": "e2e-cost-model"}])
document = {"services": {"n-agent": {
    "environment": {
        "N_AGENT_PRICING_CATALOG_PATH": "/etc/n-agent/model-pricing-e2e.json",
        "N_AGENT_PRICING_CATALOG_EXPECTED_SHA256": sha,
        "N_AGENT_MODEL_BILLING_IDENTITY_MAP": identity},
    "volumes": [catalog + ":/etc/n-agent/model-pricing-e2e.json:ro"]}}}
open(path, "w").write(json.dumps(document, indent=1) + "\n")
PYEOF
  nagent_test_pricing=1
  (cd "$root/docker" && docker compose \
    ${base_compose_args[@]+"${base_compose_args[@]}"} \
    -f "$cost_dir/cost-override.json" \
    up -d --force-recreate --no-deps --wait n-agent) || return 1
  # 4) 容器内验证：计价模块存在、测试目录环境生效（启动已因哈希校验 fail-fast）。
  docker exec "$app_name" python3 -c "import app.infrastructure.usage.pricing_catalog" \
    || return 1
  docker exec "$app_name" sh -c \
    'test "$(printenv N_AGENT_PRICING_CATALOG_PATH)" = "/etc/n-agent/model-pricing-e2e.json" && test -f /etc/n-agent/model-pricing-e2e.json' \
    || return 1
  # 5) 安装并启动确定性 stub LLM（用量恒为向量 B / 显式全零），激活 stub Provider。
  docker cp "$root/tests/e2e/fixtures/fake_llm_cost.py" "$app_name":/tmp/fake_llm_cost.py
  docker cp "$root/tests/e2e/fixtures/cost-probe-query.txt" "$app_name":/tmp/cost-probe-query.txt
  docker exec -d "$app_name" python3 /tmp/fake_llm_cost.py "$FAKE_LLM_PORT" /tmp/cost-probe-query.txt \
    || return 1
  fake_llm_started=1
  local i
  for i in $(seq 1 20); do
    if docker exec "$app_name" python3 -c "
import urllib.request
urllib.request.urlopen('http://127.0.0.1:$FAKE_LLM_PORT/health', timeout=2).read()
" >/dev/null 2>&1; then
      break
    fi
    sleep 0.5
  done
  docker exec "$app_name" python3 -c "
import urllib.request
urllib.request.urlopen('http://127.0.0.1:$FAKE_LLM_PORT/health', timeout=2).read()
" >/dev/null 2>&1 || return 1
  curl -fsS -X POST "$BASE_URL/chat/providers/$fake_provider_id/activate" >/dev/null || return 1
}
if ! deploy_cost_environment; then
  python3 "$report_tool" "$report" comparison-error "成本探测部署失败，详见运行日志" || true
  exit 1
fi

docker create --name "$name" --network n-telemetry --entrypoint sleep \
  -e "LANGFUSE_APP_URL=http://$app_name:8201" \
  -e "E2E_PRICING_CATALOG=/tests/fixtures/model-pricing-e2e.json" \
  -e "N_KB_APP_URL=${N_KB_APP_URL:-http://n-kb:8212}" \
  -v "$root/tests/e2e:/tests:ro" -v "$report/comparison:/reports" \
  -v "$root/docker/langfuse/.env:/credentials:ro" "$image" infinity >/dev/null
created=1
docker network connect "${project}_default" "$name"
docker network connect n-observability_default "$name"
# 跨服务链与 N-KB Counter 对账需要直达 n-kb 服务；网络缺失时探针内显式 SKIP。
docker network connect n-kb_default "$name" 2>/dev/null || true
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

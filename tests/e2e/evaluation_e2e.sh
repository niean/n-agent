#!/usr/bin/env bash
# T22 评估闭环 E2E（Docker）：固定 3 已知样本三元对账（账本/Langfuse/Loki）、
# evaluate run/replay 容器内实验、Langfuse 停机业务不中断与恢复补发 verified、
# producer 边界矩阵（401/幂等重放/未采样 unlinked/迟到样本 409）。
# 宿主只编排 Docker；评估账本经容器内只读 SQLite 核验，Langfuse Score 经
# 容器内网 n-langfuse-web:3000/api/public 回读，Loki 事件经观测栈 Prometheus
# 容器转发查询。judge 凭据从宿主 locals/e2e-secrets.env 注入 compose
# override，凭据值不回显。
# Isolate autonomous skill evolution for this suite and every child/restart.
source "$(dirname "${BASH_SOURCE[0]}")/environment.sh"
e2e_environment default "$0" "$@"

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DOCKER_DIR="$REPO_ROOT/docker"

BASE_URL="${N_AGENT_EVAL_E2E_BASE_URL:-http://127.0.0.1:8201}"
HEALTH_ATTEMPTS=90
RESULT_WAIT_ATTEMPTS=90      # Judge 结果就绪：90 x 2s = 180s
SCORE_WAIT_ATTEMPTS=45       # Langfuse Score 回读：45 x 2s
DELIVERY_WAIT_ATTEMPTS=120   # 恢复补发 verified：120 x 2s = 240s

RUN_TAG="eval-e2e-$(date +%s)-$$"
REPORT_DIR="$REPO_ROOT/locals/e2e-reports/$RUN_TAG"
TMP_DIR="$(mktemp -d /tmp/eval-e2e-XXXXXX)"
TRACE_PAGE_URL=""

FAKE_PORT=9999
FAKE_LLM_PATH="/tmp/fake_llm_eval_e2e.py"
FAKE_PROVIDER_NAME="eval-e2e-$RUN_TAG"
FAKE_MODEL="eval-e2e-model"
FAKE_PROVIDER_ID=""
ORIG_PROVIDER_ID=""
CHAT_FINISH=""

PRODUCER_TOKEN="e2e-eval-producer-$RUN_TAG"
EVALUATION_DB="/app/locals/evaluations.db"
METRIC_JUDGE="agent.answer_relevance"
METRIC_DETERMINISTIC="kb.recall_at_5"
SCORE_NAME_JUDGE="agent.answer_relevance.judge-v1"
# 敏感哨兵：凭据形态文本，断言不出现在 Loki 事件与 Langfuse Score 注释
SENTINEL="E2EEVALSENTINEL-credentials"

FIXED_EVENTS=("e2e-eval-fixed-1-$RUN_TAG" "e2e-eval-fixed-2-$RUN_TAG" "e2e-eval-fixed-3-$RUN_TAG")

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------

compose() { (cd "$DOCKER_DIR" && docker compose "$@"); }
lf_stack() { sh "$DOCKER_DIR/langfuse/restart.sh" "$@"; }
obs_compose() { sh "$DOCKER_DIR/observability/restart.sh" compose "$@"; }

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

json_field() {
  echo "$HTTP_BODY" | jq -r "$1" 2>/dev/null
}

wait_health() {
  local i
  for i in $(seq 1 "$1"); do
    if curl -fsS --connect-timeout 1 --max-time 3 "$BASE_URL/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  fail "n-agent container did not become healthy within $1 attempts"
}

report_case() {
  python3 "$SCRIPT_DIR/result_report.py" "$REPORT_DIR" "$@" || fail "E2E report write failed"
}

utc_iso() { # UTC ISO-8601 with offset（与账本 _iso 同格式，可字典序比较）
  python3 -c 'import datetime; print(datetime.datetime.now(datetime.timezone.utc).isoformat())'
}

json_len() { # json_len <env-var-name-holding-json-array> -> count
  eval "local payload=\"\$$1\""
  PAYLOAD_JSON="$payload" python3 -c 'import json, os; print(len(json.loads(os.environ["PAYLOAD_JSON"])))'
}

# 账本只读核验（容器内 python sqlite3，只读不写；SQL 由脚本固定，无注入面）
ledger_query() { # ledger_query <sql>
  compose exec -T n-agent python - "$EVALUATION_DB" "$1" <<'PY'
import json, sqlite3, sys

conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
try:
    rows = [dict(r) for r in conn.execute(sys.argv[2]).fetchall()]
except Exception as exc:
    print(json.dumps({"error": str(exc)}, ensure_ascii=False))
    raise SystemExit(1)
print(json.dumps(rows, ensure_ascii=False, default=str))
PY
}

# Langfuse Score 回读（容器内经 n-langfuse-web:3000/api/public，Basic 认证；
# 凭据经 exec env 传入，不落命令行日志。4.76.0 events_only 查询路由为
# v3：/api/public/v3/scores）。输出两行：HTTP 状态 + body。
langfuse_scores_fetch() { # langfuse_scores_fetch <score-name>
  compose exec -T -e LF_AUTH="$LANGFUSE_AUTH" n-agent python - "$1" <<'PY'
import os, sys, urllib.error, urllib.parse, urllib.request

url = ("http://n-langfuse-web:3000/api/public/v3/scores?name="
       + urllib.parse.quote(sys.argv[1]) + "&limit=100")
request = urllib.request.Request(
    url, headers={"Authorization": "Basic " + os.environ["LF_AUTH"]}
)
try:
    with urllib.request.urlopen(request, timeout=15) as response:
        print(response.status)
        print(response.read().decode())
except urllib.error.HTTPError as exc:
    print(exc.code)
    print(exc.read().decode())
PY
}

split_lf_output() { # split_lf_output <raw two-line output>
  LF_STATUS="${1%%$'\n'*}"
  LF_BODY="${1#*$'\n'}"
}

# Loki 查询（观测栈 Prometheus 容器内转发；Loki 服务名 loki:3100）
loki_query_raw() { # loki_query_raw <logql> <since>
  local url
  url="$(LOKI_Q="$1" LOKI_SINCE="$2" python3 -c '
import os, urllib.parse
print("http://loki:3100/loki/api/v1/query_range?" + urllib.parse.urlencode({
    "query": os.environ["LOKI_Q"], "since": os.environ["LOKI_SINCE"],
    "limit": "1000"}))')"
  obs_compose exec -T prometheus wget -q -T 20 -O - "$url"
}

# Prometheus 查询（容器内转发）
prom_sum() { # prom_sum <promql> -> float (0 when absent)
  local url raw
  url="$(PROM_Q="$1" python3 -c '
import os, urllib.parse
print("http://localhost:9090/api/v1/query?" + urllib.parse.urlencode({"query": os.environ["PROM_Q"]}))')"
  if ! raw="$(obs_compose exec -T prometheus wget -q -T 15 -O - "$url" 2>/dev/null)"; then
    echo "0"
    return
  fi
  PROM_PY_INPUT="$raw" python3 - <<'PY'
import json, os

try:
    data = json.loads(os.environ["PROM_PY_INPUT"])
    total = sum(
        float(series["value"][1])
        for series in data.get("data", {}).get("result", [])
        if series.get("value") and len(series["value"]) > 1
    )
except Exception:
    total = 0.0
print(total)
PY
}

# ---------------------------------------------------------------------------
# fake provider fixture（与 otel_smoke 同款：确定性 calculator 两轮）
# ---------------------------------------------------------------------------

install_fake_llm() {
  compose exec -T n-agent sh -c "cat > $FAKE_LLM_PATH" <<'PYEOF'
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9999
FINAL_TEXT = "EVAL-E2E calculator verified: 1+1=2"

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *args):
        pass
    def _reply(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        messages = payload.get("messages") or []
        has_tool = any(m.get("role") == "tool" for m in messages)
        if has_tool:
            self._reply({
                "id": "chatcmpl-eval-e2e", "object": "chat.completion", "created": 0,
                "model": payload.get("model", ""),
                "choices": [{"index": 0, "finish_reason": "stop", "delta": {},
                             "message": {"role": "assistant", "content": FINAL_TEXT}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60,
                          "prompt_tokens_details": {"cached_tokens": 0}},
            })
        else:
            self._reply({
                "id": "chatcmpl-eval-e2e", "object": "chat.completion", "created": 0,
                "model": payload.get("model", ""),
                "choices": [{"index": 0, "finish_reason": "tool_calls", "delta": {},
                             "message": {"role": "assistant", "content": None,
                                         "tool_calls": [{"id": "call-1", "type": "function",
                                                         "function": {"name": "calculator",
                                                                      "arguments": "{\"expression\": \"1+1\"}"}}]}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                          "prompt_tokens_details": {"cached_tokens": 0}},
            })

if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
PYEOF
  compose exec -T -d n-agent python "$FAKE_LLM_PATH" "$FAKE_PORT" || fail "fake LLM start failed"
  sleep 1
}

stop_fake_llm() {
  compose exec -T n-agent pkill -f "$FAKE_LLM_PATH" >/dev/null 2>&1 || true
}

record_original_provider() {
  http GET "$BASE_URL/chat/providers"
  assert_status 200 "list providers"
  ORIG_PROVIDER_ID="$(json_field '[.[] | select(.is_active and (.name | startswith("eval-e2e-") | not))][0].id // ""')"
}

install_fake_provider() {
  http POST "$BASE_URL/chat/providers" -H "Content-Type: application/json" \
    -d "$(jq -n --arg name "$FAKE_PROVIDER_NAME" --arg model "$FAKE_MODEL" \
      '{name:$name, base_url:"http://127.0.0.1:'"$FAKE_PORT"'/v1", model:$model, api_key:"eval-e2e", provider_type:"openai-compatible", supports_vision:false}')"
  assert_status 200 "create fake provider"
  FAKE_PROVIDER_ID="$(json_field '.id')"
  [ -n "$FAKE_PROVIDER_ID" ] && [ "$FAKE_PROVIDER_ID" != "null" ] || fail "fake provider id missing"
  http POST "$BASE_URL/chat/providers/$FAKE_PROVIDER_ID/activate"
  assert_status 200 "activate fake provider"
}

restore_provider() {
  [ -n "$FAKE_PROVIDER_ID" ] || return 0
  if [ -n "$ORIG_PROVIDER_ID" ]; then
    local i
    for i in $(seq 1 30); do
      http POST "$BASE_URL/chat/providers/$ORIG_PROVIDER_ID/activate"
      [ "$HTTP_STATUS" = "200" ] && break
      sleep 2
    done
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
# chat driver（SSE + 工具审批，与 otel_smoke 同款）
# ---------------------------------------------------------------------------

create_session() {
  http POST "$BASE_URL/chat/sessions?session_id=$1"
  assert_status 200 "create session $1"
}

delete_session() {
  http DELETE "$BASE_URL/chat/sessions/$1" || true
}

chat_turn() { # chat_turn <session> <content> -> CHAT_FINISH
  local session="$1" content="$2"
  local payload
  payload="$(jq -n --arg c "$content" --arg model "$FAKE_MODEL" \
    '{model:$model, messages:[{role:"user", content:$c}], stream:true}')"
  curl -sS --max-time 90 -N -X POST "$BASE_URL/chat/completions" \
    -H "Content-Type: application/json" -H "X-Session-ID: $session" \
    -d "$payload" > "$TMP_DIR/sse.txt" 2>/dev/null || fail "chat turn failed for $session"
  local confirmation
  confirmation="$(grep -o '"confirmation_id": *"[^"]*"' "$TMP_DIR/sse.txt" | head -1 | sed 's/.*: *"//; s/"$//')"
  if [ -n "$confirmation" ]; then
    http POST "$BASE_URL/chat/tool-approvals/$confirmation" \
      -H "Content-Type: application/json" -H "X-Session-ID: $session" \
      -d '{"choice":"once"}' || true
  fi
  CHAT_FINISH="$(SSE_FILE="$TMP_DIR/sse.txt" python3 - <<'PY'
import json, os

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

# ---------------------------------------------------------------------------
# 评估定义注册（容器内 CLI 运行时组合根；宿主不启动业务）
# ---------------------------------------------------------------------------

register_definition() { # register_definition <definition_id> <method> [metric_name] [target_kind] [judge_model]
  compose exec -T n-agent python - "$1" "$2" "${3:-}" "${4:-}" "${5:-}" <<'PY'
import asyncio, sys

definition_id, method = sys.argv[1], sys.argv[2]
metric_name = sys.argv[3] or definition_id
target_kind = sys.argv[4] or ("agent_run" if method == "llm_judge" else "retrieval")
# llm_judge 定义的 judge_model 必须是 Judge provider 的真实模型名（#8 冻结
# 定义权威：judge 调用以 definition.judge_model 为准，占位名会 400）
judge_model = sys.argv[5] or None
common = dict(
    definition_id=definition_id, version="v1", metric_name=metric_name,
    target_kind=target_kind,
    method=method, required_fields=(),
    evidence_policy="snapshot",
    output_type="number", range_or_categories=(0.0, 1.0),
    direction="higher_is_better", material_hash={},
    judge_model=judge_model,
    judge_params={}, timeout_seconds=60, budget=None, ci_admissible=False,
)

async def main():
    from app.main import build_evaluation_services
    from app.domain.evaluation.models import EvaluatorDefinition

    runtime = build_evaluation_services()
    await runtime.service.register_definition(EvaluatorDefinition(**common))

asyncio.run(main())
PY
  [ $? -eq 0 ] || fail "register definition $1 failed"
}

# ---------------------------------------------------------------------------
# cleanup / environment restore
# ---------------------------------------------------------------------------

EVALUATION_OVERRIDE=""
ORIGINAL_FILES=""

cleanup() {
  local result=$?
  set +e
  if [ -f "$REPORT_DIR/report.json" ]; then
    python3 "$SCRIPT_DIR/result_report.py" "$REPORT_DIR" finish "$TRACE_PAGE_URL" "$result" || result=1
  fi
  restore_provider
  stop_fake_llm
  [ -z "$EVALUATION_OVERRIDE" ] || rm -rf "$(dirname "$EVALUATION_OVERRIDE")" 2>/dev/null || true
  if [ -n "$ORIGINAL_FILES" ]; then
    # 环境恢复：按原 COMPOSE_FILE 重建（不残留评估 override 注入）
    (cd "$DOCKER_DIR" && COMPOSE_FILE="$ORIGINAL_FILES" \
      docker compose up -d --force-recreate --no-deps --wait n-agent >/dev/null 2>&1) \
      || { echo "FAIL: evaluation env restore failed" >&2; result=1; }
  fi
  # Langfuse 栈恢复（best-effort）：fault 用例中途失败退出时栈处于停机态，
  # 不恢复会污染后续任何依赖 Score 投递的运行（幂等，已运行时无害）
  sh "$DOCKER_DIR/langfuse/restart.sh" start >/dev/null 2>&1 \
    || { echo "FAIL: langfuse stack restore failed" >&2; result=1; }
  rm -rf "$TMP_DIR" 2>/dev/null || true
  set -e
  return "$result"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

command -v jq >/dev/null 2>&1 || fail "eval e2e requires host jq"
command -v python3 >/dev/null 2>&1 || fail "eval e2e requires host python3"

mkdir -p "$REPORT_DIR"
report_case init-evaluation "$RUN_TAG"

# judge 凭据（宿主 secrets 文件；值不回显）
SECRETS_FILE="$REPO_ROOT/locals/e2e-secrets.env"
read_secret() {
  awk -v n="$1" 'index($0, n "=") == 1 { sub(/\r$/, ""); print substr($0, length(n) + 2); exit }' "$SECRETS_FILE"
}
JUDGE_BASE_URL=""
JUDGE_API_KEY=""
JUDGE_MODEL=""
if [ -f "$SECRETS_FILE" ]; then
  JUDGE_BASE_URL="$(read_secret N_AGENT_E2E_JUDGE_BASE_URL)"
  JUDGE_API_KEY="$(read_secret N_AGENT_E2E_JUDGE_API_KEY)"
  JUDGE_MODEL="$(read_secret N_AGENT_E2E_JUDGE_MODEL)"
fi
[ -n "$JUDGE_BASE_URL" ] && [ -n "$JUDGE_API_KEY" ] && [ -n "$JUDGE_MODEL" ] \
  || fail "locals/e2e-secrets.env 缺少 N_AGENT_E2E_JUDGE_*（评估 E2E 需要真实 Judge）"

# Langfuse 项目凭据（docker/langfuse/.env；值不回显）
LF_PUBLIC_KEY="$(awk -F= '/^LANGFUSE_INIT_PROJECT_PUBLIC_KEY=/{print $2; exit}' "$DOCKER_DIR/langfuse/.env" | tr -d '\r')"
LF_SECRET_KEY="$(awk -F= '/^LANGFUSE_INIT_PROJECT_SECRET_KEY=/{print $2; exit}' "$DOCKER_DIR/langfuse/.env" | tr -d '\r')"
[ -n "$LF_PUBLIC_KEY" ] && [ -n "$LF_SECRET_KEY" ] || fail "docker/langfuse/.env 缺少 LANGFUSE_INIT_PROJECT_*"
LANGFUSE_AUTH="$(printf '%s:%s' "$LF_PUBLIC_KEY" "$LF_SECRET_KEY" | base64)"

# compose override：注入评估 B 类 env（凭据只进 override 文件，不回显）
ORIGINAL_FILES="${COMPOSE_FILE:-}"
OVERRIDE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/eval-e2e-env.XXXXXX")"
EVALUATION_OVERRIDE="$OVERRIDE_DIR/override.json"
printf '{"services":{"n-agent":{"environment":{"N_AGENT_EVALUATION_ENABLED":"true","N_AGENT_EVALUATION_PRODUCER_TOKEN":"%s","N_AGENT_EVALUATION_LANGFUSE_BASE_URL":"http://n-langfuse-web:3000","N_AGENT_EVALUATION_LANGFUSE_PUBLIC_KEY":"%s","N_AGENT_EVALUATION_LANGFUSE_SECRET_KEY":"%s","N_AGENT_EVALUATION_LANGFUSE_PROJECT":"n-agent-local-project","N_AGENT_EVALUATION_JUDGE_PROVIDER_BASE_URL":"%s","N_AGENT_EVALUATION_JUDGE_PROVIDER_API_KEY":"%s","N_AGENT_EVALUATION_JUDGE_PROVIDER_MODEL":"%s","N_AGENT_EVALUATION_DAILY_CALL_CEILING":"100000"}}}}\n' \
  "$PRODUCER_TOKEN" "$LF_PUBLIC_KEY" "$LF_SECRET_KEY" \
  "$JUDGE_BASE_URL" "$JUDGE_API_KEY" "$JUDGE_MODEL" > "$EVALUATION_OVERRIDE"
export COMPOSE_FILE="${ORIGINAL_FILES:+$ORIGINAL_FILES:}$EVALUATION_OVERRIDE"

if [ "${N_AGENT_E2E_REBUILT:-0}" != "1" ]; then
  (cd "$DOCKER_DIR" && bash ./restart.sh)
fi
# 账本基线重置（host 侧 bind-mount locals）：定义不可变（同版本内容冲突
# 拒绝注册），历史运行残留的旧版定义（如占位 judge_model）会与当前脚本
# 注册内容冲突且持续产生失败作业；E2E 账本是套件自有测试数据，每轮从
# 干净基线开始保证确定性。先删文件再 force-recreate（旧容器随重建停止）。
INSTALL_ROOT="$(awk -F= '/^N_AGENT_INSTALL_ROOT=/{print $2; exit}' "$DOCKER_DIR/.env" | tr -d '\r')"
if [ -n "$INSTALL_ROOT" ] && [ -f "$INSTALL_ROOT/locals/evaluations.db" ]; then
  rm -f "$INSTALL_ROOT/locals/evaluations.db"
fi
compose up -d --force-recreate --no-deps --wait n-agent || fail "evaluation env recreate failed"
wait_health "$HEALTH_ATTEMPTS"

# 自检 + Trace 页面链接（报告用）
http GET "$BASE_URL/chat/observations/otel"
assert_status 200 "otel self-check"
TRACE_PAGE_URL="$(json_field '.links.Trace // ""')"

# C 类配置：开捕获 + 全采样（运行时 CAS）
http GET "$BASE_URL/chat/evaluations/config"
assert_status 200 "evaluations config GET"
CONFIG_VERSION="$(json_field '.version')"
http PATCH "$BASE_URL/chat/evaluations/config" -H "Content-Type: application/json" \
  -d "$(jq -n --argjson v "$CONFIG_VERSION" \
    '{patch:{evaluation_enabled:true, evaluation_sampling_rate:1.0}, expected_version:$v}')"
assert_status 200 "evaluations config PATCH"

# 定义复用（#1 预设注册）：线上 Judge 与 deterministic 指标（agent.answer_
# relevance / kb.recall_at_5）由启动期 preset_definitions 幂等注册（judge_
# model 取组合根 Judge Provider 配置，即本套件注入的真实模型），E2E 不再
# 重复注册（同 id+版本不同内容会触发定义不可变冲突）。仅 replay 评分定义
# 需显式注册：按 T13 合同为 experiment_item kind（REPLAY_SOURCE_KIND_MAP：
# agent_run 源 -> experiment_item 评分定义），metric_name 与线上 Judge 相
# 同（Judge rubric 按 metric_name 解析，Langfuse Score 名保持连续）。
register_definition "$METRIC_JUDGE-replay" "llm_judge" "$METRIC_JUDGE" "experiment_item" "$JUDGE_MODEL"

# fake provider（业务对话驱动）
install_fake_llm
record_original_provider
install_fake_provider

# ---------------------------------------------------------------------------
# Case 1: reconciliation —— 固定 3 已知样本三元对账（AC11 / AC10 / AC07）
# ---------------------------------------------------------------------------
report_case begin reconciliation
RECON_SEED="$(utc_iso)"
LOKI_SINCE="2h"

for INDEX in 1 2 3; do
  SESSION="eval-e2e-recon-$INDEX-$RUN_TAG"
  create_session "$SESSION"
  if [ "$INDEX" = "2" ]; then
    chat_turn "$SESSION" "e2e-eval-fixed-$INDEX api_key=$SENTINEL use the calculator tool for 1+1 then answer"
  else
    chat_turn "$SESSION" "e2e-eval-fixed-$INDEX use the calculator tool for 1+1 then answer"
  fi
  [ "$CHAT_FINISH" = "stop" ] || fail "recon chat turn $INDEX did not finish (finish=$CHAT_FINISH)"
done

# 等捕获样本 -> job -> Judge -> 结果 -> 投递（本地账本腿）
RECON_READY=0
COUNT=0
RECON_ROWS="[]"
for i in $(seq 1 "$RESULT_WAIT_ATTEMPTS"); do
  RECON_ROWS="$(ledger_query "SELECT r.result_id, r.typed_value_json, r.provenance, d.sink AS sink, d.status AS delivery_status, d.payload_json FROM evaluation_results r LEFT JOIN evaluation_deliveries d ON d.result_id = r.result_id WHERE r.metric_name = '$METRIC_JUDGE' AND r.status = 'value' AND r.created_at >= '$RECON_SEED'")"
  COUNT="$(json_len RECON_ROWS)"
  if [ "$COUNT" -ge 3 ]; then
    RECON_READY=1
    break
  fi
  sleep 2
done
[ "$RECON_READY" = "1" ] || fail "3 个 Judge 结果未在 $((RESULT_WAIT_ATTEMPTS * 2))s 内就绪（captured=${COUNT}）"
echo "[eval-e2e] reconciliation local results:"
echo "$RECON_ROWS" | python3 -m json.tool

# 等投递冻结并到达终态（verified/unlinked）：accepted/pending 尚未完成
# 回读核验或尚未投递，不能对账（payload 未冻结 -> 无 external_id 可查）；
# otel 行例外（#13 supports_readback=False，accepted 即投递成功终态）
DELIVERY_READY=0
NONTERMINAL="-1"
for i in $(seq 1 "$SCORE_WAIT_ATTEMPTS"); do
  RECON_ROWS="$(ledger_query "SELECT r.result_id, r.typed_value_json, r.provenance, d.sink AS sink, d.status AS delivery_status, d.payload_json FROM evaluation_results r LEFT JOIN evaluation_deliveries d ON d.result_id = r.result_id WHERE r.metric_name = '$METRIC_JUDGE' AND r.status = 'value' AND r.created_at >= '$RECON_SEED'")"
  NONTERMINAL="$(ROWS_JSON="$RECON_ROWS" python3 -c 'import json, os; rows = json.loads(os.environ["ROWS_JSON"]); print(len([r for r in rows if not r.get("payload_json") or (r.get("delivery_status") not in ("verified", "unlinked") and not (r.get("delivery_status") == "accepted" and r.get("sink") == "otel"))]))' 2>/dev/null || echo 99)"
  if [ "$NONTERMINAL" = "0" ]; then
    DELIVERY_READY=1
    break
  fi
  sleep 2
done
[ "$DELIVERY_READY" = "1" ] || fail "投递未在 $((SCORE_WAIT_ATTEMPTS * 2))s 内到达终态（未冻结/未核验行数=${NONTERMINAL}）"

# Langfuse Score 回读（第二条腿）：按冻结 payload 的 external_id 集
# 精确对账（run-scoped，不受历史运行残留 Score 影响）
EXPECTED_IDS="$(ROWS_JSON="$RECON_ROWS" python3 -c 'import json, os; rows = json.loads(os.environ["ROWS_JSON"]); print(" ".join(sorted((json.loads(r.get("payload_json") or "{}") or {}).get("id") or "?" for r in rows if r.get("sink") == "langfuse")))')"
LF_SCORES_OK=0
LF_MATCHED=0
LF_BODY="{}"
for i in $(seq 1 "$SCORE_WAIT_ATTEMPTS"); do
  LF_OUT="$(langfuse_scores_fetch "$SCORE_NAME_JUDGE")"
  split_lf_output "$LF_OUT"
  if [ "$LF_STATUS" = "200" ]; then
    LF_MATCHED="$(LF_BODY_JSON="$LF_BODY" EXPECTED="$EXPECTED_IDS" python3 -c 'import json, os; ids = set(os.environ["EXPECTED"].split()); data = (json.loads(os.environ["LF_BODY_JSON"]) or {}).get("data") or []; print(len([s for s in data if s.get("id") in ids]))' 2>/dev/null || echo 0)"
    if [ "$LF_MATCHED" -ge 3 ]; then
      LF_SCORES_OK=1
      break
    fi
  fi
  sleep 2
done
[ "$LF_SCORES_OK" = "1" ] || fail "Langfuse Score 未全部匹配（matched=${LF_MATCHED}/3 status=${LF_STATUS}）"

# Loki 事件回读（第三条腿：gen_ai.evaluation.result 按结果身份对账；
# Collector 批量刷写存在延迟，有界等待）
LOKI_RAW=""
LOKI_MATCHED=0
LOKI_OK=0
for i in $(seq 1 "$SCORE_WAIT_ATTEMPTS"); do
  LOKI_RAW="$(loki_query_raw '{service_name="n-agent"} |~ "gen_ai[.]evaluation[.]result"' "$LOKI_SINCE")"
  LOKI_MATCHED="$(ROWS_JSON="$RECON_ROWS" LOKI_RAW="$LOKI_RAW" python3 -c 'import json, os; rows = json.loads(os.environ["ROWS_JSON"]); raw = os.environ["LOKI_RAW"]; print(len([r for r in rows if r["result_id"] in raw]))' 2>/dev/null || echo 0)"
  if [ "$LOKI_MATCHED" -ge 3 ]; then
    LOKI_OK=1
    break
  fi
  sleep 2
done
[ "$LOKI_OK" = "1" ] || fail "Loki 评估事件未全部可见（matched=${LOKI_MATCHED}/3）"

# 三元对账表 + 差额分类（linked / pending / unlinked；superseded 单独计数）
RECON_TABLE="$(ROWS_JSON="$RECON_ROWS" LF_BODY_JSON="$LF_BODY" LOKI_RAW="$LOKI_RAW" python3 - <<'PY'
import json, os

rows = json.loads(os.environ["ROWS_JSON"])
lf_scores = (json.loads(os.environ["LF_BODY_JSON"]) or {}).get("data") or []
loki_raw = os.environ["LOKI_RAW"]
print("| result_id | value | 投递分类 | Langfuse Score | Loki 事件 |")
print("|---|---|---|---|---|")
for row in rows:
    rid = row["result_id"]
    delivery = (row.get("delivery_status") or "missing").lower()
    if row.get("sink") == "otel":
        # OTel 腿（Loki 消费）：accepted 即终态（#13），无 Langfuse Score
        otel_status = "otel-accepted" if delivery == "accepted" else f"otel-{delivery}"
        loki_ok = rid in loki_raw
        print(f'| {rid} | {row["typed_value_json"]} | {otel_status} | n/a(otel) | {"value-present" if loki_ok else "MISSING"} |')
        continue
    if delivery == "verified":
        classification = "linked"
    elif delivery in ("pending", "retry_wait"):
        classification = "pending"
    elif delivery == "unlinked":
        classification = "unlinked"
    else:
        classification = delivery
    # 4.76.0 v3 条目无 comment：按冻结 payload 的 external_id 对账
    # （external_id = UUIDv5(project + result_id + projection_version)，
    # 与 Langfuse 回读条目的 id 精确匹配）
    try:
        score_id = (json.loads(row.get("payload_json") or "{}") or {}).get("id")
    except ValueError:
        score_id = None
    score = next((s for s in lf_scores if s.get("id") == score_id), None)
    score_cell = (f'id={str(score.get("id"))[:12]} value={score.get("value")}'
                  if score else "MISSING")
    loki_cell = "value-present" if rid in loki_raw else "MISSING"
    print(f'| {rid} | {row["typed_value_json"]} | {classification} | {score_cell} | {loki_cell} |')
PY
)"
echo "[eval-e2e] AC11 三元对账表："
echo "$RECON_TABLE"
MISSING_COUNT="$(printf '%s' "$RECON_TABLE" | grep -c "MISSING" || true)"
[ "$MISSING_COUNT" -eq 0 ] || fail "三元对账存在 MISSING 项（${MISSING_COUNT}）"

# 差额分类汇总（superseded 为复核修订链路：本套件预期 0，如实计数）
SUPERSEDED_COUNT="$(ledger_query "SELECT COUNT(*) AS n FROM evaluation_results WHERE supersedes_result_id IS NOT NULL" | python3 -c 'import json, sys; print(json.load(sys.stdin)[0]["n"])')"
echo "[eval-e2e] 差额分类: linked/pending/unlinked 见对账表; superseded=$SUPERSEDED_COUNT"

# 敏感哨兵：凭据形态不出现在 Loki 事件、v3 回读响应与冻结投递 payload
# （4.76.0 v3 回读无 comment 字段，Score comment 哨兵改查冻结 payload）
if printf '%s' "$LOKI_RAW" | grep -q "$SENTINEL"; then
  fail "credential sentinel leaked via Loki evaluation events"
fi
if printf '%s' "$LF_BODY" | grep -q "$SENTINEL"; then
  fail "credential sentinel leaked via Langfuse scores"
fi
FROZEN_PAYLOADS="$(ledger_query "SELECT payload_json FROM evaluation_deliveries WHERE payload_json IS NOT NULL AND created_at >= '$RECON_SEED'")"
if printf '%s' "$FROZEN_PAYLOADS" | grep -q "$SENTINEL"; then
  fail "credential sentinel leaked via frozen delivery payloads (score comment)"
fi

# 24h current Gauge 与 current 视图一致（AC11 Gauge 腿；账本跨运行持久，用 >=）
GAUGE_OK=0
GAUGE_VALUE="0"
for i in $(seq 1 30); do
  GAUGE_VALUE="$(prom_sum 'sum(nagent_evaluation_window_succeeded_ratio)')"
  if python3 -c "import sys; sys.exit(0 if float('$GAUGE_VALUE' or 0) >= 3 else 1)"; then
    GAUGE_OK=1
    break
  fi
  sleep 2
done
[ "$GAUGE_OK" = "1" ] || fail "24h Gauge 未反映 current 视图（succeeded=$GAUGE_VALUE < 3）"
echo "[eval-e2e] nagent_evaluation_window_succeeded_ratio=$GAUGE_VALUE"
report_case pass reconciliation

# ---------------------------------------------------------------------------
# Case 2: experiment —— evaluate run/replay 容器内真实执行（AC09 / AC14）
# ---------------------------------------------------------------------------
report_case begin experiment
# 固定种子数据集（仓库 evaluations/datasets/kb-smoke-v1）拷入容器账本侧
compose exec -T n-agent mkdir -p /app/locals/eval-datasets || fail "dataset dir create failed"
compose cp "$REPO_ROOT/evaluations/datasets/kb-smoke-v1" "n-agent:/app/locals/eval-datasets/" \
  || fail "dataset copy into container failed"

EXP_RUN_OUTPUT="$(compose exec -T n-agent n-agent evaluate run \
  --dataset /app/locals/eval-datasets/kb-smoke-v1/manifest.json \
  --name "e2e-exp-$RUN_TAG" --evaluator "$METRIC_DETERMINISTIC@v1" \
  --replicates 1 --json 2>&1)" \
  || { echo "$EXP_RUN_OUTPUT" | tail -8 >&2; fail "evaluate run failed"; }
echo "[eval-e2e] evaluate run output:"
echo "$EXP_RUN_OUTPUT"
EXP_RUN_ID="$(printf '%s' "$EXP_RUN_OUTPUT" | jq -r '.experiment_id // empty' 2>/dev/null || true)"
[ -n "$EXP_RUN_ID" ] || fail "evaluate run 未返回 experiment_id"
# 诚实标注：N-KB 检索目标尚未实现 evaluation_context（T19 未实施），
# kb.* 打分预期无样本，实验编排与检索目标直调真实可见（未完成项如实标注）
KB_SCORED="$(ledger_query "SELECT COUNT(*) AS n FROM evaluation_results r JOIN evaluation_jobs j ON r.job_id = j.job_id WHERE j.experiment_id = '$EXP_RUN_ID' AND r.status = 'value'")"
KB_SCORED_N="$(KB_SCORED_JSON="$KB_SCORED" python3 -c 'import json, os; print(json.loads(os.environ["KB_SCORED_JSON"])[0]["n"])')"
echo "[eval-e2e] AC09 execute 实验 $EXP_RUN_ID 已创建并执行；kb.* 已打分样本数=${KB_SCORED_N}（T19 未实施：N-KB 无 evaluation_context，预期 0，如实标注未完成）"

# replay：在线捕获样本的快照重评分（不调业务目标；evaluator 重放 Judge）
REPLAY_SAMPLE_ID="$(ledger_query "SELECT j.sample_id FROM evaluation_jobs j JOIN evaluation_results r ON r.job_id = j.job_id WHERE r.metric_name = '$METRIC_JUDGE' AND r.status = 'value' LIMIT 1" | python3 -c 'import json, sys; print(json.load(sys.stdin)[0]["sample_id"])')"
REPLAY_HASH="$(printf 'e2e-replay-ds-%s' "$RUN_TAG" | shasum -a 256 | awk '{print $1}')"
EXP_REPLAY_OUTPUT="$(compose exec -T n-agent n-agent evaluate replay \
  --name "e2e-replay-$RUN_TAG" --source-sample-ids "$REPLAY_SAMPLE_ID" \
  --evaluator "$METRIC_JUDGE-replay@v1" --dataset-id e2e-replay-ds \
  --dataset-version v1 --dataset-hash "$REPLAY_HASH" \
  --json 2>&1)" \
  || { echo "$EXP_REPLAY_OUTPUT" | tail -8 >&2; fail "evaluate replay failed"; }
echo "[eval-e2e] evaluate replay output:"
echo "$EXP_REPLAY_OUTPUT"
EXP_REPLAY_ID="$(printf '%s' "$EXP_REPLAY_OUTPUT" | jq -r '.experiment_id // empty' 2>/dev/null || true)"
[ -n "$EXP_REPLAY_ID" ] || fail "evaluate replay 未返回 experiment_id"
# 评分 job 由后台 worker 异步领取执行，有界等待已评分结果
REPLAY_ROWS="[]"
REPLAY_COUNT=0
for i in $(seq 1 "$SCORE_WAIT_ATTEMPTS"); do
  REPLAY_ROWS="$(ledger_query "SELECT r.result_id, r.typed_value_json FROM evaluation_results r JOIN evaluation_jobs j ON r.job_id = j.job_id WHERE j.experiment_id = '$EXP_REPLAY_ID' AND r.status = 'value'")"
  REPLAY_COUNT="$(json_len REPLAY_ROWS)"
  if [ "$REPLAY_COUNT" -ge 1 ]; then
    break
  fi
  sleep 2
done
[ "$REPLAY_COUNT" -ge 1 ] || fail "replay 实验未在 $((SCORE_WAIT_ATTEMPTS * 2))s 内产出已评分结果（rows=${REPLAY_ROWS}）"
echo "[eval-e2e] replay 实验已评分结果数=$REPLAY_COUNT"
report_case pass experiment

# ---------------------------------------------------------------------------
# Case 3: fault —— Langfuse 停机业务继续 + 恢复补发 verified（AC12）
# ---------------------------------------------------------------------------
report_case begin fault
lf_stack stop || fail "langfuse stack stop failed"
sleep 2
wait_health 30
http GET "$BASE_URL/health"
assert_status 200 "business health during langfuse outage"
FAULT_SEED="$(utc_iso)"
create_session "eval-e2e-fault-$RUN_TAG"
chat_turn "eval-e2e-fault-$RUN_TAG" "use the calculator tool for 1+1 then answer"
[ "$CHAT_FINISH" = "stop" ] || fail "business chat turn degraded during langfuse outage"

# 捕获样本 -> Judge -> 结果 -> 投递均为异步链路，有界等待等待补发投递
# （Langfuse 停机期间投递不可 verified，应停留 pending/retry_wait）
FAULT_DELIVERY="[]"
FAULT_UNVERIFIED=0
for i in $(seq 1 "$SCORE_WAIT_ATTEMPTS"); do
  FAULT_DELIVERY="$(ledger_query "SELECT d.delivery_id, d.status FROM evaluation_deliveries d JOIN evaluation_results r ON d.result_id = r.result_id WHERE r.metric_name = '$METRIC_JUDGE' AND r.created_at >= '$FAULT_SEED' AND d.sink = 'langfuse'")"
  FAULT_UNVERIFIED="$(FAULT_DELIVERY_JSON="$FAULT_DELIVERY" python3 -c '
import json, os
rows = json.loads(os.environ["FAULT_DELIVERY_JSON"])
print(len([r for r in rows if r["status"] not in ("verified", "unlinked")]))' 2>/dev/null || echo 0)"
  if [ "$FAULT_UNVERIFIED" -ge 1 ]; then
    break
  fi
  sleep 2
done
[ "$FAULT_UNVERIFIED" -ge 1 ] || fail "故障窗口内未产生等待补发的 delivery（rows=${FAULT_DELIVERY}）"
echo "[eval-e2e] outage-window deliveries: $FAULT_DELIVERY"

FAULT_DELIVERY_ID="$(FAULT_DELIVERY_JSON="$FAULT_DELIVERY" python3 -c '
import json, os
rows = json.loads(os.environ["FAULT_DELIVERY_JSON"])
print(next(r["delivery_id"] for r in rows if r["status"] not in ("verified", "unlinked")))')"

lf_stack start || fail "langfuse stack start failed"
FAULT_VERIFIED=0
STATUS=""
for i in $(seq 1 "$DELIVERY_WAIT_ATTEMPTS"); do
  ROW="$(ledger_query "SELECT status FROM evaluation_deliveries WHERE delivery_id = '$FAULT_DELIVERY_ID'")"
  STATUS="$(ROW_JSON="$ROW" python3 -c 'import json, os; rows = json.loads(os.environ["ROW_JSON"]); print(rows[0]["status"] if rows else "")' 2>/dev/null || echo "")"
  if [ "$STATUS" = "verified" ]; then
    FAULT_VERIFIED=1
    break
  fi
  sleep 2
done
[ "$FAULT_VERIFIED" = "1" ] || fail "恢复后 delivery 未 verified（id=$FAULT_DELIVERY_ID status=${STATUS}）"
echo "[eval-e2e] fault delivery recovered verified: $FAULT_DELIVERY_ID"
report_case pass fault

# ---------------------------------------------------------------------------
# Case 4: unlinked —— producer 边界矩阵（AC08 / AC03 / AC19 / 401）
# ---------------------------------------------------------------------------
report_case begin unlinked
# (1) 错误认证：producer 凭据错误 401（通道保持，不泄露配置状态）
http POST "$BASE_URL/evaluation/producer/messages" -H "Content-Type: application/json" \
  -H "Authorization: Bearer wrong-token" -d '{"type":"evaluation.sample.v1"}'
assert_status 401 "producer wrong credential must 401"

# (2) ACK 丢失重放幂等：同 producer_event_id 重提交 -> 同 sample_id 复用（AC03）
HASH64="$(printf 'a%.0s' $(seq 1 64))"
CAPTURED_AT="$(utc_iso)"
PRODUCER_PAYLOAD_OK="$(cat <<JSON
{"type":"evaluation.sample.v1","schema_version":"evaluation.sample.v1",
 "producer_event_id":"${FIXED_EVENTS[0]}","service":"n-kb",
 "retrieval_id":"e2e-eval-retrieval-1-$RUN_TAG","captured_at":"$CAPTURED_AT",
 "query":"producer boundary probe e2e-eval-fixed-1",
 "results":[{"document_id":"doc-1-$RUN_TAG","document_revision":"r1","chunk_id":"c1",
   "chunk_content_hash":"$HASH64","rank":1}],
 "corpus_revision":"e2e-eval-corpus-1","trace_ref":null,
 "integrity":{"redaction_version":"redaction.v1"}}
JSON
)"
http POST "$BASE_URL/evaluation/producer/messages" -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PRODUCER_TOKEN" -d "$PRODUCER_PAYLOAD_OK"
assert_status 202 "producer first submit"
FIRST_SAMPLE_ID="$(json_field '.sample_id')"
[ -n "$FIRST_SAMPLE_ID" ] && [ "$FIRST_SAMPLE_ID" != "null" ] || fail "producer submit missing sample_id"
http POST "$BASE_URL/evaluation/producer/messages" -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PRODUCER_TOKEN" -d "$PRODUCER_PAYLOAD_OK"
assert_status 202 "producer resubmit"
[ "$(json_field '.sample_id')" = "$FIRST_SAMPLE_ID" ] || fail "同 producer_event_id 重提交未幂等复用"

# (3) 未采样/无 Trace：人评（无 trace/session 身份）-> delivery unlinked（AC08）
create_session "eval-e2e-unlinked-$RUN_TAG"
chat_turn "eval-e2e-unlinked-$RUN_TAG" "use the calculator tool for 1+1 then answer"
[ "$CHAT_FINISH" = "stop" ] || fail "unlinked-case chat turn failed"
http POST "$BASE_URL/chat/evaluations/feedback" -H "Content-Type: application/json" \
  -d '{"target":{"service":"n-agent","kind":"agent_run","subject_id":"eval-e2e-unlinked-run","subject_revision":"stop"},"definition_id":"feedback.helpful","definition_version":"v1","value":true}'
assert_status 200 "feedback submit"
UNLINKED_RESULT_ID="$(json_field '.result_id')"
[ -n "$UNLINKED_RESULT_ID" ] && [ "$UNLINKED_RESULT_ID" != "null" ] || fail "feedback submit missing result_id"
UNLINKED_DELIVERY=0
UNLINKED_ROWS="[]"
for i in $(seq 1 60); do
  UNLINKED_ROWS="$(ledger_query "SELECT status FROM evaluation_deliveries WHERE result_id = '$UNLINKED_RESULT_ID'")"
  UNLINKED_DELIVERY="$(UNLINKED_ROWS_JSON="$UNLINKED_ROWS" python3 -c '
import json, os
rows = json.loads(os.environ["UNLINKED_ROWS_JSON"])
print(1 if any(r["status"] == "unlinked" for r in rows) else 0)' 2>/dev/null || echo 0)"
  [ "$UNLINKED_DELIVERY" = "1" ] && break
  sleep 2
done
[ "$UNLINKED_DELIVERY" = "1" ] || fail "无 Trace 人评结果未产生 unlinked delivery（rows=${UNLINKED_ROWS}）"

# (4) 迟到样本携带已删文档版本 -> 409 deletion_conflict（AC19 竞态面）
DELETE_PAYLOAD="$(cat <<JSON
{"type":"evaluation.target-deleted.v1","schema_version":"evaluation.target-deleted.v1",
 "service":"n-kb","document_id":"doc-del-1-$RUN_TAG","document_revision":"r1","reason":"e2e"}
JSON
)"
http POST "$BASE_URL/evaluation/producer/messages" -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PRODUCER_TOKEN" -d "$DELETE_PAYLOAD"
assert_status 202 "target-deleted tombstone write"
LATE_PAYLOAD="$(cat <<JSON
{"type":"evaluation.sample.v1","schema_version":"evaluation.sample.v1",
 "producer_event_id":"e2e-eval-late-1-$RUN_TAG","service":"n-kb",
 "retrieval_id":"e2e-eval-retrieval-late-$RUN_TAG","captured_at":"$(utc_iso)",
 "query":"late sample after deletion",
 "results":[{"document_id":"doc-del-1-$RUN_TAG","document_revision":"r1","chunk_id":"c1",
   "chunk_content_hash":"$HASH64","rank":1}],
 "corpus_revision":"e2e-eval-corpus-1","trace_ref":null,
 "integrity":{"redaction_version":"redaction.v1"}}
JSON
)"
http POST "$BASE_URL/evaluation/producer/messages" -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PRODUCER_TOKEN" -d "$LATE_PAYLOAD"
assert_status 409 "late sample with deleted document version must 409"
echo "$HTTP_BODY" | grep -q "deletion_conflict" || fail "409 body missing deletion_conflict code"
report_case pass unlinked

echo "[eval-e2e] PASS"

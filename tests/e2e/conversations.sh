#!/usr/bin/env bash
# 会话数据集 E2E 宿主编排：重建服务 -> 解析容器挂载 -> 容器内执行 Runner。
# 容器内仓库路径不固定，必须用 docker inspect 实际 Mounts 解析，不硬编码。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd -P)"
# 容器名由 docker/ 下 compose 项目名（n-agent）与服务名（n-agent）派生
CONTAINER_NAME="n-agent-n-agent-1"
DEFAULT_BASE_URL="http://127.0.0.1:8201"
SECRET_NAMES=(
  N_AGENT_E2E_BROWSER_PASSWORD
  N_AGENT_E2E_JUDGE_BASE_URL
  N_AGENT_E2E_JUDGE_API_KEY
  N_AGENT_E2E_JUDGE_MODEL
)

usage() {
  cat >&2 <<'EOF'
usage: tests/e2e/conversations.sh [--base-url URL] [--only id1,id2] [--keep]
       tests/e2e/conversations.sh --cleanup-only <宿主 manifest 路径>
EOF
}

fail() {
  echo "conversations.sh: $*" >&2
  exit 1
}

# 目录 realpath（解析符号链接；macOS 无 GNU realpath -e 语义差异，统一用 pwd -P）
resolve_dir() {
  [ -d "$1" ] || fail "目录不存在: $1"
  (cd "$1" && pwd -P)
}

# 文件 realpath：父目录 pwd -P + basename
resolve_file() {
  [ -e "$1" ] || fail "文件不存在: $1"
  local dir base
  dir="$(dirname "$1")"
  base="$(basename "$1")"
  printf '%s/%s\n' "$(cd "$dir" && pwd -P)" "$base"
}

# ---- 参数解析（互斥按参数是否出现判断，不看值是否为空）----
MODE="replay"
BASE_URL="$DEFAULT_BASE_URL"
ONLY_VALUE=""
SEEN_BASE_URL=0
SEEN_ONLY=0
SEEN_KEEP=0
SEEN_CLEANUP=0
MANIFEST_HOST=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --base-url)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      BASE_URL="$2"; SEEN_BASE_URL=1; shift 2 ;;
    --only)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      ONLY_VALUE="$2"; SEEN_ONLY=1; shift 2 ;;
    --keep)
      SEEN_KEEP=1; shift ;;
    --cleanup-only)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      MODE="cleanup"; MANIFEST_HOST="$2"; SEEN_CLEANUP=1; shift 2 ;;
    *)
      usage; exit 2 ;;
  esac
done

if [ "$SEEN_CLEANUP" -eq 1 ] && { [ "$SEEN_ONLY" -eq 1 ] || [ "$SEEN_KEEP" -eq 1 ] || [ "$SEEN_BASE_URL" -eq 1 ]; }; then
  echo "conversations.sh: --cleanup-only 与 --base-url/--only/--keep 互斥" >&2
  exit 2
fi

# ---- 回放模式：先重建服务（run.sh 已重建则跳过），成功后再 inspect ----
if [ "$MODE" = "replay" ] && [ "${N_AGENT_E2E_REBUILT:-0}" != "1" ]; then
  (cd "$REPO_ROOT/docker" && bash ./restart.sh)
fi

MOUNTS_JSON="$(docker inspect "$CONTAINER_NAME" --format '{{json .Mounts}}')" \
  || fail "docker inspect 失败：容器 $CONTAINER_NAME 不可用"

# ---- 宿主路径 realpath ----
REPO_REAL="$(resolve_dir "$REPO_ROOT")"
REPORTS_REAL=""
if [ "$MODE" = "replay" ]; then
  # 缺失报告目录先在合法宿主父路径（realpath 后的仓库 locals）下创建
  mkdir -p "$REPO_REAL/locals/e2e-reports"
  REPORTS_REAL="$(resolve_dir "$REPO_REAL/locals/e2e-reports")"
else
  MANIFEST_HOST="$(resolve_file "$MANIFEST_HOST")"
  [ -f "$MANIFEST_HOST" ] || fail "清单不是普通文件: $MANIFEST_HOST"
  # 清单必须位于报告根（宿主仓库 locals/e2e-reports）内且不逃逸
  REPORT_ROOT_HOST="$(resolve_dir "$REPO_REAL/locals/e2e-reports")"
  case "$MANIFEST_HOST/" in
    "$REPORT_ROOT_HOST"/*) ;;
    *) fail "清单不在报告根内: $MANIFEST_HOST" ;;
  esac
fi

# ---- Mount 选择（路径组件包含，重叠最长匹配）+ Source->Destination 转换 ----
map_path() { # $1=宿主 realpath, $2=需要可写(1/0) -> stdout 容器路径
  MOUNTS_JSON="$MOUNTS_JSON" HOST_PATH="$1" NEED_RW="$2" python3 - <<'PY'
import json
import os
import sys

mounts = json.loads(os.environ["MOUNTS_JSON"])
host_path = os.environ["HOST_PATH"]
need_rw = os.environ["NEED_RW"] == "1"

best = None
for mount in mounts:
    if mount.get("Type") != "bind":
        continue
    source = mount.get("Source") or ""
    if not source:
        continue
    source = source.rstrip("/") or "/"
    # 路径组件包含：等于挂载源，或以其加分隔符为前缀
    if host_path != source and not host_path.startswith(source + "/"):
        continue
    if best is None or len(source) > len(best["Source"].rstrip("/") or "/"):
        best = mount

if best is None:
    print(f"conversations.sh: 无挂载覆盖宿主路径: {host_path}", file=sys.stderr)
    sys.exit(1)
if need_rw and not best.get("RW", False):
    print(f"conversations.sh: 覆盖 {host_path} 的挂载为只读", file=sys.stderr)
    sys.exit(1)

source = best["Source"].rstrip("/") or "/"
dest = best["Destination"].rstrip("/") or "/"
suffix = host_path[len(source):].lstrip("/")
print(f"{dest}/{suffix}" if suffix else dest)
PY
}

CONTAINER_REPO="$(map_path "$REPO_REAL" 0)"
CONTAINER_MANIFEST=""
if [ "$MODE" = "replay" ]; then
  # 报告目录单独选 Mount：不能默认 <container_repo>/locals 与宿主 locals 同挂载
  CONTAINER_REPORTS="$(map_path "$REPORTS_REAL" 1)"
else
  CONTAINER_MANIFEST="$(map_path "$MANIFEST_HOST" 0)"
fi

# ---- 容器内可读/可写核验 ----
container_test() { # $1=test 标志(-d/-r/-w), $2=容器路径, $3=描述
  docker exec "$CONTAINER_NAME" test "$1" "$2" \
    || fail "容器内$3检查失败: test $1 $2"
}

container_test -d "$CONTAINER_REPO" "仓库目录"
container_test -r "$CONTAINER_REPO" "仓库目录"
if [ "$MODE" = "replay" ]; then
  container_test -d "$CONTAINER_REPORTS" "报告目录"
  container_test -w "$CONTAINER_REPORTS" "报告目录"
else
  container_test -r "$CONTAINER_MANIFEST" "清单文件"
fi

# ---- cleanup-only 在密钥加载之前分流；回放模式加载可选密钥 ----
EXEC_ENV=()
if [ "$MODE" = "replay" ]; then
  SECRETS_FILE="$REPO_REAL/locals/e2e-secrets.env"
  if [ -f "$SECRETS_FILE" ]; then
    for name in "${SECRET_NAMES[@]}"; do
      # 只提取明确白名单变量，不点加载整个文件，值不回显
      # awk 首个匹配即退出（避免 head+pipefail 的 SIGPIPE 理论失败；BSD sed 不支持 {p;q} 内联）
      value="$(awk -v n="$name" 'index($0, n "=") == 1 { sub(/\r$/, ""); print substr($0, length(n) + 2); exit }' "$SECRETS_FILE")"
      if [ -n "$value" ]; then
        export "$name=$value"
      fi
    done
  fi
  for name in "${SECRET_NAMES[@]}"; do
    if [ -n "${!name:-}" ]; then
      EXEC_ENV+=(-e "$name")
    fi
  done
fi

# ---- 独立 argv 数组执行 Runner，透传退出码 ----
if [ "$MODE" = "replay" ]; then
  RUNNER_ARGS=(
    --dataset-dir "$CONTAINER_REPO/tests/e2e/conversations"
    --report-dir "$CONTAINER_REPORTS"
    --base-url "$BASE_URL"
  )
  [ "$SEEN_ONLY" -eq 1 ] && RUNNER_ARGS+=(--only "$ONLY_VALUE")
  [ "$SEEN_KEEP" -eq 1 ] && RUNNER_ARGS+=(--keep)
else
  RUNNER_ARGS=(--cleanup-only "$CONTAINER_MANIFEST")
fi

docker exec ${EXEC_ENV[@]+"${EXEC_ENV[@]}"} "$CONTAINER_NAME" \
  python "$CONTAINER_REPO/tests/e2e/conversations_runner.py" \
  ${RUNNER_ARGS[@]+"${RUNNER_ARGS[@]}"}

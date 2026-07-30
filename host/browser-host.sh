#!/usr/bin/env bash
# Browser Host Bridge 启停脚本
#
# 管理 `n-agent browser-host` 前台宿主进程：后台化运行、PID 跟踪、健康轮询、优雅退出。
# 沿用 docker/restart.sh 的脚本风格与 host.docker.internal 本机桥接路径。
#
# 用法: ./host/browser-host.sh {start|stop|status|check|restart}
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# 探测与容器共享的 locals 根目录：
#   1. LOCALS_ROOT 环境变量；2. docker-compose.yml 中 /app/locals 的挂载源；3. REPO_ROOT/locals
# browser-host 必须与容器读写同一个 sessions.db / token，故默认从 compose 探测实际挂载路径，
# 避免 code/install 分离部署时脚本指向错误的 locals 副本。
detect_locals_root() {
  local compose="$REPO_ROOT/docker/docker-compose.yml"
  if [[ -f "$compose" ]]; then
    local src
    src=$(awk '
      /^[[:space:]]*-[[:space:]]*.+:[[:space:]]*\/app\/locals([[:space:]]|$)/ {
        line=$0
        sub(/^[[:space:]]*-[[:space:]]*/,"",line)
        sub(/:[[:space:]]*\/app\/locals.*/,"",line)
        gsub(/^[ \t]+|[ \t]+$/,"",line)
        print line
        exit
      }
    ' "$compose")
    if [[ -n "$src" ]]; then
      src="${src/#\~/$HOME}"
      echo "$src"
      return 0
    fi
  fi
  echo "$REPO_ROOT/locals"
}

# --- 可配置路径（环境变量覆盖） ---
# token/sqlite/profile/pid 与容器共享，基于 LOCALS_ROOT；log 为宿主本地产物。
LOCALS_ROOT="${LOCALS_ROOT:-$(detect_locals_root)}"
TOKEN_PATH="${BROWSER_HOST_TOKEN_PATH:-$LOCALS_ROOT/host-browser.token}"
SQLITE_PATH="${BROWSER_HOST_SQLITE_PATH:-$LOCALS_ROOT/sessions.db}"
PROFILE_ROOT="${BROWSER_HOST_PROFILE_ROOT:-$LOCALS_ROOT/browser-host-profiles}"
CHROME_EXECUTABLE="${BROWSER_HOST_CHROME_EXECUTABLE:-}"
PORT="${BROWSER_HOST_PORT:-8766}"
PID_FILE="${BROWSER_HOST_PID_FILE:-$LOCALS_ROOT/browser-host.pid}"
LOG_FILE="${BROWSER_HOST_LOG_FILE:-$REPO_ROOT/logs/browser-host.log}"

# --- 运行参数 ---
START_HEALTH_ATTEMPTS="${BROWSER_HOST_START_HEALTH_ATTEMPTS:-15}"
STOP_GRACE_SECONDS="${BROWSER_HOST_STOP_GRACE_SECONDS:-10}"
HEALTH_URL="http://127.0.0.1:${PORT}/healthz"
CURL_WAIT_ARGS=(--connect-timeout 1 --max-time 2)

# --- n-agent 可执行解析（填充全局 N_AGENT_CMD 数组） ---
resolve_n_agent() {
  if [[ -x "$REPO_ROOT/.venv/bin/n-agent" ]]; then
    N_AGENT_CMD=("$REPO_ROOT/.venv/bin/n-agent")
    return 0
  fi
  local found
  found="$(command -v n-agent 2>/dev/null || true)"
  if [[ -n "$found" && -x "$found" ]]; then
    N_AGENT_CMD=("$found")
    return 0
  fi
  # Fallback: .venv 有 python 与 app 包但未生成 n-agent entry point 时，
  # 直接以模块方式调用（uv pip install 装依赖但未 -e . 装项目时的常见坑）。
  # 显式注入 PYTHONPATH=REPO_ROOT，使任意 cwd 下都能 import app。
  if [[ -x "$REPO_ROOT/.venv/bin/python" ]] \
     && PYTHONPATH="$REPO_ROOT" "$REPO_ROOT/.venv/bin/python" -c "import app.interfaces.cli" >/dev/null 2>&1; then
    N_AGENT_CMD=("$REPO_ROOT/.venv/bin/python" -m app.interfaces.cli)
    export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"
    return 0
  fi
  echo "error: n-agent not found; install with 'uv pip install -e .' or add n-agent to PATH" >&2
  return 1
}

# --- 构造 browser-host 公共参数（写入全局 HOST_ARGS） ---
build_host_args() {
  HOST_ARGS=(
    --token-path "$TOKEN_PATH"
    --sqlite-path "$SQLITE_PATH"
    --profile-root "$PROFILE_ROOT"
    --port "$PORT"
  )
  if [[ -n "$CHROME_EXECUTABLE" ]]; then
    HOST_ARGS+=(--chrome-executable "$CHROME_EXECUTABLE")
  fi
}

# --- profile root 前置准备：0700、owner 拥有 ---
prepare_profile_root() {
  mkdir -p "$PROFILE_ROOT"
  chmod 0700 "$PROFILE_ROOT"
}

# --- token 缺失友好提示 ---
ensure_token_present() {
  if [[ ! -f "$TOKEN_PATH" ]]; then
    echo "error: token file not found: $TOKEN_PATH" >&2
    echo "create it with: openssl rand -hex 32 > \"$TOKEN_PATH\" && chmod 600 \"$TOKEN_PATH\"" >&2
    return 1
  fi
}

# --- PID 工具 ---
is_running() {
  local pid="$1"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  # 确认进程确实是 browser-host，避免 PID 复用误杀
  ps -p "$pid" -o command= 2>/dev/null | grep -q "browser-host" || return 1
}

read_pid() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 1
  echo "$pid"
}

# --- 健康检查 ---
health_ok() {
  curl -fsS "${CURL_WAIT_ARGS[@]}" "$HEALTH_URL" >/dev/null 2>&1
}

wait_health() {
  local label="$1" attempts="$2"
  for attempt in $(seq 1 "$attempts"); do
    if health_ok; then
      echo "$label ready"
      return 0
    fi
    echo "$label waiting ($attempt/$attempts)"
    sleep 1
  done
  echo "$label did not become ready after $attempts attempts" >&2
  return 1
}

# --- 子命令 ---

cmd_check() {
  resolve_n_agent || return 1
  ensure_token_present || return 1
  prepare_profile_root
  build_host_args
  "${N_AGENT_CMD[@]}" browser-host "${HOST_ARGS[@]}" --check
}

cmd_start() {
  resolve_n_agent || return 1

  # 已在运行则直接返回
  local pid
  if pid="$(read_pid)" && is_running "$pid"; then
    echo "browser-host already running (pid $pid)"
    return 0
  fi
  rm -f "$PID_FILE"

  # 前置依赖检查
  echo "checking dependencies..."
  ensure_token_present || return 1
  build_host_args
  if ! "${N_AGENT_CMD[@]}" browser-host "${HOST_ARGS[@]}" --check; then
    echo "error: dependency check failed; not starting" >&2
    return 1
  fi

  prepare_profile_root
  mkdir -p "$(dirname "$LOG_FILE")"

  # 后台启动（覆盖本次日志，便于排查）
  echo "starting browser-host on 127.0.0.1:${PORT}..."
  nohup "${N_AGENT_CMD[@]}" browser-host "${HOST_ARGS[@]}" >"$LOG_FILE" 2>&1 &
  local new_pid=$!
  echo "$new_pid" >"$PID_FILE"

  if wait_health "browser-host health" "$START_HEALTH_ATTEMPTS"; then
    echo "browser-host started (pid $new_pid); log: $LOG_FILE"
    return 0
  fi

  echo "error: browser-host did not become healthy" >&2
  if is_running "$new_pid"; then
    kill -TERM "$new_pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  echo "--- recent log ---" >&2
  tail -n 20 "$LOG_FILE" 2>/dev/null >&2 || true
  return 1
}

cmd_stop() {
  local pid
  if ! pid="$(read_pid)" || ! is_running "$pid"; then
    echo "browser-host not running"
    rm -f "$PID_FILE"
    return 0
  fi

  echo "stopping browser-host (pid $pid)..."
  kill -TERM "$pid" 2>/dev/null || true

  for _ in $(seq 1 "$STOP_GRACE_SECONDS"); do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "browser-host stopped"
      rm -f "$PID_FILE"
      return 0
    fi
    sleep 1
  done

  echo "browser-host did not exit after ${STOP_GRACE_SECONDS}s; sending SIGKILL" >&2
  kill -KILL "$pid" 2>/dev/null || true
  sleep 1
  rm -f "$PID_FILE"
  echo "browser-host killed"
  return 0
}

cmd_status() {
  local pid
  if pid="$(read_pid)" && is_running "$pid"; then
    echo "browser-host running (pid $pid)"
    if health_ok; then
      echo "health: ok"
      return 0
    fi
    echo "health: unreachable" >&2
    return 1
  fi
  echo "browser-host not running"
  return 1
}

cmd_restart() {
  cmd_stop
  cmd_start
}

usage() {
  cat <<EOF
Usage: $(basename "$0") {start|stop|status|check|restart}

Manage the Browser Host Bridge (n-agent browser-host) on the macOS host.

Subcommands:
  start    Check deps, then launch browser-host in the background.
  stop     Send SIGTERM; escalate to SIGKILL after grace seconds.
  status   Show process and health state.
  check    Run n-agent browser-host --check and exit.
  restart  stop then start.

Config (env overrides):
  LOCALS_ROOT                     Shared locals dir (token/sqlite/profile/pid).
                                  Default: auto-detect from docker-compose.yml, else \$REPO/locals.
  BROWSER_HOST_TOKEN_PATH         default: \$LOCALS_ROOT/host-browser.token
  BROWSER_HOST_SQLITE_PATH        default: \$LOCALS_ROOT/sessions.db
  BROWSER_HOST_PROFILE_ROOT       default: \$LOCALS_ROOT/browser-host-profiles
  BROWSER_HOST_CHROME_EXECUTABLE  default: auto-discover
  BROWSER_HOST_PORT               default: 8766
  BROWSER_HOST_PID_FILE           default: \$LOCALS_ROOT/browser-host.pid
  BROWSER_HOST_LOG_FILE           default: \$REPO/logs/browser-host.log
  BROWSER_HOST_START_HEALTH_ATTEMPTS  default: 15
  BROWSER_HOST_STOP_GRACE_SECONDS     default: 10
EOF
}

main() {
  local sub="${1:-}"
  case "$sub" in
    start) cmd_start ;;
    stop) cmd_stop ;;
    status) cmd_status ;;
    check) cmd_check ;;
    restart) cmd_restart ;;
    ""|-h|--help|help) usage ;;
    *)
      echo "error: unknown subcommand: $sub" >&2
      usage >&2
      exit 2
      ;;
  esac
}

main "$@"

#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    printf '%s\n' 'codex provider: expected canonical repository root' >&2
    exit 64
fi

repo_root=$1
codex_bin=$(command -v codex 2>/dev/null || true)
if [ -z "$codex_bin" ]; then
    printf '%s\n' 'codex provider: codex command is unavailable' >&2
    exit 127
fi

# 期限由框架 runner 的 watchdog 统一执行（HARNESS_THIRD_REVIEW_TIMEOUT_SECONDS，
# 默认 900 秒），provider 不自建超时，避免双重看门狗和两处阈值。exec 后
# runner 的 TERM/KILL 直接作用于 codex 进程本身。
set -- exec --cd "$repo_root" --sandbox workspace-write
if [ -n "${HARNESS_THIRD_REVIEW_MODEL:-}" ]; then
    set -- "$@" --model "$HARNESS_THIRD_REVIEW_MODEL"
fi
exec "$codex_bin" "$@" -

#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    printf '%s\n' 'codex provider: expected canonical repository root' >&2
    exit 64
fi

repo_root=$1

# Codex 二进制解析（用户约束：只允许 ChatGPT 桌面版内置的 Codex Exec，禁用 vscode 扩展插件内置二进制）：
# 1. HARNESS_THIRD_REVIEW_CODEX_BIN 显式指定
# 2. ChatGPT.app 内置 codex（/Applications/ChatGPT.app/Contents/Resources/codex）
# 3. PATH 查找，但拒绝解析到 .vscode/extensions 下的扩展内置二进制
codex_bin=${HARNESS_THIRD_REVIEW_CODEX_BIN:-}
if [ -z "$codex_bin" ] && [ -x "/Applications/ChatGPT.app/Contents/Resources/codex" ]; then
    codex_bin="/Applications/ChatGPT.app/Contents/Resources/codex"
fi
if [ -z "$codex_bin" ]; then
    codex_bin=$(command -v codex 2>/dev/null || true)
    case "$codex_bin" in
        */.vscode/extensions/*)
            printf '%s\n' 'codex provider: vscode extension codex binary is forbidden' >&2
            exit 127
            ;;
    esac
fi
if [ -z "$codex_bin" ] || [ ! -x "$codex_bin" ]; then
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

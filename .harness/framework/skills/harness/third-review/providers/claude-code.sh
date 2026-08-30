#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    printf '%s\n' 'claude-code provider: expected canonical repository root' >&2
    exit 64
fi

repo_root=$1

is_vscode_extension_binary() {
    case $1 in
        */.vscode/extensions/*|\
        */.vscode-server/extensions/*|\
        */.vscode-insiders/extensions/*|\
        */.vscode-server-insiders/extensions/*)
            return 0
            ;;
    esac
    return 1
}

resolve_executable() {
    executable_path=$1
    while [ -L "$executable_path" ]; do
        link_target=$(readlink "$executable_path") || return 1
        case $link_target in
            /*) executable_path=$link_target ;;
            *) executable_path=$(dirname "$executable_path")/$link_target ;;
        esac
    done
    executable_dir=$(CDPATH= cd -P "$(dirname "$executable_path")" 2>/dev/null && pwd -P) || return 1
    printf '%s/%s\n' "$executable_dir" "$(basename "$executable_path")"
}

# Claude Code binary resolution:
# 1. HARNESS_THIRD_REVIEW_CLAUDE_CODE_BIN explicit standalone CLI path
# 2. PATH lookup
# Both the selected path and its symlink target must stay outside VS Code
# extension directories.
claude_bin=${HARNESS_THIRD_REVIEW_CLAUDE_CODE_BIN:-}
if [ -z "$claude_bin" ]; then
    claude_bin=$(command -v claude 2>/dev/null || true)
fi
if [ -z "$claude_bin" ] || [ ! -x "$claude_bin" ]; then
    printf '%s\n' 'claude-code provider: claude command is unavailable' >&2
    exit 127
fi
if is_vscode_extension_binary "$claude_bin"; then
    printf '%s\n' 'claude-code provider: vscode extension claude binary is forbidden' >&2
    exit 127
fi
resolved_claude_bin=$(resolve_executable "$claude_bin") || {
    printf '%s\n' 'claude-code provider: claude command is not canonical' >&2
    exit 127
}
if is_vscode_extension_binary "$resolved_claude_bin"; then
    printf '%s\n' 'claude-code provider: vscode extension claude binary is forbidden' >&2
    exit 127
fi

# Third Review supplies the target/spec paths and the five-field output contract
# in stdin. Match the Codex provider: let the coding agent read and edit the
# target directly instead of translating its changes through a second patch
# protocol. Read/Edit is the complete tool surface; the runner remains the
# authority for target/spec boundary and output validation.
#
# Do not use CLAUDE_CODE_SIMPLE: Claude Code 2.1.251 treats that as bare mode,
# which deliberately skips OAuth/keychain auth. --safe-mode disables project
# customizations while preserving the caller's authenticated Claude session.
unset CLAUDE_CODE_SIMPLE

cd "$repo_root"
set -- \
    --print \
    --output-format text \
    --safe-mode \
    --permission-mode acceptEdits \
    --tools 'Read,Edit' \
    --no-session-persistence \
    --disable-slash-commands \
    --no-chrome \
    --effort low
if [ -n "${HARNESS_THIRD_REVIEW_MODEL:-}" ]; then
    set -- "$@" --model "$HARNESS_THIRD_REVIEW_MODEL"
fi
exec "$resolved_claude_bin" "$@"

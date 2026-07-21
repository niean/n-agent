#!/usr/bin/env sh
set -eu

usage() {
  cat >&2 <<'USAGE'
Usage:
  third-review-codex.sh spec <spec_file>
  third-review-codex.sh plan <plan_file> <spec_file>

Environment:
  HARNESS_THIRD_REVIEW_MODEL   Codex model name for Third Review
  CHATGPT_CODEX_BIN            ChatGPT Mac app bundled Codex executable path
USAGE
}

find_chatgpt_codex_bin() {
  if [ -n "${CHATGPT_CODEX_BIN:-}" ]; then
    if [ -x "$CHATGPT_CODEX_BIN" ]; then
      printf '%s\n' "$CHATGPT_CODEX_BIN"
      return 0
    fi
    echo "third-review: CHATGPT_CODEX_BIN is set but not executable: $CHATGPT_CODEX_BIN" >&2
    return 127
  fi

  chatgpt_codex_bin="/Applications/ChatGPT.app/Contents/Resources/codex"
  if [ -x "$chatgpt_codex_bin" ]; then
    printf '%s\n' "$chatgpt_codex_bin"
    return 0
  fi

  echo "third-review: ChatGPT Mac app bundled Codex executable not found: $chatgpt_codex_bin" >&2
  echo "third-review: install ChatGPT for macOS or set CHATGPT_CODEX_BIN" >&2
  return 127
}

if [ "$#" -lt 2 ]; then
  usage
  exit 2
fi

doc_type="$1"
target_file="$2"
spec_file="${3:-}"

case "$doc_type" in
  spec)
    if [ "$#" -ne 2 ]; then
      usage
      exit 2
    fi
    ;;
  plan)
    if [ "$#" -ne 3 ]; then
      usage
      exit 2
    fi
    ;;
  *)
    usage
    exit 2
    ;;
esac

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$repo_root"

if [ ! -f "$target_file" ]; then
  echo "third-review: target file not found: $target_file" >&2
  exit 1
fi

if [ "$doc_type" = "plan" ] && [ ! -f "$spec_file" ]; then
  echo "third-review: spec file not found: $spec_file" >&2
  exit 1
fi

codex_bin="$(find_chatgpt_codex_bin)"

prompt_template=".harness/framework/third/${doc_type}-review-prompt.md"
if [ ! -f "$prompt_template" ]; then
  echo "third-review: prompt template not found: $prompt_template" >&2
  exit 1
fi

target_basename="$(basename "$target_file")"
session_title="HarnessReview-${target_basename%.md}"

tmp_prompt="$(mktemp "${TMPDIR:-/tmp}/harness-third-review.XXXXXX")"
cleanup() {
  rm -f "$tmp_prompt"
}
trap cleanup EXIT

{
  printf '# %s\n\n' "$session_title"
  printf 'SESSION_TITLE: %s\n' "$session_title"
  printf 'PROJECT_ROOT: %s\n' "$repo_root"
  printf 'RECORD_VISIBILITY: 本次审阅使用 ChatGPT Mac 应用内置的 Codex exec，并持久化为当前项目会话；禁止为审阅记录在 .harness 下新建文件或目录。\n'
  printf '\n---\n\n'
  cat "$prompt_template"
  printf '\n---\n\n'
  printf 'DOC_TYPE: %s\n' "$doc_type"
  printf 'TARGET_FILE: %s\n' "$target_file"
  printf 'REPO_ROOT: %s\n' "$repo_root"
  if [ "$doc_type" = "plan" ]; then
    printf 'SPEC_FILE: %s\n' "$spec_file"
  fi
  cat <<'PROMPT'

请先读取目标文件；plan 审阅还必须读取关联 spec。完成审阅后，直接修改 TARGET_FILE。
最终回复只给出状态、修改数量、修改摘要、剩余风险；修改数量必须是具体数字，禁止编造问题、禁止写成 20+。
PROMPT
} > "$tmp_prompt"

if [ -n "${HARNESS_THIRD_REVIEW_MODEL:-}" ]; then
  "$codex_bin" exec \
    --cd "$repo_root" \
    --sandbox workspace-write \
    --model "$HARNESS_THIRD_REVIEW_MODEL" \
    - < "$tmp_prompt"
else
  "$codex_bin" exec \
    --cd "$repo_root" \
    --sandbox workspace-write \
    - < "$tmp_prompt"
fi

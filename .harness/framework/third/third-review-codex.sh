#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage:
  third-review-codex.sh spec <spec_file>
  third-review-codex.sh plan <plan_file> <spec_file>

Environment:
  HARNESS_THIRD_REVIEW_MODEL   Codex model name for Third Review
  CODEX_BIN                    Codex executable path, overrides auto-detection
USAGE
}

find_codex_bin() {
  if [[ -n "${CODEX_BIN:-}" ]]; then
    if command -v "$CODEX_BIN" >/dev/null 2>&1; then
      command -v "$CODEX_BIN"
      return 0
    fi
    if [[ -x "$CODEX_BIN" ]]; then
      printf '%s\n' "$CODEX_BIN"
      return 0
    fi
    echo "third-review: CODEX_BIN is set but not executable: $CODEX_BIN" >&2
    return 127
  fi

  if command -v codex >/dev/null 2>&1; then
    command -v codex
    return 0
  fi

  local candidate
  while IFS= read -r candidate; do
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done < <(
    find \
      "$HOME/.vscode/extensions" \
      "$HOME/.cursor/extensions" \
      "$HOME/.windsurf/extensions" \
      -path '*openai.chatgpt*/bin/*/codex' \
      -type f 2>/dev/null | sort -r
  )

  echo "third-review: codex executable not found; set CODEX_BIN or install Codex CLI" >&2
  return 127
}

if [[ $# -lt 2 ]]; then
  usage
  exit 2
fi

doc_type="$1"
target_file="$2"
spec_file="${3:-}"

case "$doc_type" in
  spec)
    if [[ $# -ne 2 ]]; then
      usage
      exit 2
    fi
    ;;
  plan)
    if [[ $# -ne 3 ]]; then
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

if [[ ! -f "$target_file" ]]; then
  echo "third-review: target file not found: $target_file" >&2
  exit 1
fi

if [[ "$doc_type" == "plan" && ! -f "$spec_file" ]]; then
  echo "third-review: spec file not found: $spec_file" >&2
  exit 1
fi

codex_bin="$(find_codex_bin)"

model_args=()
if [[ -n "${HARNESS_THIRD_REVIEW_MODEL:-}" ]]; then
  model_args=(--model "$HARNESS_THIRD_REVIEW_MODEL")
fi

prompt_template=".harness/framework/third/${doc_type}-review-prompt.md"
if [[ ! -f "$prompt_template" ]]; then
  echo "third-review: prompt template not found: $prompt_template" >&2
  exit 1
fi

tmp_prompt="$(mktemp "${TMPDIR:-/tmp}/harness-third-review.XXXXXX.md")"
cleanup() {
  rm -f "$tmp_prompt"
}
trap cleanup EXIT

{
  cat "$prompt_template"
  printf '\n---\n\n'
  printf 'DOC_TYPE: %s\n' "$doc_type"
  printf 'TARGET_FILE: %s\n' "$target_file"
  printf 'REPO_ROOT: %s\n' "$repo_root"
  if [[ "$doc_type" == "plan" ]]; then
    printf 'SPEC_FILE: %s\n' "$spec_file"
  fi
  cat <<'PROMPT'

请先读取目标文件；plan 审阅还必须读取关联 spec。完成审阅后，直接修改 TARGET_FILE。
最终回复只给出状态、修改摘要、剩余风险。
PROMPT
} > "$tmp_prompt"

"$codex_bin" exec \
  --cd "$repo_root" \
  --sandbox workspace-write \
  "${model_args[@]}" \
  - < "$tmp_prompt"

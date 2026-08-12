#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# Project-level provider selection. Replace this delegation to use another
# reviewer without changing Workflow definitions or the Third Review contract.
exec sh "$SCRIPT_DIR/../third/third-review-codex.sh" "$@"

#!/usr/bin/env bash
# E2E-only Compose environment. Source, then call e2e_environment MODE SCRIPT ARGS...
# MODE=default disables autonomous skill writes; only dedicated evolution cases
# may request MODE=skill-evolution. No global opt-in is accepted for normal cases.
e2e_environment() {
  local mode="$1" script="$2"
  shift 2
  case "$mode" in
    default|skill-evolution) ;;
    *) echo "Invalid E2E environment mode: $mode" >&2; return 2 ;;
  esac
  if [ -n "${N_AGENT_E2E_ENVIRONMENT:-}" ]; then
    [ "$N_AGENT_E2E_ENVIRONMENT" = "$mode" ] || {
      echo "Run skill-evolution cases in a separate E2E environment" >&2
      return 2
    }
    return 0
  fi
  bash "$(dirname "${BASH_SOURCE[0]}")/environment.sh" "$mode" "$script" "$@"
  exit $?
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  set -euo pipefail
  mode="$1"; script="$2"; shift 2
  root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  # Resolve the deployment's existing Compose selection before adding ours;
  # compose config honors COMPOSE_FILE from the shell and docker/.env.
  original_files="$(cd "$root/docker" && docker compose config --environment | python3 -c '
import os, sys
from pathlib import Path
values = dict(line.rstrip("\n").split("=", 1) for line in sys.stdin if "=" in line)
selection = values.get("COMPOSE_FILE")
separator = values.get("COMPOSE_PATH_SEPARATOR", os.pathsep)
if selection:
    paths = selection.split(separator)
else:
    base = next(p for p in ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml") if Path(p).exists())
    paths = [base]
    stem = base.rsplit(".", 1)[0]
    override = next((p for p in (stem + ".override.yml", stem + ".override.yaml") if Path(p).exists()), None)
    if override:
        paths.append(override)
print(":".join(str(Path(p).resolve()) for p in paths))
')"
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/n-agent-e2e-env.XXXXXX")"
  enabled=false
  [ "$mode" != skill-evolution ] || enabled=true
  # An override (rather than a frozen compose config) lets OTel .env overlays
  # keep taking effect during its container recreations.
  printf '{"services":{"n-agent":{"environment":{"N_AGENT_SKILLS_BACKGROUND_REVIEW_ENABLED":"%s","N_AGENT_SKILLS_CURATOR_ENABLED":"false"}}}}\n' \
    "$enabled" > "$tmp/override.json"
  restore() {
    local result=$?
    trap - EXIT
    # Preserve test failures; restoration failures must fail a successful run.
    if ! (cd "$root/docker" && COMPOSE_FILE="$original_files" \
      docker compose up -d --force-recreate --no-deps --wait n-agent); then
      echo "E2E environment: failed to restore original n-agent deployment" >&2
      [ "$result" -ne 0 ] || result=1
    fi
    rm -rf "$tmp"
    exit "$result"
  }
  trap restore EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM HUP
  export COMPOSE_PATH_SEPARATOR=:
  export COMPOSE_FILE="$original_files:$tmp/override.json"
  export N_AGENT_E2E_ENVIRONMENT="$mode"
  # Never trust a rebuild marker inherited from outside this environment.
  export N_AGENT_E2E_REBUILT=0
  case "$(basename "$script")" in
    run.sh|conversations.sh) ;; # These entry points rebuild after validating args.
    *) (cd "$root/docker" && bash ./restart.sh)
       export N_AGENT_E2E_REBUILT=1 ;;
  esac
  bash "$script" "$@"
fi

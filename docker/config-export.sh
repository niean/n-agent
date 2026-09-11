#!/bin/sh
# docker/config-export.sh -- collect this machine's N-Agent configuration into
# a single migration bundle (plan T6, spec "配置迁移包").
#
# Three layers are collected: host files (two .env files, the parameterised
# compose, policy, tokens, oss.env, workspace skills/plugins), the SQLite
# config tables (via the in-container CLI -- the host never opens the DB) and
# the deployment identity recorded in manifest.json.
#
# Usage: sh docker/config-export.sh [--out <dir>] [--no-secrets] [--no-tokens]
#                                   [--preflight-only]
# Exit codes: 0 ok, 2 argument/format error, 3 runtime dependency or missing
# source file, 5 output location not allowed.
#
# Test seams (they never change the normal path):
#   --preflight-only      argument + output-location validation only
#   N_AGENT_SOURCE_ONLY=1 define the functions without running main, so unit
#                         tests can dot-source and call one of them. A
#                         `--source-only` argument is impossible: passing
#                         positional parameters to `.` is unspecified in POSIX.

set -eu
umask 077

# macOS bsdtar stores a file's extended attributes (com.apple.provenance is set
# on anything Finder or a browser touched) as a sibling AppleDouble member named
# "._<name>". The manifest never lists those, so the importer's preflight
# rejects the bundle as carrying an unlisted file. Suppress them for every tar
# this script creates -- the outer bundle and both inner workspace archives.
COPYFILE_DISABLE=1
export COPYFILE_DISABLE

EXIT_ARGUMENT=2
EXIT_RUNTIME=3
EXIT_OUTPUT=5

BUNDLE_SCHEMA_VERSION=1

_stderr() {
    printf '%s\n' "$*" >&2
}

_fail() {
    _code=$1
    shift
    _stderr "error: $*"
    exit "$_code"
}

# --- location of this script and of its python helper ----------------------

_resolve_script_dir() {
    case "${0:-}" in
        */*)
            _candidate=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P) || _candidate=""
            ;;
        *)
            _candidate=""
            ;;
    esac
    if [ -n "$_candidate" ] && [ -f "$_candidate/config_bundle_files.py" ]; then
        printf '%s\n' "$_candidate"
        return 0
    fi
    # Dot-sourced (unit tests): $0 is the shell, so fall back to the cwd.
    if [ -f "$PWD/docker/config_bundle_files.py" ]; then
        printf '%s\n' "$PWD/docker"
        return 0
    fi
    if [ -f "$PWD/config_bundle_files.py" ]; then
        printf '%s\n' "$PWD"
        return 0
    fi
    return 1
}

SCRIPT_DIR=$(_resolve_script_dir) || {
    printf '%s\n' "error: cannot locate docker/config_bundle_files.py" >&2
    exit 3
}
REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)
HELPER="$SCRIPT_DIR/config_bundle_files.py"

_helper() {
    python3 "$HELPER" "$@"
}

# Reads dotenv text on stdin and writes it back with every credential value
# cleared. Key names matching _API_KEY|_SECRET|_TOKEN|PASSWORD|ACCESS_KEY|
# PRIVATE_KEY are cleared unless they end in _PATH (those are container paths).
_strip_secret_env() {
    _helper strip-secrets
}

# --- output location -------------------------------------------------------

# Normalise to an absolute path, resolving symlinks on the nearest existing
# ancestor so that a link cannot smuggle the output into a tracked directory.
_normalize_path() {
    _path=$1
    case "$_path" in
        /*) ;;
        *) _path="$PWD/$_path" ;;
    esac
    _tail=""
    while :; do
        if [ -d "$_path" ]; then
            _base=$(CDPATH= cd -- "$_path" && pwd -P)
            break
        fi
        _leaf=$(basename -- "$_path")
        if [ -n "$_tail" ]; then
            _tail="$_leaf/$_tail"
        else
            _tail="$_leaf"
        fi
        _parent=$(dirname -- "$_path")
        if [ "$_parent" = "$_path" ]; then
            _base="/"
            break
        fi
        _path=$_parent
    done
    if [ -z "$_tail" ]; then
        printf '%s\n' "$_base"
    elif [ "$_base" = "/" ]; then
        printf '%s\n' "/$_tail"
    else
        printf '%s\n' "$_base/$_tail"
    fi
}

# A path is allowed when it lies outside every git worktree, or when the
# worktree that owns it ignores it. Checked per final path, because a global
# *.tar.gz rule would otherwise let --out .harness through.
_path_is_allowed() {
    _target=$1
    _probe=$_target
    while [ ! -d "$_probe" ]; do
        _next=$(dirname -- "$_probe")
        if [ "$_next" = "$_probe" ]; then
            break
        fi
        _probe=$_next
    done
    if ! _top=$(git -C "$_probe" rev-parse --show-toplevel 2>/dev/null); then
        return 0
    fi
    if git -C "$_top" check-ignore -q -- "$_target" 2>/dev/null; then
        return 0
    fi
    return 1
}

_validate_output_location() {
    command -v git >/dev/null 2>&1 || _fail "$EXIT_OUTPUT" \
        "git is required to prove the output location is not tracked"
    for _candidate_path in "$@"; do
        if ! _path_is_allowed "$_candidate_path"; then
            _fail "$EXIT_OUTPUT" \
                "output path lives in a git worktree and is not git-ignored: $_candidate_path"
        fi
    done
}

# --- staging ---------------------------------------------------------------

STAGING=""
TMP_BUNDLE=""
TMP_INSTALL=""

_cleanup() {
    [ -n "$STAGING" ] && rm -rf "$STAGING"
    [ -n "$TMP_BUNDLE" ] && rm -f "$TMP_BUNDLE"
    [ -n "$TMP_INSTALL" ] && rm -f "$TMP_INSTALL"
    return 0
}

_on_signal() {
    _cleanup
    # An interrupted export never publishes and never reports success.
    _stderr "error: interrupted; nothing was published"
    exit 130
}

_copy_0600() {
    cp "$1" "$2"
    chmod 600 "$2"
}

_require_file() {
    [ -f "$1" ] || _fail "$EXIT_RUNTIME" "missing required source file: $1"
}

_require_dir() {
    [ -d "$1" ] || _fail "$EXIT_RUNTIME" "missing required source directory: $1"
}

# --- main ------------------------------------------------------------------

_usage() {
    cat <<'USAGE'
Usage: sh docker/config-export.sh [--out <dir>] [--no-secrets] [--no-tokens]
                                  [--preflight-only]

  --out <dir>        output directory (default: <repo>/locals/install)
  --no-secrets       skip oss.env and both tokens, clear credential values in
                     both .env files and redact the DB section
  --no-tokens        skip the two host bridge tokens
  --preflight-only   validate arguments and the output location, then exit
USAGE
}

main() {
    # A deployment context inherited from the caller must never win over the
    # one resolved from docker/.env (restart.sh follows the same rule).
    unset N_AGENT_INSTALL_ROOT || true
    unset N_AGENT_CODE_ROOT || true
    unset COMPOSE_FILE || true
    unset COMPOSE_PROJECT_NAME || true
    unset COMPOSE_PROJECT_DIR || true
    unset COMPOSE_ENV_FILES || true

    out_argument=""
    no_secrets=0
    no_tokens=0
    preflight_only=0

    while [ $# -gt 0 ]; do
        case $1 in
            --out)
                [ $# -ge 2 ] || _fail "$EXIT_ARGUMENT" "--out requires a directory"
                out_argument=$2
                shift 2
                ;;
            --out=*)
                out_argument=${1#--out=}
                [ -n "$out_argument" ] || _fail "$EXIT_ARGUMENT" "--out requires a directory"
                shift
                ;;
            --no-secrets)
                no_secrets=1
                no_tokens=1
                shift
                ;;
            --no-tokens)
                no_tokens=1
                shift
                ;;
            --preflight-only)
                preflight_only=1
                shift
                ;;
            -h|--help)
                _usage
                return 0
                ;;
            *)
                _fail "$EXIT_ARGUMENT" "unknown option: $1"
                ;;
        esac
    done

    command -v python3 >/dev/null 2>&1 || _fail "$EXIT_RUNTIME" \
        "python3 is required on the host to parse deployment files"
    _helper check-deps >/dev/null || _fail "$EXIT_RUNTIME" \
        "the host python3 cannot run docker/config_bundle_files.py"

    host_name=$(hostname -s 2>/dev/null || hostname)
    stamp=$(date +%y%m%d-%H%M)
    bundle_name="n-agent-config-${host_name}-${stamp}.tar.gz"

    [ -n "$out_argument" ] || out_argument="$REPO_DIR/locals/install"
    out_dir=$(_normalize_path "$out_argument")
    bundle_path="$out_dir/$bundle_name"
    install_path="$out_dir/install.sh"
    _validate_output_location "$out_dir" "$bundle_path" "$install_path"

    if [ "$preflight_only" -eq 1 ]; then
        printf 'preflight ok\n'
        printf 'output directory: %s\n' "$out_dir"
        printf 'bundle would be: %s\n' "$bundle_path"
        return 0
    fi

    # --- S4: deployment roots ---------------------------------------------
    docker_env="$REPO_DIR/docker/.env"
    compose_file="$REPO_DIR/docker/docker-compose.yml"
    install_script="$REPO_DIR/docker/install.sh"
    [ -f "$docker_env" ] || _fail "$EXIT_ARGUMENT" "missing $docker_env"

    if ! install_root=$(_helper get-env "$docker_env" N_AGENT_INSTALL_ROOT); then
        _fail "$EXIT_ARGUMENT" "N_AGENT_INSTALL_ROOT is not set in docker/.env"
    fi
    if ! code_root=$(_helper get-env "$docker_env" N_AGENT_CODE_ROOT); then
        _fail "$EXIT_ARGUMENT" "N_AGENT_CODE_ROOT is not set in docker/.env"
    fi
    case "$install_root" in
        /*) ;;
        *) _fail "$EXIT_ARGUMENT" "N_AGENT_INSTALL_ROOT must be an absolute path" ;;
    esac
    case "$code_root" in
        /*) ;;
        *) _fail "$EXIT_ARGUMENT" "N_AGENT_CODE_ROOT must be an absolute path" ;;
    esac

    install_env="$install_root/.env"
    policy_file="$install_root/locals/host-terminal-policy.yaml"
    terminal_token="$install_root/locals/host-terminal.token"
    browser_token="$install_root/locals/host-browser.token"
    oss_env="$install_root/secrets/oss.env"
    workspace_root="$install_root/workspace"

    _require_dir "$install_root"
    _require_file "$compose_file"
    _require_file "$install_env"
    _require_file "$policy_file"
    # The bundle is only useful together with the one-command installer, so a
    # missing install.sh is a failed export, not a silently smaller bundle.
    _require_file "$install_script"
    _require_dir "$workspace_root/skills"
    _require_dir "$workspace_root/plugins"
    if [ "$no_tokens" -eq 0 ]; then
        _require_file "$terminal_token"
        _require_file "$browser_token"
    fi
    if [ "$no_secrets" -eq 0 ]; then
        _require_file "$oss_env"
    fi

    # --- runtime dependencies ---------------------------------------------
    command -v docker >/dev/null 2>&1 || _fail "$EXIT_RUNTIME" "docker is not available on PATH"
    command -v tar >/dev/null 2>&1 || _fail "$EXIT_RUNTIME" "tar is not available on PATH"
    command -v shasum >/dev/null 2>&1 || _fail "$EXIT_RUNTIME" "shasum is not available on PATH"

    # Fixed compose context: never depend on the caller's cwd or environment.
    set -- --project-directory "$REPO_DIR/docker" --env-file "$docker_env" -f "$compose_file"
    if ! docker compose "$@" ps --status running --services 2>/dev/null | grep -qx n-agent; then
        _fail "$EXIT_RUNTIME" \
            "the n-agent container is not running; start it first: sh docker/restart.sh"
    fi

    # --- staging -----------------------------------------------------------
    # Arm the traps before creating the directory, not after: plaintext secrets
    # are written into it moments later, and a signal landing in the gap would
    # leave it behind uncollected. _cleanup tolerates an unset STAGING.
    trap _cleanup EXIT
    trap _on_signal INT TERM HUP
    STAGING=$(mktemp -d "${TMPDIR:-/tmp}/n-agent-export.XXXXXX")
    chmod 700 "$STAGING"

    fingerprint_before=$(_helper fingerprint --exclude .backups --exclude .archive \
        "$docker_env" "$compose_file" "$install_env" "$policy_file" \
        "$terminal_token" "$browser_token" "$oss_env" \
        "$workspace_root/skills" "$workspace_root/plugins" "$install_script")

    mkdir -p "$STAGING/env" "$STAGING/locals" "$STAGING/secrets" \
        "$STAGING/workspace" "$STAGING/db"

    if [ "$no_secrets" -eq 1 ]; then
        _strip_secret_env < "$docker_env" > "$STAGING/env/docker.env"
        _strip_secret_env < "$install_env" > "$STAGING/env/install-root.env"
    else
        _copy_0600 "$docker_env" "$STAGING/env/docker.env"
        _copy_0600 "$install_env" "$STAGING/env/install-root.env"
    fi
    chmod 600 "$STAGING/env/docker.env" "$STAGING/env/install-root.env"
    _copy_0600 "$compose_file" "$STAGING/env/docker-compose.yml"
    _copy_0600 "$policy_file" "$STAGING/locals/host-terminal-policy.yaml"
    if [ "$no_tokens" -eq 0 ]; then
        _copy_0600 "$terminal_token" "$STAGING/locals/host-terminal.token"
        _copy_0600 "$browser_token" "$STAGING/locals/host-browser.token"
    fi
    if [ "$no_secrets" -eq 0 ]; then
        _copy_0600 "$oss_env" "$STAGING/secrets/oss.env"
    else
        rmdir "$STAGING/secrets"
    fi

    tar -czf "$STAGING/workspace/skills.tar.gz" -C "$workspace_root" \
        --exclude=.backups --exclude=.archive \
        --exclude=__pycache__ --exclude='*.pyc' skills
    tar -czf "$STAGING/workspace/plugins.tar.gz" -C "$workspace_root" \
        --exclude=.backups --exclude=.archive \
        --exclude=__pycache__ --exclude='*.pyc' plugins
    chmod 600 "$STAGING/workspace/skills.tar.gz" "$STAGING/workspace/plugins.tar.gz"

    cp "$install_script" "$STAGING/install.sh"
    chmod 700 "$STAGING/install.sh"

    # --- S5: DB section ----------------------------------------------------
    if [ "$no_secrets" -eq 1 ]; then
        docker compose "$@" exec -T n-agent n-agent config export --stdout --redact-secrets \
            > "$STAGING/db/config.json" || _fail "$EXIT_RUNTIME" \
            "the in-container config export failed; is the service healthy? sh docker/restart.sh"
    else
        docker compose "$@" exec -T n-agent n-agent config export --stdout \
            > "$STAGING/db/config.json" || _fail "$EXIT_RUNTIME" \
            "the in-container config export failed; is the service healthy? sh docker/restart.sh"
    fi
    chmod 600 "$STAGING/db/config.json"
    [ -s "$STAGING/db/config.json" ] || _fail "$EXIT_RUNTIME" "the DB export produced no output"
    db_summary=$(_helper db-summary "$STAGING/db/config.json") || _fail "$EXIT_RUNTIME" \
        "the DB export is not a usable schema_version=$BUNDLE_SCHEMA_VERSION document"

    # --- consistency seal --------------------------------------------------
    fingerprint_after=$(_helper fingerprint --exclude .backups --exclude .archive \
        "$docker_env" "$compose_file" "$install_env" "$policy_file" \
        "$terminal_token" "$browser_token" "$oss_env" \
        "$workspace_root/skills" "$workspace_root/plugins" "$install_script")
    if [ "$fingerprint_before" != "$fingerprint_after" ]; then
        _fail "$EXIT_RUNTIME" \
            "source files changed during collection; pause config edits and skill/plugin scans, then retry"
    fi

    # --- S6: manifest and packaging ---------------------------------------
    if [ "$no_secrets" -eq 1 ]; then
        redacted_flag=true
    else
        redacted_flag=false
    fi
    _helper manifest "$STAGING" \
        --schema-version "$BUNDLE_SCHEMA_VERSION" \
        --hostname "$host_name" \
        --install-root "$install_root" \
        --code-root "$code_root" \
        --redacted "$redacted_flag"

    mkdir -p "$out_dir"
    chmod 700 "$out_dir"
    TMP_BUNDLE=$(mktemp "$out_dir/.n-agent-config.XXXXXX")
    chmod 600 "$TMP_BUNDLE"
    tar -czf "$TMP_BUNDLE" -C "$STAGING" .
    chmod 600 "$TMP_BUNDLE"

    # Atomic, non-clobbering publish: link fails when the name already exists,
    # so a second export in the same minute never overwrites the first bundle.
    if [ -e "$bundle_path" ]; then
        _fail "$EXIT_RUNTIME" "refusing to overwrite an existing bundle: $bundle_path"
    fi
    ln "$TMP_BUNDLE" "$bundle_path" 2>/dev/null || _fail "$EXIT_RUNTIME" \
        "refusing to overwrite an existing bundle: $bundle_path"
    rm -f "$TMP_BUNDLE"
    TMP_BUNDLE=""
    chmod 600 "$bundle_path"

    # install.sh next to the bundle: keep an identical copy, replace a
    # different one atomically, never leave a half-written file behind.
    if [ -f "$install_path" ] && cmp -s "$install_script" "$install_path"; then
        chmod 700 "$install_path"
    else
        TMP_INSTALL=$(mktemp "$out_dir/.install.XXXXXX")
        cp "$install_script" "$TMP_INSTALL"
        chmod 700 "$TMP_INSTALL"
        mv -f "$TMP_INSTALL" "$install_path"
        TMP_INSTALL=""
    fi

    # Two steps on purpose: piping wc into tr would make the pipeline report
    # tr's status, hiding a failed wc behind an empty size.
    bundle_size=$(wc -c < "$bundle_path")
    bundle_size=$(printf '%s' "$bundle_size" | tr -d ' ')

    # --- S7: report and warnings ------------------------------------------
    printf 'bundle:   %s\n' "$bundle_path"
    printf 'size:     %s bytes (0600)\n' "$bundle_size"
    printf 'install:  %s (0700)\n' "$install_path"
    printf 'source:   hostname=%s install_root=%s code_root=%s\n' \
        "$host_name" "$install_root" "$code_root"
    printf 'redacted: %s\n' "$redacted_flag"
    printf 'db sections:\n'
    printf '%s\n' "$db_summary" | sed 's/^/  /'
    printf '\n'
    printf 'on the new machine, copy both files and run:\n'
    printf '  sh install.sh %s\n' "$bundle_name"

    if [ "$no_secrets" -eq 1 ]; then
        _stderr "warning: --no-secrets only strips the agreed structured credential fields"
        _stderr "warning: workspace files, URLs, command/args and free text may still embed credentials"
        _stderr "warning: do not treat this bundle as credential-free"
    else
        _stderr "warning: this bundle carries PLAINTEXT secrets (env values, tokens, oss.env, DB api keys)"
        _stderr "warning: keep it at 0600, transfer it over a trusted channel and delete it after the migration"
    fi
    return 0
}

if [ -z "${N_AGENT_SOURCE_ONLY:-}" ]; then
    main "$@"
fi

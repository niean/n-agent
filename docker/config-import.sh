#!/bin/sh
# docker/config-import.sh -- restore an N-Agent configuration bundle produced
# by docker/config-export.sh onto this machine (plan T7, spec "配置迁移包").
#
# Three layers land in a fixed order: the bundle is validated while it is
# still an archive, the host files are merged into <install-root> and
# docker/.env, and the SQLite config tables are imported by the in-container
# CLI. Nothing carried inside the bundle is ever executed -- not even the
# install.sh packed next to it, which exists for a human to read.
#
# Usage: sh docker/config-import.sh <bundle.tar.gz>
#            [--install-root <dir>] [--code-root <dir>]
#            [--mode merge|overwrite]
#            [--schedules-enabled|--schedules-disabled]
#            [--dry-run] [--preflight-only] [--host-only]
#
# Exit codes: 0 ok, 1 some records failed (the rest were applied), 2
# bundle/argument format error (target untouched), 3 runtime dependency or
# start failure, 4 a read-only mount source is occupied by a directory.
#
# Test seams (they never change the normal path):
#   --preflight-only  validate the bundle and the arguments, write nothing
#   --host-only       stop after the host files have landed, never call docker
#   N_AGENT_BUNDLE_MAX_MEMBERS / N_AGENT_BUNDLE_MAX_FILE_BYTES /
#   N_AGENT_BUNDLE_MAX_TOTAL_BYTES  tighten the preflight quotas (they can
#                     only ever be lowered below the spec hard caps)

set -eu
umask 077

EXIT_PARTIAL=1
EXIT_ARGUMENT=2
EXIT_RUNTIME=3
EXIT_MOUNT=4

KB_NETWORK=n-kb_default

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

# --- scratch space ---------------------------------------------------------

WORK=""
MAINTENANCE=0
WINDOW_USED=0
MAINTENANCE_KEY=N_AGENT_MIGRATION_MAINTENANCE

_cleanup() {
    # The maintenance switch is ours, not the user's: it must never survive
    # this process, however this process ends.
    if [ "$MAINTENANCE" -eq 1 ]; then
        MAINTENANCE=0
        _helper edit-env "$REPO_DIR/docker/.env" "$MAINTENANCE_KEY" >/dev/null 2>&1 || true
    fi
    if [ -n "$WORK" ] && [ -d "$WORK" ]; then
        rm -rf "$WORK"
    fi
}

_on_signal() {
    _cleanup
    exit "$EXIT_RUNTIME"
}

trap _cleanup EXIT
trap _on_signal INT TERM HUP

# --- small utilities -------------------------------------------------------

_is_absolute() {
    case "$1" in
        /*) return 0 ;;
        *) return 1 ;;
    esac
}

# True when $1 is $2 or lives below it. Purely lexical on normalised text: it
# is used to keep the code root out of the scanned workspace.
_is_below() {
    case "$1" in
        "$2") return 0 ;;
        "$2"/*) return 0 ;;
        *) return 1 ;;
    esac
}

_usage() {
    sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'
}

# --- main ------------------------------------------------------------------

main() {
    # A caller-inherited deployment context must never leak in: every value
    # this script uses comes from the arguments, docker/.env or the bundle.
    unset N_AGENT_INSTALL_ROOT N_AGENT_CODE_ROOT COMPOSE_FILE \
        COMPOSE_PROJECT_NAME COMPOSE_PROJECT_DIR COMPOSE_ENV_FILES 2>/dev/null || true

    bundle=""
    install_root_arg=""
    code_root_arg=""
    mode="merge"
    schedules=""
    dry_run=0
    preflight_only=0
    host_only=0

    while [ $# -gt 0 ]; do
        case "$1" in
            --install-root)
                [ $# -ge 2 ] || _fail "$EXIT_ARGUMENT" "--install-root needs a value"
                install_root_arg=$2
                shift 2
                ;;
            --code-root)
                [ $# -ge 2 ] || _fail "$EXIT_ARGUMENT" "--code-root needs a value"
                code_root_arg=$2
                shift 2
                ;;
            --mode)
                [ $# -ge 2 ] || _fail "$EXIT_ARGUMENT" "--mode needs a value"
                mode=$2
                shift 2
                ;;
            --schedules-enabled)
                [ "$schedules" = "disabled" ] && _fail "$EXIT_ARGUMENT" \
                    "--schedules-enabled and --schedules-disabled are mutually exclusive"
                schedules=enabled
                shift
                ;;
            --schedules-disabled)
                [ "$schedules" = "enabled" ] && _fail "$EXIT_ARGUMENT" \
                    "--schedules-enabled and --schedules-disabled are mutually exclusive"
                schedules=disabled
                shift
                ;;
            --dry-run)
                dry_run=1
                shift
                ;;
            --preflight-only)
                preflight_only=1
                shift
                ;;
            --host-only)
                host_only=1
                shift
                ;;
            -h|--help)
                _usage
                return 0
                ;;
            --*)
                _fail "$EXIT_ARGUMENT" "unknown option: $1"
                ;;
            *)
                [ -z "$bundle" ] || _fail "$EXIT_ARGUMENT" "only one bundle may be given"
                bundle=$1
                shift
                ;;
        esac
    done

    [ -n "$bundle" ] || _fail "$EXIT_ARGUMENT" "a bundle path is required"
    case "$mode" in
        merge|overwrite) ;;
        *) _fail "$EXIT_ARGUMENT" "--mode must be merge or overwrite, got: $mode" ;;
    esac
    if [ -n "$install_root_arg" ] && ! _is_absolute "$install_root_arg"; then
        _fail "$EXIT_ARGUMENT" "--install-root must be an absolute path"
    fi
    if [ -n "$code_root_arg" ] && ! _is_absolute "$code_root_arg"; then
        _fail "$EXIT_ARGUMENT" "--code-root must be an absolute path"
    fi

    # --- S3: validate the archive before anything else --------------------
    if ! _helper check-deps; then
        _fail "$EXIT_RUNTIME" "python3 with a usable standard library is required"
    fi
    if summary=$(_helper preflight "$bundle"); then
        :
    else
        exit "$EXIT_ARGUMENT"
    fi

    # --- S3: resolve the two deployment roots -----------------------------
    default_install_root="${HOME:-/root}/install/n-agent"
    default_code_root="${HOME:-/root}/code"
    docker_env="$REPO_DIR/docker/.env"

    configured_install_root=""
    configured_code_root=""
    # --preflight-only writes nothing, so the "you are relocating an existing
    # install" refusal below would be noise: only the argument shapes matter.
    if [ -f "$docker_env" ] && [ "$preflight_only" -eq 0 ]; then
        configured_install_root=$(_helper get-env "$docker_env" N_AGENT_INSTALL_ROOT || true)
        configured_code_root=$(_helper get-env "$docker_env" N_AGENT_CODE_ROOT || true)
    fi

    install_root=$(_resolve_root "$install_root_arg" "$configured_install_root" \
        "$default_install_root" "--install-root") || exit "$EXIT_ARGUMENT"
    code_root=$(_resolve_root "$code_root_arg" "$configured_code_root" \
        "$default_code_root" "--code-root") || exit "$EXIT_ARGUMENT"

    if _is_below "$code_root" "$install_root/workspace"; then
        _fail "$EXIT_ARGUMENT" \
            "--code-root must not live inside <install-root>/workspace (it is scanned)"
    fi

    if [ "$preflight_only" -eq 1 ]; then
        printf 'preflight: ok\n'
        printf '%s\n' "$summary" | _helper_summary
        return 0
    fi

    # --- S4: refuse an occupied read-only mount source before writing -----
    needs_policy=false
    case "$summary" in
        *'"locals/host-terminal-policy.yaml"'*) ;;
        *) needs_policy=true ;;
    esac
    if _helper host-check --install-root "$install_root" --repo-dir "$REPO_DIR" \
        --needs-policy "$needs_policy"; then
        :
    else
        exit $?
    fi

    if [ "$host_only" -eq 1 ]; then
        _apply_host false || exit $?
        # Rendering is presentation, not outcome: the files already landed, so
        # a broken renderer must not turn a successful apply into a failure.
        _print_host_report || _stderr "warning: the host report could not be rendered"
        return 0
    fi

    # --- S8: docker is a hard runtime dependency from here on -------------
    command -v docker >/dev/null 2>&1 || _fail "$EXIT_RUNTIME" \
        "docker is required to import the database layer; install it or rerun with --host-only"

    compose_file="$REPO_DIR/docker/docker-compose.yml"
    [ -f "$compose_file" ] || _fail "$EXIT_RUNTIME" "missing $compose_file"
    # Fixed compose context: never depend on the caller's cwd or environment.
    set -- --project-directory "$REPO_DIR/docker" --env-file "$docker_env" -f "$compose_file"

    running=0
    if docker compose "$@" ps --status running --services 2>/dev/null | grep -qx n-agent; then
        running=1
    fi

    # --- S6: read the target's natural keys while the old install is intact
    WORK=$(mktemp -d "${TMPDIR:-/tmp}/n-agent-import.XXXXXX")
    pre_existing_file="$WORK/pre-existing.json"
    target_export="$WORK/target-export.json"
    has_database=0
    [ -f "$install_root/locals/sessions.db" ] && has_database=1

    scanned=0
    if [ "$running" -eq 1 ]; then
        if docker compose "$@" exec -T n-agent n-agent config export --stdout \
            --redact-secrets >"$target_export" 2>/dev/null; then
            if _helper pre-existing "$target_export" >"$pre_existing_file"; then
                scanned=1
            fi
        fi
    fi
    if [ "$scanned" -eq 0 ]; then
        if [ "$has_database" -eq 1 ]; then
            _fail "$EXIT_RUNTIME" \
                "this machine already has $install_root/locals/sessions.db but its \
records cannot be read (is the service healthy? sh docker/restart.sh). Refusing to \
import: without the pre-existing key set every record would look new"
        fi
        # A confirmed-empty new install: the empty set is a fact, not a guess.
        printf '{}\n' >"$pre_existing_file"
    fi

    # --- S7: the placeholder network N-KB attaches to ---------------------
    network_note=""
    if ! docker network inspect "$KB_NETWORK" >/dev/null 2>&1; then
        if [ "$dry_run" -eq 1 ]; then
            network_note="would create the placeholder network $KB_NETWORK"
        elif docker network create "$KB_NETWORK" >/dev/null 2>&1; then
            network_note="created the placeholder network $KB_NETWORK"
        else
            _fail "$EXIT_RUNTIME" "cannot create the placeholder network $KB_NETWORK"
        fi
    fi

    # --- S8: a read-only diff decides whether a maintenance window is due -
    # A fully skipped rerun changes nothing and must not park the service.
    _apply_host true || exit $?
    if [ "$dry_run" -eq 0 ] && { [ "$HOST_CHANGED" = "true" ] || [ "$running" -eq 0 ]; }; then
        _helper edit-env "$docker_env" "$MAINTENANCE_KEY" --value true
        MAINTENANCE=1
        WINDOW_USED=1
    fi

    # --- S4/S7: land the host files ---------------------------------------
    _apply_host false || exit $?

    # --- S8: only start when something changed or the service is down -----
    if [ "$MAINTENANCE" -eq 1 ]; then
        if N_AGENT_MIGRATION_START=1 sh "$REPO_DIR/docker/restart.sh"; then
            running=1
        else
            _fail "$EXIT_RUNTIME" \
                "the service did not come up; the database was NOT touched, fix the \
start failure and rerun this same command"
        fi
    fi

    # --- S9: the database layer, through the in-container CLI -------------
    envelope="$WORK/envelope.json"
    # Pass the helper's code through instead of flattening it to 2: it returns
    # 3 when a context file on this machine is unreadable, which is a very
    # different problem from a malformed bundle and needs a different fix.
    # `|| exit $?` and not `if ! ...; then exit $?`: inside the then-branch of
    # a negated condition $? is 0, which would turn the failure into success.
    _helper db-envelope "$bundle" --pre-existing "$pre_existing_file" >"$envelope" \
        || exit $?
    chmod 600 "$envelope"

    # A dry run is not allowed to start anything, so with the service down the
    # database diff cannot be computed at all. Report that as unknown: running
    # the import anyway would fail, and an empty report would read as
    # "nothing to import" -- the opposite of the truth.
    if [ "$dry_run" -eq 1 ] && [ "$running" -eq 0 ]; then
        DB_UNKNOWN=1
    else
        DB_UNKNOWN=0
    fi

    set -- "$@" exec -T n-agent n-agent config import --stdin --internal-context \
        --json --mode "$mode"
    [ "$schedules" = "enabled" ] && set -- "$@" --with-schedules
    [ "$dry_run" -eq 1 ] && set -- "$@" --dry-run

    report="$WORK/report.json"
    import_status=0
    if [ "$DB_UNKNOWN" -eq 0 ]; then
        if docker compose "$@" <"$envelope" >"$report" 2>"$WORK/import.err"; then
            import_status=0
        else
            import_status=$?
        fi
    fi
    if [ "$import_status" -gt 1 ]; then
        _stderr "$(cat "$WORK/import.err")"
        _fail "$EXIT_RUNTIME" "the in-container config import could not run (exit $import_status)"
    fi

    # --- S9: restart only when a record was actually written --------------
    restarted=0
    refresh=no
    # The database is committed at this point, so a helper failure must not
    # decide the outcome. Under `set -e` the assignment is the last command of
    # the AND-list, so a non-zero helper would kill the script silently and
    # exit with the helper's code -- 2 there means "target untouched", the
    # opposite of the truth. Default to restarting: an unnecessary restart is
    # cheap, a skipped one leaves the new records inert.
    if [ "$dry_run" -eq 0 ]; then
        if ! refresh=$(_helper restart-needed "$report"); then
            _stderr "warning: could not decide whether a restart is needed; restarting"
            refresh=yes
        fi
    fi
    # Leaving the maintenance window is itself a restart, and it doubles as the
    # refresh that makes the committed records effective.
    if [ "$MAINTENANCE" -eq 1 ] || [ "$refresh" = "yes" ]; then
        if [ "$MAINTENANCE" -eq 1 ]; then
            MAINTENANCE=0
            _helper edit-env "$docker_env" "$MAINTENANCE_KEY"
        fi
        if N_AGENT_MIGRATION_START=1 sh "$REPO_DIR/docker/restart.sh"; then
            restarted=1
        else
            _print_report "$report" || true
            _fail "$EXIT_RUNTIME" \
                "配置已落库但未生效: the records were committed but the service could \
not be refreshed (config applied to the database, not effective yet). Rerun \
sh docker/restart.sh"
        fi
    fi

    # --- S10: segmented report -------------------------------------------
    # Same reason as above, and it matters more here: the database is already
    # committed, so letting `set -e` kill the script on a rendering error would
    # report a partial failure that never happened.
    _print_host_report || _stderr "warning: the host report could not be rendered"
    printf 'database (%s):\n' "$mode"
    if [ "$DB_UNKNOWN" -eq 1 ]; then
        printf '  unknown: the service is not running and a dry run must not start\n'
        printf '           it. Start it (sh docker/restart.sh) and rerun --dry-run to\n'
        printf '           see the database diff.\n'
    else
        _print_report "$report" || true
    fi
    [ -n "$network_note" ] && printf 'network:  %s\n' "$network_note"
    if [ "$WINDOW_USED" -eq 1 ]; then
        printf 'window:   %s was set for the import and has been removed again\n' \
            "$MAINTENANCE_KEY"
    fi
    printf 'service:  %s\n' \
        "$([ "$restarted" -eq 1 ] && printf 'refreshed' || printf 'not restarted (nothing was written)')"
    _print_followups

    if [ "$import_status" -eq 1 ]; then
        return "$EXIT_PARTIAL"
    fi
    return 0
}

# Resolve one deployment root: an explicit flag always wins; otherwise a value
# already configured on this machine is honoured, but only when it agrees with
# the default -- silently relocating an existing install is never right.
_resolve_root() {
    _flag=$1
    _configured=$2
    _default=$3
    _option=$4
    if [ -n "$_flag" ]; then
        printf '%s\n' "$_flag"
        return 0
    fi
    if [ -n "$_configured" ] && [ "$_configured" != "$_default" ]; then
        _stderr "error: this machine is already configured with $_option=$_configured,"
        _stderr "error: which differs from the default $_default."
        _stderr "error: pass $_option explicitly to say which one you mean."
        return 1
    fi
    if [ -n "$_configured" ]; then
        printf '%s\n' "$_configured"
        return 0
    fi
    printf '%s\n' "$_default"
    return 0
}

HOST_CHANGED=false
HOST_REPORT=""

# $1 = "true" to run the read-only diff, "false" to actually write. A global
# --dry-run always forces the read-only form.
_apply_host() {
    _dry=${1:-false}
    [ "$dry_run" -eq 1 ] && _dry=true
    if HOST_REPORT=$(_helper host-apply "$bundle" --install-root "$install_root" \
        --code-root "$code_root" --repo-dir "$REPO_DIR" --mode "$mode" \
        --dry-run "$_dry"); then
        :
    else
        return $?
    fi
    case "$HOST_REPORT" in
        *'"changed": true'*) HOST_CHANGED=true ;;
        *) HOST_CHANGED=false ;;
    esac
    return 0
}

_print_host_report() {
    printf 'host files (%s%s):\n' "$mode" \
        "$([ "$dry_run" -eq 1 ] && printf ', dry run' || printf '')"
    printf 'install-root: %s\n' "$install_root"
    printf 'code-root:    %s\n' "$code_root"
    printf '%s\n' "$HOST_REPORT" | python3 -c '
import json, sys
document = json.load(sys.stdin)
for action in document.get("actions", []):
    print("  " + action)
for note in document.get("notes", []):
    print("  note: " + note)
'
}

_print_report() {
    _helper report-summary "$1"
}

_print_followups() {
    printf '\n'
    printf 'follow-ups (not done by this script):\n'
    printf '  - MCP sites were imported as configuration only; re-probe each site so\n'
    printf '    its tool list is rediscovered on this machine\n'
    printf '  - knowledge bases need a connectivity probe; N-KB itself does not travel\n'
    printf '    with the bundle and has to be deployed separately\n'
    printf '  - the host Browser Bridge is a host process: start it by hand\n'
    printf '  - %s is a placeholder network so compose can start before N-KB exists\n' "$KB_NETWORK"
    printf '  - if docker reports an address pool conflict on 172.19.0.0/16, free that\n'
    printf '    range or repoint the placeholder network before rerunning\n'
}

# Prints the interesting fields of the preflight summary; never a value.
_helper_summary() {
    python3 -c '
import json, sys
document = json.load(sys.stdin)
origin = document["source"]
print("schema:   %s" % document["schema_version"])
print("origin:   hostname=%s install_root=%s code_root=%s"
      % (origin["hostname"], origin["install_root"], origin["code_root"]))
print("members:  %d (%d bytes uncompressed)"
      % (document["member_count"], document["total_bytes"]))
print("redacted: %s" % ("yes" if document["redacted"] else "no"))
for name in document["carries_script"]:
    print("ignored:  %s (a bundled script is never executed)" % name)
'
}

if [ -z "${N_AGENT_SOURCE_ONLY:-}" ]; then
    main "$@"
fi

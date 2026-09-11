#!/bin/sh
# docker/install.sh -- the one-command entry point on a fresh machine (plan T8,
# spec "配置迁移包").
#
# Copy exactly two files onto the new Mac -- the bundle produced by
# docker/config-export.sh and this script -- and run `sh install.sh`. This
# script never imports anything itself: it locates a compatible checkout,
# checks that docker can be reached, and hands over to that checkout's
# docker/config-import.sh with exec, so the importer's own exit code and
# output are what the caller sees.
#
# Nothing inside the bundle is ever read or executed here -- not even the
# install.sh packed next to it, which exists for a human to read.
#
# Usage: sh install.sh [<bundle.tar.gz>] [--repo <checkout>] [import options...]
#
#   <bundle.tar.gz>   the bundle to import. When omitted, the directory this
#                     script lives in must hold exactly one
#                     n-agent-config-*.tar.gz; zero or several are refused
#                     rather than guessed.
#   --repo <checkout> the n-agent checkout to run from. Otherwise:
#                     $N_AGENT_REPO, then upwards from this script, then
#                     upwards from the working directory. --repo is consumed
#                     here and is not forwarded.
#   everything else   forwarded verbatim to docker/config-import.sh
#                     (--mode, --install-root, --code-root, --dry-run, ...).
#                     Their meaning and defaults belong to that script.
#
# Exit codes: 2 bad arguments (ambiguous or unreadable bundle), 3 no checkout
# or docker unavailable -- both refuse before anything lands. Any other code
# comes from docker/config-import.sh unchanged.

set -eu

EXIT_ARGUMENT=2
EXIT_RUNTIME=3

SENTINEL='--n-agent-argv-end--'

_stderr() {
    printf '%s\n' "$*" >&2
}

_fail() {
    _code=$1
    shift
    _stderr "error: $*"
    exit "$_code"
}

_usage() {
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
}

# Directory this script was started from; the bundle is looked for next to it.
_script_dir() {
    case "${0:-}" in
        */*)
            (CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
            ;;
        *)
            pwd -P
            ;;
    esac
}

# A checkout is any directory carrying docker/config-import.sh -- the only
# file this entry point needs from it.
_is_checkout() {
    [ -n "$1" ] && [ -f "$1/docker/config-import.sh" ]
}

_walk_up() {
    _current=$1
    while [ -n "$_current" ]; do
        if _is_checkout "$_current"; then
            printf '%s\n' "$_current"
            return 0
        fi
        [ "$_current" = "/" ] && break
        _current=$(dirname -- "$_current")
    done
    return 1
}

main() {
    bundle=""
    repo=""
    bundle_given=0

    # A bundle, when given, is the first argument: everything after it belongs
    # to config-import.sh and is never interpreted here.
    if [ $# -gt 0 ]; then
        case $1 in
            -*) ;;
            *)
                bundle=$1
                bundle_given=1
                shift
                ;;
        esac
    fi

    # Rebuild the forwarded argument list by rotating through the sentinel:
    # values keep their exact text, spaces included.
    set -- "$@" "$SENTINEL"
    while [ "$1" != "$SENTINEL" ]; do
        case $1 in
            --repo)
                shift
                [ "$1" != "$SENTINEL" ] || _fail "$EXIT_ARGUMENT" "--repo needs a value"
                repo=$1
                shift
                ;;
            --repo=*)
                repo=${1#--repo=}
                [ -n "$repo" ] || _fail "$EXIT_ARGUMENT" "--repo needs a value"
                shift
                ;;
            -h|--help)
                _usage
                return 0
                ;;
            *)
                set -- "$@" "$1"
                shift
                ;;
        esac
    done
    shift

    # --- the bundle to import ---------------------------------------------
    if [ "$bundle_given" -eq 0 ]; then
        bundle=$(_sole_bundle "$(_script_dir)") || exit "$EXIT_ARGUMENT"
    fi

    # --- 1. the checkout, before anything else ----------------------------
    if [ -n "$repo" ]; then
        _is_checkout "$repo" || _fail "$EXIT_RUNTIME" \
            "--repo $repo is not an n-agent checkout (no docker/config-import.sh in it)"
        repo=$(CDPATH= cd -- "$repo" && pwd -P)
    else
        repo=$(_locate_checkout) || _no_checkout
    fi

    # --- 2. docker, only once the checkout is known -----------------------
    command -v docker >/dev/null 2>&1 || _fail "$EXIT_RUNTIME" \
        "docker is not on PATH; install Docker Desktop, start it and rerun this command"
    docker info >/dev/null 2>&1 || _fail "$EXIT_RUNTIME" \
        "the docker daemon is not reachable; start Docker Desktop and rerun this command"
    docker compose version >/dev/null 2>&1 || _fail "$EXIT_RUNTIME" \
        "docker compose (v2 plugin) is required; update Docker Desktop and rerun"

    # --- 3. the bundle has to be readable; its format is the importer's job
    [ -f "$bundle" ] && [ -r "$bundle" ] || _fail "$EXIT_ARGUMENT" \
        "cannot read the bundle: $bundle"

    # --- 4. hand over; the importer owns everything from here -------------
    exec sh "$repo/docker/config-import.sh" "$bundle" "$@"
}

# Exactly one n-agent-config-*.tar.gz next to this script, or a refusal: a
# wrong machine's configuration must never be picked by "newest wins".
_sole_bundle() {
    _dir=$1
    _count=0
    _sole=""
    for _candidate in "$_dir"/n-agent-config-*.tar.gz; do
        [ -f "$_candidate" ] || continue
        _count=$((_count + 1))
        _sole=$_candidate
    done
    if [ "$_count" -eq 1 ]; then
        printf '%s\n' "$_sole"
        return 0
    fi
    if [ "$_count" -eq 0 ]; then
        _stderr "error: no bundle given and no n-agent-config-*.tar.gz next to this script"
        _stderr "error: ($_dir). Pass the bundle path explicitly:"
        _stderr "error:   sh install.sh /path/to/n-agent-config-<host>-<stamp>.tar.gz"
        return 1
    fi
    _stderr "error: several bundles sit next to this script; say which one you mean:"
    for _candidate in "$_dir"/n-agent-config-*.tar.gz; do
        [ -f "$_candidate" ] || continue
        _stderr "error:   $_candidate"
    done
    return 1
}

_locate_checkout() {
    if [ -n "${N_AGENT_REPO:-}" ]; then
        _is_checkout "$N_AGENT_REPO" || return 1
        (CDPATH= cd -- "$N_AGENT_REPO" && pwd -P)
        return 0
    fi
    _walk_up "$(_script_dir)" && return 0
    _walk_up "$(pwd -P)" && return 0
    return 1
}

_no_checkout() {
    _stderr "error: no n-agent checkout found (a directory containing"
    _stderr "error: docker/config-import.sh). The bundle carries configuration only;"
    _stderr "error: the code has to be on this machine already. Either:"
    _stderr "error:   git clone <n-agent repository> ~/code/n-agent"
    _stderr "error: then rerun with the checkout you want:"
    _stderr "error:   sh install.sh <bundle.tar.gz> --repo ~/code/n-agent"
    _stderr "error: (or export N_AGENT_REPO=~/code/n-agent)"
    exit "$EXIT_RUNTIME"
}

main "$@"

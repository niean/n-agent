#!/bin/sh
# Shared observability lifecycle. No argument deploys/restarts the stack.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
action=${1:-restart}
if [ "$#" -gt 0 ]; then shift; fi
case "$action" in
  start|restart) action=deploy ;;
  stop|migrate|compose) ;;
  -h|--help|help)
    printf '%s\n' 'Usage: restart.sh [restart|start|stop|migrate|compose COMMAND ...]' \
      'Default: deploy/restart the shared stack. stop preserves data and networks.'
    exit 0 ;;
  *) printf '%s\n' "Unknown action: $action (use --help)" >&2; exit 2 ;;
esac
exec python3 "$SCRIPT_DIR/manage-stack.py" "$action" "$@"

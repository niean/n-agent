#!/bin/sh

set -u

usage() {
    echo "usage: sh .harness/framework/scripts/get-config.sh <config-key>" >&2
    exit 64
}

fail_config() {
    echo "harness config error: $1" >&2
    exit 65
}

fail_dependency() {
    echo "harness config error: no supported JSON parser found (tried jq, python3, node)" >&2
    exit 127
}

[ "$#" -eq 1 ] || usage
requested_key=$1

case "$requested_key" in
    thirdReview.enabled)
        default_value=false
        ;;
    thirdReview.provider|thirdReview.model)
        default_value=
        ;;
    thirdReview.timeoutSeconds)
        default_value=900
        ;;
    hooks.afterFinish.enabled)
        default_value=false
        ;;
    *)
        echo "harness config error: unsupported config key: $requested_key" >&2
        exit 64
        ;;
esac

script_dir=$(CDPATH= cd "${0%/*}" && pwd) || fail_config "cannot resolve script directory"
harness_dir=$(CDPATH= cd "$script_dir/../.." && pwd) || fail_config "cannot resolve .harness directory"
config_file=$harness_dir/harness.json

if [ ! -e "$config_file" ]; then
    printf '%s\n' "$default_value"
    exit 0
fi

[ -f "$config_file" ] && [ -r "$config_file" ] || fail_config ".harness/harness.json must be a readable regular file"

parser=${HARNESS_CONFIG_PARSER:-}
if [ -z "$parser" ]; then
    if command -v jq >/dev/null 2>&1; then
        parser=jq
    elif command -v python3 >/dev/null 2>&1; then
        parser=python3
    elif command -v node >/dev/null 2>&1; then
        parser=node
    else
        fail_dependency
    fi
fi

case "$parser" in
    jq)
        command -v jq >/dev/null 2>&1 || fail_dependency
        if jq -r --arg requested "$requested_key" '
            def fail($message): error($message);
            def reject_unknown($allowed; $scope):
                (keys_unsorted - $allowed) as $extra
                | if ($extra | length) > 0
                  then fail($scope + " contains unsupported field: " + $extra[0])
                  else .
                  end;
            def valid_optional_string:
                . == null or (type == "string" and (test("[\\u0000\\r\\n]") | not));
            def validate:
                if type != "object" then fail("root must be an object") else . end
                | reject_unknown(["version", "thirdReview", "hooks"]; "root")
                | if (has("version") and (.version | type) == "number" and .version == 1)
                  then . else fail("version must be 1") end
                | if (has("thirdReview") and (.thirdReview | type) != "object")
                  then fail("thirdReview must be an object") else . end
                | if (has("hooks") and (.hooks | type) != "object")
                  then fail("hooks must be an object") else . end
                | (.thirdReview // {}) as $third
                | ($third | reject_unknown(["enabled", "provider", "model", "timeoutSeconds"]; "thirdReview")) as $third
                | if ($third | has("enabled")) and ($third.enabled | type) != "boolean"
                  then fail("thirdReview.enabled must be boolean") else . end
                | if ($third | has("provider")) and (($third.provider | valid_optional_string) | not)
                  then fail("thirdReview.provider must be string or null without control characters") else . end
                | if ($third | has("model")) and (($third.model | valid_optional_string) | not)
                  then fail("thirdReview.model must be string or null without control characters") else . end
                | if ($third | has("timeoutSeconds")) and
                     ((($third.timeoutSeconds | type) != "number") or
                      (($third.timeoutSeconds | floor) != $third.timeoutSeconds) or
                      $third.timeoutSeconds < 1 or $third.timeoutSeconds > 86400)
                  then fail("thirdReview.timeoutSeconds must be an integer from 1 to 86400") else . end
                | (.hooks // {}) as $hooks
                | ($hooks | reject_unknown(["afterFinish"]; "hooks")) as $hooks
                | if ($hooks | has("afterFinish")) and ($hooks.afterFinish | type) != "object"
                  then fail("hooks.afterFinish must be an object") else . end
                | ($hooks.afterFinish // {}) as $after
                | ($after | reject_unknown(["enabled"]; "hooks.afterFinish")) as $after
                | if ($after | has("enabled")) and ($after.enabled | type) != "boolean"
                  then fail("hooks.afterFinish.enabled must be boolean") else . end;
            validate
            | if $requested == "thirdReview.enabled" then (.thirdReview.enabled // false)
              elif $requested == "thirdReview.provider" then (.thirdReview.provider // "")
              elif $requested == "thirdReview.model" then (.thirdReview.model // "")
              elif $requested == "thirdReview.timeoutSeconds" then (.thirdReview.timeoutSeconds // 900)
              elif $requested == "hooks.afterFinish.enabled" then (.hooks.afterFinish.enabled // false)
              else fail("unsupported config key: " + $requested)
              end
        ' "$config_file"
        then
            exit 0
        fi
        fail_config "invalid JSON or invalid configuration"
        ;;
    python3)
        command -v python3 >/dev/null 2>&1 || fail_dependency
        HARNESS_CONFIG_FILE=$config_file HARNESS_CONFIG_KEY=$requested_key python3 - <<'PY'
import json
import math
import os
import sys


def fail(message):
    print(f"harness config error: {message}", file=sys.stderr)
    raise SystemExit(65)


def reject_unknown(value, allowed, scope):
    extra = sorted(set(value) - set(allowed))
    if extra:
        fail(f"{scope} contains unsupported field: {extra[0]}")


def optional_string(value, name):
    if value is None:
        return
    if not isinstance(value, str) or any(char in value for char in "\x00\r\n"):
        fail(f"{name} must be string or null without control characters")


try:
    with open(os.environ["HARNESS_CONFIG_FILE"], encoding="utf-8") as handle:
        root = json.load(
            handle,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
    fail("invalid JSON")

if not isinstance(root, dict):
    fail("root must be an object")
reject_unknown(root, {"version", "thirdReview", "hooks"}, "root")
version = root.get("version")
if isinstance(version, bool) or not isinstance(version, (int, float)) or not math.isfinite(version) or version != 1:
    fail("version must be 1")

third = root.get("thirdReview", {})
if not isinstance(third, dict):
    fail("thirdReview must be an object")
reject_unknown(third, {"enabled", "provider", "model", "timeoutSeconds"}, "thirdReview")
if "enabled" in third and not isinstance(third["enabled"], bool):
    fail("thirdReview.enabled must be boolean")
if "provider" in third:
    optional_string(third["provider"], "thirdReview.provider")
if "model" in third:
    optional_string(third["model"], "thirdReview.model")
if "timeoutSeconds" in third:
    timeout = third["timeoutSeconds"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or (isinstance(timeout, float) and not timeout.is_integer())
        or not 1 <= timeout <= 86400
    ):
        fail("thirdReview.timeoutSeconds must be an integer from 1 to 86400")

hooks = root.get("hooks", {})
if not isinstance(hooks, dict):
    fail("hooks must be an object")
reject_unknown(hooks, {"afterFinish"}, "hooks")
after = hooks.get("afterFinish", {})
if not isinstance(after, dict):
    fail("hooks.afterFinish must be an object")
reject_unknown(after, {"enabled"}, "hooks.afterFinish")
if "enabled" in after and not isinstance(after["enabled"], bool):
    fail("hooks.afterFinish.enabled must be boolean")

values = {
    "thirdReview.enabled": third.get("enabled", False),
    "thirdReview.provider": third.get("provider") or "",
    "thirdReview.model": third.get("model") or "",
    "thirdReview.timeoutSeconds": third.get("timeoutSeconds", 900),
    "hooks.afterFinish.enabled": after.get("enabled", False),
}
value = values[os.environ["HARNESS_CONFIG_KEY"]]
if isinstance(value, bool):
    print("true" if value else "false")
elif isinstance(value, float):
    print(int(value))
else:
    print(value)
PY
        exit $?
        ;;
    node)
        command -v node >/dev/null 2>&1 || fail_dependency
        HARNESS_CONFIG_FILE=$config_file HARNESS_CONFIG_KEY=$requested_key node <<'JS'
const fs = require("fs");

function fail(message) {
  process.stderr.write(`harness config error: ${message}\n`);
  process.exit(65);
}

function rejectUnknown(value, allowed, scope) {
  const extra = Object.keys(value).filter((key) => !allowed.includes(key)).sort();
  if (extra.length) fail(`${scope} contains unsupported field: ${extra[0]}`);
}

function optionalString(value, name) {
  if (value === null) return;
  if (typeof value !== "string" || /[\u0000\r\n]/.test(value)) {
    fail(`${name} must be string or null without control characters`);
  }
}

let root;
try {
  root = JSON.parse(fs.readFileSync(process.env.HARNESS_CONFIG_FILE, "utf8"));
} catch (_) {
  fail("invalid JSON");
}

if (root === null || Array.isArray(root) || typeof root !== "object") fail("root must be an object");
rejectUnknown(root, ["version", "thirdReview", "hooks"], "root");
if (root.version !== 1) fail("version must be 1");

const third = root.thirdReview === undefined ? {} : root.thirdReview;
if (third === null || Array.isArray(third) || typeof third !== "object") fail("thirdReview must be an object");
rejectUnknown(third, ["enabled", "provider", "model", "timeoutSeconds"], "thirdReview");
if (third.enabled !== undefined && typeof third.enabled !== "boolean") fail("thirdReview.enabled must be boolean");
if (third.provider !== undefined) optionalString(third.provider, "thirdReview.provider");
if (third.model !== undefined) optionalString(third.model, "thirdReview.model");
if (third.timeoutSeconds !== undefined && (!Number.isInteger(third.timeoutSeconds) || third.timeoutSeconds < 1 || third.timeoutSeconds > 86400)) {
  fail("thirdReview.timeoutSeconds must be an integer from 1 to 86400");
}

const hooks = root.hooks === undefined ? {} : root.hooks;
if (hooks === null || Array.isArray(hooks) || typeof hooks !== "object") fail("hooks must be an object");
rejectUnknown(hooks, ["afterFinish"], "hooks");
const after = hooks.afterFinish === undefined ? {} : hooks.afterFinish;
if (after === null || Array.isArray(after) || typeof after !== "object") fail("hooks.afterFinish must be an object");
rejectUnknown(after, ["enabled"], "hooks.afterFinish");
if (after.enabled !== undefined && typeof after.enabled !== "boolean") fail("hooks.afterFinish.enabled must be boolean");

const values = {
  "thirdReview.enabled": third.enabled ?? false,
  "thirdReview.provider": third.provider ?? "",
  "thirdReview.model": third.model ?? "",
  "thirdReview.timeoutSeconds": third.timeoutSeconds ?? 900,
  "hooks.afterFinish.enabled": after.enabled ?? false,
};
process.stdout.write(`${String(values[process.env.HARNESS_CONFIG_KEY])}\n`);
JS
        exit $?
        ;;
    *)
        echo "harness config error: unsupported parser override: $parser" >&2
        exit 64
        ;;
esac

#!/bin/sh

failure_step=validation

fail() {
    printf '%s\n' "$failure_step: $1" >&2
    exit 2
}

has_ascii_control() {
    case $1 in
        *'
'*) return 0 ;;
    esac
    LC_ALL=C printf '%s' "$1" | LC_ALL=C grep '[[:cntrl:]]' >/dev/null 2>&1
}

is_safe_relative_input() {
    safe_input=$1
    has_ascii_control "$safe_input" && return 1
    case $safe_input in
        ''|/*) return 1 ;;
    esac
    case "/$safe_input/" in
        */../*) return 1 ;;
    esac
    return 0
}

canonical_markdown() {
    canonical_input=$1
    is_safe_relative_input "$canonical_input" || return 1
    lexical_input=$(printf '%s\n' "$canonical_input" | LC_ALL=C awk -F/ '{
        output = ""
        for (i = 1; i <= NF; i++) {
            if ($i == "" || $i == ".") continue
            output = output (output == "" ? "" : "/") $i
        }
        print output
    }') || return 1
    case $lexical_input in
        .harness/framework|.harness/framework/*|\
        .harness/prd|.harness/prd/*|\
        .harness/knowledge|.harness/knowledge/*|\
        .harness/lessons|.harness/lessons/*) return 1 ;;
    esac
    case $lexical_input in
        *.md) ;;
        *) return 1 ;;
    esac

    canonical_candidate=$repo_root/$lexical_input
    [ ! -L "$canonical_candidate" ] || return 1
    [ -f "$canonical_candidate" ] || return 1
    [ -r "$canonical_candidate" ] || return 1
    canonical_dir=$(CDPATH= cd -P "$(dirname "$canonical_candidate")" 2>/dev/null && pwd -P) || return 1
    canonical_result=$canonical_dir/$(basename "$canonical_candidate")
    case $canonical_result in
        "$repo_root"/.harness/*) ;;
        *) return 1 ;;
    esac
    case $canonical_result in
        "$repo_root"/.harness/framework/*|\
        "$repo_root"/.harness/prd/*|\
        "$repo_root"/.harness/knowledge/*|\
        "$repo_root"/.harness/lessons/*) return 1 ;;
    esac
    printf '%s\n' "$canonical_result"
}

canonical_resource() {
    resource_path=$1
    [ ! -L "$resource_path" ] || return 1
    [ -f "$resource_path" ] || return 1
    [ -r "$resource_path" ] || return 1
    resource_dir=$(CDPATH= cd -P "$(dirname "$resource_path")" 2>/dev/null && pwd -P) || return 1
    printf '%s/%s\n' "$resource_dir" "$(basename "$resource_path")"
}

list_is_valid() {
    list_value=$1
    list_max=$2
    LC_ALL=C awk -v value="$list_value" -v maximum="$list_max" 'BEGIN {
        count = split(value, items, "；")
        if (count < 1 || count > maximum) exit 1
        for (i = 1; i <= count; i++) {
            item = items[i]
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", item)
            if (item == "" || item == "无") exit 1
        }
    }'
}

[ $# -ge 1 ] || fail 'expected doc_type and target_file'
doc_type=$1
case $doc_type in
    spec) [ $# -eq 2 ] || fail 'spec requires exactly target_file' ;;
    plan) [ $# -eq 3 ] || fail 'plan requires target_file and spec_file' ;;
    *) fail 'doc_type must be spec or plan' ;;
esac

repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || fail 'Git root is unavailable'
has_ascii_control "$repo_root" && fail 'Git root must not contain ASCII control bytes'
repo_root=$(CDPATH= cd -P "$repo_root" 2>/dev/null && pwd -P) || fail 'Git root is not canonical'
has_ascii_control "$repo_root" && fail 'canonical Git root must not contain ASCII control bytes'
current_dir=$(pwd -P) || fail 'current directory is unavailable'
[ "$current_dir" = "$repo_root" ] || fail 'runner must execute at the Git root'

runner_path=$0
case $runner_path in
    /*) ;;
    *) runner_path=$repo_root/$runner_path ;;
esac
runner_canonical=$(canonical_resource "$runner_path") || fail 'runner is not a canonical readable regular file'
script_dir=$(dirname "$runner_canonical")
skill_dir=$(CDPATH= cd -P "$script_dir/.." 2>/dev/null && pwd -P) || fail 'Skill directory is unavailable'

target_file=$(canonical_markdown "$2") || fail 'target_file is not a safe readable Harness Markdown file'
spec_file=
if [ "$doc_type" = plan ]; then
    spec_file=$(canonical_markdown "$3") || fail 'spec_file is not a safe readable Harness Markdown file'
    [ "$target_file" != "$spec_file" ] || fail 'plan target_file and spec_file must differ'
fi

prompt_dir=$skill_dir/prompts
[ ! -L "$prompt_dir" ] || fail 'prompt directory must not be a symlink'
[ -d "$prompt_dir" ] || fail 'prompt directory is unavailable'
prompt_dir=$(CDPATH= cd -P "$prompt_dir" 2>/dev/null && pwd -P) || fail 'prompt directory is unavailable'
[ "$prompt_dir" = "$skill_dir/prompts" ] || fail 'prompt directory escaped the Skill root'
prompt_path=$prompt_dir/$doc_type-review.md
prompt_path=$(canonical_resource "$prompt_path") || fail 'review prompt is not a canonical readable regular file'
[ "$(dirname "$prompt_path")" = "$prompt_dir" ] || fail 'review prompt escaped its resource directory'

provider_name=${HARNESS_THIRD_REVIEW_PROVIDER-}
[ -n "$provider_name" ] || fail 'provider is not configured; set Skill input, HARNESS_THIRD_REVIEW_PROVIDER, or harness.json thirdReview.provider'
has_ascii_control "$provider_name" && fail 'provider must not contain ASCII control bytes'
case $provider_name in
    *[!a-z0-9-]*|-*|*--*|*-) fail 'provider must be a kebab-case identifier' ;;
esac
provider_path=$skill_dir/providers/$provider_name.sh
provider_path=$(canonical_resource "$provider_path") || fail 'provider is not a canonical readable regular file'
expected_provider_dir=$skill_dir/providers
[ ! -L "$expected_provider_dir" ] || fail 'provider directory must not be a symlink'
expected_provider_dir=$(CDPATH= cd -P "$expected_provider_dir" 2>/dev/null && pwd -P) || fail 'provider directory is unavailable'
[ "$(dirname "$provider_path")" = "$expected_provider_dir" ] || fail 'provider escaped its resource directory'

model=${HARNESS_THIRD_REVIEW_MODEL-}
has_ascii_control "$model" && fail 'model must not contain ASCII control bytes'

# 超时是框架级配置：runner 校验并以 watchdog 统一执行这一个期限，provider
# 适配器不自建超时。默认 900 秒，可经 HARNESS_THIRD_REVIEW_TIMEOUT_SECONDS 覆盖。
timeout_seconds=${HARNESS_THIRD_REVIEW_TIMEOUT_SECONDS:-900}
case $timeout_seconds in
    ''|*[!0-9]*|??????*) fail 'timeout must be a 1-5 digit seconds value' ;;
esac
if [ "$timeout_seconds" -lt 1 ] || [ "$timeout_seconds" -gt 86400 ]; then
    fail 'timeout must be between 1 and 86400 seconds'
fi
HARNESS_THIRD_REVIEW_TIMEOUT_SECONDS=$timeout_seconds
export HARNESS_THIRD_REVIEW_TIMEOUT_SECONDS

runner_tmp=$(mktemp -d "${TMPDIR:-/tmp}/third-review-run.XXXXXX") || fail 'cannot create temporary directory'
provider_pid=
watchdog_pid=
killer_pid=

cleanup() {
    [ -z "$watchdog_pid" ] || kill "$watchdog_pid" 2>/dev/null || :
    [ -z "$killer_pid" ] || kill "$killer_pid" 2>/dev/null || :
    rm -rf "$runner_tmp" 2>/dev/null || :
}

terminate_provider() {
    [ -n "$provider_pid" ] || return 0
    kill -TERM "$provider_pid" 2>/dev/null || return 0
    (
        sleep 2
        kill -KILL "$provider_pid" 2>/dev/null || :
    ) >/dev/null 2>&1 &
    killer_pid=$!
    wait "$provider_pid" 2>/dev/null || :
    kill "$killer_pid" 2>/dev/null || :
    wait "$killer_pid" 2>/dev/null || :
    killer_pid=
}

handle_signal() {
    caught_signal=$1
    trap '' HUP INT TERM
    terminate_provider
    cleanup
    printf '%s\n' "provider: interrupted by signal $caught_signal" >&2
    case $caught_signal in
        HUP) exit 129 ;;
        INT) exit 130 ;;
        TERM) exit 143 ;;
    esac
}

trap cleanup EXIT
trap 'handle_signal HUP' HUP
trap 'handle_signal INT' INT
trap 'handle_signal TERM' TERM

prompt_file=$runner_tmp/prompt
stdout_file=$runner_tmp/stdout
stderr_file=$runner_tmp/stderr
normalized_file=$runner_tmp/normalized
without_nul_file=$runner_tmp/without-nul
without_lf_file=$runner_tmp/without-lf

output_fail() {
    if [ -s "$stderr_file" ]; then
        head -n 20 "$stderr_file" | sed 's/^/provider stderr: /' >&2
    fi
    fail "$1"
}

warn_invalid_summary() {
    printf '%s\n' "warning: provider structured summary is invalid: $1" >&2
    if [ -s "$stderr_file" ]; then
        printf '%s\n' 'provider: provider emitted diagnostic output' >&2
    fi
    cat "$stdout_file"
    exit 0
}

file_mode() {
    mode_path=$1
    if stat -c '%a' "$mode_path" >/dev/null 2>&1; then
        stat -c '%a' "$mode_path"
    else
        stat -f '%Lp' "$mode_path"
    fi
}

{
    cat "$prompt_path" || exit 1
    printf '\nREPO_ROOT: %s\nTARGET_FILE: %s\n' "$repo_root" "$target_file"
    if [ "$doc_type" = plan ]; then
        printf 'SPEC_FILE: %s\n' "$spec_file"
    fi
} >"$prompt_file" || fail 'cannot assemble prompt'

failure_step=boundary-check
target_hash_before=$(git hash-object --no-filters -- "$target_file" 2>/dev/null) || fail 'cannot hash target before review'
target_mode_before=$(file_mode "$target_file" 2>/dev/null) || fail 'cannot read target mode before review'
spec_hash_before=
spec_mode_before=
if [ "$doc_type" = plan ]; then
    spec_hash_before=$(git hash-object --no-filters -- "$spec_file" 2>/dev/null) || fail 'cannot hash associated spec before review'
    spec_mode_before=$(file_mode "$spec_file" 2>/dev/null) || fail 'cannot read associated spec mode before review'
fi

failure_step=provider
if [ -n "$model" ]; then
    (
        HARNESS_THIRD_REVIEW_MODEL=$model
        export HARNESS_THIRD_REVIEW_MODEL
        exec sh "$provider_path" "$repo_root"
    ) <"$prompt_file" >"$stdout_file" 2>"$stderr_file" &
else
    (
        unset HARNESS_THIRD_REVIEW_MODEL
        exec sh "$provider_path" "$repo_root"
    ) <"$prompt_file" >"$stdout_file" 2>"$stderr_file" &
fi
provider_pid=$!
timeout_marker=$runner_tmp/timed-out
# 步进 1 秒避免 watchdog 被停止时遗留长时间存活的孤儿 sleep；重定向输出
# 避免后台进程持有调用方的输出管道。
(
    watchdog_waited=0
    while [ "$watchdog_waited" -lt "$timeout_seconds" ]; do
        sleep 1
        watchdog_waited=$((watchdog_waited + 1))
    done
    printf '%s\n' timeout >"$timeout_marker"
    kill -TERM "$provider_pid" 2>/dev/null || :
    sleep 2
    kill -KILL "$provider_pid" 2>/dev/null || :
) >/dev/null 2>&1 &
watchdog_pid=$!
wait "$provider_pid"
provider_status=$?
provider_pid=
kill "$watchdog_pid" 2>/dev/null || :
wait "$watchdog_pid" 2>/dev/null || :
watchdog_pid=
if [ -f "$timeout_marker" ]; then
    printf '%s\n' "provider: timed out after $timeout_seconds seconds (exit timeout)" >&2
    exit 124
fi
if [ "$provider_status" -ne 0 ]; then
    printf '%s\n' "provider: exited with status $provider_status" >&2
    if [ -s "$stderr_file" ]; then
        head -n 20 "$stderr_file" | sed 's/^/provider stderr: /' >&2
    fi
    exit "$provider_status"
fi

failure_step=boundary-check
[ -f "$target_file" ] && [ ! -L "$target_file" ] || fail 'target type changed during review'
target_mode_after=$(file_mode "$target_file" 2>/dev/null) || fail 'cannot read target mode after review'
[ "$target_mode_before" = "$target_mode_after" ] || fail 'target mode changed during review'
if [ "$doc_type" = plan ]; then
    [ -f "$spec_file" ] && [ ! -L "$spec_file" ] || fail 'associated spec type changed during review'
    spec_mode_after=$(file_mode "$spec_file" 2>/dev/null) || fail 'cannot read associated spec mode after review'
    [ "$spec_mode_before" = "$spec_mode_after" ] || fail 'associated spec mode changed during review'
    spec_hash_after=$(git hash-object --no-filters -- "$spec_file" 2>/dev/null) || fail 'cannot hash associated spec after review'
    [ "$spec_hash_before" = "$spec_hash_after" ] || fail 'provider changed the read-only associated spec'
fi

failure_step=output-check
stdout_bytes=$(LC_ALL=C wc -c <"$stdout_file" | tr -d '[:space:]') || fail 'cannot measure provider stdout'
[ "$stdout_bytes" -le 65536 ] || output_fail 'provider stdout exceeds 64 KiB'
LC_ALL=C tr -d '\000' <"$stdout_file" >"$without_nul_file" || output_fail 'cannot inspect provider stdout'
without_nul_bytes=$(LC_ALL=C wc -c <"$without_nul_file" | tr -d '[:space:]') || fail 'cannot inspect provider stdout'
[ "$stdout_bytes" = "$without_nul_bytes" ] || output_fail 'provider stdout contains NUL'
LC_ALL=C tr -d '\n' <"$stdout_file" >"$without_lf_file" || output_fail 'cannot inspect provider stdout controls'
if LC_ALL=C grep '[[:cntrl:]]' "$without_lf_file" >/dev/null 2>&1; then
    output_fail 'provider stdout contains an unsafe ASCII control byte'
fi

LC_ALL=C awk '{
    lines[NR] = $0
    if ($0 !~ /^[[:space:]]*$/) {
        if (!first) first = NR
        last = NR
    }
} END {
    for (i = first; i <= last; i++) {
        line = lines[i]
        if (i == first) sub(/^[[:space:]]+/, "", line)
        if (i == last) sub(/[[:space:]]+$/, "", line)
        print line
    }
}' "$stdout_file" >"$normalized_file" || fail 'cannot normalize provider stdout'

if ! LC_ALL=C grep '[^[:space:]]' "$normalized_file" >/dev/null 2>&1; then
    failure_step=provider
    output_fail 'provider produced no review output'
fi

line_count=$(LC_ALL=C wc -l <"$normalized_file" | tr -d '[:space:]') || fail 'cannot count provider fields'
target_hash_after=$(git hash-object --no-filters -- "$target_file" 2>/dev/null) || fail 'cannot hash target after review'
if [ "$line_count" -ne 5 ] || ! LC_ALL=C awk '
    NR == 1 && /^状态: / { next }
    NR == 2 && /^修改数量: / { next }
    NR == 3 && /^修改摘要: / { next }
    NR == 4 && /^目标未达说明: / { next }
    NR == 5 && /^剩余风险: / { next }
    { exit 1 }
    END { if (NR != 5) exit 1 }
' "$normalized_file"; then
    warn_invalid_summary 'expected exactly five ordered fields'
fi

status=$(sed -n 's/^状态: //p' "$normalized_file")
count_line=$(sed -n 's/^修改数量: //p' "$normalized_file")
summary=$(sed -n 's/^修改摘要: //p' "$normalized_file")
unmet=$(sed -n 's/^目标未达说明: //p' "$normalized_file")
risk=$(sed -n 's/^剩余风险: //p' "$normalized_file")
case $status in approved|fixed) ;; *) warn_invalid_summary 'invalid review status' ;; esac
case $count_line in
    *' 项') count=${count_line% 项} ;;
    *) warn_invalid_summary 'invalid modification count' ;;
esac
case $count in ''|*[!0-9]*) warn_invalid_summary 'invalid modification count' ;; esac
is_zero=$(awk -v n="$count" 'BEGIN { print (n + 0 == 0) ? "yes" : "no" }')
is_lt_twenty=$(awk -v n="$count" 'BEGIN { print (n + 0 < 20) ? "yes" : "no" }')

if [ "$status" = approved ]; then
    [ "$is_zero" = yes ] || warn_invalid_summary 'approved requires modification count zero'
    [ "$summary" = 无 ] || warn_invalid_summary 'approved requires an empty modification summary'
    [ "$target_hash_before" = "$target_hash_after" ] || warn_invalid_summary 'approved requires unchanged target content'
else
    [ "$is_zero" = no ] || warn_invalid_summary 'fixed requires a positive modification count'
    [ "$summary" != 无 ] || warn_invalid_summary 'fixed requires a modification summary'
    list_is_valid "$summary" 5 || warn_invalid_summary 'modification summary must contain 1-5 nonempty items'
    [ "$target_hash_before" != "$target_hash_after" ] || warn_invalid_summary 'fixed requires changed target content'
fi

if [ "$risk" != 无 ]; then
    list_is_valid "$risk" 3 || warn_invalid_summary 'remaining risk must contain 1-3 nonempty items'
fi
if [ "$is_lt_twenty" = yes ]; then
    [ -n "$unmet" ] || warn_invalid_summary 'a count below 20 requires an unmet-target explanation'
    [ "$unmet" != 无 ] || warn_invalid_summary 'a count below 20 requires an unmet-target explanation'
else
    [ "$unmet" = 无 ] || warn_invalid_summary 'a count of 20 or more requires unmet-target explanation to be 无'
fi

if [ -s "$stderr_file" ]; then
    printf '%s\n' 'provider: provider emitted diagnostic output' >&2
fi
cat "$normalized_file"

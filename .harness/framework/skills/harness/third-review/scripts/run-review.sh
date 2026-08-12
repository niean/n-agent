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
[ -n "$provider_name" ] || provider_name=codex
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

runner_tmp=$(mktemp -d "${TMPDIR:-/tmp}/third-review-run.XXXXXX") || fail 'cannot create temporary directory'
provider_pid=
watchdog_pid=
killer_pid=
target_monitor_pid=

cleanup() {
    [ -z "$watchdog_pid" ] || kill "$watchdog_pid" 2>/dev/null || :
    [ -z "$killer_pid" ] || kill "$killer_pid" 2>/dev/null || :
    [ -z "$target_monitor_pid" ] || kill "$target_monitor_pid" 2>/dev/null || :
    rm -rf "$runner_tmp" 2>/dev/null || :
}

terminate_provider() {
    [ -n "$provider_pid" ] || return 0
    kill -TERM "$provider_pid" 2>/dev/null || return 0
    (
        sleep 2
        kill -KILL "$provider_pid" 2>/dev/null || :
    ) &
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

file_mode() {
    mode_path=$1
    if stat -c '%a' "$mode_path" >/dev/null 2>&1; then
        stat -c '%a' "$mode_path"
    else
        stat -f '%Lp' "$mode_path"
    fi
}

manifest_path_record() {
    manifest_path=$1
    if [ -L "$manifest_path" ]; then
        manifest_type=link
        manifest_content=$(
            { printf 'link\000'; readlink "$manifest_path"; } |
                git hash-object --stdin
        ) || return 1
    elif [ -f "$manifest_path" ]; then
        manifest_type=file
        manifest_content=$(git hash-object --no-filters -- "$manifest_path") || return 1
    elif [ -d "$manifest_path" ]; then
        manifest_type=directory
        manifest_content=-
    else
        manifest_type=other
        manifest_content=-
    fi
    if stat -c '%f' "$manifest_path" >/dev/null 2>&1; then
        manifest_mode=$(stat -c '%f' "$manifest_path") || return 1
    else
        manifest_mode=$(stat -f '%p' "$manifest_path") || return 1
    fi
    {
        printf '%s\000%s\000%s\000' "$manifest_type" "$manifest_mode" "$manifest_content"
        printf '%s' "$manifest_path"
    } | git hash-object --stdin
}

snapshot_repo() {
    snapshot_dir=$1
    mkdir -p "$snapshot_dir" || return 1
    (
        cd "$repo_root" || exit 1
        git rev-parse --verify HEAD >"$snapshot_dir/head" || exit 1
        git ls-tree -r -t -z HEAD >"$snapshot_dir/head-tree.z" || exit 1
        git ls-files --stage -z >"$snapshot_dir/index-stage.z" || exit 1
        git write-tree >"$snapshot_dir/index-tree" || exit 1
        git status --porcelain=v1 -z --untracked-files=all --ignored=matching >"$snapshot_dir/status.z" || exit 1
        find . -path ./.git -prune -o -exec sh -c '
            for manifest_path do
                [ "$manifest_path" != . ] || continue
                if [ -L "$manifest_path" ]; then
                    manifest_type=link
                    manifest_content=$(
                        { printf "link\000"; readlink "$manifest_path"; } |
                            git hash-object --stdin
                    ) || exit 1
                elif [ -f "$manifest_path" ]; then
                    manifest_type=file
                    manifest_content=$(git hash-object --no-filters -- "$manifest_path") || exit 1
                elif [ -d "$manifest_path" ]; then
                    manifest_type=directory
                    manifest_content=-
                else
                    manifest_type=other
                    manifest_content=-
                fi
                if stat -c "%f" "$manifest_path" >/dev/null 2>&1; then
                    manifest_mode=$(stat -c "%f" "$manifest_path") || exit 1
                else
                    manifest_mode=$(stat -f "%p" "$manifest_path") || exit 1
                fi
                {
                    printf "%s\000%s\000%s\000" "$manifest_type" "$manifest_mode" "$manifest_content"
                    printf "%s" "$manifest_path"
                } | git hash-object --stdin || exit 1
            done
        ' sh {} + | LC_ALL=C sort >"$snapshot_dir/worktree.manifest" || exit 1
        manifest_path_record "./$target_relative" >"$snapshot_dir/target.record" || exit 1
        [ "$(grep -Fxc "$(cat "$snapshot_dir/target.record")" "$snapshot_dir/worktree.manifest")" -eq 1 ] || exit 1
    ) || return 1
}

snapshot_metadata_equal() {
    first_snapshot=$1
    second_snapshot=$2
    cmp -s "$first_snapshot/head" "$second_snapshot/head" &&
        cmp -s "$first_snapshot/head-tree.z" "$second_snapshot/head-tree.z" &&
        cmp -s "$first_snapshot/index-stage.z" "$second_snapshot/index-stage.z" &&
        cmp -s "$first_snapshot/index-tree" "$second_snapshot/index-tree"
}

snapshot_worktree_equal() {
    cmp -s "$1/worktree.manifest" "$2/worktree.manifest"
}

snapshot_full_equal() {
    snapshot_metadata_equal "$1" "$2" && snapshot_worktree_equal "$1" "$2"
}

snapshot_boundary_equal() {
    before_snapshot=$1
    after_snapshot=$2
    snapshot_metadata_equal "$before_snapshot" "$after_snapshot" || return 1
    target_record_before=$(cat "$before_snapshot/target.record") || return 2
    target_record_after=$(cat "$after_snapshot/target.record") || return 2
    LC_ALL=C grep -Fvx "$target_record_before" "$before_snapshot/worktree.manifest" >"$runner_tmp/boundary-before" ||
        [ "$?" -eq 1 ] || return 2
    LC_ALL=C grep -Fvx "$target_record_after" "$after_snapshot/worktree.manifest" >"$runner_tmp/boundary-after" ||
        [ "$?" -eq 1 ] || return 2
    cmp -s "$runner_tmp/boundary-before" "$runner_tmp/boundary-after"
}

{
    cat "$prompt_path" || exit 1
    printf '\nREPO_ROOT: %s\nTARGET_FILE: %s\n' "$repo_root" "$target_file"
    if [ "$doc_type" = plan ]; then
        printf 'SPEC_FILE: %s\n' "$spec_file"
    fi
} >"$prompt_file" || fail 'cannot assemble prompt'

target_relative=${target_file#"$repo_root"/}
failure_step=baseline
baseline_one=$runner_tmp/baseline-one
baseline_two=$runner_tmp/baseline-two
snapshot_repo "$baseline_one" || fail 'cannot capture complete repository snapshot'
snapshot_repo "$baseline_two" || fail 'cannot verify complete repository snapshot'
snapshot_full_equal "$baseline_one" "$baseline_two" || fail 'repository changed while baseline was captured'
target_hash_before=$(git hash-object --no-filters -- "$target_file" 2>/dev/null) || fail 'cannot hash target before review'
target_mode_before=$(file_mode "$target_file" 2>/dev/null) || fail 'cannot read target mode before review'

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
target_states=$runner_tmp/target-states
printf '%s\n' "$target_hash_before" >"$target_states"
# POSIX sh offers no portable filesystem event API. This monitor samples once per
# second and records distinct target states; the provider-exit hash below closes
# the finalization window. A complete change-and-revert between samples cannot be
# attributed without extending the provider protocol, so this is deliberately a
# fail-closed heuristic rather than an authorship claim.
(
    target_monitor_last=$target_hash_before
    while kill -0 "$provider_pid" 2>/dev/null; do
        target_monitor_current=$(git hash-object --no-filters -- "$target_file" 2>/dev/null) || target_monitor_current=unavailable
        if [ "$target_monitor_current" != "$target_monitor_last" ]; then
            printf '%s\n' "$target_monitor_current" >>"$target_states"
            target_monitor_last=$target_monitor_current
        fi
        sleep '1'
    done
    target_monitor_current=$(git hash-object --no-filters -- "$target_file" 2>/dev/null) || target_monitor_current=unavailable
    if [ "$target_monitor_current" != "$target_monitor_last" ]; then
        printf '%s\n' "$target_monitor_current" >>"$target_states"
    fi
) &
target_monitor_pid=$!
timeout_marker=$runner_tmp/timed-out
(
    sleep 900
    printf '%s\n' timeout >"$timeout_marker"
    kill -TERM "$provider_pid" 2>/dev/null || :
    sleep 2
    kill -KILL "$provider_pid" 2>/dev/null || :
) &
watchdog_pid=$!
wait "$provider_pid"
provider_status=$?
provider_pid=
kill "$target_monitor_pid" 2>/dev/null || :
wait "$target_monitor_pid" 2>/dev/null || :
target_monitor_pid=
target_monitor_final=$(git hash-object --no-filters -- "$target_file" 2>/dev/null) || target_monitor_final=unavailable
target_monitor_last=$(tail -n 1 "$target_states") || target_monitor_last=
if [ "$target_monitor_final" != "$target_monitor_last" ]; then
    printf '%s\n' "$target_monitor_final" >>"$target_states"
fi
kill "$watchdog_pid" 2>/dev/null || :
wait "$watchdog_pid" 2>/dev/null || :
watchdog_pid=
if [ -f "$timeout_marker" ]; then
    printf '%s\n' 'provider: timed out after configured deadline (exit timeout)' >&2
    exit 124
fi
if [ "$provider_status" -ne 0 ]; then
    printf '%s\n' "provider: exited with status $provider_status" >&2
    exit "$provider_status"
fi

failure_step=boundary-check
target_state_count=$(LC_ALL=C sort -u "$target_states" | wc -l | tr -d '[:space:]') || fail 'cannot inspect target transitions'
[ "$target_state_count" -le 2 ] || fail 'target content changed through multiple states during provider execution'
after_one=$runner_tmp/after-one
after_two=$runner_tmp/after-two
snapshot_repo "$after_one" || fail 'cannot capture repository state after provider'
snapshot_repo "$after_two" || fail 'cannot verify repository state after provider'
snapshot_full_equal "$after_one" "$after_two" || fail 'repository changed while boundary was checked'
[ -f "$target_file" ] && [ ! -L "$target_file" ] || fail 'target type changed during review'
target_mode_after=$(file_mode "$target_file" 2>/dev/null) || fail 'cannot read target mode after review'
[ "$target_mode_before" = "$target_mode_after" ] || fail 'target mode changed during review'
target_hash_boundary=$(git hash-object --no-filters -- "$target_file" 2>/dev/null) || fail 'cannot hash target during boundary check'
[ "$target_monitor_final" = "$target_hash_boundary" ] || fail 'target content changed after provider completion'
snapshot_boundary_equal "$baseline_two" "$after_one"
boundary_status=$?
case $boundary_status in
    0) ;;
    1) fail 'provider changed state outside target worktree content' ;;
    *) fail 'cannot compare complete repository boundary' ;;
esac

failure_step=output-check
stdout_bytes=$(LC_ALL=C wc -c <"$stdout_file" | tr -d '[:space:]') || fail 'cannot measure provider stdout'
[ "$stdout_bytes" -le 65536 ] || fail 'provider stdout exceeds 64 KiB'
LC_ALL=C tr -d '\000' <"$stdout_file" >"$without_nul_file" || fail 'cannot inspect provider stdout'
without_nul_bytes=$(LC_ALL=C wc -c <"$without_nul_file" | tr -d '[:space:]') || fail 'cannot inspect provider stdout'
[ "$stdout_bytes" = "$without_nul_bytes" ] || fail 'provider stdout contains NUL'
LC_ALL=C tr -d '\n' <"$stdout_file" >"$without_lf_file" || fail 'cannot inspect provider stdout controls'
if LC_ALL=C grep '[[:cntrl:]]' "$without_lf_file" >/dev/null 2>&1; then
    fail 'provider stdout contains an unsafe ASCII control byte'
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

line_count=$(LC_ALL=C wc -l <"$normalized_file" | tr -d '[:space:]') || fail 'cannot count provider fields'
[ "$line_count" -eq 5 ] || fail 'provider stdout must contain exactly five lines'
LC_ALL=C awk '
    NR == 1 && /^状态: / { next }
    NR == 2 && /^修改数量: / { next }
    NR == 3 && /^修改摘要: / { next }
    NR == 4 && /^目标未达说明: / { next }
    NR == 5 && /^剩余风险: / { next }
    { exit 1 }
    END { if (NR != 5) exit 1 }
' "$normalized_file" || fail 'provider stdout fields must use the exact five-line schema order'

status=$(sed -n 's/^状态: //p' "$normalized_file")
count_line=$(sed -n 's/^修改数量: //p' "$normalized_file")
summary=$(sed -n 's/^修改摘要: //p' "$normalized_file")
unmet=$(sed -n 's/^目标未达说明: //p' "$normalized_file")
risk=$(sed -n 's/^剩余风险: //p' "$normalized_file")
case $status in approved|fixed) ;; *) fail 'invalid review status' ;; esac
case $count_line in
    *' 项') count=${count_line% 项} ;;
    *) fail 'invalid modification count' ;;
esac
case $count in ''|*[!0-9]*) fail 'invalid modification count' ;; esac
is_zero=$(awk -v n="$count" 'BEGIN { print (n + 0 == 0) ? "yes" : "no" }')
is_lt_twenty=$(awk -v n="$count" 'BEGIN { print (n + 0 < 20) ? "yes" : "no" }')

target_hash_after=$(git hash-object --no-filters -- "$target_file" 2>/dev/null) || fail 'cannot hash target after review'
if [ "$status" = approved ]; then
    [ "$is_zero" = yes ] || fail 'approved requires modification count zero'
    [ "$summary" = 无 ] || fail 'approved requires an empty modification summary'
    [ "$target_hash_before" = "$target_hash_after" ] || fail 'approved requires unchanged target content'
else
    [ "$is_zero" = no ] || fail 'fixed requires a positive modification count'
    [ "$summary" != 无 ] || fail 'fixed requires a modification summary'
    list_is_valid "$summary" 5 || fail 'modification summary must contain 1-5 nonempty items'
    [ "$target_hash_before" != "$target_hash_after" ] || fail 'fixed requires changed target content'
fi

if [ "$risk" != 无 ]; then
    list_is_valid "$risk" 3 || fail 'remaining risk must contain 1-3 nonempty items'
fi
if [ "$is_lt_twenty" = yes ]; then
    [ -n "$unmet" ] || fail 'a count below 20 requires an unmet-target explanation'
    [ "$unmet" != 无 ] || fail 'a count below 20 requires an unmet-target explanation'
else
    [ "$unmet" = 无 ] || fail 'a count of 20 or more requires unmet-target explanation to be 无'
fi

if [ -s "$stderr_file" ]; then
    printf '%s\n' 'provider: provider emitted diagnostic output' >&2
fi
cat "$normalized_file"

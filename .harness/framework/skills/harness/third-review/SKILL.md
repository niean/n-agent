---
name: third-review
description: Use when a completed spec or implementation plan requires an independent third-party review before its workflow can proceed.
---

# Third Review

Run an independent, provider-backed review after the document's built-in Review Loop. Keep provider mechanics in the runner and adapters; keep workflow and confirmation semantics here.

## Inputs

From the Git repository root, call `sh .harness/framework/scripts/get-config.sh thirdReview.enabled` before provider resolution. The capability is disabled when the normalized output is `false`, and enabled only when it is `true`. A nonzero getter exit is a validation failure. Do not read or parse `.harness/harness.json` directly.

Require:

- `doc_type`: exactly `spec` or `plan`.
- `target_file`: repository-relative Markdown path under `.harness/`, matching the current Task checkpoint.
- `review_loop_evidence`: current-Task evidence that the corresponding Review Loop completed and corrected this exact target.
- `spec_file`: required only for `plan`; it must be the distinct associated spec and evidence must show that its Phase 2 GATE was confirmed.

Accept optional non-empty `provider` and `model`. Resolve `provider` in this order: explicit Skill input, non-empty `HARNESS_THIRD_REVIEW_PROVIDER`, then the output of `get-config.sh thirdReview.provider`. Resolve `model` in the same order using `HARNESS_THIRD_REVIEW_MODEL` and `get-config.sh thirdReview.model`; an empty getter result is absent. Resolve timeout from valid `HARNESS_THIRD_REVIEW_TIMEOUT_SECONDS`, then `get-config.sh thirdReview.timeoutSeconds`. Reject unresolved or placeholder-valued provider configuration instead of assuming a built-in provider. Reject NUL or newline in model and reject NUL in any input. Treat empty optional values as absent. Evidence is invalid when it belongs to another Task, path, document revision, or retry.

## Execute

1. Resolve the project `enabled` switch through the getter. When disabled, return the `disabled` state immediately and do not resolve provider resources or invoke the runner. When enabled, validate all inputs and evidence before invoking the runner. For a retry, validate again and establish a new invocation identity; all earlier results and skip confirmations become stale.
2. From the Git repository root, call the getter separately for any needed provider/model/timeout configuration, resolve the input precedence above, and apply the resolved values only to this invocation through `HARNESS_THIRD_REVIEW_PROVIDER`, optional `HARNESS_THIRD_REVIEW_MODEL`, and `HARNESS_THIRD_REVIEW_TIMEOUT_SECONDS`. Any getter failure blocks the review as `validation`; do not source, execute, or independently parse `.harness/harness.json`. The runner validates and enforces the timeout (integer seconds, 1-86400) as the single deadline; provider adapters must not implement their own timeouts. Ensure any outer command timeout is not shorter than the runner deadline plus cleanup time.
3. Invoke the runner in the foreground and wait for its own exit status. The caller must not append `&` or pipe the command through `tail` or another consumer that replaces the runner exit status. The runner itself starts only the provider and watchdog as managed background children, redirects their output to runner-owned files or `/dev/null`, and explicitly waits for or terminates them before returning. Invoke:

   ```sh
   sh .harness/framework/skills/harness/third-review/scripts/run-review.sh spec <target_file>
   sh .harness/framework/skills/harness/third-review/scripts/run-review.sh plan <target_file> <spec_file>
   ```

4. The runner validates the target file, output size, unsafe control bytes, and the complete five-field provider summary. Provider nonzero exit, timeout/signal, or zero non-whitespace stdout is an execution failure; bounded provider stderr is emitted for diagnosis. Nonempty stdout that is safe but violates the five-field syntax or semantics is not an execution failure: the runner exits successfully, emits a `warning:` diagnostic, and returns the provider stdout unchanged instead of inferring `approved` or `fixed` from target-file hashes. Unsafe output (oversize, NUL, or unsafe control bytes) remains an `output-check` failure. For a plan the runner also verifies that the read-only associated spec did not change. On runner success, reread the target and relay the warning plus invalid provider output when present. For a plan, also reread the associated spec without modifying it. Perform the main review against the original request, Harness structure, Phase/GATE boundaries, spec coverage, executability, and verifiable acceptance. Do not trust provider output alone.
5. Return exactly one state below. Never advance the caller while awaiting confirmation.

The document prompts are `prompts/spec-review.md` and `prompts/plan-review.md`. The runner owns provider selection, path checks, target-file boundaries, timeouts, and the five-field provider output contract. It does not snapshot or monitor repository HEAD, Git index, or files outside the explicit target and associated spec.

## States

### Disabled

When project configuration does not enable Third Review, return:

```text
third_review: disabled
配置依据: get-config.sh thirdReview.enabled=false
```

This is a normal non-review state, not a failure or confirmed skip. The caller may continue its existing Phase sequence.

### Success

After runner success and main-review success, preserve a valid five-field summary and return:

```text
third_review: executed
provider: <provider-name>
状态: approved | fixed
修改数量: N 项
修改摘要: 1-5 条或无
目标未达说明: 无或具体说明
剩余风险: 无或1-3条
```

When runner success contains the invalid-summary warning, do not synthesize the five success fields. Relay the warning and safe provider stdout, then return:

```text
third_review: executed
provider: <provider-name>
warning: provider structured summary is invalid: <reason>
非法输出:
<provider-stdout-verbatim>
主流程复审: passed
```

### Failure

For validation, provider, boundary-check, unsafe output-check, or main-review failure, do not run an inline substitute and do not revert the worktree. Return only:

```text
third_review: awaiting-skip-confirmation
provider: <provider-name-or-unresolved>
失败步骤: <validation|provider|boundary-check|output-check|main-review>
失败原因: <sanitized-error>
失败命令: <command-without-secrets-or-not-applicable>
退出码: <integer|signal|timeout|not-applicable>
目标文件状态: <unchanged|changed|unknown>
```

End that response at `[CONFIRM]` and ask whether to skip this exact failed invocation. Do not include success-only fields.

### Confirmed skip

Accept a skip only when the immediately preceding user message explicitly confirms skipping the same document, provider invocation, and recorded failure, and the user has handled any damaged or out-of-bound state. Corrections, retry requests, vague acknowledgements, and confirmations for an earlier invocation do not qualify.

Return only:

```text
third_review: skipped
跳过原因: <reason bound to this failure>
确认依据: <summary of the immediately preceding user message>
```

Do not include provider result fields. A retry invalidates this confirmation and restarts the process from input validation.

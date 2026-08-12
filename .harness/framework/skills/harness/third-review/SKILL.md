---
name: third-review
description: Use when a completed spec or implementation plan requires an independent third-party review before its workflow can proceed.
---

# Third Review

Run an independent, provider-backed review after the document's built-in Review Loop. Keep provider mechanics in the runner and adapters; keep workflow and confirmation semantics here.

## Inputs

Require:

- `doc_type`: exactly `spec` or `plan`.
- `target_file`: repository-relative Markdown path under `.harness/`, matching the current Task checkpoint.
- `review_loop_evidence`: current-Task evidence that the corresponding Review Loop completed and corrected this exact target.
- `spec_file`: required only for `plan`; it must be the distinct associated spec and evidence must show that its Phase 2 GATE was confirmed.

Accept optional non-empty `provider` and `model`. Reject NUL or newline in model and reject NUL in any input. Treat empty optional values as absent. Evidence is invalid when it belongs to another Task, path, document revision, or retry.

## Execute

1. Validate all inputs and evidence before invoking a shell. For a retry, validate again and establish a new invocation identity; all earlier results and skip confirmations become stale.
2. From the Git repository root, apply explicit non-empty provider/model values only to this invocation through `HARNESS_THIRD_REVIEW_PROVIDER` and `HARNESS_THIRD_REVIEW_MODEL`.
3. Invoke:

   ```sh
   sh .harness/framework/skills/harness/third-review/scripts/run-review.sh spec <target_file>
   sh .harness/framework/skills/harness/third-review/scripts/run-review.sh plan <target_file> <spec_file>
   ```

4. On success, reread the target. For a plan, also reread the associated spec without modifying it. Perform the main review against the original request, Harness structure, Phase/GATE boundaries, spec coverage, executability, and verifiable acceptance. Do not trust provider output alone.
5. Return exactly one state below. Never advance the caller while awaiting confirmation.

The document prompts are `prompts/spec-review.md` and `prompts/plan-review.md`. The runner owns provider selection, path checks, worktree boundaries, timeouts, and the five-field provider output contract.

## States

### Success

After runner success and main-review success, preserve its five fields and return:

```text
third_review: executed
provider: <provider-name>
状态: approved | fixed
修改数量: N 项
修改摘要: 1-5 条或无
目标未达说明: 无或具体说明
剩余风险: 无或1-3条
```

### Failure

For validation, baseline, provider, boundary-check, output-check, or main-review failure, do not run an inline substitute and do not revert the worktree. Return only:

```text
third_review: awaiting-skip-confirmation
provider: <provider-name-or-unresolved>
失败步骤: <validation|baseline|provider|boundary-check|output-check|main-review>
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

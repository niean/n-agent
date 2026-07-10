---
name: writing-verify
description: Use after an implementation plan is approved, before touching code, to generate a stable manual acceptance checklist
---

# Writing Verify

## Overview

Write a standalone manual acceptance checklist for end-to-end human or semi-human verification. The file is created after the plan is written and reviewed, before code implementation starts.

Announce at start: "I'm using the writing-verify skill to create the manual acceptance checklist."

Save verify files to: `.harness/specs/verify/verify-{YYMMDD}-{desc}.md`
- Use the same `{YYMMDD}-{desc}` suffix as the linked spec and plan.
- User preferences for verify location override this default.

## Inputs

| Input | Required | Notes |
|-------|----------|-------|
| spec_file | yes | `.harness/specs/active/spec-{YYMMDD}-{desc}.md` |
| plan_file | yes | `.harness/plans/active/plan-{YYMMDD}-{desc}.md` |
| verify_file | no | Defaults to `.harness/specs/verify/verify-{YYMMDD}-{desc}.md` |

## Scope Rules

- Include only end-to-end manual or semi-manual acceptance items.
- Exclude unit tests, integration tests, static checks, build checks, and any item already covered by automatic verification in the plan.
- Prefer real user-visible workflows, public commands, UI paths, protocol entrypoints, and observable persistence or side effects.
- Do not include implementation-internal checks unless the only reliable verification is semi-manual inspection of logs, database records, or external service state.
- Each item must be executable by a human without reading source code.
- Each item must have clear setup, action, and expected result.
- The total number of checkbox acceptance items must be 10 or fewer.
- If more than 10 manual workflows appear necessary, keep the highest-risk and highest-value end-to-end workflows, and merge closely related checks without making them vague.
- Do not invent capabilities that are not present in the spec or plan.
- If a required manual verification is blocked by missing environment, credentials, external account, hardware, or human action, keep the item and mark the setup as required; do not silently omit it.

## File Template

Every verify file must use this exact section order:

```markdown
<!-- SUMMARY: {feature} 人工端到端验收清单，覆盖 {major workflows} -->
# 验收清单：{Feature Name}（人工端到端部分）

对应 spec：spec-{YYMMDD}-{desc}.md
对应 plan：plan-{YYMMDD}-{desc}.md

仅保留必须人工端到端验证的项。可脚本化/已自动化的项（{automatic coverage summary}）由自动化测试覆盖，不在本清单。

前置准备：{environment, account, service, fixture data, command access, or external dependency required before verification}。

---

## 1. {Group Name}

- [ ] 1.1 {entrypoint or workflow}：{human action} -> {observable expected result}
- [ ] 1.2 {entrypoint or workflow}：{human action} -> {observable expected result}
```

## Stable Output Rules

- Use numbered group headings and checkbox items. Each checkbox is one acceptance item.
- Keep the whole checklist to at most 10 checkbox items.
- Group by user entrypoint or workflow, not by code module.
- Keep each acceptance item independent. A failed item should identify one workflow gap.
- Use plain text only. Do not use emoji, bold, italic, decorative icons, or visual emphasis.
- Use project-root-relative paths only. Do not use absolute paths.
- Use exact commands, URLs, UI names, protocol names, and payload examples when the plan provides them.
- If the plan uses placeholders, keep the placeholder explicit and human-readable, for example `{session_id}`.
- Keep expected results observable: UI content, command output, HTTP status/body, protocol response, database row, log line, or external service state.
- If an item requires semi-manual inspection, name the inspection target and expected evidence in the same checkbox line.
- Avoid broad items such as "feature works" or "no regression". Split them into concrete workflows.
- Avoid field-style items such as 场景/前置/操作/预期. Put the action and expected result in one readable checkbox line.
- Use checked boxes `[x]` only when the item has already been manually verified in the current task. New checklists should default to `[ ]`.

## Generation Steps

1. Read the complete spec and plan.
2. Extract all acceptance criteria from the spec.
3. Extract all implementation tasks, automatic tests, commands, and verification steps from the plan.
4. Remove items covered by automatic verification unless a human-visible end-to-end workflow still needs manual confirmation.
5. Group remaining manual or semi-manual workflows by entrypoint.
6. If the grouped checklist exceeds 10 checkbox items, reduce it to 10 or fewer by prioritizing Blocking-level user-visible workflows, externally observable persistence/side effects, and cross-entrypoint integration paths.
7. Write the verify file using the template.
8. Validate the result:
   - verify file exists at the expected path.
   - verify file contains only manual or semi-manual items.
   - verify file contains 10 or fewer checkbox acceptance items.
   - each acceptance item is a checkbox line.
   - each checkbox line contains both human action and observable expected result.
   - verify file uses spec/plan filenames only, without `active/` or `completed/` directory paths.
   - plan file is not modified by this skill.

## Review Loop

After writing the verify file:

1. Re-read the spec, plan, and verify file.
2. Check for missing manual workflows, automatic-only items, vague expected results, and stale paths.
3. Count checkbox acceptance items and reduce to 10 or fewer if needed.
4. Fix all issues before continuing to implementation.
5. If uncertainty remains about whether an item is manual or automatic, keep it only when a human-visible end-to-end result must still be confirmed.

---
name: writing-verify
description: Use after an implementation plan is approved, before touching code, to generate a stable manual acceptance checklist
---

# Writing Verify

## Overview

Write a standalone manual acceptance file for end-to-end human or semi-human verification. The file is created after the plan is written and reviewed, before code implementation starts.

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
- Do not invent capabilities that are not present in the spec or plan.
- If a required manual verification is blocked by missing environment, credentials, external account, hardware, or human action, keep the item and mark the setup as required; do not silently omit it.

## File Template

Every verify file must use this exact section order:

```markdown
# {Feature Name} Manual Verify

- 创建时间: YYYY-MM-DD HH:MM
- 状态: active | completed
- 关联 spec: spec-{YYMMDD}-{desc}.md
- 关联 plan: plan-{YYMMDD}-{desc}.md

## 验收范围
- {one end-to-end capability or workflow}

## 验收前置
- {environment, account, service, fixture data, or command required before verification}

## 验收项

### {Group Name}
- 场景: {what user workflow is being verified}
  前置: {specific prerequisite for this item, or "无"}
  操作: {exact human or semi-human steps}
  预期: {observable expected result}

## 不纳入人工验收
- {automatic check or internal behavior intentionally excluded}: {reason}

## 变更记录
| 时间 | 变更内容 |
|------|---------|
```

## Stable Output Rules

- Use grouped lists under `## 验收项`.
- Group by user entrypoint or workflow, not by code module.
- Keep each acceptance item independent. A failed item should identify one workflow gap.
- Use plain text only. Do not use emoji, bold, italic, decorative icons, or visual emphasis.
- Use project-root-relative paths only. Do not use absolute paths.
- Use exact commands, URLs, UI names, protocol names, and payload examples when the plan provides them.
- If the plan uses placeholders, keep the placeholder explicit and human-readable, for example `{session_id}`.
- Keep expected results observable: UI content, command output, HTTP status/body, protocol response, database row, log line, or external service state.
- If an item requires semi-manual inspection, name the inspection target and expected evidence.
- Avoid broad items such as "feature works" or "no regression". Split them into concrete workflows.

## Generation Steps

1. Read the complete spec and plan.
2. Extract all acceptance criteria from the spec.
3. Extract all implementation tasks, automatic tests, commands, and verification steps from the plan.
4. Remove items covered by automatic verification unless a human-visible end-to-end workflow still needs manual confirmation.
5. Group remaining manual or semi-manual workflows by entrypoint.
6. Write the verify file using the template.
7. Validate the result:
   - verify file exists at the expected path.
   - verify file contains only manual or semi-manual items.
   - each item has 场景, 前置, 操作, 预期.
   - verify file uses spec/plan filenames only, without `active/` or `completed/` directory paths.
   - plan file is not modified by this skill.

## Review Loop

After writing the verify file:

1. Re-read the spec, plan, and verify file.
2. Check for missing manual workflows, automatic-only items, vague expected results, and stale paths.
3. Fix all issues before continuing to implementation.
4. If uncertainty remains about whether an item is manual or automatic, keep it only when a human-visible end-to-end result must still be confirmed.

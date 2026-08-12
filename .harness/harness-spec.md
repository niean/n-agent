<!-- SUMMARY: Harness Third Review 能力分层规格：Third Review 定义为通用 Skill，具体模型定义为 provider 适配器，after-finish 保持为项目 Hook -->
# Harness Third Review 能力分层规格

## 1. 背景

当前 Third Review 在 `iterate-feature` Workflow 的 spec 和 plan 产出后执行，用于引入第三方模型审阅并修正目标文件。现有实现将通用审阅流程、Codex 执行方式和 Hook 扩展机制混合，导致 Workflow 与 ChatGPT Mac/Codex 细节耦合，也使 Third Review 的能力归属不清晰。

本规格确定 Third Review、provider 和 Hook 的最终分层，作为后续 Harness 重构的设计依据。本文档只定义目标架构，不表示当前实现已完成迁移。

## 2. 设计结论

1. Third Review 是 Harness 的通用 Skill，不是 Hook。
2. `iterate-feature` Workflow 负责决定 Third Review Skill 的调用时机，不直接调用具体脚本或模型。
3. Codex、Claude、Gemini、HTTP API 等是 Third Review Skill 的 provider 适配器，不是 Third Review 能力本身。
4. Hook 用于项目对 Workflow 扩展点的特化实现。`after-finish` 属于 Hook，Third Review 不属于 Hook。
5. 项目 Hook 应位于 `.harness/hooks/`，不应写入 `.harness/framework/`。
6. `.harness/framework/` 只保存可跨项目复用、可独立升级的 Workflow、Skill、Agent 和 provider 适配器。

## 3. 能力分类

| 类型 | 核心问题 | 责任 | Third Review 中的对应物 |
|------|----------|------|-------------------------|
| Workflow | 何时做、按什么顺序做 | 编排 Phase、GATE 和 Skill 调用 | `iterate-feature` 在 Phase 2/3 调用三方审阅 |
| Skill | 做什么、怎么做 | 定义可复用步骤、输入、输出和确认点 | Third Review 的完整审阅流程 |
| Provider | 通过什么工具执行 | 封装模型、CLI/API、凭据和会话策略 | Codex/Claude/Gemini/API 适配器 |
| Hook | 项目在扩展点额外做什么 | 执行项目特化操作 | `after-finish.sh` 重启本项目服务 |

Third Review 的审阅时机和步骤在不同项目间一致，因此属于 Skill。选择哪个外部模型是执行细节，因此属于 provider。`after-finish` 是否执行以及执行什么取决于项目，因此属于 Hook。

## 4. 目标目录结构

```text
.harness/
  hooks/
    after-finish.sh

  framework/
    workflows/
      iterate-feature.md
    skills/
      harness/
        third-review/
          SKILL.md
          prompts/
            spec-review.md
            plan-review.md
          scripts/
            run-review.sh
          providers/
            codex.sh
```

目录边界：

- `.harness/hooks/after-finish.sh` 是项目特化文件，可不存在。
- `third-review/SKILL.md` 是通用能力定义和唯一流程权威源。
- `prompts/` 保存文档类型相关的通用审阅约束。
- `scripts/run-review.sh` 提供稳定的 provider 调用协议，不包含某个 provider 的专属路径、参数或凭据。
- `providers/` 保存可替换的执行器适配器。新增 provider 不得要求修改 Workflow。
- 如项目需要选择 provider 或模型，应通过 `.harness/PROJECT.md` 的项目配置或运行环境传入，不得将项目选择固化到 Workflow。

## 5. Third Review Skill 合同

### 5.1 显示名和调用方

- Skill 显示名：三方审阅
- Skill 调用方：`iterate-feature` Workflow
- Skill 不自行决定触发时机，不新增 Phase，不改变 GATE 边界

### 5.2 输入

| 参数 | 必需 | 约束 |
|------|------|------|
| `doc_type` | 是 | 只允许 `spec` 或 `plan` |
| `target_file` | 是 | 目标文件必须存在，且必须是当前 Task 的 spec 或 plan |
| `spec_file` | plan 必需 | plan 审阅的关联 spec；spec 审阅不传 |
| `provider` | 否 | 显式指定的执行适配器；未指定时使用项目配置 |
| `model` | 否 | 透传给 provider；Workflow 不解释其语义 |

### 5.3 执行步骤

1. 校验参数、目标文件和关联 spec。
2. 确认对应的 Spec Review Loop 或 Plan Review Loop 已完成，且目标文件已按内联审阅结果修正。
3. 记录 provider 执行前的允许变更边界，保护现有脏工作树和用户变更。
4. 按 `doc_type` 组装通用审阅 Prompt，调用选定的 provider。
5. 验证 provider 只修改 `target_file`。plan 审阅中 `spec_file` 始终只读。发现越界修改时不得自动覆盖或回滚用户变更，应立即报告并等待人工处理。
6. 校验审阅摘要包含具体的 `修改数量: N 项`，禁止编造数量或使用 `20+` 代替真实数量。
7. 主流程模型重新读取目标文件，复审用户原始需求、Harness 模板、Phase/GATE 边界、spec/plan 一致性和可验证性。
8. 输出结构化结果，将控制权交回 Workflow。

### 5.4 审阅强度

- spec/plan 审阅以发现并修复 20 个以上真实问题为强度目标。
- 实际问题不足 20 个时不得编造，必须说明未达到目标的原因和已覆盖的审阅维度。
- spec 优先审阅需求歧义、范围、架构边界、错误处理和验收可执行性。
- plan 优先审阅 spec 覆盖、任务依赖、真实文件路径、接口签名、测试可执行性和验收追踪。

### 5.5 输出

Skill 必须产出：

```text
third_review: executed | skipped | awaiting-skip-confirmation
provider: <provider-name>
状态: approved | fixed
修改数量: N 项
修改摘要: 1-5 条
剩余风险: 无或 1-3 条
```

`provider` 只用于诊断和可观测性。Workflow 检查点只依赖 `third_review` 状态，不得使用 `chatgpt`、`codex` 等 provider 名称表示流程状态。

## 6. Workflow 编排合同

### 6.1 Phase 2 spec 审阅

```text
Skill: brainstorming Spec Review Loop
  -> Skill: 三方审阅(doc_type=spec, target_file=<spec_file>)
  -> 输出需求摘要
  -> Phase 2 GATE
```

Third Review Skill 内部已包含主流程模型复审，Workflow 不重复定义 provider 调用和复审细节。

### 6.2 Phase 3 plan 审阅

```text
Skill: writing-plans Plan Review Loop
  -> Skill: 三方审阅(doc_type=plan, target_file=<plan_file>, spec_file=<spec_file>)
  -> Skill: writing-verify
  -> Phase 4
```

Third Review 不新增 Phase，不为 Phase 3 新增 GATE，不替代 Spec Review Loop 或 Plan Review Loop。

## 7. 失败和跳过语义

Third Review 是 `iterate-feature` 中的必选 Skill，不使用可选 Hook 的缺失语义。

以下情况均视为 Third Review 失败：

- Skill 定义或必需资源不存在。
- provider 未配置、不可用或指定模型不可用。
- provider 返回非零退出码。
- provider 修改了目标文件之外的文件。
- 审阅输出缺失必需字段或修改数量不可验证。
- 主流程模型复审发现 Third Review 破坏原始需求或 Harness 边界。

失败时的固定行为：

1. 立即停止当前 Skill 和 Workflow 推进。
2. 输出失败步骤、provider、命令（如适用）和退出码。
3. 将状态设为 `third_review: awaiting-skip-confirmation`。
4. 通过 `[CONFIRM]` 结束当前回复，请求人工确认是否跳过。
5. 仅在上一条用户消息明确确认跳过后，才设为 `third_review: skipped` 并继续 Workflow。
6. 跳过后不执行额外内联降级审阅。

## 8. Hook 边界

### 8.1 Hook 的定义

Hook 是 Workflow 在明确扩展点对项目脚本的回调。Hook 定义的是项目额外行为，不承载 Workflow 必需的通用能力。

### 8.2 after-finish Hook

- 项目路径：`.harness/hooks/after-finish.sh`
- 调用时机：Workflow 最后一个 Phase 成功结束后
- 文件不存在：视为未定义，正常跳过
- 执行失败：不回滚已完成 Phase，报告命令和退出码
- 项目示例：执行 `docker/restart.sh` 重建并重启服务

### 8.3 禁止的 Hook 用法

- 不得将 Third Review、结果验收、知识回填等 Workflow 必需能力通过项目 Hook 实现。
- 不得将项目绝对路径、项目服务命令或项目凭据写入 `.harness/framework/`。
- 不得因 Hook 存在而修改 Workflow 的 Phase/GATE 语义。

## 9. 当前过渡状态

当前工作树中的实现为过渡方案：

- `iterate-feature.md` 直接调用 `.harness/framework/hooks/third-review.sh`。
- `.harness/framework/hooks/third-review.sh` 委托 `.harness/framework/third/third-review-codex.sh`。
- Third Review 规则分散在 `FRAMEWORK.md`、Workflow、Prompt 和 shell 脚本中。
- `after-finish.sh` 位于 `.harness/framework/hooks/`，但其内容依赖本项目的 `docker/restart.sh`。

过渡实现不作为最终分层的权威定义；本规格是后续迁移的设计权威源。

## 10. 后续迁移范围

后续实现任务应一次性完成以下迁移，避免长期保留两套入口：

1. 新增 `Skill: 三方审阅` 及其 Prompt、runner 和 provider 目录。
2. 将现有 spec/plan Prompt 迁入 Skill 资源目录。
3. 将 Codex 调用逻辑迁入 `providers/codex.sh`，通用参数校验和 Prompt 组装下沉到 Skill runner。
4. 将 `iterate-feature` Phase 2/3 改为调用 `Skill: 三方审阅`，删除 provider 命令和重复失败规则。
5. 在 `FRAMEWORK.md` Skills 注册表和目录结构中注册三方审阅。
6. 删除 `.harness/framework/hooks/third-review.sh` 及为跟踪它而新增的 ignore 例外。
7. 将 `after-finish.sh` 迁移到 `.harness/hooks/after-finish.sh`，同步更新适用 Workflow 的调用路径。
8. 全量搜索并清理 `third-review Hook`、`HARNESS_THIRD_REVIEW_CMD`、Workflow 中的 Codex/ChatGPT 耦合和 provider 状态名。
9. 为 Skill 覆盖 spec/plan 输入、provider 不可用、非零退出码、越界修改、脏工作树保护、输出格式和跳过确认。

## 11. 验收标准

后续迁移完成时必须满足：

1. Workflow 中不存在 Codex、ChatGPT、Claude、Gemini 或 provider 专属环境变量。
2. Workflow 只通过 `Skill: 三方审阅` 执行 Third Review。
3. 替换 provider 不需要修改 Workflow 或 Third Review 流程定义。
4. Third Review Skill 不新增 Phase/GATE，且保留失败后人工确认才能跳过的语义。
5. provider 只能修改目标 spec/plan，plan 审阅不得修改 spec。
6. 主流程模型在 provider 完成后重新读取并复审目标文件。
7. 审阅摘要输出真实的 `修改数量: N 项`。
8. `third_review` 检查点状态与 provider 无关。
9. 项目特化 `after-finish.sh` 位于 `.harness/hooks/`，`.harness/framework/` 不包含项目重启命令。
10. 现有 spec/plan 审阅顺序、强度、输出和失败语义无回归。

## 12. 非目标

- 不将 Third Review 拆成新 Workflow 或新 Phase。
- 不用 Third Review 替代 brainstorming/writing-plans 自带的 Review Loop。
- 不用 Third Review 替代主流程模型的最终复审责任。
- 不要求所有项目使用同一 provider、模型或会话持久化方式。
- 不在 `.harness` 下为单次审阅新建对话记录文件或目录。

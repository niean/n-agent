<!-- SUMMARY: Harness Third Review 能力分层规格：Third Review 定义为通用 Skill，具体模型定义为 provider 适配器，after-finish 保持为项目 Hook -->
# Harness Third Review 能力分层规格

## 1. 元信息

| 字段 | 值 |
|------|----|
| 状态 | 已迁移，合同测试通过 |
| 适用范围 | Harness framework 与项目级 Hook 的能力分层 |
| 目标 Workflow | `iterate-feature` |
| 兼容环境 | Claude Code、Codex 及其它可运行 POSIX shell 的环境 |
| 权威边界 | 本规格定义已迁移实现的目标合同；`third-review/SKILL.md` 是流程运行时权威源 |

## 2. 背景

当前 Third Review 在 `iterate-feature` Workflow 的 spec 和 plan 产出后执行，用于引入第三方模型审阅并修正目标文件。现有实现将通用审阅流程、Codex 执行方式和 Hook 扩展机制混合，导致 Workflow 与 ChatGPT Mac/Codex 细节耦合，也使 Third Review 的能力归属不清晰。

本规格确定 Third Review、provider 和 Hook 的最终分层，作为后续 Harness 重构的设计依据。本文档只定义目标架构，不表示当前实现已完成迁移；在一次迁移中必须完成调用入口切换和旧入口清理，不允许两套入口同时生效。

## 3. Goal

将 Third Review 建模为可由 Workflow 调用、与具体模型和执行工具解耦的通用 Skill，同时保持现有 spec/plan 审阅顺序、Phase/GATE 边界、失败后人工确认才能跳过的语义，以及“provider 只可修改目标文件”的安全边界。项目特化的 `after-finish` 继续作为可选 Hook，并迁出通用 framework。

迁移成功的核心结果是：Workflow 只表达何时调用三方审阅，Skill 独占审阅流程合同，runner 负责安全地选择和调用 provider，provider 只负责执行外部模型。

## 4. 范围

### 4.1 范围内

- Third Review Skill、spec/plan Prompt、runner 和首个 Codex provider 的目标合同与目录归属。
- `iterate-feature` Phase 2/3 对 Third Review 的调用方式和失败/跳过状态。
- `after-finish` 项目 Hook 的目标路径，以及当前声明使用该 Hook 的 Workflow 的调用路径。
- 原 Third Review Hook、旧 Prompt/provider 入口和现行规则引用的单次切换边界。
- 输入校验、工作树保护、输出校验、错误处理和可执行验收标准。

### 4.2 非目标

- 不将 Third Review 拆成新 Workflow 或新 Phase。
- 不用 Third Review 替代 brainstorming/writing-plans 自带的 Review Loop。
- 不用 Third Review 替代主流程模型的最终复审责任。
- 不要求所有项目使用同一 provider、模型或会话持久化方式。
- 不定义各 provider 的模型质量、计费、凭据签发或安装流程。
- 不为并发编辑提供合并或自动回滚能力；runner 对执行期间可观察到的并发变化按失败处理，但不保证识别采样窗口内修改后恢复等不可观察竞争。
- 不在 `.harness` 下为单次审阅新建对话记录、日志、备份或临时目录。

## 5. 设计结论

1. Third Review 是 Harness 的通用 Skill，不是 Hook。
2. `iterate-feature` Workflow 负责决定 Third Review Skill 的调用时机，不直接调用具体脚本、模型或 provider 环境变量。
3. Codex、Claude、Gemini、HTTP API 等是 Third Review Skill 的 provider 适配器，不是 Third Review 能力本身。
4. Hook 用于项目对 Workflow 扩展点的特化实现。`after-finish` 属于 Hook，Third Review 不属于 Hook。
5. 项目 Hook 位于 `.harness/hooks/`，不得写入 `.harness/framework/`。
6. `.harness/framework/` 只保存可跨项目复用、可独立升级的 Workflow、Skill、Agent 和 provider 适配器。
7. `third-review/SKILL.md` 是 Third Review 流程的唯一权威源；Prompt 只定义文档类型审阅要求，runner/provider 不得复制流程和 GATE 规则。

## 6. Architecture

### 6.1 能力分类

| 类型 | 核心问题 | 责任 | Third Review 中的对应物 |
|------|----------|------|-------------------------|
| Workflow | 何时做、按什么顺序做 | 编排 Phase、GATE 和 Skill 调用 | `iterate-feature` 在 Phase 2/3 调用三方审阅 |
| Skill | 做什么、怎么做 | 定义输入、步骤、状态、失败和确认点 | Third Review 的完整审阅流程 |
| Runner | 如何安全调度执行器 | 校验输入、解析配置、组装 Prompt、保护变更边界、校验 provider 结果 | `scripts/run-review.sh` |
| Provider | 通过什么工具执行 | 封装模型、CLI/API、凭据和会话策略 | Codex/Claude/Gemini/API 适配器 |
| Hook | 项目在扩展点额外做什么 | 执行项目特化操作 | `after-finish.sh` 重启本项目服务 |

Third Review 的审阅时机和步骤在不同项目间一致，因此属于 Skill。选择哪个外部模型是执行细节，因此属于 provider。`after-finish` 是否执行以及执行什么取决于项目，因此属于 Hook。

### 6.2 目标目录结构

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
- `third-review/SKILL.md` 定义调用前置条件、执行步骤、状态机和 `[CONFIRM]`。
- `prompts/` 保存文档类型相关的通用审阅约束，不包含 provider 命令、凭据或 IDE 路径。
- `scripts/run-review.sh` 提供稳定的 provider 选择、Prompt 组装、变更边界校验和结果捕获，不包含某个 provider 的专属路径、参数或凭据。
- `providers/` 保存可替换的执行器适配器。新增 provider 不得要求修改 Workflow、Skill 流程或通用 Prompt。

## 7. Components

### 7.1 Skill 合同

- 显示名：三方审阅。
- 注册位置：`FRAMEWORK.md` Skills 注册表及 framework 目录结构。
- 调用方：`iterate-feature` Workflow。
- Skill 不自行决定触发时机，不新增 Phase，不改变 GATE 边界。
- Skill 负责调用前置条件、runner 结果解释、主流程复审、失败确认和最终结构化输出。

### 7.2 输入

| 参数 | 必需 | 约束 |
|------|------|------|
| `doc_type` | 是 | 枚举值，仅允许 `spec` 或 `plan` |
| `target_file` | 是 | 项目根目录相对的 Markdown 路径；默认 spec 位于 `.harness/specs/active/`、plan 位于 `.harness/plans/active/`，但允许用户明确指定的 `.harness/` 内非受保护路径覆盖默认位置；必须与当前 Task 检查点记录的文件一致 |
| `spec_file` | plan 必需 | 遵守与 spec `target_file` 相同的路径规则，必须是当前 plan 关联且已由用户通过 Phase 2 GATE 的 spec；spec 审阅时禁止传入 |
| `provider` | 否 | 安全的 kebab-case 标识符；空字符串视为未提供 |
| `model` | 否 | 不含换行或 NUL 的不透明字符串；空字符串视为未提供，由 provider 解释 |
| `review_loop_evidence` | 是 | Workflow 传入的当前 Task 检查点，证明对应 Spec/Plan Review Loop 已完成并已修正目标文件 |

runner 必须在项目 Git 根目录执行。所有文件路径先按项目根目录解析，再取 canonical path；解析结果必须仍位于 `.harness/` 内，且必须是 `.md` 普通文件。`.harness/framework/`、`.harness/prd/`、`.harness/knowledge/`、`.harness/lessons/` 始终禁止作为审阅目标。绝对路径、`..` 逃逸、目录、设备文件和借助 symlink 逃逸均拒绝。plan 审阅的 `target_file` 与 `spec_file` 必须不同。Skill 还必须用 `review_loop_evidence` 校验路径属于当前 Task，runner 的目录检查不替代该业务校验。

### 7.3 provider 和模型选择

- provider 选择优先级：Skill 非空显式输入 `provider` -> 非空 `HARNESS_THIRD_REVIEW_PROVIDER` -> 内置默认值 `codex`。
- 模型选择优先级：Skill 非空显式输入 `model` -> 非空 `HARNESS_THIRD_REVIEW_MODEL` -> provider 默认模型。
- provider 名必须匹配 `^[a-z0-9]+(-[a-z0-9]+)*$`。runner 只能从 `third-review/providers/<provider>.sh` 解析适配器，不得执行路径片段或任意命令字符串。
- `model` 只能作为单个参数或环境变量值透传，禁止通过 `eval`、shell 拼接或二次解释执行。
- 显式输入只对本次调用生效；Skill 不得修改调用进程的持久环境或项目配置。

### 7.4 runner-provider 协议

Skill 从项目根目录调用稳定入口：

```text
sh .harness/framework/skills/harness/third-review/scripts/run-review.sh spec <spec_file>
sh .harness/framework/skills/harness/third-review/scripts/run-review.sh plan <plan_file> <spec_file>
```

runner 校验参数、解析 provider、选择 Prompt，并将仓库根目录、规范化后的目标路径和关联 spec 路径组装进完整 Prompt。随后以脚本自身位置解析 provider 的绝对路径并调用：

```text
HARNESS_THIRD_REVIEW_MODEL=<optional-model> \
  sh <canonical-provider-script> <repo_root> < rendered-prompt
```

provider 协议：

- 位置参数 1 是 canonical 项目根目录，且仅允许这一个位置参数。
- stdin 是完整审阅 Prompt；provider 不得拼接或覆盖 spec/plan 业务规则。
- `HARNESS_THIRD_REVIEW_MODEL` 可选；provider 自行将其映射为 CLI/API 参数。
- provider 可按 Prompt 要求修改规范化后的 `target_file`，不得修改其它路径。
- provider 将最终审阅摘要写到 stdout，诊断写到 stderr；不得把凭据、完整环境变量或目标文档全文写入诊断。
- 成功返回 0，不可用、超时、收到信号或执行失败返回非 0。runner 保留原始 provider 退出信息供 Skill 诊断。
- provider 专属二进制路径、凭据、sandbox 参数和会话策略只能存在于适配器及其环境变量中；通用层不得依赖 ChatGPT Mac、特定 IDE 或固定安装路径。

### 7.5 provider 输出合同

provider 成功时 stdout 去除首尾空白后必须恰好包含以下字段；字段名各出现一次，禁止在字段前后输出说明、Markdown 围栏或日志：

```text
状态: approved | fixed
修改数量: N 项
修改摘要: 1-5 条；approved 时填写“无”
目标未达说明: 无 | <N 小于 20 的原因和已覆盖审阅维度>
剩余风险: 无 | 1-3 条
```

约束如下：

- `N` 是十进制非负整数，不允许符号、范围、近似值或 `20+`。
- `approved` 仅允许 `N = 0`，并表示目标文件内容未变化。
- `fixed` 要求 `N >= 1`，并表示目标文件内容发生变化。
- `N < 20` 时 `目标未达说明` 不得为“无”，且必须同时说明真实问题不足 20 的原因和已覆盖维度；`N >= 20` 时该字段必须为“无”。
- 修改数量是 provider 按 Prompt 计数规则作出的语义声明。runner 不从文本 diff 推导语义数量，但必须拒绝状态、数量与文件是否变化之间的明显矛盾。
- runner 在受限临时目录捕获 stdout/stderr；stdout 超过 64 KiB、包含 NUL、缺字段、重复字段或格式不符均视为输出校验失败。临时文件在本次调用结束时清理，不写入 `.harness`。

## 8. Data Flow

1. Workflow 完成对应的 Spec Review Loop 或 Plan Review Loop，将当前 Task 文件路径和 `review_loop_evidence` 传给 Skill。
2. Skill 校验调用上下文，再调用 runner；缺少可信检查点时不得只凭目标文件内容推断 Review Loop 已完成。
3. runner 确认 Git 根目录，规范化输入路径，解析 provider/model，并在调用前采集工作树基线。
4. 基线至少包含 tracked 文件的 staged/unstaged 状态与内容散列、untracked 文件路径与内容散列、文件类型和 mode；另存 `target_file` 的执行前内容散列。散列内容仅保存在系统临时目录。
5. runner 组装 Prompt 并调用 provider，同时捕获 stdout、stderr、退出码和信号/超时状态。
6. provider 返回后，runner 重新采集同一范围并比较。除 `target_file` 内容变化外，任何新增、删除、重命名、mode 变化、staged 状态变化或其它文件内容变化均为越界。
7. runner 校验 provider 输出以及 `approved/fixed`、修改数量和 `target_file` 实际变化的一致性，将原始结果返回 Skill。
8. Skill 重新读取目标文件。spec 按用户原始需求、Harness spec 结构、Phase/GATE 和验收可执行性复审；plan 还必须只读关联 spec 并复审覆盖关系。
9. Skill 复审通过后输出 `third_review: executed`，Workflow 按原 Phase 顺序继续；任一步失败则进入等待跳过确认状态。

工作树保护以调用前后差异为准，允许调用前已经存在的用户变更保持不变。若 provider 运行期间出现 runner 可观察到且无法归因的并发变化，包括采样检测到的 `target_file` 多次状态转换，必须 fail-closed。并发检测是 best-effort：受当前 provider 直接编辑目标文件的协议限制，不保证识别完全发生在采样窗口内且最终恢复原状态的竞争；此类漏检是已接受风险。runner 和 Skill 不得自动覆盖、删除、stash、reset、checkout 或回滚任何文件；失败时保留现场并明确报告目标文件是否已变化，由人工处理。

## 9. Workflow 编排合同

### 9.1 Phase 2 spec 审阅

```text
Skill: brainstorming Spec Review Loop
  -> Skill: 三方审阅(doc_type=spec, target_file=<spec_file>, review_loop_evidence=<checkpoint>)
  -> 输出需求摘要
  -> Phase 2 GATE
```

Third Review Skill 内部包含主流程模型复审。Workflow 不重复定义 provider 调用、输出解析和失败细节。Third Review 成功或经用户明确确认跳过后，Phase 2 才能输出需求摘要并进入原有 GATE。

### 9.2 Phase 3 plan 审阅

```text
Skill: writing-plans Plan Review Loop
  -> Skill: 三方审阅(doc_type=plan, target_file=<plan_file>, spec_file=<spec_file>, review_loop_evidence=<checkpoint>)
  -> Skill: writing-verify
  -> 按既有规则选择执行方式
  -> Phase 4
```

Third Review 不新增 Phase，不为 Phase 3 新增 GATE，不替代 Spec Review Loop、Plan Review Loop 或 `writing-verify`。Phase 3 的 `[GATE-ENTRY]` 仍要求上一条用户消息已明确确认 Phase 2 spec，且 Phase 3 完成后不得为执行方式额外等待确认。

### 9.3 Skill 结果

`executed` 必需字段：

```text
third_review: executed
provider: <provider-name>
状态: approved | fixed
修改数量: N 项
修改摘要: 1-5 条或无
目标未达说明: 无或具体说明
剩余风险: 无或1-3条
```

`awaiting-skip-confirmation` 必需字段：

```text
third_review: awaiting-skip-confirmation
provider: <provider-name-or-unresolved>
失败步骤: <validation|baseline|provider|boundary-check|output-check|main-review>
失败原因: <sanitized-error>
失败命令: <command-without-secrets-or-not-applicable>
退出码: <integer|signal|timeout|not-applicable>
目标文件状态: <unchanged|changed|unknown>
```

`skipped` 必需字段：

```text
third_review: skipped
跳过原因: <与本次失败记录一致的原因>
确认依据: <紧邻的上一条用户消息摘要>
```

`awaiting-skip-confirmation` 和 `skipped` 禁止伪造 `approved/fixed`、修改数量、修改摘要、目标未达说明或剩余风险。`provider` 只用于诊断和可观测性。Workflow 检查点只依赖 `third_review` 状态，不得使用 `chatgpt`、`codex` 等 provider 名称表示流程状态。

## 10. Error Handling

Third Review 是 `iterate-feature` 中的必选 Skill，不使用可选 Hook 的缺失语义。以下情况均视为失败：

- Skill、Prompt、runner、provider 或其它必需资源不存在、不是普通可读文件，或 provider 标识不合法。
- 输入参数数量、类型、Task 归属、目录边界、canonical path 或 Review Loop 前置证据不合法。
- Git 根目录不可确定，或基线无法完整采集、保存、重采集或比较。
- provider 未配置、不可用、指定模型不可用、超时、收到信号或返回非零退出码。
- provider 修改了目标文件之外的文件，改变 staging/mode，或发生 runner 已观察到但无法安全归因的并发变更。
- provider 输出不符合合同，或 `approved/fixed`、修改数量与目标文件实际变化明显矛盾。
- 主流程模型复审发现 Third Review 破坏原始需求、关联 spec、Harness 结构或 Phase/GATE 边界。

失败时固定执行：

1. 立即停止当前 Skill 和 Workflow 推进，不执行 `writing-verify`、后续 Phase 或额外内联降级审阅。
2. 对可识别的 provider 进程执行有界终止；默认超时为 15 分钟，可由 provider 专属环境变量缩短，但通用 Workflow 不感知该配置。
3. 清理 runner 自己创建的系统临时文件，不清理或回滚工作树文件。
4. 输出脱敏后的失败步骤、provider、命令、退出信息和目标文件状态，并设为 `third_review: awaiting-skip-confirmation`。
5. 通过 Skill 的 `[CONFIRM]` 结束当前回复，请求人工确认是否跳过。该确认只绑定本次文档、本次 provider 调用和本次失败，不得复用于另一文档或重试。
6. 仅当紧邻的上一条用户消息明确确认跳过本次失败，且人工已处理任何越界或损坏状态后，才设为 `third_review: skipped` 并继续 Workflow；修正指令、重试指令或含糊回复不算跳过确认。
7. 若用户要求重试，必须重新校验输入并建立新基线；旧的确认和执行结果全部失效。

## 11. Hook 边界

Hook 是 Workflow 在明确扩展点对项目脚本的回调，定义项目额外行为，不承载 Workflow 必需的通用能力。

`after-finish` 合同：

- 项目路径固定为 `.harness/hooks/after-finish.sh`，由项目根目录解析；脚本存在时以 `sh .harness/hooks/after-finish.sh` 调用，不依赖 executable bit。
- 调用时机保持为声明该 Hook 的 Workflow 最后一个 Phase 成功结束后；迁移时必须同步更新所有现有调用方，而非只更新 `iterate-feature`。
- 文件不存在视为未定义并正常跳过；文件存在但不是普通可读文件时按 Hook 失败处理。
- Hook 失败不回滚已完成 Phase，必须报告脱敏后的命令和退出码。
- 项目示例可以执行 `docker/restart.sh`，但项目绝对路径、服务命令和凭据不得进入 `.harness/framework/`。

不得将 Third Review、结果验收、知识回填等 Workflow 必需能力通过项目 Hook 实现，也不得因 Hook 存在而修改 Workflow 的 Phase/GATE 语义。

## 12. Constraints

- 迁移必须是单入口切换：新 Skill 及其资源就绪、注册和 Workflow 引用更新后，删除旧 Third Review Hook、旧 Prompt/provider 入口及当前生效引用，禁止保留可被误调用的兼容入口。
- 通用脚本以 POSIX `sh` 为最低合同，不依赖 IDE、GUI、macOS 专属固定路径或某个 provider 已安装；平台差异封装在 provider 中。
- `.harness/` 内所有项目文件引用使用项目根目录相对路径。
- runner/provider 不得修改 Git index，不得自动提交，不得更改用户已有工作树状态。
- provider 凭据仅通过其支持的环境或安全存储注入，不进入 Prompt、命令诊断、stdout、项目文件或持久审阅记录。
- 运行记录可由执行平台按自身策略持久化，但 Harness 不得要求平台持久化，也不得在 `.harness` 下创建审阅记录。
- 审阅以发现并修复 20 个以上真实问题为强度目标；实际问题不足 20 个时不得编造，必须明确原因和已覆盖维度。
- spec 优先覆盖需求歧义、范围、架构边界、组件职责、数据流、错误处理、约束和验收可执行性。
- plan 优先覆盖 spec 追踪、任务依赖、真实文件路径、接口签名、测试可执行性和验收追踪。

## 13. 当前过渡状态

当前工作树已完成目标迁移：

- `iterate-feature.md` 仅编排 `Skill: 三方审阅`，不感知 provider 或 runner。
- `third-review/SKILL.md` 统一定义输入、执行、主流程复审、状态和失败确认，Codex 细节仅存在于 provider 适配器。
- 旧 Third Review Hook 与 `.harness/framework/third/` 生效入口已删除，不保留兼容 wrapper。
- 项目特化的 `after-finish.sh` 已迁至 `.harness/hooks/`，所有现有调用方已同步更新。

迁移后的 runner、provider、Skill 静态合同和 Workflow/Hook 集成已由隔离测试验证。目标文件并发修改检测保持 best-effort，采样窗口内修改后恢复的漏检为已接受风险。

## 14. 迁移边界

本次实现已在同一迁移中交付以下完整结果，具体执行证据由 plan 记录：

- 新增并注册 `Skill: 三方审阅`，包含 SKILL、Prompt、runner 和 Codex provider；通用参数校验和 Prompt 组装归 runner，Codex 调用细节归 provider。
- 将 `iterate-feature` Phase 2/3 切换为调用 Skill，并保留 Review Loop、主流程复审、`writing-verify`、GATE 和失败确认语义。
- 删除 `.harness/framework/hooks/third-review.sh` 和 `.harness/framework/third/` 下被新 Skill 取代的生效资源；如存在只为这些文件新增的 ignore 例外，一并清理。
- 将 `after-finish.sh` 移至 `.harness/hooks/after-finish.sh`，同步更新 FRAMEWORK 和所有现有 Workflow 调用方。
- 清理当前生效的 FRAMEWORK、Workflow、Skill 和脚本中对 `third-review Hook`、`HARNESS_THIRD_REVIEW_CMD`、ChatGPT/Codex 固定入口以及 provider 名充当状态值的引用。历史 diff、backup、已完成 spec/plan 的审阅记录及本规格的过渡状态说明不属于清理对象。
- 补齐输入、路径逃逸、provider 选择、成功、无修改、低于强度目标、不可用、超时、非零退出、输出异常、越界修改、脏工作树、可观察并发变化、主流程复审失败和跳过确认的自动化覆盖。

## 15. Acceptance Criteria

迁移完成时必须全部满足：

1. 搜索当前生效的 Workflow，除 Skill 名和通用 `third_review` 状态外，不存在 Codex、ChatGPT、Claude、Gemini、provider 脚本路径或 provider 专属环境变量；`iterate-feature` Phase 2/3 只通过 `Skill: 三方审阅` 执行 Third Review。
2. `FRAMEWORK.md` Skills 注册表和目录结构只注册一个 Third Review 流程权威源，且新 SKILL 明确定义本规格的输入、步骤、输出和 `[CONFIRM]`。
3. 在不修改 Workflow、SKILL 和 Prompt 的前提下，放入一个符合命名及协议的测试 provider 并通过显式输入选择后，可完成一次审阅；非法 provider 名、路径片段和不存在 provider 均在调用前失败。
4. 自动化测试证明绝对路径、`..`、目录、非 Markdown/非普通文件、`.harness/` 外文件、禁止子目录和 symlink 逃逸被拒绝；合法 active spec/plan 与用户明确覆盖的 `.harness/` 内目标被接受，plan 必须携带不同且已通过 GATE 的关联 spec。
5. 在包含 staged、unstaged 和 untracked 既有变更的临时 Git 仓库中运行测试 provider 后，既有变更的内容、mode 和 staging 状态保持不变，只有目标文件可产生本次增量。
6. 测试 provider 新增、删除、重命名或修改目标外文件，改变任意文件 mode/staging 状态，或模拟 runner 可观察到的并发变化时，Skill 均输出 `awaiting-skip-confirmation`、停止 Workflow，且不自动回滚现场；采样窗口内修改后恢复等不可观察竞争不作为阻断验收项。
7. provider 不可用、模型不可用、超时、信号退出、非零退出、stdout 超限、字段缺失/重复、非法整数和状态/数量/文件变化矛盾均有自动化用例，并产生脱敏且符合 schema 的失败结果。
8. `approved` 且 `N=0`、`fixed` 且 `N>=1` 的有效输出可执行成功；`N<20` 缺失目标未达说明时失败，存在合规说明时成功；Skill 不把 provider 日志混入结构化结果。
9. provider 成功后，主流程模型重新读取目标文件；plan 审阅同时只读关联 spec。复审失败时 Workflow 停止，不执行 `writing-verify` 或后续 Phase。
10. Phase 2 的 Spec Review Loop -> Third Review -> 需求摘要 -> GATE 顺序不变；Phase 3 的 Plan Review Loop -> Third Review -> writing-verify -> 执行方式选择 -> Phase 4 顺序不变，且未新增 GATE。
11. Third Review 失败后，非跳过回复不得推进；只有紧邻的、明确绑定本次失败的用户确认才能产生 `third_review: skipped`，重试会建立新基线并使旧确认失效。
12. `third_review` 检查点只出现 `executed`、`awaiting-skip-confirmation` 或 `skipped`，不得以 provider 名称表示状态；三种结果分别包含本规格要求的全部字段，且互斥字段不会混用。
13. `.harness/framework/hooks/third-review.sh` 和被替代的旧 Third Review 生效入口不存在；全量引用扫描仅允许历史材料与本规格“当前过渡状态”章节命中旧入口。
14. `.harness/hooks/after-finish.sh` 是唯一项目 Hook 目标路径，所有声明使用 after-finish 的 Workflow 均从项目根目录以 `sh` 调用；文件缺失和执行失败行为符合本规格。
15. `.harness/framework/` 不包含本项目绝对路径、项目重启命令、provider 凭据或 ChatGPT Mac 固定安装路径；通用流程可在具备 POSIX shell 和 Git 的非 IDE 环境执行。
16. 测试运行前后 `.harness` 下没有新增审阅记录、日志、备份或临时目录，runner 创建的系统临时文件已清理，Git index 未被修改。
17. 现有 spec/plan Prompt 的原始需求保持不变：只修改目标文件、不得编造修改数量、低于强度目标时说明原因和维度、plan 不修改关联 spec，且无 emoji、夸张格式或营销文案。

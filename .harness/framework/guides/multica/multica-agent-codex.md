# Multica Agent Instructions（Codex）

你通过 Multica 配置的 Codex Runtime 执行任务。Runtime 和具体模型由 Multica Agent 配置决定，本文不会切换实际执行工具或模型。

你负责使用相同 Harness 的各项目的四个工作流。每次运行先从当前 Issue 的
项目资源确定目标仓库，核对实际工作目录，再读取该仓库 AGENTS.md 及其引用的规范。
项目规则、知识、运行配置和产物均以当前仓库为准，禁止沿用其它项目的上下文。
无法唯一确定目标仓库时先澄清，不凭 Agent 名称或上次工作目录猜测。
用户明确指定 Workflow 时读取对应定义；未指定时按 FRAMEWORK.md 路由并报告：
- 迭代功能：.harness/framework/workflows/iterate-feature.md
- 精调功能：.harness/framework/workflows/refine-feature.md
- 修复Bug：.harness/framework/workflows/fix-bug.md
- 迭代文档：.harness/framework/workflows/iterate-docs.md
只读咨询按任务调度直接执行，不强制进入上述流程。
用户只需描述想改什么或异常现象，不要求填写完整需求表。
范围、相关文件、影响面、依赖和验收条件由你查阅仓库后整理，
只询问无法判断且影响执行的关键问题，不把未知内容编造成用户要求。
按所选 Workflow 原有确认点提供方案或摘要，不新增确认阶段。

将当前 Multica Issue 作为同一 Harness Task。
在 Issue 中记录 Workflow、Phase、已有产物路径（不适用填 N/A）、
确认依据、待办及下一步。续跑先读最新讨论和检查点，核对文件及证据，
从未完成步骤继续，不因新 Run 自行重选流程。按原定义执行角色和输出格式。

每个 Phase 完成或暂停时，立即通过 Multica 评论工具或 CLI 向当前 Issue
发布该阶段消息，不只写在模型对话/transcript 中，不留到最后合并成 P7 总结。
消息保留 Workflow 定义的 Phase 名称和角色，附状态、结果及实际证据路径；
验收重入时注明轮次。确认阶段仍需完整方案摘要，不能被简短进度替代。
评论触发的 Run 必须回复触发评论（CLI 使用 --parent <触发评论ID>）；
按当前版本工具规则发布，不添加 Agent/Squad mention 来触发额外运行。
确认发布成功，续跑前检查已有评论避免重复；失败或结果不确定时记录原因，
不声称已发布，也不为补消息重跑已完成代码、测试或 Hook。
非确认阶段发完立即继续，发布进度不新增人工确认或拆分 Run。

Phase 1 按选定 Workflow 的加载参数和 PROJECT.md 建立 SUMMARY 索引，读取必读文件；
大文件分批读取，按需文件不预先全文加载。
迭代功能 Phase 2 按 brainstorming 每次澄清一个问题；等待前记录已明确事项和待答问题。

仅迭代功能：spec/plan 的 Review Loop 后，按当前配置执行 Third Review 和主流程复审。
本 Agent 的三方审阅参数：provider=claude-code，timeoutSeconds=900。
项目启用审阅时，将上述 provider 显式传入 Third Review Skill，
并仅对本次调用将 HARNESS_THIRD_REVIEW_TIMEOUT_SECONDS 设置为上述 timeoutSeconds；
由 Skill 按既有流程调用 runner，不绕过输入校验、审阅证据或主流程复审。
外层命令超时不得短于上述期限加清理时间；确认对应 provider 在 Runtime 中可用且已认证。
这些是本 Agent 的调用级覆盖，不修改项目 harness.json，也不改变项目启用开关。
配置 getter 正常返回 enabled=false 时，Third Review 返回 disabled，
这是正常的非审阅状态，按 Workflow 继续；配置读取失败不得当作 disabled。
不得静默禁用或跳过；返回 awaiting-skip-confirmation 时，
在 Issue 记录失败及绑定的调用信息，请求人工决策并结束当前 Run。

迭代功能 Phase 2 完成后，将完整设计摘要和 spec 路径写入 Issue，
结束本次运行，等待人工明确确认；不得自动进入 Phase 3。
人工提出修改时，更新设计并重新等待确认。

迭代功能收到对当前 spec 的明确确认后，从 Phase 3 继续，
按 Workflow 推进计划、实现、验收、知识回填和任务总结。
恢复前检查已有文件，复用同一 Task 的产物。
如果当前确认无法对应到实际 spec，先澄清，不凭空补足确认。
精调功能：最小化加载后直接实现，Phase 3 执行 build_only 验收；
不新建 spec/plan/verify，不增加设计确认、Third Review 或知识回填阶段。
修复Bug：按 systematic-debugging 定位根因、验证假设、TDD 修复，
再完整验收、知识回填和总结；不套用迭代功能的 spec 确认或产物要求。
迭代文档：Phase 2 搜索引用与影响面，回写目标、变更和受影响文件的完整方案，
结束 Run 等人工确认后进入 Phase 3，再做一致性验证与变更报告。
不要求 spec/plan/verify，不执行 Third Review 或 after-finish Hook。
确认需对应当前 Issue 的 spec 或文档方案版本，并符合原 GATE-ENTRY。
修正不是确认，更新后重新给出完整摘要等待确认。受保护文件按仓库规则逐个确认。
无确认要求的阶段连续推进；必要信息缺失或 Skill 要求确认时，记录后结束 Run。

迭代功能 Phase 4→5、精调功能及修复Bug Phase 2→3 的验收循环均最多三轮。
有 plan 时追加“验收记录”并同步 Issue；无 plan 时在 Issue 评论逐轮记录。
轮次开始记进行中，结束记结果；跨 Run 不重置。第三轮仍失败时记录检查点、
未通过项与已尝试修复，结束 Run 等人工介入，不自动开启第四轮或进入收尾。

等待设计确认不表示整个功能已交付。
仅在 Workflow 声明的收尾位置按配置执行 Hook，失败报告命令和退出码，
不回滚已完成阶段，等待人工决策。文档流程不执行 Hook。
全流程完成后，将实际验证范围、结果及人工验收说明写入 Issue，
交由人工最终验收。

构建、测试、服务启动和工具依赖遵循当前仓库 PROJECT.md，
不沿用其它项目的命令、环境、凭据或 Hook 行为。

<!-- SUMMARY: 对比 N-Agent 与 Claude Code 2.1.88 公开源码镜像，分析上下文管理、工具发现、并发执行及运行可靠性的可借鉴设计和实施优先级 -->
# 分析 N-Agent 可借鉴的 Claude Code 设计

日期：2026-10-25

状态：研究报告；未实施，不构成功能承诺。

## 研究结论

N-Agent 最值得参考 Claude Code（下文简称 CC）的，是上下文管理和工具执行的工程细节。建议保留现有 DDD、Policy、Task、Artifact 和单层委派体系，优先改善长任务的成本、执行速度和连续性。

推荐顺序：压缩失败熔断与上下文余量 → 工作状态恢复和工具结果分层 → 批次内安全并发 → 按需工具发现。如果观测表明工具 schema 已占输入的大头，则提前实施工具发现。

## 范围与证据边界

- N-Agent：读取项目架构、文件映射、产品定位文档，并检查 agent_graph、context_service、context_compressor、prompt_builder、tool_service 和工具领域模型等实现。
- CC：查看公开镜像 Exhen/claude-code-2.1.88 中的选定 TypeScript 源文件。它是 2.1.88 历史快照，不能代表当前正式版。
- 镜像的真实性、完整性及其与原始 npm 发布物的一致性未独立验证；结论是基于所见源码的设计比较，并非全面源码审计。
- 受 feature flag、构建条件、Provider 能力约束的分支，不等于所有用户实际启用的行为。
- 本报告没有运行产品、性能基准或端到端测试；收益和优先级属于工程判断，需通过实施后的指标验证。
- 借鉴目标是设计原则与行为契约，不建议直接移植镜像代码。

## 当前能力对比

| 维度 | N-Agent 已有基础 | CC 镜像中可观察的设计 | 可借鉴方向 |
| --- | --- | --- | --- |
| 上下文管理 | 首尾保护、摘要、压缩冷却、统计、ContextPolicy | 工具结果微压缩、完整压缩、压缩后恢复分别处理 | 建立分层策略和恢复契约 |
| 工具发现 | 工具注册、多来源工具、权限过滤 | ToolSearchTool 检索延迟工具 | 按需加载 schema |
| 工具执行 | 主执行路径逐个执行 tool call | 并发安全工具并行，其他工具独占，结果按接收顺序交付 | 有限并发和确定性组装 |
| 运行可靠性 | 预算、取消、终止原因、Task 和委派恢复机制 | 自动压缩连续失败熔断、输出空间预留 | 补强上下文压力下的失败收敛 |
| Prompt | 统一分节，已考虑前缀缓存稳定性 | 微压缩与缓存有效性协同 | 细化分段、计量和 Provider 适配 |

## 专项对比：完整压缩与历史消息压缩

两者核心机制相同：调用 LLM 把历史转换成摘要，再以摘要继续对话。N-Agent 已有目标、进展、决策、文件、待办五个结构化栏目，也支持旧摘要与新增历史的增量合并。改进重点在输入信息质量、压缩边界和继续执行时的状态恢复。

| 维度 | N-Agent 当前实现 | CC 镜像中的相关设计 |
| --- | --- | --- |
| 压缩范围 | 保护头部和尾部，压缩中间段，边界对齐工具调用组 | 完整压缩建立新的历史交接边界，另有局部压缩路径 |
| 总结输入 | 中间段转换成 role: content 文本；长工具正文预先截断 | 总结指令强调代码操作、错误修复、用户反馈和技术细节 |
| 摘要内容 | 目标、进展、决策、文件、待办，每节不超过 3 行 | 更详细的工作交接要求，包括具体文件、代码、错误、用户修正和下一步 |
| 连续压缩 | 上次摘要与新增中间段合并，生成替换摘要 | 结合历史摘要、压缩边界及后续恢复继续执行 |
| 压缩后输入 | 原头部 + 新摘要 + 原尾部，系统消息单独保留 | 摘要之外，按预算恢复文件、计划、技能、工具等材料 |
| 失败处理 | LLM 摘要失败后尝试备用 summarizer，最后退化为静态摘取 | 自动压缩具有连续失败计数与熔断等控制 |

### 差异一：保留原消息与重建工作上下文

N-Agent 保留头尾原消息，可以保留近期对话和工具输出的精确信息，恢复成本较低。局限是受保护部分本身很大时，压缩中间段仍可能无法释放足够空间。

CC 完整压缩更强调用交接摘要建立边界，并补回继续执行所需的材料，因此更依赖摘要质量与恢复机制。不能据此断言 CC 每条路径都会清除全部历史，也不能将其局部压缩路径与完整压缩混为一谈。

### 差异二：总结前的信息损失

检查 N-Agent 的 ContextCompressor 实现发现：

- _prune_old_tool_results 会把待总结的长工具正文截到前 500 字符，也会截断长工具调用参数。
- _generate_summary 随后仅按 role 和 content 构造文本，没有显式序列化 assistant 消息中的 tool_calls 名称、参数及调用关联信息。因此前一步截断后的参数也没有进入这条总结输入路径。
- 仅含工具调用、正文为空的 assistant 消息，无法通过这一转换向总结模型提供完整的操作意图。
- 长日志尾部的错误、输出后半部分的关键结论，可能在总结前就已丢失。

这意味着仅升级摘要 Prompt 不足以解决问题：总结模型无法保留未收到的信息。CC 的详细交接指令值得参考，但详细指令本身也不能保证摘要无遗漏。

建议优先改进总结输入的显式投影：保留工具名、必要参数、调用与结果的关联、执行状态，以及输出的首尾和关键错误。投影仍需遵循脱敏、权限和长度预算，不应无条件发送所有原始参数。

验收方向：使用只有 tool_calls 而没有 assistant 正文的消息、错误位于日志尾部的输出、长文件读取等样例，确认摘要能够保留操作对象、失败原因和下一步所需证据。

### 差异三：流程完成与摘要质量

N-Agent 的备用摘要链路有利于保持流程继续运行，但最终静态摘取仅能保留少量用户和助手内容，信息量可能明显低于模型生成的摘要。当前返回的 compressed 状态不应被理解为任务信息已充分保留。

建议区分模型摘要成功、备用摘要成功、静态降级等质量状态，并将其用于日志、观测和后续策略。上下文仍无法满足预算或摘要质量不足时，应根据任务需要决定重试、恢复材料或明确终止，而不是仅凭压缩流程返回成功就继续。

### 建议实施顺序

1. 改善总结输入，减少工具信息在摘要生成前的损失。
2. 区分摘要质量和降级状态，补齐失败计数、退避与熔断。
3. 加入按预算恢复工作状态的机制。
4. 在证据和恢复机制具备后，再扩展工具结果分层与更积极的历史裁剪。

此顺序是上下文压缩专项内的依赖建议；整体优先级仍应结合上下文溢出、工具 schema 成本和长任务失败率的实际基线判断。

## 建议一：恢复工作现场

N-Agent 当前压缩链路已有较完整基础，无需重建。值得补强的是压缩后恢复执行状态的显式契约。

CC 的 compact.ts 包含计划、文件、技能和已发现工具等恢复相关逻辑，并设置恢复预算。这说明压缩后的继续执行需要处理摘要之外的工作上下文。

建议 N-Agent 在摘要之外生成结构化继续执行状态：

- 用户目标、已确认约束、待处理问题。
- 当前 Task、Artifact ID 和 revision。
- 已完成步骤、下一步和关键证据引用。
- 当前仍有效的技能和工具集合。

权限与审批不能由模型摘要恢复，必须重新查询权威状态并评估。Artifact revision、任务状态和工具可用性也应在使用时复核，避免过期摘要成为事实来源。

建议落点：app/application/context_service.py、app/infrastructure/context/context_compressor.py，以及对应的 Context 领域模型与持久化端口。

验收方向：构造压缩前后连续执行场景，确认用户约束、目标制品和下一步不丢失；摘要中伪造的授权不能改变工具执行权限。

## 建议二：分层保留工具结果

CC 的 microCompact.ts 对旧工具结果单独处理，并考虑缓存是否过期及模型是否支持 cache editing。部分路径受内部开关限制，源码明确存在不执行该微压缩路径、交给自动压缩处理的情况。

N-Agent 可以参考分层原则，设计 Provider 无关的基础策略：

1. 最近且相关的结果保留全文。
2. 较旧的大结果保留摘要和可重新读取的引用。
3. 原始结果持久化，模型需要时再读取。
4. 错误信息、关键决策证据和未完成操作优先保留。

区分审计存储和模型上下文：持久化完整证据，不意味着每轮都需要发送全文。文件引用需要带版本或内容摘要，明确“当时读取结果”与“当前文件内容”的区别；重新读取仍需权限校验。

该建议是 N-Agent 的拟议实现，并非断言 CC 所有路径都采用同样的持久化引用方案。

验收方向：比较长工具链的输入 token、完整压缩次数及答案证据完整性；确保 tool call/result 配对合法，原始结果可追溯。

## 建议三：按需发现工具

CC 的 ToolSearchTool 支持关键词搜索和按工具名直接选择延迟工具。

N-Agent 已有 MCP、Plugin、Skill、Browser、Artifact、Task 等来源。随着工具增长，schema 成本和工具误选概率都值得观测。建议默认暴露常用工具和搜索入口，按需加入相关工具定义。

边界要求：

- 搜索候选先经过现有权限与工具可用性过滤。
- 搜索命中不代表获得执行授权。
- 执行阶段继续通过 ToolPolicy，不绕过预算和审批。
- Provider 不支持原生延迟工具时，由应用层在后续模型调用中加入 schema。
- 处理工具下线、配置变化、会话恢复后的失效问题。

第一版可用名称、描述、来源的关键词检索，不必先引入向量检索。

建议落点：app/application/tool_service.py、app/application/context_service.py、工具注册表及 Provider 适配层。

验收方向：记录 schema token 占比、首次工具发现成功率、误选率、额外模型轮次和任务总成本；防止省下 schema token 却增加过多搜索轮次。

## 建议四：引入有边界的工具并发

N-Agent 的 AgentGraphRunner._execute_tools 当前通过循环逐个执行待处理工具。CC 的 StreamingToolExecutor 区分并发安全工具和独占工具，允许进度即时交付，并缓冲最终结果以保持接收顺序。

建议先实施批次内有限并发，再考虑模型流式生成期间提前执行：

- 独立查询和读取可并发。
- 写入、审批、浏览器操作默认串行。
- 每个调用独立保留预算、超时、取消和审计。
- 最终消息组装保持确定顺序。
- 同一资源存在依赖时串行，不能仅凭工具名称判断独立性。

现有 RiskLevel.SAFE 表示风险等级，不能证明并发安全。应单独建模并发属性或资源约束，避免把 SAFE 直接映射为可并发。

建议落点：app/domain/tool.py 和 app/application/agent_graph.py；保持 ToolService 为授权与执行入口。

验收方向：独立慢查询的批次耗时下降；写入不会交错；取消后无孤儿调用；预算结算和审批不重复；tool call/result 顺序稳定。

## 建议五：控制压缩失败和上下文余量

CC 的 autoCompact.ts 根据模型窗口与输出上限计算有效空间，并对自动压缩连续失败计数、达到阈值后停止重复尝试。

N-Agent 已有压缩冷却和预算控制，可进一步区分：成功后的冷却、失败后的退避、连续失败后的熔断，以及上下文不足时的降级或终止。

阈值应根据模型窗口、输出上限、system prompt、工具 schema 和消息整体大小计算，不照搬 CC 的固定 token 常量。错误类型也应区分：瞬时 Provider 故障与无法容纳压缩请求，需要不同处理。

建议落点：ContextPolicy 决定策略，ContextService 编排，ContextCompressor 执行；计数状态应明确按会话或运行隔离。

验收方向：持续压缩失败能够有界终止，不产生无效重试风暴；切换小窗口模型时仍能检测空间不足；不同会话互不影响。

## 建议六：深化 Prompt 缓存观测

N-Agent 的 prompt_builder.py 已明确避免 Worker/Judge 运行中追加指导语破坏前缀缓存，现有方向合理。

建议继续推进：

- 稳定内容在前，频繁变化内容在后。
- 工具 schema 使用稳定排序。
- 记录各段 token、缓存命中量及压缩前后成本。
- 修改旧工具结果时评估缓存失效代价，避免只追求更短输入。
- 缓存编辑等专有协议放入 Provider 适配层。

验收方向：在同一模型与可比任务下比较缓存命中、实际计费和端到端耗时；以总成本而非输入长度单一指标判断收益。

## 实施优先级

| 顺序 | 工作项 | 预期价值 | 主要复杂度 |
| --- | --- | --- | --- |
| 1 | 压缩失败熔断、上下文余量 | 失败有界，减少无效调用 | 错误分类、模型差异、状态隔离 |
| 2 | 工作状态恢复、工具结果分层 | 改善长任务连续性和 token 成本 | 引用生命周期、证据保留、权限复核 |
| 3 | 批次内安全并发 | 降低独立工具调用耗时 | 资源冲突、取消、预算与顺序 |
| 4 | 按需工具发现 | 控制工具增长成本 | 发现质量、额外轮次、Provider 兼容 |
| 持续 | Prompt 缓存观测 | 为上述策略提供量化反馈 | 指标口径及 Provider 能力差异 |

这些优先级是研究判断。应先收集基线，再按独立任务制定 spec 和验收方案，避免一次性重构 Runtime。

## 暂不优先借鉴的部分

暂不以增加 Agent 团队拓扑或复刻实验开关为目标。N-Agent 已有单层委派、预算账本和恢复机制，当前应优先验证长任务成功率、压缩后续执行、取消收敛和单任务成本。

不建议因 CC 的文件组织或实现语言改变 N-Agent 的 DDD 分层。可将对应行为分别落到领域策略、应用编排和基础设施适配中。

## 源码依据

CC 公开镜像，查阅日期为 2026-10-25：

- [autoCompact.ts：有效上下文窗口、输出预留及连续失败熔断](https://github.com/Exhen/claude-code-2.1.88/blob/main/source/src/services/compact/autoCompact.ts)
- [compact.ts：压缩及压缩后上下文恢复](https://github.com/Exhen/claude-code-2.1.88/blob/main/source/src/services/compact/compact.ts)
- [prompt.ts：完整与局部压缩的交接摘要指令](https://github.com/Exhen/claude-code-2.1.88/blob/main/source/src/services/compact/prompt.ts)
- [microCompact.ts：工具结果处理及缓存相关条件分支](https://github.com/Exhen/claude-code-2.1.88/blob/main/source/src/services/compact/microCompact.ts)
- [ToolSearchTool.ts：延迟工具发现](https://github.com/Exhen/claude-code-2.1.88/blob/main/source/src/tools/ToolSearchTool/ToolSearchTool.ts)
- [StreamingToolExecutor.ts：并发安全判断、独占执行和结果顺序](https://github.com/Exhen/claude-code-2.1.88/blob/main/source/src/services/tools/StreamingToolExecutor.ts)

N-Agent 本地依据：

- app/application/agent_graph.py
- app/application/context_service.py
- app/infrastructure/context/context_compressor.py
- app/application/prompt_builder.py
- app/application/tool_service.py
- app/domain/context_policy.py
- app/domain/tool.py
- .harness/knowledge/01-overview.md
- .harness/knowledge/02-architecture.md
- .harness/knowledge/22-file-map.md
- .harness/prd/01-prd-sense.md

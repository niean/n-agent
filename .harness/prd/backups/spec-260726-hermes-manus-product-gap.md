# N-Agent 对比 Hermes、Manus 的关键产品功能差距

- 创建时间：2026-07-26
- 状态：active
- 任务来源：分析 N-Agent 相比 Hermes、Manus 缺少的关键产品功能
- 排除范围：Project、Tenant
- 调研口径：竞品以官方公开文档为准；N-Agent 以当前仓库代码、测试和 `.harness/` 文档为准

## 1. 结论摘要

排除 Project、Tenant 后，N-Agent 的主要差距不在 Agent Runtime，而在面向最终用户的执行能力、结果交付能力和跨环境产品体验。

N-Agent 已具备较完整的技术底座：OpenAI-compatible API、LangGraph TurnLoop、会话与上下文、外部记忆、工具与 Policy、知识库、MCP、Skill、Plugin、沙盒、定时任务、后台 Task、Gateway、飞书、CLI/TUI、ACP、用量观测和安全审计。与 Hermes、Manus 相比，下一阶段最需要补齐的是“Agent 能替用户完成什么”和“用户如何观察、接管、复用和交付结果”。

建议优先补齐四项 P0：

1. Browser / Computer User：从网页抓取升级为可登录、可操作、可接管的浏览器执行。
2. 多 Agent 委派与并行：在现有 Task 基础上建立任务拆解、并发执行和结果聚合。
3. Artifact 制品工作台：将附件和 Artifact 元数据升级为可预览、编辑、导出和发布的结果交付体系。
4. 持久化执行环境：让任务拥有可休眠、恢复、快照和远程运行的 Workspace。

完成这四项后，N-Agent 才会从“能力完整的本地 Agent Runtime”进一步成为“Hermes、Manus 同级的可交付 Agent 产品”。

## 2. 调研边界与证据规则

### 2.1 调研时间

本分析以 2026-07-26 可访问的 Hermes、Manus 官方公开资料为快照。竞品更新频繁，正式立项时应重新核验功能行为、套餐限制和接口契约。

### 2.2 排除项

以下已知缺口不纳入本次优先级判断：

- Project：持久工作区、项目级指令、文件和资源继承。
- Tenant：用户、组织、成员、角色和资源隔离。

依赖 Project 或 Tenant 才能完整成立的协作功能，也不作为本次核心缺口排序依据。

### 2.3 事实与判断

- “竞品已具备”表示其官方文档明确公开，不代表本次验证了其内部实现。
- “N-Agent 已具备”以当前代码、测试和知识文档为准，不把规划项视为现有能力。
- 优先级与路线建议属于本分析结论，不是竞品官方观点。
- 不把竞品营销口径或性能数字直接转化为 N-Agent 验收标准。

### 2.4 与历史分析的关系

本文是排除 Project、Tenant 后的当前综合决策依据。`spec-260717-manus-gap-analysis.md` 和 `spec-260711-hermes-gap-analysis.md` 保留各自调研日期的详细快照；其现状判断与本文冲突时，以本文和当前代码为准。

## 3. N-Agent 当前能力基线

### 3.1 已形成正式能力

| 能力域 | N-Agent 当前事实 |
|---|---|
| Agent Runtime | LangGraph TurnLoop，支持上下文准备、模型调用、工具执行、记忆更新和结束判断 |
| API | OpenAI-compatible 同步与流式接口、模型列表 |
| 模型 | OpenAI-compatible、Anthropic 等 Provider，支持管理和运行时切换 |
| 会话与上下文 | SQLite 会话、消息、摘要、动态压缩、会话来源和外部记忆 |
| 工具治理 | Tool Registry、ToolPolicy、风险等级、审批、预算和信息流策略 |
| 网络检索 | `web_fetch`，沙盒内提供 `web_search`、`web_extract` |
| 代码执行 | Docker/Local Sandbox、`terminal`、`execute_code`、受白名单控制的 `host_terminal` |
| Knowledge | 知识库 SPI、实例管理和 `search_knowledge` |
| MCP | MCP Site 管理、探测、工具发现和远程工具执行 |
| Skill | 扫描、启停、读取、写入治理、自进化、备份和 Curator 周期维护 |
| Plugin | Plugin 扫描、配置、能力聚合和工具注入 |
| Schedule | Cron、时区、租约、执行记录、Agent 管理工具和结果投递 |
| Task | 后台 Task、Worker、状态机、看板、审批、重试、附件和 Artifact 元数据 |
| Gateway/Platform | Dashboard、CLI/TUI、ACP、飞书和统一 Gateway 会话映射 |
| 多模态输入 | Chat 图片上传、Vision 模型调用 |
| 可观测性 | Token、成本、上下文构成、压缩收益、工具调用、任务和安全状态 |

### 3.2 当前优势

N-Agent 在以下方面已经具备较强底座，不应作为下一阶段重复建设目标：

- 工具风险分级和审批；
- 预算、信息流和运行安全策略；
- Task 状态机、租约、重试和人工决策；
- MCP、Plugin、Skill 的扩展边界；
- Skill 自创建、自改进和 Curator；
- 外部记忆 Provider 和上下文压缩；
- Docker 沙盒、代码执行和宿主机白名单能力；
- Usage、工具调用和运行状态观测。

这些能力使 N-Agent 具备承载 Browser、多 Agent、Artifact 和远程 Workspace 的治理基础。

## 4. 产品能力差距矩阵

| 产品能力 | N-Agent | Hermes | Manus | 优先级 |
|---|---|---|---|---|
| 浏览器自动化、登录态和人工接管 | 仅网页抓取与搜索，没有真实浏览器操作 | 浏览器自动化，支持本地和云端后端 | Cloud Browser、Browser Operator、Take Over | P0 |
| 多 Agent 拆解与并行执行 | 有后台 Task Worker，但不支持 Worker 自动拆分子任务 | `delegate_task` 创建隔离子 Agent 并并发执行 | Wide Research 并行部署大量 Agent | P0 |
| 文档、幻灯片、表格、网页等制品生成 | 有附件和 Artifact 元数据，缺少产品化编辑、预览和导出 | 可通过文件、终端和 Skill 完成，偏开发者体验 | Slides、报告、Dashboard、网页和媒体完整交付链 | P0 |
| 持久化远程执行环境 | 本地/Docker 沙盒和 Host Terminal，缺少按任务持久化远端 Workspace | local、Docker、SSH、Modal、Daytona 等后端 | 临时 Sandbox、Cloud Computer、My Computer | P0 |
| 开箱即用的第三方 Connector | 有 MCP/Plugin 基础，主要依赖技术配置 | MCP、插件和大量平台工具 | Gmail、Notion、Slack、Calendar、GitHub 等 OAuth Connector | P1 |
| 多模态生成与处理 | 支持图片输入和 Vision，缺图片生成、音频、视频和语音 | 图片生成、Vision、TTS、语音模式 | 图片和视频生成与理解、语音、转写、会议纪要 | P1 |
| 多渠道和跨端连续体验 | Dashboard、CLI/TUI、ACP、飞书 | 20+ 消息平台、桌面、CLI 和语音 | Web、Desktop、Browser Operator、Mail、Slack | P1 |
| 用户记忆和历史检索体验 | 有上下文压缩和外部记忆，缺统一会话全文搜索和用户画像入口 | FTS5 会话搜索、USER/MEMORY、Honcho 用户建模 | 连接数据和历史上下文驱动个性化 | P1 |
| 执行接管、快照和回滚 | 有审批、取消和 Task 状态机，缺 Workspace checkpoint/rollback | 文件修改前快照，支持 `/rollback` | 浏览器、VS Code 实时查看与 Take Over | P1 |
| Plugin/Skill 分发生态 | 有 Plugin、Skill、自进化和 Curator，缺发现与一键安装目录 | 内置和可选 Plugin/Skill 分发 | Skills、Connector 目录 | P2 |

## 5. P0：Browser / Computer Use

### 5.1 差距判断

这是当前最大的执行能力断层。

N-Agent 的 `web_fetch`、`web_search` 和 `web_extract` 能读取公开网页信息，但不能：

- 点击、滚动和填写表单；
- 使用登录后的网页；
- 上传和下载文件；
- 操作 SaaS 管理后台；
- 处理多页面交互流程；
- 让用户实时观察和临时接管。

Hermes 已提供浏览器自动化和多个执行后端。Manus Cloud Browser 支持登录态、网页操作、实时观察和 Take Over，Browser Operator 可复用用户本地浏览器登录态。

### 5.2 建议最小范围

- 新增 Browser 领域端口和 Browser Session。
- 首期使用 Playwright 或 Chrome CDP 实现隔离浏览器。
- 提供 `navigate`、`observe`、`click`、`type`、`scroll`、`upload`、`download`、`screenshot`。
- Browser Session 与 N-Agent Session/Task 绑定。
- Dashboard 展示实时截图、动作历史和当前 URL。
- 支持用户停止、接管和归还控制权。
- Cookie、密码和本地存储不进入模型消息、通用日志或 Artifact。
- 支付、发布、发送消息和删除数据等副作用继续经过 ToolPolicy 审批。

### 5.3 后续扩展

- 本地 Chrome/Edge Browser Operator；
- 登录态加密和授权生命周期；
- 浏览器会话恢复；
- 网络出口和域名策略；
- 页面内容脱敏；
- 录制、回放和失败现场保留。

## 6. P0：多 Agent 委派与并行执行

### 6.1 差距判断

N-Agent 已有后台 Task、Worker、租约、心跳、审批和重试，但当前执行模型仍是一项任务对应一个 Worker Agent。Worker 不会自主创建子任务，也没有父任务拆解、并发执行和结果聚合。

Hermes 的 `delegate_task` 可创建隔离上下文、受限工具集和独立终端的子 Agent。Manus Wide Research 会自动拆解可并行任务、为每个子任务分配独立 Agent，并由父任务汇总结果。

### 6.2 建议模型

```text
Parent Task
  -> Planner 生成 TaskGraph
  -> 多个 Child Task/Run 并发执行
  -> Aggregator 校验、去重、处理冲突
  -> Parent Task 生成最终结果和 Artifact
```

### 6.3 建议范围

- Task 增加父子关系和 TaskGraph。
- 自动判断任务是否适合并行，不对所有任务强制拆解。
- 子任务拥有独立上下文、工具白名单、预算和超时。
- 支持并发上限、总预算和单子任务预算。
- Dashboard 展示父子任务、状态和资源消耗。
- 支持单个子任务重试、取消和人工修订。
- Aggregator 负责引用整理、冲突处理、缺失检查和结果合成。
- 父任务取消向全部子任务和执行环境传播。

### 6.4 边界

- 多 Agent 不等于多轮串行提示。
- 子 Agent 不能绕过父任务的 ToolPolicy、BudgetPolicy 和 InformationFlowPolicy。
- TaskGraph 应复用当前 Task 状态机，不新建第二套任务系统。
- 第一阶段只实现树形委派，暂不实现任意循环图和 Agent 间自由通信。

## 7. P0：Artifact 制品工作台

### 7.1 差距判断

N-Agent 已支持 Task 附件上传下载，并具有 `TaskArtifact`、`storage_ref` 等领域字段，但这些能力主要是数据结构和任务结果记录，还没有形成用户可直接消费的制品交付体验。

Manus 可以直接生成和继续编辑：

- PPTX、PDF 和 Web Slides；
- 数据报告和交互 Dashboard；
- 独立网页和分享链接；
- 图片、视频和语音；
- 可部署的全栈应用。

### 7.2 建议首期制品

优先覆盖三类高频结果：

1. Markdown、HTML、PDF 报告；
2. CSV、XLSX 数据表；
3. 静态网站或数据 Dashboard。

### 7.3 建议范围

- 建立统一 Artifact Store，不让 `storage_ref` 指向任意宿主路径。
- Task 详情提供 Artifact Gallery。
- 支持浏览器内预览、下载和打开原始文件。
- Artifact 记录版本、MIME、大小、校验和、生成来源和摘要。
- 支持基于既有 Artifact 继续修改，而不是每次从头生成。
- 对话、Task、Schedule 统一引用 Artifact。
- 失败任务区分可交付结果、临时文件和不完整产物。
- 后续增加 DOCX、PPTX、图片和发布托管。

### 7.4 验收方向

- Agent 完成长任务后，用户无需查看工具日志即可识别最终交付物。
- Artifact 可预览、下载并追溯到来源 Task/Run。
- 修改 Artifact 会生成新版本，旧版本仍可回溯。
- 下载和预览不能突破 Workspace、权限和信息流边界。

## 8. P0：持久化执行环境

### 8.1 差距判断

N-Agent 当前已有 Docker Sandbox、Local Sandbox 和 Host Terminal，具备受控执行基础，但缺少按任务生命周期管理的持久 Workspace：

- 环境不能作为独立产品资源管理；
- 缺少休眠、唤醒和恢复；
- 缺少远程执行后端；
- 缺少 Workspace 快照和回滚；
- 缺少面向用户的文件浏览与终端观察。

Hermes 支持 local、Docker、SSH、Modal、Daytona、Singularity 等后端。Manus 同时提供临时 Sandbox、持久化 Cloud Computer 和本地 My Computer。

### 8.2 建议范围

- 新增 Task/Session 级 Workspace 资源。
- 保留已安装依赖、工作文件和后台进程。
- 支持环境休眠、唤醒、过期和销毁。
- 增加 SSH 和远程 Docker 执行后端。
- 设置 CPU、内存、磁盘、时长和网络配额。
- 提供文件快照、恢复点和回滚。
- Dashboard 提供文件浏览器、终端输出和环境状态。
- Workspace、Sandbox、Host Terminal 使用统一能力描述，但保持不同授权策略。

### 8.3 边界

- 持久 Workspace 不等于直接暴露宿主机。
- 密钥默认不持久化到文件系统快照。
- Task 结束是否保留环境必须有明确策略。
- 环境销毁、快照恢复和网络放开属于高风险操作。

## 9. P1：Connector 产品化

### 9.1 差距判断

N-Agent 的 MCP 和 Plugin 已建立技术扩展协议，但用户仍需要理解 endpoint、transport、密钥和工具配置。Manus 面向用户提供 Gmail、Notion、Slack、Google Calendar、Google Drive、GitHub 等预置 OAuth Connector。

两者差异是：

- MCP 解决工具如何接入；
- Connector 产品解决用户如何发现、授权、验证、使用和撤销业务应用。

### 9.2 建议范围

- Connector Catalog；
- OAuth 授权和回调；
- 授权范围说明；
- Token 刷新和撤销；
- 连接健康状态；
- 预制业务动作和示例工作流；
- Connector 与 ToolPolicy、Schedule、Task 的组合；
- 首期只选择 2 至 3 个高价值应用，不追求数量。

## 10. P1：多模态生成与处理

N-Agent 已支持图片上传和 Vision，因此不是完全缺失。主要缺口是生成与音视频链路：

- 图片生成和编辑；
- 音频转写、说话人识别和时间戳；
- TTS 和语音对话；
- 视频理解和摘要；
- 短视频生成；
- 会议录音到纪要、行动项和 Task。

建议先实现音频转写和图片生成，两者容易复用现有 Tool、Artifact 和 Task 体系；视频生成和实时语音后置。

## 11. P1：多渠道和跨端体验

N-Agent 已有 Dashboard、CLI/TUI、ACP 和飞书，开发者入口较完整，但用户消息入口覆盖较窄。

Hermes 已支持 Telegram、Discord、Slack、WhatsApp、Signal、Teams 等平台及语音模式。N-Agent 不应简单追求平台数量，应优先选择目标用户真实使用的入口：

1. Slack 或企业微信；
2. 邮件触发任务；
3. 移动端通知和任务审批；
4. 语音消息和转写。

所有入口应继续复用 Gateway、Session、Task 和 ToolPolicy，而不是建立独立 Agent 流程。

## 12. P1：记忆与历史检索体验

N-Agent 已具备上下文压缩、外部记忆 Provider、检索注入和外部记忆管理，底层能力并不弱。当前主要是产品体验缺口：

- 会话全文搜索；
- 跨会话语义检索；
- 用户偏好和长期记忆的统一查看入口；
- 记忆来源、置信度和冲突展示；
- 用户确认、修改和遗忘；
- 从搜索结果恢复或继续会话。

建议优先补齐会话全文搜索和统一 Memory 面板，不再新增另一套记忆存储。

## 13. P1：执行接管、快照与回滚

N-Agent 已有工具审批、Task 审批、取消和重试，但仍缺少工作结果级安全网：

- 文件修改前 Workspace checkpoint；
- 用户可见的变更摘要；
- 一键回滚；
- 浏览器或终端实时接管；
- 用户追加指令后安全中断并继续；
- 长任务阶段性确认点。

该能力应与持久 Workspace、Browser 和 Artifact 共用快照与版本模型。

## 14. P2：Plugin/Skill 分发生态

N-Agent 已有较完整的 Plugin、Skill、自进化和 Curator 能力，主要缺口不在运行时，而在分发：

- 官方、团队和社区目录；
- 搜索、分类和可信来源；
- 一键安装、升级和卸载；
- 依赖、密钥和配置引导；
- 版本兼容和变更说明；
- 安全扫描、签名和评价；
- 从 GitHub 或压缩包导入。

在核心执行能力补齐前，该项不应高于 Browser、多 Agent、Artifact 和 Workspace。

## 15. 推荐实施顺序

```text
Browser / Computer Use
  -> 多 Agent TaskGraph
  -> Artifact 预览与交付
  -> 持久 Workspace / 远程执行
  -> OAuth Connectors
  -> 多模态和更多消息入口
  -> 记忆检索、快照回滚和分发生态
```

### 15.1 第一阶段：扩大行动面

- Browser Session；
- 基础网页操作；
- 浏览器观察与停止；
- ToolPolicy 风险控制。

目标：让 N-Agent 从“检索网页”升级为“完成网页任务”。

### 15.2 第二阶段：扩大任务规模

- Parent/Child Task；
- Planner 和 Aggregator；
- 并发与预算；
- 子任务进度和重试。

目标：让 N-Agent 从“单 Agent 长任务”升级为“受控并行执行”。

### 15.3 第三阶段：完善交付

- Artifact Store；
- 报告、表格、静态网页；
- 预览、版本和下载；
- Task Artifact Gallery。

目标：让用户获得可继续使用的成品，而不只是消息和工具日志。

### 15.4 第四阶段：持续运行

- 持久 Workspace；
- 快照和恢复；
- SSH/远程 Docker；
- 文件和终端观察。

目标：支持跨会话、跨设备和长时间运行的复杂任务。

## 16. 不建议近期投入的方向

在上述 P0 完成前，不建议优先投入：

- 大量低使用率消息平台适配；
- 完整视频生成平台；
- 为每种文档格式分别建立独立领域模型；
- 复杂 Agent 社交或自由通信网络；
- 与现有 Policy、Task、Artifact 平行的第二套编排系统；
- 仅用于展示的竞品功能数量对齐。

产品判断应始终回到三个问题：

1. 是否显著扩大 Agent 能完成的真实任务范围；
2. 是否让执行过程更可观察、可接管和可恢复；
3. 是否让结果成为可复用、可交付的用户资产。

## 17. 官方参考资料

### 17.1 Hermes

- [Hermes Agent 官方文档](https://hermes-agent.nousresearch.com/docs/)
- [Hermes Features Overview](https://hermes-agent.nousresearch.com/docs/user-guide/features/overview)
- [Hermes Tools & Toolsets](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools/)
- [Hermes Code Execution](https://hermes-agent.nousresearch.com/docs/user-guide/features/code-execution/)
- [Hermes Web Dashboard](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-dashboard)
- [Hermes GitHub](https://github.com/nousresearch/hermes-agent)

### 17.2 Manus

- [Manus Documentation Index](https://manus.im/docs/llms.txt)
- [Cloud Browser](https://manus.im/docs/features/cloud-browser)
- [Wide Research](https://manus.im/docs/features/wide-research)
- [Integrations](https://manus.im/docs/integrations/integrations)
- [Data Analysis & Visualization](https://manus.im/docs/features/data-visualization)
- [Manus Slides](https://manus.im/docs/features/slides)
- [Multimedia Processing](https://manus.im/docs/features/multi-modal)
- [Cloud Infrastructure](https://manus.im/docs/website-builder/cloud-infrastructure)
- [Cloud Computer](https://help.manus.im/en/articles/15392111-what-is-the-cloud-computer)
- [My Computer](https://help.manus.im/en/articles/14178443-what-is-the-my-computer-feature-capable-of)

## 18. 仓库参考

- `README.md`
- `.harness/knowledge/01-overview.md`
- `.harness/knowledge/02-architecture.md`
- `.harness/knowledge/22-file-map.md`
- `.harness/prd/01-prd-sense.md`
- `.harness/prd/02-prd-baseline.md`
- `.harness/specs/active/spec-260717-manus-gap-analysis.md`
- `.harness/specs/completed/spec-260711-hermes-gap-analysis.md`
- `app/infrastructure/tools/builtin.py`
- `app/application/task_tools.py`
- `app/domain/task.py`
- `app/infrastructure/sandbox/`

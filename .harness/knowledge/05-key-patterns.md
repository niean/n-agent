<!-- SUMMARY: Agent MVP 的关键实现模式，包括 DDD 边界、协议适配、运行事件、工具权限、Memory 端口和演进基线 -->
# 关键代码模式

项目中反复出现但不易从单个文件推断的模式，供新功能实现时参照。

## 模式一：Evolution Baseline

MVP 是完整 Agent 能力的第一阶段，设计目标是建立可持续演进的架构基线，而不是一次性 demo。

规则：
- 当前实现只覆盖 MVP 验收标准。
- 代码结构、领域模型、端口和依赖方向必须为后续完整 Agent 能力保留扩展点。
- 后续能力包括更多 Provider、工具生态、长期 Memory、权限审批、多 Agent、自动化任务、可观测性和多入口交互。
- 不因未来能力提前实现复杂功能，避免 MVP 范围膨胀。

陷阱：把 roadmap 能力写进当前验收标准，会导致计划失控；正确做法是保留端口和边界，不提前实现完整能力。

## 模式二：DDD 外层依赖内层

项目采用 Domain / Application / Infrastructure / Interfaces 分层。

依赖方向：
1. Interfaces -> Application -> Domain。
2. Infrastructure 实现 Domain 端口，并在启动入口注入。
3. Domain 不依赖 FastAPI、LangGraph、SQLite、OpenAI SDK 或具体工具 handler。
4. Application 不 import Infrastructure 具体实现。
5. Interfaces 不直接访问 SQLite，不直接执行工具 handler。

陷阱：为了快速实现把 Provider SDK、SQLite 查询或 FastAPI 请求对象传入 Domain，会破坏长期演进边界。

## 模式三：LangGraph 只做 Application 编排

LangGraph 用于表达 Agent Runtime 状态图，包括加载上下文、调用 LLM、执行工具、更新 Memory 和结束判断。

规则：
- LangGraph 内部事件先转换为 Application 层 ChatEvent。
- Interfaces 层只把 ChatEvent 编码为 OpenAI-compatible SSE chunk 或非流式响应。
- Domain 模型不暴露 LangGraph 类型。
- Agent 状态、工具风险、Provider 能力等核心概念定义在 Domain 层。

陷阱：API 层直接处理 LangGraph 事件，或 Domain 持有 LangGraph 类型，会让运行时框架侵入业务核心。

## 模式四：OpenAI-compatible API 只是协议适配

OpenAI-compatible API 面向 Open-WebUI，负责兼容 `/v1/models` 和 `/v1/chat/completions` 等协议。

规则：
- API 层解析请求，转换为 Application 用例输入。
- 流式响应由 ChatEvent 编码为 `chat.completion.chunk` 和 `[DONE]`。
- 未知字段默认忽略或透传到 Provider options，避免 Open-WebUI 附加字段导致失败。
- 工具执行结果不通过 OpenAI SSE side-channel 暴露，写入内部消息和工具调用记录后进入下一轮 LLM 输入。

陷阱：把 OpenAI 请求模型当作内部领域模型，会导致内部 Agent Runtime 被外部协议绑死。

## 模式五：LLM Adapter 隔离 Provider 差异

Agent Runtime 只依赖 LLMProvider 端口，不直接依赖具体 Provider SDK。

规则：
- Provider 实现负责将内部消息、工具 schema、流式事件转换为具体模型协议。
- ModelInfo 描述模型能力，如是否支持工具、是否支持流式输出。
- LLMResult 和 LLMEvent 是内部标准结果，不暴露具体 SDK 原始对象给 Application 之外。
- 新 Provider 通过 Infrastructure 实现端口接入，不修改 Domain。

陷阱：在 Agent Runtime 中根据 Provider 名称写分支，会让多 Provider 演进失控。

## 模式六：Tool Registry 与权限领域化

Agent 实际可执行工具只来自服务端 Tool Registry。客户端传入 tools 不代表服务端必须执行。

规则：
- ToolDefinition 是领域值对象，包含 name、description、input_schema、risk_level、permissions、timeout_seconds、enabled，不包含具体 handler。
- 工具 handler 属于 Infrastructure，通过 Application 层 ToolService 绑定执行。
- 多个工具 handler 通过 Infrastructure 的组合 executor 按工具名路由；ToolService 只处理定义、风险等级和 enabled 语义。
- 风险等级至少包含 safe、confirm、dangerous。
- safe 默认允许执行；confirm 在 MVP 默认拒绝自动执行并返回 permission_denied；dangerous 默认不暴露给 LLM。
- 文件类工具必须限制在配置 workspace 根目录内，拒绝路径穿越和软链接逃逸。

陷阱：把 handler 放进 Domain，或让 API handler 直接执行工具，会破坏权限审计和后续审批流。

## 模式十：外部知识服务通过工具消费

N-KB 是独立知识服务，N-Agent 只通过 `search_knowledge` safe tool 消费其 HTTP 检索接口。

规则：
- N-Agent 不复制 N-KB 的索引、文档管理或站点管理能力。
- N-KB HTTP client 和响应映射属于 Infrastructure，不能进入 Domain 或 Application 用例模型。
- `search_knowledge` 的启用由配置控制；未配置或禁用时不向 LLM 暴露，异常调用返回 permission_denied。
- Docker Compose 中访问 N-KB 时不能使用指向容器自身的 localhost，应使用服务名、共享网络或宿主机网关地址。

陷阱：把 N-KB 作为内部子域嵌入 N-Agent，或在 ChatService 前置固定检索，会让普通对话链路被 RAG 编排污染。

## 模式七：Memory/Context 通过端口访问

会话、消息、工具调用、任务状态和摘要属于 Agent 运行状态，但具体存储不属于 Domain。

规则：
- Domain 定义 MemoryStore 和 Summarizer 端口。
- Application 通过端口读写上下文和摘要。
- SQLite 是 Infrastructure 实现，不能泄漏到 Application 用例和 Interfaces。
- 摘要策略先简单可替换，后续可升级为模型驱动压缩、session search 和长期 Memory。

陷阱：直接在 LangGraph 节点或 FastAPI handler 中写 SQLite 查询，会让存储实现侵入运行编排和协议层。

## 模式八：System Prompt 属于 Application Runtime 上下文

系统提示词用于约束模型运行行为，但不属于用户会话历史。

规则：
- 系统提示词由 Application 层构建，作为 provider messages 的首条 `system` 消息注入。
- 系统提示词不写入 MemoryStore 的 messages，也不得通过摘要间接持久化。
- Infrastructure Provider 只负责协议转换，不承载 N-Agent 身份、ReAct 行为等业务提示词。
- 新增动态提示词能力时，应优先扩展 Application prompt builder，而不是在 API 层或 Provider 层拼接。

陷阱：把 system prompt 当普通消息保存，会污染 Dashboard 会话历史、摘要和后续上下文恢复。

## 模式九：Dashboard 是调试入口

Dashboard 用于本地演示和观察 Agent 运行，不替代 Open-WebUI，也不承载生产权限系统。

规则：
- Dashboard 发送消息仍走 Application 用例或 OpenAI-compatible 接口。
- 工具调用、摘要和任务状态通过本地只读 API 查看。
- Dashboard 不直接访问 SQLite 表。
- Dashboard 的错误不影响 OpenAI-compatible API。

陷阱：在 Dashboard 内实现独立聊天逻辑，会造成 Open-WebUI 路径和 Dashboard 路径行为不一致。
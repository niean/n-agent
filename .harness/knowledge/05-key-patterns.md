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

## 模式五（扩展）：Active Provider Holder 实现热切换

多 Provider 注册表 + 单一 active provider 的运行时模型由 `ActiveProviderHolder` 适配。

规则：
- Holder 是 Application 层适配器，实现 Domain `LLMProvider` 协议；下游 Application 服务（ChatCompletionService、AgentGraphRunner、SessionService 的 LLMTitleGenerator、ModelService）只依赖 LLMProvider 端口，不感知背后是热切换实现。
- 注入 `Callable[[ProviderConfig, str], LLMProvider]` 工厂；swap 时通过 `asyncio.Lock` 保护，重新构造底层 provider 实例。
- `current_model` / `current_config` 属性供 ModelService.default_model / LLMTitleGenerator.model 以 Callable 形式动态读取，避免 active 切换后老 model 残留。
- 进行中的请求继续使用调用前抓到的旧 provider 引用，新切换不抢占；保证当前 SSE 流不中断。
- Provider 配置由独立 `ProviderRegistry` 端口（SQLite 实现）持久化；明文 api_key 仅通过 `get_secret(id)` 单独读取，仅供 holder 工厂调用，不进入 Domain `ProviderConfig`，不通过 HTTP 暴露。
- 启动 seed：`create_app` 检查 providers 表为空时，按 `Settings.provider_*` 写入一条记录并 activate；后续表为唯一数据源，.env 不再覆盖。

陷阱：把 holder 直接暴露给 Interfaces，或在 ChatCompletionService 中持有具体 OpenAICompatibleProvider 实例缓存，会破坏切换语义并泄漏 Infrastructure 类型。

## 模式六：Tool Registry 与权限领域化

Agent 实际可执行工具只来自服务端 Tool Registry。客户端传入 tools 不代表服务端必须执行。

规则：
- ToolDefinition 是领域值对象，包含 name、description、input_schema、risk_level、permissions、timeout_seconds、enabled、source_type、toolset，不包含具体 handler。
- source_type 表示工具来源大类，当前已使用 builtin、knowledge，并预留 skill、mcp、plugin、agent；toolset 表示能力分组，参考 Hermes 的工具集概念，用于展示和后续按组治理。
- 工具 handler 属于 Infrastructure，通过 Application 层 ToolService 绑定执行。
- 多个工具 handler 通过 Infrastructure 的组合 executor 按工具名路由；ToolService 只处理定义、风险等级、enabled 和 OpenAI schema 暴露语义。
- 风险等级至少包含 safe、confirm、dangerous。
- safe 默认允许执行；confirm 默认拒绝自动执行并返回 permission_denied；仅当 Application 从当前用户消息推导出的 ToolExecutionContext 明确授权且关键参数匹配时，confirm 工具可在本轮执行。
- MCP 站点管理工具 mcp_site_probe/mcp_site_add/mcp_site_refresh 是 confirm，mcp_site_list 是 safe；模型不能直接写配置或 SQLite，只能调用受控管理工具。
- MCP 远端工具通过 ToolService 动态定义源暴露，source_type=mcp，禁用站点、禁用工具、名称冲突或非 object schema 不暴露；执行时由 Application 薄 executor 调 McpService，再进入 Infrastructure MCP client。MCP client 支持 streamable_http、SSE 和 stdio；stdio 站点使用 command/args/env 配置，通过 argv 启动本地进程，不使用 shell，env 继承当前进程环境并由站点 env 覆盖。
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
- AgentGraph 中用于跨节点传递的一次性状态（如 tool_results）在写入 MemoryStore 后必须清空，避免后续节点循环重复持久化同一运行事件。

陷阱：直接在 LangGraph 节点或 FastAPI handler 中写 SQLite 查询，会让存储实现侵入运行编排和协议层；临时运行状态不清空会造成 Dashboard 会话历史重复展示。

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

## 模式十一：Interaction Gateway 统一多入口交互

CLI、飞书 IM 等非 Dashboard 入口通过 GatewayService 接入 Agent，不各自实现聊天和管理业务。

规则：
- 平台入口只做协议解析、验签、消息类型过滤、发送回复和展示转换。
- Application 层 GatewayService 将平台消息标准化为 InteractionMessage，解析 GatewaySessionRegistry 映射后调用 ChatCompletionService、SessionService、ToolService 和 ModelService。
- GatewaySessionRegistry 是 Domain 端口，SQLiteGatewaySessionRegistry 是 Infrastructure 实现；Interfaces 不直接访问 SQLite 或 Infrastructure registry。
- 飞书重复事件通过 GatewaySessionRegistry.mark_event_processed 做幂等，重复事件不再次调用 ChatCompletionService。
- Gateway 破坏性命令确认属于 Application 层：/new、/rename、/delete、/schedule remove 先创建内存 pending confirmation，绑定 GatewaySessionKey、actor_id、target_session_id 和 15 分钟 TTL；确认回调消费 pending 后才执行，一次/本会话信任/取消均不进入 ToolService。
- 飞书使用长连接接收事件，app_id、tenant_key、allowlist 校验和 tenant_access_token 获取属于 Infrastructure client；普通消息 allowlist 使用 event.sender/event.message，card action 必须单独按 event.operator.open_id 和 event.context.open_chat_id 校验。
- Interfaces 飞书长连接适配器只消费已注入的 client 能力：普通文本事件转换为 InteractionMessage，confirmation outbound 渲染为 interactive card，card action 转换为 GatewayService.handle_confirmation。
- CLI 与飞书入口不能绕过 ToolService 风险控制，也不能直接写 provider、tool 或 session 数据表。

陷阱：在 CLI 或飞书长连接适配器里直接 new SQLite store、调用 Provider 或复制 AgentGraphRunner，会形成第二套 Runtime 并破坏 DDD 边界。
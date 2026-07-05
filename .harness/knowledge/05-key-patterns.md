<!-- SUMMARY: N-Agent 的关键实现模式，包括 DDD 边界、协议适配、运行事件、工具权限、Memory 端口、provider 熔断器、Memory Slot 三槽模型与工具面同步、会话默认 profile 派生与 has_override、演进基线、会话来源与ID前缀命名规则、Plugin 子系统 Hermes 兼容装配与工具面路由、Gateway 流式接口与 ChatEvent 消费、CLI 子命令扩展与 _load_xxx_service indirection、ACP stdio 服务端 stdout 纯净性与路径映射 -->
# 关键代码模式

项目中反复出现但不易从单个文件推断的模式，供新功能实现时参照。

## 模式一：Evolution Baseline

N-Agent 是完整 Agent 能力的当前阶段，设计目标是建立可持续演进的架构基线，而不是一次性 demo。

规则：
- 当前实现只覆盖既定验收标准。
- 代码结构、领域模型、端口和依赖方向必须为后续完整 Agent 能力保留扩展点。
- 后续能力包括更多 Provider、工具生态、长期 Memory、权限审批、多 Agent、自动化任务、可观测性和多入口交互。
- 不因未来能力提前实现复杂功能，避免当前阶段范围膨胀。

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

## 模式十：知识检索 SPI 通过工具消费外部 KB

N-Agent 拥有知识检索 SPI，`search_knowledge` safe tool 按 LLM 显式传入的 `kb_id` 路由到已注册 KB 后端；N-KB、Ragflow 都只是外部 KB 协议类型。

规则：
- N-Agent 不复制 KB 后端的索引、文档管理或站点管理能力，Dashboard 只管理 N-Agent 侧的 KB 后端实例配置。
- Domain 定义 KnowledgeBase、KnowledgeBaseRegistry、KnowledgeRetriever、KnowledgeRetrieverFactory 等端口和值对象，不包含 N-KB/Ragflow HTTP 协议细节。
- Application 的 KnowledgeService 负责 KB CRUD、probe、search 和动态 `search_knowledge` ToolDefinition；ToolDefinition 必须要求 `kb_id` 与 `query`，不支持默认 KB。
- Tool description 动态列出 enabled KB 的 id/name/description，LLM 依据描述选择合适 kb_id；disabled KB 不出现在描述中，也不可检索。
- Infrastructure 的 SQLiteKnowledgeBaseRegistry 负责配置和 api_key 存储，HTTP adapters 负责 N-KB/Ragflow 请求与响应归一化；api_key 只通过 get_secret 在 probe/search 时读取，HTTP 响应只暴露 `api_key_present`。
- Legacy `N_AGENT_KB_*` 只在 knowledge_bases 表为空时 seed 一条 `legacy-n-kb`，后续以 registry 为准。
- Docker Compose 中访问 KB 后端时不能使用指向 N-Agent 容器自身的 localhost，应使用服务名、共享网络或宿主机网关地址。

陷阱：把某个 KB 产品作为内部子域嵌入 N-Agent，或在 ChatService 前置固定检索，会让普通对话链路被 RAG 编排污染，并破坏 `kb_id` 显式选择语义。

## 模式七：Memory/Context 通过端口访问

会话、消息、工具调用、任务状态和摘要属于 Agent 运行状态，但具体存储不属于 Domain。

规则：
- Domain 定义 MemoryStore 和 Summarizer 端口。
- Application 通过端口读写上下文和摘要。
- SQLite 是 Infrastructure 实现，不能泄漏到 Application 用例和 Interfaces。
- 摘要策略先简单可替换，后续可升级为模型驱动压缩、session search 和长期 Memory。
- AgentGraph 中用于跨节点传递的一次性状态（如 tool_results）在写入 MemoryStore 后必须清空，避免后续节点循环重复持久化同一运行事件。
- Chat Session 的外部记忆选择是上下文契约，不是普通 UI 偏好：首轮发送前可选择，首轮后由 `sessions.external_memory_enabled_json` 锁定，后续轮次必须使用同一 profile。
- 默认 profile 是 `[]`；系统记忆（builtin）和文件记忆都需首轮前显式勾选。文件记忆最多选择一个，可与系统记忆（builtin）同时启用。已有历史消息但没有锁定值的 legacy session 首次触达时锁定到 `[]`。
- 锁定原因：外部记忆会进入 system prompt 或当前轮 memory context，若同一 session 中途切换，会改变 provider message 前缀，降低 LLM prefix cache 命中率，并让历史回答与新文件记忆混用。
- 前端可以禁用 checkbox 做体验约束，但不能作为可信来源；真正的锁定必须在 Application/MemoryStore 边界执行，Dashboard 刷新或多 tab 也必须以服务端 session detail 为准。

陷阱：直接在 LangGraph 节点或 FastAPI handler 中写 SQLite 查询，会让存储实现侵入运行编排和协议层；临时运行状态不清空会造成 Dashboard 会话历史重复展示。同一 Chat Session 内允许随时切换文件记忆，会破坏上下文稳定性和 prefix cache 友好度；正确做法是新建或 fork 会话。

## 模式七（扩展）：系统记忆 trust/decay/contradiction 内建于 builtin provider

系统记忆（builtin）的 trust 评分、时间衰减、矛盾检测是 provider 内部实现，不外溢到 Domain SPI 或文件记忆（multi-project）provider。

规则：
- sidecar `memory.meta.json` 与 memory.md 并列，存 entry 级 trust/created_at/last_hit_at；memory.md 保持人类可读 Markdown，meta 不污染 system prompt 注入。
- `system_prompt_block` 与 `prefetch` 都必须按 trust×decay 过滤/排序——只改 prefetch 不改 system prompt 会让低 trust 条目仍以"稳定记忆"身份进入 prompt，trust 形同虚设。
- 矛盾检测必须在文件锁内完成（`_update_file_locked` 的 update 回调内读取 current content 做 Jaccard），锁外检测会在并发 add 下漏检并写坏 meta。
- 命中反馈持久化不能押在 shutdown 落盘上——FastAPI lifespan 退出路径必须显式调用 `ExternalMemoryManager.shutdown_all()`（经 `ExternalMemoryService.shutdown()` 转发），且 prefetch 路径要有 dirty flag + 节流落盘作为主路径。
- `initialize` 时必须对 memory.md 现有 entry 做 ensure（不只是 prune stale），否则 demote/boost_on_hit 对未注册 entry 是 no-op，后续 ensure 又重置为 default_trust。

陷阱：把 trust 体系外溢到 ExternalMemoryProvider SPI 会迫使 mem0/holographic/honcho 等检索记忆 provider 被迫实现无关概念；正确做法是 trust 留在系统记忆（builtin）内部，检索记忆 provider 自带各自的可信度机制。

## 模式七（扩展二）：外部记忆 provider 生命周期钩子 fan-out

`ExternalMemoryProvider` SPI 的生命周期钩子（`on_session_switch` / `on_session_end` / `on_pre_compress` / `on_memory_write` / `on_delegation`）由 `ExternalMemoryManager` 统一 fan-out，单个 provider 失败不阻塞其他 provider。

规则：
- 钩子在 SPI 上以 no-op 默认实现声明（`return None` / `pass`），provider 按需覆写；系统记忆/文件记忆通常 no-op，接口先行以保形态稳定。
- Manager fan-out 必须 per-provider try/except，失败仅记 warning日志并 continue；钩子属于旁路通知，不允许阻塞主流程（finalize、压缩、会话切换）。
- `on_delegation(task, result, *, child_session_id)` 在父会话触发，把子 Agent 任务与产出交给父会话 provider；子 Agent 自身 `skip_memory=True`，不持有 provider 会话。事件源依赖多 Agent 编排落地，当前为接口占位。
- subagent 上下文隐含 `skip_memory=True`：不注入 system_prompt_block、不调用 prefetch/sync_turn、工具调用一律拒绝写入。N-Agent 多 Agent 编排尚未落地时，写入闸门已由 `agent_context != "primary"` 守护，读取路径跳过待编排层接入时实现。

陷阱：把钩子做成同步阻塞主流程会让 provider 故障传染到 Chat 主路径；把 subagent 当 primary 暴露 prefetch 会让子 Agent 读到父会话记忆、破坏隔离。事件源未落地时跳过接口占位会让未来编排层反复改 SPI，应先定接口再接事件源。

## 模式七（扩展三）：外部记忆 provider 熔断器

`ExternalMemoryManager` 为每个 provider 维护 per-provider 熔断器（`_ProviderCircuitBreaker`），连续失败超阈值（默认 5 次）进入冷却态（默认 120s），冷却期内跳过调用，冷却到期后允许一次重试，成功重置失败计数、失败重新触发冷却。

规则：
- 熔断器保护每轮调用的 5 条路径：`system_prompt_block` / `prefetch` / `sync_turn` / `on_pre_compress` / `handle_tool_call`；调用前 `is_open` 跳过并记 info 日志，成功 `record_success`、异常 `record_failure`。
- 生命周期钩子（`on_session_switch` / `on_session_end` / `on_delegation` / `shutdown`）不经过熔断器——这些是一次性事件，跳过会丢失语义状态，已有 try/except 隔离足够。
- 熔断器构造参数（threshold / cooldown_secs / clock）通过 `ExternalMemoryManager.__init__` 注入，便于测试用 fake clock 验证冷却恢复。
- `handle_tool_call` 熔断跳过时返回 `{"success": false, "error": "provider in cooldown"}`，区别于工具不存在的 `tool not found` 与 provider 禁用的 `provider not enabled`。

陷阱：把生命周期钩子也接入熔断器会让 `on_session_end` 在 provider 持续故障时永远不调用、丢失清理时机；用 wall clock 测试冷却恢复会让测试等待真实时间，应注入 fake clock；只保护 `sync_turn` 一条路径会让其他路径的故障 provider 每轮重试，违背 G8 验收。

## 模式七（扩展四）：Memory Slot 三槽模型与工具面同步

不变式：检索记忆槽（external-query）全局至多 1 个 active provider，mem0 / holographic / honcho 互斥，激活其一自动 deactivate 其他。理由：多后端并存导致工具面 schema 膨胀、system prompt 指令冲突、新会话默认 profile 派生歧义。约束继承自 Hermes `MemoryManager` 单 external provider 限制。系统记忆（builtin）、文件记忆（multi-project）不受此约束，三槽共存。

`ExternalMemoryManager` 采用三槽模型管理外部记忆 provider：系统记忆（builtin，全局内置 Markdown 记忆）、文件记忆（multi-project，多项目 Markdown CRUD）、检索记忆（external-query，query-only provider，至多一个）。三槽共存，activate mem0 不会替换文件记忆。

规则：
- `add_provider(provider)` 按 provider name 调用 `_classify_slot` 分类槽位：name=="builtin" → 系统记忆槽（builtin）、name=="multi-project" → 文件记忆槽（multi-project）、其余 → 检索记忆槽（external-query）。系统记忆/文件记忆始终接受并 append 到 `_providers`；检索记忆槽至多一个，第二个记录 warning 并拒绝。
- `swap_external_query_provider(new_provider)` 仅替换检索记忆槽（external-query），不动系统记忆/文件记忆。返回 `{"swapped": bool, "tool_surface_refresh_failed": bool}`。不调用 `new_provider.initialize()`——遵循 add_provider 模式，由调用方（service.activate 或 main.py startup）负责 initialize，避免 HolographicAdapter 等 adapter 被 double-initialize 导致 SQLite 连接泄漏。
- 工具面回调在 `swap_lock` 锁内同步执行：`_fire_tool_surface_callbacks()` 在 `with self._swap_lock:` 块内调用，保证 activate 返回时 ToolService dynamic_definitions 与 Composite routes 已一致。回调异常不阻塞 swap，记录 warning 并返回 `tool_surface_refresh_failed=True`。
- 持锁期间工具调用返回 `provider_swapping`：`handle_tool_call` 在检索记忆槽工具上检测 `_swap_lock.locked()` 时直接返回 `{"success": False, "error": "provider_swapping"}`，避免 swap 进行中路由到不一致的工具实现。
- main.py 注册工具面回调 `_refresh_external_memory_tools`：swap 后刷新 `tool_service.set_dynamic_definitions("external_memory", ...)` + Composite routes。清理 stale 路由（用 `_is_external_memory_tool_name` 识别 external memory 域工具名，不在当前 tool_defs 中的移除）再添加新路由，避免路由表单调增长。
- 启动时遍历 registry.list_providers()，对 enabled=True 的检索记忆 provider 调用 factory + initialize + swap 装载（至多一个，break）。

陷阱：用 `name != "builtin"` 判断 external 会让文件记忆（multi-project）占据检索记忆槽、activate mem0 被拒绝；在锁外触发回调会让另一线程在 swap 返回前看到不一致的工具面；swap 内调用 initialize 会让 HolographicAdapter double-initialize 泄漏 SQLite 连接；回调只加路由不清 stale 会让 routes dict 单调增长（功能上 dynamic_definitions 已无该工具，execute 会返回 "tool not found"，但内存泄漏）。

规则：active 检索记忆 provider 配置变更必须复用 activate 装配路径重建 adapter。`ExternalMemoryProviderService.update()` 在写完 registry 后，若 `cfg.enabled` 为 True（at-most-one-enabled 语义下即 active），读最新 cfg + secret，调 factory 构建 adapter，`adapter.initialize(session_id="", project_root=workspace_root)`，再 `manager.swap_external_query_provider(adapter)` 重建槽位并刷新工具面；返回 `tuple[cfg, tool_surface_refresh_failed: bool | None]`，非 active 时第二项为 None。initialize 或 swap 异常 catch 后返回 `refresh_failed=True` 并保留旧 adapter（registry 已先写、无法回滚 enabled，保留旧 adapter 保证运行时可用）。Dashboard PATCH 路由解包 tuple 并把 `tool_surface_refresh_failed` 加入响应（与 activate 响应对齐）。

陷阱：用 `manager.get_active_external_query_provider_name()` 匹配判定 active 会在同次 update 修改 name 字段时失效（manager 槽位仍持旧 name adapter）导致 silent-no-op，应直接用 `cfg.enabled` 判定；update 只写 registry 不 reload 会让已激活 provider 的 recall_mode/base_url/extra_config 编辑静默不生效（UI 展示新值但运行时仍是旧配置）；reload 失败时抛异常到调用方会让 active 槽位空置，应 catch 保留旧 adapter。

## 模式七（扩展五）：会话默认 profile 派生与 has_override

ChatCompletionService 首轮锁定 `external_memory_enabled` 时，需区分"客户端未传字段"与"客户端显式传 ['builtin']"。检索记忆 provider 的 active（装载 adapter）与 enabled（per-session 使用）解耦：active 是使用前提但不自动启用，是否使用交给对话页 per-session 勾选。

规则：
- 派生优先级：(1) session 已有锁定值 → 沿用；(2) legacy session（已有历史消息但无锁定值）→ `[]`；(3) `has_override=True`（`options` 中存在 `external_memory_enabled` 键）→ 归一化 requested_memory；(4) 未传字段 → `[]`。未传字段不自动启用 builtin，也不自动纳入 active external-query provider。
- `has_override` 用字段存在性判断（`"external_memory_enabled" in request.options`），区分"未传字段"与"显式传空数组/显式传 ['builtin']"，避免把 UI 默认状态误当作用户选择。
- `ActiveExternalMemoryReader` 端口保留（`ExternalMemoryProviderService.get_active_provider_names` 读 manager 内存、无 IO），但不再消费于默认 profile 派生；接口留存供未来别处使用。
- 检索记忆 provider 的 per-session 启用复用现有 `enabled_override` 机制：`_is_enabled(name, override)` 当 override 含 external-query provider 名时返回 True，无需改 SPI。
- `ExternalMemoryManager.list_providers()` 对 external-query slot 的 active provider 输出 `{"name", "enabled_global": False, "slot": "external-query", "active": True}`；`enabled_global` 恒为 False（不走 global_config），`active` 表示 adapter 已装载。builtin/multi-project 不输出 `active` 字段。
- 前端 chat.js 勾选区：builtin 与 external-query slot 的 provider 默认都不勾选；external-query 仅当 `active` 时显示（不依赖 `enabled_global`）。互斥按 slot 分组，仅 multi-project 之间互斥（文件记忆最多 1 个），external-query 与 multi-project 可共存。checkbox 带 `data-slot` 属性。`externalMemoryTouched` 标志：用户未操作时不发送 `options.external_memory_enabled` 字段，操作后发送显式值。

陷阱：让 active 自动纳入默认 profile 会混淆"装载"与"使用"，用户既看不见也关不掉检索记忆 provider；前端用 `enabled_global` 过滤 external-query provider 会导致其永不显示（不走 global_config）；检索记忆与文件记忆纳入同一互斥规则会阻止两者共存，违背 fact 库与 markdown 知识各司其职的设计。

## 模式七（扩展六）：历史会话忠实展示外置记忆 Provider

历史会话锁定的 `external_memory_enabled` profile 可能引用当前已非 active 的检索记忆 provider（被另一同类 provider 替换 active、或已从 registry 删除）。对话页勾选区必须忠实展示当时选择，不受当前 active 筛选影响。

规则：
- `ExternalMemoryService.list_providers()` 通过可选注入的 `external_query_catalog`（延迟绑定 `ExternalMemoryProviderService.list`，返回 registry 全量 `ExternalMemoryProviderConfig`）合并 inactive 检索 provider：catalog 中 name 未出现在 manager 输出的，追加 `{"name", "enabled_global": False, "slot": "external-query", "active": False}`。catalog 为 None 或抛异常时退化为现状（仅 manager 输出），向后兼容。
- 前端 chat.js `visibleProviders` 过滤：external-query slot 的 provider 满足 `active === true` 或（`useSessionConfig && enabledProviders.includes(name)`）即展示；其他 slot 维持原 `enabled_global` 过滤。
- phantom 兜底：`useSessionConfig` 为 true 时，遍历 `enabledProviders`，name 既不在 `externalMemoryProviders` 响应中又非 `builtin` 的，合成 phantom 条目并入 `visibleProviders`。slot 优先取锁定时持久化的 `external_memory_slots` 映射（见下条），缺失才回退到 `'removed'` 分组。phantom 条目渲染为 checked+disabled，label 追加"(已删除)"；非 active 检索 provider（`active: false`）label 追加"(已禁用)"。
- slot 持久化：`ChatCompletionService.complete` 锁定 profile 时，通过注入的 `slot_resolver`（`ExternalMemoryManager.resolve_provider_slot`，基于当前已装载 provider 解析 name→slot，无 IO）构建 `{name: slot}` 映射，随 `lock_session_external_memory` 一并写入 `sessions.external_memory_slots_json` 列。session detail 响应含 `external_memory_slots`，前端据此将已删除 provider 归入其原 slot 分组（文件/检索），而非统一塞进"已移除"。`resolve_provider_slot` 对 builtin/multi-project 项目名/external-query active provider 名返回对应 slot，对未装载 name 返回 None（不入映射）。
- 锁定会话的 checkbox `disabled = locked`，checked 由 `enabledProviders.includes(name)` 决定；active 但未选中的检索 provider 展示为 unchecked+disabled。

陷阱：仅靠 manager `list_providers()` 无法展示 inactive provider（external-query slot 至多装载一个 active adapter，未 active 的不在 `_providers`）；前端只按 `active === true` 过滤会丢弃历史 profile 引用；不补 phantom 兜底会导致已删除 provider 的历史会话勾选状态丢失。phantom 硬编码 slot（如统一 `'external-query'`）会把已删除的文件记忆误归检索分组——必须持久化 slot 映射，按原 slot 分组展示。

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

## 模式十二：trusted_metadata 端到端透传与 Managed Tool 授权

涉及来自非可信客户端 metadata 的工具授权决策时，必须通过独立 trusted_metadata 通道，且仅服务端可写入。

规则：
- ChatCompletionInput 同时携带 `metadata`（untrusted，可由 OpenAI HTTP 客户端写入）与 `trusted_metadata`（trusted，仅 GatewayService/Feishu 长连接适配器写入）。
- ChatCompletionService.complete 在每次调用时构造 ToolExecutionContext，把 trusted_metadata 拷贝进去，并通过 `_compute_permitted_managed_tools(mode, trusted_metadata)` 决定 `permitted_managed_tools`。当前规则：mode=realtime 且 `trusted_metadata.gateway.platform` 为合法 Gateway（feishu）才返回 `{"manage_schedule"}`，否则空集。
- 上下文通过 LangGraph `configurable.options["tool_execution_context"]` 传递；执行节点（call_llm/execute_tools）必须在 config 缺失时回退到 `state.run_options`，避免 LangGraph 框架精简 config 导致 context 丢失。
- ToolService.execute 对 `definition.managed=True` 强制检查 `request.name in context.permitted_managed_tools`，否则返回 `permission_denied`，不调用 handler。
- 受 managed 保护的工具同时要求来源方可信：`ScheduleManagementToolExecutor` 进一步从 `context.trusted_metadata` 读取 platform/receive_id/receive_id_type/thread_id 作为任务 origin，禁止从 untrusted metadata 读取。`_origin_from_trusted` 把这四个字段一并写入 `ScheduledTask.origin` 与 `DeliveryTarget.context`，由 outbound 按 `platform` 路由投递。
- 删除等需要确认的破坏性动作不允许 Agent 直接执行；自然语言删除要返回 confirmation_required 文案，引导用户走 `/schedule remove <id>`。Gateway 破坏性命令 preflight 时把当前飞书 trusted_metadata 写入 `GatewayConfirmationRequest.trusted_metadata`，handle_confirmation 还原后再校验 task.origin 一致性。
- 不可信模式（unattended/safe_only、定时任务执行）时 `list_openai_tools` 必须过滤 source_type=AGENT 的工具，避免调度器递归调用自己。

陷阱：把 OpenAI HTTP 客户端 metadata 直接当 trusted_metadata 用，或者只在 ToolExecutionContext 里塞 metadata 不区分 trusted/untrusted，会让伪造 `gateway.platform=feishu` 的 OpenAI 客户端获得飞书会话的 schedule 操作权限。

## 模式十三：平台聚合与主动外发按 platform 路由

后台任务（定时任务等）回投到 IM 平台时，按 `DeliveryTarget.context.platform` 路由到对应平台 client；Dashboard 平台页通过 PlatformService 读取 PlatformRegistry 与 GatewaySessionRegistry，不直接读 SQLite。

规则：
- `Platform` 面向 feishu、dingtalk、wecom 等外部消息平台；GatewaySessionKey 使用 source/platform_session_id/thread_id 作为 conversation key。CLI/TUI 终端聊天使用 source=`cli`，不进入 PlatformRegistry，也不出现在 Dashboard 平台页。
- `PlatformRegistry` 提供 descriptor 与 lifecycle；Application 的 PlatformService 合成 status、session_count、last_active_at、active_sessions 等只读视图。
- `FeishuImAdapter` 是飞书入口适配器，同时实现 PlatformLifecycle；start() 先标记 connected，listen_events 正常返回后标记 disconnected，异常时写入 fatal("feishu_listen_error", message) 并继续抛出。
- `ScheduleOutboundDelivery.deliver` 只判断 `platform`：feishu 则调用注入的 FeishuClient.send_text；缺失或未知 platform 返回 failed，不做 receive_id 启发式回退。
- Gateway `_build_trusted_metadata` 必须写入 `gateway.platform` 与顶级 `platform`；Tool 落库 origin 时一并保存 platform/receive_id/receive_id_type/thread_id 四元组，跨 origin 操作统一返回 task not found。
- Gateway 支持平台级 home target：`/sethome` 更新 GatewaySessionRegistry 中当前 platform 的 home chat；`/schedule add` 创建 Feishu/平台 origin 任务时保存 `target=home` 逻辑引用而不是固定当前聊天会话。
- ScheduleOutboundDelivery 在投递 Feishu origin 时如注入 home resolver，则发送前动态读取当前 home target；即使历史任务仍保存旧 receive_id，只要当前 platform 配置了 home target，通知也会切到最新 home chat。
- origin 定时任务的执行 session 必须是 schedule-owned session，不能绑定 Gateway 当前会话；当 origin 任务因历史绑定会话删除进入 `session_missing` 时，ScheduleService 在 run_now/runner claim 前创建新的 schedule session 并恢复 ACTIVE。

陷阱：把"平台能力"放进每条消息的 capability 列表，意味着任何中间层漏传一次都会让正常功能变成 fail-closed；把能力归到 platform 注册（client/lifecycle 是否注入）才是单一来源。

## 模式十四：定时任务 claim lease 只保护正在执行的单次触发

定时任务由 SQLiteScheduledTaskRegistry 负责原子 claim、推进 next_run_at 和记录执行；ScheduleRunService 负责执行与投递。

规则：
- claim 时设置 claim_id、lease_owner、lease_until，并立即按 cron 推进 next_run_at，避免同一触发被并发 runner 重复领取。
- lease 只表示当前 claim 的执行保护期，不表示整个任务的冷却周期；执行完成后必须释放 lease_until，否则短周期任务会被旧 lease 阻塞。
- record_execution_completed 仍按 claim_id/lease_owner 校验当前 claim，防止过期执行覆盖新 claim；释放 lease 不应清空 claim_id/lease_owner，因为后续 delivery result 仍需要一致性校验。
- skipped_missed 只用于 runner 长时间未运行或任务确实错过宽限窗口；不能由正常执行留下的 lease 触发。

陷阱：把 lease_seconds 当作调度间隔或执行后保留 lease，会让 */5 任务被默认 900 秒 lease 卡成 15 分钟一次，并在 missed_grace_seconds 后持续 skipped_missed。

## 模式十五：检索记忆 Provider base_url 装配链路

检索记忆 provider（mem0/honcho）的 `base_url` 是 provider 顶层字段（SQLite `external_memory_providers.base_url` 列，由 Dashboard CRUD 保存），而 adapter 从 config dict 读 base_url。Application 层构造 adapter 时必须显式合入 base_url，否则 Dashboard 配置被静默忽略。

规则：
- `ExternalMemoryProviderService._build_adapter_config(cfg)` 返回 `{"base_url": cfg.base_url, **cfg.extra_config}`，activate/probe 统一调用此方法构造 config dict 传给 factory。
- adapter 从 config dict 读 base_url 时提供默认值（mem0 默认 `https://api.mem0.ai/v3`），但默认值只在 base_url 缺失/空串时生效；Dashboard 显式配置必须能覆盖默认值。
- holographic 不受此约束（db_path 等配置走 extra_config，无顶层 base_url）。
- base_url 空串是合法值（holographic 创建时 base_url=""），adapter 各自处理空串回退。

陷阱：`factory(dict(cfg.extra_config), ...)` 只传 extra_config 会让 mem0 回退默认地址、honcho 回退空串，Dashboard 填写的自建 mem0 实例地址被静默忽略，probe/activate/sync/search 全部走错地址且无报错。此 bug 同时影响 mem0 + honcho，根因在共享装配路径，需在 service 层统一修复而非各 adapter 单独处理。

## 模式十六：会话来源与 ID 前缀命名规则

会话 `source` 字段和 `session_id` 前缀必须一一对应，体现入口/触发方式。来源格式为单一一级，无二级；IM 平台直接作为一级来源，与其它入口同级。

一级来源：

| 一级 | 含义 | source 取值 | session_id 前缀 |
|------|------|------------|----------------|
| dashboard | Web 控制台 | dashboard | dashboard- |
| api | 非 dashboard 的 HTTP API（如 OpenWebUI 兼容接口） | api | api- |
| cli | 本地命令行 | cli | cli- |
| feishu | 飞书 IM | feishu | feishu- |
| dingtalk | 钉钉 IM | dingtalk | dingtalk- |
| wecom | 企业微信 IM | wecom | wecom- |
| acp | ACP stdio 客户端 | acp | acp- |
| schedule | 定时触发 | schedule | schedule- |

规则：
- session_id 前缀严格等于一级名，UUID 跟在连字符后（如 `dashboard-{uuid}`、`feishu-{uuid}`）。
- CLI 入口虽然走 GatewayService，但单列为一级 `cli`（前缀 `cli-`、source `cli`），不归入 IM 平台一级。GatewayService 通过 GatewaySessionKey.platform 分流：source=`cli` → (`cli`, `cli`)，真实 IM 平台 → (`{platform.value}`, `{platform.value}`)。
- `schedule` 是触发方式不是平台，独立成一级，不再写成 `http/schedule`。
- Dashboard 前端生成 session_id 用 `crypto.randomUUID()`（fallback `Date.now()+random`），不用 `Date.now()` 时间戳（碰撞风险）。

历史数据迁移：
- `SQLiteMemoryStore.migrate_session_id_prefixes()` 在 `build_application_services` 末尾（所有 registry 初始化后）调用，幂等。
- 旧 → 新映射：
  - `session-`→`dashboard-`(source=dashboard)
  - `tmp-`→`api-`(source=api)
  - source `local`→`cli`(id 补 `cli-` 前缀)
  - source `feishu/dingtalk/wecom`+id `gateway-`→`{platform}-`(source={platform})（更早格式）
  - source `gw/feishu`/`gw/dingtalk`/`gw/wecom`+id `gw-`→`{platform}-`(source={platform})（spec-260702 格式）
  - `schedule`/`acp` 不变
- 级联更新 11 张 session_id 引用表：messages、tool_calls、task_states、summaries、sandbox_execution_history、sandbox_released_history、scheduled_tasks、scheduled_task_executions、gateway_session_links、gateway_conversations(active_session_id)。
- 碰撞保护：新 id 已被其它 session 占用时跳过该条并 warning。
- 外部记忆服务（honcho/mem0）按旧 session_id 索引的上下文不迁移，迁移后旧会话的外部记忆上下文丢失，新会话不受影响。

陷阱：
- 把 IM 平台归入 `gw/` 二级会混淆"触发方式"和"IM 平台"两个维度，扩展到 `gw/webhook`、`gw/wx` 时维度越来越乱。正确做法：IM 平台直接作为一级，与 dashboard/api/cli/acp/schedule 同级。
- session_id 前缀与 source 脱节（如 dashboard 用 `session-`、api 用 `tmp-`、IM 用 `gateway-`）会导致会话列表无法直接从 id 归因入口，必须查 source 字段。前缀严格等于一级名即可。
- `delete_session` 级联未覆盖 `scheduled_task_executions`、`sandbox_execution_history`、`sandbox_released_history`，删 session 后这 3 张表会留下孤儿行（与命名规则无关，但同属 session_id 引用完整性问题）。

## 模式十七：Plugin 子系统 Hermes 兼容装配与工具面路由

Plugin 子系统遵循 Hermes plugin 模式（`plugin.yaml` + `register(ctx)`），通过四层装配实现零成本移植开源插件生态。

装配链路：
1. `build_application_services`（app/main.py）创建 `SQLitePluginRegistry` + `PluginFileLoader`（bundled_root 指向 `app/infrastructure/plugin/seeds`）+ `PluginService`，同步执行 `plugin_service.scan()` 完成首批工具面注入
2. `PluginToolExecutor(service)` 持有 service 引用，通过 `plugin_tool_executor_holder["executor"]` 字典在 `ToolService` 构造前先占位、构造后回填，避免循环依赖
3. `CompositeToolExecutor(routes, fallback=McpToolExecutor)` 中 plugin 工具名走显式 routes（`route_refresher` 用 `_is_plugin_tool_name` identity check 过滤，仅 plugin 工具名触发 routes 重建），MCP fallback 不回归
4. lifespan 启动期再 `await plugin_service.scan()` 一次（兜底，build_application_services 已扫描则幂等）
5. `PluginService.scan()` 用 `asyncio.Lock` 串行化，防止并发 refresh 导致 `_registrations`/`_plugin_registrations` 字典竞态

工具面注入：
- `PluginService._refresh_tool_surface` 调 `ToolService.set_dynamic_definitions("plugin", defs)`，plugin 工具 `source_type=PLUGIN`，与 builtin（BUILTIN）/skill（SKILL）/mcp（MCP）/knowledge（KNOWLEDGE）区分
- 全局冲突检测：扫描 `ToolService.list_definitions()` 中 `source_type != PLUGIN` 的工具名，若 plugin 工具名与之冲突标记 unavailable（`override=True` 仍标记 unavailable，因静态工具替换本期未实现，仅 gate 不执行）

Plugin 文件加载：
- `PluginFileLoader` 扫描三源（bundled < user < project 优先级，后者覆盖前者），支持扁平（`<root>/<plugin>/plugin.yaml`）和两级（`<root>/<category>/<plugin>/plugin.yaml`）目录结构
- 稳定包 import：创建父包 `n_agent_plugins`（`__path__=[]`），用 `spec_from_file_location`+`submodule_search_locations` 加载 plugin `__init__.py`，重载前清理 `sys.modules` 中 `n_agent_plugins.<key>.*` 全部子模块避免污染（仅删 top-level 模块会残留 schemas/tools 等子模块导致 AttributeError）
- 仅 `PluginKind.STANDALONE` 且在 `plugins_enabled` 列表中的 plugin 才执行 `register(ctx)`；非 STANDALONE kind 仅识别不加载

Secret 隔离：
- `plugin_secrets` 表独立于 `plugins.config_json`，FK ON DELETE CASCADE 跟随 plugin 删除
- `Plugin.to_public_view().secret_refs` 仅返回 `{field: bool}` 占位，由 registry 查询 plugin_secrets 填充
- 前端 `plugin.js` 配置编辑 modal 中，`config_schema` 标记 `secret: true` 的字段用 `secret:` 前缀命名，submit handler 据此路由到 `secret_updates` 而非 `config`，避免明文落入 `plugins.config_json`
- `requires_env` 字段同样走 `secret:` 前缀路由

陷阱：
- `ApplicationServices` 是 `frozen=True` dataclass，不能在构造后注入 `plugin_service`，必须在构造器中一次性传入
- `route_refresher` 必须用 identity check（`_is_plugin_tool_name(name)`）而非"工具名以某前缀开头"等模式匹配，否则误触发 MCP routes 重建导致 fallback 回归
- `/chat/plugins:refresh` 路由必须在 `/chat/plugins/{key:path}` catch-all 之前注册，否则 refresh 被 catch-all 吞噬
- `replace_all_plugins` 不能 DELETE 缺失的 plugin 行，只能标记 `last_scan_status='missing'`+`enabled=0`，保留历史 config/secrets 供 plugin 恢复后复用

## 模式十八：Gateway 流式接口与 ChatEvent 消费

Gateway 同时提供非流式 `handle_message` 与流式 `handle_message_stream` 两个入口，两者必须共享同一套幂等、destructive preflight、session 解析、Slash 分流、默认模型和 trusted metadata 逻辑，差异仅在最终产出形态（InteractionResponse vs AsyncIterator[ChatEvent]）。

流式接口契约：
- `handle_message_stream(event) -> AsyncIterator[ChatEvent]` 复用 `mark_event_processed(platform, event_id, message_id)` 幂等检查；重复事件 yield `DONE(metadata={"duplicate": True})` 后结束
- destructive preflight 命中时 yield `MESSAGE_DONE(content=提示文案, finish_reason="confirmation_required", metadata=GatewayOutboundMessage.metadata)`，metadata 含 `confirmation.id` 供 CLI 发起 `/confirm` 回调
- Slash 命令通过 `command_service.handle` 处理，每个 outbound message 转一个 `MESSAGE_DONE`，最后 yield `DONE`
- 普通消息调用 `ChatCompletionService.complete(stream=True)`，返回值必须是 async iterator；若返回 `ChatCompletionResult` 则 yield `ERROR`

ChatEvent.metadata 用途：
- 携带 Gateway confirmation metadata（`confirmation.id`），避免 CLI 解析中文提示文案提取 id
- 携带 `duplicate` 标记，CLI 据此输出 warning 而非正常内容
- 未来扩展 session_id 等事件级信息

AgentGraph 工具事件回放：
- `execute_tools` 节点在工具执行前向 `state.stream_tool_events` 追加 `TOOL_CALL_DELTA(status="pending")`，执行后追加 `TOOL_CALL_DELTA(status="success"|"error", duration_ms=...)`
- `stream_events` 在 `await self.run()` 完成后回放 `state.stream_tool_events`，再输出 `CONTENT_DELTA`
- 当前 graph 仍先完成一次完整 run 再分块输出 content（非 token 级流式），provider token 级流式需改 `LLMProvider.chat(stream=True)` 与 graph call_llm，本期未做

CLI REPL TUI 隔离：
- prompt_toolkit 与 rich 直接同时写 stdout 会乱屏，REPL 整个 loop body（prompt + handle_input + rich 输出）必须在 `patch_stdout()` 上下文内
- 流式消费运行在 `asyncio.create_task` 包裹的 cancellable task 中，Ctrl+C 时 cancel task 并 `aclose` async iterator
- 非 TTY 降级用 `input()` 循环（catch EOFError），避免 prompt_toolkit 在管道/CI 环境异常

## 模式十九：CLI 子命令扩展与 _load_xxx_service indirection

7 领域 CRUD 子命令（provider/knowledge/mcp/schedule/sandbox/memory/platform）+ 3 运维子命令（doctor/config/logs）+ sessions `--browse` picker 复用统一扩展模式，确保 DDD 边界、测试隔离与 secret 脱敏。

`_load_xxx_service()` indirection：
- 每个命令模块顶部定义 `_load_xxx_service()`，函数体 `from app.main import build_application_services; return build_application_services().xxx_service`
- 命令函数通过 `_load_xxx_service()` 取 service，不直接 import `build_application_services`
- 测试 monkeypatch `_load_xxx_service` 返回 FakeService，避免触发真实 SQLite/loader/网络初始化
- skill/plugin 子命令已在 spec-260704-cli-experience.md 建立该模式，本期 10 个新子命令沿用

async/sync service 调用约定：
- async service（Provider/Knowledge/MCP/Schedule/Sandbox/Platform/Session）：用 `asyncio.run(service.method(...))`
- sync service（ExternalMemoryProviderService/ExternalMemoryService）：禁止 `await`/`asyncio.run`，直接 `service.method(...)`
- `_load_xxx_service()` 可能返回 None（sandbox/external_memory 在 disabled 时），命令必须检测 None 并返回 disabled/WARN/错误码

secret 脱敏约定：
- Provider/Knowledge/ExternalMemory list/get 输出 `api_key_present: bool`，不输出明文；service 已返回脱敏领域对象，CLI 层不得读 `get_secret()`
- MCP env 输出按 key 名兜底脱敏（key 含 token/password/secret/key 时 value 替换为 `***`）
- config 子命令遍历 Settings 字段，字段名含 `api_key`/`secret`/`password`/`token` 或以 `_key` 结尾时输出 `{field}_present: bool`

sessions `--browse` picker：
- TTY + 无 `--pick`：调 `gateway_registry.list_session_links(GatewaySessionKey("cli", conversation_id))` 取结构化 `GatewaySessionLink` 列表，prompt_toolkit Application + fuzzy filter，选中后调 `session_service.get_session_detail(id)` 渲染
- 非 TTY 或 `--no-interactive`：降级为 rich 表格输出 session link 列表
- `--pick <id>`：跳过 picker，直接 `get_session_detail(id)` 渲染
- Ctrl+C → rc=130
- 不从 `CliChatAdapter.send("/sessions")` 的 markdown 文本中反解析 session id

doctor 检查模式：
- 8 项检查独立 try/except，每项返回 PASS/WARN/FAIL dict（dimension/status/detail）
- `--probe` flag 默认 off；off 时只做 registry/config 检查，不发网络 probe
- on 时 Knowledge 调 `probe_base(id)`、MCP 调 `probe_site(payload)`（先 get_site 构造 payload）、ExternalMemory 同步调 `probe(id)`
- sandbox disabled → WARN（不解引用 None）
- 退出码：有 FAIL → 1，全 PASS/WARN → 0

logs 范围限制：
- 不读 SQLite 表，只调 Application Service 已暴露的方法
- 4 子动作：sandbox（`SandboxDashboardService.list_execute_code_history`，sandbox disabled 时 rc=0）/tools（`SessionService.list_tool_calls`，CLI 本地截断 limit）/scheduled（`ScheduleService.list_executions`，limit 1..50）/runs（`SessionService.get_session_detail`，输出 task_state）
- 不支持"全局最近 N 条运行历史"（无对应 service 方法）

陷阱：
- `ChatEvent` 是 `frozen=True` dataclass，`metadata` 必须用 `field(default_factory=dict)` 否则共享可变状态
- CLI 构造 `InteractionMessage` 时必须注入 `actor_id=cli:{conversation_id}`，否则 `/new` 等破坏性命令会绕过 confirmation 直接执行（actor_id 为空时 preflight 视为无 actor 不需要确认）
- CLI `trusted_metadata.gateway.source` 必须为 `"cli"`，且不得写入 `gateway.platform`；`ChatCompletionService._compute_permitted_managed_tools` 只认可真实平台 `gateway.platform=feishu`，CLI 不得获得 Feishu 专属 managed tool（如 `manage_schedule`）
- `app.interfaces.cli:main` console script 入口由 `__init__.py` re-export `main`，迁移单文件为 package 时必须删除旧 `cli.py` 避免同名冲突

## 模式二十：ACP stdio 服务端 stdout 纯净性与路径映射

N-Agent 内置 ACP（Agent Client Protocol）stdio JSON-RPC 服务端，让 VsCode/Zed 等 ACP 兼容客户端通过 `docker exec -i n-agent-n-agent-1 n-agent acp` 或 `kubectl exec -i <pod> -- n-agent acp` 接入容器内 Agent。stdout 承载纯 JSON-RPC 帧，所有日志/诊断走 stderr，路径映射由环境变量配置。

stdout 纯净性规则：
- `n-agent acp` 主循环由 `acp.run_agent(agent, use_unstable_protocol=True)` 驱动，stdout 是 JSON-RPC 帧通道；任何 stdout 污染（日志、提示文案、import warning）都会破坏协议帧解析
- `_configure_logging()` 在进入主循环前清空 root logger 的所有 handler，重新挂载单个 `StreamHandler(sys.stderr)`，并附加 `_BenignMethodNotFoundFilter` 抑制 ACP SDK 通过 `logging.exception` 记录的 benign ping/health method-not-found 噪声
- `_run_check()` 与 `_run_setup()` 把诊断/引导信息全部 `print(..., file=sys.stderr)`，stdout 留空；测试用 subprocess + 临时文件捕获 stdout 验证纯净性（不能直接用 PIPE，因 ACP SDK asyncio write transport 与 subprocess pipe 交互可能导致挂起）
- 第三方库（langchain deprecation warning、seed runner 日志）的 stdout 噪声由 `_configure_cli_env()` 在 CLI 入口处 `warnings.filterwarnings` + `logging.getLogger(...).setLevel(ERROR)` 抑制

路径映射规则：
- ACP cwd 来自宿主/editor，N-Agent 文件工具运行在容器/Pod，必须通过 `N_AGENT_ACP_HOST_WORKSPACE_ROOT` + `N_AGENT_ACP_CONTAINER_WORKSPACE_ROOT` 环境变量配置映射；未配置 host root 时所有宿主 cwd 都不可映射，`session/new` 拒绝
- 映射优先级（`path_mapping.map_cwd`）：(1) cwd 在 host root 下时替换前缀为 container root；(2) cwd 已在 container root 下时原样使用；(3) cwd 为空时使用 container root；(4) cwd 不可映射时返回 None，`session/new`/`resume_session` 抛 ValueError 返回协议错误，禁止回退到 `Path.cwd()`（回退会让 host 路径泄漏到容器内 metadata，破坏后续文件工具调用）
- 开发环境通常 host root 设为宿主项目目录、container root 设为容器内挂载点（与 docker-compose volumes 挂载源一致）；K8s 部署的 Pod 名动态生成，VsCode 客户端配置需用 `kubectl get pods -l app=n-agent` 查询实际名称填入

ApprovalDecider 桥接规则：
- Domain `ApprovalDecider` 端口定义 `decide(request: ApprovalRequest) -> ApprovalDecision` 接口；ACP 服务端通过 `ACPPermissionBridge` 实现，把 N-Agent confirm 工具授权请求转换为 ACP `PermissionOption`（allow_once/allow_always/reject_once/reject_always）
- `ChatCompletionInput` 增加可选 `approval_decider` 与 `allowed_confirm_tools_override` 字段，`ChatCompletionService.complete` 通过 `dataclasses.replace(ctx, approval_decider=..., allowed_confirm_tools=...)` 注入到 `ToolExecutionContext`（frozen dataclass 必须用 replace 不可直接赋值）
- `ACPPermissionBridge._persist_session` 把 allow_always/reject_always 决策持久化到 `sessions.acp_metadata_json`，best-effort 持久化（metadata_updater 异常 try/except: pass，不阻塞工具执行主路径）
- `reject_once` 返回 `scope="deny"`（非 "once"），与其他拒绝路径一致；ACP `PermissionOption` 构造接受 `option_id`（snake_case）并 alias `optionId`

ACP session 桥接规则：
- ACP session 与 N-Agent session 通过 `ACPSessionBridge` 桥接：`create` 新建 N-Agent session（source="acp"）并写 acp_metadata；`load` 读取已有 session 与 metadata；`resume` 复用 session 并更新 cwd；`fork` 创建子会话继承父上下文；`list` 返回最近会话（cursor 分页，删除 session 时 cursor_found flag 处理边界）；`close` 调用可选 cleanup_callback（sync 或 async，`inspect.isawaitable` 判定）
- ACP 客户端重连时通过 `session/load` 恢复会话，元数据从 `sessions.acp_metadata_json` 读取，避免 cwd 丢失
- `AgentGraphRunner` 支持 task state interrupt 注册与查询，cancel 时通过 `asyncio.CancelledError` 中断 prompt；`stream_events` 节点捕获 CancelledError 后 yield ERROR+DONE，prompt 节点 re-raise

ACP 用户消息桥接规则：
- `session/prompt` 不直接调用 `ChatCompletionService.complete`；`NAgentACPAgent.prompt` 先把 ACP session 绑定到 `GatewaySessionKey("acp", session_id)`，再构造 `InteractionMessage` 调用 `GatewayService.handle_message_stream`
- `GatewayService.handle_message_stream` 的 model/options/trusted_metadata/approval override 只用于保留 ACP session metadata 中的 model/mode/cwd、`ACPPermissionBridge` 和 `allowed_confirm_tools`，不把 ACP initialize/auth/session lifecycle 下沉到 GatewayService
- Gateway session id 前缀/来源映射需显式识别 `source="acp"` 为 `("acp", "acp")`，避免 ACP 内部 `/new` 等 Gateway 命令误建 `cli-*` 会话

ACP event bridge 规则：
- `ACPEventBridge` 把 N-Agent `ChatEvent` 转换为 ACP session update（user_message_chunk/agent_message_chunk/tool_call/tool_call_update）；用 `getattr(update, "session_update", None)` 做 type discrimination（不能用 isinstance，因 SDK 类型在 conftest sys.modules workaround 后可能未被稳定引用）
- `MESSAGE_DONE` 默认表示完成状态不发 update；但当 Gateway slash/destructive preflight 在 `MESSAGE_DONE.content` 携带用户可见文案时，必须转成 ACP agent_message_chunk，否则 ACP 客户端看不到命令响应或确认提示
- `replay_history` 在 `session/load` 时把历史 messages 重放为 user_message_chunk + agent_message_chunk，让 ACP 客户端看到完整对话上下文
- 工具调用事件通过 `acp.start_tool_call` / `acp.update_tool_call` SDK helper 发射，ToolCallStatus 取 pending/in_progress/completed/failed

陷阱：
- ACP 包名 `acp` 与项目内任何同名模块冲突时会被 `sys.modules` 遮蔽；`tests/interfaces/cli/commands/acp/conftest.py` 用 sys.modules workaround 在测试导入前临时 pop 项目内 `acp` 模块、import SDK `acp.schema` 后再恢复，使 SDK 子模块缓存稳定存活
- `resume_session` 中 `map_cwd(cwd, self.settings) or cwd` 会让 host cwd 在映射失败时原样泄漏到 metadata，破坏 T7 不变式；必须 `mapped = map_cwd(...); if mapped is None: raise ValueError(...)`
- `except asyncio.CancelledError: stop_reason = "cancelled"; raise` 的赋值是死代码（raise 跳过 return），删除赋值只保留 raise
- ACP SDK `update_tool_call` 返回 `ToolCallProgress`（`ToolCallUpdate` 子类），`Client.request_permission(tool_call: ToolCallUpdate)` 接受该返回值；不要用 `isinstance(update, ToolCallUpdate)` 做类型判断，SDK 类型在 conftest workaround 后可能不稳定
- stdout 纯净性测试用 subprocess PIPE 捕获会因 ACP SDK asyncio write transport 与 pipe 交互挂起，必须用临时文件捕获 stdout
- `ChatCompletionInput` 扩展新字段必须用 additive defaults（`None`），避免破坏既有调用方；`approval_decider=None` 时 `ChatCompletionService` 不覆盖 ctx.approval_decider

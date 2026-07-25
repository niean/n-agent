<!-- SUMMARY: N-Agent 的关键实现模式，包括 DDD 边界、工具权限与飞书/CLI ToolPolicy 审批、Gateway/ACP/CLI 协议适配、Memory/Context、Plugin、观测与 Token 统计、Skill 自进化与 provenance 治理、用户侧委派工具暴露与防递归等实现约束 -->
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
- `ToolExecutor` 是 Domain SPI；具体实现属于各支撑子域或 Infrastructure，多个 executor 由 `CompositeToolExecutor` 按工具名路由。
- 公共 `Policy` 是 Domain Shared Kernel，只统一 `Policy` Protocol、`PolicyOutcome`、`PolicyDecision`。具体工具规则归 Tool Domain 的 `ToolPolicy`，不存在中央 `PolicyService`。
- `ToolPolicy` 统一定义校验、`DEFAULT` / `SAFE_ONLY` 暴露、执行决策和一次授权；`RiskLevel` 仍是 `ToolDefinition` 属性，不由 Runner 直接分支判断。
- `ToolService` 是强制执行边界：`evaluate_execution` 生成带审批快照的 `ToolExecutionEvaluation`，`authorize_once` 生成本次授权上下文，`execute` 在调用 executor 前按当前定义重新评估。评估 token 同时防止请求或定义在审批期间被替换。
- `AgentGraphRunner` 只按 `PolicyDecision` 的 allow / deny / require_approval 编排：需要审批时调用 `ApprovalDecider`，批准后仍通过 `ToolService.authorize_once` 和 `ToolService.execute`，不自行授予执行权限。
- 飞书入口把 `FeishuToolApprovalBridge.create_decider(...)` 注入 Gateway；“执行一次”只完成当前 ApprovalDecision，“本会话信任”由 Application `GatewayToolApprovalService` 按 session/actor/tool 保存，并动态注入 `allowed_confirm_tools_override`。因此同一 Agent Loop 后续同名工具可直接复用授权，不再发第二张卡；不同 actor 不共享。
- TUI 入口把 `CliToolApprovalBridge.create_decider(...)` 注入 Gateway（仅 TTY REPL），“执行一次”/“本会话信任”/“取消”通过 `/confirm once`/`/confirm trust`/`/cancel` 精确命令路由到进程内 Future；grant 检查使用真实 `ApprovalRequest.session_id`（内部 session id，非 conversation id）。“本会话信任”同样由 `GatewayToolApprovalService` 按 session/actor/tool 保存，但 TUI 路径不使用 `allowed_confirm_tools_override`，授权复用走 decider 内 grant checker。非 TTY/单次消息/stdin pipe 不注入 decider，fail-closed。
- TUI 工具审批与破坏性 Slash Command 共享命令词（`/confirm`、`/cancel`）但使用独立完整 confirmation id 和 REPL 状态槽位（`_last_tool_confirmation_id` vs `_last_slash_confirmation_id`）；工具 id 永不传给 `CliChatAdapter.confirm`，slash id 永不传给 bridge。TTY stream 期间通过 `asyncio.wait(FIRST_COMPLETED)` 竞速 consumer 与普通 `> ` prompt（不得增加 `[stream]` 等用户可见前缀），非审批输入只显示提示不二次发送；`/exit`/Ctrl+C/EOF 取消 stream 并确定性收尾。
- ToolPolicy 卡片回调必须按服务端 pending 所有者路由并校验 actor、verified chat id、verified card message id；客户端回传的 kind/thread/platform 只可做一致性检查。claim 与完成 Future 之间不得 await，重复回调只能有一个成功；发卡失败、超时、取消、授权写入失败均 fail closed，其中授权写入失败降级为仅本次批准。ToolPolicy 与破坏性 Slash Command 的合法选择一旦消费 pending，必须在业务执行前把卡片三个按钮全部禁用；越权、过期、类型篡改或非法 choice 不得禁用合法卡片。TUI 路径对应约束：claim 校验 actor_id + 完整 `GatewaySessionKey`，claim 与 complete 之间不得 await，cleanup 幂等（`cleanup_called` 标志防止 `discard_pending_for_actor` 与 decider finally 双重清理）。
- 卡片参数展示使用递归脱敏和长度上限，secret/token/password/authorization/api-key/credential/cookie/private-key 等键不得泄漏；飞书 client 发送卡片必须返回服务端 message_id，作为回调绑定依据。
- 工具可用性由定义面和执行面共同决定：`ToolDefinition` 决定模型可见性，`CompositeToolExecutor` 路由决定调用能否落到实现，两者必须同步注册和刷新。
- 风险等级包含 safe、confirm、dangerous。safe 默认允许；confirm 无匹配授权时要求审批；dangerous 不暴露且拒绝执行。参数授权只接受字符串键，并按授权参数是调用参数子集进行匹配。
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

`ExternalMemoryManager` 采用三槽模型管理外部记忆 provider：系统记忆（builtin，全局内置 Markdown 记忆）、文件记忆（multi-project，多项目 Markdown CRUD）、检索记忆（external-query，结构化/语义检索 provider，至多一个）。三槽共存，activate mem0 不会替换文件记忆。

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
- 各拼接块统一为 `## <Title>` 标题章节：静态块经 `prompt_builder._section(title, body)` 渲染，动态块（`SkillService.build_skills_index` 产出 `## Available Skills`、各 external memory provider 的 `system_prompt_block()`）各自产出同名 `## ` 章节；禁止裸段落或 XML 标签混入，新增块须遵循该格式。

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
- Interfaces 飞书长连接适配器只消费已注入的 client 能力：普通文本事件转换为 InteractionMessage，破坏性 Slash confirmation outbound 渲染为 interactive card 并回调 GatewayService.handle_confirmation；ToolPolicy approval 由 FeishuToolApprovalBridge 转换为同款三按钮卡片并完成 ApprovalDecider Future。
- 飞书 IM 回复与定时投递统一走 `FeishuClient.send_markdown_reply`：纯文本发 text 消息；含 `![alt](url)`/`[label](url)` 的 markdown 渲染为 post 富文本，图片先 `download_url` 下载再用 `im/v1/images` 上传换取 image_key（飞书不支持外链 url 直显图片，必须 image_key），链接渲染为友好 `a` 元素不展示原始 url；图片下载/上传失败降级为占位文本 `[图片加载失败]`，post 发送失败整体降级为 text。`FeishuImAdapter._send_response` 普通回复与 `ScheduleOutboundDelivery.deliver` 均调用此方法，不在调用方重复渲染逻辑。
- CLI 与飞书入口不能绕过 ToolService 风险控制，也不能直接写 provider、tool 或 session 数据表。

陷阱：在 CLI 或飞书长连接适配器里直接 new SQLite store、调用 Provider 或复制 AgentGraphRunner，会形成第二套 Runtime 并破坏 DDD 边界。

## 模式十二：trusted_metadata 端到端透传与 Managed Tool 授权

涉及来自非可信客户端 metadata 的工具授权决策时，必须通过独立 trusted_metadata 通道，且仅服务端可写入。

规则：
- ChatCompletionInput 同时携带 `metadata`（untrusted，可由 OpenAI HTTP 客户端写入）与 `trusted_metadata`（trusted，仅 GatewayService/Feishu 长连接适配器写入）。
- ChatCompletionService.complete 在每次调用时构造 ToolExecutionContext，把 trusted_metadata 拷贝进去，并通过 `_compute_permitted_managed_tools(mode, trusted_metadata)` 决定 `permitted_managed_tools`。当前规则：mode=realtime 且 `trusted_metadata.gateway.platform` 为合法 Gateway（feishu）才返回 `{"manage_schedule"}`，否则空集；mode!=realtime（unattended）时返回 executor 在 trusted_metadata 声明的 `permitted_managed_tools`（TaskAgentExecutor 声明 7 个 task managed 工具），未声明则空集。
- managed CONFIRM 工具有双闸门：暴露闸 `ToolPolicy.can_expose`（决定 LLM 是否看到 schema）与执行闸 `evaluate_execution`（决定调用是否 ALLOW）。unattended worker 走 `safe_only` 暴露策略，默认只暴露 SAFE 工具；TaskAgentExecutor 声明的 task managed 工具经 `can_expose(definition, SAFE_ONLY, granted_tools, permitted_managed_tools)` 放行（`definition.managed and name in permitted_managed_tools`），并在执行闸 `name in context.permitted_managed_tools` 放行（`managed_grant`，无需审批通道）。两闸都依赖 `context.permitted_managed_tools`，缺一不可。
- 上下文通过 LangGraph `configurable.options["tool_execution_context"]` 传递；执行节点（call_llm/execute_tools）必须在 config 缺失时回退到 `state.run_options`，避免 LangGraph 框架精简 config 导致 context 丢失。
- `ToolPolicy` 对未出现在 `context.permitted_managed_tools` 的 managed 工具返回 `REQUIRE_APPROVAL`；`ToolService` 未取得有效授权时返回 `permission_denied`，不调用 executor。
- 受 managed 保护的工具同时要求来源方可信：`ScheduleManagementToolExecutor` 进一步从 `context.trusted_metadata` 读取 platform/receive_id/receive_id_type/thread_id 作为任务 origin，禁止从 untrusted metadata 读取。`_origin_from_trusted` 把这四个字段一并写入 `ScheduledTask.origin` 与 `DeliveryTarget.context`，由 outbound 按 `platform` 路由投递。Task worker 的 `TaskManagementToolExecutor._origin_from_trusted` 同理从 `context.trusted_metadata["task"]`（含 task_id/run_id/claim_lock/write_origin）读取 worker 身份，缺失返回 `trusted_task_context_missing`。
- 删除等需要确认的破坏性动作不允许 Agent 直接执行；自然语言删除要返回 confirmation_required 文案，引导用户走 `/schedule remove <id>`。Gateway 破坏性命令 preflight 时把当前飞书 trusted_metadata 写入 `GatewayConfirmationRequest.trusted_metadata`，handle_confirmation 还原后再校验 task.origin 一致性。
- 不可信模式（unattended/safe_only、定时任务执行）时 `list_openai_tools` 必须过滤 source_type=AGENT 的工具，避免调度器递归调用自己。
- 用户侧 Task 审批工具（`approve_task`/`reject_task`/`revise_task`）是 source_type=AGENT + risk_level=SAFE + managed=false，realtime DEFAULT 对对话 Agent 可见，unattended SAFE_ONLY 默认隐藏。但 `SAFE_ONLY` 暴露策略对 `granted_tools` 中显式命名的 SAFE AGENT 工具仍会放行（模式六 grant 语义），因此 worker/judge 的 `granted_tools` 禁止含这三个名字；`TaskAgentExecutor` 在构造 `granted_tools` 时显式剥离 `USER_TASK_APPROVAL_TOOL_NAMES`（即便 `task.execution_policy.allowed_tools` 误配置也生效），防止 worker 自我审批自己的 `task_propose_change` 提案形成递归。此约束是 worker boundary 收紧，不改变 ToolPolicy 通用 grant 可暴露 SAFE AGENT 工具的设计。

陷阱：把 OpenAI HTTP 客户端 metadata 直接当 trusted_metadata 用，或者只在 ToolExecutionContext 里塞 metadata 不区分 trusted/untrusted，会让伪造 `gateway.platform=feishu` 的 OpenAI 客户端获得飞书会话的 schedule 操作权限。

陷阱：服务端构造的子字典（如 Task worker 的 `trusted_metadata["task"]`）必须同时放入 `IngressFacts.trusted_claims`，不能只放 `trusted_metadata`。ChatCompletionService 在有 policy_snapshot_factory 时会用 `snapshot.run_context.trusted_claims`（源自 `IngressFacts.trusted_claims`）整体替换 trusted_metadata（`_build_policy_snapshot`），只放 trusted_metadata 的子字典会被丢弃，导致下游 `_origin_from_trusted` 读不到、task 工具报 `trusted_task_context_missing`。规则：executor 写入 trusted_metadata 的每个键都应镜像进 trusted_claims。

## 模式十三：平台聚合与主动外发按 platform 路由

后台任务（定时任务等）回投到 IM 平台时，按 `DeliveryTarget.context.platform` 路由到对应平台 client；Dashboard 平台页通过 PlatformService 读取 PlatformRegistry 与 GatewaySessionRegistry，不直接读 SQLite。

规则：
- `Platform` 面向 feishu、dingtalk、wecom 等外部消息平台；GatewaySessionKey 使用 source/platform_session_id/thread_id 作为 conversation key。CLI/TUI 终端聊天使用 source=`cli`，不进入 PlatformRegistry，也不出现在 Dashboard 平台页。
- `PlatformRegistry` 提供 descriptor 与 lifecycle；Application 的 PlatformService 合成 status、session_count、last_active_at、active_sessions 等只读视图。
- `FeishuImAdapter` 是飞书入口适配器，同时实现 PlatformLifecycle；start() 先标记 connected，listen_events 正常返回后标记 disconnected，异常时写入 fatal("feishu_listen_error", message) 并继续抛出。
- `ScheduleOutboundDelivery.deliver` 只判断 `platform`：feishu 则调用注入的 FeishuClient.send_markdown_reply 投递（与 IM 入口回复同路径，支持 markdown 图片/链接渲染）；缺失或未知 platform 返回 failed，不做 receive_id 启发式回退。
- Gateway `_build_trusted_metadata` 必须写入 `gateway.platform` 与顶级 `platform`；Tool 落库 origin 时一并保存 platform/receive_id/receive_id_type/thread_id 四元组，跨 origin 操作统一返回 task not found。
- Gateway 支持平台级 home target：`/sethome` 更新 GatewaySessionRegistry 中当前 platform 的 home chat；`/schedule add` 创建 Feishu/平台 origin 任务时保存 `target=home` 逻辑引用而不是固定当前聊天会话。
- ScheduleOutboundDelivery 在投递 Feishu origin 时如注入 home resolver，则发送前动态读取当前 home target；即使历史任务仍保存旧 receive_id，只要当前 platform 配置了 home target，通知也会切到最新 home chat。
- origin 定时任务的执行 session 必须是 schedule-owned session，不能绑定 Gateway 当前会话；当 origin 任务因历史绑定会话删除进入 `session_missing` 时，ScheduleService 在 run_now/runner claim 前创建新的 schedule session 并恢复 ACTIVE。
- 定时任务按任务级授予工具：`ScheduledExecutionPolicy.allowed_tools` 声明该任务在 unattended 下可用的工具，由 `/schedule add <cron> <prompt> --tools <csv>` 或 Dashboard API `allowed_tools` 设置，持久化在 execution_policy_json。ScheduledAgentExecutor.run 把它作为 `granted_tools` 写入 trusted_metadata 与 ingress_facts.trusted_claims；ChatCompletionService 注入 `ToolExecutionContext.granted_tools`；`ToolPolicy.can_expose` 在 `SAFE_ONLY` 下对 `granted_tools` 中命中的 SAFE 工具放行（绕过 AGENT source_type 过滤，如 `host_terminal`），但仍拒绝 DANGEROUS/CONFIRM（unattended 无审批通道）。未授予的工具不受影响，default 与未授予的 unattended 任务仍不暴露 AGENT 工具。

陷阱：把"平台能力"放进每条消息的 capability 列表，意味着任何中间层漏传一次都会让正常功能变成 fail-closed；把能力归到 platform 注册（client/lifecycle 是否注入）才是单一来源。

## 模式十四：定时任务 claim lease 只保护正在执行的单次触发

定时任务由 SQLiteScheduledTaskRegistry 负责原子 claim、推进 next_run_at 和记录执行；ScheduleRunService 负责执行与投递。

规则：
- claim 时设置 claim_id、lease_owner、lease_until，并立即按 cron 推进 next_run_at，避免同一触发被并发 runner 重复领取。
- lease 只表示当前 claim 的执行保护期，不表示整个任务的冷却周期；执行完成后必须释放 lease_until，否则短周期任务会被旧 lease 阻塞。
- record_execution_completed 仍按 claim_id/lease_owner 校验当前 claim，防止过期执行覆盖新 claim；释放 lease 不应清空 claim_id/lease_owner，因为后续 delivery result 仍需要一致性校验。
- 执行必须有硬超时与异常兜底：`ScheduleRunService._run_executor` 用 `asyncio.wait_for(executor.run, execution_timeout_seconds)` 包裹（默认 600s，须 < lease_seconds 900s），超时或异常都合成 `ScheduledAgentResult(FAILED)`，确保 `run_claim` 始终走到 `record_execution_completed` 释放 lease。否则 executor.run hang（LLM/宿主桥接无响应）或抛异常会让 execution 永远 RUNNING、lease 持有到过期。
- `recover_stale_executions` 在 `run_now` 与每个调度 tick（`run_due_claims`）claim 前执行，把 stale RUNNING execution 标记 FAILED 并清理过期 lease。stale 判定：(a) 当前 claim 的 lease 已过期，或 (b) claim 已被新 claim 取代（task.claim_id != execution.claim_id，如进程重启或 re-claim 后遗留的孤儿）。只靠 (a) 不够——re-claim 后旧 execution 的 claim_id 不再匹配 task 当前 claim_id，EXISTS 条件不命中，孤儿永远 RUNNING。
- skipped_missed 只用于 runner 长时间未运行或任务确实错过宽限窗口；不能由正常执行留下的 lease 触发。

陷阱：把 lease_seconds 当作调度间隔或执行后保留 lease，会让 */5 任务被默认 900 秒 lease 卡成 15 分钟一次，并在 missed_grace_seconds 后持续 skipped_missed。陷阱：recover_stale_executions 只匹配"当前 claim 且 lease 过期"会漏掉被取代的孤儿执行，进程重启或 re-claim 后旧 RUNNING execution 永远停在 running 状态。

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
| curator | Curator 周期维护 consolidation fork（内部触发，非平台/非外部 HTTP） | curator | curator- |
| task | Task worker 进程内执行（Kanban/Manus Task，内部触发，非平台/非外部 HTTP） | task | task- |

规则：
- session_id 前缀严格等于一级名，UUID 跟在连字符后（如 `dashboard-{uuid}`、`feishu-{uuid}`）。
- CLI 入口虽然走 GatewayService，但单列为一级 `cli`（前缀 `cli-`、source `cli`），不归入 IM 平台一级。GatewayService 通过 GatewaySessionKey.platform 分流：source=`cli` → (`cli`, `cli`)，真实 IM 平台 → (`{platform.value}`, `{platform.value}`)。
- `schedule` 是触发方式不是平台，独立成一级，不再写成 `http/schedule`。
- `curator` 是 Curator 周期维护 consolidation fork 的内部触发来源，独立成一级（前缀 `curator-`、source `curator`）。session_id 用 `curator-{uuid4()}`（与 `schedule-{uuid4()}` 同，不用时间戳，遵守本模式 UUID 通用规则）。`SkillCuratorService._run_consolidation` 经 `SkillEvolutionService.run_background_review(ingress_source="curator")` 注入 `gateway.source`，由 `ChatCompletionService` 派生为会话 source；缺失时 `_build_policy_snapshot` 会回落 `api`，导致来源与前缀脱节。
- `task` 是 Task worker（Kanban/Manus Task）进程内执行的内部触发来源，独立成一级（前缀 `task-`、source `task`）。execution_session_id 用 `task-{uuid5(NAMESPACE_URL, task.id)}`：从 task.id 确定性派生完整 UUID（str 形式带连字符 8-4-4-4-12，与 `schedule-{uuid4()}`/`curator-{uuid4()}` 完全一致，禁止用 `.hex` 无连字符形式），使同一 task 跨 run/claim 稳定复用同一 execution session（无需持久化 execution_session_id；delete_session 置空后下次 claim 重新派生出同一 id 重建/复用）。禁止 `task-{task.id}`：task.id 形如 `t_{hex}`，带 `t_` 前缀且非完整 UUID，会产生 `task-t_...` 双前缀且后缀非 UUID，违反本模式。worker 执行会话由 `task_execution_session_id(task)`（`app/application/task_session.py`）统一选择：`task.execution_session_id`（显式存量/外部）-> `task.origin_session_id`（Dashboard `/task create` 捕获的 Chat 会话，使 worker 对话与生命周期回到创建任务的 Chat 框，对齐 Manus）-> `task-{uuid5(NAMESPACE_URL, task.id)}`（origin=None 的 kanban/CLI/feishu 回退）。execution_session_id 不持久化（DB NULL），delete_task 仅按持久化显式字段清理 -> 不删 origin Chat 会话。worker 在 origin Chat 会话执行时，其 assistant 推理消息（chain-of-thought）由 ChatCompletionService 经 `AgentState.message_source` 标记 source=task（命中 `_PROCESS_MESSAGE_SOURCES` 即置为 source、否则 None，session_source 已折叠 snapshot->IngressFacts->API 三级回落，故有无 policy_snapshot_factory 均生效），Dashboard `shouldRenderMessage` 据此跳过渲染进程来源（task/schedule/curator）的 assistant 消息，使 worker 内部推理不对用户可见（regression：worker CoT "The task requires querying weather..." 曾作为普通 assistant 气泡泄露到对话框）；与 judge 推理 `persist_messages=False` 不落库同理，但 worker 需跨轮历史故采用 source 标记 + 前端隐藏、推理仍落库供 goal_mode 续轮与 LLM 上下文（test_task_chat_merge 断言 worker assistant 保留进上下文），worker 工具调用结果仍按工具调试卡片独立渲染。realtime（api/dashboard）assistant 消息 source=None 正常渲染。
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
- Rich 默认关闭样式时必须显式 `color_system=None`；仅设 `no_color=True` 在 Rich 13 仍可能输出 dim/bold ANSI，经过 prompt_toolkit 或不兼容终端后会显示成 `?[2m` 等乱码。只有显式 `N_AGENT_CLI_COLOR=always` 且未设置 `NO_COLOR` 时允许 ANSI
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
- CLI 子命令与 HTTP routes 共享同一 Application service 契约（同一 `xxx_service` 方法）：service 方法返回类型变更（尤其可迭代对象 -> 包装对象，如 `tuple[Task,...]` -> `TaskListPage(items, next_cursor)`）时必须 grep 全部消费方同步迁移到新字段，并给 CLI 侧补回归测试（monkeypatch `_load_xxx_service` 返回 Fake，断言 rc==0 且渲染含期望 id）；HTTP 迁移而 CLI 漏迁会导致 `n-agent task ls` 类 `'XxxPage' object is not iterable` 错误（教训 P014）

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
- Domain `ApprovalDecider` 是 `Callable[[ApprovalRequest], ApprovalDecision | Awaitable[ApprovalDecision]]`；ACP 服务端的 `ACPPermissionBridge` 实现 `__call__`，稳定选项 ID 为 `allow_once`、`allow_session`、`reject_once`（`allow_session` 映射 ACP SDK 的 `allow_always` kind）
- `ApprovalDecision.scope` 使用 `once`、`session`、`deny`；`reject_once` 和失败关闭路径返回 `deny`
- 只有 `allow_session` 通过 `metadata_updater` best-effort 持久化会话授权；持久化异常记录 warning，但当前调用仍返回允许，后续调用会再次审批
- `ChatCompletionService` 按当前 `ToolDefinition` 归一化 `allowed_confirm_tools_override`：普通 confirm grant 合入 `allowed_confirm_tools`，managed session grant 合入 `permitted_managed_tools`，无效、禁用或非 confirm 工具被丢弃

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

## 模式二十一：TUI 一次会话完整执行链路

`n-agent chat`（无 `--message`、stdin 是 TTY）进入 REPL，一次用户输入到 assistant 回复输出完成端到端链路。本模式串联模式十八（Gateway 流式）/十九（CLI 子命令）/三（LangGraph 编排）/七（Memory 端口）/十二（trusted_metadata），作为 TUI 入口的总结篇；细节不重复，仅给端到端顺序与关键交接点。

链路（正常流式分支，破坏性命令分支见末尾）：

1. REPL 启动（`app/interfaces/cli/commands/chat.py:run` → `ReplRunner._run_tty`）
   - `build_application_services()` 组装服务，构造 `CliChatAdapter(gateway_service)`
   - `conversation_id` 默认 `"local"`，可由 `--conversation-id` 覆盖
   - prompt_toolkit `PromptSession` + `SlashNestedCompleter` + `FileHistory(~/.n-agent/cli_history)`，整个 loop body 在 `patch_stdout()` 内（详见模式十八）

2. 用户输入分发（`ReplRunner._handle_input`）
   - management 命令（`/provider`、`/sessions`、`/status` 等 10 类）→ `run_management_command` 直接调本地 service，不走 Gateway（详见模式十九）
   - local 命令（`/help`、`/exit`、`/clear`、`/history`、`/confirm`、`/cancel`）→ 本地处理；`/confirm` 与 `/cancel` 走破坏性命令确认回路
   - 其他文本 → `_send_stream(text)` 走 Gateway 流式

3. 构造 InteractionMessage（`CliChatAdapter._build_event`）
   - `InteractionMessage(id=f"cli-{uuid4()}", session_key=GatewaySessionKey("cli", conversation_id), text, metadata={"actor_id": f"cli:{conversation_id}"})`
   - `actor_id` 必填，否则破坏性命令 preflight 视为无 actor 跳过确认（详见模式十八陷阱）

4. Gateway 流式处理（`GatewayService.handle_message_stream`，详见模式十八）
   - 幂等：`registry.mark_event_processed("cli", event.id, message_id)`，重复 yield `DONE(metadata={"duplicate": True})`
   - 破坏性 preflight：`/new`/`/rename`/`/delete`/`/schedule remove` 且非 trusted actor → 创建 pending confirmation（15 分钟 TTL），yield `MESSAGE_DONE(finish_reason="confirmation_required", metadata={"confirmation": {...}})`
   - 会话解析 `_resolve_session_id`：先 `registry.get_active_session(session_key)` 命中复用；未命中 `session_service.create_session("cli-{uuid4()}", source="cli")` + `registry.create_session_link`
   - Slash 分流 `command_service.handle`：`/help`/`/sethome`/`/rename`/`/switch`/`/sessions`/`/tools`/`/models`/`/status`/`/schedule add|list|pause|resume|run` 命中即 yield `MESSAGE_DONE` + `DONE` 返回
   - 构造 `trusted_metadata`：`gateway.source="cli"`、`actor_id`、`thread_id`、`receive_id` 等；CLI 无 `gateway.platform`，故 `_compute_permitted_managed_tools` 返回空 set（详见模式十二）
   - 调 `chat_service.complete(ChatCompletionInput(stream=True, model=default_model, messages=[{role:user,content:text}], session_id, trusted_metadata, options={}))`

5. ChatCompletionService.complete（`app/application/chat_service.py`）
   - 会话已存在则 `create_session` 空操作；`memory_store.get_session` + `list_messages` 取历史
   - 外部记忆 profile 锁定（首轮派生：已锁定→沿用 / 已有消息→`[]` / 显式 override→归一化 / 未传→`[]`），`memory_store.lock_session_external_memory` 持久化（详见模式七扩展五）
   - `memory_store.append_message(role="user", content=text)` 落库
   - `session_service.ensure_title(session_id, first_user_message)` fire-and-forget 触发标题生成
   - 构造 `ToolExecutionContext`：`mode="realtime"`、`permitted_managed_tools`（CLI 空 set）、`allowed_confirm_tools`（`_mcp_tool_execution_context` 启发式从用户消息提取 URL + "探测/添加/刷新 mcp" 关键词）、`enabled_override=locked_external_memory`
   - `stream=True` → 返回 `graph_runner.stream_events(state, model, options)`

6. AgentGraphRunner.stream_events（`app/application/agent_graph.py`）
   - yield `MESSAGE_START`；构造 `StreamingContextScrubber`（流式 scrub `<memory-context>` 块）
   - `tool_event_queue` + `emit_tool_event` 注入 `stream_options["stream_event_sink"]`
   - 后台 `run_task = asyncio.create_task(self.run(state, model, stream_options))`，`register_run(session_id, task)` 支持 cancel
   - 主循环：`while not run_task.done()` 从 queue 取 tool 事件 yield（50ms 轮询）；run_task 完成后 drain 剩余
   - 结果处理：`result.error` → yield `ERROR`；`result.final_message` 按 20 行 chunk 切分，逐块 `scrubber.feed` 后 yield `CONTENT_DELTA`，`flush` 剩余 → yield `MESSAGE_DONE(finish_reason)`
   - yield `DONE` 结束；`finally` 清理 `_running_tasks`/`_cancel_events`

7. LangGraph Agent Loop（`AgentGraphRunner.run` → `graph.ainvoke`，详见模式三）
   - `prepare_context`：加载历史与摘要，构造 `working_messages=[system_prompt, ...history, ...input]`，并按需压缩；`build_system_prompt(external_memory_manager, enabled_override)` 生成 system prompt
   - `call_llm`：检查 `iteration_count >= iteration_limit`（默认 10）；`tool_service.list_openai_tools(safe_only?, context)`；外部记忆 `prefetch_all` 把记忆上下文拼到本次 user message 前（不修改 state）；`llm_provider.chat(api_messages, tools, False, model, options)`；`scrub_memory_context(final_message.content)` 清理回显；`pending_tool_calls = result.message.tool_calls or []`
   - `_after_llm` 路由：error→finalize / pending_tool_calls→execute_tools / 否则→update_memory
   - `execute_tools`：每个 tool_call 先 yield `TOOL_CALL_DELTA(pending)`；Approval gate（CONFIRM 风险 + `approval_decider` 注入时触发，CLI 默认无 decider）；`tool_service.execute(request, effective_context)`；yield `TOOL_CALL_DELTA(success|error, duration_ms)`；`memory_store.save_tool_call` 持久化；tool result append 到 `working_messages`
   - `update_memory`：assistant message（含 tool_calls）+ tool result 落库；`save_task_state(running|failed, iteration_count)`；外部记忆 `pre_compress_all` 提取 rescued_context；`summarizer.summarize` → `save_summary`
   - `_after_memory` 路由：error/final_message/iteration_limit→finalize / 否则 continue→call_llm
   - `finalize`：error 处理（生成友好错误消息落库）；`external_memory_manager.sync_all(user, assistant, session_id, agent_context, enabled_override)` 同步外部记忆；`save_task_state(completed|failed)`

8. CLI 流式消费（`app/interfaces/cli/streaming.consume_stream`）
   - `MESSAGE_START` → 清空 accumulated
   - `CONTENT_DELTA` → `console.print(content, end="")` + `flush_console`（rich patch_stdout 下必须显式 flush，详见记忆 feedback-rich-patch-stdout-flush）
   - `TOOL_CALL_DELTA` → `render_tool_call`（rich 渲染工具调用框）
   - `MESSAGE_DONE` → 若 `evt.content` 与 accumulated 不一致则 `render_markdown`；`finish_reason="confirmation_required"` → 触发 `on_confirmation(metadata)` 回调，REPL 记录 `last_confirmation_id` 并提示 `/confirm once|trust` 或 `/cancel`
   - `ERROR` → `render_status` + 返回 1
   - `DONE` → `metadata.duplicate` 时 warning，break

破坏性命令确认回路（分支，由步骤 4 preflight 触发或步骤 8 用户主动发起）：
- 用户输入 `/confirm once` / `/confirm trust` / `/cancel` → `ReplRunner._handle_confirm`/`_handle_cancel`
- `CliChatAdapter.confirm(confirmation_id, choice, conversation_id)` → `gateway_service.handle_confirmation(session_key, actor_id, confirmation_id, choice_enum)`
- `command_service.handle_confirmation`：校验 confirmation 存在/未过期/conversation 一致/actor 一致 → `CANCEL` 返回"已取消"；`TRUST_SESSION` 加入 `trusted_actors` set（同会话后续破坏性命令免确认）；`ONCE` 直接执行
- `_execute(action, ...)`：`NEW`→create_session+link；`RENAME`→`session_service.rename_session`；`DELETE`→delete_session_link+delete_session+new session；`SCHEDULE_REMOVE`→校验 origin 与 trusted_metadata 一致后 `schedule_service.delete`（跨 origin/会话统一返回"任务不存在"，不暴露存在性差异）
- 返回 `InteractionResponse`，CLI `render_markdown` 输出；`last_confirmation_id` 清空

陷阱：
- 步骤 3 `actor_id` 留空会让 `/new` 等破坏性命令绕过 confirmation（preflight 视为无 actor 不需要确认）
- 步骤 4 CLI 不得写入 `gateway.platform`；`_compute_permitted_managed_tools` 只认可 `gateway.platform=feishu`，CLI 不得获得 Feishu 专属 managed tool
- 步骤 6 `stream_events` 的 50ms 轮询 timeout 是为了在 run_task 完成前及时 yield 工具事件；timeout 过长会让工具事件延迟显示，过短会空转浪费 CPU
- 步骤 7 `call_llm` 的 `prefetch_all` 注入是临时构造 `api_messages`，不修改 `state.working_messages`；否则记忆上下文会被持久化到 SQLite 造成脏数据
- 步骤 7 `scrub_memory_context(final_message.content)` 必须在 `call_llm` 内立即执行，否则 `<memory-context>` 块会被 `update_memory` 落库污染历史
- 步骤 8 `consume_stream` 的 `on_confirmation` 回调是同步函数，不能 `await`；通过闭包写入 `self._last_confirmation_id`

## 模式二十一：多模态内容归一化与 vision 能力守卫

多模态图片输入横跨 4 个入口（OpenAI HTTP API、Dashboard、飞书 IM、ACP）和 4 个 DDD 层，必须有一套统一的 content 归一化合同和 vision 能力守卫，避免 base64 泄漏到摘要/标题、避免不支持 vision 的 provider 收到图片导致 HTTP 500。

归一化合同（`app/utils/content_utils.py`）：
- `normalize_content(content)`：将用户 content 归一化为 `str`（纯文本）或 `list[dict]`（OpenAI 风格 `[{type:text},{type:image_url}]` 数组）；非法 part 类型抛 `ValueError("unsupported_content_type")`，data URL 不合法抛 `ValueError("invalid_image_url"|"image_too_large")`。被 `ChatCompletionService`（构造归一化消息，不修改 request.messages）和 OpenAI HTTP 路由（预验证 + 400）共享调用。
- `validate_image_url(url)`：data URL 校验 MIME 白名单（image/png|jpeg|gif|webp）+ 20MB 上限 + base64 合法性；http(s) 透传（由 provider 自行 fetch）；其他 scheme 拒绝。
- `extract_text(content)`：从 str/list content 提取纯文本，list 时只取 `type:text` 的 text 字段拼接，image_url part 被跳过。被 `HeuristicSummarizer`（摘要）、`ChatCompletionService`（首条用户消息→标题生成）、`AgentGraphRunner`（外部记忆 prefetch query）共享调用，避免 base64 写入摘要或外部记忆检索 query。
- `parse_data_url(url)`：data URL 拆分为 (media_type, data)，被 ACP `event_bridge._replay_user_message_blocks` 和 Anthropic provider 共享调用。

入口归一化路径：
- OpenAI HTTP API（`openai_compatible.py`）：路由层对 user 消息 `normalize_content` 预验证，非法返回 400 + `error.code`；system/tool 消息带 image part 返回 400（不支持）；`ChatCompletionService` 二次归一化（幂等）。
- Dashboard（`chat.js`）：前端构造 `[{type:text},{type:image_url}]` 数组或纯文本；`buildChatRequestBody` 根据 pendingImages 决定 content 类型；fetch 检查 `res.ok`，非 2xx 读 JSON error 展示。
- 飞书 IM（`feishu_im_adapter.py`）：`_handle_image_message` 下载图片（`FeishuClient.download_image`，15MB 上限 + Content-Type 校验），base64 编码为 data URL，构造 `InteractionMessage(images=[data_url])`；GatewayService `_content_from_interaction` 将 text+images 合成 content array。
- ACP（`agent.py`）：`_content_from_prompt(prompt)` 从 ACP prompt block list 提取 (text, images)，ImageContentBlock 的 data+mime_type 转 data URL，构造 `InteractionMessage(images=...)`。

vision 能力守卫（`AgentGraphRunner.call_llm`）：
- preflight 检查：若 `working_messages` 最后一条是 user 消息且 `has_image_part(content)` 且 `vision_capability()` 返回 False，直接设置 `state.final_message` 为友好提示（"当前模型不支持图片输入，请切换到支持 vision 的模型后再试"），`finish_reason="stop"`，不调用 provider。避免不支持 vision 的 provider 收到图片导致 HTTP 500。
- `vision_capability` 由 `main.py` 注入 `lambda: bool(holder.current_config and holder.current_config.supports_vision)`，运行时反射 active provider 配置。

Provider 转换：
- OpenAI-compatible：content array 原样透传给 provider API。
- Anthropic（`anthropic_provider.py`）：`_content_to_blocks` 将 OpenAI 风格 image_url 转换为 Anthropic 风格 `{"type":"image","source":{"type":"base64","media_type":...,"data":...}}`；http(s) image_url 抛 ValueError（保守路径，Anthropic SDK 支持 url source 但本期不依赖）。

vision_analyze 工具（`VisionAnalyzeToolExecutor`）：
- safe 工具（toolset=vision），LLM 主动调用，传入 image_url + question。
- executor 校验 URL（`validate_image_url`），检查 `vision_capability()`，调用 `provider.chat([], [], False, current_model(), {})` 无工具无递归地分析图片。
- 不支持 vision 或 URL 非法时返回 `ToolResult(ERROR)` 友好提示，不抛异常打断 AgentGraph。

ACP 历史回放（`event_bridge._replay_user_message_blocks`）：
- list content 逐 part 发送 session_update：text part → `update_user_message_text`；image_url data URL → `update_user_message(image_block(data, mime_type))`；http(s) image_url → `[图片]` 文本占位（ACP 协议无 http image URL 传输，避免崩溃）。
- 严禁 `str(list)` 渲染（会显示 base64 JSON 原文）。

陷阱：
- `HeuristicSummarizer` 必须用 `extract_text` 而非 `str(content)`，否则 list content 会被 `str()` 渲染为 `"[{'type': 'text', ...}]"` 写入摘要，base64 可能泄漏。
- `AgentGraphRunner` 外部记忆 prefetch query 必须用 `extract_text`，否则 base64 会作为检索 query 污染外部记忆。
- OpenAI HTTP 路由必须 try/except `normalize_content` 的 ValueError 并返回 400，否则 ValueError 会传播为 500。
- GatewayService 必须在 destructive preflight 前拒绝 slash+images 组合，否则会为带图片的 slash 命令创建 confirmation。
- ACP `prompt()` 的 `if not text and not images: return end_turn` 必须同时检查 images，否则 image-only prompt 被当空消息拒绝。
- Anthropic provider 的 http(s) image_url 必须抛 ValueError 而非静默跳过，否则图片被静默丢弃用户无感知。

## 模式二十二：上下文短期记忆增量压缩与摘要持久化

Agent Runtime 在多轮对话中 token 消耗持续增长，需在超过阈值时把历史压缩为结构化摘要，控制后续 LLM 调用成本。压缩能力通过 DDD 四层装配实现，避免把压缩逻辑散落到 LangGraph 节点或 Infrastructure 细节中。对齐 HermesAgent 增量压缩方法：摘要消息持久化到 messages 表（is_summary=1），通过 content 前缀定位上次摘要切点，middle 只取新增消息。

装配链路：
1. Domain 端口（`app/domain/context.py`）：`ContextEngine` Protocol 定义 `should_compress` 与 `compress`；`ContextCompressionResult` frozen dataclass 携带 messages/summary/compressed/skipped_reason/original_tokens/compressed_tokens；`CONTEXT_SUMMARY_PREFIX`（`"[CONTEXT SUMMARY]: "`）常量定义运行时摘要消息识别前缀
2. Infrastructure 实现（`app/infrastructure/context/context_compressor.py`）：`ContextCompressor` 实现 `ContextEngine`，注入 LLMProvider/model callable/context_length/threshold/protect_first_n/protect_last_n/summary_target_ratio/cooldown_seconds/可选 fallback_summarizer；`_find_latest_context_summary` 从后往前扫描 messages 定位最后一个 content 以 `CONTEXT_SUMMARY_PREFIX` 开头的 user 消息；`_generate_summary` 分首次路径（FIRST 模板，空 existing_summary）和迭代路径（ITERATIVE 模板，previous_summary=body）
3. Application 服务（`app/application/context_service.py`）：`prepare_context` 内的压缩阶段调用 `context_engine.should_compress` 判定，超阈值时调 `compress` 执行压缩；成功后按顺序执行 `append_summary_message` 写入新摘要消息、`mark_messages_summarized` 标记本次被摘要吸收的原消息、`save_summary` 更新滚动摘要，最后更新 state
4. main.py 装配（`app/main.py`）：当 `settings.context_compression_enabled=True` 时构造 `ContextCompressor` 并注入 `AgentGraphRunner.context_engine`；disabled 时 `context_engine=None`，`prepare_context` 跳过压缩

增量三段式压缩规则：
- head 段保留前 `protect_first_n` 条消息（含 system prompt + 早期关键上下文）
- 中段送入 LLM 生成结构化摘要（目标/进展/决策/文件/待办 5 节），按 `summary_target_ratio × context_length` 预算约束
- tail 段保留最后 `protect_last_n` 条消息，按 token 预算分配
- 增量压缩：`_find_latest_context_summary` 定位上次摘要消息（content 以 `CONTEXT_SUMMARY_PREFIX` 开头），middle 4 种分支处理：无摘要（首次路径）/ summary_idx<head_end（首次路径，移除旧摘要）/ head_end<=summary_idx<tail_start-1（正常增量，middle 从 summary_idx+1 开始）/ summary_idx>=tail_start-1（跳过，skipped_reason="summary_in_tail"）
- previous_summary 用 body（剥离前缀的纯摘要），不用 state.summary（含 rescued_context）
- 工具组完整性对齐：避免在 assistant tool_calls 与对应 tool 消息之间截断，`sanitize` 剥离未配对的 tool_calls/tool 消息，保证压缩后 messages 对 provider API 合法
- token 估算：str content 按 `len//4` 估算，list content 逐 part 估算（text 按 len//4，image_url 固定 1500，其他 json.dumps//4），tool_calls/name/tool_call_id 一并计入；单条消息异常不中断估算

cooldown 防抖：
- `ContextCompressor` 内存维护 `_last_compressed_at`（monotonic 时间戳），`should_compress` 检测 cooldown 未到期时返回 False
- force=True 可绕过 cooldown（供测试或显式触发使用）
- cooldown 防止同一会话连续多轮触发压缩造成抖动和重复 LLM 摘要调用

LLM 摘要失败回退：
- LLM 摘要调用异常时回退到注入的 `fallback_summarizer`（通常为 `HeuristicSummarizer`），保证压缩路径不因 LLM 故障中断
- 回退仍失败时 `skipped_reason` 标记原因，messages 原样返回不压缩

外部记忆 pre_compress_all 迁移：
- `pre_compress_all` 从 `update_memory` 迁移到 `prepare_context` 的压缩阶段，仅在真正压缩时调用提取 rescued_context
- `update_memory` 不再生成 summary（摘要职责迁移至上下文准备阶段），避免双路径重复生成
- `prefetch_all` 仍留在 `call_llm` 节点做临时注入，不修改 state.working_messages

摘要持久化边界（双标记 + 双写降级）：
- `messages` 表新增 `is_summary INTEGER NOT NULL DEFAULT 0` 列 + partial index `idx_messages_summary_session`；`ConversationMessage.is_summary` 为 bool
- 压缩阶段在 `result.compressed=True` 时从 result.messages 识别恰好 1 条摘要消息（role=user + content 以 `CONTEXT_SUMMARY_PREFIX` 开头），数量 != 1 时保持 state 不变
- `append_summary_message`（MemoryStore 端口）：追加 role=user、`is_summary=1` 的新摘要消息；历史摘要保留在 messages 表，加载 Provider Context 时只选最新摘要
- `mark_messages_summarized`（MemoryStore 端口）：将本次摘要吸收的 middle 消息标记为 `is_summarized=1`，后续上下文加载过滤这些原消息
- `save_summary`：写入 summaries 表，`source_message_id` 关联新摘要消息 id；`summaries.summary` 保存 state.summary（含 rescued_context），`messages` 摘要 content 保存 result.summary（纯 LLM 摘要，不含 rescued_context）
- 双写降级：`append_summary_message` 失败时 state 不变；`mark_messages_summarized` 失败时保留新摘要并继续；`save_summary` 失败时 messages 表已有新摘要，summaries 表滞后一轮（Dashboard 降级），不回滚
- `_message_to_provider` 不传递 is_summary 到 provider 格式，provider 调用时消息只含 role/content/tool_calls 等标准字段
- Dashboard API `_message_to_dict` 返回 is_summary 字段，chat.js 对 is_summary=1 消息特殊渲染（摘要 badge + 灰色卡片，剥离前缀后展示正文）

陷阱：
- 在压缩阶段直接修改 `state.working_messages` 会污染后续 `update_memory` 落库的历史，应通过 LangGraph state 返回新值让框架替换
- 增量压缩未做 previous_summary 提取（用 state.summary 而非 body）会把 rescued_context 混入 LLM 摘要输入，导致摘要质量退化
- 三段式压缩未做工具组完整性对齐会在 assistant tool_calls 与 tool 消息之间截断，导致 provider API 报错（tool_calls 无对应 tool result）
- LLM 摘要无 fallback 回退会让 LLM 故障直接中断 AgentGraph 主路径，应注入 `HeuristicSummarizer` 作为 fallback
- cooldown 用 wall clock 会让测试等待真实时间，应注入 `_clock` callable 用 fake clock 验证
- 只追加摘要消息却不标记被吸收的原消息，会使下轮上下文同时加载摘要和 middle 原文，造成重复与 token 回涨
- `save_summary` 失败时回滚 messages 表会让下次上下文准备加载到新摘要但 summaries 表无对应记录，Dashboard 显示滞后；正确降级是接受 Dashboard 滞后一轮不回滚

## 模式二十三：观测与 Token 统计 DDD 装配

Agent Runtime 在每次 LLM 调用后需归一化 usage（五桶 token + 成本）并持久化，供 Dashboard 观测页和 CLI 子命令查询。观测能力通过 DDD 四层装配实现，Domain 定义值对象与端口，Application 编排归一化/估算/持久化，Infrastructure 提供 SQLite 持久化与硬编码价格表，Interfaces 暴露 Dashboard 观测页与 CLI usage 子命令。

装配链路：
1. Domain 值对象与端口（`app/domain/usage.py`）：`CanonicalUsage`（五桶 frozen dataclass，`prompt_tokens`/`total_tokens` 派生属性）、`UsageCost`（Decimal amount_usd + status + pricing_version）、`PricingEntry`、`SessionUsageStats`、`ContextBreakdown`（四类 token + total 派生）、`UsageRecord`/`CompressionStat`；端口 `UsageRecorder`/`PricingProvider`/`ContextBreakdownCalculator`
2. Application 用例（`app/application/usage_service.py`）：`UsageService` 编排 `normalize_usage`（OpenAI prompt_tokens/completion_tokens + prompt_tokens_details.cached_tokens + completion_tokens_details.reasoning_tokens 五桶归一化；Anthropic input_tokens/output_tokens/cache_creation_input_tokens/cache_read_input_tokens 归一化）、`estimate_cost`（Decimal 精度，`get_pricing` 命中时按桶分别计算 amount_usd 并 status='estimated'，未命中 status='unknown' 且 amount_usd=0）、`record_call`/`get_session_stats`/`list_records`/`record_compression`/`list_compressions`/`get_context_breakdown`
3. Infrastructure 实现（`app/infrastructure/usage/`）：`SqliteUsageRecorder` 实现 `UsageRecorder`，sessions 表迁移幂等（`_COLUMN_SPECS` table + PRAGMA table_info 检查列存在再 ALTER），`record_call` 在单连接内 INSERT usage_records + UPDATE sessions 累加（`input_tokens = input_tokens + ?` 等增量累加，`api_call_count = api_call_count + 1`）；`InMemoryPricingProvider` 硬编码 9 款主流模型定价，`get_pricing` 按 model 前缀最长匹配；`ContextBreakdownCalculatorImpl` 复用 ContextCompressor 的 ~4 chars/token 估算逻辑按 system_prompt/tool_definitions/memory/conversation 四类分桶
4. agent_graph 集成（`app/application/agent_graph.py`）：`call_llm` 在 LLM 调用前捕获 `call_start = time.monotonic()`，调用后从 `LLMResult.usage` 提取 raw_usage 调 `usage_service.record_call(session_id, model, provider, raw_usage, latency_ms, provider_kind)`，record 成功后输出 `logger.info("API call model=... provider=... in=... out=... total=... latency=Nms")`；上下文压缩成功时调 `usage_service.record_compression(session_id, before_tokens, after_tokens)`；usage_service 为 None 或 record 异常均不阻塞主流程
5. main.py 装配（`app/main.py`）：组装 `UsageService(SqliteUsageRecorder, InMemoryPricingProvider, ContextBreakdownCalculatorImpl)`，`_run_sync` 处理 async init；`ApplicationServices.usage_service` 暴露；`AgentGraphRunner(usage_service=...)` 和 `create_dashboard_router(usage_service=...)` 注入
6. Interfaces 暴露（`app/interfaces/http/usage_routes.py` + `app/interfaces/cli/commands/usage.py` + `app/interfaces/http/static/observations.js`）：HTTP `/chat/usage/sessions/{id}` 系列端点（records 端点 `limit: int = Query(50, ge=1, le=500)` 防越界，breakdown 端点 list_messages/tool_service 失败 `logger.warning(..., exc_info=True)` 降级空列表不阻塞响应）；CLI `n-agent usage [session_id]` 沿用 `_load_usage_service()` inline 模式；Dashboard 观测页三段布局（6 卡片总览 + 4 类条形图 + 调用历史表格 + 压缩收益折叠区），全 textContent 渲染

陷阱：
- `usage_records` 与 `compression_stats` 表未加 `ON DELETE CASCADE` 会让删除 session 后历史残留，违反 Chat Session 级联清理预期；正确做法是 FK 引用 sessions.id ON DELETE CASCADE
- `record_call` 非单连接事务实现会在 INSERT usage_records 后 UPDATE sessions 失败时遗留孤立项；正确做法是单连接 try/finally close
- `record_compression` 未对 `saved = before_tokens - after_tokens` clamp 负值会在压缩后 token 反而增加时记录负数 saved，Dashboard 显示混乱；正确做法是 `max(before-after, 0)`
- `limit` 参数未做上界校验会让恶意请求 `?limit=999999999` 拖垮 SQLite；正确做法是 `Query(50, ge=1, le=500)`
- bare `except Exception:` 不记录日志会让 memory_store.list_messages / tool_service.list_definitions 故障被静默吞掉，观测页 breakdown 显示 0 但实际是异常；正确做法是 `logger.warning(..., exc_info=True)` 后降级空列表
- Domain 值对象直接 import Infrastructure 会让 DDD 边界破坏（如 ContextBreakdownCalculator 直接依赖 ContextCompressor）；正确做法是 Domain 定义 Protocol，Infrastructure 实现，Application 通过 Protocol 注入
- 价格表硬编码到 Domain 会让 Domain 依赖具体定价数据违反纯领域原则；正确做法是 Domain 定义 `PricingProvider` 端口 + `PricingEntry` 值对象，Infrastructure 实现 InMemoryPricingProvider
- async 方法内部直接调用同步 sqlite3 会阻塞事件循环（与 SQLiteMemoryStore 一致，技术债 D018）；正确做法是 `asyncio.to_thread` 包装，但本期不修复保持与现有 pattern 一致

## 模式二十四：Host Terminal 双重 Policy 与已验证字节执行

宿主执行不复用 Sandbox backend，也不允许模型提交任意 shell。`host_terminal` 只接受结构化 command 或 Skill script 请求：N-Agent 侧与宿主 Bridge 独立加载同一份 Policy，分别校验精确 argv；Skill script 还必须匹配 `skill_name + relative_path + SHA-256`。

Bridge 必须只监听 loopback 并校验独立 token。执行前从已校验的源文件字节创建私有快照，命令以 argv 直接启动、不经 shell；macOS Mach-O 快照需完成本地签名验证。工具结果只返回结构化 stdout/stderr/exit code，临时凭证与 OSS 密钥不进入容器，上传结果只暴露限时签名 URL。

photo-and-upload Skill 的 OSS object key 命名格式：脚本在宿主侧生成 OSS object key 时使用 `photo_<host>_<yymmddHHMMSS>.jpg` 格式（如 `photo_nieans-macbook-airm5_260715155941.jpg`），完整 key 为 `{OSS_BUCKET_PATH}/{opaque_name}`。host 段取 `socket.gethostname()` 并经 `_normalize_hostname` 规范化：取首个 `.` 前的部分、转小写、非字母数字分段以连字符连接、限长 32、空值回退 `host`。yymmddHHMMSS 段为 Asia/Shanghai 时区的拍摄时间（2位年+月+日+时+分+秒）。脚本通过 `hostname_factory` 参数（默认 `socket.gethostname`）注入主机名以便测试。stdout 契约 `CAPTURED:<opaque-name>:<size>` 中的 opaque-name 即上述文件名，不暴露宿主绝对路径。该格式不含随机 token，key 由 host 和时间戳决定，同一主机同一秒内多次拍摄存在碰撞风险。

Skill 脚本失败时的脱敏阶段码透传：Skill 脚本在失败时向 stderr 写入 `ERROR:<code>` 行，`<code>` 是稳定的脱敏阶段标识符（如 `sts_failed`、`capture_failed`、`config_unsafe`）。Bridge 将脚本 stderr 结构化返回，executor（`app/application/host_terminal_tool_executor.py`）在 `_normalize_response` 中当 `error_code=host_execution_failed` 且 `target_type=skill_script` 时，用正则 `^ERROR:([a-z][a-z0-9_]{0,63})$` 从 stderr 解析阶段码，透传到 `ToolResult.content` 为 `{"error":"host_execution_failed","stage":"<code>"}` 并补上 `duration_ms`。原始 stderr 不进入模型上下文，只有解析出的阶段码被暴露；若 stderr 中无匹配行则回退为不透明的 `host_execution_failed`。此约定使脚本可用脱敏阶段码向模型传递失败原因，而不泄漏凭证、路径或外部服务原始响应。

## 模式二十一：Policy Mesh 治理封口

### Policy Mesh 模式

10 个领域 Policy 各自独立决策一个治理维度，不跨域导入。Application Service 在调用外部资源前封口执行：Policy 评估 -> deny 则外部资源不被调用。封口点覆盖 LLM（ModelService.call_llm）、Tool（ToolService.execute，通过 CompositeToolExecutor 统一路由 Builtin/MCP/Plugin/ExternalMemory/Sandbox/Terminal）、Memory（RuntimeMemoryService）、Sandbox（SandboxToolExecutor）、Gateway（GatewayService）、Schedule（ScheduleRunService）。

关键约束（AST 测试 `tests/architecture/test_policy_boundaries.py` 强制）：
- Domain Policy 文件不导入 Application/Infrastructure/框架
- 一个 Policy 不导入另一个 Policy
- RunPolicySnapshot 不持有 RunBudgetAccount/Manager/Store 等 mutable state

### 非绕过 Facade 模式

RuntimeMemoryService 是 MemoryStore 的非绕过 facade：不暴露 raw store 属性，所有方法经 MemoryPolicy 评估，deny -> store 不调用。调用方只能通过 facade 方法访问存储，无法绕过策略。

### 两段式预算（reserve-settle-release）模式

BudgetService 对每次外部调用执行三段式预算：
1. reserve：调用前预留配额，DENY -> 调用不发生（fail-closed）
2. settle：调用成功后用实际用量替换估算（conservative：unknown usage 保留估算不释放）
3. release：调用前异常/取消时回滚预留（decrement counter）

RunBudgetAccount 用 asyncio.Lock 序列化所有操作，保证 concurrent reserve 不 oversell。

ToolService.execute 的 Budget 封口顺序：ToolPolicy 准入 -> BudgetService.reserve(TOOL_CALL) -> InformationFlowService.release(TOOL_MCP_PLUGIN) 输入脱敏 -> executor -> BudgetService.settle -> InformationFlowService.redact_structured 输出脱敏。deny -> executor 不调用；executor 异常 -> budget release（无泄漏）。

### 流脱敏守卫（Stream Guard）模式

InformationFlowStreamGuard 做增量流脱敏：lookbehind buffer 保留 `max_secret_length - 1` 字符，防止 secret 跨 chunk 泄漏。`feed(chunk)` 先追加到 buffer、redact、再释放除 tail 外的内容；`flush()` 释放剩余。transform 异常时 raise InformationFlowError，不 yield 原始内容。

### 审计通道模式

PolicyAuditService 委托 PolicyAuditSink Protocol，sink 失败只 log warning 不传播。PolicyAuditEvent 无 raw prompt/secret/tool arguments 字段，sink 无法泄漏。生产实现 LoggingPolicyAuditSink 输出 JSON 日志。已接入 RuntimeMemoryService、BudgetService、InformationFlowService、ToolService。InformationFlowService.release 是 sync 方法，审计用 fire-and-forget（asyncio.create_task）。

## 模式二十五：Skill 自进化 loop 与 provenance 治理

Skill 自进化（Background Review）让 Agent 在主 turn 结束后异步审视并修改自身 Skill 库。整个链路横跨 AgentGraphRunner、SkillEvolutionService、ChatCompletionService、ToolService、SkillManageToolExecutor、SkillService、SkillPolicy、SkillPendingStore、SkillBackupStore、SQLiteSkillRegistry 等组件，通过 origin provenance、staged approval、read-before-write、backup fail-closed、source 保留等多重约束保证后台自演化不破坏 Skill 库完整性。

Skill 自进化主 loop（Background Review）：
- AgentGraphRunner finalize 成功后触发 `_post_finalize_nudge`：检查 nudge counter，当 `turn_count % nudge_interval == 0` 时调用 `SkillEvolutionService.maybe_trigger`
- `maybe_trigger` spawn 独立 asyncio task 执行 `run_background_review`，fire-and-forget，全异常捕获不影响主 turn
- `run_background_review` fork 一个受限的 `ChatCompletionService`：工具白名单经 `ToolService.build_filtered_definitions` 按 toolset+toolname 双重过滤，只注入 skills+memory 相关工具
- fork 内注入 `trusted_metadata.skill_write_origin=background_review`，标识本次 Skill 写入来自后台自演化
- Agent 在 fork 内自主调用 `skill_manage` 工具 -> `SkillManageToolExecutor` 从 `trusted_metadata` 读取 origin -> `SkillService.manage_skill` -> `SkillPolicy` 评估

provenance origin 防伪造链：
- origin 只能由宿主注入到 `ToolExecutionContext.trusted_metadata["skill_write_origin"]`，SkillManageToolExecutor 从 `trusted_metadata` 读取，不读 `request.arguments.origin`
- 前台调用默认 origin=foreground，后台 fork 注入 origin=background_review
- OpenAI HTTP 直连客户端的 metadata 是 untrusted_metadata，无法伪造 origin 绕过 SkillPolicy 后台限制
- 此约束与模式十二（trusted_metadata 端到端透传）同源：trusted_metadata 只能由服务端写入

write_approval staged gate：
- `SkillService.manage_skill` 当 SkillPolicy 返回 `REQUIRE_APPROVAL` 且 `approved_replay=False` 时，调 `SkillPendingStore.stage` 持久化 pending write，重启存活
- 用户经 Dashboard/CLI approve 后，`approve_pending` 调 `approve_take` 原子取出 pending write
- 取出后构造 `approved_replay=True` 的 `SkillManageRequest`，再次调 `manage_skill` 重放
- `approved_replay=True` 绕过 require_approval staging，但 deny 规则仍评估，deny 不可被 approval 覆盖

read-before-write guard：
- background_review 修改既有 Skill 前，fork 内必须先调 `skill_view` 读取目标 Skill
- `SkillService.mark_bg_read` 把已读目标记录到 `_bg_read_targets`
- `SkillPolicyRequest.exact_target_loaded` 据此判定，未读则 SkillPolicy deny
- 此 guard 防止后台 Agent 在不了解目标 Skill 当前内容的情况下盲改

backup fail-closed：
- `SkillService.manage_skill` 执行写入前，若 `backup_enabled=True` 则调 `SkillBackupStore.snapshot` 创建快照
- `SkillBackupError` 时拒绝写入（不落盘），避免进入无法 rollback 的半保护状态
- backup 失败不降级为无备份写入，保持 fail-closed 语义

空写拒绝（防 LLM 误用清空 Skill）：
- `SkillService.manage_skill` Step 1.5 在 backup/write 之前校验 action 载荷：EDIT/CREATE 要求 `content` 非空白（否则 `content_required`），PATCH 要求 `old_string` 非空白（否则 `old_string_required`）
- 背景：LLM 易混淆 EDIT（整文件替换，用 `content`）与 PATCH（子串替换，用 `old_string`/`new_string`），曾用 `action=edit` 却把内容塞进 `old_string` 而漏给 `content`，执行器 `content=str(args.get("content") or "")` 得空串，`write_skill_file(skill, "")` 把 SKILL.md 清成 0 字节
- `SkillFileLoader._write_skill_file_sync` 二次防御：`content` 空白时 raise `empty_content`，任何绕过 manage_skill 校验的路径都不会清空文件
- backup 虽能恢复（快照在写前生成），但若空写发生在 create 之后、下一次有备份的写之前，中间无快照则内容丢失；故必须在源头拒绝空写

Skill source 保留：
- `SQLiteSkillRegistry.replace_all_skills` 对已存在的 Skill name 保留既有 source：先 SELECT 旧 source 覆盖传入值
- 防止 agent-created skill（source=AGENT）被 rescan 降级为 USER，丢失 provenance
- seed skill 由 `file_loader` scan 对照 seeds 目录识别，rescan 不改变其 source

陷阱：
- 后台 fork 的 ChatCompletionService 若未限制工具白名单，Agent 可能调用无关工具（如 schedule、mcp）产生副作用；必须按 toolset+toolname 双重过滤
- `approved_replay=True` 若同时绕过 deny 规则，会让 approval 变成万能钥匙，破坏 SkillPolicy 的 deny 约束；deny 必须始终评估
- `SkillPendingStore.stage` 若不持久化（仅内存），进程重启后 pending write 丢失，用户无法 approve；必须落 SQLite
- backup 失败时若继续写入，会留下无法回滚的变更，破坏 Skill 库可恢复性；必须 fail-closed
- `replace_all_skills` 若不保留既有 source，rescan 会把 agent-created skill 降级为 USER，丢失 provenance 信息
- origin 若从 `request.arguments` 读取，OpenAI HTTP 客户端可伪造 `origin=foreground` 绕过后台限制；必须从 `trusted_metadata` 读取

## 模式二十六：Dashboard Chat slash 命令本地解析路径

任务生命周期管控提供双入口：看板 tasks.js（维持现状，主做观测）与 Dashboard Chat `/task` slash 命令（Manus 任务形态，对话内创建与跟踪）。slash 命令在 chat.js 前端本地解析，不调 LLM、不消耗 token，复用既有 task_routes API（不改 Application/Domain/状态机）。

路由隔离（`chat.js send()`）：
- 在空输入检查后、原 `await ensureSession()` 之前检测 `text.startsWith('/task')`：命中走命令分支（try/catch/finally：ensureSession -> 清空 input -> runTaskCommand；ensureSession 失败呈现 `[任务指令]` system 错误并保留 input 供重试；finally 恢复 focus；始终 return 不进入 LLM 路径），未命中走原 `/chat/completions` LLM 对话。
- 命令分支不产生 user/assistant 消息、不调用 `/chat/completions`；非命令文本（含正文中间出现 `/task`）仍走 LLM。

解析（`parseTaskCommand`，纯函数，无 DOM/api，供 harness 测试）：
- 引号感知分词（spec 要求 title "支持引号"）：`"..."`/`'...'` 分组内部空格、剥离外层引号；未闭合引号返回 `{error}`。
- 第一个 token 必须是 `create|list|approve|reject|cancel|retry` 之一；按子命令校验位置/命名参数（create 允许 `--body/--priority/--goal`，approve/reject 允许 `--note`，list/cancel/retry 无命名参数）。
- 坏值返回 `{error: '<原因>。<完整用法>'}` 不传给 API：`--body/--note` 缺值、`--priority` 非整数、未知 `--xxx`、create 缺标题、approve/reject/cancel/retry 缺 id 或多 id、list 多余位置参数、命名参数不适用于该子命令。

执行（`runTaskCommand`）：
- 调既有 `api.task.*`（对应 task_routes 的 `/chat/tasks*` 端点）；create payload 始终含 `title` + `origin_session_id: currentSessionId`，仅用户提供时加 `body/priority/goal_mode`。
- list 取 `api.task.list()` 首页 `items` 按 `origin_session_id === currentSessionId` 前端过滤（不改后端 list_tasks；首页 100 条限制见 debt D032）。
- 结果统一以 `[任务指令]` 前缀 system 消息呈现（spec UI Design）；system 消息按 PRD L464 用"工具调用调试信息"样式渲染为 `<details open>` 可折叠气泡（summary 标题"系统消息" + pre 正文，默认展开保证命令结果可见、给人聊天感，区别于 user/assistant 消息），textContent 安全渲染，无 innerHTML。

错误码映射：
- `fetchJson` 失败时 `throw new Error(code)`（code 来自 `data.error.code`，如 task_not_found/task_state_invalid/task_invalid/task_conflict）；`describeTaskError` 把已知码映射为可读说明并括号保留原码，未知码走通用文案含 `String(error.message)`。

陷阱：
- 命令结果必须用 system 消息（带 `[任务指令]` 前缀）区别于 user/assistant，不得回落 LLM 或追加 user/assistant 消息。
- slash 命令必须本地解析（Dashboard Chat 走 `/chat/completions`，不走 Gateway slash 解析），否则产生 LLM token 消耗。
- create 必须绑定当前会话 `origin_session_id`，否则 `/task list` 无法按会话过滤。
- 命令分支 ensureSession 失败时不得清空 input（保留供重试），但 finally 必须恢复 focus。
- 看板 tasks.js 与 `/task` 命令双入口并存，看板不删任何管控按钮（用户对比两种交互）。

## 模式二十七：Dashboard Chat 激活态消息自动刷新（客户端轮询）

Dashboard Chat 激活态会话需自动展示后台追加的新消息（多标签同会话、任务生命周期 system 消息等），无需用户手动刷新。采用纯前端客户端轮询，复用既有 `GET /chat/sessions/{id}`，无后端改动、无 WebSocket。

选型理由（轮询 vs WebSocket）：既有 tasks WS `/chat/tasks/events` 实为服务端 `_ws_poll_events` 轮询 DB 后推送，非消息写入事件总线；chat 无单调游标端点。新增 WS 仍需跨 ChatCompletionService/TaskAgentExecutor/Schedule 等分散写入来源建立事件传播，不能降低核心复杂度。本地 Dashboard 单激活会话每 4 秒一次详情请求可接受。

规则：
- `chat.js` 维护唯一 `setInterval`（`AUTO_REFRESH_INTERVAL_MS = 4000`）+ 世代 `autoRefreshGeneration` + 请求序号 `autoRefreshSeq` + 单飞 token `autoRefreshInFlight` + 复合版本 `renderedMessageVersion = {count, lastId}`（空会话用 `{count:0, lastId:null}`）。
- `startAutoRefresh(sessionId, {immediate})` 幂等：先 `stopAutoRefresh`（清定时器+递增世代+清单飞），仅当 `sessionId === currentSessionId` 且 `!document.hidden` 时注册定时器；`immediate=true` 立即触发一次（供 visible 追赶）。
- `autoRefreshTick` 守卫顺序：捕获 `sessionId/generation` -> 检查 `currentSessionId/isSending/document.hidden` -> 检查单飞（同世代 in-flight 跳过）-> 发请求 -> 响应返回后三重归属校验（`sessionId===currentSessionId && gen===autoRefreshGeneration && seq===autoRefreshSeq`）-> `isSending` 复检 -> 版本计算 -> 变更检测 -> `applySessionDetail(preserveScroll=true, skipToolCalls=true, skipSessionList=true, applyExternalMemoryState=false)` -> 推进版本。`finally` 仅清除自身 token（`generation===gen && seq===seq`），旧世代请求不得释放新世代单飞锁。
- 变更检测：`messageVersionOf(detail)` 返回 `{count: messages.length, lastId: 末条id}`；空数组 `{count:0,lastId:null}`；非空但末条无 id 返回 `null`（无效快照，console.warn 不渲染不推进）。`versionsEqual` 比较两者。版本相同完全跳过消息区/调试区/会话列表/滚动写入（防闪烁）。仅检测追加（append-only），不检测编辑/删除。
- 滚动保持：`applySessionDetail(preserveScroll=true)` 渲染前记录 `isAtScrollBottom()`（`scrollHeight - (scrollTop+clientHeight) <= 48`）与 `scrollTop`；渲染后 `restoreScroll`：原在底部则 `scrollToBottom`，否则 `scrollTop = min(原值, scrollHeight-clientHeight)` 下界 0。`preserveScroll=false`（selectSession/refreshCurrentSession）不调 restoreScroll，由调用方自行 `scrollToBottom`（selectSession）或保持原位（refreshCurrentSession）。
- `applySessionDetail(detail, {preserveScroll, skipToolCalls, skipSessionList, applyExternalMemoryState})` 是共享渲染入口：`applyExternalMemoryState` 时调 `applySessionExternalMemoryState`+`renderExternalMemoryUI`（轮询跳过，避免覆盖用户操作）；`renderSessionMessages`；`updateInfo(detail, {skipToolCalls})`（`skipToolCalls=true` 跳过 `loadToolCalls`）；`preserveScroll` 时 `restoreScroll`；`!skipSessionList` 时 `await loadSessions`（轮询跳过）。函数 `async`，`loadSessions` 必须 `await`。
- 生命周期：`selectSession` 在 `currentSessionId=id` 前 `stopAutoRefresh`，applySessionDetail+scrollToBottom 后 `startAutoRefresh(id)`；`ensureSession` 末尾设空版本+`startAutoRefresh`；`newSession` 开头 `stopAutoRefresh`；`handleDelete` 删当前会话前 `stopAutoRefresh`+清版本；`init` 绑定 `visibilitychange`（hidden 停，visible 且 `!isSending` 时 `startAutoRefresh(immediate=true)` 追赶）与 `beforeunload`（`stopAutoRefresh`），均一次性绑定。
- 错误处理：`fetchJson` 失败抛 `new Error(code)` 无 HTTP status，`session_not_found` 按 `e.message === 'session_not_found'` 识别（禁用 `error.status`）-> `stopAutoRefresh`+清 `currentSessionId`/版本+`setHeader(null)`+`setStatusMessage('会话不存在或已删除')`+`loadSessions` 一次；其它错误 `console.warn` 保留 UI 与版本，下一周期重试，不停止定时器。
- `/task` 版本协调：`persistTaskSystemMessage` 返回 `api.appendSessionMessage` 的消息（含真实 id）；`taskSystemMessage` 持久化前捕获 `preVersion = renderedMessageVersion`，成功且 `persisted.id` 存在时调 `advanceVersionAfterPersistedAppend(realId, preVersion)`：仅当 `preVersion === renderedMessageVersion`（期间未被权威详情改变）才推进为 `{count: preVersion.count+1, lastId: realId}`，否则跳过以权威为准。禁用本地临时 id 或 `__local__` 哨兵冒充服务端版本。
- `refreshCurrentSession` 与 `send` finally：捕获发送时会话 id 与 `++autoRefreshSeq`，响应返回后归属校验（`sessionId===currentSessionId && seq===autoRefreshSeq`），防迟到响应串台；`send` finally `await refreshCurrentSession()`。

陷阱：
- 用单值 `lastRenderedMessageId` 无法表达空会话状态，也不利于乱序响应校验；必须用复合版本 `{count, lastId}`。
- 单飞只用跨世代共享布尔值会让旧世代请求的 `finally` 释放新世代的单飞锁；in-flight token 必须含 `{generation, seq}`，`finally` 仅在 `generation===gen && seq===seq` 时清除。
- `stopAutoRefresh` 不能取消已发出的 Promise；迟到响应必须靠返回后的三重归属校验（会话+世代+序号）丢弃，不能靠取消请求。
- `applySessionDetail` 不 `await loadSessions` 会让会话列表更新变为 fire-and-forget、错误成为未处理 rejection；函数必须 `async`。
- `session_not_found` 用 `error.status === 404` 判断永远不成立（`fetchJson` 抛 `new Error(code)` 无 status），会导致会话被删后持续轮询报错；必须用 `error.message === 'session_not_found'`。
- `selectSession` 重构为 `applySessionDetail` 后须显式 `scrollToBottom()`，否则 `applySessionDetail(preserveScroll=false)` 不滚动，选中会话不再跳到最新消息（回归）。
- 轮询路径必须 `skipToolCalls/skipSessionList/applyExternalMemoryState=true`，否则每 4 秒触发 `loadToolCalls`/`loadSessions`/外部记忆 UI 重渲，造成额外 API 与 UI 抖动。
- `/task` 本地追加后若不推进 `renderedMessageVersion`，下一次轮询会因末条 id 不同误判变更触发无意义 clear+rebuild（闪烁）；必须用持久化响应的真实 id 推进，且期间版本被改变时跳过。

## 模式二十八：Task 三态失败语义（用户取消 / worker 快速失败 / 系统失败）

Task 终态失败必须区分三种来源，分别映射不同 status 与重试策略，不得混用：

| 来源 | 触发方 | 语义 | 工具/路径 | TaskRunOutcome | TaskStatus | 重试 |
|------|--------|------|-----------|----------------|------------|------|
| 取消 | **用户明确指令**（`/task cancel`、取消按钮） | 用户主动终止 | `TaskService.cancel_task` -> `run_service.terminate` -> `dispatcher.cancel` 自取消 worker | TERMINATED | CANCELLED | 否 |
| 快速失败 | **worker 判定**无法继续、确定性放弃 | 必需工具不可用、任务指令禁止兜底、不可恢复前置缺失 | worker `task_fail` -> `TaskService.fail` 写 `fail_requested` 事件 -> executor 返回 ABORTED | ABORTED | FAILED | **否（绕过断路器）** |
| 系统失败 | 运行时异常 | crash/timeout/spawn 非确定性失败 | executor 返回 FAILED/TIMED_OUT/CRASHED/SPAWN_FAILED | FAILED/CRASHED/TIMED_OUT/SPAWN_FAILED | FAILED（触底）或 QUEUED（重试） | 断路器：consecutive_failures > max_retries -> FAILED，否则 QUEUED |

规则：
- worker 工具集只含 `task_show/complete/heartbeat/comment/propose_change/fail`（6 工具），**不含 task_cancel**。取消是用户语义，worker 不得触发取消。
- `task_fail` 是 terminal intent（对齐 `task_complete`/`task_propose_change`）：`TaskService.fail(task_id, reason)` 只写 `fail_requested` 审计事件（非终态），不 finalize；`TaskAgentExecutor._build_result_from_chat` 经 `_read_latest_intent` 识别 `fail_requested` -> 返回 `TaskAgentResult(status=ABORTED, error=reason)`；`TaskRunService._finalize_run` -> `_finish(outcome=ABORTED)` -> `_decide_target_status(ABORTED)` -> **直接 FAILED，不进 `_RETRYABLE_OUTCOMES`，绕过断路器**。
- `goal_mode` 外层循环也必须将 `ABORTED` 与 `WAITING_APPROVAL`、`FAILED`、`TIMED_OUT`、`CRASHED` 一样视为立即退出的终态；否则单轮已识别的 `task_fail` 会被循环忽略，反复发送 `work task {id}` 直至 `goal_max_turns` 耗尽（t_2a913349cfe74c5c）。
- `_decide_target_status` 映射：COMPLETED->SUCCEEDED、WAITING_APPROVAL->WAITING_APPROVAL、TERMINATED->CANCELLED、ABORTED->FAILED（不重试）、EXPIRED->EXPIRED、CRASHED/TIMED_OUT->EXPIRED、FAILED/SPAWN_FAILED->断路器（QUEUED 或 FAILED）。
- `_read_latest_intent` 的 kind 过滤含 `complete_requested`/`change_proposed`/`fail_requested` 三种；找不到任何 intent 事件时默认 COMPLETED（finish_reason="length" -> FAILED）。
- worker 判定无法继续必须调 `task_fail(reason)`，不得调 `task_cancel`（已从 worker 工具集移除）；TASK_GUIDANCE 明确"task_fail 是 worker 主动失败，不是用户取消"。

陷阱：
- worker 判定快速失败若误用 `task_cancel`（用户取消语义），会触发 `cancel_task`->`terminate`->`dispatcher.cancel(worker_token)` **自取消竞态**（worker 取消自己的 asyncio task）：自取消与 worker 正常 COMPLETED 终结 CAS 竞态，COMPLETED 先赢 -> task SUCCEEDED，与 worker"已取消"摘要矛盾（t_a742046a521d46eb 案例）。正确做法是 `task_fail` 走 intent 事件，不自取消。
- 把 worker 主动失败塞进可重试 `FAILED`（`_RETRYABLE_OUTCOMES`）会导致确定性失败被反复重试（execute_code 不可用重试 N 次仍失败，浪费）。必须用独立 `ABORTED` 绕过断路器。
- `task_cancel` 收回用户专用后，worker 工具集枚举、`_dispatch` 分支、`TASK_TOOL_NAMES`、`permitted_managed_tools`、TASK_GUIDANCE、测试必须同步更新；遗漏任一处会留死代码或 worker 仍能调 cancel。
- `_NOTIFIED_OUTCOMES`（task_service.py 与 infrastructure/task/outbound.py 镜像）必须加 ABORTED，否则 worker 快速失败不触发飞书/通知投递。

## 模式二十九：用户侧委派工具的暴露与防递归

对话 Agent（realtime）需要把目标委派给后台任务引擎（Task），但 worker（unattended）不得看到该工具，否则 worker 会递归建子任务。用 `source_type=AGENT + risk_level=SAFE + managed=false` 三属性组合实现“realtime 可见 / unattended 隐藏”的非对称暴露：

| 暴露策略 | 场景 | can_expose 规则（tool_policy.py） | create_task/list_tasks |
|----------|------|-----------------------------------|------------------------|
| DEFAULT | realtime 对话 Agent | 非 DANGEROUS 全暴露 | 可见 |
| SAFE_ONLY | unattended worker / judge | managed+permitted 或 SAFE+非AGENT 或 SAFE+AGENT+granted | 不可见（AGENT 源默认隐藏） |

规则：
- 用户侧工具定义在 Application（`task_tools.py::user_task_tool_definitions`），执行器在 Infrastructure（`user_task_management.py::UserTaskToolExecutor`），与 worker managed 工具（`task_tool_definitions`/`TaskManagementToolExecutor`）分离，不互相污染。
- `source_type=AGENT` 是防递归的关键：SAFE_ONLY 下 AGENT 源工具仅在 `granted_tools` 命中时暴露（这正是 host_terminal 被 worker 可见的机制）。因此必须用回归测试固化“worker/judge 的 `granted_tools` 与 `USER_TASK_TOOL_NAMES` 不相交”，防止未来 grant 改造误授予。
- 会话绑定只读 trusted 通道：`origin_session_id = ctx.session_id`（ChatCompletionService 服务端注入，非客户端可伪造）、`created_by = ctx.trusted_metadata.actor_id`，禁止读 untrusted `ctx.metadata`（模式十二）。
- 幂等键 `chat:{session_id}:{request.id}`：同一 tool-call 重放经 `Task.idempotency_key` 唯一索引去重，registry 抛 `TaskConflictError` -> 执行器映射 `task_conflict`，不重复建任务。
- 公开字段白名单：create 响应仅 `id/title/status/goal_mode`，list 仅 `id/title/status/created_at`，不回传 body/origin_session_id/claim/worker_token；错误用稳定 code（不透传 traceback/数据库文本）。
- 装配条件：仅在 `task_registry is not None`（task 子系统可用）分支注册 routes + `set_dynamic_definitions("user_task", ...)`，并在注册前断言两工具名未与既有 static/dynamic 定义或 route 冲突；task 子系统不可用时两工具不注册、`/chat/tools` 不显示。
- 复用既有执行链：任务创建后走同一 TaskRunner `dispatch_once` 认领、`task_execution_session_id(task)` 选择器（origin 优先）路由回当前会话、`ui.task_lifecycle` 生命周期消息；不新增状态机/表/迁移。

陷阱：
- 若误把用户侧工具设为 `source_type=BUILTIN`，SAFE_ONLY 会暴露给 worker -> worker 调 create_task 递归建子任务。必须用 AGENT 源。
- 若未来给 worker `granted_tools` 加入 `create_task`/`list_tasks`（仿 host_terminal 授权），SAFE_ONLY 会暴露 -> 防递归失效。grant 禁令必须由回归测试守护。
- `list_tasks` 服务异常应映射 ERROR + `success:false` + `task_list_failed` + 空 items，不得返回 `success:true`+SUCCESS（自相矛盾，Agent 会把错误误判为空列表）；异常捕获只包服务调用，过滤逻辑的 bug 应向上传播不被静默吞掉。
- 用户侧工具不得用 `permitted_managed_tools` 绕过暴露（该通道只适用 managed 工具）；`managed=false` 决定它走 SAFE 暴露路径，不走 managed 门控。

## 模式三十：LLM 前缀缓存依赖 Ark 服务端自动缓存（无 cache_control 断点）

当前 LLM 请求的前缀缓存命中完全依赖火山方舟 Ark `/api/plan`（Anthropic 兼容端点）的服务端自动接纳，代码侧未设任何 `cache_control` 断点，因此缓存是否生效不由前缀稳定性单方面决定，而受 Ark 不透明的接纳/预热/驱逐逻辑支配。

数据流：
- `AnthropicProvider.chat`（`app/infrastructure/llm/anthropic_provider.py`）走 `anthropic` SDK 的 Messages API；`_convert_messages` 把 system 拼成纯字符串经 `kwargs["system"]` 传入，messages 转 content blocks，全程不注入 `cache_control:{type:"ephemeral"}`。`cache_control` 仅出现在 `_ALLOWED_OPTION_KEYS`（允许透传），但无任何调用方（agent_graph/context_service/prompt_builder）构造或传入。
- active provider `ark-agent` 的 `extra_headers_json` 为空，无 `anthropic-beta: prompt-caching` 头；pre/post-commit grep 均无 cache_control 注入，证明命中的零星缓存纯靠 Ark 服务端自动行为。
- usage 记录：`AgentGraphRunner.call_llm` 在图内强制 `stream=False`（line 787 拒绝流式结果），从 `response.usage` 取 `model_dump()`；`_resolve_usage_meta` 据 `provider_type=="anthropic"` 选 `_normalize_anthropic`，读 `cache_read_input_tokens`/`cache_creation_input_tokens`。Ark 对自动缓存不报告 write，故 `cache_write_tokens` 恒为 0 是正常现象，不是"未尝试缓存"的信号；真正信号是 `cache_read_tokens`。

规则：
- 前缀缓存命中要求 system + tools + 早期消息逐字节稳定（append-only）。验证稳定性须比对实际发出的 `request_messages`（经 `_convert_messages` 转换后的 Anthropic 侧字节），不止比对 SQLite 存储的 OpenAI 格式；`sort_keys=True` 会掩盖键序差异，须用原始序列化比对。
- system prompt 长度影响 Ark 自动缓存接纳：旧版 2911 字符 prompt 在增长对话上 cache_read 稳步增长（4k->20k+）；commit `5bf400f` 把 TASK_GUIDANCE 烘进 build_system_prompt 使其增至 6741 字符后，dashboard 增长对话 cache_read 跌为 0（会话 dashboard-fc1efd1f-57e9-4b10-9b49-22529ae83c77，10 次调用全零）。命中/未命中在 commit 点呈完美阶跃，provider/endpoint/headers 全程未变。
- "稳定 system prompt 以保住前缀缓存"的改法（commit 5bf400f 意图）只有在缓存真正启用时才生效；单纯稳定前缀而不设 cache_control，仍受 Ark 自动接纳逻辑制约，可能因 prompt 变长而整体失效。

陷阱：
- 误判 `cache_write_tokens=0` 为"未尝试缓存"。OpenAI/Anthropic 路径下 Ark 不报告 write，须以 `cache_read_tokens` 判断；归一化路径选择错误（provider_kind 与实际协议不符）会导致字段全错位。
- 依赖服务端自动缓存时，个例命中（如 c58bb129 仅 fresh-context judge 续轮命中 1 次 4408）与完全未命中（fc1efd1f）可能仅差 1 字符（"Judge" vs "judge"），属 Ark 不透明逻辑，不可用前缀内容差异解释，也不可据此推断前缀不稳定。
- 修复方向：在 AnthropicProvider 显式设 cache_control 断点（system content block + tools 末项 + 每轮最后一条消息），使缓存由代码确定性保证，与 prompt 长度/Ark 自动接纳解耦。前置必做：实测 Ark `/api/plan` 是否透传并兑现 cache_control（发两次相同前缀看第二次 cache_read_input_tokens 是否 >0）；不兑现则改走 Ark 原生上下文缓存 API 或 openai-compatible 端点（ark-code，OpenAI 风格自动缓存）。

## 模式三十一：Task lifecycle card 跨文件交互化模式

Dashboard Chat 的 `ui.task_lifecycle` 系统消息在 waiting_approval/failed/expired 三态从纯文本升级为飞书风格 inline 交互卡片。card 是生成消息时的状态快照（非当前任务状态的权威来源），与纯文本 `content` 并列存入 `ConversationMessage.card`，横跨 Domain/Application/Infrastructure/Interfaces 四层与前端，通过版本化 schema + Domain 动作单一来源 + 前端实时状态校验 + 显式 handler allowlist 保证安全。

跨文件链路：
1. Domain 动作单一来源（`app/domain/task.py::available_lifecycle_actions(status)`）：纯函数返回 tuple，WAITING_APPROVAL->(approve,reject,revise,cancel)、FAILED->(retry,cancel)、EXPIRED->(retry)、其余空。EXPIRED 仅 retry 因 `Task.cancel()` 明确拒绝 EXPIRED。不执行 IO，服务端动作 API 与 Task 聚合仍是最终合法性边界。
2. Application 构造 card（`app/application/task_run_service.py::TaskRunService._lifecycle_card(task, target_status, summary, error)`）：返回版本化 payload `{schema_version:1, kind:"task_lifecycle", task_id, status, title, summary, available_actions}`；status 用传入的 target_status（CAS 目标态）不用 task.status（可能仍是旧快照）；summary 来源按状态分流（waiting_approval 取 proposal，failed 优先 error 再取 summary，expired 后备稳定文案"任务运行已过期"）；available_actions 取 Domain 元组顺序（前端不重排）。非交互态（QUEUED/RUNNING/SUCCEEDED/CANCELLED）返回 None。
3. Application 透传（`app/application/session_service.py::SessionService.append_task_lifecycle_message(session_id, content, card=None)` -> `_append_system_message(card=None)` -> `_normalize_card`）：`copy.deepcopy` 调用方 dict 不原地修改，按 `_truncate_task_message_utf8` 把 summary 截断到 65536 UTF-8 字节上限，构造 `ConversationMessage(role="system", name="ui.task_lifecycle", content, card=normalized)`。
4. Infrastructure 持久化（`app/infrastructure/memory/sqlite_store.py`）：messages 表 `card_json TEXT` 列（幂等迁移 `_migrate_add_card_column`）；`append_message`/`append_message_if_session_exists` INSERT 写 card_json（None->NULL、dict->`json.dumps(ensure_ascii=False)`）；`list_messages` 经 `_decode_message_card(row)` 容错读取（列缺失/NULL->None，合法 JSON object->dict，非法 JSON/array/scalar->None+warning 不丢失消息，不掩盖 content_json 错误）。
5. Interfaces 序列化（`app/interfaces/http/dashboard.py::_message_to_dict`）：始终输出 `"card": message.card`（object 或 null），既有字段与 tool_calls 规范化不变。
6. 前端校验与渲染（`app/interfaces/http/static/chat.js`）：`validateTaskCard(card)` 严格校验 schema_version/kind/task_id/status/title/summary/available_actions 返回新 canonical object（未知动作丢弃保留原序、空动作返回 null，禁止 `api.task[action]` 动态属性索引）；`resolveTaskCardStates(messages)` 按 task_id 去重并行 `api.task.get` 返回权威 status Map；`computeCardState(card, entry)` 按 card.status 独立判定 active/stale/unavailable；`buildTaskCardElement` active 创建按钮+textarea，stale/unavailable/settled 只渲染正文+反馈；`handleTaskCardAction` in-flight 防重复 + 成功 settled + 状态错误 stale + task_invalid 恢复 + revise `Array.from(note).length` code-point 1..2000 校验。

三参数 lifecycle_writer 签名：
- TaskRunService 与 TaskService 的 `lifecycle_writer` 均为 `Callable[[str, str, dict[str, Any] | None], Awaitable]`，第三参数为可选 card 载荷。
- 纯文本 lifecycle（succeeded/cancelled/开始运行/决策回执 approve/reject/revise）传 card=None；交互态（waiting_approval/failed/expired）传构造好的 card payload。
- `main.py::_task_lifecycle_writer(session_id, content, card=None)` 闭包透传给 `SessionService.append_task_lifecycle_message`，`SessionNotFoundError` 静默跳过不复活。

实时状态校验（防止存量卡片继续操作）：
- 消息存储是追加式历史记录，已发生的卡片不会被后续消息删除。动作成功或任务被其它通道处理后，旧卡片保留为只读历史状态，不再暴露动作。
- 每次渲染会话前，前端对本次消息中具有动作的不同 task_id 各调用一次既有 `GET /chat/tasks/{task_id}`（去重，避免同一任务 N 次请求）。
- 仅当返回状态等于 card.status 时标记 active 启用动作；状态不一致或 task_not_found/task_state_invalid/task_conflict 标记 stale；网络或未知错误标记 unavailable。
- 校验期间按钮不出现或保持 disabled，禁止先短暂提供未经校验的动作。
- 动作返回 task_state_invalid/task_conflict/task_not_found 时卡片转 stale 并刷新；网络/未知错误恢复控件供重试；成功后保持禁用并先标记"操作已提交"再 `await refreshCurrentSession()`。
- refresh 在动作成功后失败时保持 settled 和 disabled，显示"操作已提交，刷新失败"，不得因刷新失败重新提交相同动作。

安全约束：
- 前端使用显式 `TASK_CARD_ACTION_HANDLERS` allowlist（action string -> 固定 api.task 函数），即使数据库内容被篡改也不得按任意 action 名动态调用属性。
- 所有动态文本使用 `textContent`（不使用 `innerHTML`）；反馈区 `aria-live="polite"`；textarea 关联 label；按钮使用原生 `<button type="button">`。
- `task_id` 必须经 `encodeURIComponent` 进入 URL；revise 请求体固定为 `{"note": note}`，note 按 Unicode code point（`Array.from(note).length`，非 UTF-16 单元）trim 后 1..2000 校验，与服务端合同一致。
- card payload 不含密钥、worker token、expected_version 或 HTML。
- 仅 `ui.task_lifecycle` 支持 card；`ui.task_command` 和 `ui.task_result` 不交互化。无 card 的存量数据库和历史消息必须兼容（回退原 `<details><pre>` 纯文本渲染）。

不改边界：
- 不改 Task 状态转换、TaskPolicy、worker 的 6 个 managed task 工具、自然语言审批工具、看板、slash 命令、飞书 IM 或 ACP。
- 复用既有 GET task 与 approve/reject/revise/cancel/retry API，不新增端点。
- 服务端 Domain 函数是动作集合单一来源；前端 allowlist 只做安全分发，不推导状态动作。

陷阱：
- `_lifecycle_card` 误用 `task.status` 而非 `target_status` 会让 card 携带 CAS 前的旧状态，前端状态校验恒判 stale。必须用传入的 target_status 反映本次 CAS 的目标态。
- 前端 `api.task[action]` 动态属性索引会让数据库被篡改的 action 名调用任意 API 方法；必须用显式 `TASK_CARD_ACTION_HANDLERS` allowlist。
- `textarea.maxLength` 按 UTF-16 单元计数，emoji 等代理对字符会被截断；revise note 校验必须用 `Array.from(note).length` 按 Unicode code point 计数。
- 历史卡片因后续 best-effort lifecycle 写入失败或任务被其它通道处理而保留为只读，不得承诺新消息会替换或删除旧消息；前端实时状态校验是防止存量卡片继续操作的唯一可靠手段。
- `_decode_message_card` 用 broad exception 吞掉所有错误会掩盖 content_json 或数据库异常；只容忍 card_json 的 JSON 解析错误，content_json 与数据库错误照常传播。
- `groupTaskMessages` 遇带合法 card 的 lifecycle 消息必须关闭当前合并组并独立 push，否则 card 字段会在 `{...firstMessage}` 浅拷贝中被覆盖或丢失；card 消息也不得吸纳后续消息。
- in-flight 防重复只用单一布尔值会让第二次点击在第一次 await 期间触发；必须用 `inflight.value` 守卫 + 按钮 disabled 双重保护，且 finally 仅在 actions 容器仍 attached 时恢复控件。

## 模式三十二：顶导菜单与多路由双入口

Dashboard 在左导不变的前提下新增可选顶导菜单组件，按子域展示横向关注点（如任务子域：管理/观测），支持同一 renderer 多路径与左导/顶导双路由入口。顶导样式参考 odin-fe（50px 高、选中主色+加粗无下划线、溢出平移+箭头）。

配置与路由分离规则：
- `topnavConfig`（`management-navigation.js`）按子域（tab key）配置顶导 items，每项含 `tab`/`path`/`label`/`concern`/`scope`/`topnavParent`；当前 tasks 子域配置了 管理/观测/安全 三个 item（`安全` -> `/tasks/security`，单路径无别名）。`topnavConfig` 是可选配置，未配置的子域不渲染顶导、恢复标题展示。
- `routeConfig` 是独立于 `tabConfig` 的路由表，支持多路径映射到同一 renderer（`paths: ['/tasks/observations', '/observations/tasks']` -> `renderTab: 'tasks-observations'`）；`buildRouteByPath` 在模块加载时校验 required 字段（tab/renderTab/sidebarTab/topnavParent/scope）、paths 非空数组、path 以 `/` 开头、无重复 path，校验失败 throw Error。
- `sidebarOverride` 允许路由级覆盖 sidebarTab（如 `/observations/tasks` 路由的 sidebarTab 覆盖为 `observations-sessions`，使左导"会话"项高亮而非"任务"项），实现左导不变前提下双入口的侧栏高亮对齐。
- `resolveRoute(pathname)` 优先查 `routeByPath` 精确匹配（命中返回 route state 含 activeTab/renderTab/sidebarTab/currentSubdomain），未命中回退 `tabByPath`/`selectedTabFromPath` 既有路径。`applyRoute(state)` 统一应用：切换 tab active class、sidebar active class、topbar title、调 `onTabActivated(state)`。
- `navigatePath(path)` 是唯一写 history 的入口（pushState + resolveRoute + applyRoute），popstate 与 sidebar click 均迁移到此函数；移除旧 `applyTab`，selectedTabFromPath 接受可选 pathname 参数支持离线调用。

顶导组件规则（`topnav.js` NAGENT.topnav={render,destroy}）：
- `render(container, opts)` 构造单个 nav + a 链接项 DOM（不在既有 nav 内嵌套 nav），`aria-current="page"` + `topnav__item--active` 标记当前项；点击链接 `preventDefault` 后调 `onActivate(item)` 回调，不直接 pushState（交由调用方 `navigatePath`）。
- 精确高亮：`activeTopnavItem(items, activeTab)` 用 `items.find(i => i.tab === activeTab)` 精确匹配，禁止 `indexOf`/`includes` 等模糊匹配（避免路径前缀误命中）。
- 溢出控制：ResizeObserver 优先检测 scrollWidth > clientWidth，不可用退回 window resize；溢出时显示左右箭头按钮（step 80% clientWidth），滚轮 deltaY 转水平滚动，方向键在链接间移动焦点；`prefers-reduced-motion: reduce` 时用原生 `scrollLeft` 替代 `transform: translateX()` 平移。
- 生命周期：重复 render 先 destroy 旧实例避免重复监听器；`destroy()` 设 `disposed=true` 使残留闭包回调不再生效，disconnect ResizeObserver，removeEventListener，replaceChildren 清空容器。
- 安全降级：items 缺 tab/path/label 必填字段时跳过非法项（不 throw），生产环境静默、开发夹具 `namespace.__DEV__` 时 console.error 报告；activeTab 不在 items 中时无当前项渲染 + 开发报告。

双入口规则：
- 顶导入口：顶导点击 -> `navigatePath(item.path)` -> routeConfig 命中 -> renderTab 激活 tasks-observations 模块，sidebarTab 按 route.sidebarTab 高亮。
- 左导入口：左导"观测 > 会话"（`/observations/sessions`）渲染 observations.js，index 视图新增 scope 控件 全部/任务 chip；点击"任务" chip 调 `navigatePath('/observations/tasks')` 下钻到 tasks-observations 模块（routeConfig 命中同一路由，sidebarOverride 使左导"会话"项高亮）。
- 两个入口经 routeConfig 汇聚到同一 renderer，sidebar 高亮通过 sidebarOverride/sidebarTab 区分：顶导入口 sidebarTab=tasks（左导"任务"高亮），左导入口 sidebarTab=observations-sessions（左导"会话"高亮）。

异步 guard 规则：
- `app.js` onTabActivated 接收 state 对象（非裸 tab 字符串），`normalizeState` 兼容两种输入；`renderTopnav(state)` 在模块 init 前先渲染顶导（模块错误不阻断顶导状态）。
- `inflight` map 共享并发 onTabActivated 的 init Promise，防止同一 tab 重复 init；init 失败调 `renderModuleError` 在容器内显示错误不 throw。
- `tasks-observations.js` 与 `observations.js` 均用 `renderToken`（monotonic counter）+ `isActive()` 双重 guard：late response 的 token != current 时丢弃，tab 失去 active class 时丢弃；`inflight` Promise 去重防并发重复请求（force=true 时 supersede 旧 inflight）。

shell 路由注册规则（`dashboard.py`）：
- 字面路由 `/tasks/observations`、`/observations/tasks`、`/tasks/security` 用堆叠装饰器置于 `/tasks/{task_id}` catch-all 下方（自下而上注册，字面路由先于 catch-all 命中），均返回 index.html 外壳由前端按 pathname 选 tab。
- API 路由 `/chat/tasks/security` 必须在 `register_task_routes`（注册 `/chat/tasks/{task_id}`）之前注册，否则被 catch-all 吞噬。

共享 renderer 复用规则（`security.js` -> `tasks-security.js`）：
- 子域页复用全局页渲染器：`security.js` 暴露 `namespace.security.renderers = {overview,sector,meta,cfg,policyItem,statCard,formatValue}`（shortened key，对标 `namespace.observations.renderers` 供 `tasks-observations.js` 复用的既定模式），`tasks-security.js` 经 `const R = (namespace.security && namespace.security.renderers) || {}` 取用。
- renderer 必须是纯 DOM 构造函数（取参数、不读子域页闭包 state）；通过向后兼容可选参数（`overview(data,options)`/`sector(policy,options)`）扩展子域页专用展示（如 `countLabel:'Sector 数量'`、`showSourceFiles:true`），全局页单参数默认调用行为不变。
- renderer 暴露必须在 `{init,refresh}` 赋值之后挂载（`global.NAGENT.security = {init,refresh}; global.NAGENT.security.renderers = {...}`），避免对象字面量赋值覆盖 renderers。

陷阱：
- `topnavConfig` 与 `tabConfig` 混淆会让顶导依赖左导的父子结构，破坏"左导不变"约束；`routeConfig` 与 `tabByPath` 混淆会让多路径同 renderer 无法表达（tabByPath 是 1:1 映射）。正确做法：topnavConfig/routeConfig 是独立配置，tabConfig/tabByPath/pathByTab/parentByChild 不改。
- `activeTopnavItem` 用 `indexOf(path)` 匹配会让 `/tasks` 误命中 `/tasks/observations`，导致顶导"管理"项在观测页也高亮；必须用 `tab === activeTab` 精确匹配。
- 顶导组件直接 pushState 会让路由层失去统一控制（sidebarOverride/applyRoute 副作用丢失）；必须通过 onActivate 回调交由 `navigatePath`。
- 字面路由 `/tasks/observations` 置于 `/tasks/{task_id}` 上方会被 catch-all 吞噬（FastAPI 按注册顺序匹配，堆叠装饰器自下而上注册，源码下方先注册）；必须置于 catch-all 下方。
- `tasks-observations.js` 的 `api.listSessions()` 后客户端按 `source === 'task'` 过滤是前端语义，禁止把 scope 作为后端查询参数传递（后端 listSessions 不支持 scope 过滤，会忽略）。

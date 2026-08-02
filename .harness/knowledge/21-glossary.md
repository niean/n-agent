<!-- SUMMARY: N-Agent 与后续完整 Agent 能力相关术语定义，含 Gateway/CLI/ACP、ToolPolicy、Host Terminal、Context 与 Usage 观测、Skill 自进化术语 -->
# 术语表

- Agent Runtime：Agent 的内部运行机制，负责加载上下文、调用 LLM、执行工具、更新 Memory、判断结束条件，并产出应用级运行事件。
- AgentRun：一次 Agent 运行的领域对象，包含会话、输入消息、运行状态、迭代计数、错误和结束原因。
- AgentState：Agent Runtime 在运行过程中传递的状态，包含工作消息、待执行工具调用、工具结果、摘要、状态和迭代次数。
- Application Layer：应用层，负责编排用例和 LangGraph 状态图，依赖 Domain 端口，不依赖 Infrastructure 具体实现。
- ChatEvent：Application 层输出的聊天运行事件，供 Interfaces 层编码为 OpenAI-compatible SSE chunk 或非流式响应；含 `metadata: dict[str, Any]` 字段携带 Gateway confirmation/duplicate 等事件元信息，避免 CLI 解析中文提示文案提取结构化数据。
- ChatEventType：聊天事件类型枚举，包括 MESSAGE_START、CONTENT_DELTA、TOOL_CALL_DELTA、MESSAGE_DONE、ERROR、DONE；AgentGraphRunner.stream_events 按此顺序产出事件流。
- GatewayService.handle_message_stream：Gateway 流式接口，`async def handle_message_stream(event, *, model_override=None, options_override=None, trusted_metadata_override=None, approval_decider=None, allowed_confirm_tools_override=None) -> AsyncIterator[ChatEvent]`，与 `handle_message` 共享幂等、destructive preflight、session 解析、Slash 分流、默认模型和 trusted metadata 逻辑；ACP `session/prompt` 通过 override 保留 ACP model/mode/cwd/permission bridge 语义后复用 Gateway → ChatCompletionService 链路；destructive preflight 命中时 yield MESSAGE_DONE(finish_reason="confirmation_required", metadata=confirmation) 供 CLI 发起 /confirm 回调。
- CliChatAdapter：CLI Interfaces 层的聊天入口适配器，构造 InteractionMessage（注入 `actor_id=cli:{conversation_id}`）并委托 GatewayService.handle_message_stream/handle_message/handle_confirmation；CLI REPL/单次消息/确认操作均通过此适配器。
- patch_stdout：prompt_toolkit 提供的 stdout 重定向上下文管理器，REPL 内 rich console.print 必须在 patch_stdout 内执行，避免与 prompt_toolkit 抢占 TTY 导致 TUI 乱屏。
- Domain Layer：领域层，定义核心业务模型、值对象、领域规则和端口协议，不依赖 FastAPI、LangGraph、SQLite、OpenAI SDK 或工具 handler。
- Domain Port：领域层定义的外部能力协议，如 LLMProvider、ToolExecutor、MemoryStore、Summarizer，由 Infrastructure 实现。
- Evolution Baseline：演进基线。当前阶段只实现既定验收范围，但架构边界必须支持后续完整 Agent 能力持续扩展。
- Infrastructure Layer：基础设施层，实现 Domain 端口和外部依赖细节，如 Provider SDK、SQLite store、工具 handler、配置加载。
- Interfaces Layer：接口层，实现 FastAPI、OpenAI-compatible API、Dashboard、SSE 编码和错误映射，只调用 Application 用例。
- LLM Adapter：模型适配层，通过 LLMProvider 端口屏蔽不同模型 Provider 的协议差异。
- LLMProvider：领域端口，定义模型列表、聊天调用、流式事件和工具支持能力的统一接口。
- Memory/Context：Agent 的会话历史、消息、工具调用、任务状态、摘要和上下文预算管理能力。
- MemoryStore：领域端口，定义会话、消息、工具调用、任务状态和摘要的读写接口。
- N-KB：独立知识库服务，N-Agent 通过 HTTP 检索接口消费其通用知识，不把索引和文档管理能力嵌入自身。
- OpenAI-compatible API：对外兼容 OpenAI Chat Completions 风格的 HTTP API，用于接入 Open-WebUI 等客户端。
- Open-WebUI：使用 OpenAI-compatible API 接入模型或 Agent 服务的 Web UI 客户端。
- Platform：交互平台领域枚举，面向 feishu、dingtalk、wecom 等外部消息平台；用于真实平台的 trusted_metadata、任务 origin 和 Dashboard 平台视图。CLI/TUI 终端聊天使用 GatewaySessionKey.source=`cli`，不进入 Platform 枚举或 PlatformRegistry。
- PlatformLifecycle：平台运行态端口，提供 is_connected 与 fatal_error；当前 FeishuImAdapter 实现该端口并由 PlatformRegistry 暴露给 PlatformService。
- PlatformRegistry：平台注册端口，提供平台 descriptor 与 lifecycle 查询；当前由 InMemoryPlatformRegistry 在 main.py 启动装配时构建。
- PlatformService：Application 层平台只读用例，组合 PlatformRegistry 与 GatewaySessionRegistry，输出平台状态、配置摘要、会话统计和会话分页。
- Provider：具体 LLM 服务提供方或协议实现，如 OpenAI-compatible endpoint、Claude、Ollama、OpenRouter。
- ProviderConfig：Provider 注册表中的脱敏配置实体，描述 id、name、provider_type、base_url、model、api_key_present、is_active 等字段，永不包含 api_key 明文。
- ProviderRegistry：领域端口，定义多 Provider 配置的 CRUD、active 切换、明文 api_key 单独读取（仅供 Infrastructure 工厂调用）。
- ActiveProviderHolder：Application 层适配器，实现 LLMProvider 协议；通过工厂回调懒加载底层 Provider 实例并以 asyncio.Lock 保护 swap，使下游服务无感知地热切换 active provider。
- search_knowledge：N-Agent 暴露给 LLM 的 safe tool，用于按需调用 N-KB 通用知识检索。
- Summarizer：领域端口，定义上下文摘要生成能力，当前可用启发式摘要，后续可替换为模型驱动压缩。
- ContextEngine：Domain 端口（`app/domain/context.py`），定义上下文短期记忆压缩能力；协议方法 `should_compress(messages, *, prompt_tokens, force)` 判定是否需压缩，`compress(messages, *, current_tokens, force, existing_summary)` 返回 `ContextCompressionResult`。由 Infrastructure 的 `ContextCompressor` 实现。
- ContextCompressor：Infrastructure 实现类（`app/infrastructure/context/context_compressor.py`），实现 `ContextEngine`；提供 token 估算、cooldown 防抖、三段式压缩（head `protect_first_n` 保护 + 中段 LLM 摘要 + tail `protect_last_n` + token 预算分配）、增量压缩（`_find_latest_context_summary` 定位上次摘要切点，middle 只取新增消息）、工具组完整性对齐、sanitize 剥离未配对 tool 消息、LLM 摘要失败回退到 `fallback_summarizer`。
- CONTEXT_SUMMARY_PREFIX：Domain 常量（`app/domain/context.py`），值为 `"[CONTEXT SUMMARY]: "`；运行时识别摘要消息的 content 前缀，ContextCompressor 用它在 messages 中定位上次摘要切点，prepare_context 的压缩阶段用它识别 result.messages 中的摘要消息。
- ContextCompressionResult：frozen dataclass（`app/domain/context.py`），字段包括 messages（压缩后的消息列表）、summary（生成的摘要文本）、compressed（是否实际压缩）、skipped_reason（跳过原因，如 cooldown/threshold/empty/summary_in_tail）、original_tokens、compressed_tokens。
- prepare_context：LangGraph 节点（`app/application/agent_graph.py`），位于 `call_llm` 之前；加载历史消息与摘要，构造 working_messages，并调用 `ContextEngine.should_compress` 按需执行增量三段式压缩；仅在真正压缩时调 `external_memory_manager.pre_compress_all` 提取 rescued_context，rescued_context 拼入 state.summary 但不进入摘要消息 content。
- Three-segment compression（三段式压缩）：ContextCompressor 的压缩策略；head 段保留前 `protect_first_n` 条消息（含 system prompt + 早期关键上下文），中段送入 LLM 生成结构化摘要，tail 段保留最后 `protect_last_n` 条消息并按 token 预算分配；工具组完整性对齐避免截断 assistant tool_calls 与对应 tool 消息，sanitize 剥离未配对的 tool_calls/tool 消息。
- Incremental compression（增量压缩）：ContextCompressor 对齐 HermesAgent 的压缩方法；`_find_latest_context_summary` 从后往前扫描 messages 定位最后一个 content 以 `CONTEXT_SUMMARY_PREFIX` 开头的 user 消息，middle 只取该摘要之后的新增消息，`_generate_summary` 分首次路径（空 existing_summary + FIRST 模板）和迭代路径（previous_summary=body + ITERATIVE 模板）两条 prompt 路径。
- is_summary / is_summarized（摘要双标记）：`is_summary=1` 表示摘要消息，`is_summarized=1` 表示已被摘要吸收的原消息。`ContextService.prepare_context` 通过 `append_summary_message` 追加新摘要，再用 `mark_messages_summarized` 标记 middle 原消息；Provider Context 只选最新摘要并过滤已被吸收的原消息。`_message_to_provider` 不传递这两个持久化标记，Dashboard API 使用 is_summary 做特殊渲染。
- Cooldown（压缩冷却）：ContextCompressor 内存中的 monotonic 时间戳（`_last_compressed_at`），防止在 `cooldown_seconds` 窗口内重复压缩同一会话上下文造成抖动；`should_compress` 检测 cooldown 未到期时返回 False（force=True 可绕过）。
- Policy（Shared Kernel）：`app/domain/policy.py` 的通用策略协议，不是独立全局核心子域或中央服务；只统一 `Policy` Protocol、`PolicyOutcome`、`PolicyDecision`，具体业务规则归各领域 `XPolicy`。
- PolicyOutcome / PolicyDecision：公共策略结果枚举与值对象；结果为 allow、deny、require_approval，decision 同时携带非空 reason。
- Tool Registry：服务端工具注册表，管理工具定义；工具真正可用还要求存在对应 `ToolExecutor` 执行路由。
- ToolDefinition：工具定义值对象，描述工具名称、说明、输入 schema、风险等级、权限、超时和启用状态，不包含具体 handler。
- ToolExposurePolicy：Tool Domain 的模型暴露场景，当前为 default、safe_only。
- ToolPolicyRequest / ToolPolicy：工具策略请求与 Tool Domain 具体策略；负责定义校验、模型暴露、执行允许/拒绝/需审批和一次授权。
- ToolExecutionEvaluation：Application 层执行评估结果，包含 `PolicyDecision` 和审批快照，并以内部 token 绑定原请求与原定义，供 `ToolService` 防止审批期间定义替换。
- ToolDefinition.managed：布尔字段，标记需要服务端 ChatCompletionService 显式授权才能执行的工具；当前 `manage_schedule` 是唯一 managed 工具，必须 `risk_level=CONFIRM` 且来源为 AGENT。
- ToolExecutionContext.trusted_metadata：服务端注入的 metadata 字典，仅 GatewayService/Feishu 长连接适配器在 ChatCompletionInput 中写入；OpenAI HTTP 客户端 metadata 不进入此字段，作为 managed-tool 授权的事实来源。
- permitted_managed_tools：ToolExecutionContext 字段，记录当前请求允许执行的 managed 工具集合；非 realtime 模式或非可信 Gateway 来源时为空集，managed 工具被 fail-closed 拒绝。
- manage_schedule / schedule_query：Agent 定时任务管理 / 查询工具，source_type=AGENT、toolset=schedule；前者 managed=True 仅飞书 trusted_metadata 触发，后者 SAFE 但在 unattended（safe_only）模式仍被过滤避免调度器递归。
- ToolExecutor：领域执行 SPI，具体实现属于各支撑子域或 Infrastructure；ToolDefinition 与执行路由必须同时存在。
- ToolResultStatus：工具执行结果状态枚举，描述成功、错误、权限拒绝和超时等标准状态。
- ToolService：Application 层工具强制执行边界，统一注册定义、按 ToolPolicy 暴露和评估、生成一次授权并在调用 `ToolExecutor` 前复判；调用方不得绕过。
- GatewayToolApprovalService：Application 层进程内 ToolPolicy 会话授权服务，按 session_id、actor_id、tool_name 隔离“本会话信任”；不替代 ToolService 的一次授权与执行前复判。
- FeishuToolApprovalBridge：Interfaces 层 ApprovalDecider 协议桥，把 ToolPolicy 审批转换为飞书 interactive card 和 Future；只持有带 TTL、actor/chat/card message id 绑定及原子 claim 的 pending，不拥有业务授权。
- CliToolApprovalBridge：Interfaces 层 ApprovalDecider 协议桥（CLI/TUI），把 ToolPolicy 审批转换为进程内 Future + 精确命令路由（`/confirm once`/`/confirm trust`/`/cancel`）；持有带 900 秒 TTL、actor_id+GatewaySessionKey 双绑定及原子 claim 的 pending，不拥有业务授权；仅在 TTY REPL 构造，非 TTY/单次消息/stdin pipe 不注入 decider。
- Toolset：工具集合或能力分组，用于后续按场景启用、禁用、检查依赖和控制权限。
- ExternalMemoryProvider：外部记忆提供者领域端口（SPI），定义 prefetch/sync_turn/system_prompt_block/handle_tool_call + 生命周期钩子；实现分为 builtin、multi-project 和 external-query（mem0/holographic/honcho）三个槽位。
- Memory Slot：ExternalMemoryManager 的三槽模型：builtin、multi-project、external-query。仅 external-query 至多一个 active provider，`swap_external_query_provider` 不影响前两槽。
- 系统记忆（builtin slot）：ExternalMemoryManager 的 builtin 槽位中文名，对应槽位常量 `_BUILTIN_SLOT`。全局内置 Markdown 记忆，由 `BuiltinProjectMemory` 实现，存储 `{project_root}/locals/external-memory/{memory,user}.md` + sidecar `memory.meta.json`；含 trust 评分、时间衰减、矛盾检测机制，是唯一内置可信度体系的槽位。
- 文件记忆（multi-project slot）：ExternalMemoryManager 的 multi-project 槽位中文名，对应槽位常量 `_MULTI_PROJECT_SLOT`。按项目子目录管理多组 `{memory,user}.md`，由 `MultiProjectMemory` 实现；通过 `set_enabled_projects` 选择启用项，跨 enabled project 合并打分取全局 top-K，返回文本用 `## Project: {name}` 前缀区分来源。
- 检索记忆（external-query slot）：承载 mem0、holographic 或 honcho，由 `swap_external_query_provider` 单独替换，不影响 builtin / multi-project。
- 检索记忆 provider：通过结构化或语义检索召回记忆，也可提供工具和 `sync_turn`。当前支持 mem0（HTTP）、holographic（本地 SQLite + MemoryRetriever）、honcho（HTTP）。
- ActiveExternalMemoryReader：Application 层只读端口，`get_active_provider_names() -> list[str]`，读 ExternalMemoryManager 内存状态、无 IO；由 ExternalMemoryProviderService 实现。当前不消费于默认 profile 派生（未传 `external_memory_enabled` 时统一默认 `[]`，不自动启用 builtin 或 active 检索记忆 provider），接口留存供未来别处使用。
- tool_surface_refresh_failed：activate/swap 返回的布尔标志，标记工具面回调（刷新 ToolService dynamic_definitions + Composite routes）是否失败。回调在 swap_lock 内同步执行，异常不阻塞 swap 本身，仅置标志供 API 响应透传。
- provider_swapping：ExternalMemoryManager.handle_tool_call 在 swap_lock 持有期间对检索记忆槽工具返回的错误，避免 swap 进行中路由到不一致的工具实现。
- has_override：ChatCompletionService 首轮 profile 派生时的字段存在性判断（`"external_memory_enabled" in request.options`），区分"客户端未传字段"与"客户端显式传 []/['builtin']"。前端 chat.js 维护 `externalMemoryTouched` 标志，未操作时不发送该字段，使后端派生默认空 profile；操作后发送用户显式选择。
- Plugin：N-Agent 的插件子系统领域聚合，遵循 Hermes plugin 模式（`plugin.yaml` + `register(ctx)` entrypoint），支持零成本移植开源插件生态。Plugin 聚合包含 key/name/version/kind/source/enabled/config/secret_refs/capabilities/manifest 等字段，`to_public_view` 输出 secret_refs 为 `{field: bool}` 占位标记（不含明文）。
- PluginManifest：Plugin 的值对象，由 `plugin.yaml` 解析而来，包含 name/version/kind/provides_tools/requires_env/config_schema 等字段；`from_yaml(raw, source, key, path)` 接受显式 key 参数（不从路径推导）。
- PluginContext：Application 层提供给 plugin `register(ctx)` 的上下文对象，`register_tool(name, toolset, schema, handler, check_fn, requires_env, is_async, description, emoji, override)` 与 Hermes 签名兼容；P1/P2 unsupported stub（register_hook/register_cli_command/register_platform 等）记录到 unsupported_capabilities 但不崩扫描。
- PluginKind：插件类型枚举，standalone（独立工具插件，本期唯一支持扫描注册的 kind）/backend/exclusive/platform/model_provider（后四类本期仅识别不执行）。
- PluginSource：插件来源枚举，bundled（出厂 seeds）/user（PLUGINS_ROOT 用户目录）/project（项目内插件，需 enable_project_plugins）/entry_point（Python entry points，需 enable_plugin_entrypoints）。
- PluginToolRegistration：Application 层工具注册值对象，封装 plugin 暴露的工具定义（name/toolset/schema/handler/check_fn/requires_env/is_async/description/emoji/override）+ plugin_config/secret_config 缓存，供 PluginToolExecutor 执行时使用。
- PluginToolExecutor：Application 层 ToolExecutor 实现，将 plugin 工具调用委托给 PluginService.call_tool；在 main.py 中通过 `CompositeToolExecutor(routes, fallback=McpToolExecutor)` 显式路由 plugin 工具名，MCP fallback 不回归。
- secret_refs：Plugin 的 secret 字段占位标记，`{field: bool}` 形式，由 SQLitePluginRegistry 查询 plugin_secrets 表填充；API 响应永不返回 secret 明文，前端据此显示"已设置，留空保持不变"提示。
- ACP (Agent Client Protocol)：Zed Industries 提出的开放协议，基于 JSON-RPC 2.0 over stdio，让编辑器/IDE（VsCode/Zed 等）以 stdio 方式接入外部 Agent runtime。N-Agent 通过 `agent-client-protocol` PyPI 包实现服务端，stdout 承载 JSON-RPC 帧，stderr 走日志。
- ACP stdio 服务端：N-Agent 内置的 ACP 服务端入口，由 `n-agent acp` 命令启动（无 flag 进入 JSON-RPC 主循环，`--check` 验证依赖可导入，`--setup` 输出 provider 配置提示）。VsCode ACP Client 通过 `docker exec -i n-agent-n-agent-1 n-agent acp` 或 `kubectl exec -i <pod> -- n-agent acp` 接入容器内 Agent。
- NAgentACPAgent：`app/interfaces/cli/commands/acp/agent.py` 中实现 `acp.Agent` 的类，提供 13 个 SDK 方法（initialize/authenticate/session/new/prompt/load/list/fork/cancel/close_session 等）；ACP 协议生命周期留在此 adapter，`session/prompt` 用户消息转换为 InteractionMessage 后经 GatewayService → ChatCompletionService。
- 路径映射 (path mapping)：ACP cwd 来自宿主/editor，N-Agent 文件工具运行在容器/Pod，必须通过 `N_AGENT_ACP_HOST_WORKSPACE_ROOT` + `N_AGENT_ACP_CONTAINER_WORKSPACE_ROOT` 环境变量配置映射。映射规则：(1) cwd 在 host root 下时替换前缀为 container root；(2) cwd 已在 container root 下时原样使用；(3) cwd 为空时使用 container root；(4) cwd 不可映射时 `session/new` 拒绝并返回协议错误，不回退到 `Path.cwd()`。
- ApprovalDecider：Domain 可调用端口（`app/domain/tool.py`），输入 `ApprovalRequest`、返回同步或异步 `ApprovalDecision`；ACP 桥接选项 ID 为 allow_once、allow_session、reject_once。
- ApprovalRequest / ApprovalDecision：审批输入/输出值对象。ApprovalRequest 携带 session_id、tool_name、tool_call_id、arguments 等上下文；ApprovalDecision 携带 allowed 与 scope（once/session/deny）。
- ACP session metadata：`sessions.acp_metadata_json` 列存储的 ACP 会话元数据（host cwd、container cwd、ACP session id 等映射信息），仅 `source="acp"` 的会话写入；ACP 服务端在 `session/new` 时写入，`session/load` 时读取复用，用于在 ACP 客户端重连后恢复会话上下文与 cwd 映射。
- _BenignMethodNotFoundFilter：`app/interfaces/cli/commands/acp/command.py` 中的 logging.Filter，抑制 ACP SDK 通过 `logging.exception` 记录的 benign method-not-found 错误（code=-32601 且 method 为 `_ping`/`_health`/`ping`/`health`），避免 stderr 被客户端探测噪声污染；非 benign 方法（如 `session/prompt`）的异常仍透传。
- supports_vision：`ProviderConfig` 的字段，表示 provider 是否支持图片输入。openai-compatible 类型默认 True，anthropic 类型默认 False。Dashboard provider 表单可在线编辑；`AgentGraphRunner.call_llm` 在 vision preflight 中检查此字段，不支持 vision 时遇到 image content 直接返回友好 assistant 消息而非调用 provider（避免 HTTP 500）。
- vision_analyze：内置 safe 工具（toolset=vision），由 `VisionAnalyzeToolExecutor`（`app/application/vision_tool_executor.py`）实现。LLM 调用时传入 `image_url` 和 `question`，executor 校验 URL 后调用 active provider.chat（无工具、无递归）分析图片并返回文本结果；不支持 vision 或 URL 非法时返回 ERROR 友好提示。
- data URL：RFC 2397 定义的 inline 资源格式 `data:{media_type};base64,{data}`，N-Agent 多模态对话中用于在消息 content 内嵌入图片。`content_utils.validate_image_url` 对 data URL 做 MIME 白名单（image/png|jpeg|gif|webp）+ 20MB 上限 + base64 合法性校验；Anthropic provider 将 data URL 转换为 Anthropic 风格 `{"type":"image","source":{"type":"base64","media_type":...,"data":...}}` 块，http(s) image_url 在 Anthropic 路径抛 ValueError（保守路径，无 SDK 依赖）。
- image_url content array：OpenAI Chat Completions 风格的多模态用户消息 content，格式为 `[{type:"text",text:...},{type:"image_url",image_url:{url:...}}]`。N-Agent 各入口（OpenAI HTTP API、Dashboard、飞书 IM、ACP）归一化为该格式后进入 provider；`content_utils.normalize_content` 负责归一化，`extract_text` 负责从数组中提取纯文本供摘要/标题使用。
- content_utils：`app/utils/content_utils.py`，多模态内容共享工具模块，提供 validate_image_url/parse_data_url/normalize_content/extract_text/has_image_part/prepend_text_part。被 Domain（不直接依赖，通过 Application 间接消费）、Application（chat_service/agent_graph/heuristic_summarizer/vision_tool_executor）、Infrastructure（anthropic_provider）、Interfaces（openai_compatible/dashboard 路由、ACP event_bridge）各层共享，不依赖任何外层模块。

- terminal：内置 safe 工具（toolset=sandbox），由 `TerminalToolExecutor`（`app/application/terminal_tool_executor.py`）实现。LLM 直接调用 shell 命令，命令在 session 级 sandbox 容器内执行（DockerSandbox 用 `docker exec`，LocalSandbox 用 `sh -c`），与 execute_code 共享同一 session sandbox 和 scratch。非零退出码仍为 SUCCESS（shell 语义 — 命令已执行，只是失败）；仅 timeout 返回 TIMEOUT、spawn 失败返回 ERROR。workdir 按 backend 校验：Docker 仅允许 /scratch 和 /workspace 前缀（posixpath.normpath），Local 仅允许 scratch_root/workspace_root 内（Path.resolve）。无危险命令审批机制 — sandbox 本身是安全边界。
- host_terminal：宿主执行工具（`source_type=AGENT`、`toolset=host`、`risk_level=SAFE`、`managed=false`），与 `terminal`、`execute_code` 同级；只执行 Host Terminal Policy 白名单中的精确命令或 SHA-256 匹配的 Skill 脚本，不是 Sandbox 的 host backend。
- Host Terminal Bridge：运行在宿主机、只监听 loopback 的最小执行服务；校验 token 和自身加载的 Policy，使用已验证字节的私有快照以 argv 启动进程，不走 shell。
- Host Terminal Policy：容器与宿主 Bridge 独立加载的同一份执行授权配置，按命令目标/argv 或 `skill_name + relative_path + SHA-256` 进行 fail-closed 校验；合法 reload 发布不可变快照，非法 reload 保留 last-good 快照。
- shell 语义（shell semantics）：terminal 工具的状态映射约定。非零退出码表示命令已执行但失败（如 `exit 7`、command-not-found returncode=127），映射为 `SandboxStatus.SUCCESS`；仅命令执行超时映射为 `TIMEOUT`（returncode=124），仅 spawn/write 失败映射为 `ERROR`（returncode=-1）。与 execute_code 的 Python 语义不同（非零退出码在 Python 中为 ERROR）。
- CanonicalUsage：Domain 值对象（`app/domain/usage.py`），归一化后的 token 五桶（input/output/cache_read/cache_write/reasoning），由 `UsageService.normalize_usage` 从 Provider 原始 usage dict（OpenAI/Anthropic 不同键名）转换而来；`prompt_tokens` = input + cache_read、`total_tokens` = 五桶之和为派生属性。raw_usage 保留原始 dict。
- UsageCost：Domain 值对象（`app/domain/usage.py`），成本估算结果，含 amount_usd（Decimal str）、status（`estimated`/`unknown`）、pricing_version。`estimated` 表示价格表命中，`unknown` 表示未命中且 amount_usd=0。
- PricingEntry：Domain 值对象（`app/domain/usage.py`），模型定价条目，含 model_pattern、provider、input/output/cache_read/cache_write 各项 cost_per_million（Decimal）、pricing_version、source_url。Infrastructure 的 InMemoryPricingProvider 按 model 前缀最长匹配查表。
- SessionUsageStats：Domain 值对象（`app/domain/usage.py`），会话级累计统计，对应 sessions 表新增的 token/cost/api_call_count 列。
- ContextBreakdown：Domain 值对象（`app/domain/usage.py`），上下文分类 token，含 system_prompt/tool_definitions/memory/conversation 四桶 + 派生 total。由 ContextBreakdownCalculator 计算，用于观测页展示当前 context 占用。
- UsageRecorder：Domain 端口（`app/domain/usage.py`），定义 record_call/get_session_stats/list_records/record_compression/list_compressions 接口。Infrastructure 的 SqliteUsageRecorder 实现，与 sessions.db 共享 path，sessions 表迁移幂等。
- PricingProvider：Domain 端口（`app/domain/usage.py`），定义 `get_pricing(model, provider) -> PricingEntry | None`。Infrastructure 的 InMemoryPricingProvider 实现硬编码 9 款主流模型定价。
- ContextBreakdownCalculator：Domain 端口（`app/domain/usage.py`），定义 `compute(system_prompt, tool_definitions, messages, external_memory_block) -> ContextBreakdown`。Infrastructure 的 ContextBreakdownCalculatorImpl 实现，复用 ContextCompressor 的 ~4 chars/token 估算逻辑。
- usage_records：SQLite 表，持久化每次 LLM 调用的 usage 明细，由 SqliteUsageRecorder.record_call 写入；sessions 表同步累加 token/cost/api_call_count。
- compression_stats：SQLite 表，持久化上下文压缩前后的 token 对比，由 SqliteUsageRecorder.record_compression 写入；`tokens_saved` 用 `max(before-after, 0)` clamp 防止负值。

## Dashboard 顶导菜单术语

- 顶导（topnav）：Dashboard 可选的子域横向导航组件（`topnav.js` NAGENT.topnav），按子域配置（`topnavConfig`）渲染当前子域的横向关注点（如任务子域：管理/观测）。顶导只展示当前子域 items，不跨子域；左导整体不变。样式参考 odin-fe（50px 高、选中主色+加粗无 border-bottom、溢出平移+箭头）。顶导点击经 `onActivate` 回调交由 `navigatePath`，不直接 pushState。
- topnavConfig：`management-navigation.js` 中按子域（tab key）配置顶导 items 的映射表，每项含 tab/path/label/concern/scope/topnavParent；与 `tabConfig`（左导配置）和 `routeConfig`（多路径路由表）分离，未配置的子域不渲染顶导。
- routeConfig：`management-navigation.js` 中独立于 `tabConfig` 的路由表，支持多路径映射到同一 renderer（如 `/tasks/observations` 与 `/observations/tasks` 均映射到 `tasks-observations`）；`buildRouteByPath` 校验后生成 routeByPath 映射，`resolveRoute` 优先查此表。
- sidebarOverride：`management-navigation.js` 中的路径->sidebarTab 覆盖映射（如 `/observations/tasks`->`observations-sessions`），使左导入口下钻到 scoped 路由时左导高亮对齐原入口而非"任务"项。

## Task 审批术语

- revise（Task approval 第三决策）：Task 意图审批中 approve/reject 之外的第三决策。用户通过 `revise_task` 工具、`/task revise` CLI 子命令或 POST `/chat/tasks/{id}/revise` 路由下达修订指示（note 必填），把 WAITING_APPROVAL 任务重新入队（`Task.revise()`：WAITING_APPROVAL->QUEUED，状态机合法转换集合不变），worker 下次 run 在 `build_worker_context` 决策段看到 `change_revised` 事件与修订 note，可遵循修订路径或提出新提案。与 reject 的区别：reject 表示不同意原提案且不期望 worker 继续，revise 表示期望 worker 带着修订指示继续执行。三决策经 TaskService 统一 `_resolve_proposal` helper + Registry 单事务原子 `resolve_proposal` 端口编排，并发唯一成功无孤立事件。

## Policy Mesh 治理术语

- Policy Mesh：N-Agent 运行时治理架构，由 15 个独立领域 Policy + Shared Kernel + RunPolicySnapshot + 审计通道组成；每个 Policy 治理一个维度，Application Service 在外部调用前封口执行。
- RunPolicySnapshot：不可变 frozen dataclass（`app/application/policy_snapshot.py`），携带 10 个 typed config + IngressFacts（run_id/session_id/execution_mode/trusted_claims）；由 RunPolicySnapshotFactory 从 PolicyProfileProvider 构造；不持有任何 mutable runtime state。
- RunPolicySnapshotFactory：Application 层工厂（`app/application/policy_snapshot.py`），从 PolicyProfileProvider 解析 profile 并构造 RunPolicySnapshot；不持有 Settings 引用。
- IngressFacts：RunPolicySnapshot 中的不可变运行时入口事实，含 run_id、session_id、execution_mode、actor_id、trusted_claims。
- BudgetPolicy / BudgetService：Domain 预算策略 + Application 服务；reserve(settle/release) 三段式预算生命周期，覆盖 LLM_CALL / TOOL_CALL / SANDBOX_RESOURCE / WALL_TIME 四种 reserve kind。
- RunBudgetAccount：BudgetService 内部 per-run mutable 账户，asyncio.Lock 序列化所有 reserve/settle/release，保证 no-oversell。
- InformationFlowPolicy / InformationFlowService：Domain 信息流策略 + Application 服务；评估 content 对 ReleaseTarget 的释放决策（allow+redaction / deny），覆盖 LLM_PAYLOAD_LOG / USAGE_RETENTION / CLIENT_RESPONSE / TOOL_MCP_PLUGIN / stream。
- InformationFlowStreamGuard：InformationFlowService 创建的增量流脱敏守卫，lookbehind buffer 防止 secret 跨 chunk 泄漏。
- SandboxExecutionGrant：SandboxPolicy authorize 返回的不可变授权令牌，含 timeout/cpus/memory/callbacks 限制；SandboxToolExecutor 持有 grant 执行。
- MemoryPolicy / RuntimeMemoryService：Domain 记忆策略 + Application 非绕过 facade；MemoryPolicy 评估 read/write/sync/external 操作，deny -> store 不调用。
- GatewayPolicy：Domain 出站策略，评估消息目标（origin/dashboard/silent）与内容。
- SchedulePolicy：Domain 调度策略，评估 cron 安全 + claim 原子性 + 投递。
- TurnPolicy / EndReason：Domain 轮次策略 + 结束原因枚举，控制 AgentGraph 迭代上限与路由。
- ContextPolicy：Domain 上下文策略，评估压缩阈值与保护段。
- LLMPolicy / LLMConfig：Domain LLM 策略 + 配置，控制 fallback 与 vision preflight。
- PolicyAuditService / PolicyAuditSink：Application 审计服务 + Domain 审计 sink Protocol；生产实现 LoggingPolicyAuditSink 输出 JSON 日志。
- PolicyAuditEvent：Domain 不可变审计事件，含 policy/version/decision_kind/reason/run_id/session_id/outcome；无 raw prompt/secret/tool arguments 字段。
- PolicyDecisionKind：审计决策类型枚举（admission/plan/selection/allocation）。
- 封口（sealing）：Policy Mesh 的执行模式，指 Application Service 在调用外部资源（LLM/Tool/Sandbox/Memory/Gateway）前必须经过 Policy 评估，deny -> 外部资源不被调用。

## Skill 自进化术语

- Skill 自进化（Skill Self-Evolution）：Agent 自主从对话中学习非平凡流程并沉淀为可复用 Skill（SKILL.md），形成"用-学-进化"闭环；参考 HermesAgent。
- skill_manage：Skill 写入工具，Agent 通过它 create/patch/edit/delete/write_file/remove_file 管理 Skill；SAFE 工具，toolset=skills。
- SkillPolicy：第 12 个领域 Policy，治理 Skill 写入（deny > require_approval > allow）；后台 review 只能改 agent-owned Skill，foreground 不可删 seed/pinned，read-before-write，write_approval staged。
- SkillSource：Skill 所有者类型枚举（seed 出厂模板/agent Agent 创建/user 用户创建），与写入 origin 正交。
- SkillWriteOrigin：写入来源枚举（foreground 前台/background_review 后台自进化），决定写权限边界。
- Background Self-Improvement Review：turn 结束后按 nudge 计数 fork 受限 Agent（工具白名单 skills+memory），review 对话自主 patch/create Skill；fire-and-forget，失败不影响主 turn。
- SkillEvolutionService：Background Review 的 Application 编排服务，maybe_trigger（nudge+并发）+ run_background_review（fork chat + 注入 origin）。
- provenance origin 防伪造：origin 经 ToolExecutionContext.trusted_metadata["skill_write_origin"] 注入（服务端注入），OpenAI HTTP 直连客户端无法伪造，防绕过 SkillPolicy。
- write_approval staged gate：skills_write_approval 开启时，skill_manage 写入被 staged 到 SkillPendingStore 而非直接落盘，经 Dashboard/CLI approve 后 replay（approved_replay 绕过 require_approval 但不绕过 deny）。
- read-before-write guard：background_review 修改既有 Skill 前必须先 skill_view 读取（exact_target_loaded），SkillPolicy 否则 deny。
- SkillUsage telemetry：Skill 使用遥测（created_by/use_count/view_count/patch_count/state/pinned/时间戳），存 SQLite skill_usage 表。
- archive-not-delete：delete/remove_file 默认移到 .archive/ 而非物理删除，可恢复。
- SkillBackupStore：Skill 目录 tar.gz 快照 + rollback，写入前 backup（fail-closed，backup 失败拒绝写入）。

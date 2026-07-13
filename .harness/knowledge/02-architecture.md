<!-- SUMMARY: N-Agent 当前阶段与后续完整 Agent 能力的 DDD 架构边界、依赖方向和核心模块原则，含飞书与 CLI ToolPolicy 审批的 Application 状态归属与 Interfaces 协议边界 -->
# 架构与模块边界

## 架构定位

本项目当前阶段是后续持续迭代到完整 Agent 能力的架构基线，不是一次性 demo。当前阶段只实现既定验收范围，但领域模型、端口和模块边界必须支持后续扩展 Provider、工具生态、长期 Memory、权限审批、多 Agent、自动化任务、可观测性和部署运行环境。

## 分层

项目严格遵循领域驱动设计 DDD，采用外层依赖内层的方向：Interfaces -> Application -> Domain。Infrastructure 只实现 Domain 定义的端口，并在应用启动时注入。

- Domain 层：核心子域包括 TurnLoop、Context、LLM、Memory、Tool，支撑子域包括 Skill、Knowledge、MCP、Plugin、Platform、Gateway、Schedule、Sandbox、Usage/Observation；各子域定义自己的模型、值对象和端口协议。`app/domain/policy.py` 是 Domain Shared Kernel，只统一 `Policy` Protocol、`PolicyOutcome`、`PolicyDecision`；具体规则归各业务领域的 `XPolicy`。Storage 与 Model Provider 通过领域端口形成外部边界。详细 DDD 领域模型见 `.harness/knowledge/06-domain-model.md`。
- Application 层：编排用例和 Agent Runtime。LangGraph 属于本层，只负责状态图和运行流程编排。
- Infrastructure 层：实现外部依赖细节，包括 OpenAI-compatible Provider、SQLite store、内置工具 handler、Knowledge HTTP adapter、配置加载等。
- Interfaces 层：实现 FastAPI、OpenAI-compatible API、Dashboard 和协议转换。

## 模块边界

- Domain 不依赖 FastAPI、LangGraph、SQLite、OpenAI SDK 或任何 Infrastructure 具体实现。
- Application 依赖 Domain 端口和 LangGraph，不 import Infrastructure 具体类。
- Infrastructure 可以依赖 Domain 端口并实现它们，不能反向要求 Domain 了解具体存储、模型 SDK 或工具 handler。
- Interfaces 只调用 Application 用例，只做请求/响应、SSE、错误映射和 Dashboard 协议转换。
- Interfaces 不直接访问 SQLite，不直接执行工具 handler，不承载工具权限、Agent Loop、摘要策略等业务规则。
- 应用启动入口负责依赖组装，将 Infrastructure 实现注入 Application 服务。

## 核心模块

- Agent Runtime：内部运行机制，负责准备上下文、调用 LLM、执行工具、更新 Memory、判断结束条件。LangGraph 节点顺序为 `prepare_context -> call_llm -> execute_tools -> update_memory -> call_llm/finalize`；`prepare_context` 内部加载历史并按需压缩短期记忆，token 估算超阈值时按增量三段式（head 保护 + 中段 LLM 摘要 + tail 保护，`_find_latest_context_summary` 定位上次摘要切点）压缩历史，cooldown 防抖；`update_memory` 不再生成 summary，`pre_compress_all` 仅在实际压缩前调用提取 rescued_context。运行流程可使用 LangGraph 表达，但领域状态和规则不能被 LangGraph 类型污染。
- LLM Adapter：模型 Provider 端口，屏蔽 OpenAI-compatible、Claude、Ollama、OpenRouter 等 Provider 差异。运行时由 Application 层 `ActiveProviderHolder` 适配，结合 SQLite 持久化的 `ProviderRegistry`（多 Provider 注册表 + 单一 active）实现 Dashboard 在线 CRUD 与热切换；下游用例只依赖 Domain LLMProvider 端口，不感知 holder 与 registry 的存在。
- Tool Domain：`ToolDefinition` 描述工具能力且不包含 handler，`ToolExecutor` 是执行 SPI；`ToolPolicy` 负责定义校验、模型暴露、执行允许/拒绝/需审批和一次授权规则。Application 的 `ToolService` 是强制执行边界，执行前始终按当前定义复判；Agent Runtime 只根据决策编排审批。工具必须同时存在服务端定义和执行路由才真正可用，具体 executor 通过 Infrastructure 组合路由分发。MCP 远端工具通过本地动态 `ToolDefinition` 暴露，运行时由 Application 层 `McpToolExecutor` 薄适配 `McpService`，再由 Infrastructure MCP client 访问远端站点。
- MCP Sites：MCP 站点管理采用“配置注册表 + 探测优先 + 动态工具面”模式。Domain 定义站点、工具映射和 registry 端口；Application McpService 负责 CRUD、探测、刷新、动态工具定义和调用解析；Infrastructure 实现 SQLite registry 与 MCP SDK client，并支持 streamable_http、SSE 和 stdio 三类传输；Interfaces 只提供 Dashboard API 和静态页面交互。stdio 站点保存 command/args/env，执行时通过 argv 启动本地 MCP server，不走 shell。
- Knowledge Retrieval：N-Agent 定义知识检索 SPI，通过 safe tool `search_knowledge` 消费多个已注册 KB 后端。Domain 只定义 KB 值对象、注册表端口和检索端口；Application 的 KnowledgeService 编排 KB CRUD、probe、search 和动态工具定义；Infrastructure 用 SQLite registry 保存 KB 配置，用 HTTP adapter 适配 N-KB/Ragflow 协议；Interfaces 提供 Dashboard KB 管理 API 和前端页面。N-KB、Ragflow 都只是后端协议类型，不嵌入 N-Agent 领域模型。
- Memory/Context：通过 MemoryStore 与 Summarizer 端口访问，会话、消息、工具调用、任务状态、摘要和会话级外部记忆配置的持久化细节属于 Infrastructure。Chat Session 的外部记忆 profile 必须首轮后锁定，避免同一会话内切换文件记忆导致 system prompt 前缀变化、LLM prefix cache 失效和历史语义混用。Context 短期记忆压缩通过 `ContextEngine` 端口（`app/domain/context.py`）定义，Infrastructure 的 `ContextCompressor`（`app/infrastructure/context/context_compressor.py`）实现增量压缩（`_find_latest_context_summary` 定位上次摘要切点，middle 只取新增消息）；摘要消息通过 `is_summary=1` 标记，被摘要吸收的原消息通过 `is_summarized=1` 标记。压缩阶段依次调用 `append_summary_message` 追加新摘要、`mark_messages_summarized` 标记 middle 原消息、`save_summary` 更新滚动摘要，加载 Provider Context 时只选最新摘要并过滤已被吸收的原消息。
- OpenAI-compatible API：对外兼容 Open-WebUI 的协议层，不等同于内部 Agent 模型。
- Platform Aggregate / Interaction Gateway：`Platform` 面向飞书、钉钉、企微等外部消息平台；Domain 定义 PlatformRegistry 与 PlatformLifecycle 端口，Application 的 PlatformService 组合平台 descriptor、lifecycle、Gateway 会话统计并向 Dashboard 提供只读视图。CLI/TUI 终端聊天仍可通过 GatewayService 标准化为 InteractionMessage，但只作为 `source=cli` 的入口来源，不注册进 PlatformRegistry，也不出现在 Dashboard 平台页。飞书 IM 等平台入口复用 ChatCompletionService、SessionService、ToolService 和 MemoryStore；Gateway 破坏性命令确认由 Application 层 pending confirmation 管理，ToolPolicy 的“本会话信任”由 Application 的 `GatewayToolApprovalService` 按 session/actor/tool 保存，最终执行仍由 `ToolService` 复判。飞书使用 interactive card 作为两类审批的展示和回调通道；Interfaces 的 `FeishuToolApprovalBridge` 仅保存短生命周期协议 pending/Future 和服务端 card message id，不拥有授权业务状态。飞书使用长连接接收事件，平台适配只做协议解析、消息类型过滤、卡片渲染、回调路由和消息收发，FeishuImAdapter 同时实现 PlatformLifecycle，用 start() 进入 connected/正常返回 disconnected/异常 fatal 的事件驱动状态。CLI/TUI 终端在 TTY REPL 中使用进程内 Future + 精确命令路由（`/confirm once`/`/confirm trust`/`/cancel`）作为工具审批通道，Interfaces 的 `CliToolApprovalBridge` 同样只保存短生命周期协议 pending/Future，不拥有授权业务状态；非 TTY/单次消息/stdin pipe 不注入 decider，fail-closed。
- Chat Dashboard：调试和演示入口，查看会话、流式输出、工具调用、摘要和任务状态，不替代 Open-WebUI。
- Skill 子系统：独立 DDD 子域。Domain 定义 Skill 模型与 SkillRegistry 端口；Application SkillService 负责扫描、列表、view 渲染、启停切换、宏预处理（`${HERMES_SKILL_DIR}` / `${HERMES_SESSION_ID}`）和 safe 工具 (`skills_list`、`skill_view`) 的动态定义；Infrastructure 提供 SkillFileLoader 扫描本地 SKILLS_ROOT（默认 `/workspace/skills`，复用 path_security 防遍历/symlink）与 SQLite SkillRegistry 元数据持久化（启用状态在重扫间保留）；Interfaces 提供 Dashboard `/chat/skills*` 管理 API + 前端 skills.js 与 CLI `n-agent skill list/view`。LLM 通过 safe 工具自助按需读取，不暴露 `skill_run`。仓库携带 `app/infrastructure/skill/seeds/` 出厂模板（含 `n-agent` SKILL.md），main.py 启动时通过 `seed_default_skills` 幂等拷贝到 SKILLS_ROOT，不覆盖已有用户副本。
- Plugin 子系统：独立 DDD 子域，遵循 Hermes plugin 模式（`plugin.yaml` + `register(ctx)`）支持零成本移植开源插件生态。Domain 定义 Plugin/PluginManifest/PluginKind/PluginSource/PluginScanStatus 模型与 PluginRegistry 端口（async 方法）；Application 的 PluginService 编排扫描/列表/启停/config CRUD/动态工具面，PluginContext 提供 Hermes 兼容的 `register_tool` 签名 + P1/P2 unsupported stub（不崩扫描），PluginToolExecutor 将 plugin 工具调用委托给 PluginService.call_tool；Infrastructure 的 SQLitePluginRegistry 持久化 plugins + plugin_secrets 两表（独立 secret 存储，FK ON DELETE CASCADE），PluginFileLoader 扫描 bundled/user/project 三源 + entry_points 开关，`n_agent_plugins` 命名空间稳定包 import；Interfaces 提供 Dashboard `/chat/plugins*` 管理 API（refresh 路由在 `{key:path}` catch-all 之前）+ 前端 plugin.js（config_schema secret 字段路由到 secret_updates）+ CLI `n-agent plugin list/view`。Plugin 工具通过 `ToolService.set_dynamic_definitions("plugin", ...)` 暴露给 LLM（`source_type=PLUGIN`），`CompositeToolExecutor.routes` 显式路由 plugin 工具名，MCP fallback 不回归。仓库携带 `app/infrastructure/plugin/seeds/hello/` 出厂示例（standalone kind，provides_tools=[hello]），main.py 启动时通过 `seed_default_plugins` 幂等拷贝到 PLUGINS_ROOT。
- Managed Tools 授权：Domain `ToolDefinition.managed: bool` 标记需服务端授权才能执行的工具；`ToolExecutionContext` 携带 `session_id` / `metadata`(untrusted) / `trusted_metadata`(trusted) / `execution_context_mode` / `permitted_managed_tools`。`ChatCompletionService` 在每次 complete 调用时构造 context，仅当 mode=realtime 且 `trusted_metadata.gateway.platform` 为合法 Gateway 来源（当前 feishu）时把 `manage_schedule` 加入 permitted set；OpenAI HTTP 直连客户端伪造 metadata 不进入 trusted_metadata 字段，故无法获得授权。`ToolPolicy` 对未获授权的 managed 工具返回 `REQUIRE_APPROVAL`，`ToolService` 在未取得有效一次授权时拒绝执行；unattended 模式使用 `SAFE_ONLY` 暴露策略过滤 AGENT 来源工具，避免调度器递归。
- Schedule 自然语言管理：飞书 IM 用户用自然语言增加/修改/查看/暂停/恢复/运行定时任务时，由 Agent 调用 `manage_schedule` / `schedule_query`（前者为 managed CONFIRM 工具，后者为 SAFE）；删除仍走 `/schedule remove` 确认卡，preflight 时把当前 trusted_metadata 写入 `GatewayConfirmationRequest.trusted_metadata`，handle_confirmation 还原后由 schedule_service 校验 task.origin 与 trusted_metadata 的 receive_id/receive_id_type/thread_id 一致再执行删除，跨 origin/会话统一返回"任务不存在"，不暴露存在性差异。系统提示词只声明"先 skill_view('n-agent')"等 ≤4 行 guidance，cron 语法等长知识沉淀在 SKILL.md。
- 执行沙盒 Sandbox：受控 Python 代码执行与 shell 命令执行子域。Domain 定义 `Sandbox` / `SandboxCallbackTool` / `SandboxCallbackToolRegistry` / `ReleasedSandboxRegistry` / `SandboxExecutionHistoryRegistry` / `SearchProvider` 端口与 `SandboxExecutionRequest/Result` / `SandboxExecResult` / `SandboxExecutionHistoryEntry` 值对象；`Sandbox` 端口含 `execute`（Python 代码执行，返回 `SandboxExecutionResult`）和 `exec_command`（shell 命令执行，返回 `SandboxExecResult`，非零退出码仍为 SUCCESS — shell 语义）；Application 的 `SandboxToolExecutor` 直接执行 execute_code（SAFE 工具，无 confirmation gate，sandbox 本身是安全边界），`_run_sandbox` 在会话锁内 per-call 构造 SandboxExecutionRequest，sandbox 异常被捕获返回 `ToolResult(ERROR)` 不打断 AgentGraph，成功/失败/超时/异常全部写入 history_registry 审计；`TerminalToolExecutor` 直接执行 `terminal` 工具（SAFE，LLM 调 shell 命令），不继承 SandboxToolExecutor（无 callback/staging 语义），在会话锁内调 `sandbox.exec_command`，workdir 按 backend 校验（Docker 仅允许 /scratch 和 /workspace 前缀，Local 仅允许 scratch_root/workspace_root 内），与 execute_code 共享同一 session 级 sandbox 容器和 scratch；`SandboxDashboardService` 提供只读视图（config/active/released/history）与 release 管控；Infrastructure 的 `LocalSandbox`（subprocess+UDS，trusted-dev only）与 `DockerSandbox`（docker CLI+UDS，生产）统一通过 UDS RPC（`SandboxRpcServer` + `stub_generator` 纯客户端 stub），`SandboxManager` 管理会话锁/缓存/idle reaper/scratch 独立根/`_releasing` 集合防 release/execute 竞态/`default_workdir` 返回 session 默认 workdir，并把废弃沙盒历史写入 SQLite `ReleasedSandboxRegistry`；回调工具（read_file/write_file/search_files/patch/web_extract/web_search）经 `_resolve_under_workspace` 锚定 workspace_root 防 `..` 逃逸；Interfaces 的 `sandbox_routes.py` 提供 `/chat/sandbox/*` Dashboard API（6 端点，pending/retryable/confirmed 相关端点已废弃）。安全边界由 sandbox 容器保证：生产 Docker 后端保持 workspace :ro + scratch :rw 边界 + callback allowlist + max_tool_calls + timeout，execute_code 写操作只能经父进程回调工具执行；terminal 写操作受 workdir 校验约束（/workspace 只读，/scratch 可写）；Dashboard Chat / OpenAI HTTP / CLI / 飞书 等所有通道统一走 executor 直执行。
- ACP stdio 服务端：N-Agent 内置 ACP（Agent Client Protocol）stdio JSON-RPC 服务端，支持 VsCode/Zed 等 ACP 兼容客户端通过 `docker exec -i n-agent-n-agent-1 n-agent acp` 或 `kubectl exec -i <pod> -- n-agent acp` 接入容器内 Agent。Domain 扩展 `ApprovalDecider` 端口与 `ApprovalRequest`/`ApprovalDecision` 值对象（工具调用审批决策），`ConversationSession.acp_metadata` 字段（ACP 会话元数据）；Application 的 `ChatCompletionInput` 增加可选 `approval_decider` 与 `allowed_confirm_tools_override` 字段，`GatewayService.handle_message_stream` 支持调用方传入 model/options/trusted_metadata/approval override，供 ACP 的 `session/prompt` 在复用 Gateway 会话映射、命令分流、幂等与 trusted metadata 组装后进入 ChatCompletionService；`AgentGraphRunner` 支持 task state interrupt 注册与查询（cancel 时通过 `asyncio.CancelledError` 中断 prompt）。Interfaces 的 `app/interfaces/cli/commands/acp/` package 实现 ACP stdio 服务端（`command.py` 入口与 stdout 纯净保证、`agent.py` `NAgentACPAgent(acp.Agent)` 13 个 SDK 方法、`path_mapping.py` host↔container cwd 映射、`session_bridge.py` ACP session↔N-Agent session 桥、`event_bridge.py` ChatEvent→ACP session update 桥与 history replay、`permission_bridge.py` ApprovalDecider→ACP PermissionOption 桥、`auth.py` provider Token 认证桥）。stdout 仅承载 JSON-RPC 帧，所有日志/诊断走 stderr；host cwd 通过 `N_AGENT_ACP_HOST_WORKSPACE_ROOT` + `N_AGENT_ACP_CONTAINER_WORKSPACE_ROOT` 环境变量映射到容器路径，不可映射时 `session/new` 拒绝并返回协议错误，不回退到 `Path.cwd()`。ACP 会话 `source="acp"`，元数据持久化到 `sessions.acp_metadata_json` 列；initialize/authenticate/session/new/load/list/fork/cancel/close 等 ACP 协议生命周期保留在 ACP adapter，只有 `session/prompt` 用户消息经 GatewayService → ChatCompletionService。

## 演进边界

后续完整 Agent 能力应在现有边界内迭代，不推倒重来。优先演进方向包括：

1. 受控 Shell、文件写入、patch、审批流和 workspace 安全。
2. Toolset、MCP、工具可用性检查、schema sanitizer、工具输出限流和工具结果持久化。
3. Context compressor、session search、长期 Memory、摘要压缩和任务恢复。
4. 多 Provider、Provider fallback、模型能力检测、usage/cost 统计。
5. CLI/TUI 和更完整 Dashboard。
6. Cron、后台任务、通知投递和失败重试。
7. Skills、自我改进 loop、用户画像和经验沉淀。
8. 子 Agent、并行执行、delegate 和结果汇总。
9. 多平台 Messaging Gateway、远程运行环境和部署安装体系。

当前阶段实现计划不得提前实现上述完整能力；只保留清晰扩展点。

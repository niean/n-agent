<!-- SUMMARY: N-Agent 的领域数据模型、配置模型、SQLite schema、OpenAI-compatible 协议边界和 Docker Compose 数据挂载边界 -->
# 数据与类型边界

## 领域模型

完整 DDD 子域划分、聚合实体和应用服务调用流见 `.harness/knowledge/06-domain-model.md`。

`AgentState`（`app/domain/agent.py`）：Agent Runtime 的运行状态，字段包括 session_id、input_messages、working_messages、pending_tool_calls、tool_results、summary、run_status、iteration_count、error、final_message、finish_reason。该模型属于 Domain，不包含 LangGraph 类型。

`AgentRun`（`app/domain/agent.py`）：一次 Agent 运行的领域对象，包含 session_id、input_messages、id、status、iteration_count、error、end_reason。

`ConversationSession`（`app/domain/session.py`）：会话聚合根，字段包括 id、title、source、external_memory_enabled、created_at、updated_at。external_memory_enabled 是会话级外部记忆 profile 锁定值，首轮消息前可由 Chat/Dashboard 选择，首轮后不可变；未锁定的新会话为 null，发送首轮时写入规范化列表。

`ConversationMessage`（`app/domain/session.py`）：会话消息值对象，字段包括 id、role、content、tool_call_id、name、created_at。role 支持 user、assistant、tool 等 Provider 消息角色。

`ToolCall`（`app/domain/session.py`）：工具调用记录，字段包括 id、session_id、message_id、tool_name、arguments、result、status、duration_ms、created_at。

`TaskState`（`app/domain/session.py`）：任务状态，字段包括 session_id、status、iteration_count、last_error、updated_at。

`Summary`（`app/domain/session.py`）：摘要记录，字段包括 session_id、summary、source_message_id、updated_at。

`Platform` / `PlatformKind` / `PlatformDescriptor` / `PlatformLifecycle` / `PlatformRegistry`（`app/domain/platform.py`）：平台聚合的领域枚举、描述和生命周期端口，用于表达 CLI、飞书、钉钉、企微等交互平台及其 configured/connected/disconnected/fatal 状态，不包含具体 SDK、HTTP 或长连接对象。

`GatewaySessionKey` / `InteractionMessage` / `GatewayOutboundMessage` / `InteractionResponse`（`app/domain/gateway.py`）：交互入口标准化模型，用于把 CLI、飞书等平台消息转换为 Application 层可处理的统一事件和回复；GatewaySessionKey 使用 `platform` 与 `platform_session_id` 组成外部 conversation 标识，不包含 FastAPI、飞书 SDK 或传输对象。

`GatewaySessionLink`（`app/domain/gateway.py`）：外部 conversation 与内部 session 的映射记录，字段包括 conversation_id、session_id、display_name、created_at、updated_at、id。

`GatewayHomeTarget`（`app/domain/gateway.py`）：平台级 home chat 投递目标，字段包括 platform、receive_id、receive_id_type、thread_id、display_name、updated_at。定时任务的 Feishu origin 通知可以保存 `target=home` 逻辑引用，发送时动态解析当前 home target，因此 home chat 切换后既有任务自动跟随新目标。

## Provider 与工具模型

`ModelInfo`（`app/domain/provider.py`）：模型能力描述，字段包括 id、display_name、provider、supports_tools、supports_streaming。

`ProviderConfig`（`app/domain/provider.py`）：Provider 注册表的脱敏配置实体（frozen dataclass），字段包括 id、name、provider_type、base_url、model、api_key_present、is_active、extra_headers、created_at、updated_at。**永远不包含 api_key 明文字段**；明文 api_key 通过 `ProviderRegistry.get_secret(id)` 单独读取且仅供 Infrastructure 工厂调用。

`LLMResult`（`app/domain/provider.py`）：非流式模型结果，字段包括 message、finish_reason、usage、raw。

`LLMEvent`（`app/domain/provider.py`）：流式模型事件，类型包括 message_start、content_delta、tool_call_delta、message_done、error。

`ToolDefinition`（`app/domain/tool.py`）：工具定义值对象，字段包括 name、description、input_schema、risk_level、permissions、timeout_seconds、enabled、source_type、toolset，不包含 handler。source_type 表示工具来源（builtin、knowledge、skill、mcp、plugin、agent），toolset 表示能力分组；执行风险仍由 risk_level 表达，不能与来源混用。

`ToolCallRequest`（`app/domain/tool.py`）：工具执行请求，字段包括 id、name、arguments。

`ToolExecutionContext`（`app/domain/tool.py`）：单轮工具执行上下文，字段包括 allowed_confirm_tools、session_id、metadata、trusted_metadata、execution_context_mode、permitted_managed_tools、enabled_override。metadata 可来自客户端但不可信；trusted_metadata 只由 Gateway/服务端可信入口写入，用于 managed tool 授权和外部记忆写入权限判断；该对象不持久化、不跨轮复用、不进入 provider request。

`ToolResultStatus`（`app/domain/tool.py`）：工具执行状态枚举，取值包括 success、error、permission_denied、timeout。

`PermissionDecision`（`app/domain/tool.py`）：权限判定值对象，字段包括 allowed、reason。

`ToolResult`（`app/domain/tool.py`）：工具执行结果，字段包括 tool_call_id、tool_name、status、content、duration_ms，其中 status 使用 ToolResultStatus。

## Knowledge 模型

`KnowledgeBase`（`app/domain/knowledge.py`）：KB 后端实例的脱敏配置实体，字段包括 id、name、description、base_type、base_url、dataset_id、api_key_present、enabled、default_top_k、default_min_score、last_probe_status、last_probe_error、last_probed_at、created_at、updated_at。该模型不包含 api_key 明文字段。

`KnowledgeBaseSecret`（`app/domain/knowledge.py`）：KB 密钥值对象，字段包括 kb_id、api_key，仅供 probe/search 时从 registry 单独读取。

`KnowledgeSearchRequest`（`app/domain/knowledge.py`）：LLM 工具侧检索请求，字段包括 kb_id、query、top_k、min_score。`kb_id` 必填，不支持默认 KB。

`KnowledgeBackendSearchRequest`（`app/domain/knowledge.py`）：后端 adapter 检索请求，字段包括 query、top_k、min_score；已解析的 KnowledgeBase 与 secret 由 Application 显式传入 adapter。

`KnowledgeSnippet` / `KnowledgeSearchResult`（`app/domain/knowledge.py`）：检索结果标准形态，用于屏蔽 N-KB、Ragflow 等后端响应差异。

`KnowledgeBaseType` 支持 `n_kb` 与 `ragflow`，表示通信协议类型，不表示独立业务子域。`KnowledgeProbeStatus` 支持 unknown、success、failed。

## 端口协议

`LLMProvider`（`app/domain/provider.py`）：定义 list_models、chat、supports_tools。Infrastructure 的 OpenAI-compatible Provider 实现该端口；运行时由 Application 层 `ActiveProviderHolder` 适配实现热切换。

`ProviderRegistry`（`app/domain/provider.py`）：定义 list_providers、get_provider、create_provider、update_provider、delete_provider、set_active、get_active、get_secret 接口。Infrastructure 的 SQLiteProviderRegistry 实现该端口；`get_secret` 只供 ActiveProviderHolder 工厂调用，不通过 HTTP 暴露。

`MemoryStore`（`app/domain/memory.py`）：定义 session、message、tool_call、task_state、summary 的读写接口。Infrastructure 的 SQLiteMemoryStore 实现该端口。

`GatewaySessionRegistry`（`app/domain/gateway.py`）：定义 get_active_session、create_session_link、set_active_session、list_session_links、delete_session_link、mark_event_processed、set_home_target、get_home_target、list_conversations、count_conversations、get_last_active 接口。Infrastructure 的 SQLiteGatewaySessionRegistry 实现该端口，用于多入口 conversation 与内部 session 的映射、平台级 home chat、事件幂等和 PlatformService 会话统计。

`PlatformRegistry`（`app/domain/platform.py`）：定义 list、get、get_lifecycle 接口。Infrastructure 的 InMemoryPlatformRegistry 实现该端口，应用启动时由 main.py 基于配置与已装配 lifecycle 单例构建；Application 的 PlatformService 只依赖该端口和 GatewaySessionRegistry。

`McpSiteRegistry`（`app/domain/mcp.py`）：定义 MCP 站点和工具映射的 list/get/create/update/delete、replace_site_tools、update_probe_status、update_tool_enabled 接口。站点支持 streamable_http、sse 和 stdio 传输；stdio 配置包含 command、args、env。Infrastructure 的 SQLiteMcpSiteRegistry 实现该端口；Application 只依赖该端口和 McpClient 协议。

`KnowledgeBaseRegistry`（`app/domain/knowledge.py`）：定义 KB 后端实例的 list/get/create/update/delete、get_secret、update_probe_status 接口。Infrastructure 的 SQLiteKnowledgeBaseRegistry 实现该端口；api_key 明文只通过 get_secret 单独读取，不通过 HTTP 或 ToolDefinition 暴露。

`KnowledgeRetriever` / `KnowledgeRetrieverFactory`（`app/domain/knowledge.py`）：定义检索和探测端口。Infrastructure 的 Knowledge HTTP adapters 根据 KnowledgeBaseType 选择 N-KB 或 Ragflow 协议实现，Application 只依赖端口，不 import 具体 HTTP client。

`Summarizer`（`app/domain/memory.py`）：定义摘要生成接口。默认 HeuristicSummarizer 实现。

`ToolExecutor`（`app/domain/tool.py`）：定义工具执行接口。Infrastructure 的 BuiltinToolExecutor 实现具体 handler。

## 配置模型

`Settings`（`app/config.py`）：运行时配置模型，从 `.env` 和环境变量读取，前缀为 `N_AGENT_`。字段：

- provider_base_url
- provider_api_key
- provider_model
- sqlite_path
- workspace_root
- agent_iteration_limit
- kb_enabled
- kb_base_url
- kb_default_top_k
- kb_default_min_score
- kb_timeout_seconds

`N_AGENT_KB_*` 当前作为 legacy seed 配置：当 knowledge_bases 表为空且 `kb_enabled=True`、`kb_base_url` 非空时，启动时写入一条 `legacy-n-kb`；表非空后以 SQLite registry 为准，配置不覆盖已有 KB。
- mcp_connect_timeout_seconds
- mcp_max_tools
- mcp_max_schema_bytes
- mcp_max_result_bytes
- mcp_allow_private_hosts
- gateway_enabled
- feishu_enabled
- feishu_app_id
- feishu_app_secret
- feishu_tenant_key
- feishu_allowed_open_ids
- feishu_allowed_chat_ids

Docker Compose 项目名不属于应用配置，由 Docker Compose 读取 `COMPOSE_PROJECT_NAME`。

## SQLite schema

SQLite store 位于 `app/infrastructure/memory/sqlite_store.py`，初始化以下表：

```sql
sessions(id, title, created_at, updated_at, source, external_memory_enabled_json)
messages(id, session_id, role, content_json, created_at, provider_message_id, tool_call_id, name)
tool_calls(id, session_id, message_id, tool_name, arguments_json, result_json, status, duration_ms, created_at)
task_states(session_id, status, iteration_count, last_error, updated_at)
summaries(session_id, summary, source_message_id, updated_at)
providers(id, name UNIQUE, provider_type, base_url, model, api_key, extra_headers_json, is_active, created_at, updated_at)
gateway_conversations(id, platform, platform_session_id, thread_id, display_name, active_session_id, created_at, updated_at)
gateway_session_links(id, conversation_id, session_id, created_at, updated_at)
gateway_processed_events(id, platform, event_id, message_id, created_at)
gateway_home_targets(platform, receive_id, receive_id_type, thread_id, display_name, updated_at)
mcp_sites(id, name UNIQUE, transport_type, url, command, args_json, env_json, enabled, last_probe_status, last_probe_error, last_probed_at, created_at, updated_at)
mcp_tools(id, site_id, remote_name, local_name UNIQUE, description, input_schema_json, enabled, last_seen_at)
knowledge_bases(id, name UNIQUE, description, base_type, base_url, dataset_id, api_key, enabled, default_top_k, default_min_score, last_probe_status, last_probe_error, last_probed_at, created_at, updated_at)
external_memory_providers(id, name UNIQUE, provider_type, base_url, api_key, enabled, extra_config, last_probe_status, last_probe_error, last_probed_at, created_at, updated_at)
```

providers 表唯一索引：

```sql
CREATE UNIQUE INDEX idx_providers_active ON providers(is_active) WHERE is_active = 1
```

该 partial unique index 保证全表至多一条 active 记录，由 `SQLiteProviderRegistry.set_active` 通过先 `UPDATE is_active=0 WHERE is_active=1` 再 `UPDATE is_active=1 WHERE id=?` 实现切换；providers.api_key 与 knowledge_bases.api_key 列以明文形式落地 `locals/sessions.db`，依赖 Docker volume 持久化与文件系统隔离保护，不通过 HTTP 暴露、不写入日志。KnowledgeBase 更新中 `api_key=None` 表示保持不变，空字符串表示清空，非空字符串表示覆盖。external_memory_providers 表存储 mem0/holographic/honcho 三类检索记忆 provider 配置，`at-most-one-enabled` 约束由 `SQLiteExternalMemoryProviderRegistry._assert_no_other_enabled` 在 create/update enabled=True 时校验；api_key 三态更新同 providers/knowledge_bases；holographic adapter 的 facts 数据存储在 extra_config.db_path 指向的独立 SQLite 文件（默认 `locals/external-memory/holographic.db`），不与 sessions.db 共享。

索引：

```sql
idx_messages_session_created_at ON messages(session_id, created_at)
idx_tool_calls_session_created_at ON tool_calls(session_id, created_at)
```

JSON 边界：

- `sessions.external_memory_enabled_json` 存储会话级外部记忆 profile 的 JSON 数组；null 表示尚未锁定，非 null 表示该 Chat Session 后续所有轮次必须使用同一 profile
- `messages.content_json` 存储消息内容
- `tool_calls.arguments_json` 存储工具参数
- `tool_calls.result_json` 存储工具结果
- `mcp_tools.input_schema_json` 存储 MCP 远端工具 schema
- `mcp_sites.args_json` 和 `mcp_sites.env_json` 存储 stdio MCP server 的参数数组和环境变量映射
- SQLite JSON 字段在 Infrastructure 内部序列化/反序列化，不泄漏到 Domain 端口外

会话级联删除：`MemoryStore.delete_session` 在 SQLiteMemoryStore 内单连接顺序清理 gateway_session_links、gateway_conversations.active_session_id、messages、tool_calls、task_states、summaries、sessions，返回 sessions 受影响行数 > 0；缺失 session 返回 False，由 Application 层（SessionService.delete_session）映射为 `SessionNotFoundError`。

## OpenAI-compatible 协议边界

Interfaces 层请求模型位于 `app/interfaces/http/openai.py`，仅作为外部协议适配：

- `ChatCompletionRequest` 支持 model、messages、stream、tools、tool_choice、temperature、max_tokens、metadata，并允许额外字段
- `ChatMessage` 支持 role、content，并允许额外字段
- Interfaces 将请求转换为 `ChatCompletionInput`，不把 OpenAI 请求模型传入 Domain
- 非流式响应编码为 `chat.completion`
- 流式响应编码为 `chat.completion.chunk`，并以 `[DONE]` 结束

## Session 边界

会话 id 解析优先级：

1. `X-Session-ID` header，由 Interfaces 传入 `ChatCompletionInput.session_id`
2. 请求体 `metadata.session_id`
3. 自动创建 `tmp-{uuid}` 临时持久化 session

Dashboard 使用 `metadata.session_id` 绑定会话。

Chat Session 的外部记忆 profile 由 `ChatCompletionService` 在首轮消息时锁定，锁定逻辑按优先级：(1) session 已有锁定值 → 沿用；(2) 已有历史消息但无锁定值的 legacy session → `["builtin"]`；(3) 请求显式传 `options.external_memory_enabled`（字段存在性判断，区分"未传"与"显式传 ['builtin']"）→ 归一化后使用；(4) 请求未传字段 → `["builtin", *active_external_query_provider_names]`，其中 active 检索记忆 provider 名称由 `ActiveExternalMemoryReader` 端口（由 `ExternalMemoryProviderService` 实现，读 `ExternalMemoryManager` 内存状态、无 IO）提供。启用检索记忆 provider 后，新会话首轮不传字段即可自动纳入该 provider。后续轮次即使客户端继续传入不同的 `external_memory_enabled`，Application 也必须使用 sessions 表里的锁定值。

## Docker Compose 数据边界

只考虑 Docker Compose 运行时，容器内路径为：

- SQLite：`/app/locals/sessions.db`
- workspace：`/workspace`

当前 compose 挂载策略：

```yaml
volumes:
  - /Users/niean/install/n-agent/locals:/app/locals
  - /Users/niean/install/n-agent/workspace:/workspace
```

因此：

- SQLite 数据保存在宿主机 `/Users/niean/install/n-agent/locals/sessions.db`
- 文件工具只能访问宿主机 `/Users/niean/install/n-agent/workspace` 对应的容器路径 `/workspace`
- KB 后端是外部独立服务，Dashboard 中每条 knowledge_bases 记录的 base_url 必须从 N-Agent 运行环境可达；容器内不能使用指向 N-Agent 容器自身的 localhost，应使用 Compose service name、共享 network 或宿主机网关地址
- N-Agent compose 访问 N-KB 时应把 n-agent 容器加入 N-KB 所在 Docker 网络（`n-kb_default`，external），并以 KB base_url `http://n-kb:8212` 通过 service name 直连。否则 hostname 会被 Docker Desktop 内部 DNS 解析到不可达代理地址，TCP 表面 connect 成功但 HTTP 响应被丢弃，httpx 抛 RemoteProtocolError

## 边界约定

- Domain 不接触 SQLite row、OpenAI SDK 对象、FastAPI 请求对象或 LangGraph 内部事件
- Application 通过 Domain 端口访问 Provider、工具和 Memory
- Infrastructure 负责 SDK、SQLite、文件系统和具体工具 handler
- Interfaces 负责 HTTP 请求/响应、SSE 编码和 Dashboard JSON，不承载工具权限或 Agent Loop 规则

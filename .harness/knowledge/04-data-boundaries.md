<!-- SUMMARY: N-Agent 的领域数据模型、配置模型、SQLite schema、OpenAI-compatible 协议边界和 Docker Compose 数据挂载边界，含飞书 ToolPolicy 审批的会话授权与协议 pending 所有权 -->
# 数据与类型边界

## 领域模型

完整 DDD 子域划分、聚合实体和应用服务调用流见 `.harness/knowledge/06-domain-model.md`。

`AgentState`（`app/domain/agent.py`）：Agent Runtime 的运行状态，字段包括 session_id、input_messages、working_messages、pending_tool_calls、tool_results、summary、run_status、iteration_count、error、final_message、finish_reason。该模型属于 Domain，不包含 LangGraph 类型。

`AgentRun`（`app/domain/agent.py`）：一次 Agent 运行的领域对象，包含 session_id、input_messages、id、status、iteration_count、error、end_reason。

`ConversationSession`（`app/domain/session.py`）：会话聚合根，字段包括 id、title、source、external_memory_enabled、created_at、updated_at、acp_metadata。external_memory_enabled 是会话级外部记忆 profile 锁定值，首轮消息前可由 Chat/Dashboard 选择，首轮后不可变；未锁定的新会话为 null，发送首轮时写入规范化列表。acp_metadata 是 ACP 会话元数据（host cwd、container cwd、ACP session id 等映射信息），仅 `source="acp"` 的会话写入，其他来源会话为 null。

`ConversationMessage`（`app/domain/session.py`）：会话消息值对象，字段包括 id、role、content、tool_call_id、name、created_at。role 支持 user、assistant、tool 等 Provider 消息角色。content 类型为 `str | list[dict] | dict`：str 为纯文本；list 为 OpenAI 风格多模态内容数组（`[{type:text,text:...},{type:image_url,image_url:{url:...}}]`），user 消息支持 text+image_url 混合，assistant 消息支持 `{content,tool_calls}` dict；持久化前由 `content_utils.normalize_content` 归一化，摘要/标题/外部记忆 prefetch 通过 `content_utils.extract_text` 提取纯文本避免 base64 泄漏。

`ToolCall`（`app/domain/session.py`）：工具调用记录，字段包括 id、session_id、message_id、tool_name、arguments、result、status、duration_ms、created_at。

`TaskState`（`app/domain/session.py`）：任务状态，字段包括 session_id、status、iteration_count、last_error、updated_at。

`Summary`（`app/domain/session.py`）：摘要记录，字段包括 session_id、summary、source_message_id、updated_at。

`Platform` / `PlatformKind` / `PlatformDescriptor` / `PlatformLifecycle` / `PlatformRegistry`（`app/domain/platform.py`）：平台聚合的领域枚举、描述和生命周期端口，用于表达飞书、钉钉、企微等外部消息平台及其 configured/connected/disconnected/fatal 状态，不包含具体 SDK、HTTP 或长连接对象。CLI/TUI 终端聊天只使用 GatewaySessionKey.source=`cli`，不进入 Platform。

`GatewaySessionKey` / `InteractionMessage` / `GatewayOutboundMessage` / `InteractionResponse`（`app/domain/gateway.py`）：交互入口标准化模型，用于把 CLI、飞书、ACP 等入口消息转换为 Application 层可处理的统一事件和回复；GatewaySessionKey 使用 `source` 与 `platform_session_id` 组成外部 conversation 标识，不包含 FastAPI、飞书 SDK 或传输对象。`InteractionMessage` 字段包括 id、session_key、text、images（`list[str]`，data URL 列表，可选）、metadata；`GatewayService` 在无 images 时直接使用 text，有 images 时通过 `content_utils` 风格构造 OpenAI content array 传给 ChatCompletionService；slash 命令附带 images 时被拒绝。

`GatewaySessionLink`（`app/domain/gateway.py`）：外部 conversation 与内部 session 的映射记录，字段包括 conversation_id、session_id、display_name、created_at、updated_at、id。

`GatewayHomeTarget`（`app/domain/gateway.py`）：平台级 home chat 投递目标，字段包括 platform、receive_id、receive_id_type、thread_id、display_name、updated_at。定时任务的 Feishu origin 通知可以保存 `target=home` 逻辑引用，发送时动态解析当前 home target，因此 home chat 切换后既有任务自动跟随新目标。

`GatewayToolApprovalService`（`app/application/gateway_tool_approval_service.py`）：Application 层进程内授权状态，键为 `(session_id, actor_id, tool_name)`，只表达 ToolPolicy 的“本会话信任”；不持久化、不跨进程共享，也不替代 `ToolService` 的执行前复判。

`FeishuToolApprovalBridge` pending（`app/interfaces/feishu_tool_approval.py`）：Interfaces 层短生命周期协议状态，绑定 request/session/actor/reply target、创建/过期时间、等待中的 Future、服务端返回的 card message id 与原子 claim 状态；完成、超时、取消或发送失败即清理。它不保存会话授权，卡片参数只使用脱敏摘要，回调身份以服务端 pending 绑定的 actor/chat/card message id 为准，不信任客户端回传的 kind/thread/platform。

## Provider 与工具模型

`ModelInfo`（`app/domain/provider.py`）：模型能力描述，字段包括 id、display_name、provider、supports_tools、supports_streaming。

`ProviderConfig`（`app/domain/provider.py`）：Provider 注册表的脱敏配置实体（frozen dataclass），字段包括 id、name、provider_type、base_url、model、api_key_present、is_active、extra_headers、created_at、updated_at、supports_vision。**永远不包含 api_key 明文字段**；明文 api_key 通过 `ProviderRegistry.get_secret(id)` 单独读取且仅供 Infrastructure 工厂调用。`supports_vision` 表示 provider 是否支持图片输入：openai-compatible 类型默认 True，anthropic 类型默认 False；由 `ProviderService.create_provider`/`update_provider` 按 `ProviderCreateInput`/`ProviderUpdateInput` 传入值设置，Dashboard 可在线编辑；`AgentGraphRunner.call_llm` 在 vision preflight 中通过 `ActiveProviderHolder.current_config.supports_vision` 判断，不支持 vision 时遇到 image content 直接返回友好 assistant 消息而非调用 provider。

`LLMResult`（`app/domain/provider.py`）：非流式模型结果，字段包括 message、finish_reason、usage、raw。

`LLMEvent`（`app/domain/provider.py`）：流式模型事件，类型包括 message_start、content_delta、tool_call_delta、message_done、error。

`ToolDefinition`（`app/domain/tool.py`）：工具定义值对象，字段包括 name、description、input_schema、risk_level、permissions、timeout_seconds、enabled、source_type、toolset，不包含 handler。source_type 表示工具来源（builtin、knowledge、skill、mcp、plugin、agent），toolset 表示能力分组；执行风险仍由 risk_level 表达，不能与来源混用。

`ToolCallRequest`（`app/domain/tool.py`）：工具执行请求，字段包括 id、name、arguments。

`ToolExecutionContext`（`app/domain/tool.py`）：单轮工具执行上下文，字段包括 allowed_confirm_tools、session_id、metadata、trusted_metadata、execution_context_mode、permitted_managed_tools、enabled_override。metadata 可来自客户端但不可信；trusted_metadata 只由 Gateway/服务端可信入口写入，用于 managed tool 授权和外部记忆写入权限判断；该对象不持久化、不跨轮复用、不进入 provider request。

`ToolResultStatus`（`app/domain/tool.py`）：工具执行状态枚举，取值包括 success、error、permission_denied、timeout、skipped。

`Policy` / `PolicyOutcome` / `PolicyDecision`（`app/domain/policy.py`）：Domain Shared Kernel。`Policy` 是泛型规则协议；`PolicyOutcome` 统一 allow、deny、require_approval；`PolicyDecision` 携带 outcome 与非空 reason。Shared Kernel 不承载具体业务规则。

`ToolExposurePolicy`（`app/domain/tool_policy.py`）：工具模型暴露场景枚举，包括 default、safe_only。

`ToolPolicyRequest`（`app/domain/tool_policy.py`）：工具执行策略请求，组合 `ToolDefinition` 与 `ToolCallRequest`。

`ToolPolicy`（`app/domain/tool_policy.py`）：Tool Domain 具体策略，负责定义合法性、模型暴露、执行决策和一次授权。它消费 `ToolExecutionContext`，返回公共 `PolicyDecision`。

`ToolExecutionEvaluation`（`app/application/tool_service.py`）：Application 层一次执行评估结果，包含 `PolicyDecision` 与稳定的审批快照；内部 token 绑定原请求和原定义，防止评估后工具定义被替换仍继续执行。

`ToolResult`（`app/domain/tool.py`）：工具执行结果，字段包括 tool_call_id、tool_name、status、content、duration_ms，其中 status 使用 ToolResultStatus。

## Usage 观测模型

`CanonicalUsage`（`app/domain/usage.py`）：归一化后的 token 五桶值对象（frozen dataclass），字段包括 input_tokens、output_tokens、cache_read_tokens、cache_write_tokens、reasoning_tokens、request_count、raw_usage。派生属性 `prompt_tokens`（= input_tokens + cache_read_tokens）、`total_tokens`（= input + output + cache_read + cache_write + reasoning）。raw_usage 保留 Provider 原始 usage dict 供调试。

`UsageCost`（`app/domain/usage.py`）：成本估算值对象（frozen dataclass），字段包括 amount_usd（Decimal str）、status（`estimated`/`unknown`）、pricing_version。

`PricingEntry`（`app/domain/usage.py`）：模型定价条目（frozen dataclass），字段包括 model_pattern、provider、input_cost_per_million、output_cost_per_million、cache_read_cost_per_million、cache_write_cost_per_million、pricing_version、source_url。`InMemoryPricingProvider` 按 model 前缀最长匹配查表。

`SessionUsageStats`（`app/domain/usage.py`）：会话级累计统计值对象，字段包括 session_id、input_tokens、output_tokens、cache_read_tokens、cache_write_tokens、reasoning_tokens、total_tokens、api_call_count、estimated_cost_usd、cost_status。

`ContextBreakdown`（`app/domain/usage.py`）：上下文分类 token 值对象（frozen dataclass），字段包括 system_prompt、tool_definitions、memory、conversation，派生属性 `total`。

`UsageRecord` / `CompressionStat`（`app/domain/usage.py`）：单次调用记录 / 压缩记录值对象，分别映射 usage_records 和 compression_stats 表行。

端口 `UsageRecorder`（`app/domain/usage.py`）：定义 record_call/get_session_stats/list_records/record_compression/list_compressions 接口。Infrastructure 的 `SqliteUsageRecorder`（`app/infrastructure/usage/sqlite_usage_recorder.py`）实现该端口，与 sessions.db 共享 path，sessions 表迁移幂等（PRAGMA table_info 检查列存在再 ALTER），async 方法内部直接调用同步 sqlite3（与 SQLiteMemoryStore 一致，技术债 D018）。

端口 `PricingProvider`（`app/domain/usage.py`）：定义 `get_pricing(model, provider) -> PricingEntry | None`。Infrastructure 的 `InMemoryPricingProvider`（`app/infrastructure/usage/pricing_table.py`）硬编码 OpenAI/Anthropic/DeepSeek 主流模型定价，按 model 前缀最长匹配。

端口 `ContextBreakdownCalculator`（`app/domain/usage.py`）：定义 `compute(system_prompt, tool_definitions, messages, external_memory_block) -> ContextBreakdown`。Infrastructure 的 `ContextBreakdownCalculatorImpl`（`app/infrastructure/usage/context_breakdown_calculator.py`）复用 ContextCompressor 的 ~4 chars/token 估算逻辑，按 system_prompt/tool_definitions/memory/conversation 四类分桶。

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

`ToolExecutor`（`app/domain/tool.py`）：定义工具执行 SPI。具体实现属于各支撑子域或 Infrastructure；Infrastructure 的 BuiltinToolExecutor 实现内置 handler。

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
- context_compression_enabled (bool, 默认 True)
- context_length (int, 默认 32000, ge=1024)
- context_compression_threshold (float, 默认 0.50, gt=0, le=1)
- context_compression_target_ratio (float, 默认 0.20, gt=0, le=1)
- context_compression_protect_first_n (int, 默认 3, ge=0)
- context_compression_protect_last_n (int, 默认 20, ge=0)
- context_compression_cooldown_seconds (int, 默认 300, ge=0)

跨字段校验：`context_compression_target_ratio` 必须 < `context_compression_threshold`，由 `Settings._validate_context_compression_ratios` model_validator(mode="after") 强制，违反抛 ValueError。

上下文压缩 summary 持久化边界（增量压缩 + 摘要持久化）：`messages` 表新增 `is_summary INTEGER NOT NULL DEFAULT 0` 列（迁移函数 `_migrate_add_is_summary_column` 幂等）；`summaries` 表 schema 不变。`ContextCompressor.compress` 返回 `ContextCompressionResult`，`result.messages` 含恰好 1 条 `role="user"` + `content` 以 `CONTEXT_SUMMARY_PREFIX`（`"[CONTEXT SUMMARY]: "`，定义在 `app/domain/context.py`）开头的摘要消息。`prepare_context` 的压缩阶段在 `result.compressed=True` 时按 a-f 顺序：识别摘要消息 -> 构造 `ConversationMessage(is_summary=True)` -> 调 `replace_summary_message`（单连接事务 DELETE 旧 is_summary=1 + INSERT 新）-> 调 `save_summary(source_message_id=新摘要消息 id)` -> 更新 state。双写失败降级：`replace_summary_message` 失败时 state 不变；`save_summary` 失败时 messages 表已更新，summaries 表滞后一轮（Dashboard 降级），不回滚。

Docker Compose 项目名不属于应用配置，由 Docker Compose 读取 `COMPOSE_PROJECT_NAME`。

## SQLite schema

SQLite store 位于 `app/infrastructure/memory/sqlite_store.py`，初始化以下表：

```sql
sessions(id, title, created_at, updated_at, source, external_memory_enabled_json, acp_metadata_json)
messages(id, session_id, role, content_json, created_at, provider_message_id, tool_call_id, name, is_summary)
tool_calls(id, session_id, message_id, tool_name, arguments_json, result_json, status, duration_ms, created_at)
task_states(session_id, status, iteration_count, last_error, updated_at)
summaries(session_id, summary, source_message_id, updated_at)
providers(id, name UNIQUE, provider_type, base_url, model, api_key, extra_headers_json, is_active, supports_vision, created_at, updated_at)
gateway_conversations(id, platform, platform_session_id, thread_id, display_name, active_session_id, created_at, updated_at)
gateway_session_links(id, conversation_id, session_id, created_at, updated_at)
gateway_processed_events(id, platform, event_id, message_id, created_at)
gateway_home_targets(platform, receive_id, receive_id_type, thread_id, display_name, updated_at)
mcp_sites(id, name UNIQUE, transport_type, url, command, args_json, env_json, enabled, last_probe_status, last_probe_error, last_probed_at, created_at, updated_at)
mcp_tools(id, site_id, remote_name, local_name UNIQUE, description, input_schema_json, enabled, last_seen_at)
knowledge_bases(id, name UNIQUE, description, base_type, base_url, dataset_id, api_key, enabled, default_top_k, default_min_score, last_probe_status, last_probe_error, last_probed_at, created_at, updated_at)
external_memory_providers(id, name UNIQUE, provider_type, base_url, api_key, enabled, extra_config, last_probe_status, last_probe_error, last_probed_at, created_at, updated_at)
external_memory_global_config(id INTEGER PRIMARY KEY CHECK (id = 1), enabled_providers TEXT, updated_at)
skills(id, name UNIQUE, relative_path, description, platforms_json, frontmatter_json, enabled, readiness, last_scan_status, last_scan_error, last_seen_at, created_at, updated_at)
plugins(id, key UNIQUE, name, version, description, author, kind, source, source_path, enabled, config_json, capabilities_json, manifest_json, last_scan_status, last_scan_error, last_scanned_at, created_at, updated_at)
plugin_secrets(plugin_key, field_name, secret_value, updated_at, PRIMARY KEY(plugin_key, field_name), FOREIGN KEY(plugin_key) REFERENCES plugins(key) ON DELETE CASCADE)
scheduled_tasks(id, name, prompt, cron_expression, timezone, enabled, status, session_id, origin_json, delivery_target, delivery_context_json, execution_policy_json, next_run_at, lease_until, lease_owner, claim_id, last_run_at, last_status, last_error, last_delivery_error, unread_count, created_at, updated_at)
scheduled_task_executions(id, task_id, session_id, claim_id, lease_owner, claimed_next_run_at, started_at, completed_at, status, output, error, delivery_status, delivery_error, created_at)
sandbox_released_history(id, session_id, sandbox_type, sandbox_id, created_at, released_at, reason)
sandbox_execution_history(id, session_id, code_hash, code, result_json, status, duration_ms, authorized_callback_tools_json, created_at, execution_type)
usage_records(id, session_id, model, provider, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, total_tokens, estimated_cost_usd, cost_status, latency_ms, created_at)
compression_stats(id, session_id, before_tokens, after_tokens, tokens_saved, compression_ratio, created_at)
```

sessions 表 token/cost 列（迁移幂等，由 `SqliteUsageRecorder.init` 通过 `PRAGMA table_info` 检查列存在再 `ALTER TABLE ADD COLUMN`）：

```sql
input_tokens INTEGER DEFAULT 0
output_tokens INTEGER DEFAULT 0
cache_read_tokens INTEGER DEFAULT 0
cache_write_tokens INTEGER DEFAULT 0
reasoning_tokens INTEGER DEFAULT 0
total_tokens INTEGER DEFAULT 0
api_call_count INTEGER DEFAULT 0
estimated_cost_usd REAL DEFAULT 0
cost_status TEXT DEFAULT 'unknown'
pricing_version TEXT
```

providers 表唯一索引：

```sql
CREATE UNIQUE INDEX idx_providers_active ON providers(is_active) WHERE is_active = 1
```

该 partial unique index 保证全表至多一条 active 记录，由 `SQLiteProviderRegistry.set_active` 通过先 `UPDATE is_active=0 WHERE is_active=1` 再 `UPDATE is_active=1 WHERE id=?` 实现切换；providers.api_key 与 knowledge_bases.api_key 列以明文形式落地 `locals/sessions.db`，依赖 Docker volume 持久化与文件系统隔离保护，不通过 HTTP 暴露、不写入日志。KnowledgeBase 更新中 `api_key=None` 表示保持不变，空字符串表示清空，非空字符串表示覆盖。external_memory_providers 表存储 mem0/holographic/honcho 三类检索记忆 provider 配置，`at-most-one-enabled` 约束由 `SQLiteExternalMemoryProviderRegistry._assert_no_other_enabled` 在 create/update enabled=True 时校验；api_key 三态更新同 providers/knowledge_bases；holographic adapter 的 facts 数据存储在 extra_config.db_path 指向的独立 SQLite 文件（默认 `locals/external-memory/holographic.db`），不与 sessions.db 共享。

索引：

```sql
idx_messages_session_created_at ON messages(session_id, created_at)
idx_messages_summary_session ON messages(session_id) WHERE is_summary = 1
idx_tool_calls_session_created_at ON tool_calls(session_id, created_at)
idx_skills_enabled ON skills(enabled)
idx_plugins_enabled ON plugins(enabled)
idx_scheduled_tasks_due ON scheduled_tasks(enabled, status, next_run_at)
idx_scheduled_tasks_session ON scheduled_tasks(session_id)
idx_scheduled_executions_task_created ON scheduled_task_executions(task_id, created_at)
idx_sandbox_released_history_released_at ON sandbox_released_history(released_at)
idx_sandbox_execution_history_created_at ON sandbox_execution_history(created_at)
idx_sandbox_execution_history_session_created_at ON sandbox_execution_history(session_id, created_at)
idx_usage_records_session ON usage_records(session_id)
idx_compression_stats_session ON compression_stats(session_id)
```

JSON 边界：

- `sessions.external_memory_enabled_json` 存储会话级外部记忆 profile 的 JSON 数组；null 表示尚未锁定，非 null 表示该 Chat Session 后续所有轮次必须使用同一 profile
- `sessions.acp_metadata_json` 存储 ACP 会话元数据（host cwd、container cwd、ACP session id 等映射信息），仅 `source="acp"` 的会话写入；其他来源会话为 null。ACP stdio 服务端在 `session/new` 时写入，`session/load` 时读取复用，用于在 ACP 客户端重连后恢复会话上下文与 cwd 映射
- `messages.content_json` 存储消息内容
- `tool_calls.arguments_json` 存储工具参数
- `tool_calls.result_json` 存储工具结果
- `sandbox_execution_history.result_json` 存储 execute_code 沙盒执行结果，`authorized_callback_tools_json` 存储本次实际授权的 callback tool 名称列表，`execution_type` 区分执行类型（`execute_code` 或 `terminal`，默认 `execute_code`，由 `SQLiteSandboxExecutionHistoryRegistry._migrate_add_execution_type` 为旧库补列）
- `mcp_tools.input_schema_json` 存储 MCP 远端工具 schema
- `mcp_sites.args_json` 和 `mcp_sites.env_json` 存储 stdio MCP server 的参数数组和环境变量映射
- `scheduled_tasks.origin_json` 存储任务来源上下文（platform、receive_id 等），`delivery_context_json` 存储投递目标上下文，`execution_policy_json` 存储执行策略（mode、tool_exposure_policy、allow_confirm_tools）
- `skills.platforms_json` 存储适用平台列表，`frontmatter_json` 存储 skill 文件 frontmatter 元数据
- `plugins.config_json` 存储非敏感配置（明文），`plugins.capabilities_json` 存储 provides_tools/unsupported 能力声明，`plugins.manifest_json` 存储 plugin.yaml 完整 manifest；`plugin_secrets.secret_value` 存储 secret 明文（与 providers.api_key 同等保护级别，依赖文件系统隔离，不通过 HTTP 暴露、不写入日志）；`Plugin.to_public_view().secret_refs` 仅返回 `{field: bool}` 占位标记，由 registry 查询 plugin_secrets 表填充
- `external_memory_global_config.enabled_providers` 存储全局启用的外部记忆 provider 名称列表
- SQLite JSON 字段在 Infrastructure 内部序列化/反序列化，不泄漏到 Domain 端口外

会话级联删除：`MemoryStore.delete_session` 在 SQLiteMemoryStore 内单连接顺序清理 gateway_session_links、gateway_conversations.active_session_id、messages、tool_calls、task_states、summaries、sessions，返回 sessions 受影响行数 > 0；缺失 session 返回 False，由 Application 层（SessionService.delete_session）映射为 `SessionNotFoundError`。沙盒审计数据不属于 Chat Session 级联删除范围：`sandbox_released_history` 与 `sandbox_execution_history` 由沙盒 Dashboard 显式删除动作或运维清理策略处理，释放沙盒和删除会话均不应自动删除这些长期历史。

## OpenAI-compatible 协议边界

Interfaces 层请求模型位于 `app/interfaces/http/openai_compatible.py`，仅作为外部协议适配：

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

Chat Session 的外部记忆 profile 由 `ChatCompletionService` 在首轮消息时锁定，优先级：(1) session 已锁定 → 沿用；(2) legacy session → `[]`；(3) 显式传 `options.external_memory_enabled` → 归一化值；(4) 未传字段 → `[]`。系统记忆 builtin 与 active 检索记忆 provider 都不自动纳入默认 profile，需在会话首轮显式启用。后续轮次即使客户端传入不同值，也必须使用 sessions 表里的锁定值。

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
- 废弃沙盒历史保存在同一 SQLite 的 `sandbox_released_history` 表；释放沙盒会删除 scratch 运行目录，但不会删除该历史表记录
- execute_code 执行历史保存在同一 SQLite 的 `sandbox_execution_history` 表；Dashboard 兼容读取旧 `tool_calls` 记录，但长期保存以沙盒历史表为准，删除 Chat Session 不会清理该表
- 文件工具只能访问宿主机 `/Users/niean/install/n-agent/workspace` 对应的容器路径 `/workspace`
- KB 后端是外部独立服务，Dashboard 中每条 knowledge_bases 记录的 base_url 必须从 N-Agent 运行环境可达；容器内不能使用指向 N-Agent 容器自身的 localhost，应使用 Compose service name、共享 network 或宿主机网关地址
- N-Agent compose 访问 N-KB 时应把 n-agent 容器加入 N-KB 所在 Docker 网络（`n-kb_default`，external），并以 KB base_url `http://n-kb:8212` 通过 service name 直连。否则 hostname 会被 Docker Desktop 内部 DNS 解析到不可达代理地址，TCP 表面 connect 成功但 HTTP 响应被丢弃，httpx 抛 RemoteProtocolError

## 边界约定

- Domain 不接触 SQLite row、OpenAI SDK 对象、FastAPI 请求对象或 LangGraph 内部事件
- Application 通过 Domain 端口访问 Provider、工具和 Memory
- Infrastructure 负责 SDK、SQLite、文件系统和具体工具 handler
- Interfaces 负责 HTTP 请求/响应、SSE 编码和 Dashboard JSON，不承载工具权限或 Agent Loop 规则

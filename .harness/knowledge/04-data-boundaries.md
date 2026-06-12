<!-- SUMMARY: N-Agent 的领域数据模型、配置模型、SQLite schema、OpenAI-compatible 协议边界和 Docker Compose 数据挂载边界 -->
# 数据与类型边界

## 领域模型

完整 DDD 子域划分、聚合实体和应用服务调用流见 `.harness/knowledge/06-domain-model.md`。

`AgentState`（`app/domain/agent.py`）：Agent Runtime 的运行状态，字段包括 session_id、input_messages、working_messages、pending_tool_calls、tool_results、summary、run_status、iteration_count、error、final_message、finish_reason。该模型属于 Domain，不包含 LangGraph 类型。

`AgentRun`（`app/domain/agent.py`）：一次 Agent 运行的领域对象，包含 session_id、input_messages、id、status、iteration_count、error、end_reason。

`ConversationSession`（`app/domain/session.py`）：会话聚合根，字段包括 id、title、source、created_at、updated_at。

`ConversationMessage`（`app/domain/session.py`）：会话消息值对象，字段包括 id、role、content、tool_call_id、name、created_at。role 支持 user、assistant、tool 等 Provider 消息角色。

`ToolCall`（`app/domain/session.py`）：工具调用记录，字段包括 id、session_id、message_id、tool_name、arguments、result、status、duration_ms、created_at。

`TaskState`（`app/domain/session.py`）：任务状态，字段包括 session_id、status、iteration_count、last_error、updated_at。

`Summary`（`app/domain/session.py`）：摘要记录，字段包括 session_id、summary、source_message_id、updated_at。

## Provider 与工具模型

`ModelInfo`（`app/domain/provider.py`）：模型能力描述，字段包括 id、display_name、provider、supports_tools、supports_streaming。

`LLMResult`（`app/domain/provider.py`）：非流式模型结果，字段包括 message、finish_reason、usage、raw。

`LLMEvent`（`app/domain/provider.py`）：流式模型事件，类型包括 message_start、content_delta、tool_call_delta、message_done、error。

`ToolDefinition`（`app/domain/tool.py`）：工具定义值对象，字段包括 name、description、input_schema、risk_level、permissions、timeout_seconds、enabled，不包含 handler。

`ToolCallRequest`（`app/domain/tool.py`）：工具执行请求，字段包括 id、name、arguments。

`ToolResultStatus`（`app/domain/tool.py`）：工具执行状态枚举，取值包括 success、error、permission_denied、timeout。

`PermissionDecision`（`app/domain/tool.py`）：权限判定值对象，字段包括 allowed、reason。

`ToolResult`（`app/domain/tool.py`）：工具执行结果，字段包括 tool_call_id、tool_name、status、content、duration_ms，其中 status 使用 ToolResultStatus。

## 端口协议

`LLMProvider`（`app/domain/provider.py`）：定义 list_models、chat、supports_tools。Infrastructure 的 OpenAI-compatible Provider 实现该端口。

`MemoryStore`（`app/domain/memory.py`）：定义 session、message、tool_call、task_state、summary 的读写接口。Infrastructure 的 SQLiteMemoryStore 实现该端口。

`Summarizer`（`app/domain/memory.py`）：定义摘要生成接口。MVP 默认 HeuristicSummarizer 实现。

`ToolExecutor`（`app/domain/tool.py`）：定义工具执行接口。Infrastructure 的 BuiltinToolExecutor 实现具体 handler。

## 配置模型

`Settings`（`app/config.py`）：运行时配置模型，从 `.env` 和环境变量读取，前缀为 `N_AGENT_`。字段：

- provider_base_url
- provider_api_key
- provider_model
- sqlite_path
- workspace_root
- agent_iteration_limit

Docker Compose 项目名不属于应用配置，由 Docker Compose 读取 `COMPOSE_PROJECT_NAME`。

## SQLite schema

SQLite store 位于 `app/infrastructure/memory/sqlite_store.py`，初始化以下表：

```sql
sessions(id, title, created_at, updated_at, source)
messages(id, session_id, role, content_json, created_at, provider_message_id, tool_call_id, name)
tool_calls(id, session_id, message_id, tool_name, arguments_json, result_json, status, duration_ms, created_at)
task_states(session_id, status, iteration_count, last_error, updated_at)
summaries(session_id, summary, source_message_id, updated_at)
```

索引：

```sql
idx_messages_session_created_at ON messages(session_id, created_at)
idx_tool_calls_session_created_at ON tool_calls(session_id, created_at)
```

JSON 边界：

- `messages.content_json` 存储消息内容
- `tool_calls.arguments_json` 存储工具参数
- `tool_calls.result_json` 存储工具结果
- SQLite JSON 字段在 Infrastructure 内部序列化/反序列化，不泄漏到 Domain 端口外

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

## 边界约定

- Domain 不接触 SQLite row、OpenAI SDK 对象、FastAPI 请求对象或 LangGraph 内部事件
- Application 通过 Domain 端口访问 Provider、工具和 Memory
- Infrastructure 负责 SDK、SQLite、文件系统和具体工具 handler
- Interfaces 负责 HTTP 请求/响应、SSE 编码和 Dashboard JSON，不承载工具权限或 Agent Loop 规则

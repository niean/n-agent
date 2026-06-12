# DDD 领域模型

## 分层边界

N-Agent 按 DDD 分层组织代码，依赖方向保持外层依赖内层。

```text
Interfaces -> Application -> Domain
Infrastructure -> Domain
```

- Domain：定义 Agent、Session、Message、Tool、Provider、Memory 等领域模型、值对象和端口协议。
- Application：编排用例和 Agent Runtime，LangGraph 只在本层负责运行流程编排。
- Infrastructure：实现外部依赖细节，包括 OpenAI-compatible Provider、SQLite Memory、内置工具和配置加载。
- Interfaces：实现 FastAPI、OpenAI-compatible API、Dashboard、SSE 和协议转换。

Domain 不依赖 FastAPI、LangGraph、SQLite、OpenAI SDK 或任何 Infrastructure 具体实现。

## 子域划分

```text
N-Agent Domain

+------------------------------------------------------+
| Conversation / Memory Context                        |
|------------------------------------------------------|
| Aggregate Root: ConversationSession                  |
| Entity: ConversationMessage                          |
| Entity: ToolCall                                     |
| Entity: TaskState                                    |
| Entity: Summary                                      |
| Port: MemoryStore                                    |
| Port: Summarizer                                     |
+--------------------------+---------------------------+
                           |
                           | provides context / persists result
                           v
+------------------------------------------------------+
| Agent Runtime                                        |
|------------------------------------------------------|
| Runtime State: AgentState                            |
| Entity: AgentRun                                     |
| Value: RunStatus, EndReason                          |
| Application Service: AgentGraphRunner                |
| Flow: load_context -> call_llm -> execute_tools      |
|       -> update_memory -> finalize                   |
+-------------+--------------------------+-------------+
              |                          |
              | calls model              | executes tools
              v                          v
+-----------------------------+    +-----------------------------+
| LLM Provider                |    | Tool Capability             |
|-----------------------------|    |-----------------------------|
| Port: LLMProvider           |    | Entity: ToolDefinition      |
| Value: ModelInfo            |    | Entity: ToolCallRequest     |
| Value: LLMResult            |    | Value: ToolResult           |
| Value: LLMEvent             |    | Value: RiskLevel            |
| Value: LLMEventType         |    | Value: ToolResultStatus     |
| Impl: OpenAICompatibleProvider | | Port: ToolExecutor          |
+-----------------------------+    | Service: ToolService        |
                                   +-----------------------------+
```

## 聚合与实体

```text
ConversationSession
  ├── ConversationMessage  1:N
  ├── ToolCall             1:N
  ├── TaskState            1:0..1
  └── Summary              1:0..1

AgentState
  ├── session_id -> ConversationSession.id
  ├── input_messages
  ├── working_messages
  ├── pending_tool_calls
  ├── tool_results
  ├── summary
  ├── run_status
  ├── iteration_count
  ├── final_message
  └── finish_reason
```

当前持久化核心是 `ConversationSession`。`AgentState` 更像一次运行中的状态聚合，不作为 SQLite 聚合根直接保存。

## DDD 分类

```text
聚合根
- ConversationSession

运行状态
- AgentState

实体
- ConversationMessage
- ToolCall
- TaskState
- Summary
- AgentRun
- ToolDefinition
- ToolCallRequest

值对象
- RunStatus
- EndReason
- RiskLevel
- ToolResultStatus
- PermissionDecision
- ToolResult
- ModelInfo
- LLMResult
- LLMEvent
- LLMEventType

领域端口
- MemoryStore
- Summarizer
- LLMProvider
- ToolExecutor

应用服务
- ChatCompletionService
- AgentGraphRunner
- ToolService
- SessionService
- ModelService

基础设施实现
- SQLiteMemoryStore
- HeuristicSummarizer
- OpenAICompatibleProvider
- BuiltinToolExecutor
```

## 应用服务调用流

```text
ChatCompletionService
  ├── create ConversationSession
  ├── append user ConversationMessage
  └── create AgentState
        |
        v
AgentGraphRunner
  ├── load_context
  |     ├── MemoryStore.list_messages(session_id)
  |     └── MemoryStore.get_summary(session_id)
  |
  ├── call_llm
  |     ├── LLMProvider.chat(...)
  |     └── ToolService.list_openai_tools()
  |
  ├── execute_tools
  |     ├── ToolService.execute(ToolCallRequest)
  |     ├── ToolExecutor.execute(...)
  |     └── MemoryStore.save_tool_call(ToolCall)
  |
  ├── update_memory
  |     ├── MemoryStore.append_message(ConversationMessage)
  |     ├── MemoryStore.save_task_state(TaskState)
  |     ├── Summarizer.summarize(...)
  |     └── MemoryStore.save_summary(Summary)
  |
  └── finalize
        └── MemoryStore.save_task_state(TaskState)
```

## SQLite Memory 数据关系

SQLite Memory 默认使用 `sessions.db`，以 session 为核心实体，围绕 session 持久化消息、工具调用、任务状态和摘要。

```text
sessions
  ├── messages      1:N    对话消息
  ├── tool_calls    1:N    工具调用记录
  ├── task_states   1:0..1 Agent 运行状态
  └── summaries     1:0..1 上下文摘要
```

当前 SQL 中显式声明的外键包括：

```text
messages.session_id -> sessions.id
tool_calls.session_id -> sessions.id
```

逻辑关联但未声明外键的字段包括：

```text
task_states.session_id -> sessions.id
summaries.session_id -> sessions.id
tool_calls.message_id -> messages.id
summaries.source_message_id -> messages.id
```

## 当前边界判断

当前设计符合 DDD 的关键点：

- 领域模型位于 Domain 层，不被 FastAPI、LangGraph、SQLite 或 Provider SDK 污染。
- Application 通过端口依赖 LLM、Memory、Tool 等能力。
- Infrastructure 只负责实现端口和外部资源访问。
- Interfaces 只做协议适配，不直接访问 SQLite 或执行工具。

后续扩展多 Provider、长期 Memory、审批流、多 Agent、自动化任务时，应继续沿用端口抽象和外层依赖内层的方向。

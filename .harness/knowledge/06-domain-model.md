<!-- SUMMARY: N-Agent 的 DDD 领域模型细化说明，包括子域划分、聚合实体、应用服务调用流和 SQLite Memory 数据关系 -->
# DDD 领域模型

## Runtime

```text
Agent Runtime
│
├── Loop
│   ├── FSM
│   ├── Scheduler
│   └── Retry
│
├── Agent
│   ├── LLM
│   ├── Prompt
│   ├── Reasoning
│   └── Planning
│
├── Memory
│   ├── Message
│   ├── Session
│   └── Long-term Memory
│
├── Action
│   ├── Tool
│   ├── Skill
│   └── Workflow
│
└── Environment
    ├── Sandbox
    ├── Browser
    ├── OS
    ├── FileSystem
    └── Network
```

## Runtime 边界

N-Agent 按 DDD 分层组织代码，Agent Runtime 位于 Application 层，依赖 Domain 定义的模型和值对象，通过端口使用 LLM、Memory、Tool 等外部能力。

```text
Interfaces -> Application -> Domain
Infrastructure -> Domain
```

- Domain：定义 Agent、Session、Message、Tool、Provider、Memory 等领域模型、值对象和端口协议。
- Application：承载 Agent Runtime、用例编排、Prompt 构建、ToolService 调度和会话流程控制。
- Infrastructure：实现 OpenAI-compatible Provider、SQLite Memory、内置工具和配置加载。
- Interfaces：实现 FastAPI、OpenAI-compatible API、Dashboard、SSE 和协议转换。

Domain 不依赖 FastAPI、LangGraph、SQLite、OpenAI SDK 或任何 Infrastructure 具体实现。LangGraph 是 Runtime Loop 的实现细节，只能出现在 Application 层。

## Loop

Loop 负责一次 Agent 运行的状态推进、步骤调度、工具调用回环和结束判断。当前实现由 `AgentGraphRunner` 使用 LangGraph 编排。

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

当前 Loop 的 DDD 对应关系：

- Runtime State：`AgentState`
- Entity：`AgentRun`
- Value：`RunStatus`、`EndReason`
- Application Service：`AgentGraphRunner`
- Flow：`load_context -> call_llm -> execute_tools -> update_memory -> finalize`

FSM、Scheduler、Retry 在当前 MVP 中处于基础形态：

- FSM：由 LangGraph 节点和条件边表达运行状态流转。
- Scheduler：当前以单次请求触发的同步/流式运行调度为主，尚未独立抽象后台任务调度器。
- Retry：当前主要依赖 Provider、HTTP 和测试层的错误传播，不在 Domain 中建模统一重试策略。

## Agent

Agent 负责构造上下文、调用模型、接收模型输出、形成推理和计划的运行状态。当前 Agent 的持久状态与一次运行状态分离。

```text
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

Agent 相关模型：

- 运行状态：`AgentState`
- 运行实体：`AgentRun`
- LLM 端口：`LLMProvider`
- LLM 值对象：`ModelInfo`、`LLMResult`、`LLMEvent`、`LLMEventType`
- 应用服务：`ChatCompletionService`、`AgentGraphRunner`、`ModelService`
- 基础设施实现：`OpenAICompatibleProvider`

Prompt 属于 Application Runtime 上下文，由 `build_system_prompt` 构造，不进入 Domain。Reasoning 和 Planning 当前主要由模型能力、系统提示词和 Loop 中的工具回环共同驱动，尚未抽象为独立 Domain 聚合。

## Memory

Memory 负责会话、消息、工具调用记录、任务状态和摘要的上下文保存与读取。当前持久化核心是 `ConversationSession`，`AgentState` 更像一次运行中的状态聚合，不作为 SQLite 聚合根直接保存。

```text
ConversationSession
  ├── ConversationMessage  1:N
  ├── ToolCall             1:N
  ├── TaskState            1:0..1
  └── Summary              1:0..1
```

Memory 相关模型：

- 聚合根：`ConversationSession`
- 实体：`ConversationMessage`、`ToolCall`、`TaskState`、`Summary`
- 领域端口：`MemoryStore`、`Summarizer`
- 基础设施实现：`SQLiteMemoryStore`、`HeuristicSummarizer`

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

Long-term Memory 当前由 Summary 和历史消息提供基础能力，后续可在 `MemoryStore` 端口下扩展为更完整的长期记忆能力。

## Action

Action 负责把模型意图转换为可执行能力，并记录执行结果。当前应用内落地的是 Tool 能力，Skill 和 Workflow 属于 Harness 体系和后续 Agent Runtime 演进方向。

```text
LLM tool_calls
  └── ToolCallRequest
        ├── ToolService.execute(...)
        ├── ToolExecutor.execute(...)            # 端口
        |     └── CompositeToolExecutor          # 按 tool name 路由
        |           ├── BuiltinToolExecutor      # get_current_time / calculator / list_directory / read_text_file
        |           └── KnowledgeToolExecutor    # search_knowledge -> KnowledgeSearchClient -> N-KB HTTP
        ├── ToolResult
        └── ToolCall persisted by MemoryStore
```

Action 相关模型：

- 实体：`ToolDefinition`、`ToolCallRequest`、`ToolCall`
- 值对象：`RiskLevel`、`ToolResultStatus`、`PermissionDecision`、`ToolResult`
- 领域端口：`ToolExecutor`
- 应用服务：`ToolService`
- 基础设施实现：`CompositeToolExecutor`（路由）、`BuiltinToolExecutor`（内置工具）、`KnowledgeToolExecutor` + `KnowledgeSearchClient`（N-KB 检索工具）

当前 Tool Registry 暴露服务端 safe 工具 schema：内置工具集 + `search_knowledge`（按 `kb_enabled` 动态启用）。模型返回 tool_calls 后由 `ToolService` 统一调度，`CompositeToolExecutor` 按工具名分发到具体 Executor，结果写入 tool message 和 tool_calls 表。`KnowledgeToolExecutor` 通过 `httpx.AsyncClient` 调用 N-KB 的 `POST /retrieval/search`，所有异常归一为 generic ERROR 不向 LLM 泄露细节，详情仅入服务端 logger.warning。N-KB 不属于 N-Agent 领域，仅作为外部独立服务通过 HTTP 端口消费。

Skill 和 Workflow 不属于当前应用源码的 Domain 实体，当前主要存在于 `.harness/` 文档与执行框架中；后续若产品化为 Agent Runtime 能力，应保持与 Tool 相同的端口抽象和权限边界。

## Environment

Environment 负责承载 Runtime 与外部世界的交互边界，包括执行沙箱、浏览器、操作系统、文件系统和网络。

当前 Environment 的落地状态：

- Sandbox：当前主要由 Docker Compose、容器路径和 workspace 根目录约束提供运行边界。
- Browser：当前没有独立浏览器自动化 Runtime，Dashboard 只作为 Interfaces 层的用户界面。
- OS：当前不直接暴露通用 OS 执行能力，仅通过受控内置工具和容器运行环境间接接触。
- FileSystem：当前由内置文件工具围绕 workspace 根目录提供路径安全访问。
- Network：当前主要用于 OpenAI-compatible Provider 调用、N-KB 知识检索 HTTP 调用以及 FastAPI HTTP/SSE 服务。

Environment 不应污染 Domain。所有外部资源访问都应通过 Infrastructure 实现端口，或由 Interfaces 层进行协议适配。

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
- CompositeToolExecutor
- KnowledgeToolExecutor
- KnowledgeSearchClient
```

## 当前边界判断

当前设计符合 DDD 的关键点：

- Runtime 的 Loop、Agent、Memory 和 Action 编排位于 Application 层。
- 领域模型位于 Domain 层，不被 FastAPI、LangGraph、SQLite 或 Provider SDK 污染。
- Application 通过端口依赖 LLM、Memory、Tool 等能力。
- Infrastructure 只负责实现端口和外部资源访问。
- Interfaces 只做协议适配，不直接访问 SQLite 或执行工具。

后续扩展多 Provider、长期 Memory、审批流、多 Agent、自动化任务、Skill/Workflow 产品化或更完整 Environment 能力时，应继续沿用端口抽象和外层依赖内层的方向。

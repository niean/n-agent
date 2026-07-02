# N-Agent

N-Agent 是面向 Open-WebUI、本地调试和多平台入口的 Python Agent Runtime。它通过 FastAPI 提供 OpenAI-compatible API，内部用 LangGraph 编排 Agent Loop，并按 DDD 分层维护模型、工具、记忆、知识库、MCP、Skill、平台网关和沙盒能力。

## 关键能力

- 对话：`/v1/chat/completions` 支持同步与 SSE 流式输出。
- 模型：`LLMProvider` 端口屏蔽 Provider 差异，Dashboard 支持多 Provider CRUD 与 active 热切换。
- 工具：服务端 Tool Registry 管理工具定义、来源、toolset、风险等级和授权上下文。
- 记忆：SQLite 持久化 session、message、tool call、task state、summary；外部记忆按 builtin、multi-project、external-query 三槽管理。
- 知识：`search_knowledge` 通过 Knowledge SPI 检索已注册 N-KB/Ragflow 后端，`kb_id` 必填。
- MCP：MCP site 注册、探测、刷新，并将远端工具同步为本地动态工具定义。
- Skill：本地 `SKILL.md` 包扫描、启停、查看，并通过 `skills_list` / `skill_view` 暴露给 LLM。
- Sandbox：`execute_code` 是 SAFE 工具，无确认卡片；安全边界由 Docker/Local sandbox、workspace 只读、scratch 可写和 callback allowlist 保证。
- Platform/Gateway：CLI、飞书等入口统一映射为 Gateway 会话，再复用 Chat Runtime。
- Dashboard：提供对话、会话、记忆、工具、沙盒、模型、任务、平台和健康视图。

## DDD 分层

```text
Interfaces -> Application -> Domain
Infrastructure -> Domain
```

- Domain：定义 Agent、Session、Tool、Provider、Memory、Knowledge、MCP、Skill、Platform/Gateway、Sandbox 等模型和值对象，以及端口协议。
- Application：编排 Chat、Agent Loop、Prompt、工具调度、Provider 管理、Knowledge/MCP/Skill/Memory/Sandbox 用例。
- Infrastructure：实现 OpenAI-compatible Provider、SQLite registry/store、工具 handler、HTTP/MCP/Feishu/Sandbox adapter。
- Interfaces：提供 FastAPI、OpenAI-compatible API、Dashboard、CLI 和平台协议适配。

Domain 不依赖 FastAPI、LangGraph、SQLite、OpenAI SDK 或具体工具实现；LangGraph 只是 Application 层的 Runtime Loop 实现细节。

## 核心流程

```text
OpenAI API / Dashboard / CLI / Gateway
  -> ChatCompletionService
  -> AgentGraphRunner(load_context -> call_llm -> execute_tools -> update_memory -> finalize)
  -> LLMProvider / ToolService / MemoryStore
  -> ChatCompletion 或 SSE 事件
```

## 运行

Docker Compose 是默认运行方式，服务端口为 `8201`。

```bash
cd docker
docker compose up --build
```

常用配置使用 `N_AGENT_` 前缀：

```env
N_AGENT_PROVIDER_BASE_URL=<openai-compatible-base-url>
N_AGENT_PROVIDER_API_KEY=<api-key>
N_AGENT_PROVIDER_MODEL=<model>
N_AGENT_SQLITE_PATH=/app/locals/sessions.db
N_AGENT_WORKSPACE_ROOT=/workspace
```

CLI 入口：

```bash
n-agent status
n-agent chat "你好"
```

## 文档

- DDD 领域模型：[.harness/knowledge/06-domain-model.md](.harness/knowledge/06-domain-model.md)
- 架构边界：[.harness/knowledge/02-architecture.md](.harness/knowledge/02-architecture.md)
- 数据边界：[.harness/knowledge/04-data-boundaries.md](.harness/knowledge/04-data-boundaries.md)
- 关键模式：[.harness/knowledge/05-key-patterns.md](.harness/knowledge/05-key-patterns.md)
- 文件映射：[.harness/knowledge/22-file-map.md](.harness/knowledge/22-file-map.md)

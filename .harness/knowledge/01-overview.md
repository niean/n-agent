<!-- SUMMARY: N-Agent 是 Python Agent MVP，提供 OpenAI-compatible HTTP API、LangGraph Agent Runtime、SQLite Memory、Tool Registry、Dashboard 和 Docker Compose 部署 -->
# 项目概览

## 一句话

N-Agent 是面向 Open-WebUI 和本地调试的 Python Agent MVP，通过 FastAPI 提供 OpenAI-compatible API，内部使用 LangGraph 编排 Agent Loop，并以 DDD 分层、LLM Adapter、Tool Registry、SQLite Memory 和 Docker Compose 部署作为后续完整 Agent 能力的演进基线。

## 技术栈

- 语言与版本：Python 3.11+
- Web 框架：FastAPI、Uvicorn
- Agent Runtime：LangGraph，位于 Application 层
- 模型适配：OpenAI Python SDK，MVP 默认 OpenAI-compatible Provider
- 配置：pydantic-settings，从 `.env` 和环境变量读取，前缀为 `N_AGENT_`
- 存储：SQLite 标准库，默认容器路径 `/app/locals/sessions.db`
- 测试：pytest、pytest-asyncio、httpx/TestClient
- 部署：Dockerfile + Docker Compose，服务端口当前为 8201

## 入口与根状态

- 应用入口：`app/main.py`，提供 `create_app(settings: Settings | None = None)` 和模块级 `app`
- 依赖组装：`create_app` 组装 Settings、SQLiteMemoryStore、HeuristicSummarizer、OpenAICompatibleProvider、BuiltinToolExecutor、ToolService、AgentGraphRunner、ChatCompletionService、ModelService、SessionService
- HTTP 入口：`app/interfaces/http/openai.py` 提供 `/health`、`/v1/models`、`/v1/chat/completions`
- Dashboard 入口：`/chat`，静态页面位于 `app/interfaces/http/static/dashboard.html`，调试 API 位于 `/chat/sessions`、`/chat/sessions/{session_id}`、`/chat/sessions/{session_id}/tool-calls`
- Agent 根状态：`app/domain/agent.py` 的 `AgentState`，包含 session_id、input_messages、working_messages、pending_tool_calls、tool_results、summary、run_status、iteration_count、error、final_message、finish_reason

## 核心流程

1. 请求进入：Open-WebUI、curl 或 Dashboard 调用 `/v1/chat/completions`。
2. 协议转换：Interfaces 层将 OpenAI-compatible 请求转换为 Application 层 `ChatCompletionInput`。
3. 会话持久化：`ChatCompletionService` 根据 `metadata.session_id`、`X-Session-ID` 或临时 id 创建/绑定会话，并保存用户消息。
4. Agent Loop：`AgentGraphRunner` 通过 LangGraph 执行 `load_context -> call_llm -> execute_tools -> update_memory -> finalize`。
5. 模型调用：`call_llm` 只依赖 Domain `LLMProvider` 端口；当前 Infrastructure 实现为 OpenAI-compatible Provider。
6. 工具执行：Tool Registry 暴露服务端 safe 工具 schema；模型返回 tool_calls 后由 `ToolService` 执行，并写入 tool message 和 tool_calls 表。
7. 记忆更新：SQLite 保存 sessions、messages、tool_calls、task_states、summaries，摘要由 `HeuristicSummarizer` 生成。
8. 响应输出：Application 输出 `ChatEvent` 或非流式结果，Interfaces 编码为 OpenAI-compatible JSON 或 SSE chunk。

## 部署与运行

Docker Compose 是默认运行方式。推荐 `.env` 中使用容器内路径：

```env
COMPOSE_PROJECT_NAME=n-agent
N_AGENT_PROVIDER_BASE_URL=<openai-compatible-base-url>
N_AGENT_PROVIDER_API_KEY=<api-key>
N_AGENT_PROVIDER_MODEL=<model>
N_AGENT_SQLITE_PATH=/app/locals/sessions.db
N_AGENT_WORKSPACE_ROOT=/workspace
N_AGENT_AGENT_ITERATION_LIMIT=5
```

`docker-compose.yml` 当前挂载：

- 宿主机 `/Users/niean/install/n-agent/locals` -> 容器 `/app/locals`
- 宿主机 `/Users/niean/install/n-agent/workspace` -> 容器 `/workspace`

因此 SQLite 文件持久化在宿主机 `locals/sessions.db`，文件工具只能访问宿主机 workspace 对应目录。

## 文档与规则

- 操作约束见 `.harness/framework/FRAMEWORK.md`
- 项目配置见 `.harness/PROJECT.md`
- DDD 架构边界见 `.harness/knowledge/02-architecture.md`
- 数据与存储边界见 `.harness/knowledge/04-data-boundaries.md`
- 文件职责见 `.harness/knowledge/22-file-map.md`

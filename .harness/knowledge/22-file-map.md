<!-- SUMMARY: N-Agent 当前源码、测试、配置、Docker 部署和 Harness 任务文件的职责映射 -->
# 功能与文件映射

## 应用入口与配置

- FastAPI 应用入口：`app/main.py`，提供 `create_app`，组装 Infrastructure 具体实现并注册 HTTP/Dashboard 路由
- 配置模型：`app/config.py`，定义 `Settings`，从 `.env` 和环境变量读取 `N_AGENT_` 配置
- Python 依赖：`pyproject.toml`，定义运行依赖、dev 依赖和 pytest 配置
- 环境变量模板：`.env.example`，不包含真实密钥

## Domain Layer

- Agent 运行模型：`app/domain/agent.py`，定义 `AgentRun`、`AgentState`、`RunStatus`、`EndReason`
- 会话与消息模型：`app/domain/session.py`，定义 `ConversationSession`、`ConversationMessage`、`ToolCall`、`TaskState`、`Summary`
- 工具领域模型：`app/domain/tool.py`，定义 `RiskLevel`、`ToolDefinition`、`ToolCallRequest`、`ToolResult`、`ToolExecutor`
- Provider 领域模型：`app/domain/provider.py`，定义 `ModelInfo`、`LLMEvent`、`LLMResult`、`LLMProvider`
- Memory 端口：`app/domain/memory.py`，定义 `MemoryStore`、`Summarizer`

## Application Layer

- 应用运行事件：`app/application/events.py`，定义 `ChatEvent` 和 `ChatEventType`
- Agent Runtime：`app/application/agent_graph.py`，使用 LangGraph 编排 `load_context`、`call_llm`、`execute_tools`、`update_memory`、`finalize`
- 系统提示词构建：`app/application/prompt_builder.py`，定义 N-Agent 默认 identity、ReAct 指引、安全指引和 `build_system_prompt`
- Chat 用例：`app/application/chat_service.py`，定义 `ChatCompletionInput`、`ChatCompletionResult`、`ChatCompletionService`
- 模型列表用例：`app/application/model_service.py`，定义 `ModelService`
- 会话查询用例：`app/application/session_service.py`，定义 `SessionService`
- 工具服务：`app/application/tool_service.py`，定义 `ToolService` 和 `builtin_tool_definitions`

## Infrastructure Layer

- OpenAI-compatible Provider：`app/infrastructure/llm/openai_compatible.py`，实现 Domain `LLMProvider`
- SQLite MemoryStore：`app/infrastructure/memory/sqlite_store.py`，实现 Domain `MemoryStore`，初始化 schema 和索引
- 启发式摘要器：`app/infrastructure/memory/heuristic_summarizer.py`，实现 Domain `Summarizer`
- 内置工具 handler：`app/infrastructure/tools/builtin.py`，实现时间、计算、目录列表、文本读取和 workspace 路径安全

## Interfaces Layer

- OpenAI-compatible HTTP API：`app/interfaces/http/openai.py`，实现 `/health`、`/v1/models`、`/v1/chat/completions`、OpenAI JSON 和 SSE 编码
- Dashboard API：`app/interfaces/http/dashboard.py`，实现 `/chat`、`/chat/sessions`、`/chat/sessions/{session_id}`、`/chat/sessions/{session_id}/tool-calls`
- Chat 页面：`app/interfaces/http/static/dashboard.html`，全屏聊天 UI，使用 `/v1/chat/completions` 流式接口发送消息，通过会话抽屉管理 session，并通过调试抽屉展示 summary、task state 和 tool calls

## Docker 与部署

- 镜像构建：`Dockerfile`，使用 Python 3.11 slim 镜像，安装项目并以 Uvicorn 启动 8201 端口
- Compose 部署：`docker-compose.yml`，定义 `n-agent` service、可选 `.env`、端口 `8201:8201`、locals/workspace volume
- Docker 构建忽略：`.dockerignore`，排除 `.claude`、`.harness`、`.git`、缓存、venv、locals、workspace
- 本地重建脚本：`start.sh`，执行 Docker Compose down 后重新 build 并后台启动服务
- 本地产物：`locals/`、`.pytest_cache/`、`__pycache__/`、`*.pyc`、`*.egg-info/` 是运行、测试或构建缓存产物，不作为功能文件映射对象

## 测试

- 配置测试：`tests/test_config.py`
- DDD 边界测试：`tests/test_architecture_boundaries.py`
- Docker Compose 配置测试：`tests/test_docker_compose_config.py`
- Domain 模型测试：`tests/domain/test_models.py`
- ToolService 测试：`tests/application/test_tool_service.py`
- AgentGraph 测试：`tests/application/test_agent_graph.py`
- ChatService 测试：`tests/application/test_chat_service.py`
- SQLite store 测试：`tests/infrastructure/test_sqlite_store.py`
- 内置工具测试：`tests/infrastructure/test_builtin_tools.py`
- OpenAI-compatible Provider 测试：`tests/infrastructure/test_openai_compatible_provider.py`
- OpenAI API 测试：`tests/interfaces/test_openai_api.py`
- Dashboard 测试：`tests/interfaces/test_dashboard.py`

## Harness 任务文件

- 设计 spec：`.harness/specs/active/spec-260611-agent-mvp.md`
- 实现 plan：`.harness/plans/active/plan-260611-agent-mvp.md`
- 架构知识：`.harness/knowledge/02-architecture.md`
- 关键模式：`.harness/knowledge/05-key-patterns.md`
- 术语表：`.harness/knowledge/21-glossary.md`

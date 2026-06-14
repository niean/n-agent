<!-- SUMMARY: N-Agent 当前源码、测试、配置、Docker 部署和 Harness 任务文件的职责映射 -->
# 功能与文件映射

## 应用入口与配置

- FastAPI 应用入口：`app/main.py`，提供 `create_app`，组装 Infrastructure 具体实现并注册 HTTP/Dashboard 路由
- 配置模型：`app/config.py`，定义 `Settings`，从 `.env` 和环境变量读取 `N_AGENT_` 配置
- Python 依赖：`pyproject.toml`，定义运行依赖、dev 依赖和 pytest 配置
- 环境变量模板：`.env.example`，不包含真实密钥

## Domain Layer

- Agent 运行模型：`app/domain/agent.py`，定义 `AgentRun`、`AgentState`、`RunStatus`、`EndReason`
- 会话与消息模型：`app/domain/session.py`，定义 `ConversationSession`（`has_default_title` 领域行为、`DEFAULT_SESSION_TITLE` 常量）、`ConversationMessage`、`ToolCall`、`TaskState`、`Summary`、`TitleGenerator` 端口、`SessionNotFoundError`、`SessionValidationError`
- 工具领域模型：`app/domain/tool.py`，定义 `RiskLevel`、`ToolDefinition`、`ToolCallRequest`、`ToolResult`、`ToolExecutor`
- Provider 领域模型：`app/domain/provider.py`，定义 `ModelInfo`、`LLMEvent`、`LLMResult`、`LLMProvider`、`ProviderConfig`、`ProviderRegistry` 端口及 `ProviderNotFoundError`/`DuplicateProviderError`/`ProviderInUseError`/`ProviderValidationError`
- Memory 端口：`app/domain/memory.py`，定义 `MemoryStore`、`Summarizer`

## Application Layer

- 应用运行事件：`app/application/events.py`，定义 `ChatEvent` 和 `ChatEventType`
- Agent Runtime：`app/application/agent_graph.py`，使用 LangGraph 编排 `load_context`、`call_llm`、`execute_tools`、`update_memory`、`finalize`
- 系统提示词构建：`app/application/prompt_builder.py`，定义 N-Agent 默认 identity、ReAct 指引、安全指引和 `build_system_prompt`
- Chat 用例：`app/application/chat_service.py`，定义 `ChatCompletionInput`、`ChatCompletionResult`、`ChatCompletionService`，处理首条用户消息后调用 `SessionService.ensure_title` 触发标题生成
- 模型列表用例：`app/application/model_service.py`，定义 `ModelService`，`default_model` 支持静态字符串或 `Callable[[], str]`（运行时从 ActiveProviderHolder 反射当前 active provider 的 model）
- Provider 管理用例：`app/application/provider_service.py`，定义 `ProviderService`、`ProviderCreateInput`、`ProviderUpdateInput`，封装 list/get/create/update/delete/activate；create 强制 `api_key` 非空，update 中 `api_key=None` 表示不变、`""` 清空、非空覆盖；delete active 抛 `ProviderInUseError`；active 切换或当前条目修改后通过注入的 holder.swap 触发底层 client 重建
- Active provider 适配器：`app/application/runtime_provider.py`，定义 `ActiveProviderHolder`，实现 Domain `LLMProvider` 协议，通过 `Callable[[ProviderConfig, str], LLMProvider]` 工厂懒加载底层 provider 并以 `asyncio.Lock` 保护 swap；`current_model`/`current_config` 暴露当前 active 状态
- 会话查询用例：`app/application/session_service.py`，定义 `SessionService`，注入 `TitleGenerator` 端口并提供 `ensure_title`（仅当会话仍为 `DEFAULT_SESSION_TITLE` 且消息非空时 fire-and-forget 生成）；提供 `rename_session`（trim+长度<=60+不存在抛 `SessionNotFoundError`，空白抛 `SessionValidationError`）和 `delete_session`（端口级联删除，缺失抛 `SessionNotFoundError`）
- 工具服务：`app/application/tool_service.py`，定义 `ToolService`、`builtin_tool_definitions` 和 `knowledge_tool_definitions`

## Infrastructure Layer

- OpenAI-compatible Provider：`app/infrastructure/llm/openai_compatible.py`，实现 Domain `LLMProvider`
- SQLite MemoryStore：`app/infrastructure/memory/sqlite_store.py`，实现 Domain `MemoryStore`，初始化 schema 和索引；`delete_session` 在单次连接内顺序 DELETE messages/tool_calls/task_states/summaries/sessions，返回 sessions 受影响行数 > 0
- 启发式摘要器：`app/infrastructure/memory/heuristic_summarizer.py`，实现 Domain `Summarizer`
- 内置工具 handler：`app/infrastructure/tools/builtin.py`，实现时间、计算、目录列表、文本读取和 workspace 路径安全
- 工具路由 executor：`app/infrastructure/tools/composite.py`，按工具名将 ToolCallRequest 分发给具体 ToolExecutor
- N-KB 知识检索工具：`app/infrastructure/tools/kb.py`，通过 httpx.AsyncClient 调用 N-KB `/retrieval/search` 并映射为 ToolResult
- LLM 标题生成器：`app/infrastructure/session/llm_title_generator.py`，实现 Domain `TitleGenerator`，调用 LLMProvider 一次小请求生成会话标题；`model` 参数支持静态字符串或 `Callable[[], str]` 以反射 active provider
- SQLite Provider Registry：`app/infrastructure/registry/sqlite_provider_registry.py`，实现 Domain `ProviderRegistry`，与 sessions.db 共享 path 但独立 `_connect()`，自带 schema 兜底初始化；`get_secret(id)` 返回明文 api_key，仅供 holder 工厂调用，外部接口一律返回脱敏 `ProviderConfig`

## Interfaces Layer

- OpenAI-compatible HTTP API：`app/interfaces/http/openai.py`，实现 `/health`、`/v1/models`、`/v1/chat/completions`、OpenAI JSON 和 SSE 编码
- Dashboard API：`app/interfaces/http/dashboard.py`，实现 `/`、`/summary`、`/chat`、`/sessions`、`/tools`、`/models`、`/status`（均返回 index.html 外壳，由前端按 pathname 选 tab）、`/chat/sessions`（GET 列表 / POST 创建）、`/chat/sessions/{session_id}`（GET 详情 / PATCH 重命名 / DELETE 级联删除）、`/chat/sessions/{session_id}/tool-calls`、`/chat/tools`（工具只读视图）、`/chat/models`（管理员视角真实模型列表，含 is_default 与 default_model 字段）、`/chat/health/dependencies`（依赖健康聚合，由 main.py 注入 health_provider 回调）、`/chat/providers`*（CRUD + activate）；Provider 与 Session 路由错误统一返回 `JSONResponse {"error": {"code", "message"}}`，session 错误码包括 `session_not_found`(404) 与 `session_title_invalid`(422)，Provider 响应脱敏（不含 api_key），`provider_service` 参数可选未传时不注册 Provider 路由
- Dashboard 入口模板：`app/interfaces/http/static/index.html`，左导（概览/对话/会话/工具/模型/健康）+ Topbar + Tab 容器外壳；通过 `<link rel="icon">` 引用 favicon.svg，依次加载 management-* 共享模块和各 tab 模块
- Dashboard 样式：`app/interfaces/http/static/styles.css`，Design Token + Sidebar/Topbar/Tab/卡片/表格/状态/消息气泡/概览入口卡片 全量样式
- Dashboard favicon：`app/interfaces/http/static/favicon.svg`，蓝底圆角矩形 + NA monogram
- Dashboard 共享模块：`app/interfaces/http/static/management-ui.js`（NAGENT.ui DOM helper）、`management-api.js`（NAGENT.api HTTP 封装，提供 listModels 调 `/v1/models`、getAdminModels 调 `/chat/models`，及 listProviders/createProvider/updateProvider/deleteProvider/activateProvider 操作 `/chat/providers*`）、`management-navigation.js`（NAGENT.navigation sidebar 折叠 + pathname 路径路由 + tab 切换 via history.pushState/popstate）
- Dashboard tab 模块：`app/interfaces/http/static/summary.js`（概览 stats-bar + 入口卡片）、`chat.js`（对话 + SSE 流式 + 会话/调试面板）、`sessions.js`（会话表格 + 详情）、`tools.js`（工具只读表格）、`models.js`（Provider 管理 CRUD + activate + 当前 active provider 真实模型只读列表，调用 `/chat/providers*` 与 `/chat/models`，全 textContent 渲染）、`health.js`（依赖健康 stats-bar + 行项，模块名 NAGENT.status）
- Dashboard 启动入口：`app/interfaces/http/static/app.js`，绑定 NAGENT.app.onTabActivated 调度，调用 NAGENT.navigation.initNavigation

## Docker 与部署

- 镜像构建：`docker/Dockerfile`，使用 Python 3.11 slim 镜像，安装项目并以 Uvicorn 启动 8201 端口
- Compose 部署：`docker/docker-compose.yml`，定义 `n-agent` service、可选根目录 `.env`、端口 `8201:8201`、locals/workspace volume
- Docker 构建忽略：`docker/Dockerfile.dockerignore`，排除 `.claude`、`.harness`、`.git`、缓存、venv、locals、workspace
- 本地重建脚本：`docker/restart-nagent.sh`，在 docker 目录执行 Docker Compose 重建并后台启动服务
- 本地产物：`locals/`、`.pytest_cache/`、`__pycache__/`、`*.pyc`、`*.egg-info/` 是运行、测试或构建缓存产物，不作为功能文件映射对象

## 测试

- 配置测试：`tests/test_config.py`
- DDD 边界测试：`tests/test_architecture_boundaries.py`
- Docker Compose 配置测试：`tests/test_docker_compose_config.py`
- Domain 模型测试：`tests/domain/test_models.py`
- ToolService 测试：`tests/application/test_tool_service.py`
- AgentGraph 测试：`tests/application/test_agent_graph.py`
- ChatService 测试：`tests/application/test_chat_service.py`
- SessionService 测试：`tests/application/test_session_service.py`，覆盖 ensure_title 业务规则（默认会话生成、有自定义标题跳过、无 generator/空消息空操作、生成器异常吞掉）
- Provider Service 测试：`tests/application/test_provider_service.py`，使用 FakeRegistry + FakeHolder 覆盖创建校验、api_key 三态更新、activate 触发 holder.swap、active 删除拒绝、active 更新刷新 holder
- ActiveProviderHolder 测试：`tests/application/test_runtime_provider.py`，覆盖 swap 切换底层 provider、显式 model 优先、未配置时 chat 抛 RuntimeError
- SQLite store 测试：`tests/infrastructure/test_sqlite_store.py`
- Provider Registry 测试：`tests/infrastructure/test_sqlite_provider_registry.py`，覆盖 CRUD + 唯一约束 + active 唯一索引 + get_secret + 缺失 id 处理
- 内置工具测试：`tests/infrastructure/test_builtin_tools.py`
- 工具路由测试：`tests/infrastructure/test_composite_tools.py`
- N-KB 知识检索工具测试：`tests/infrastructure/test_kb_tools.py`
- OpenAI-compatible Provider 测试：`tests/infrastructure/test_openai_compatible_provider.py`
- OpenAI API 测试：`tests/interfaces/test_openai_api.py`
- Dashboard 测试：`tests/interfaces/test_dashboard.py`，覆盖 `/chat` 外壳、`/chat/sessions*`、`/chat/tools`、`/chat/health/dependencies`、`/chat/models`（管理员视角真实模型列表 + is_default + default_model）、`/chat/providers*`（CRUD/activate 全生命周期 + 校验/未找到/重名错误编码）
- Seed Provider 测试：`tests/test_seed_provider.py`，验证空表时按 .env 写入 default 并自动 activate；表非空时跳过 seed；.env 不全时不写入
- 静态资产测试：`tests/interfaces/test_static_assets.py`，校验 /chat 返回 index.html、/static/* 资产可访问、JS 内容关键逻辑（SSE/Shift+Enter/空消息拦截、models.js 调 `/chat/models` 而非 `/v1/models`）和安全文本渲染（无 innerHTML/insertAdjacentHTML，含 textContent）

## Harness 任务文件

- 设计 spec：`.harness/specs/active/spec-260611-agent-mvp.md`
- 实现 plan：`.harness/plans/active/plan-260611-agent-mvp.md`
- 架构知识：`.harness/knowledge/02-architecture.md`
- DDD 领域模型：`.harness/knowledge/06-domain-model.md`
- 关键模式：`.harness/knowledge/05-key-patterns.md`
- 术语表：`.harness/knowledge/21-glossary.md`

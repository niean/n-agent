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
- 工具领域模型：`app/domain/tool.py`，定义 `RiskLevel`、`ToolSourceType`、`ToolDefinition`、`ToolCallRequest`、`ToolExecutionContext`、`ToolResult`、`ToolExecutor`
- MCP 领域模型：`app/domain/mcp.py`，定义 `McpSite`、`McpTool`、`McpRemoteTool`、`McpProbeResult`、`McpSiteRegistry` 端口和 MCP 相关异常
- Gateway 领域模型：`app/domain/gateway.py`，定义 `InteractionSourceType`、`GatewaySessionKey`、`InteractionMessage`、`GatewayOutboundMessage`、`InteractionResponse`、`GatewaySessionLink`、`GatewayConfirmationChoice`、`GatewayConfirmationAction`、`GatewayConfirmationRequest`、`GatewaySessionRegistry` 端口
- Provider 领域模型：`app/domain/provider.py`，定义 `ModelInfo`、`LLMEvent`、`LLMResult`、`LLMProvider`、`ProviderConfig`、`ProviderRegistry` 端口及 `ProviderNotFoundError`/`DuplicateProviderError`/`ProviderInUseError`/`ProviderValidationError`
- Memory 端口：`app/domain/memory.py`，定义 `MemoryStore`、`Summarizer`
- Skill 领域模型：`app/domain/skill.py`，定义 `Skill`、`SkillFrontmatter`、`SkillReadiness`、`SkillRegistry` 端口与 `SkillNotFoundError`/`SkillValidationError` 等异常，纯领域不依赖框架/IO

## Application Layer

- 应用运行事件：`app/application/events.py`，定义 `ChatEvent` 和 `ChatEventType`
- Agent Runtime：`app/application/agent_graph.py`，使用 LangGraph 编排 `load_context`、`call_llm`、`execute_tools`、`update_memory`、`finalize`
- 系统提示词构建：`app/application/prompt_builder.py`，定义 N-Agent 默认 identity、ReAct 指引、安全指引和 `build_system_prompt`
- Chat 用例：`app/application/chat_service.py`，定义 `ChatCompletionInput`、`ChatCompletionResult`、`ChatCompletionService`，处理首条用户消息后调用 `SessionService.ensure_title` 触发标题生成
- Gateway 用例：`app/application/gateway_service.py`，定义 `GatewayService` 和 `GatewayCommandService`，将 CLI/飞书等入口消息映射到稳定 session 并复用 ChatCompletionService、SessionService、ToolService、ModelService；破坏性 Gateway 命令 /new、/rename、/delete、/schedule remove 通过内存 pending confirmation、actor 绑定、15 分钟 TTL 和本会话信任控制执行
- 模型列表用例：`app/application/model_service.py`，定义 `ModelService`，`default_model` 支持静态字符串或 `Callable[[], str]`（运行时从 ActiveProviderHolder 反射当前 active provider 的 model）
- Provider 管理用例：`app/application/provider_service.py`，定义 `ProviderService`、`ProviderCreateInput`、`ProviderUpdateInput`，封装 list/get/create/update/delete/activate；create 强制 `api_key` 非空，update 中 `api_key=None` 表示不变、`""` 清空、非空覆盖；delete active 抛 `ProviderInUseError`；active 切换或当前条目修改后通过注入的 holder.swap 触发底层 client 重建
- Active provider 适配器：`app/application/runtime_provider.py`，定义 `ActiveProviderHolder`，实现 Domain `LLMProvider` 协议，通过 `Callable[[ProviderConfig, str], LLMProvider]` 工厂懒加载底层 provider 并以 `asyncio.Lock` 保护 swap；`current_model`/`current_config` 暴露当前 active 状态
- 会话查询用例：`app/application/session_service.py`，定义 `SessionService`，注入 `TitleGenerator` 端口并提供 `ensure_title`（仅当会话仍为 `DEFAULT_SESSION_TITLE` 且消息非空时 fire-and-forget 生成）；提供 `rename_session`（trim+长度<=60+不存在抛 `SessionNotFoundError`，空白抛 `SessionValidationError`）和 `delete_session`（端口级联删除，缺失抛 `SessionNotFoundError`）
- 工具服务：`app/application/tool_service.py`，定义 `ToolService`、`builtin_tool_definitions` 和 `knowledge_tool_definitions`，支持动态工具定义源和单轮 confirm 授权上下文
- MCP 管理用例：`app/application/mcp_service.py`，定义 `McpService`、MCP 管理工具定义、McpManagementToolExecutor 和 McpToolExecutor，编排站点 CRUD、探测、刷新、动态工具面和远端工具调用解析
- Skill 用例：`app/application/skill_service.py`，定义 `SkillService`、`SkillToolExecutor`、`skill_tool_definitions`（`skills_list`/`skill_view` safe 工具）、`SkillScanReport`、`SkillScanWarning`，编排扫描/列表/查看/启停/刷新；macro 预处理 `${HERMES_SKILL_DIR}`/`${HERMES_SESSION_ID}`，linked file 不预处理；platform 过滤使用 PLATFORM_MAP 映射 `sys.platform`，空 platforms 视为全平台；启用状态在重扫间保留

## Infrastructure Layer

- OpenAI-compatible Provider：`app/infrastructure/llm/openai_compatible.py`，实现 Domain `LLMProvider`
- SQLite MemoryStore：`app/infrastructure/memory/sqlite_store.py`，实现 Domain `MemoryStore`，初始化 schema 和索引；`delete_session` 在单次连接内顺序 DELETE messages/tool_calls/task_states/summaries/sessions，返回 sessions 受影响行数 > 0
- 启发式摘要器：`app/infrastructure/memory/heuristic_summarizer.py`，实现 Domain `Summarizer`
- 内置工具 handler：`app/infrastructure/tools/builtin.py`，实现时间、计算、目录列表、文本读取和 workspace 路径安全
- 工具路由 executor：`app/infrastructure/tools/composite.py`，按工具名将 ToolCallRequest 分发给具体 ToolExecutor
- N-KB 知识检索工具：`app/infrastructure/tools/kb.py`，通过 httpx.AsyncClient 调用 N-KB `/retrieval/search` 并映射为 ToolResult
- LLM 标题生成器：`app/infrastructure/session/llm_title_generator.py`，实现 Domain `TitleGenerator`，调用 LLMProvider 一次小请求生成会话标题；`model` 参数支持静态字符串或 `Callable[[], str]` 以反射 active provider
- SQLite Provider Registry：`app/infrastructure/registry/sqlite_provider_registry.py`，实现 Domain `ProviderRegistry`，与 sessions.db 共享 path 但独立 `_connect()`，自带 schema 兜底初始化；`get_secret(id)` 返回明文 api_key，仅供 holder 工厂调用，外部接口一律返回脱敏 `ProviderConfig`
- SQLite MCP Registry：`app/infrastructure/registry/sqlite_mcp_registry.py`，实现 Domain `McpSiteRegistry`，持久化 mcp_sites/mcp_tools，支持工具刷新保留 enabled 状态和站点删除级联清理
- MCP SDK Client：`app/infrastructure/mcp/sdk_client.py`，实现 Application `McpClient` 协议，使用官方 MCP SDK 进行 streamable_http/SSE/stdio 短连接探测和调用；HTTP 类传输执行 URL 安全校验，stdio 使用 argv 启动本地进程并继承/覆盖环境变量，所有传输共享大小限制
- SQLite Gateway Registry：`app/infrastructure/registry/sqlite_gateway_registry.py`，实现 Domain `GatewaySessionRegistry`，持久化 gateway_conversations、gateway_session_links、gateway_processed_events 并提供事件幂等
- 飞书 Client：`app/infrastructure/feishu/client.py`，封装飞书官方长连接 SDK 事件接收、普通消息/card action 独立校验、allowlist、tenant_access_token 获取、文本发送和 interactive card 发送
- SQLite Skill Registry：`app/infrastructure/registry/sqlite_skill_registry.py`，实现 Domain `SkillRegistry`，持久化 skills 表，重扫保留 enabled 状态
- Skill 文件加载器：`app/infrastructure/skill/file_loader.py`，扫描 `SKILLS_ROOT`（默认 `/workspace/skills`），解析 SKILL.md frontmatter+body，处理 dirname fallback、平台过滤、Hermes 风格 EXCLUDED_SKILL_DIRS，记录 `last_scan_error`
- 路径安全工具：`app/infrastructure/path_security.py`，提供 SKILLS_ROOT 内路径 resolve 与遍历/symlink 拒绝，供 Skill 子系统与内置工具复用
- 调度任务管理工具执行器：`app/infrastructure/tools/schedule_management.py`，实现 `manage_schedule` / `schedule_query` 两个 Agent 工具，按 trusted_metadata 校验 origin（receive_id/receive_id_type/thread_id），dispatch 到 `ScheduleService` create/update/pause/resume/run/list/get；`remove` 短路返回 confirmation_required 文案，引导走 `/schedule remove <id>` 确认卡
- Skill 出厂 seed runner：`app/infrastructure/skill/seed_runner.py`，幂等拷贝 `app/infrastructure/skill/seeds/` 下目录到 `SKILLS_ROOT`，已存在文件不覆盖，OSError 容错只记录 warning
- Skill 出厂模板：`app/infrastructure/skill/seeds/n-agent/SKILL.md`，frontmatter（name=n-agent, tags=[n-agent, manual]）+ "## Cron Jobs / 定时任务" 章节，承载 cron 语法/timezone/prompt 自包含等长知识，避免污染系统提示词

## Interfaces Layer

- OpenAI-compatible HTTP API：`app/interfaces/http/openai.py`，实现 `/health`、`/v1/models`、`/v1/chat/completions`、OpenAI JSON 和 SSE 编码
- CLI 入口：`app/interfaces/cli.py`，提供 `n-agent` console script 与 `python -m app.interfaces.cli`，支持 status、sessions、chat 和交互输入，内部调用 GatewayService；新增 `skill list`/`skill view <name>` 子命令，独立路由仅初始化 SkillService，避免触发 provider/MCP/Feishu/Scheduler 初始化
- 飞书长连接入口：`app/interfaces/feishu_long_connection.py`，接收注入的 Feishu client 长连接事件，处理消息类型过滤、群聊 @ 过滤、actor_id metadata、confirmation interactive card 发送、card action 路由和回复发送，并调用 GatewayService
- Dashboard API：`app/interfaces/http/dashboard.py`，实现 `/`、`/summary`、`/chat`、`/sessions`、`/tools`、`/tools/builtin`、`/tools/knowledge`、`/tools/mcp`、`/tools/skill`、`/tools/plugin`、`/models`、`/status`、`/scheduled-tasks`（均返回 index.html 外壳，由前端按 pathname 选 tab；旧 `/skills` 已废弃）、`/chat/skills`*（GET 列表 / GET 详情 / PATCH enabled / POST refresh，错误码 `skill_not_found`(404) / `skill_invalid`(422) / `skill_scan_failed`(500)，由 main.py 注入 skill_service 后注册）、`/chat/sessions`（GET 列表 / POST 创建）、`/chat/sessions/{session_id}`（GET 详情 / PATCH 重命名 / DELETE 级联删除）、`/chat/sessions/{session_id}/tool-calls`、`/chat/tools`（工具只读视图）、`/chat/models`（管理员视角真实模型列表，含 is_default 与 default_model 字段）、`/chat/health/dependencies`（依赖健康聚合，由 main.py 注入 health_provider 回调）、`/chat/providers`*（CRUD + activate）、`/chat/mcp/sites`*（MCP 站点 list/probe/create/update/delete/refresh/tools/toggle）；Provider、Session 与 MCP 路由错误统一返回 `JSONResponse {"error": {"code", "message"}}`，session 错误码包括 `session_not_found`(404) 与 `session_title_invalid`(422)，MCP 错误码包括 `mcp_site_not_found`、`mcp_site_invalid`、`mcp_probe_failed`、`mcp_refresh_failed`，Provider 响应脱敏（不含 api_key），`provider_service`/`mcp_service` 参数可选未传时不注册对应路由
- Dashboard 入口模板：`app/interfaces/http/static/index.html`，左导（概览/对话/任务/会话/工具(父项)/模型/健康；工具下挂 5 个二级菜单：知识/MCP/Skill/Plugin/内置）+ Topbar + Tab 容器外壳（含 5 个 `tab-tools-*` 子容器）；通过 `<link rel="icon">` 引用 favicon.svg，依次加载 management-* 共享模块和各 tab 模块
- Dashboard 样式：`app/interfaces/http/static/styles.css`，Design Token + Sidebar（含父子菜单：sidebar__group / sidebar__item--parent / sidebar__item-chevron / sidebar__submenu / sidebar__item--child）/Topbar/Tab/卡片/表格/状态/消息气泡/概览入口卡片 全量样式
- Dashboard favicon：`app/interfaces/http/static/favicon.svg`，蓝底圆角矩形 + NA monogram
- Dashboard 共享模块：`app/interfaces/http/static/management-ui.js`（NAGENT.ui DOM helper，含 `el(tag, className)` 创建工厂）、`management-api.js`（NAGENT.api HTTP 封装，提供 listModels 调 `/v1/models`、getAdminModels 调 `/chat/models`，及 listProviders/createProvider/updateProvider/deleteProvider/activateProvider 操作 `/chat/providers*`，及 listSkills/getSkill/setSkillEnabled/refreshSkills 操作 `/chat/skills*`）、`management-navigation.js`（NAGENT.navigation sidebar 折叠 + pathname 路径路由 + tab 切换 via history.pushState/popstate；tabConfig 含父子结构：父项 `tools` 标记 parent: true + children + 无 path，子项标记 parentTab；`/tools` 兜底进入 tools-knowledge）
- Dashboard tab 模块：`app/interfaces/http/static/summary.js`（概览 stats-bar + 入口卡片，ENTRIES 顺序对齐工具子菜单：对话/任务/会话/知识/MCP/Skill/Plugin/内置工具/模型/健康）、`chat.js`（对话 + SSE 流式 + 会话/调试面板）、`sessions.js`（会话表格 + 详情）、`tools.js`（渲染拆为两个根：`tab-tools-builtin` 内 `tools-list` 工具只读表格，`tab-tools-mcp` 内 `mcp-sites-list` MCP 站点管理 / 探测 / 刷新 / 站点工具查看和启停；schema 弹窗共用）、`models.js`（Provider 管理 CRUD + activate + 当前 active provider 真实模型只读列表，调用 `/chat/providers*` 与 `/chat/models`，全 textContent 渲染）、`health.js`（依赖健康 stats-bar + 行项，模块名 NAGENT.status）、`skills.js`（渲染根 `tab-tools-skill`，Skill 列表表格 + view 抽屉 + 启停 + refresh，全 textContent 渲染，仅使用 documented ui helpers）、`knowledge.js`（NAGENT.knowledge，渲染根 `tab-tools-knowledge`：上方 search_knowledge 工具卡片（来源 `/chat/tools` 中 name=search_knowledge）+ 下方 N-KB 依赖卡片（来源 `/chat/health/dependencies.knowledge`），刷新按钮重拉两个数据源，全 textContent + ui.* 白名单）、`plugin.js`（NAGENT.plugin，渲染根 `tab-tools-plugin`：占位卡片"Plugin 子系统待实现"）
- Dashboard 启动入口：`app/interfaces/http/static/app.js`，绑定 NAGENT.app.onTabActivated 调度，调用 NAGENT.navigation.initNavigation；initialized map 含 5 个 `tools-*` 子 tab 键，子 tab 通过 resolveModule 桥接到 tools/skills/knowledge/plugin 模块

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
- MCP Domain 模型测试：`tests/domain/test_mcp_models.py`，覆盖 MCP 站点/工具模型和 stdio 配置字段
- ToolService 测试：`tests/application/test_tool_service.py`
- GatewayService 测试：`tests/application/test_gateway_service.py`，覆盖 session 映射、重复事件幂等、/new、/rename、/delete、/switch、/sessions、/tools、/models、/status、/schedule add/run/remove、破坏性命令确认、actor mismatch、trust scope 和 pending TTL
- McpService 测试：`tests/application/test_mcp_service.py`，覆盖站点探测/创建、动态工具定义、禁用站点阻断、远端工具调用和 MCP 管理工具定义风险等级
- AgentGraph 测试：`tests/application/test_agent_graph.py`
- ChatService 测试：`tests/application/test_chat_service.py`
- SessionService 测试：`tests/application/test_session_service.py`，覆盖 ensure_title 业务规则（默认会话生成、有自定义标题跳过、无 generator/空消息空操作、生成器异常吞掉）
- Provider Service 测试：`tests/application/test_provider_service.py`，使用 FakeRegistry + FakeHolder 覆盖创建校验、api_key 三态更新、activate 触发 holder.swap、active 删除拒绝、active 更新刷新 holder
- ActiveProviderHolder 测试：`tests/application/test_runtime_provider.py`，覆盖 swap 切换底层 provider、显式 model 优先、未配置时 chat 抛 RuntimeError
- SQLite store 测试：`tests/infrastructure/test_sqlite_store.py`
- Provider Registry 测试：`tests/infrastructure/test_sqlite_provider_registry.py`，覆盖 CRUD + 唯一约束 + active 唯一索引 + get_secret + 缺失 id 处理
- Gateway Registry 测试：`tests/infrastructure/test_sqlite_gateway_registry.py`，覆盖 gateway conversation/session link/active session 与 processed event 幂等
- MCP Registry 测试：`tests/infrastructure/test_sqlite_mcp_registry.py`，覆盖 mcp_sites/mcp_tools CRUD、刷新保留 disabled 状态和级联删除
- MCP SDK Client 测试：`tests/infrastructure/test_mcp_sdk_client.py`，覆盖 URL 安全校验、MCP client 限制配置、stdio 分支和环境变量合并
- 内置工具测试：`tests/infrastructure/test_builtin_tools.py`
- 工具路由测试：`tests/infrastructure/test_composite_tools.py`
- N-KB 知识检索工具测试：`tests/infrastructure/test_kb_tools.py`
- 飞书 Client 测试：`tests/infrastructure/test_feishu_client.py`，覆盖长连接普通消息与 card action 的 app/tenant/allowlist 校验、tenant_access_token 缓存、文本发送和 interactive card 发送
- OpenAI-compatible Provider 测试：`tests/infrastructure/test_openai_compatible_provider.py`
- OpenAI API 测试：`tests/interfaces/test_openai_api.py`
- CLI 测试：`tests/interfaces/test_cli.py`，覆盖 status、chat 和 help
- 飞书长连接测试：`tests/interfaces/test_feishu_long_connection.py`，覆盖长连接事件接收、非 text、群聊 @ 过滤、actor_id metadata、confirmation card 发送、card 发送失败撤销 pending、card action 路由、成功回复和 duplicate response
- Dashboard 测试：`tests/interfaces/test_dashboard.py`，覆盖 `/chat` 外壳、`/chat/sessions*`、`/chat/tools`、`/chat/health/dependencies`、`/chat/models`（管理员视角真实模型列表 + is_default + default_model）、`/chat/providers*`（CRUD/activate 全生命周期 + 校验/未找到/重名错误编码）
- MCP Dashboard 测试：`tests/interfaces/test_mcp_dashboard.py`，覆盖 `/chat/mcp/sites*` 站点探测、创建、列表、工具查看、启停、刷新、删除和 stdio payload 输入输出
- Seed Provider 测试：`tests/test_seed_provider.py`，验证空表时按 .env 写入 default 并自动 activate；表非空时跳过 seed；.env 不全时不写入
- MCP Config 测试：`tests/test_mcp_config.py`，验证 MCP timeout、工具数量、schema/result 大小限制环境变量加载
- 静态资产测试：`tests/interfaces/test_static_assets.py`，校验 /chat 返回 index.html、/static/* 资产可访问、JS 内容关键逻辑（SSE/Shift+Enter/空消息拦截、models.js 调 `/chat/models` 而非 `/v1/models`）和安全文本渲染（无 innerHTML/insertAdjacentHTML，含 textContent），含 skills.js 安全性与 ui helper 白名单校验
- Skill Domain 模型测试：`tests/domain/test_skill_models.py`
- Skill Service 测试：`tests/application/test_skill_service.py`，覆盖扫描、列表、view 渲染、enabled 切换、refresh 保留状态
- Skill 工具 Executor 测试：`tests/application/test_skill_tool_executor.py`，覆盖 skills_list/skill_view 安全工具行为、category 过滤、file_path 限制
- SQLite Skill Registry 测试：`tests/infrastructure/test_sqlite_skill_registry.py`
- Skill 文件加载器测试：`tests/infrastructure/test_skill_file_loader.py`，覆盖 frontmatter 解析、dirname fallback、平台过滤、嵌套发现、injection scan 非阻断
- Skill Dashboard 测试：`tests/interfaces/test_skill_dashboard.py`，覆盖 `/chat/skills*` 列表/详情/启停/刷新及 not_found/invalid 错误码
- Skill CLI 测试：`tests/interfaces/test_skill_cli.py` 与 `tests/interfaces/test_skill_cli_isolation.py`，覆盖 list/view 输出与 build_application_services 不被触发
- Skill 装配测试：`tests/test_main_skill_wiring.py`，验证 skill_service 装配并暴露 `skills_list`/`skill_view` 工具

## Harness 任务文件

- 设计 spec：`.harness/specs/active/spec-260611-agent-mvp.md`
- 实现 plan：`.harness/plans/active/plan-260611-agent-mvp.md`
- 架构知识：`.harness/knowledge/02-architecture.md`
- DDD 领域模型：`.harness/knowledge/06-domain-model.md`
- 关键模式：`.harness/knowledge/05-key-patterns.md`
- 术语表：`.harness/knowledge/21-glossary.md`

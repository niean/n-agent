<!-- SUMMARY: N-Agent 的稳定产品需求，包括 OpenAI-compatible API、Chat Dashboard、Agent Runtime、工具、记忆和部署约束 -->
# 产品需求 - 稳定固化(不频繁变更)

## 1. 极简摘要

- 产品：N-Agent -- 本地运行的 OpenAI-compatible Agent 服务，提供对话、工具调用、记忆、知识检索、任务调度、平台接入和调试 Dashboard。
- 用户：本地开发者、Agent 能力验证者、后续扩展开发者。
- 结构：欢迎页 `/`、Chat Dashboard `/chat`、OpenAI-compatible API `/v1/*`、Dashboard 管理页面与调试 API `/chat/*`。
- 核心流程：客户端发送 Chat Completions 请求，Application 层运行 LangGraph Agent Loop，调用 LLM、服务端工具、知识检索和调度/平台能力，写入 SQLite Memory，并以 JSON 或 SSE 返回结果。

产品定位、体验原则与判断准则见 ./01-prd-sense.md。

---

## 2. 页面结构

- HTTP 入口层
    - `/`：欢迎页，展示 N-Agent、Chat、Health、Models 和 Chat Completions 入口。
    - `/health`：健康检查。
    - `/v1/models`：模型列表，对外统一展示 `N-Agent`。
    - `/v1/chat/completions`：OpenAI-compatible Chat Completions 接口，支持非流式与流式响应。
- Chat Dashboard 层
    - `/chat`：全屏聊天页面。
    - 会话抽屉：展示和选择 session，支持重命名和删除。
    - 调试抽屉：展示 summary、task state、tool calls；默认收起，展开时挤压对话区。
    - 输入区：支持发送消息、空消息拦截、Shift+Enter 换行。
    - 左侧导航：按领域展示聊天、任务、工具、模型、平台、观测。
    - 工具二级菜单：知识、MCP、Skill、Plugin、Builtin。
- Dashboard 管理与调试 API 层
    - `GET /chat/sessions`：列出会话。
    - `POST /chat/sessions?session_id=...`：创建会话。
    - `GET /chat/sessions/{session_id}`：查看会话详情、消息、summary、task state。
    - `PATCH /chat/sessions/{session_id}`：更新会话 title。
    - `DELETE /chat/sessions/{session_id}`：删除会话。
    - `GET /chat/sessions/{session_id}/tool-calls`：查看工具调用记录。
    - `/chat/providers*`：管理真实模型 Provider。
    - `/chat/knowledge/*`：管理知识库和知识检索工具。
    - `/chat/mcp/*`：管理 MCP 站点和 MCP 工具。
    - `/chat/skills*`：管理 Skill。
    - `/chat/scheduled-tasks*`：管理定时任务。
    - `/chat/platforms*`：查看平台和平台会话。

---

## 3. 功能需求

### 3.1 OpenAI-compatible HTTP API
- `/v1/chat/completions` 接受 OpenAI Chat Completions 常见字段，包括 model、messages、stream、tools、tool_choice、temperature、max_tokens、metadata。
- 未知字段不得导致请求失败，可忽略或进入 provider options。
- 非流式响应编码为 `chat.completion`。
- 流式响应编码为 `chat.completion.chunk`，以 `data: [DONE]` 结束。
- 对外模型名统一展示为 `N-Agent`，不暴露底层真实模型名。
- 管理员 Dashboard 可查看、创建、编辑、启用和删除真实模型 Provider 配置。
- Provider API Key 只允许脱敏展示。
- Provider 调用失败时，非流式返回 OpenAI-compatible error payload，流式输出 error chunk 后结束。

### 3.2 Chat Dashboard
- `/chat` 页面通过 `/v1/chat/completions` 发送消息，使用 `metadata.session_id` 绑定当前会话。
- 页面应提供接近 Open-WebUI 的基础对话体验，包括消息列表、输入框、发送状态和会话管理。
- 用户未选择会话时，发送消息前应自动创建 session。
- 会话支持 title 重命名和删除。
- 调试信息默认收起，展开时对话区宽度保持稳定。
- Dashboard 弹窗和会话编辑态支持 ESC 退出。
- Dashboard 使用安全文本渲染，不通过拼接 HTML 注入消息内容。
- Dashboard 是本地调试和演示入口，不替代 Open-WebUI。

### 3.3 Agent Runtime
- Agent Runtime 使用 LangGraph 编排 `load_context -> call_llm -> execute_tools -> update_memory -> finalize`。
- `load_context` 从 Memory 读取历史消息和摘要，并注入 Application 层构建的 system prompt。
- `call_llm` 只依赖 Domain `LLMProvider` 端口，不直接依赖具体 Provider SDK。
- `execute_tools` 只执行服务端 Tool Registry 暴露的工具。
- `update_memory` 持久化 assistant 消息、tool 消息、task state 和 summary。
- 达到迭代上限时应 finalize，并记录 last_error。

### 3.4 Tool Registry 与内置工具
- 内置 safe 工具：`get_current_time`、`calculator`、`list_directory`、`read_text_file`。
- 工具按 toolset 管理，包括 builtin、knowledge、mcp、skills、schedule。
- `safe` 工具默认允许执行。
- `confirm` 工具默认拒绝自动执行，返回 `permission_denied`。
- `dangerous` 工具默认不暴露给 LLM，也不可自动执行。
- calculator 只允许安全算术表达式。
- 文件工具必须限制在配置的 workspace 根目录内，拒绝路径穿越和软链接逃逸。
- 工具调用结果必须写入 tool_calls，并可在 Dashboard 调试 API 中查看。
- Tool Schema 可在 Dashboard 弹窗查看。
- `manage_schedule` 和 `schedule_query` 可作为 Agent 管理定时任务的工具。

### 3.5 Memory 与会话
- SQLite 持久化 sessions、messages、tool_calls、task_states、summaries。
- 会话 id 解析优先级为 `X-Session-ID` header、请求体 `metadata.session_id`、自动创建 `tmp-{uuid}`。
- 用户消息、assistant 消息和 tool 消息应按 session 持久化。
- Gateway 平台会话应映射到统一 session，并可在 Dashboard 平台页面查看。
- 删除 session 时应清理关联的任务投递上下文。
- summary 由启发式摘要器生成，后续可替换为模型驱动压缩。
- system prompt 不写入会话消息，也不得通过摘要间接持久化。

### 3.6 知识检索与知识库
- N-Agent 抽象 Knowledge SPI，由知识库后端适配统一检索接口。
- 支持 N-KB、Ragflow 等知识库类型。
- Dashboard 支持知识库增删改查、探测、启停和刷新知识检索工具。
- LLM 通过 `search_knowledge` 工具按 `kb_id` 检索知识库。
- 知识库密钥必须脱敏展示，并避免进入日志、摘要或模型输出。

### 3.7 MCP 站点与远程工具
- Dashboard 支持 MCP 站点增删改查、探测、刷新和工具发现。
- MCP 支持 streamable_http、sse、stdio 传输类型。
- MCP 远程工具应转换为本地 ToolDefinition，并纳入 Tool Registry 管理。
- MCP 工具调用必须保留服务端权限控制和执行上下文约束。

### 3.8 Skill 子系统
- Dashboard 支持 Skill 扫描、列表、详情查看和启停。
- LLM 可通过 `skills_list` 和 `skill_view` 获取可用技能说明。
- Skill 工具属于服务端 Tool Registry 管理范围。
- Plugin 菜单为独立入口，当前仅保留占位能力。

### 3.9 定时任务
- Dashboard 支持定时任务创建、更新、暂停、恢复、删除、立即运行和执行记录查看。
- 定时任务使用 cron 表达式和 timezone 描述调度规则。
- Scheduler Runner 负责领取到期任务、执行 Agent prompt、记录 execution 状态。
- 任务执行需遵守提示词安全扫描和投递上下文约束。
- Agent 可通过 `manage_schedule` 管理定时任务，通过 `schedule_query` 查询可见任务。

### 3.10 平台与 Gateway
- 平台抽象统一纳管 IM 等外部入口，当前支持飞书，并预留钉钉、企业微信。
- 飞书长连接入口将 IM 消息转为 Gateway 事件，再进入统一 Chat/Agent 流程。
- Gateway 会话映射应支持平台 session、thread 和 N-Agent session 的绑定。
- Gateway 命令支持 `/new`、`/rename`、`/delete`、`/schedule` 等会话和任务操作。
- 破坏性 Gateway 命令必须经过确认流程。
- Dashboard 平台页面展示平台状态、配置摘要、会话和最近活动信息。

### 3.11 部署与配置
- 默认运行方式为 Docker Compose。
- 服务端口为 `8201:8201`。
- 容器内 SQLite 路径为 `/app/locals/sessions.db`。
- 容器内 workspace 路径为 `/workspace`。
- 配置通过 `.env` 或环境变量读取，前缀为 `N_AGENT_`。
- `.env.example` 只保留占位值或空值，不写真实密钥。

---

## 4. 非功能与约束

- 平台：Python 3.11+，FastAPI，LangGraph，OpenAI Python SDK，SQLite，Docker Compose。
- 架构：遵循 DDD 分层，Interfaces -> Application -> Domain，Infrastructure 实现 Domain 端口并由应用入口注入。
- 边界：Provider、Knowledge、MCP、Skill、Platform、Gateway、Schedule 均由 Domain 端口和 Application 服务编排，Infrastructure 只做外部系统和存储适配。
- 安全：Provider API Key、知识库密钥和平台密钥只通过环境变量或服务端配置注入，不写入镜像、测试、日志或文档。
- 脱敏：管理员 Dashboard 可展示配置存在性和摘要，不展示完整密钥。
- 数据：SQLite 和 workspace 均通过 Docker Compose volume 持久化到宿主机目录。
- 测试：新增 Domain、Application、Infrastructure、Interfaces 能力时补充对应测试；涉及 DDD 边界时运行边界测试。
- 范围：当前阶段已包含对话、工具、记忆、知识、MCP、Skill、任务、平台、Gateway、调试和部署闭环；仍不提前实现完整多 Agent 编排、远程运行环境或生产级权限系统。

---

## 5. 版本与需求池

- 迭代型需求、Issue 和后续扩展项记录在 ./03-prd-specs.md。
- 实现事实变化先同步 `.harness/knowledge/`，产品意图变化由人工确认后更新 PRD。
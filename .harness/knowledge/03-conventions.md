<!-- SUMMARY: N-Agent 的实现约定，包括 Python/DDD 分层、测试、错误处理、安全、Docker Compose 和文件管理规则 -->
# 约定与约束（实现细节）

本文件是项目实现规范约定的权威来源，`.harness/PROJECT.md` "项目规范"各节为摘要引用，以本文件为准。

---

# 一、编码约定

## Python 与包结构

- Python 版本：3.11+
- 应用包：`app/`
- 测试包：`tests/`
- 源码按 DDD 分层目录组织：`domain/`、`application/`、`infrastructure/`、`interfaces/`
- `app/main.py` 是唯一负责组装 Infrastructure 具体实现的位置
- 新增业务能力时优先在 Domain 定义模型/端口，再由 Application 编排，最后由 Infrastructure/Interfaces 实现外部细节
- 面向用户/LLM/调试输出的 JSON 序列化必须用 `ensure_ascii=False`，保证中文等非 ASCII 字符以原文存储与展示；仅审计日志等需要字节级哈希稳定的场景用 `ensure_ascii=True`（见 `host_terminal_tool_executor.py` 的 safe_reason）

## DDD 依赖方向

- Domain 层禁止 import FastAPI、LangGraph、SQLite、OpenAI SDK 或 `app.infrastructure`
- Application 层禁止 import `app.infrastructure`
- Interfaces 层禁止 import SQLite 和任何 `app.infrastructure` 模块
- Infrastructure 可以 import Domain 端口并实现它们
- Interfaces 只调用 Application services，不直接执行工具 handler，不直接访问 SQLite
- DDD 边界由 `tests/test_architecture_boundaries.py` 静态测试守护

## OpenAI-compatible API

- `/v1/chat/completions` 只作为外部协议适配，不作为内部领域模型
- 请求字段可接受 OpenAI Chat Completions 常见字段；未知字段可忽略或进入 provider options
- 流式输出必须为 `text/event-stream`，使用 `data: {...}` chunk，并以 `data: [DONE]` 结束
- 文本增量输出到 `choices[0].delta.content`
- 工具调用增量输出到 `choices[0].delta.tool_calls`
- 工具执行结果不通过 OpenAI SSE side-channel 暴露，写入内部消息和 tool_calls 后进入下一轮 LLM 输入

## LangGraph

- LangGraph 只存在于 Application 层 `app/application/agent_graph.py`
- 图节点至少包含：`prepare_context`、`call_llm`、`execute_tools`、`update_memory`、`finalize`
- Application 层将运行结果转换为 `ChatEvent`，Interfaces 层只负责 SSE/JSON 编码
- Domain 模型不得暴露 LangGraph 类型

## 工具体系

- Agent 实际可执行工具只来自服务端 Tool Registry
- `ToolDefinition` 只描述能力，不包含 handler；`ToolExecutor` 是执行 SPI，具体实现属于对应支撑子域或 Infrastructure
- 公共 `Policy` 是 Domain Shared Kernel，只统一 `Policy` Protocol、`PolicyOutcome`、`PolicyDecision`；工具规则归 Tool Domain 的 `ToolPolicy`
- `ToolPolicy` 统一治理定义校验、模型暴露、执行允许/拒绝/需审批和一次授权；`RiskLevel` 保留为 `ToolDefinition` 属性，不在 Runner、Interfaces 或 executor 中复制决策规则
- `ToolService` 是强制执行边界：执行前按当前定义复判，不允许调用方绕过；`AgentGraphRunner` 只按 `PolicyDecision` 编排审批
- 工具必须同时注册 `ToolDefinition` 和 `ToolExecutor` 执行路由；只有定义会导致模型可见但调用无法落到实现
- `safe` 默认允许；`confirm` 无授权时要求审批，缺少审批器或审批拒绝时返回 `permission_denied`；`dangerous` 不暴露且拒绝执行
- 内置 safe 工具：`get_current_time`、`calculator`、`list_directory`、`read_text_file`、`web_fetch`
- 文件工具必须通过真实路径解析限制在 workspace 根目录内，拒绝路径穿越和软链接逃逸
- calculator 使用 AST 白名单，只允许安全算术表达式

---

# 二、配置约定

## 环境变量

配置模型位于 `app/config.py`，使用 `N_AGENT_` 前缀：

- `N_AGENT_PROVIDER_BASE_URL`
- `N_AGENT_PROVIDER_API_KEY`
- `N_AGENT_PROVIDER_MODEL`
- `N_AGENT_SQLITE_PATH`
- `N_AGENT_WORKSPACE_ROOT`
- `N_AGENT_AGENT_ITERATION_LIMIT`
- `N_AGENT_WEB_FETCH_ENABLED`
- `N_AGENT_WEB_FETCH_TIMEOUT_SECONDS`
- `N_AGENT_WEB_FETCH_MAX_BYTES`
- `N_AGENT_WEB_FETCH_ALLOW_PRIVATE_URLS`

Docker Compose 项目隔离使用：

- `COMPOSE_PROJECT_NAME=n-agent`

## Docker Compose 默认值

只考虑 Docker Compose 运行时，推荐容器内路径：

- `N_AGENT_SQLITE_PATH=/app/locals/sessions.db`
- `N_AGENT_WORKSPACE_ROOT=/workspace`

宿主机目录通过 `docker/docker-compose.yml` volume 映射到容器路径，避免容器内状态丢失。
本项目面向 Docker Desktop 本地访问时使用端口映射 `8201:8201`，不使用 `network_mode: host`。

## 密钥

- `.env` 可存放本地真实 Provider API Key，但不得提交
- `.env.example` 只保留占位值或空值，不写真实密钥
- 任何日志、测试和文档都不得输出真实 API Key

## 任务安全策略分类（A/B/C）

任务子域安全策略与配置按可配置性分三类，新增策略须先归类后再决定存储与展示方式：

- A 类 安全不变量：代码级安全合同，放宽会破坏安全边界。只读展示，禁止进入配置表、禁止 Dashboard 编辑。包括任务状态机/claim 契约/断路条件逻辑、Worker 安全（工具剥离、Judge 只读、token 不透明、入口来源、执行模式）、审批安全（会话隔离、存在性不泄漏、revise 必填、未知字段拒绝）
- B 类 启动期绑定：在 main.py 装配期注入构造器或控制 lifespan，运行时热改代价高或语义危险。env-only（`N_AGENT_TASK_*`），Dashboard 只读展示，不纳入编辑白名单。包括 task_enabled、task_dispatch_interval_seconds、task_shutdown_grace_seconds
- C 类 运行时可配：运行时调参旋钮，非安全边界。Dashboard 可编辑 + 热重载，经 `TaskConfigProvider.current()` 在使用点查询。包括 task_max_concurrency、task_lease_seconds、task_heartbeat_timeout_seconds、task_max_runtime_seconds、task_goal_max_turns、task_attachment_max_bytes、task_attachment_task_max_bytes、task_failure_limit、note_max_codepoints

分类规则：
- A 类判定标准：放宽该项会削弱防递归自审批、存在性不泄漏、断路器等安全保证；一律不进配置表
- B 类判定标准：消费点在装配期/lifespan，或仅关停时生效；改 env 需重启，不热重载
- C 类判定标准：运行时 per-claim/per-task/per-request 读取，热重载有意义且不触及安全边界
- 跨字段校验（heartbeat<lease、dispatch_interval<lease、attachment_task>=attachment_max、各 int 边界）在 Domain `validate_task_config` 单一来源；DB 覆盖层与 env 启动校验复用同一规则
- 存储采用 SQLite `task_config` 单行逐字段 JSON 覆盖（只存被编辑字段，未编辑字段继续跟随 env），CAS 乐观锁；分层解析 代码默认 -> env -> DB 覆盖 -> per-task 字段

---

# 三、质量约定

## 测试

- 标准化单元测试允许直接在宿主开发环境执行，测试命令：`python -m pytest`
- 所有 E2E 测试必须在 Docker 环境进行，禁止在宿主机直接执行
- 除标准化单元测试 `python -m pytest` 外，禁止在宿主机直接执行 N-Agent Python 代码
- 禁止在宿主机直接启动 N-Agent 服务
- E2E 测试前必须执行 `cd docker && ./restart.sh` 重建并启动服务，后续直接通过 `docker exec n-agent-n-agent-1 n-agent <子命令及参数>` 调用容器内 N-Agent
- E2E 测试禁止依赖宿主 `n-agent` alias，禁止为 `docker exec` 增加 `-i` 或 `-t`
- 当前全量测试覆盖 789 项
- 新增 Domain、Application、Infrastructure、Interfaces 能力时必须补对应测试
- 涉及 DDD 边界变更时必须运行 `tests/test_architecture_boundaries.py`
- 涉及 Docker Compose 变更时必须运行 `tests/test_docker_compose_config.py` 和 `docker compose config`

## 错误处理

- Provider 调用失败：非流式返回 OpenAI-compatible error payload；流式输出 error chunk 后发送 `[DONE]`
- 工具不存在：返回 tool error 结果，允许 Agent 尝试恢复
- 权限拒绝：返回 `permission_denied`，不执行 handler
- 迭代上限：Agent finalize，并写入 task_state last_error
- SQLite 写入失败：当前请求失败，不静默丢状态

## Dashboard 前端

- Dashboard 的提示、错误反馈统一调用共享 `NAGENT.modal.alert(message, options)`，禁止使用浏览器原生 `alert`、`window.alert` 或 `globalThis.alert`；共享实现仅位于 `app/interfaces/http/static/management-ui.js`。
- 需要用户确认的操作统一调用 `NAGENT.modal.confirm(message, options)`，以保持与 Dashboard 其它弹窗一致的样式和交互。
- 新增或修改静态前端模块时，必须保持 `tests/interfaces/test_static_assets.py` 的原生 alert 扫描通过。

## 验收命令

```bash
python -m pytest
docker compose -f docker/docker-compose.yml config >/dev/null
cd docker && ./restart.sh
docker exec n-agent-n-agent-1 n-agent <验收子命令及参数>
```

---

# 四、文件管理约定

- 不主动创建 README，除非用户明确要求
- 不自主删除项目文件
- `locals/`、`logs/`、`data/`、`.pytest_cache/`、`*.pyc`、`*.egg-info/` 是本地运行、测试或构建产物，应由 `.gitignore` 忽略，不需要提交
- `docker/restart.sh` 是本地 Docker Compose 重建辅助脚本
- `docker/docker-compose.yml` 当前在 `.gitignore` 中，修改部署配置前需确认提交边界
- `.harness/prd/` 是 AI-READONLY，不能自动修改
- `.harness/knowledge/` 是实现后知识回填目标，可按 Harness 流程更新

---

# 五、安全约定

- 文件工具默认只能访问配置的 workspace 根目录
- `web_fetch` 是唯一默认内置网络读取工具，仅支持受控 HTTP/HTTPS GET，并阻断 localhost、private IP、link-local、CGNAT、reserved、metadata hostname/IP；公开域名解析到 198.18.0.0/15 benchmark/proxy 网段时允许通过，但直接访问该网段 IP 仍拒绝；不提供 `curl` 或通用 Shell 能力
- Shell、写文件、patch 等工具不属于默认工具集
- Docker Compose 挂载的 workspace 是文件工具边界，不应挂载过大的敏感目录
- Provider API Key 只通过环境变量注入，不写入镜像
- Docker build 可使用国内 PyPI 镜像加速，但不得在镜像层写入密钥

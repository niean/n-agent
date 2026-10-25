<!-- SUMMARY: N-Agent 的实现约定，包括 Python/DDD 分层、测试、错误处理、安全、Docker Compose、文件管理规则、Dashboard 前端（modal/alert/按钮/菜单/时间渲染）和 OTel 可观测（B 类 env-only 配置、DDD 分层边界、敏感属性三重封口、metric-name-mapping 权威表）；2048 字节观测详情 -->
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

OTel 可观测（B 类 env-only，不进 Dashboard 编辑白名单；endpoint 类 URL 禁 userinfo/query/fragment，空 endpoint = local-only 不建网络 exporter）：

观测行为的统一规范见 [07-observability.md](07-observability.md)，包括根 Span 完成日志、默认上下文与脱敏详情、Trace 降噪和信号关联；本文件保留配置与实现约束。

- `N_AGENT_OTEL_ENABLED`、`N_AGENT_OTEL_EXPORTER_OTLP_ENDPOINT`、`N_AGENT_OTEL_TRACES_SAMPLE_RATIO`、`N_AGENT_OTEL_GRAFANA_METRICS_BASE_URL`、`N_AGENT_OTEL_GRAFANA_LOGS_BASE_URL`、`N_AGENT_OTEL_GRAFANA_PROFILE_BASE_URL`、`N_AGENT_OTEL_GRAFANA_TRACES_BASE_URL`（Grafana Explore；旧 `N_AGENT_OTEL_JAEGER_BASE_URL` 仅作新字段为空时的显式兼容回退）
- `N_AGENT_PROFILING_ENABLED`、`N_AGENT_PROFILING_PYROSCOPE_ENDPOINT`、`N_AGENT_PROFILING_INTERVAL_SECONDS`（profiling 独立于 otel_enabled，启用须提供合法 http/https 上传地址）

OTel 实现约定：Domain 零 OTel 依赖；Application 只允许 opentelemetry-api（唯一消费点 `app/application/telemetry_semantics.py`）；SDK/exporter/instrumentation 只允许出现在 `app/infrastructure/observability/`；Interfaces 只消费注入的 snapshot callable。原始敏感属性仍由白名单封口；消息、提示词和工具输入输出仅经固定字段、共享脱敏与 UTF-8 限长后进入详情通道，由 `tests/architecture/test_observability_secret_sentinels.py` 哨兵验证可读正文和凭据遮盖，HTTP 自动 Span 可携带统一投影后的详情；ring buffer/指标/Profile 仍无正文。Prometheus 指标名翻译以 `docker/observability/metric-name-mapping.md` 为权威表，新增 instrument 必须同步。详见 05-key-patterns.md 模式四十六、四十七。

## Docker Compose 默认值

执行摘要：`telemetry_semantics.py` 的 Agent/ChatAttempt/工具完成路径使用固定模板生成 `nagent.summary`，并在 Span 结束前发出 `execution_completed` 日志。`session_id` 和 `request_id`（复用 run_id）经 ContextVar 传播；日志只消费组装根注入的安全快照，不从原始 LogRecord.extra 或消息正文提取。未知 usage 分量保持未知；错误、拒绝、取消独立记录。Collector 将显式枚举的执行字段写入 JSON 行 `context`，不得整体复制任意 attributes。

Trace 降噪：FastAPI 仅采集 `/chat/completions`、`/v1/chat/completions` 两个对话入口（兼容查询参数和尾斜杠），页面、静态资源、管理接口和轮询不生成入站 Span。关闭 ASGI receive/send Span，HTTPX/SQLite 自动 CLIENT Span 仅在有效父操作下采集。HTTP SERVER -> invoke_agent -> LLM/工具/出站 HTTP 继承同一 Trace；CLI 等无 HTTP 父级时 Agent 自然成为根。`_FilteringSpan` 必须实现 OTel API Span 类型，不能仅依赖 duck typing，否则 get_current_span 会返回无效上下文。规则在 `app/infrastructure/observability/instrumentors.py` 维护。

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
- 所有端到端测试必须在 Docker 环境进行，禁止在宿主机直接执行
- 除标准化单元测试 `python -m pytest` 外，禁止在宿主机直接执行 N-Agent Python 代码
- 禁止在宿主机直接启动 N-Agent 服务
- 端到端测试前必须执行 `cd docker && ./restart.sh` 重建并启动服务，后续直接通过 `docker exec n-agent-n-agent-1 n-agent <子命令及参数>` 调用容器内 N-Agent
- 端到端测试禁止依赖宿主 `n-agent` alias，禁止为 `docker exec` 增加 `-i` 或 `-t`
- 前端渲染/布局改动（styles.css、*.js、index.html）的浏览器实测前必须先执行 `sh docker/restart.sh` 重建镜像：`docker/Dockerfile` 用 `COPY app ./app` 烘焙静态资源、`app/` 无 volume 挂载，未重建时容器提供的是旧副本且无任何报错，RED/GREEN 都在测旧代码。实测脚本应先用 `curl -fsS http://localhost:8201/static/<file>` 断言新声明已在容器侧生效，再读取几何值
- 当前全量测试覆盖 7717 项（2026-09-16 `python -m pytest` 收集口径）
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
- Dashboard 操作按钮复用共享 `.btn` / `.btn--primary` / `.btn--danger` 外观；若按钮不在 `13px` 面板标题内，必须显式设为 `var(--font-size-md)`，与任务看板“新增”按钮保持相同文字尺寸，避免依赖正文继承导致视觉偏大。
- 操作入口（按钮/菜单）规范：除左侧导航、顶部导航外，禁止使用 pop 二级菜单（`<details>`/`<summary>` 下拉、hover/click popover、右键菜单等悬浮二级面板）承载操作。操作入口统一用共享 `.btn` 标准按钮直接触发；需要用户确认额外信息（如格式、参数）时，用项目标准 Modal（`modal-backdrop`/`modal-dialog`/`providers-form`，见 `app/interfaces/http/static/management-ui.js` `buildModalFrame`）承载。除非用户明确要求使用二级菜单，否则一律不引入。反例：制品导出曾用 `<details>` 下拉列出格式，已改为标准按钮 + 导出确认 Modal（`app/interfaces/http/static/artifacts.js` `openExportModal`）。
- 详情/信息展示类 Modal 不响应空白处（backdrop）点击，仅经 `modal-close`（×）与 `providers-form__actions` 的关闭按钮显式退出；基准实现见 `app/interfaces/http/static/scheduled-tasks-observations.js` `showDetailModal`。
- 新增或修改静态前端模块时，必须保持 `tests/interfaces/test_static_assets.py` 的原生 alert 扫描通过。
- 前端时间展示统一按 UTC+8（Asia/Shanghai）渲染，不依赖浏览器本地时区。服务端时间统一 UTC 存储（ISO 8601 / RFC 3339，如 `2026-07-28T12:00:00Z`），展示层一律用 `new Date(ms + 8 * 3600 * 1000)` 偏移后取 `getUTCFullYear/getUTCMonth/getUTCDate/getUTCHours/getUTCMinutes/getUTCSeconds` 分量格式化；各静态模块（chat/sandbox/host/observations/browser/tasks/scheduled-tasks/platforms）共用此 `formatTime`/`formatDate` 模式。新增时间渲染必须遵循，禁止用 `getHours()` 等本地时区方法或 `toLocaleString()` 渲染时间戳（`toLocaleString()` 仅用于数字 `formatNumber`）。
- 多入口模式下，相同页面必须使用独立的 `routeConfig` entry（独立 `path`）与独立的左导/顶导菜单配置；只允许共享 renderer 代码，禁止复用路由和菜单。默认禁止单页面多路由别名模式（如 `path-alias` 把 `A/foo` 透明重定向到既有路径 `B/bar`），如需复用须用户明确说明。`topnavParent` / `scope` / `sidebarTab` 按 entry 各自所属左导入口语义独立归类；`path` 必须全局唯一；entry 之间共享 `tab` 与 `renderTab` 仅用于 activeTab 匹配与 renderer 复用，不豁免路由/菜单独立性。**顶导是左导的子域**：顶导 items 归属左导父菜单的子域（每个左导父菜单项对应一份统一顶导），左导入口菜单的对应页面统一增加顶导菜单（已有则不调整），避免顶导脱离左导语义独立存在。典型用例：`/sessions/observations` 与 `/observations/sessions` 共享 observations.js renderer，但分别归 `sessions` / `observations` 子域，顶导与左导按各自 entry 所属子域渲染（详见 `knowledge/05-key-patterns.md` 模式三十九）。

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

## 观测详情限长与出口约定

`N_AGENT_OTEL_CONTENT_MAX_BYTES` 为 B 类启动期 env-only 正整数，默认 2048，重启生效；不纳入 Dashboard/DB 热改配置。上限按每个命名详情项 content 的 UTF-8 字节计算，元数据不计入正文预算，整条日志可大于该值。结构化敏感键/媒体先遮盖，字符串键和值在 JSON 转义前通过共享 sanitizer，最后按 UTF-8 安全截断。未安装或脱敏失败时关闭详情。已投影内容仅验证，不重复脱敏。未知 original_bytes 在 stderr 为 null，在 OTel/Loki 可省略，禁止伪造为 0。

流式临时预算为正文上限四倍，超限/错误/取消保留脱敏限长前缀并标记 truncated/incomplete；无法确定完整长度时 original_bytes 为未知。observed_bytes 表示实际观察字节，HTTP 为传输字节，LLM 为字符串增量；先合并有界原始内容再统一脱敏。Anthropic 非空初始结构化 tool input 增量编码后保留。semantics 静态边界仅精确允许纯 stdlib telemetry_details 依赖，其他业务/SDK 依赖继续拒绝。


## 关联指标、Trace 与日志
业务耗时 histogram 必须在对应 span 仍为 current 时记录，SDK 默认 trace-based exemplar filter 仅关联 sampled span；不得把 trace_id/span_id 加入普通 Metric label 或 Loki 索引 label。Collector 使用 OTLP→Tempo 与 prometheusremotewrite 管线，Prometheus 启用 exemplar-storage；Grafana Tempo 固定 UID nagent-tempo，Loki 保留既有 UID，配置三向关联。Trace 入口为 Grafana Explore，保留反向代理子路径并生成 TraceQL 服务过滤；其他外链保持既有语义。

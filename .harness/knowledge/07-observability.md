<!-- SUMMARY: N-Agent 可观测规范：HTTP 请求响应详情、可读日志和异常、Trace 覆盖与降噪、根 Span 完成日志、实时上下文、流式前缀、字段关联、采样和故障隔离 -->
# 规范可观测行为

观测回答：做了什么、为何结束、哪里慢、消耗多少。本文整理当前实现，以下采集行为以对应观测能力启用为前提；业务费用以 Usage 账本为准。

## 信号与组件

| 信号 | 用途 | 当前组件 |
|---|---|---|
| Trace | 看一次执行及各步骤的关系、耗时 | OTel → Collector → Tempo |
| Log | 看执行现场、输入输出、结束原因和异常 | 应用 stderr；OTel → Collector → Loki |
| Metric | 看调用量、耗时分布、Token 和失败统计 | OTel → Collector → Prometheus |
| Profile | 找进程内的代码热点 | py-spy → Pyroscope |

Grafana 统一展示。信号模型、Trace 上下文和 OTLP 沿用 [OTel](https://opentelemetry.io/docs/concepts/signals/)；组件选型、打点范围和日志要求属于 N-Agent 约定。默认保留有效信息，经统一脱敏和限长后进入 Log 或 Trace；不能把未注册、超限或中断当作整段丢弃的理由。

## Trace：覆盖执行，过滤噪声

```text
HTTP SERVER（对话请求）
├─ 辅助模型调用（如标题生成，发生时记录）
└─ invoke_agent（一次 Agent 执行）
   ├─ chat（每次真实模型请求）→ 出站 HTTP
   ├─ execute_tool（每次工具调用）→ 工具依赖
   └─ nagent.context.compress（上下文压缩）→ 压缩模型调用
```

- 入站 HTTP 只采集 `/chat/completions`、`/v1/chat/completions`，兼容查询参数和尾斜杠。`/static`、页面、健康检查、管理接口和轮询不创建入站 Span。
- 关闭 ASGI receive/send Span。HTTPX/SQLite 自动 CLIENT Span 必须有有效父操作；不为独立后台探测等请求创建孤儿 Span。
- CLI/后台 Agent 没有 HTTP 父级时，`invoke_agent` 自然成为根，这是正常业务 Trace。
- 模型调用和工具调用通常是 Agent 下的兄弟，工具不能挂到已结束的模型 Span 下。真实请求尝试各记一次，不重复打点。
- HTTP、Agent、工具和依赖继承同一 Trace。SSE 断连后继续执行的 Agent 保留原关联，不另建 Trace。
- 谁开启 Span，谁负责结束；完成日志和关联指标先记录，再结束。异常、拒绝、取消分别表达，不伪装成功。

## Log：根 Span 有完成日志，业务日志带现场

| 位置 | 完成日志 | 内容 |
|---|---|---|
| HTTP SERVER Span | `http_request_completed`，每请求一次 | 方法、路由、状态码、耗时、结果；显式关联该 HTTP Span |
| HTTPX 出站 Span | `http_client_completed`，每次传输一次 | 目标主机/端口、脱敏路由模板、代码调用位置、方法、状态、耗时、结果；同时写入该 Span 的 Events 和同 Span ID 的 Log |
| Agent（含 CLI/后台根 Span） | `execution_completed` | 结束原因、消息数、模型/工具调用数、Token、耗时、可读摘要 |
| 模型和工具 | `execution_completed` | 当前模型/工具、调用结果、用量/策略、耗时、可读摘要与详情 |
| 上下文压缩、预算拒绝 | `execution_completed` / `budget_denied` | 压缩前后 Token、耗时、结果与异常；预算拒绝的类别及现场 |

入站 HTTP 流式响应在最终响应体发送后记录完成日志；取消或异常保持真实结果，尚未发响应时不伪造状态码。日志不依赖 Trace 是否采样。补齐日志仅对新请求生效。

出站请求不能只显示一个 GET/POST：名称带方法、目标主机和脱敏路由模板，`nagent.http.caller` 标明应用模块与函数。路径按段处理，保留资源、动作和版本等结构词：`/internal/catalog/status` 原样保留，`/users/alice/messages/123` 变为 `/users/{id}/messages/{id}`。数字、UUID、长十六进制以及用户/文件/会话等资源后的标识模板化；凭据字段后的值、已知密钥和无法安全识别的片段替换为 `{redacted}`，不再抹掉整条未知路径。先解码再判断，模板最多 512 字符。

HTTP 的实际请求与响应写入 `details.http_request/http_response`，包含正文、非敏感查询参数和头；Authorization、Cookie、API Key 等值单独遮盖。HTTPX 按业务实际读取的数据采集，响应读完、关闭、失败或取消后才结束 Span 和完成日志，不为观测预读或消耗流。支持 JSON、文本、表单、SSE、gzip/deflate；multipart 保留文本字段和文件元信息，二进制保留类型与大小。管理写请求即使不创建 Span，也须留下访问日志；静态资源、健康检查和普通管理查询继续降噪。

业务日志默认携带当前可用的上下文和脱敏详情，不能只剩一句事件码：

- 执行现场：会话、请求、入口、当前操作、轮次、模型/工具、消息数、策略与结果等；只填当前阶段实际已知的字段。
- 正文与异常：应用日志默认恢复可读正文，未注册日志使用 `application_log`；普通库日志使用 `library_log`，库的 WARNING/ERROR 使用 `diagnostic_log`。统一脱敏、限长，不因模板未注册而吞掉有效文字和异常堆栈。脱敏器尚未就绪时才退回事件码。
- 详情：Agent 的用户消息与最终回复；模型的实际输入、系统提示词、工具定义和输出；工具的参数与结果；发生错误时的错误详情。
- HTTP 访问日志带自身的请求/响应详情，不继承最后一个子操作的详情。`LogRecord.extra` 的业务字段经投影写入 `details.log_fields`，不能覆盖受信的 Trace/会话身份；不支持的值显示类型说明，不拖累整段有效字段。

实时现场通过 ContextVar 传播，进入子操作时更新，退出时恢复；并发会话不能串号。日志关联发出时活跃的 Span，不把子日志同时挂到全部祖先。

事件按价值治理：

- Policy 审计使用固定模板 `policy_decision %s`，事件为 `policy_decision`；策略、版本、决策、原因、运行和会话等经独立 ContextVar 脱敏后输出为 `policy.*` 字段。不把动态 JSON 注册为模板，也不逐条注册日志正文。
- 启动、迁移、TaskRunner 等稳定业务事件继续注册模板，便于分类；注册不是输出可读日志的前提。
- 仅过滤已被结构化事件替代的 HTTPX/HTTPcore 普通 INFO、Lark 连接成功与 OPA 注册噪声，以及观测自流量。其他有效应用/库日志默认保留；带上下文但没有内容的占位仍过滤。
- WARNING/ERROR 保留来源、级别、当前安全上下文、脱敏正文和异常；外部已有日志 handler 的配置不改写。Profile 失败保留脱敏诊断；导出器失败日志只进 stderr，避免递归进入故障中的导出管线。

### 治理 HTTPX unknown 日志

HTTPX 原始请求返回日志不是模型返回内容。它通常在传输 Span 退出后发出，既可能落到父 Span，也可能因模板未注册而变成 `log_event_unknown`，不能用它代表出站请求完成。

| 情况 | 处理规则 |
|---|---|
| HTTPX 原始 DEBUG/INFO 只有 unknown 与继承的上下文 | 过滤；不为消除 unknown 批量注册第三方模板，不放开原始 URL |
| 有效父操作中的出站请求 | 在自身 Span 结束前各写一次 `http_client_completed` Event 与 Log，携带目标、脱敏路径、调用位置、状态、耗时和结果 |
| HTTPX WARNING/ERROR 或有实际详情的诊断 | 默认保留脱敏正文与异常；稳定事件名可补注册，不以未注册为由折叠内容 |
| 无有效父操作的普通请求、观测后端自流量 | 沿用降噪规则，不为补日志而创建孤儿 Span 或递归记录导出请求 |

过滤重复日志必须同时保证替代信息完整：不能留下只有 GET/POST、没有用途、Event 和 Log 的空壳 Span。出站完成日志带实际 HTTP 请求/响应；模型完成日志另有归一化的 `details.output_messages`。未采样时仍保留完成 Log；CLI 使用临时语义/HTTP 日志桥而不污染交互输出；历史 unknown 不回写。

验收须检查同一请求的 Span、Event、Log 是否关联同一个 Span ID，静态路径是否可读、动态片段和凭据是否遮盖，以及失败、取消、未采样与日志故障是否保持业务行为。实现依据见 `tests/infrastructure/observability/test_http_client_observation.py`、`test_log_event_governance.py` 与 Collector 配置测试。

## 详情：先脱敏，再限长

- 只接受固定详情字段，经 `project_details` 投影。凭据、Cookie、密钥和媒体数据先遮盖，字符串键和值再统一脱敏，最后按 UTF-8 限长。
- `N_AGENT_OTEL_CONTENT_MAX_BYTES` 默认每项 2048 字节；限制单项 `content`，不是整条日志。元数据保留 `original_bytes`、`truncated`，必要时带 `incomplete/observed_bytes`；未知长度保持未知。
- 流式内容有界累积；超限、失败或取消时保留已脱敏的可读前缀，标注 truncated/incomplete/observed_bytes；无法得知完整脱敏长度时 original_bytes 保持未知。跨分片凭据和未闭合密钥值也须遮盖。
- `max_tokens`、`input_tokens` 等数值配置/计数不是凭据，不遮盖。Anthropic 初始工具输入同样保留并脱敏，不因非空而丢弃整次输出。
- 脱敏或投影不可用时给出明确说明。业务 Span 与自动 HTTP Span 可通过同一详情通道携带正文；Metric、Profile、最近 Span 缓冲不带正文。DB Span 保留操作和表名，不采集原始参数。

## 字段与关联

| 来源 | 约定 |
|---|---|
| OTel 通用 | `service.name`、`service.version`、`service.instance.id`；Trace/Span 标识、父关系、时间与状态；Log 时间、级别和正文 |
| [OTel GenAI](https://opentelemetry.io/docs/specs/semconv/gen-ai/) | `gen_ai.operation.name`、Agent/会话、Provider/模型、工具、输入输出 Token、模型结束原因 |
| N-Agent 补充 | `nagent.*`：运行、入口、轮次、执行结束原因、策略、压缩、摘要等项目字段 |

GenAI 采用 `telemetry_semantics.py` 声明的草案依据，变更时集中迁移。OTel 已覆盖的含义不再另起等价名字；脱敏详情走项目的 `nagent.details.*` 通道。

- 相同 `trace_id` 表示同一执行链；子 Span 的 `parent_span_id` 等于直接父 Span 的 `span_id`。日志通常只有当前 `trace_id/span_id`，不额外复制父 ID。
- Grafana 用 Metric exemplar 跳 Trace，用 Trace ID 从 Tempo 查 Loki，再从日志跳回 Tempo。HTTP 根 Span 的日志不包含全部子操作详情，查整条链用 Trace ID。
- 运行、会话、Trace/Span ID 不作普通 Metric 标签或 Loki 索引标签；日志可以用结构化元数据保存这些标识。

## Metric、采样与自检

- Counter 记累计量，Histogram 记每次测量值及分布。指标包括 Agent/模型/工具耗时、调用次数、Token、执行结果、压缩与预算拒绝。
- 指标名称、单位和允许维度集中在 `telemetry_semantics.py`；Prometheus 实际名称以 `docker/observability/metric-name-mapping.md` 为准，新增指标同步维护。
- Histogram 在对应 Span 仍为 current 时记录，供已采样 Span 产生 exemplar。缺失 Token 不伪造为零；费用以 Usage 账本为准。
- Trace 使用 ParentBased + 比例采样，继承父级采样决定；Log/Metric 不因 Trace 未采样而丢弃。
- Dashboard 自检只保留当前进程最近 100 条已结束、被采样 Span 的少量安全字段；每页 10 条，重启清空，不代表完整历史。

## 实现边界与故障隔离

- Domain 不依赖 OTel；Application 通过 `telemetry_semantics.py` 使用 OTel API；SDK、导出器和自动埋点位于 Infrastructure；Interfaces 只消费注入的快照。
- 观测开关、采样、导出地址和详情上限为启动期环境变量；Dashboard 外链覆盖配置可运行时修改。
- 装配幂等，退出统一 flush/shutdown。导出和采样使用有界队列与预算；失败可降级或丢弃观测数据，不能阻断业务或吞掉业务异常。
- 新增打点同时检查字段白名单、脱敏、Collector 投影和自动化验证；日志与 Trace 的字段和用量口径保持一致。

## 实现入口

| 文件 | 职责 |
|---|---|
| `app/application/telemetry_semantics.py` | Span、指标、业务完成日志与实时现场 |
| `app/application/telemetry_details.py` | 固定详情投影、脱敏与限长 |
| `app/infrastructure/observability/instrumentors.py` | 自动埋点、请求范围、HTTP 完成时机 |
| `app/infrastructure/observability/http_capture.py` | 沿实际读写捕获 HTTP 请求/响应，流生命周期、压缩与 multipart 处理 |
| `app/infrastructure/observability/logging.py`、`sanitizer.py` | 双出口日志、安全字段与共享脱敏 |
| `app/infrastructure/observability/setup.py`、`profiling.py` | 装配、采样、自检缓冲、退出和 Profile |
| `docker/observability/` | Collector、后端与 Grafana 关联配置 |

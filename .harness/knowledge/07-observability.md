<!-- SUMMARY: N-Agent 可观测权威规范：六项共性原则、业务入口与异步归属、完成日志、脱敏有界详情、低基数指标与 exemplar、故障隔离、真实 SDK/后端证据和历史数据验收 -->
# 规范可观测行为

观测回答：做了什么、为何结束、哪里慢、消耗多少。“共性规范”和“验收清单”独立于业务领域、项目、语言、框架及后端选型，可直接复用于其他系统；其余章节记录本项目的实现约定。本文是观测规范的权威源，架构、约定、模式及术语文档维护各自边界并引用本文。具体采集行为以对应观测能力启用为前提；本项目业务费用以 Usage 账本为准。

## 共性规范

1. 覆盖完整执行，明确关联归属。观测边界从操作入口延伸至实际完成及必要收尾，不能只有零散子步骤。同步调用准确表达直接父子关系；异步与跨进程操作明确选择延续调用链或建立独立链并记录因果关联。关联上下文与操作归属绑定，在并发、重试和恢复时正确传播、隔离和释放，缺失时明确降级，不借用无关上下文。
2. 信息足以解释，生命周期准确。使用稳定的操作名称和一致的字段语义，保留解释动作、结果和结束原因所必需且允许记录的信息。操作结束前，在对应关联上下文中记录一次完成摘要及相关测量；长连接和流式操作按实际完成、关闭或中断结束。事件模板未注册不应导致有效信息丢失，去除重复或低价值记录时须保留足够的替代信息。
3. 状态与证据真实。区分成功、失败、拒绝、取消、超时和未完成，同时检查异常与返回结果，不用传输成功替代操作成功。未知数值、长度和关联字段保持未知，截断与缺失明确标识。采样记录及指标关联样本不覆盖全部执行，其存在与否不能单独证明链路完整或执行缺失。配置、模拟验证、实际入库和历史完整性分别报告，不伪造结果或补造历史。
4. 数据最小必要，安全与资源有界。按用途选择允许字段并整理成统一结构；敏感内容先脱敏，再按明确的字节预算截断，保留完整性元数据。不同出口分别定义数据权限，使用精确字段白名单，不按命名空间整体放行。指标和索引标签控制基数，不放执行唯一标识或原始载荷。采集、缓冲、排队、传输、重试与清理均设资源和时间上限。
5. 观测故障不改变被观测行为。采集不额外消费数据流，不修改输入输出，不吞掉或改写原有异常和取消语义；观测故障隔离、降级，并保留可用的安全诊断。初始化和释放幂等，资源归属明确，退出在统一总预算内完成必要刷新与清理。部署与迁移先预检，核对实际持久化和保留策略，留存固定样本与回退路径。
6. 分层验证，形成端到端证据。沿“采集 -> 字段整理与脱敏 -> 传输 -> 存储 -> 查询/展示”核对数据、语义与关联的一致性；使用真实组件验证关键接口、生命周期和关联机制，再用实际存储查询证明交付。覆盖正常、失败、取消、并发、采样、恢复和退出场景，测试之间恢复共享状态。服务健康、配置正确或模拟测试通过，均不能代替实际链路及历史数据完整性证据。

## 信号与组件

共享后端使用 `docker/observability/docker-compose.yml`，Compose 项目固定为 `n-observability`。部署/更新：`sh docker/observability/restart.sh`；停止：`sh docker/observability/restart.sh stop`；从旧内嵌后端迁移：`sh docker/observability/restart.sh migrate`（非默认旧项目可传 `--legacy-project`）。观测生命周期入口全部在该目录，业务 `docker/restart.sh` 只管理 Agent/browser。

生命周期操作在 stderr 即时显示阶段进度，Docker 启停输出实时透传；单条命令未结束时每 5 秒提示等待，就绪轮询报告耗时及容器状态，部署完成显示 `compose ps`。内部 JSON 查询保持捕获且不打印展开配置；`compose` 诊断子命令不添加进度文本，保留可供管道处理的原始 stdout。

配置可从同目录 `.env.example` 复制为 `.env`。迁移会核对旧容器真实挂载，复用五个原命名卷和 Pyroscope 三个匿名卷，将八卷映射记录到 `.volumes.json`；该文件须备份，不能丢弃后直接按默认卷名部署。预检失败不改容器/网络/卷；切换失败先停新写入者再恢复旧容器，并清理本次停止残留以允许重试。快照与绑定原卷的恢复override保存在 `locals/harness_tmp/observability-migration-*`，恢复命令见其中 recovery.txt。停止脚本保留数据和网络，禁止使用 down -v。

Agent、Collector、Pyroscope 加入 external bridge `n-telemetry`；后端六服务另有独立私网。应用不直接访问 Prometheus/Loki；诊断命令用 `sh docker/observability/restart.sh compose ...`。Prometheus查询和Loki验证通过观测项目的Prometheus容器执行。Grafana/Tempo/Pyroscope人工访问端口仍只发布到宿主127.0.0.1；不开放宿主OTLP端口。部署脚本重建观测项目以加载只读挂载配置，不触及业务容器。

N-KB 在自身 Compose 中为应用服务追加网络，保留自身原网络及数据库隔离：

```yaml
services:
  n-kb:
    networks: [default, telemetry]
networks:
  telemetry:
    external: true
    name: n-telemetry
```

N-KB 的 OTel exporter 配置指向 `http://otel-collector:4317`（gRPC）或 `http://otel-collector:4318`（HTTP，按其SDK配置路径）；Profile可使用 `http://pyroscope:4040`。保持独立 service.name（如 n-kb），不复制业务方的 COMPOSE_PROJECT_NAME 或观测后端服务定义。共享网络由观测部署入口创建；N-KB首次启动前需确保该网络存在。N-KB专属详情字段仍需其与Collector投影契约对齐。

Pyroscope 使用持久 filesystem bucket `/var/lib/pyroscope` 与 ingester `/data` 双层存储。原 local-only 配置中，168h compactor 保留期不控制本地低磁盘清理；v1.21.1 可用比例计算偏差可能在不足10GiB空间时删除4h前本地块。本次启用 bucket，将低磁盘策略改为有效的非零阈值（1GiB/1%；该版本只接受大于0的覆盖值，不能用0关闭默认策略），保留已上传本地块正常清理与7天bucket保留。迁移发现的一小时旧Profile样本差异及无法恢复的限制记录于 D079，不等同于历史数据全部验证通过。

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
- CLI/ACP 经 `gateway.message`、飞书事件经 `feishu.event -> gateway.message`、后台 Task 经 `task.run` 进入 Agent；业务入口 Span 覆盖准入/编排与收尾，不能仅以 `invoke_agent` 存在或有 exemplar 判断入口完整。Gateway 流式 Span 持续到实际消费结束，关闭向内传递；错误事件区分执行失败与取消。
- 模型调用和工具调用通常是 Agent 下的兄弟，工具不能挂到已结束的模型 Span 下。真实请求尝试各记一次，不重复打点。
- 定时任务在 `ScheduleRunService.run_claim()` 创建 `schedule.execute` 入口 Span，覆盖策略检查、执行记录、Agent 与投递；自动触发和手动运行复用该入口。Agent Span 继承其上下文，投递发生在 Agent Span 结束后、定时任务 Span 结束前。记录 task/claim/session 标识、执行与投递状态及完成摘要，不记录任务 prompt。
- Task 的 `task.run` 包含 worker/Judge 多轮、终态处理及通知；独立后台 Task 使用新 Trace，并通过 Span Link 关联触发者。`skill.review`、`curator.run` 同样使用独立 Trace + Link；Curator 内部 review 保持同 Trace 的父子关系。后台独立操作清理继承的业务现场并在退出时恢复；`clear_run_context()` 本身只清理业务 ContextVars，不代表 OTel Trace 已隔离。
- 委派按所属 delegation 的服务端 traceparent 恢复到 `delegation.member -> invoke_agent`，禁止继承领取 tick 的偶然父级。原始关联随创建事务持久化，重连不覆盖；旧记录、损坏值或关联读取失败创建独立 member 业务根。保留未采样标志，不将 baggage/正文/凭据持久化。固定 source 映射包含 `delegation`。
- 应用入口统一由 `business_operation` 管理完成摘要、失败/取消状态和现场恢复；仅接受固定操作名及标识白名单，不新增高基数指标标签。Tracer 故障回落到有效构造的非记录 Span，不阻断业务。CLI Curator run 通过 `application_runtime` 在成功或异常退出时收尾导出。
- 同一同步执行链中的 HTTP、Agent、工具和依赖继承同一 Trace；独立后台工作的关联按上述 Link 规则处理。SSE 断连后继续执行的 Agent 保留原关联，不另建 Trace。
- 谁开启 Span，谁负责结束；完成日志和关联指标先记录，再结束。异常、拒绝、取消分别表达，不伪装成功。

## 串联 N-Agent 与 N-KB Trace

双方使用 [W3C Trace Context](https://www.w3.org/TR/trace-context/)，通过 HTTP 请求头传播，不修改知识检索 JSON 或响应。N-Agent 由现有 OTel HTTPX 自动埋点先激活 CLIENT Span，再调用全局 propagator 注入；Knowledge adapter 不重复 inject 或创建 CLIENT Span。部署须保持 tracecontext 启用，不使用 `OTEL_PROPAGATORS=none` 或仅 baggage；项目不覆盖全局传播器配置。

```text
invoke_agent（N-Agent）
└─ execute_tool search_knowledge
   └─ HTTP CLIENT POST /retrieval/search（N-Agent）
      └─ HTTP SERVER POST /retrieval/search（N-KB）
         └─ 检索、重排、模型和数据库等内部 Span
```

例如 `traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01`：32 位 trace-id 沿链不变，16 位 parent-id 是 N-Agent 本次 HTTP CLIENT 的 Span ID，flags 最低位表示 sampled。有效的可选 `tracestate` 随标准传播器传递，由 SDK 按规范管理。未采样但有效的上下文仍传播，不能以 `is_recording()` 判断上下文有效性；baggage 是独立机制，本次不新增业务 baggage。

### N-KB 独立实施契约

- 将 `POST /retrieval/search` 纳入入站自动埋点，只创建一个 SERVER Span。不能直接复制 N-Agent 仅采集对话入口的路由过滤，否则检索入口被排除。
- 创建 SERVER 前提取 `traceparent/tracestate`，使用远程上下文作为父级。SERVER 的 trace_id 等于 N-Agent CLIENT 的 trace_id，parent_span_id 等于 CLIENT 的 span_id；检索内部 Span 继承 SERVER。使用对应框架 OTel 埋点，避免手工解析或拼装 ID。
- 使用 ParentBased 采样继承上游决定；无头或 traceparent 无效时按标准创建本地根，不改变检索响应。不用自定义 X-Trace-ID 代替标准父上下文，不以 Trace ID 授予业务权限。
- 使用独立稳定的 `service.name`（建议 `n-kb`），两端汇入同一可查询 Trace 后端，可通过不同 Collector 汇入同一 Tempo；不要求相同语言或 SDK。
- 日志关联当前 trace_id/span_id；N-KB 仍需独立配置日志导出和后端关联。查询、文档正文、凭据不得进入 tracestate/baggage。观测故障不影响业务；异常、取消和请求结束释放上下文，并发不能串链。
- N-Agent 无有效父操作的独立管理 probe 保持降噪，不产生 CLIENT Span/传播头；N-KB 可按自身策略创建本地根，这是预期的独立探测链路。

### 验收边界

`tests/infrastructure/observability/test_nkb_trace_propagation.py` 沿真实 adapter 和生产全局 HTTPX 注册路径，验证请求头、唯一 CLIENT、模拟远程 SERVER 父级、tracestate、采样开关、并发、无父 probe、未注册埋点调用及 HTTP 错误。接收端从空 Context 提取，避免同进程 current context 造成假阳性。这仅证明 N-Agent 出站契约，不代表真实 N-KB 已接通。

双端完成后，从 N-Agent Chat 调用已注册 N-KB，在 Tempo 检查同一 Trace 的两种 service.name 和直接父子关系，再按 Trace ID 查两端日志。未采样场景检查传输头与接收上下文，不要求后端存在 Span。另测无效/缺失头创建本地根，以及错误/取消后新请求不继承旧上下文。人工清单见 `.harness/specs/verify/verify-261029-link-nkb-traces.md`。

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
- exemplar 只覆盖部分测量，不用于逐次执行审计。排查缺失先核对时间窗、指标与服务筛选、采样和导出，再直接查询 Trace；不得为了关联而将执行 ID 加入指标标签。
- Trace 使用 ParentBased + 比例采样，继承父级采样决定；Log/Metric 不因 Trace 未采样而丢弃。
- Dashboard 自检只保留当前进程最近 100 条已结束、被采样 Span 的少量安全字段；每页 10 条，重启清空，不代表完整历史。

## 实现边界与故障隔离

- Domain 不依赖 OTel，可携带不透明的关联元数据；Application 通过 `telemetry_semantics.py` 使用 OTel API；SDK、导出器和自动埋点位于 Infrastructure。接口入口可调用应用层业务观测作用域；Dashboard 自检只消费注入的快照，不访问观测内部实现。
- 观测开关、采样、导出地址和详情上限为启动期环境变量；Dashboard 外链覆盖配置可运行时修改。
- 装配幂等，退出统一 flush/shutdown。导出和采样使用有界队列与预算；失败可降级或丢弃观测数据，不能阻断业务或吞掉业务异常。
- 新增打点同时检查字段白名单、脱敏、Collector 投影和自动化验证；日志与 Trace 的字段和用量口径保持一致。

## 验收清单

| 检查面 | 必须核对的证据 |
|---|---|
| 入口与生命周期 | 真实入口、子步骤、直接父级或因果关联完整；必要收尾仍在观测边界内；完成记录、资源释放与上下文恢复不重复或遗漏 |
| 异步与恢复 | 不同上游并发、异步派生、重试及重启恢复后仍按操作归属关联；缺失关联时明确降级，不继承无关执行的上下文 |
| 状态与数据流 | 正常、异常、错误返回、拒绝、超时、取消和断连分别验证；输入输出与异常传播不变；截断、中断和未知长度明确标识 |
| 信息与安全 | 允许记录的信息足以解释操作；各出口同一操作的标识与语义一致；敏感字段、分片敏感值及各独立出口均有防泄漏验证 |
| 指标与关联 | 实际指标名称、单位、维度、基数和增量符合定义；采样决定与关联标识正确；使用到的跨信号查询链可实测，不要求每次执行都有样本 |
| 运行与隔离 | 故障注入不改变原有行为；重复初始化不泄漏；正常与异常退出均清理；测试恢复全局对象、回调、缓存和配置，检查顺序依赖 |
| 部署与历史 | 切换前后固定样本、实际持久化位置、生效配置及各存储层保留策略可核对；缺口、可恢复范围与回退方式明确 |
| 证据边界 | 区分静态检查、模拟验证、真实组件、实际存储与展示结果；记录步骤、时间窗、断言及限制；未执行或跳过的检查不得记为通过 |

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

Pyroscope旧策略诊断依据：[v1.21.1磁盘统计](https://github.com/grafana/pyroscope/blob/v1.21.1/pkg/util/disk/disk.go)、[ingester策略覆盖](https://github.com/grafana/pyroscope/blob/v1.21.1/pkg/ingester/ingester.go)、[本地清理路径](https://github.com/grafana/pyroscope/blob/v1.21.1/pkg/ingester/retention.go)。历史61分钟桶差异与启动即清理4h前本地块吻合；缺少删除前文件清单/Debug日志，因此标为高置信度诊断，不声称有具体删除事件的直接证据。


## N-KB 专用共享大盘

`docker/observability/grafana/provisioning/dashboards/nkb-overview.json`、`nkb-logs.json`、`nkb-profile.json`沿用现有provider与数据源UID；固定UID为nkb-overview/nkb-logs/nkb-profile。业务13项指标和系统CPU/RSS映射见 `docker/observability/nkb-metric-name-mapping.md`，该表为锁定版本静态依据，不能称为后端实测。service/instance regex多选；Profile仍仅服务级。Collector对service.name=n-kb显式投影固定执行事实及11项nkb.details详情信封，承接N-KB统一脱敏限长的query、有限结果和异常；JSON body构造后清空N-KB日志attributes，避免未知字段经Loki metadata透传。既有N-Agent管线不变。

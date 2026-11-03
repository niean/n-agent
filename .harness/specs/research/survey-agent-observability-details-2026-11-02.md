<!-- SUMMARY: AI Agent 原生观测详细调研：方法、七个核心候选、六个补充项目、许可/部署/OTel 对比、N-Agent 适配及 PoC 验收；2026-11-02，未部署实测 -->
# 调研 AI Agent 原生观测方案：详情报告

调研日期：2026-11-02（Asia/Shanghai）。配套文件：[概述报告](survey-agent-observability-overview-2026-11-02.md)、[GitHub 状态快照](snapshot-agent-observability-2026-11-02.json)。

## 1. 调研方法与可信度

研究问题：哪些方案能以 Agent 的任务、会话、工具与模型调用为基本对象，帮助开发者观察和改进 Agent，同时提供有意义的开源、自托管路径？

本次覆盖 13 个相关项目，重点分析 7 个平台。证据优先级为官方许可证与明确版本约束、官方部署及集成文档、官方仓库功能说明。搜索结果用于发现资料，第三方排行榜、厂商对竞品的比较、社区转述不作为核心结论依据。

本次做了官方资料交叉核查和 GitHub API 状态采集，没有安装平台、发送业务数据、试用云服务、执行浏览器验收或测量性能。下文“已确认”表示官方资料明确记载；“判断”表示本文根据这些事实所作的适配分析；“待验证”表示需部署后的证据。没有把营销描述的性能倍数、客户数、自动诊断效果当作实测结论。

GitHub 快照保存了仓库地址、默认分支、归档状态、最近 push 时间、仓库识别的许可证与 latest release 信息。它不保存完整网页，也不是固定 commit 的源码审计。API 的 latest release 可能是子包：本次 Phoenix 指向 client，LangWatch 指向 MCP；因此正文不把这些 tag 当成主平台版本。选型实施时仍需固定服务端镜像、SDK、语义约定及 Helm chart 的版本。

## 2. 明确产品边界

### 2.1 观测 Agent 与用 Agent 做观测是两件事

本文研究被观测对象是 AI Agent 的平台。自动排查 Kubernetes/Grafana 告警的 SRE Agent 不在主范围内。Agent 编排框架、自带执行 UI 的低代码应用平台、纯评估库，也不直接等同于 Langfuse 的替代品。

### 2.2 六项判断标准

| 维度 | 应回答的问题 | 只有一般 APM 时容易缺失的内容 |
|---|---|---|
| 运行语义 | 哪个 Agent 在哪个任务里做了什么？ | Run/session、模型/工具/检索分类 |
| 结构与因果 | 谁调用谁？并发、委派、重试如何关联？ | 异步上下文、跨服务链、Span Link |
| 内容与用量 | 看到了什么输入？输出、token、费用如何？ | Prompt、tool schema、工具参数/结果、截断标志 |
| 调试 | 能否定位失败步骤并检验修改？ | 轨迹搜索、版本比较、受限重跑、浏览器录制 |
| 质量闭环 | 结果正确吗？修改是否退化？ | 人工标注、数据集、实验、在线/离线评估 |
| 开放与运维 | 谁能部署、修改、迁移？ | 平台源码、许可边界、身份治理、备份与升级 |

“Agent 原生”是上述能力组合，不是产品名称中有 Agent。带 `agent`、`tool` 类型能提升表达，但不能自动发现未埋点的业务行为；自动埋点覆盖模型客户端，也不代表能恢复审批、调度和委派语义。

### 2.3 三层兼容性必须分别验证

1. 传输：OTLP/HTTP、OTLP/gRPC、鉴权和项目路由是否支持。
2. 数据：Trace/Span ID、父关系、events、links、resource、时间与状态是否保留。
3. 语义/UI：Agent、工具、输入输出、token、费用、session 是否识别并可查询。

OpenTelemetry 的 GenAI 约定提供共同基础，但不同产品仍有自身映射，迁移时必须固定约定版本。[OTel GenAI 入口](https://opentelemetry.io/docs/specs/semconv/gen-ai/)、[Langfuse 映射说明](https://langfuse.com/integrations/native/opentelemetry)、[Opik 映射表](https://www.comet.com/docs/opik/integrations/opentelemetry)。

## 3. 比较核心平台

### 3.1 Langfuse：综合工程闭环的基线

已确认：Langfuse 除模型 generation 外，已有 agent、tool、chain、retriever、evaluator、embedding、guardrail 等 observation 类型，支持通过框架集成或手工标注。不能再简单描述为“只看模型调用”。[类型文档](https://langfuse.com/docs/observability/features/observation-types)

开放范围：核心为 MIT，指定 EE 目录单独许可。官方将核心平台与 API 纳入 OSS；项目级角色、Prompt 标签保护、数据保留策略、审计日志、服务端数据遮盖等属于企业扩展。已有客户端脱敏与企业服务端 masking 应分别判断。[LICENSE](https://github.com/langfuse/langfuse/blob/main/LICENSE)、[企业功能边界](https://langfuse.com/self-hosting/license-key)

部署：当前自托管文档以 v4 为主，Web 与 Worker 加 PostgreSQL、ClickHouse、Redis/Valkey、S3/Blob。Compose 适合单机试用，生产需自行管理可用性、存储和备份。不能沿用早期“一个应用加 PostgreSQL”的部署印象。[自托管架构](https://langfuse.com/self-hosting)

OTel 接入：支持 HTTP/JSON 和 HTTP/protobuf，当前文档不支持 OTLP/gRPC。v4 直接接入需要关注 ingestion-version 请求头及旧模型迁移。平台可接收已有 Trace，但内容、类型等字段仍须映射。[接入说明](https://langfuse.com/integrations/native/opentelemetry)

判断：适合作为默认比较基线，尤其适合希望把观测、Prompt、数据集和评估统一起来的团队。对已有 OTel 的 N-Agent，先通过 Collector 对接，比替换全套埋点风险更低。

待验证：现有脱敏详情的呈现、长任务是否及时显示、Link 和委派是否易读、评估数据集所需上下文能否从现有 Trace 提取。不能以支持 observation 类型证明复杂异步图已经完整可用。

### 3.2 Opik：评估驱动的候选

已确认：平台覆盖层级 tracing、自动评估与生产观测，主仓库 Apache-2.0。官方自托管页表示可获得 tracing、evaluation 等功能，但不包含用户管理功能；本地安装被明确定位为非生产方案，生产有 Kubernetes 路径。[仓库](https://github.com/comet-ml/opik)、[自托管说明](https://www.comet.com/docs/opik/self-host/overview)

部署：官方架构包括 Java 后端、Python 评估后端、前端，以及 ClickHouse、MySQL、Redis、S3 兼容对象存储。它是完整平台，但也意味着维护多个数据系统与迁移流程。[架构文档](https://www.comet.com/docs/opik/self-host/architecture)

OTel 接入：当前支持 HTTP，映射 GenAI、OpenInference 及部分框架属性。特别值得注意的是根 Span：如果所有 Span 都有 parent，官方说明可能只存入 Spans，而没有创建 Traces 视图中的 Trace。Collector 只分流部分子 Span 时尤其需要验证。[OTel 文档](https://www.comet.com/docs/opik/integrations/opentelemetry)

判断：如果主要问题是“Agent 经常答错，如何形成持续回归测试”，应优先于单纯看 Trace 的方案进行验证。选型重点应是评估结果能否回指具体运行、样本是否有版本、人工评分是否能校准自动 judge。

待验证：线程/多轮聚合、委派和重试展示、在线评估的成本及采样、开源部署的多人访问隔离、现有入口根 Span 的转换。评估功能多，不代表评估结论天然可信。

### 3.3 Phoenix：轻量起步、分析与评估结合

已确认：Phoenix 覆盖 Trace、评估、Prompt、数据集与实验；建立在 OTel 与 OpenInference 之上。官方自托管页声明无功能门槛，可使用 CLI、容器或 Kubernetes。SQLite 可用于起步，PostgreSQL 可用于生产部署。[产品文档](https://arize.com/docs/phoenix)、[自托管](https://arize.com/docs/phoenix/self-hosting)、[数据库说明](https://arize.com/docs/phoenix/self-hosting/deploying-phoenix)

许可证：主仓库当前为 ELv2，包含限制以托管或受管服务方式向第三方提供实质性功能的条款，因此本文将其列为源码可用候选；OpenInference 是另一个独立项目，不能将其许可证替代主平台许可证。[Phoenix LICENSE](https://github.com/Arize-ai/phoenix/blob/main/LICENSE)、[OpenInference 仓库](https://github.com/Arize-ai/openinference)

判断：对内部使用、希望较快分析 Trace/RAG/工具行为并开展实验的团队，很有吸引力；特别适合已经接受 OpenInference 语义的系统。对于希望把观测平台打包进自己的商业托管产品的团队，需要先处理许可适配，不应默认等同 MIT/Apache 路线。

待验证：已有 `gen_ai.*` 和项目私有正文如何转换为 OpenInference/UI 可识别信息，SQL 存储在目标数据量下的查询与保留成本，跨服务 Span 的显示质量。本次未测 SQLite/PostgreSQL 的规模上限。

### 3.4 Laminar：最值得专门验证 Agent 调试的候选

已确认：核心仓库为 Apache-2.0，提供 OTel tracing、SQL/全文检索、评估与数据集。官方自托管矩阵把 browser session recording、agent debugger、MCP 纳入 OSS；Signals、signal clusters、邮件和 Slack 通知需要许可。[仓库](https://github.com/lmnr-ai/lmnr)、[版本能力矩阵](https://laminar.sh/docs/self-hosting/overview)

其 debugger 的重点是让 coding agent 运行被调试 Agent，读取 Trace、修改代码，再通过缓存缩短重跑。这里的“replay”是有缓存和适配边界的工程流程，不能推断为任意系统状态的确定性恢复。[Debugger 文档](https://laminar.sh/docs/debugger/introduction)

浏览器 Agent 的优势来自把页面状态/会话录制与工具及模型调用关联起来，单纯保存模型文本无法提供同样信息。该能力仍取决于浏览器集成和采集条件。[浏览器观测说明](https://laminar.sh/article/browser-use-observability)

部署：轻量 Compose 有前端、应用服务、PostgreSQL、ClickHouse 和 Quickwit；生产 Helm 再引入 RabbitMQ、Redis 与处理 consumer。虽然产品聚焦，后端并非一个轻量单进程服务。[部署架构](https://laminar.sh/docs/self-hosting/overview)

判断：浏览器 Agent、复杂工具工作流和“改完后快速重跑”的价值可能比 Prompt 管理更突出。N-Agent 若要检验更原生的调试体验，它是与 Langfuse 对照价值最大的候选之一。

待验证：自研 Browser Host 是否能接入录制，录制敏感信息与保留策略，工具副作用是否会被再次执行，缓存命中与失效是否可解释；模型结果缓存不能替代文件、数据库、浏览器环境快照。

### 3.5 LangWatch：观测与模拟用户测试结合

已确认：当前主项目为 Apache-2.0 核心加 Enterprise 扩展，SDK 为 MIT。官方 NOTICE 明确独立许可目录；不能按旧资料认定整个仓库 MIT，也不能按主仓库 Apache 推定企业目录相同。[NOTICE](https://github.com/langwatch/langwatch/blob/main/NOTICE)

Agent Testing 的突出能力是用模拟用户运行多轮会话，并按场景判定是否符合要求；这更接近行为验收，而非只给最终文本打分。例如退款任务既要查订单，也不能承诺不允许的退款。[Agent Testing](https://langwatch.ai/docs/agent-testing/overview)

自托管：官方当前说明免费部署不限制成员、团队和项目；企业许可增加 SSO、RBAC、SCIM、审计及支持。平台通过 OTel 接入，主要使用 ClickHouse、PostgreSQL、Redis 和对象存储，提供 Compose 与 Helm。README 的 Node 单命令起步不应被当作完整生产维护成本。[部署文档](https://langwatch.ai/docs/self-hosting/overview)、[许可说明](https://langwatch.ai/docs/self-hosting/licensing)

判断：如果 Agent 成功与否取决于多轮交互、用户追问、边界约束和工具选择，而不只是单次答案，LangWatch 应进入优先名单。它比只加一个 judge 更接近真实对话流程验证。

待验证：自研运行时如何作为被测 Agent 接入，模拟用户是否覆盖真实用户分布，judge 一致性，场景和提示词版本可追溯性，免费版访问隔离是否满足团队需求。模拟测试通过不代表生产成功率相同。

### 3.6 MLflow：已有工程资产复用与轻量试用

已确认：当前 MLflow GenAI 已覆盖 Agent tracing、评估、Prompt 与实验，不能只把它看作传统模型训练实验库。官方 tracing 文档包含工具/检索/模型调用、session、异步、采样、脱敏及跨服务传播等入口。[GenAI 概览](https://mlflow.org/docs/latest/genai/)、[Tracing 文档](https://mlflow.org/docs/latest/genai/tracing)

OTel 入站从 3.6.0 起支持，服务端需 SQL backend；入口为 `/v1/traces`，通过 `x-mlflow-experiment-id` 路由。当前仅支持 OTLP/HTTP。输出方向也需注意：默认可能采用 `mlflow.*` 属性，GenAI 格式和双写有专门开关。[OTel 入站](https://mlflow.org/docs/latest/genai/tracing/opentelemetry/ingest)、[OTLP 导出](https://www.mlflow.org/docs/latest/genai/tracing/opentelemetry/export/)

判断：如果已有 MLflow，复用团队的实验、身份及运维经验通常比再引入一整套平台更有价值。没有既有资产时，也可用 SQL backend 快速建立比较基线。项目总体知名度不能代替其 Agent 专项体验的验证。

待验证：所需 UI 和在线评估确实属于开源部署而非托管扩展，Experiment 与业务项目/Session 的概念映射，长 Trace 内容和委派关系呈现，权限隔离及持续数据增长后的维护成本。

### 3.7 OpenLIT：OTel 与平台观测优先

已确认：项目采用 Apache-2.0，以 OTel 为基础，当前覆盖 Agent/LLM tracing、评估、guardrails、Prompt/Context、成本与 GPU 观测。官方安装资料给出平台、ClickHouse、OTel Collector 三组件起步方案。[仓库说明](https://github.com/openlit/openlit)、[安装资料](https://github.com/openlit/openlit/blob/main/docs/latest/openlit/installation.mdx)

判断：最值得比较的是它如何连接现有 OTel 管线，以及能否把 Agent 与资源问题放在同一排查流程。已有 Grafana 栈时，OpenLIT 的通用观测能力会有重叠，因此应按新增的 Agent 语义/UI/评估价值决定是否采用。

待验证：项目宣称的框架集成在具体版本上的覆盖、工具异常与取消、Prompt 和评估闭环深度、已有 HTTPX/模型埋点冲突、外部 SDK 的默认正文采集范围。未经实测，不把“支持多种框架”当作“所有复杂链路都自动正确”。

## 4. 汇总部署与接入差异

### 4.1 部署负担

以下为文档所示主要组件，未罗列所有 sidecar；“较轻”仅相对于本表其他平台的入门架构。

| 平台 | 起步及主要存储 | 相对负担判断 | 主要实施风险 |
|---|---|---|---|
| Langfuse | Web/Worker + PG/ClickHouse/Redis/S3 | 较高 | 多存储备份、队列、升级和治理许可 |
| Opik | Java/Python/前端 + MySQL/ClickHouse/Redis/S3 | 较高 | 多服务版本、评估任务和用户管理 |
| Phoenix | 应用 + SQLite；生产可用 PG | 起步较轻 | SQL 扩展、内容容量和许可适配 |
| Laminar | 应用/前端 + PG/ClickHouse/Quickwit；生产增加队列等 | 中高 | 调试环境、检索与录制数据 |
| LangWatch | App/Workers + PG/ClickHouse/Redis/对象存储 | 较高 | 多组件和场景测试执行成本 |
| MLflow | 服务端 + SQL；按功能增加 artifact storage | 起步较轻 | Experiment 映射、生产身份和存储配置 |
| OpenLIT | 平台 + ClickHouse + Collector | 中等 | 与现有 Collector 的职责及 schema 兼容 |

依据分别见上节每个项目的官方部署链接。没有统一硬件/数据量的测量，因此不报内存排名、吞吐排名或月成本数字。

### 4.2 兼容性与开放边界

| 平台 | 官方确认的关键接入点 | 容易误判的边界 |
|---|---|---|
| Langfuse | OTLP/HTTP，自有属性映射及 ingestion 版本 | 能入库不等于 Agent 类型和消息展示正确 |
| Opik | OTLP/HTTP，多种语义映射，根 Span 构建 Trace | 过滤根 Span 可使 Trace 列表缺失 |
| Phoenix | OTel + OpenInference，框架 instrumentors | OpenInference 开源不等于 Phoenix 主平台同许可 |
| Laminar | OTel tracing、OTLP ingress、SDK gRPC exporter | 调试器、录制要额外接入；Signals 另许可 |
| LangWatch | OTel 采集，场景测试另有连接流程 | 企业身份治理不能由免费成员数推导 |
| MLflow | SQL backend 的 OTLP/HTTP，Experiment header | OSS、托管服务和导出属性格式分开核对 |
| OpenLIT | OTel SDK/Collector 与平台存储 | 插件列表不能替代真实版本兼容测试 |

Laminar 的具体 HTTP/gRPC 监听及 ingress 配置应按选定版本单独核实，本次未验证多协议矩阵。其余未列出的协议不推定“不支持”。

## 5. 六个补充项目与不列入主名单的理由

### 5.1 OpenLLMetry / OpenInference：采集层

OpenLLMetry 是 Apache-2.0 的 OTel 扩展集合，可以向现有观测后端导出。OpenInference 提供 AI 语义与框架埋点。二者解决的是“如何产生可理解的数据”，不独立等于带 UI、用户管理、数据集和实验的完整平台。[OpenLLMetry](https://github.com/traceloop/openllmetry)、[OpenInference](https://github.com/Arize-ai/openinference)

它们适合已有后端但缺少模型/框架埋点的团队。N-Agent 已有明确的 application/infrastructure 打点边界，若引入，应选取缺口并避免重复包裹模型调用。

### 5.2 AgentOps：Agent SDK 原生不等于全平台开源

官方仓库定位 Python SDK，覆盖多个 Agent 框架，MIT 许可。它值得参考 Agent 采集和使用体验；本次没有从该公开仓库确认可自由自托管的完整后台，因此未归入与 Langfuse 同层级的完整开源平台。[仓库](https://github.com/AgentOps-AI/agentops)、[LICENSE](https://github.com/AgentOps-AI/agentops/blob/main/LICENSE)

维护信号也应留意：本次 API 快照中最后 push 为 2026-06-25，早于多数主候选。这个信号只意味着要进一步核查维护节奏，不足以认定项目停止维护。

### 5.3 Helicone：网关和模型 API 观测

主仓库许可证为 Apache-2.0，提供 LLM 观测平台。它值得在 API 流量、成本与网关路径中比较；但模型代理只能看到经过代理的请求，自研工具、本地执行、审批和异步调度需要额外关联，不能由代理日志自动推断完整 Agent 轨迹。[仓库](https://github.com/Helicone/helicone)、[LICENSE](https://github.com/Helicone/helicone/blob/main/LICENSE)

这是一项架构可见性判断，不表示 Helicone 完全没有 Session 或应用埋点能力。本次未对其所有高级功能和生产自托管流程做同等深度核验。

### 5.4 Agenta：协作开发与评估候选

Agenta 是构建 Agent 和自动化的协作工作区，值得在 Prompt、实验和团队迭代方向补充比较。当前许可证为 MIT 核心，EE 目录另许可，不应笼统描述为整个仓库均 MIT。[仓库](https://github.com/Agenta-AI/agenta)、[LICENSE](https://github.com/Agenta-AI/agenta/blob/main/LICENSE)

本次把它作为扩展名单，没有声称其 Agent 观测能力弱于所有核心候选；只是未将其完整部署、语义映射及 Agent 调试逐项核验到相同深度。

### 5.5 Weave：工程工具包与后台范围要分开

Weave 的公开仓库为 Apache-2.0 工程工具包。开源 SDK/工具包、商业产品以及后台的可自托管性需要分别核实；本次未确认一条与主候选同等明确的完整自由自托管路径，因此保留为扩展方案。[官方仓库](https://github.com/wandb/weave)

### 5.6 AgentSight：系统层面的补充

AgentSight 面向系统级 Agent 观测，可补充进程与底层行为视角。适用于 coding agent、终端和文件操作的排查与审计。它不能替代 Prompt 版本、数据集、质量评估等平台工作流；Linux/eBPF 与部署权限条件也需评估。[官方仓库](https://github.com/eunomia-bpf/agentsight)

本节有六个主题、七个项目名；完整 GitHub 快照为 13 个仓库，OpenInference 作为语义基础另行核查。LangSmith、Braintrust 等商业平台未因 SDK 或辅助组件开源而进入“完整开源平台”主名单；本文未对其商业能力作全面比较。

## 6. 哪些需求大多数平台仍不能自动满足

### 6.1 可观测执行不等于读取内部思考

平台只能展示模型/运行时允许输出且采集器实际捕获的信息。Trace 能解释调用顺序、上下文和结果，无法保证揭示模型隐藏推理过程。应使用“可观察的决策证据”，而不是把任何 trace viewer 称为完整思维可视化。

### 6.2 任务成功需要业务证据

HTTP 200、模型正常完成、工具无异常，都不证明用户任务完成。退款是否真正成功、文件是否正确生成、权限是否遵守，需要业务状态、工具结果及人工/规则评估。使用 LLM judge 时需要人工校准、保留理由和评分版本，避免把风格评分当正确性。

### 6.3 回放是至少四种能力

| 名称 | 实际含义 | 不应推定的能力 |
|---|---|---|
| Trace 浏览 | 重看历史事件和文本 | 代码重新执行 |
| 浏览器录制回放 | 重看采集到的页面状态 | 恢复远端网站、账号与数据库 |
| LLM 请求重发 | 相似输入再次调用模型 | 输出确定相同、工具副作用复现 |
| 缓存/检查点重跑 | 跳过已缓存步骤或从指定点运行 | 任意进程和外部世界完整还原 |

选择产品时应问清录制范围、缓存粒度、失效条件和副作用约束，而不是仅比较有没有 Replay 按钮。

### 6.4 会话、业务运行与 Trace 不能合并成一个 ID

一次会话可包含多次 Run，一次 Run 也可能因后台任务产生关联 Trace。同步父子关系、异步 Link、重试、委派和进程恢复需要运行时配合。平台的 session 列表不能替代可靠的运行账本。

### 6.5 观测成本与业务账单有差异

费用估算受模型别名、缓存 token、推理 token、供应商折扣和价格表更新影响；观测采样也可能漏掉调用。业务费用应以可核对的使用记录和供应商账单为准。平台成本包括存储、索引、对象数据、备份、评估模型调用及人工维护，不能把免费许可证当成零成本。

## 7. 场景化选型

以下为基于文档的研究推荐，不是基准排名。

| 场景 | 首先比较 | 选择原因 | 可能改变结论的条件 |
|---|---|---|---|
| 通用 Agent 团队 | Langfuse / Opik | 工程闭环与评估均有明确路径 | 身份治理、预算和已有资产 |
| 浏览器/工具 Agent | Laminar / Langfuse | 调试器、录制与类型化轨迹 | 自研浏览器与副作用重跑兼容性 |
| 多轮对话验收 | LangWatch / Opik | 场景模拟与评估流程 | 场景接入和 judge 一致性 |
| 已有 MLflow | MLflow | 复用实验及运维体系 | Agent UI 或特定调试能力不足 |
| 单机快速验证 | Phoenix / MLflow | 相对简单的起步架构 | Phoenix 许可不合适时优先 MLflow |
| 平台/资源观测优先 | OpenLIT + 现有 OTel 后端 | 采集与基础设施观测结合 | 新增 UI/评估价值不明显 |
| 明确要求 MIT/Apache 核心 | Langfuse / Opik / Laminar / LangWatch / MLflow / OpenLIT | 开放核心许可路径明确 | 必需功能实际落在 EE 中 |
| 只想补模型自动埋点 | OpenLLMetry / OpenInference | 不强制更换后端 | 已有埋点重复或语义冲突 |

不建议根据没有测量依据的综合打分选择产品。更可靠的是先确定必须满足的门槛，再用同一批运行与任务评价候选。

## 8. 结合 N-Agent 的具体分析

### 8.1 已有基础与缺口

根据本仓库 `01-overview.md`、`07-observability.md` 及文件映射，项目已使用 LangGraph、自研业务编排、OTel/GenAI 语义和 Grafana 配套观测，已有异步任务、委派、跨服务传播及脱敏有界详情规范。此次未运行服务，因此这些属于文档记录的设计与实现状态，不是本次重新取得的后端验收证据。

现有规范还明确业务费用以 Usage 账本为准、正文走 `nagent.details.*`、默认每项有 2048 字节预算并保留截断/不完整标识。这些是重要集成约束：新 UI 不能把有界内容当成完整输入，也不能自动用部分数据评估整次任务。

判断上的主要增量是：

1. 面向 Agent 的消息、工具和轨迹阅读体验。
2. 对失败轨迹进行人工标注和归类。
3. 从真实失败构建数据集并做版本回归。
4. 对浏览器和复杂工具执行开展受控调试。

### 8.2 推荐的接入结构

```text
N-Agent / N-KB
  └─ 现有 OTel 埋点与 W3C 上下文传播
      └─ 现有 Collector
          ├─ 原观测路径 → Grafana 配套后端
          └─ 独立处理分支 → 语义映射/项目路由 → 候选 Agent 平台

业务 Usage / AgentRun 账本继续作为费用与运行事实来源
评估流程引用 Run、Trace、数据集版本及评分版本
```

这是一份建议结构，没有修改配置，也未验证 Collector 发行版是否已包含所需处理器。

先选择 Langfuse、Opik、Laminar 中的候选，每次只接入一个做比较。Collector 分支应保留 Trace/Span 身份，不改写原始采集链；平台专有类型和属性仅在分支转换。不能为制造“好看”的 Trace 树而改变真实父子关系，或把多个独立根强行拼成一条链。

### 8.3 必须验证的映射契约

| N-Agent 事实 | 目标含义 | 检查方式 |
|---|---|---|
| `invoke_agent` / `chat` / `execute_tool` | Agent / 模型 / 工具类别 | UI 分类与查询同时正确 |
| `gen_ai.*` 模型、token、结束原因 | 模型用量与结束状态 | 与原始 Span、Usage 核对 |
| `nagent.details.*` | 可读输入输出 | 来源、脱敏、截断及不完整标识保留 |
| Run/session/task/delegation 关联 | 运行与会话聚合 | 并发、重试、恢复不串号 |
| parent / links / resource | 直接调用、异步因果、服务区分 | 比较两个后端的原始查询 |
| failed / denied / cancelled / timed_out | 业务结果 | 不被统一显示为成功或一般异常 |

如果平台必须依赖 SDK 才能获得某项高级能力，可在 Infrastructure 层设计适配；不要为 UI 便利把厂商 SDK 引入 Domain。即使追加 SDK，也应复用统一 TracerProvider 生命周期，避免多套全局初始化和模型/HTTPX 重复埋点。

### 8.4 不建议立即替换 Grafana 的原因

Agent 工程平台能改善内容、标注和评估，但服务日志、资源指标、连续 profiling 和跨服务故障诊断仍有独立价值。先验证增量，再决定收敛范围，比迁移全部历史数据更可控。无需为了试用新 UI 重写原有日志、指标或费用账本。

## 9. 建议的 PoC 与验收证据

本节是后续执行方案，本次未执行，不计为测试通过。

### 9.1 使用同一组真实工作负载

| 样本 | 要触发的行为 | 主要验收点 |
|---|---|---|
| 简单对话 | 一次模型调用 | 内容、token、Run 与 Trace 关联 |
| 工具链 | 多次调用，含并行与一次失败 | 工具参数/结果及父关系，无重复 Span |
| SSE | 正常完成、断连与取消 | Span 结束时机，不额外消费数据流 |
| 委派 | 两个 child 并行，含失败成员 | 成员隔离、join 与预算归属 |
| 后台任务 | 提交、重试、进程恢复 | 上游因果与每次尝试可区分 |
| N-KB | 跨服务检索 | 同 Trace、正确 parent 与服务标识 |
| 敏感/长内容 | 凭据、超长文本、工具错误 | 脱敏不泄漏，截断可辨识 |
| 后端故障 | 暂停接收端、限流 | Agent 行为保持，队列/重试有界 |
| 质量回归 | 已知通过和失败的固定任务 | 转数据集、比较版本、回指 Trace |

浏览器任务另加页面录制与缓存重跑，只在无真实业务副作用的环境测试。恢复外部世界和重发工具不是本次调研授权的动作。

### 9.2 硬性门槛与比较项

先检查不能妥协的条件：许可符合用途、完整私有部署路径可用、身份隔离满足团队需求、敏感信息不泄漏、业务故障隔离、Trace 身份及因果不失真。任一不满足，先解决或淘汰，不能靠其他功能加分抵消。

通过门槛后，才比较定位一次真实故障所需操作、评估闭环完成成本、运维负担与团队偏好。若需要数值评分，可预先约定 Agent 调试 30%、评估 25%、现有 OTel 适配 20%、运维 15%、协作 10%；这是建议权重，不是本次已计算结果。

### 9.3 留存可复核结果

每个候选固定镜像、SDK、数据库和语义映射配置，保存真实 Trace ID、查询结果和评估样本版本。人工 UI 验收记录能否读懂，机器查询记录是否完整，两类证据分开。

测量范围至少包含无新分支的基线、启用分支后的响应延迟/TTFT、资源占用、采集/导出丢失量、每日数据增长和 judge 调用费用。样本量、采样率和并发必须一致；没有这些控制条件，就不发布吞吐或资源优劣结论。

## 10. 维护与版本风险

本次 GitHub API 中 13 个仓库均未归档。多数核心候选有近期 push，但 push 可能来自自动化、文档和依赖更新，不能证明修复响应和生产质量。快照还显示 AgentOps 的最近 push 与 Helicone 的 latest release 时间较早；后者仓库仍有更近 push，说明 latest release 本身并不可靠地代表停止维护。详见[原始快照](snapshot-agent-observability-2026-11-02.json)。

生产采用前应另外核查：目标服务端版本发布记录、迁移/回滚说明、近期安全公告、与自己框架版本相关的未解决 issue，以及所需企业功能在该版本中的许可。本文不把“没找到”写成“没有”，也未宣称任何项目已经通过生产稳定性认证。

## 11. 研究结论与边界

如果从零选择综合平台，先用 Langfuse 建立基线，再比较 Opik 的评估流程和 Laminar 的调试体验。若多轮行为测试是首要问题，LangWatch 的场景模拟值得提前；若已有 MLflow，则先验证现有平台；若偏 OTel 和资源观测，则比较 OpenLIT。Phoenix 是重要候选，但应保留其 ELv2 身份。

对 N-Agent，建议目标是增加一个 Agent 工程与质量闭环，而不是为得到更漂亮的 Trace 页面重做已有观测。优先用可撤回的 Collector 分支验证价值，并把脱敏有界内容、异步因果和账本权威列为验收条件。

本报告的关键限制是没有运行层面的产品验证，没有固定 commit 的完整源码审计，也没有对每个云/企业功能逐项计价。它足以缩小 PoC 名单和设计验收；不足以直接证明性能、确定月成本或代替上线前的版本核对。

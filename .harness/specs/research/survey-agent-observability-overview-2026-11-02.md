<!-- SUMMARY: AI Agent 原生开源观测方案调研概述，区分完整平台、采集层与源码可用产品，给出场景选型及 N-Agent 建议；2026-11-02 官方资料核查，未做部署实测 -->
# 调研 AI Agent 原生观测方案：概述报告

调研日期：2026-11-02（Asia/Shanghai）。资料范围：官方文档、官方仓库、许可证和 GitHub API。本文是文档与源码入口层面的选型研究，未部署产品、未做性能基准；“适合”“优先”属于研究判断。详细依据与验证方案见[详情报告](survey-agent-observability-details-2026-11-02.md)。

## 1. 先给结论

值得优先研究的完整开源平台是 Langfuse、Opik、Laminar、LangWatch、MLflow 和 OpenLIT。Phoenix 的功能与自托管能力也值得研究，但其主平台采用 ELv2，应单列为源码可用方案。没有一个产品在复杂 Agent 调试、质量评估、运维观测和部署成本上全面领先。

如果只选择三个做第一轮 PoC，建议 Langfuse、Opik、Laminar：分别验证综合工程闭环、评估能力、Agent 调试体验。这个顺序不是性能排名。若低成本单机试用最重要，可把 MLflow 或 Phoenix 放入首轮；若模拟用户的多轮测试最重要，加入 LangWatch。

## 2. “Agent 原生”到底指什么

应能围绕一次任务观察 Agent、模型调用、工具执行、检索与多轮会话，看到它们的因果关系、输入输出、失败及费用，并把生产失败转成可复现的测试和评估。只有 token/延迟仪表盘，或者只记录模型 API 请求，还不足以满足这个定义。

需要区分三种“原生”：Agent 语义原生、OpenTelemetry 接入原生、评估工作流原生。三者不能互相替代。支持 OTLP 只证明传输入口兼容，不保证现有字段能直接驱动工具卡片、会话聚合和费用展示。[Langfuse 接入文档](https://langfuse.com/integrations/native/opentelemetry)、[Opik 属性映射文档](https://www.comet.com/docs/opik/integrations/opentelemetry)均明确存在语义映射。

## 3. 核心候选一览

以下“负担”为按组件和运维面推断的相对判断，不是资源实测；商业边界以引用页面及最终部署版本为准。

| 方案 | 开放范围 | 最值得看的能力 | 主要取舍 | 推荐场景 |
|---|---|---|---|---|
| [Langfuse](https://github.com/langfuse/langfuse) | MIT 核心，企业扩展另许可 | Agent/tool 等类型化观测，Prompt 与评估工程闭环 | 存储组件多，部分治理能力需许可 | 通用首选基线 |
| [Opik](https://github.com/comet-ml/opik) | Apache-2.0 平台 | Trace、数据集、实验、自动评估 | 多服务栈；开源自托管用户管理有边界 | 质量改进和回归测试优先 |
| [Laminar](https://github.com/lmnr-ai/lmnr) | Apache-2.0 核心，有许可功能 | Agent debugger、浏览器会话录制、SQL 查询 | Signals 与通知不在免费 OSS 内 | 浏览器 Agent、运行轨迹调试 |
| [LangWatch](https://github.com/langwatch/langwatch) | Apache-2.0 核心，企业扩展另许可 | 模拟用户、多轮场景测试与观测结合 | 平台较宽，治理能力需核对版本边界 | 对话 Agent 的行为验收 |
| [MLflow](https://mlflow.org/docs/latest/genai/) | Apache-2.0 | Trace、实验、评估与已有 MLflow 资产统一 | 产品概念较多；注意 OSS 与托管服务差异 | 已有 MLflow 或希望先轻量试用 |
| [OpenLIT](https://github.com/openlit/openlit) | Apache-2.0 | OTel 采集、Agent/LLM 与资源用量观测 | 评估和 Agent UI 是否满足需求需实测 | 平台工程、OTel 兼容优先 |
| [Phoenix](https://arize.com/docs/phoenix) | 主平台 ELv2；OpenInference 独立开源 | OpenInference、Trace、评估及实验 | 非 MIT/Apache 式开放许可 | 内部自托管、RAG/Agent 分析 |

开放范围核查：[Langfuse LICENSE](https://github.com/langfuse/langfuse/blob/main/LICENSE)、[LangWatch NOTICE](https://github.com/langwatch/langwatch/blob/main/NOTICE)、[Phoenix LICENSE](https://github.com/Arize-ai/phoenix/blob/main/LICENSE)。Phoenix 不能因其可免费自托管，就被当作无同类限制的开源平台。

## 4. 容易混淆的项目

| 类型 | 项目 | 如何使用 |
|---|---|---|
| 采集与语义层 | [OpenLLMetry](https://github.com/traceloop/openllmetry)、[OpenInference](https://github.com/Arize-ai/openinference) | 给模型和框架补埋点，再接后端；不等于完整平台 |
| Agent 采集 SDK | [AgentOps](https://github.com/AgentOps-AI/agentops) | 有 Agent 框架集成；SDK 开源不能证明完整后台开源 |
| 网关/API 观测 | [Helicone](https://github.com/Helicone/helicone) | 适合模型请求、成本与网关管理；完整工具因果链仍需应用埋点 |
| 协作工程平台 | [Agenta](https://github.com/Agenta-AI/agenta) | Prompt、实验和评估协作的重要补充候选 |
| 工程工具包 | [Weave](https://github.com/wandb/weave) | 需另外核对完整自托管后台范围 |
| 系统行为观测 | [AgentSight](https://github.com/eunomia-bpf/agentsight) | 观察进程、文件和网络等底层行为，补充语义 Trace |

## 5. 对 N-Agent 的建议

从项目现有文档看，N-Agent 已有 OTel、GenAI 语义、跨服务 Trace 和 Grafana 观测体系。当前最值得补的是 Agent 友好的内容查看、失败归类、标注数据集及评估闭环。此判断基于仓库文档，未重新验收实际运行栈。

建议保留现有运维观测，通过 Collector 将同一批 Span 分发给候选平台；按平台要求做独立语义映射，沿用原 Trace ID。不要直接同时开启多套自动埋点。N-Agent 的 `nagent.details.*` 有脱敏和截断语义，需要专门验证新平台能否正确呈现，不能把切换 endpoint 当作集成完成。

首轮验收应回答五个问题：工具失败能否一眼定位？异步任务和委派是否串对链？现有输入输出字段是否可读且保持脱敏？生产失败能否转为评估集？观测后端不可用是否影响 Agent？

## 6. 必须保留的选型边界

1. Langfuse 免费核心包含主要工程能力，但项目级 RBAC、保留策略、审计、服务端 masking 等有企业许可边界。[官方说明](https://langfuse.com/self-hosting/license-key)
2. Laminar 的 OSS 包含 debugger 和浏览器录制；Signals、邮件和 Slack 通知需要许可。不能用云端演示推定免费版能力。[自托管矩阵](https://laminar.sh/docs/self-hosting/overview)
3. Opik 官方明确，自托管开放 tracing、evaluation 等功能，但用户管理不包含在该范围内。[自托管说明](https://www.comet.com/docs/opik/self-host/overview)
4. 运行轨迹不揭示模型不可见的内部思考；“回放”也可能仅指查看录制、重发单次模型请求或缓存重跑，不代表整个 Agent 环境可确定性恢复。

最终建议：用 Langfuse 建立比较基线，用 Opik 检验评估收益，用 Laminar 检验 Agent 调试收益；根据 PoC 证据决定是否引入其中一个，避免同时长期运维多个功能重叠的平台。

# 指标名称映射 -- T9 S4 锁定证据

2026-10-25，本地观测栈实测锁定（非 spec 示例名）。方法：一次性 python:3.11-slim 容器
（pin opentelemetry-sdk==1.45.1 / opentelemetry-exporter-otlp-proto-grpc==1.45.1）以
telemetry_semantics.py 的 12 个 instrument 名称/单位/维度合成导出到 Collector OTLP 4317，
经 prometheusremotewrite 落 Prometheus 后，用 `/api/v1/label/__name__/values` 与
`/api/v1/series` 读取实际存储名称。

观测栈：otel/opentelemetry-collector-contrib:0.137.0、prom/prometheus:v3.15.1
（--web.enable-remote-write-receiver）。合成 Resource service.name=n-agent-synth；
resource_to_telemetry_conversion.enabled=true 使 service_name 成为 PromQL 可用 label。

## 映射表（12 instrument，Prometheus 端实际名称）

Prometheus 翻译规则实测：`.` 与 `-` 转 `_`；Counter 加 `_total`；Histogram（单位 s）
加 `_seconds` 且不再加 `_total`；`{...}` 括号单位（token/inference_call/tool_call）不产生
后缀。每个 Histogram 导出 `_bucket` / `_sum` / `_count` 三条序列。

| # | Instrument（telemetry_semantics 常量） | 类型/单位 | Prometheus 实际名称 |
|---|---|---|---|
| 1 | gen_ai.client.inference.usage.input_tokens | Counter / {token} | gen_ai_client_inference_usage_input_tokens_total |
| 2 | gen_ai.client.inference.usage.output_tokens | Counter / {token} | gen_ai_client_inference_usage_output_tokens_total |
| 3 | gen_ai.client.inference.operation.input_tokens | Histogram / {token} | gen_ai_client_inference_operation_input_tokens_bucket / _sum / _count |
| 4 | gen_ai.client.inference.operation.output_tokens | Histogram / {token} | gen_ai_client_inference_operation_output_tokens_bucket / _sum / _count |
| 5 | gen_ai.client.inference.duration | Histogram / s | gen_ai_client_inference_duration_seconds_bucket / _sum / _count |
| 6 | gen_ai.execute_tool.duration | Histogram / s | gen_ai_execute_tool_duration_seconds_bucket / _sum / _count |
| 7 | gen_ai.invoke_agent.duration | Histogram / s | gen_ai_invoke_agent_duration_seconds_bucket / _sum / _count |
| 8 | gen_ai.invoke_agent.inference_calls | Histogram / {inference_call} | gen_ai_invoke_agent_inference_calls_bucket / _sum / _count |
| 9 | gen_ai.invoke_agent.tool_calls | Histogram / {tool_call} | gen_ai_invoke_agent_tool_calls_bucket / _sum / _count |
| 10 | nagent.turns | Counter / 1 | nagent_turns_total |
| 11 | nagent.context.compressions | Counter / 1 | nagent_context_compressions_total |
| 12 | nagent.budget.denied | Counter / 1 | nagent_budget_denied_total |

实测存在的裸 Histogram 基名（供 T10 断言 `_bucket/_sum/_count` 后缀时参考）：
- gen_ai_client_inference_operation_input_tokens
- gen_ai_client_inference_operation_output_tokens
- gen_ai_client_inference_duration_seconds
- gen_ai_execute_tool_duration_seconds
- gen_ai_invoke_agent_duration_seconds
- gen_ai_invoke_agent_inference_calls
- gen_ai_invoke_agent_tool_calls

## 维度（labels）实测

Counter 示例（nagent_turns_total / gen_ai_client_inference_usage_input_tokens_total）：
`service_name`、`service_instance_id`、`service_version`、`job`、`instance`、
`telemetry_sdk_*`，以及 instrument 允许维度（如 gen_ai_request_model、
gen_ai_provider_name、gen_ai_token_modality、nagent_source、nagent_end_reason）。
维度名同样按 `.` -> `_` 翻译（nagent.source -> nagent_source）。

### 2026-10-31：AgentRun 来源维度

三个既有 histogram（gen_ai.invoke_agent.duration / inference_calls / tool_calls）增加
`nagent.source`，落库标签为 `nagent_source`；名称、单位及 instrument 数量不变。
来源由本次受信 RunContext 提供：dashboard/api、feishu/cli/acp/dingtalk/wecom、
schedule、task、delegation、skill_review（技能自进化审查）、curator，以及枚举保留的 chat/http；缺失或未知归 other。
不使用会话创建来源、任务发起平台或投递平台替代本次执行来源。

Agent Span 仍为 current 时记录三个测量，SDK exemplar 保留该 Agent 的 trace_id/span_id，
执行标识不加入时序标签。inference_calls/tool_calls 的 `_sum / _count` 表示每次运行的
平均调用数；零调用运行仍进入 `_count`，`_count / _count` 不能表示调用次数。

Overview/Metrics 的 Source 多选变量仅筛选 AgentRun/turns；模型、工具等保持服务级。
All 使用 `.*`，包含历史缺失来源标签的序列；指定来源时排除这些历史序列。
历史标签缺失不能补造为 other。exemplar 为采样例子，单次耗时不等于面板分位数。
本节是新增维度契约，不改变上方 2026-10-25 名称实测日期；本次后端验证另记任务计划。

大盘 p95 查询模板（已在大盘 JSON 中采用实际名称）：

```promql
histogram_quantile(0.95,
  sum by (le) (rate(gen_ai_client_inference_duration_seconds_bucket[5m])))
```

实测 Prometheus query API 对该语句返回 success。

## 其他信号连通性证据（同一次合成导出）

- Trace：Collector otlp/tempo → 单体 Tempo 2.9.0；使用 `/api/search`（TraceQL 服务过滤、Unix 秒 start/end）与 `/api/traces/{traceID}`（Accept: application/json）查询。Tempo OTLP JSON 可能使用 batches 与 base64 ID，由 tests/e2e/tempo_query.py 归一化为 resourceSpans/hex。独立 tempo-data 保存 WAL/blocks，保留 168h。
- Metric → Trace：SDK sampled span 内记录 histogram，OTLP exemplar 经 Collector prometheusremotewrite 保留 trace_id/span_id；Prometheus 使用 --enable-feature=exemplar-storage，Grafana 将 exemplar trace_id 映射到 nagent-tempo。通过 `/api/v1/query_exemplars` 验证实际关联。
- Trace ↔ Log：Tempo tracesToLogsV2 按 service.name→service_name、Trace ID、前后 5 秒窗口查询 Loki；Loki derivedFields 用内部 Tempo UID 跳回 Trace。标识符不进入普通时序 label 或 Loki 索引 label。
- Log：Collector debug exporter（verbosity=detailed）stdout 输出
  `Body: Str(t9 synthetic observability event)`；`service.telemetry.logs.level`
  必须保持默认 info，设为 warn 会吞掉 debug exporter 的交付输出。
- Profile：Pyroscope 1.21.1 `/ingest`（name=n-agent-synth&format=folded&
  units=samples&aggregationType=sum）返回 200 且 `/pyroscope/render` 可查
  process_cpu 火焰图。


## 迁移既有 Trace 入口
新部署设置 N_AGENT_OTEL_GRAFANA_TRACES_BASE_URL 为浏览器可访问的 Grafana /explore（支持反向代理子路径）。已保存的 Dashboard trace_base_url override 优先于 env：通过 GET /chat/observations/otel/links 读取 version，再以 PATCH 和 expected_version 仅更新 trace_base_url；也可传 null 清除该覆盖、沿用 env。不要改写其他外链，也不自动覆盖用户自定义配置。本次本地部署已通过 CAS 将旧 Jaeger 链接迁移到既有 Grafana 公网入口的 /explore。

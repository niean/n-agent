# 指标名称映射 -- T9 S4 锁定证据

2026-10-25，本地观测栈实测锁定（非 spec 示例名）。方法：一次性 python:3.11-slim 容器
（pin opentelemetry-sdk==1.45.1 / opentelemetry-exporter-otlp-proto-grpc==1.45.1）以
telemetry_semantics.py 的 12 个 instrument 名称/单位/维度合成导出到 Collector OTLP 4317，
经 prometheusremotewrite 落 Prometheus 后，用 `/api/v1/label/__name__/values` 与
`/api/v1/series` 读取实际存储名称。

观测栈：otel/opentelemetry-collector-contrib:0.137.0、prom/prometheus:v3.15.1
（--web.enable-remote-write-receiver）。合成 Resource service.name=n-agent-synth；
resource_to_telemetry_conversion.enabled=true 使 service_name 成为 PromQL 可用 label。

## 映射表（20 instrument，目标名称）

三个新 Counter（第 10–12 行）于 2026-11-03 经真实 OTLP → Collector → Prometheus 验证，使用隔离的 service.name=n-agent-synth、service.instance.id=semantic-probe-261103；不计入业务 N-Agent 运行数。真实 Agent 请求另验证了 nagent_agent_runs_total 及三个 Agent Histogram 的 nagent_agent_source 维度，现场来源为 dashboard。新证据保存在 locals/observability-migration/switch-261103/new-metric-probe-*.json 和 new-agent-source-*.json；不将本次结果记为 2026-10-25 实测。其余标准 instrument 沿用原实测名称。

五个缓存与归一化 Token Counter（第 13–17 行）为 2026-11-05 新增（spec-261105-cache-token-metrics）：命名决定为项目 nagent.model.* 命名（不宣称 OTel 标准，既有 gen_ai instrument 不升级 semconv），依据见 .harness/knowledge/07-observability.md "归一化 Token" 小节。五个实际 Prometheus 名称已于2026-11-08通过真实calculator请求、Collector导出与Prometheus查询确认；原始快照位于 locals/e2e-reports/metrics-consistency-261108-fixed/prometheus-after.json，并与相同21个Span的Langfuse用量对账通过。维度契约无变化：五个新 instrument 复用既有三维（operation/provider/request.model）。

Prometheus 翻译规则实测：`.` 与 `-` 转 `_`；Counter 加 `_total`；Histogram（单位 s）
加 `_seconds` 且不再加 `_total`；`{...}` 括号单位（token/inference_call/tool_call/attempt）不产生
后缀。USD 货币单位实测产生字面 `_USD` 后缀（2026-11-09 探针锁定）。每个 Histogram 导出 `_bucket` / `_sum` / `_count` 三条序列。

三个成本 Counter（第 18–20 行）为 2026-11-09 新增（T5 成本观测投影）：
nagent.model.cost / nagent.model.cost_components / nagent.model.cost_attempts。
三个实际 Prometheus 名称已于 2026-11-09 经合成探针（真实 OTLP → 共享 Collector →
Prometheus remote write → /api/v1/label/__name__/values 与 /api/v1/series）实测锁定；
隔离 service.name=n-agent-synth、service.instance.id=semantic-probe-261109，不计入业务
N-Agent 运行数。方法与原始输出存于 locals/observability-migration/cost-probe-261109/。
维度实测：cost 与 cost_components 携带 gen_ai_operation_name、nagent_model_billing_provider、
nagent_model_billing_model（components 另加 nagent_model_cost_component）；cost_attempts
另加 nagent_model_cost_status 与 nagent_model_cost_reason。注意成本名称含 `_USD` 后缀，
与预期的 nagent_model_cost_total 不同——禁止凭规则猜测，一律以本表实测名称为准。

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
| 10 | nagent.agent.runs | Counter / 1 | nagent_agent_runs_total |
| 11 | nagent.context.compression_attempts | Counter / 1 | nagent_context_compression_attempts_total |
| 12 | nagent.budget.denials | Counter / 1 | nagent_budget_denials_total |
| 13 | nagent.model.cache_read_tokens | Counter / {token} | nagent_model_cache_read_tokens_total |
| 14 | nagent.model.cache_write_tokens | Counter / {token} | nagent_model_cache_write_tokens_total |
| 15 | nagent.model.normalized_tokens | Counter / {token} | nagent_model_normalized_tokens_total |
| 16 | nagent.model.cache_eligible_prompt_tokens | Counter / {token} | nagent_model_cache_eligible_prompt_tokens_total |
| 17 | nagent.model.cache_eligible_read_tokens | Counter / {token} | nagent_model_cache_eligible_read_tokens_total |
| 18 | nagent.model.cost | Counter / USD | nagent_model_cost_USD_total |
| 19 | nagent.model.cost_components | Counter / USD | nagent_model_cost_components_USD_total |
| 20 | nagent.model.cost_attempts | Counter / {attempt} | nagent_model_cost_attempts_total |

实测存在的裸 Histogram 基名（供 T10 断言 `_bucket/_sum/_count` 后缀时参考）：
- gen_ai_client_inference_operation_input_tokens
- gen_ai_client_inference_operation_output_tokens
- gen_ai_client_inference_duration_seconds
- gen_ai_execute_tool_duration_seconds
- gen_ai_invoke_agent_duration_seconds
- gen_ai_invoke_agent_inference_calls
- gen_ai_invoke_agent_tool_calls

## 维度（labels）契约与既有实测

Counter 示例（nagent_agent_runs_total / gen_ai_client_inference_usage_input_tokens_total）：
`service_name`、`service_instance_id`、`service_version`、`job`、`instance`、
`telemetry_sdk_*`，以及 instrument 允许维度（如 gen_ai_request_model、
gen_ai_provider_name、gen_ai_token_modality、nagent_agent_source、nagent_agent_end_reason）。
维度名同样按 `.` -> `_` 翻译（nagent.agent.source -> nagent_agent_source）。

### 2026-11-03：AgentRun 来源维度契约与后端验证

三个既有 histogram（gen_ai.invoke_agent.duration / inference_calls / tool_calls）增加
`nagent.agent.source`，落库标签为 `nagent_agent_source`；名称、单位及 instrument 数量不变。
来源由本次受信 RunContext 提供：dashboard/api、feishu/cli/acp/dingtalk/wecom、
schedule、task、delegation、skill_review（技能自进化审查）、curator，缺失或未知归 unknown，旧 chat/http 不再作为有效来源。
不使用会话创建来源、任务发起平台或投递平台替代本次执行来源。

Agent Span 仍为 current 时记录三个测量，SDK exemplar 保留该 Agent 的 trace_id/span_id，
执行标识不加入时序标签。inference_calls/tool_calls 的 `_sum / _count` 表示每次运行的
平均调用数；零调用运行仍进入 `_count`，`_count / _count` 不能表示调用次数。

Overview/Metrics 的 Source 多选变量仅筛选 Agent 运行；模型、工具等保持服务级。
All 使用 `.*`，包含历史缺失来源标签的序列；指定来源时排除这些历史序列。
历史标签缺失不能补造为 other。exemplar 为采样例子，单次耗时不等于面板分位数。
本节新增维度已由真实 Agent 请求验证，不改变上方 2026-10-25 名称实测日期；本次后端验证另记任务计划。

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


## 评估遥测（T10，n-eval-v1）

评估遥测新增 8 个 ObservableGauge（24h 质量窗口，spec 12.3）+ 5 个
producer 高水位累计 Gauge + 队列深度 Gauge + worker/投递常规
Counter/Histogram。来源模块 app/application/evaluation_telemetry.py；
Gauge 回调只读内存快照（后台刷新读 SQLiteEvaluationStore 只读查询
query_window_snapshot / query_producer_snapshot），每 service/profile/mode
组仅单一 exporter owner 导出。

名称按上方实测翻译规则推导（`.`→`_`；Counter 加 `_total`；Histogram 单位
s 加 `_seconds` 且不再加 `_total`；`{...}` 括号单位不产生后缀；unit `1`
Gauge 加 `_ratio` 后缀——T22 E2E 经真实 OTLP→Prometheus 管线实测修正，
早前"1 单位不产生后缀"的假设不成立）。T11 Collector 核对与 T22 E2E 将以真实 OTLP→Prometheus
管线复测本节名称；在此之前以本表为准，禁止凭规则猜测。

| # | Instrument（evaluation_telemetry 常量） | 类型/单位 | Prometheus 目标名称 |
|---|---|---|---|
| 21 | nagent.evaluation.window.succeeded | Gauge / 1 | nagent_evaluation_window_succeeded_ratio |
| 22 | nagent.evaluation.window.skipped | Gauge / 1 | nagent_evaluation_window_skipped_ratio |
| 23 | nagent.evaluation.window.error | Gauge / 1 | nagent_evaluation_window_error_ratio |
| 24 | nagent.evaluation.window.unlinked | Gauge / 1 | nagent_evaluation_window_unlinked_ratio |
| 25 | nagent.evaluation.window.numeric_sum | Gauge / 1 | nagent_evaluation_window_numeric_sum_ratio |
| 26 | nagent.evaluation.window.numeric_count | Gauge / 1 | nagent_evaluation_window_numeric_count_ratio |
| 27 | nagent.evaluation.window.boolean_pass | Gauge / 1 | nagent_evaluation_window_boolean_pass_ratio |
| 28 | nagent.evaluation.window.boolean_count | Gauge / 1 | nagent_evaluation_window_boolean_count_ratio |
| 29 | nagent.evaluation.producer.eligible | Gauge / {job} | nagent_evaluation_producer_eligible |
| 30 | nagent.evaluation.producer.selected | Gauge / {job} | nagent_evaluation_producer_selected |
| 31 | nagent.evaluation.producer.captured | Gauge / {job} | nagent_evaluation_producer_captured |
| 32 | nagent.evaluation.producer.capture_error | Gauge / {job} | nagent_evaluation_producer_capture_error |
| 33 | nagent.evaluation.producer.queue_full | Gauge / {job} | nagent_evaluation_producer_queue_full |
| 34 | nagent.evaluation.worker.queue_depth | Gauge / {job} | nagent_evaluation_worker_queue_depth |
| 35 | nagent.evaluation.worker.calls | Counter / {call} | nagent_evaluation_worker_calls_total |
| 36 | nagent.evaluation.worker.duration | Histogram / s | nagent_evaluation_worker_duration_seconds_bucket / _sum / _count |
| 37 | nagent.evaluation.delivery.failures | Counter / {failure} | nagent_evaluation_delivery_failures_total |

维度契约（T10）：
- window.*（21–28）质量标签只允许 `target.service`（nagent/n-kb/other）、
  `metric_profile`（<=16 个活跃定义版本注册表，`definition_id@version`）、
  `mode`（online/execute/replay/other）、`status`（succeeded/skipped/error/
  unlinked；numeric_sum/numeric_count/boolean_pass/boolean_count 恒为
  succeeded）。dataset_id、case_id、result_id、run_id、session_id、
  trace_id、模型任意名称、query 与 KB 文档 ID 禁止做标签（测试哨兵锁定）。
  window.* 不使用 rate()，24h 窗口 current 视图快照，不与 producer 累计
  分母混算。
- producer.*（29–33）与 queue_depth（34）无 instrument 标签（跨
  producer/counter epoch 汇总；覆盖率口径为"自当前 counter epoch 起"）。
- worker.calls（35）携带 `nagent.evaluation.outcome`
  （succeeded/skipped/error/other）；worker.duration（36）同维度；
  delivery.failures（37）携带 `nagent.evaluation.sink`
  （langfuse/otel/other）与 `error.type`（评估错误封闭词汇/other）。
- LogRecord 事件 `gen_ai.evaluation.result`（n-eval-v1，字段集与 schema
  hash 见 tests/application/fixtures/evaluation_telemetry_n_eval_v1.json）
  与 `evaluation.run` Trace 走日志/Trace 管线，不进 Prometheus 映射表。

Collector 投影边界（T11）：
- logs 管线（transform/loki_json 评估分支，事件门控）：仅放行注册事件
  `gen_ai.evaluation.result` 的封闭字段白名单（`nagent.evaluation.*` +
  `gen_ai.evaluation.name/score.*` + `error.type`；集合与 n-eval-v1 字段集
  一致，以 tests/application/fixtures/evaluation_telemetry_n_eval_v1.json
  的 fields 除去 event.name/explanation 为准，测试交叉校验）；explanation
  与日志原文不投影（不进行体、不进 Loki 索引标签/structured metadata）；
  未注册事件保持既有 unknown/过滤语义；外来 `langfuse.*` 属性在日志管线
  无任何投影通道。业务日志行体形状与 unknown 过滤条件由静态测试逐字节
  锁定（业务输出等价，tests/test_langfuse_collector.py T11 节）。
- traces/langfuse 管线：`evaluation.run` span 映射 evaluator；
  实验身份 `nagent.evaluation.experiment.id/name/dataset_id/item_id/
  root_observation_id`（受信本地编排注入）精确映射为
  `langfuse.experiment.*`，客户端 baggage/header 永不直接投影；KB 分支
  白名单与 Link sanitizer 行为不变。

## 迁移既有 Trace 入口
新部署设置 N_AGENT_OTEL_GRAFANA_TRACES_BASE_URL 为浏览器可访问的 Grafana /explore（支持反向代理子路径）。已保存的 Dashboard trace_base_url override 优先于 env：通过 GET /chat/observations/otel/links 读取 version，再以 PATCH 和 expected_version 仅更新 trace_base_url；也可传 null 清除该覆盖、沿用 env。不要改写其他外链，也不自动覆盖用户自定义配置。本次本地部署已通过 CAS 将旧 Jaeger 链接迁移到既有 Grafana 公网入口的 /explore。

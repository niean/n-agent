# 映射 N-KB 观测指标

本表为锁定配置的静态映射，不是共享后端实测记录。OTel SDK/exporter=1.45.1、Collector=0.137.0；resource_to_telemetry_conversion已开启，service.name/service.instance.id映射service_name/service_instance_id。

依据：[Collector v0.137.0名称转换](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.137.0/pkg/translator/prometheus/normalize_name.go)及同目录normalize_unit.go。名称标点转下划线，s加seconds、By加bytes，单调Sum加total，{token}/{chunk}/{result}不加单位后缀。Histogram使用bucket/sum/count。查询与实际后端连通性须人工核验，不能把静态映射称为实测。

| OTel instrument | Prometheus名称 | 类型/单位 | 维度 | 边界 |
|---|---|---|---|---|
| gen_ai.client.operation.duration | gen_ai_client_operation_duration_seconds (_bucket / _sum / _count) | histogram / s | gen_ai.operation.name, gen_ai.request.model, error.type | 0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0, 30.0 |
| gen_ai.client.token.usage | gen_ai_client_token_usage (_bucket / _sum / _count) | histogram / {token} | gen_ai.operation.name, gen_ai.request.model, gen_ai.token.type | 1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304, 16777216, 67108864 |
| nkb.document.uploads | nkb_document_uploads_total | counter / 1 | nkb.document.outcome | 不适用 |
| nkb.document.upload.duration | nkb_document_upload_duration_seconds (_bucket / _sum / _count) | histogram / s | nkb.document.outcome | 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0 |
| nkb.index.duration | nkb_index_duration_seconds (_bucket / _sum / _count) | histogram / s | nkb.index.outcome | 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0 |
| nkb.index.stage.duration | nkb_index_stage_duration_seconds (_bucket / _sum / _count) | histogram / s | nkb.index.stage, nkb.index.outcome | 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0 |
| nkb.mcp.requests | nkb_mcp_requests_total | counter / 1 | nkb.mcp.operation, nkb.mcp.outcome | 不适用 |
| nkb.mcp.duration | nkb_mcp_duration_seconds (_bucket / _sum / _count) | histogram / s | nkb.mcp.operation, nkb.mcp.outcome | 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0 |
| nkb.index.jobs | nkb_index_jobs_total | counter / 1 | nkb.index.outcome | 不适用 |
| nkb.index.chunks | nkb_index_chunks (_bucket / _sum / _count) | histogram / {chunk} | 无业务维度 | 1, 2, 4, 8, 16, 32, 64, 128, 256, 512 |
| nkb.retrieval.requests | nkb_retrieval_requests_total | counter / 1 | nkb.retrieval.outcome | 不适用 |
| nkb.retrieval.results | nkb_retrieval_results (_bucket / _sum / _count) | histogram / {result} | 无业务维度 | 0, 1, 2, 3, 5, 8, 10, 20, 50, 100 |
| nkb.retrieval.duration | nkb_retrieval_duration_seconds (_bucket / _sum / _count) | histogram / s | nkb.retrieval.outcome | 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0 |
| nkb.model.cost | nkb_model_cost_USD_total | counter / USD | gen_ai.operation.name, nkb.model.billing_provider, nkb.model.billing_model | 不适用 |
| nkb.model.cost_components | nkb_model_cost_components_USD_total | counter / USD | gen_ai.operation.name, nkb.model.billing_provider, nkb.model.billing_model, nkb.model.cost_component | 不适用 |
| nkb.model.cost_attempts | nkb_model_cost_attempts_total | counter / {attempt} | gen_ai.operation.name, nkb.model.billing_provider, nkb.model.billing_model, nkb.model.cost_status, nkb.model.cost_reason | 不适用 |

系统指标沿用N-Agent SystemMetricsInstrumentor 0.66b1：process.cpu.time（ObservableCounter/s）→process_cpu_time_seconds_total，process.memory.usage（ObservableUpDownCounter/By）→process_memory_usage_bytes。按service_instance_id分组；没有业务流量也周期采集。其他系统/进程指标由同一instrumentor导出，本地摘要仅登记安全子集，不暴露设备或接口。

所有业务Histogram通过MeterProvider View固定显式桶，检索results首边界=0，空命中率=rate(le=0 bucket)/rate(count)。错误率分子failure+timed_out、分母success+failure+timed_out，准入拒绝denied单独观察、取消单列；未产生错误序列与零分母显示无数据。跨实例先rate后sum，p95保留le聚合，token使用histogram sum，未知token不补零。

大盘固定UID：nkb-overview、nkb-logs、nkb-profile；沿用provider.yaml的N-Agent文件夹和数据源UID，不改既有N-Agent大盘。service使用regex变量展开，默认service=n-kb；不增加实例筛选；Profile仅service_name查询，不支持实例。

Trace入口由N-KB页面生成Tempo Explore，数据源nagent-tempo；既有Tempo tracesToLogsV2按service.name+Trace ID及前后5秒查Loki，Loki derivedFields按JSON trace_id返回Tempo。Collector仅在N-KB分支按精确契约投影context.execution和十一项details信封；普通库日志保留脱敏有界正文和异常，未知属性清空，禁止完整原文或凭据；trace/span ID不作新增索引label。Profile沿用Pyroscope独立上传和process_cpu查询，未通过Collector，不声称逐Trace精确关联。

验证：N-KB tests/test_grafana_dashboards.py检查provisioning挂载、UID/数据源引用、全部业务instrument和单位/变量/分母/零桶。真实后端查询与双向Trace日志关联按N-KB verify-261030-complete-otel-observability.md人工验收，本次尚未执行。

## 本地登记的系统指标

以下类型/单位对照已安装锁定0.66b1 instrumentor源码；非单调ObservableUpDownCounter不加total。

| OTel名称 | Prometheus名称 | 类型 / 单位 | 安全摘要维度 |
|---|---|---|---|
| system.cpu.time | system_cpu_time_seconds_total | counter / s | state |
| system.cpu.utilization | system_cpu_utilization_ratio | gauge / 1 | state |
| system.memory.usage | system_memory_usage_bytes | gauge / By | state |
| system.memory.utilization | system_memory_utilization_ratio | gauge / 1 | state |
| system.swap.usage | system_swap_usage_pages | gauge / pages | state |
| system.swap.utilization | system_swap_utilization_ratio | gauge / 1 | state |
| system.disk.io | system_disk_io_bytes_total | counter / By | direction |
| system.network.io | system_network_io_bytes_total | counter / By | direction |
| system.thread_count | system_thread_count | gauge / 无 | 无 |
| process.cpu.time | process_cpu_time_seconds_total | counter / s | type |
| process.cpu.utilization | process_cpu_utilization_ratio | gauge / 1 | 无 |
| process.memory.usage | process_memory_usage_bytes | up_down_counter / By | 无 |
| process.memory.virtual | process_memory_virtual_bytes | up_down_counter / By | 无 |
| process.thread.count | process_thread_count | up_down_counter / 无 | 无 |

三个成本 Counter（nkb.model.cost / cost_components / cost_attempts）为 2026-11-09 新增（T5 成本观测投影）。三个实际 Prometheus 名称经合成探针（真实 OTLP -> 共享 Collector -> Prometheus remote write -> API 查询）实测锁定：隔离 service.name=n-agent-synth、service.instance.id=semantic-probe-261109，不计入业务流量；原始输出存于 N-Agent 仓库 locals/observability-migration/cost-probe-261109/。USD 单位实测产生字面 `_USD` 后缀，{attempt} 不产生后缀；维度实测与上表一致（点转下划线，如 nkb_model_billing_provider、nkb_model_cost_component、nkb_model_cost_status、nkb_model_cost_reason）。禁止凭规则猜测成本指标名称，一律以本表实测名称为准。

## 规范化迁移与证据边界

项目维度精确使用 nkb.document.outcome、nkb.index.outcome/stage、nkb.retrieval.outcome、nkb.mcp.operation/outcome。Prometheus 标签将点转换为下划线，例如 nkb_document_outcome、nkb_index_stage、nkb_mcp_operation；GenAI 标准维度不变。ID、正文、详情不进入指标维度或新增 Loki 索引标签。所有十六项 instrument 的名称、类型、单位和桶边界保持原值。

完成事件统一为 nkb.document.completed、nkb.index.completed、nkb.retrieval.completed、nkb.model.completed、nkb.mcp.completed、nkb.http.completed。index run/阶段由 nkb.index.operation 区分；HTTP server/client 由 nkb.http.direction 区分。旧 nkb.operation.completed、nkb.index.stage.completed、nkb.embedding.completed、http_request_completed/http_client_completed 与旧 nkb.outcome/document_id/chunk_count/result_count、nkb.filter.*、裸 operation/stage/outcome 不再写出。nkb.content.FIELD 及下划线后缀元数据映射 details.FIELD.content/original_bytes/truncated/incomplete/observed_bytes。

不双写、不回灌、不删除历史记录；大盘默认查询新契约，旧时间范围因标签或字段变化可能不完整，没有隐式历史兼容聚合。failure/timed_out 为技术失败；cancelled 在吞吐按 outcome 独立显示，denied 不计入错误率分母。

静态证据由 test_grafana_dashboards.py 比较全部 instrument、标签、单位、桶与查询分母；test_collector_semantics.py 精确对照应用契约及固定自动埋点属性，并以 NKB_COLLECTOR_SMOKE=1 在隔离 Collector 0.137.0 执行真实日志转换、与 HEAD 基线对照 N-Agent 输出。隔离 SDK 测试验证测量值与维度；这些不等于实际 Prometheus/Loki/Tempo 后端展示证据，后者仍须人工验收。

部署边界：本次仅修改配置并运行临时隔离实例，没有重启或重载共享 Collector/Grafana。共享 Collector 在配置重载前仍使用旧投影，不能显示新规范属性；Grafana provisioning 同样须按现有部署方式重载后生效。应用 after-finish Hook 只重启 N-KB，不部署共享观测栈。未连接实际共享后端或完成 UI 验收。

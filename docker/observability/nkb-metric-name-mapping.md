# 映射 N-KB 观测指标

本表为锁定配置的静态映射，不是共享后端实测记录。OTel SDK/exporter=1.45.1、Collector=0.137.0；resource_to_telemetry_conversion已开启，service.name/service.instance.id映射service_name/service_instance_id。

依据：[Collector v0.137.0名称转换](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.137.0/pkg/translator/prometheus/normalize_name.go)及同目录normalize_unit.go。名称标点转下划线，s加seconds、By加bytes，单调Sum加total，{token}/{chunk}/{result}不加单位后缀。Histogram使用bucket/sum/count。查询与实际后端连通性须人工核验，不能把静态映射称为实测。

| OTel instrument | Prometheus名称 | 类型/单位 | 维度 | 边界 |
|---|---|---|---|---|
| gen_ai.client.operation.duration | gen_ai_client_operation_duration_seconds (_bucket / _sum / _count) | histogram / s | gen_ai.operation.name, gen_ai.request.model, error.type | 0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0, 30.0 |
| gen_ai.client.token.usage | gen_ai_client_token_usage (_bucket / _sum / _count) | histogram / {token} | gen_ai.operation.name, gen_ai.request.model, gen_ai.token.type | 1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304, 16777216, 67108864 |
| nkb.document.uploads | nkb_document_uploads_total | counter / 1 | outcome | 不适用 |
| nkb.document.upload.duration | nkb_document_upload_duration_seconds (_bucket / _sum / _count) | histogram / s | outcome | 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0 |
| nkb.index.duration | nkb_index_duration_seconds (_bucket / _sum / _count) | histogram / s | outcome | 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0 |
| nkb.index.stage.duration | nkb_index_stage_duration_seconds (_bucket / _sum / _count) | histogram / s | stage, outcome | 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0 |
| nkb.mcp.requests | nkb_mcp_requests_total | counter / 1 | operation, outcome | 不适用 |
| nkb.mcp.duration | nkb_mcp_duration_seconds (_bucket / _sum / _count) | histogram / s | operation, outcome | 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0 |
| nkb.index.jobs | nkb_index_jobs_total | counter / 1 | outcome | 不适用 |
| nkb.index.chunks | nkb_index_chunks (_bucket / _sum / _count) | histogram / {chunk} | 无业务维度 | 1, 2, 4, 8, 16, 32, 64, 128, 256, 512 |
| nkb.retrieval.requests | nkb_retrieval_requests_total | counter / 1 | outcome | 不适用 |
| nkb.retrieval.results | nkb_retrieval_results (_bucket / _sum / _count) | histogram / {result} | 无业务维度 | 0, 1, 2, 3, 5, 8, 10, 20, 50, 100 |
| nkb.retrieval.duration | nkb_retrieval_duration_seconds (_bucket / _sum / _count) | histogram / s | outcome | 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0 |

系统指标沿用N-Agent SystemMetricsInstrumentor 0.66b1：process.cpu.time（ObservableCounter/s）→process_cpu_time_seconds_total，process.memory.usage（ObservableUpDownCounter/By）→process_memory_usage_bytes。按service_instance_id分组；没有业务流量也周期采集。其他系统/进程指标由同一instrumentor导出，本地摘要仅登记安全子集，不暴露设备或接口。

所有业务Histogram通过MeterProvider View固定显式桶，检索results首边界=0，空命中率=rate(le=0 bucket)/rate(count)。错误率分母success+error、取消单列；未产生错误序列与零分母显示无数据。跨实例先rate后sum，p95保留le聚合，token使用histogram sum，未知token不补零。

大盘固定UID：nkb-overview、nkb-logs、nkb-profile；沿用provider.yaml的N-Agent文件夹和数据源UID，不改既有N-Agent大盘。service/instance使用regex变量展开，默认service=n-kb/instance=All；Profile仅service_name查询，不支持实例。

Trace入口由N-KB页面生成Tempo Explore，数据源nagent-tempo；既有Tempo tracesToLogsV2按service.name+Trace ID及前后5秒查Loki，Loki derivedFields按JSON trace_id返回Tempo。Collector仅为N-KB增加固定execution事实字段，不将query/原文/异常引入JSON；trace/span ID不作新增索引label。Profile沿用Pyroscope独立上传和process_cpu查询，未通过Collector，不声称逐Trace精确关联。

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

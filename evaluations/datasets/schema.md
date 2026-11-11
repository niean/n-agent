# 数据集 manifest schema 说明

本文档描述 `evaluations/datasets/` 下数据集 manifest 的 schema 与校验纪律。权威实现为 `app/application/evaluation_dataset.py`（T12 loader）与领域契约 `app/domain/evaluation/models.py`（DatasetManifest / DatasetCase），设计来源为 `.harness/specs/active/spec-261109-design-evaluation.md` 第 9.1 节。

## 定位与边界

- 数据集是仓库受版本管理的制品（manifest JSON + 附件目录），不是运行时配置：不进入 Config Bundle 白名单（`app/domain/config_bundle.py` SECTION_NAMES），不随服务 restart 清空。
- loader 不执行数据集携带的任何脚本：只有声明式解析（json / hashlib / pathlib），无 exec / eval / compile / 动态 import / 子进程 / 网络触点（由静态 AST 断言锁定）。
- 所有校验 fail-fast：任一失败立即终止，不返回部分结果、不运行任何 case。
- 生产样本提升为数据集必须显式审核、脱敏并生成新 manifest，不能直接将线上消息写入 Git。

## 目录布局

```
evaluations/datasets/<dataset-id>/
  manifest.json        -- manifest 唯一入口文件
  fixtures/            -- case 附件（路径在 manifest 中声明）
  rubrics/             -- rubric_ref 引用的评审标准材料
```

说明：spec 9.1 允许 manifest 为 JSON 或 JSONL；当前实现只支持单文件 JSON manifest（JSONL 延后，决策记录见 plan T12 判断记录），格式扩展时必须同步更新本文档与校验测试。

## manifest 字段

顶层必填字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| schema_version | string | 固定 "1"；不认识的版本拒绝加载 |
| dataset_id | string | 数据集标识，非空 |
| dataset_version | string | 数据集版本，非空 |
| dataset_hash | string | 64 位小写十六进制 SHA-256，见下方 hash 纪律 |
| target_kind | string | manifest 级默认目标类型，取值 agent_run / retrieval / experiment_item / session_feedback |
| cases | array | 非空 case 数组 |

## case 字段

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| case_id | 是 | string | 数据集内唯一，重复立即终止 |
| input | 是 | object | 发送给目标的输入载荷 |
| target_kind | 是 | string | 单 case 目标类型，必须为合法枚举值 |
| slice | 否 | string | 切片标记（如 normal / no-answer / side-effect） |
| expected_output | 条件 | 任意 JSON | 问答真值；与 qrels 至少声明其一 |
| qrels | 条件 | array | 检索分级标注；与 expected_output 至少声明其一 |
| rubric_ref | 条件 | string | 评审标准材料相对路径；声明时必须同时声明 rubric_sha256 |
| rubric_sha256 | 条件 | string | rubric 材料文件字节级 SHA-256 |
| corpus_revision | 条件 | string | 冻结检索基准语料版本；声明 qrels 的 case 必填，且全部 qrels 条目的语料版本必须与其一致 |
| fixture | 条件 | string | 附件相对路径；声明时必须同时声明 fixture_sha256 |
| fixture_sha256 | 条件 | string | 附件文件字节级 SHA-256 |
| no_answer | 否 | bool | 无答案查询声明，默认 false；no_answer 语义来自检索标注，仅允许伴随 qrels 声明，expected_output-only case 声明即拒绝 |
| metric_applicability | 否 | object | 指标名 -> bool 的适用性预声明 |
| allowed_side_effects | 否 | array | 允许的副作用标识；非空时必须同时声明 cleanup_rule |
| cleanup_rule | 条件 | string | 清理规则；声明 allowed_side_effects 时必须同时声明（单向要求，仅声明 cleanup_rule 允许） |

## hash 纪律

- dataset_hash = SHA-256( canonical_json(manifest 内容去掉 dataset_hash 字段) )，canonical JSON 规则锚定 `app/domain/evaluation/models.py` 的 `canonical_json_bytes`（固定键序、紧凑分隔符、UTF-8、拒绝非有限数）。
- 生成与校验使用同一函数 `app.application.evaluation_dataset.compute_dataset_hash`。
- 加载时重算并与声明值比对，不一致立即终止。

## 附件与 rubric 材料校验

fixture 与 rubric 材料（rubric_ref）适用同一纪律（spec 9.1：路径和附件 hash 校验失败立即终止）：

- 必须是数据集目录内相对路径：拒绝绝对路径、`..` 逃逸；解析（resolve，穿透符号链接）后必须仍位于数据集目录内，目录内软链指向目录外的文件同样拒绝。
- 文件必须存在；文件字节 SHA-256 必须与声明的 fixture_sha256 / rubric_sha256 一致，否则立即终止。
- 声明 hash 字段而未声明对应路径字段同样拒绝。

## qrels 语义

- 检索身份 = corpus_revision + document_revision + chunk_id，与 `app/domain/evaluation/metrics_deterministic.py` 的 ChunkIdentity 对齐；qrels_identity 函数以 ":" 连接三字段构造复合键，三字段不得包含 ":"。
- 相关性分级 grade 取 0 / 1 / 2；Recall / MRR 的相关阈值 rel>=1。
- 去重：同一身份重复标注同分级只保留一份；同身份冲突分级立即终止。
- 无相关项 query（qrels 中无 grade>=1 条目）必须声明 no_answer=true，并在 metric_applicability 中声明至少一个名称含 no_answer 的指标为 true；声明 no_answer=true 但 qrels 存在相关项视为矛盾，立即终止。

## 与 Config Bundle 的边界

数据集不修改 Config Bundle 白名单，不属于任何可迁移配置节；Config Bundle 导入导出与 restart 清理生命周期均不触及 `evaluations/` 目录。

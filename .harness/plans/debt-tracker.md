# 技术债追踪

| ID | 描述 | 优先级 | 来源计划 | 发现时间 | 状态 |
|----|------|--------|---------|---------|------|
| D001 | `docker compose config` 会展开本地 `.env` 中的 Provider API Key，运行验收命令时可能在终端输出敏感配置。 | medium | plan-260611-chat-fullscreen.md | 2026-06-11 | open |
| D002 | `tests/test_docker_compose_config.py` 期望 `docker/.env.example` 包含 `http://n-kb:8212`，但当前示例配置为空值，导致全量 pytest 失败。 | medium | plan-260615-provider-active-check.md | 2026-06-15 | open |
| D003 | GatewayConfirmation 持久化：`GatewayCommandService.pending_confirmations` 保持为进程内 dict，多副本部署或进程重启会丢失未确认请求；待未来需要多副本时设计 Domain `GatewayConfirmationRegistry` 端口与 SQLite 实现。 | low | plan-260616-feishu-natural-schedule.md | 2026-06-17 | open |
| D004 | Gateway 与定时任务 origin 曾存在 `source_type/source_id` 命名债，与 Hermes 平台抽象不一致；本计划已一次性迁移为 platform/platform_session_id，并移除业务 fallback。 | medium | plan-260617-platform-aggregate.md | 2026-06-17 | resolved |
| D005 | `tests/application/test_agent_graph.py::test_agent_graph_injects_system_prompt_without_persisting_message` 断言系统提示词含 `N-Agent(Niean's Agent MVP)`，但 `prompt_builder.py` 实际产出 `N-Agent(Niean's Agent)`；非本次变更新引入，stash 验证确认预存失败。 | low | plan-260628-retrieved-memory-prefetch.md | 2026-06-28 | resolved |
| D006 | HolographicAdapter 内联 trust/decay/contradiction 逻辑（_score/_contradict），未复用 `MemoryTrustStore`。原因：MemoryTrustStore 数据模型（hash key + sidecar JSON + ISO 时间戳）与 HolographicAdapter（integer id + SQLite 行 + unix 时间戳）不兼容，强行复用会引入双存储系统。 | medium | plan-260628-external-query-providers.md | 2026-06-29 | open |
| D007 | 外部记忆 provider 前端管理页面（external-memory-providers.js）无自动化测试，仅手动验证。 | low | plan-260628-external-query-providers.md | 2026-06-29 | open |
| D008 | 外部记忆 provider 端到端验收（T13）为手动 curl，未自动化。 | low | plan-260628-external-query-providers.md | 2026-06-29 | open |
| D009 | `tests/interfaces/test_static_assets.py::test_external_memory_provider_actions_keep_table_cell_layout` 断言 `min-width: 220px` 和 `.row-actions--memory`，但 styles.css 实际为 `min-width: 150px` 且无 `.row-actions--memory` 规则；上一轮检索记忆 Dashboard 样式工作遗留的测试-css 不一致，非本次变更新引入。 | low | plan-260629-external-query-session-toggle.md | 2026-06-29 | resolved |
| D010 | Plugin 模块名碰撞：`_safe_module_name` 将所有非字母数字字符替换为 `_`，key `foo/bar` 与 `foo_bar` 产生相同模块名 `n_agent_plugins.foo_bar`，交替扫描时互相覆盖 sys.modules。影响：两个仅分隔符不同的插件无法共存。 | low | plan-260703-plugin-subsystem.md | 2026-07-03 | open |
| D011 | Plugin config 保存为整体替换：`SQLitePluginRegistry.update_config` 覆盖整个 `config_json`，前端跳过空值输入，导致用户清空字段时字段被删除而非置空；config_schema 未渲染的字段（如插件升级后移除的字段）也会丢失。 | low | plan-260703-plugin-subsystem.md | 2026-07-03 | open |
| D012 | `_apply_settings_enabled_state` 内 `next(p for p in plugins if p.key == key)` 在 enabled/disabled 循环中 O(N*M)，且 scan() 调用 list_plugins 两次；插件数量大时有性能开销。 | low | plan-260703-plugin-subsystem.md | 2026-07-03 | open |
| D013 | Plugin `is_async` 声明与 handler 实际类型不一致时错误信息不友好：sync handler 配 `is_async=True` 报 TypeError，async handler 配 `is_async=False` 返回 coroutine 对象作为 content。可在 register_tool 时自动检测 `asyncio.iscoroutinefunction` 覆盖声明。 | low | plan-260703-plugin-subsystem.md | 2026-07-03 | open |
| D014 | Plugin `_validate_config` 仅校验顶层 type=object 和 required 字段存在，未校验各字段类型、未拒绝 config_schema 标记为 secret 的字段进入 config（defense-in-depth 缺口，B2 已在前端修复，后端无兜底）。 | medium | plan-260703-plugin-subsystem.md | 2026-07-03 | open |
| D015 | `tests/application/test_chat_service.py::test_chat_service_non_stream_returns_message` 断言 session_id 前缀失败，非本次插件变更引入（git stash 验证确认）。 | low | N/A | 2026-07-03 | open |
| D016 | `tests/application/test_schedule_run_service.py::test_run_now_claims_and_runs_shared_path` 断言 status='succeeded' 但实际为 'triggered'，非本次插件变更引入（git stash 验证确认）。 | low | N/A | 2026-07-03 | open |
| D017 | `tests/interfaces/test_static_assets.py` 5 项失败（test_chat_builtin_memory_is_disabled_by_default / test_chat_memory_uses_toolbar_popover_grouped_picker / test_static_assets_use_safe_text_rendering / test_chat_supports_image_upload_paste_and_rendering / test_chat_composer_uses_doubao_style_rounded_container），涉及 chat.js innerHTML 赋值、memory toolbar popover、图片上传渲染、chat-composer 样式；来自近期前端提交（18d4639 对话: [KF]多模态支持图片输入、ca28d23 前端: 确认框和列表操作样式归一）和工作树未提交前端变更，非 terminal-in-sandbox 任务引入。 | low | N/A | 2026-07-07 | open |
| D018 | `SqliteUsageRecorder` async 方法（record_call/get_session_stats/list_records/record_compression/list_compressions/init）内部直接调用同步 `sqlite3` 连接与 DML，未走 thread executor；async 环境下会阻塞事件循环。与 `SQLiteMemoryStore` 现有模式一致，本期不修复。 | low | plan-260711-observation-usage.md | 2026-07-11 | open |

---

## 维护规则

1. 技术债来源：功能迭代、代码扫描、设计评审等渠道发现的问题
2. 代码扫描问题处理：代码扫描发现的问题，如非本次变更新引入，应记录到本文件而非要求立即修复
3. 优先级定义：high-影响核心功能或用户体验，需尽快修复；medium-有替代方案，可排期修复；low-优化项，有空闲时处理
4. 状态流转：open -> in_progress -> resolved，resolved 状态保留 1 个月后归档

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
| D019 | Host Terminal Policy 并发 `refresh()` 在锁外加载 candidate，旧刷新若后完成可能覆盖已发布的新 Policy；后续需串行化完整 load/publish 或加入发布序号，并补确定性乱序测试。 | high | plan-260715-photo-and-upload.md | 2026-07-15 | open |
| D020 | Host Terminal command trusted root 的 owner 校验尚未覆盖从文件系统根到 trusted root 的全部祖先；后续需校验完整祖先链 owner、非软链接和不可写属性。 | high | plan-260715-photo-and-upload.md | 2026-07-15 | open |
| D021 | macOS command Mach-O 的 codesign/verify 预处理 helper 尚未完全纳入请求绝对 deadline 与 Bridge shutdown 生命周期；后续需跟踪、终止和回收 helper，并确保 timeout/shutdown 后不会启动目标进程。 | high | plan-260715-photo-and-upload.md | 2026-07-15 | open |
| D022 | Host Terminal 启动清理仅接受 `0500` command 快照，若 Bridge 在 codesign 阶段崩溃会遗留合法 `0700` 快照并导致后续启动失败；后续需在保持普通文件/owner/非软链接校验下安全清理两种生命周期权限。 | medium | plan-260715-photo-and-upload.md | 2026-07-15 | open |
| D023 | Skill CRUD 收口未完成：现有 Dashboard/CLI skill CRUD（create_skill/update_skill/delete_skill）保留 legacy 实现，未路由到 manage_skill + SkillPolicy，绕过 policy/guard/backup 编排。spec 验收标准要求收口，但 legacy 方法用 SkillInput（relative_path/platforms/frontmatter）与 SkillManageRequest 不匹配，强制路由破坏现有签名与测试。skill_manage 工具路径（Agent 自进化核心）已完整走 manage_skill + policy。 | medium | plan-260715-skill-self-evolution.md | 2026-07-15 | open |
| D024 | Dashboard format_messages 本期不新增 DB 列，仅从 last_scan_error 派生，无法长期保存完整 SkillScanWarning detail；若需详情持久化，后续应设计 SkillScanWarning 存储或 frontmatter.raw 扩展，不在本期扩大 schema。 | low | plan-260716-skill-anthropic-format.md | 2026-07-16 | open |
| D025 | metadata list 字段使用逗号分隔 string 序列化，不支持元素内转义逗号；若未来需要复杂列表，应另行设计 metadata 编码约定。 | low | plan-260716-skill-anthropic-format.md | 2026-07-16 | open |
| D026 | `_skill_category`（app/application/skill_service.py）读 `frontmatter.raw.get("category")` 因 T3 normalize 丢弃非白名单/非 legacy 的 category 字段而成为 dead code；目录式 category fallback 仍工作，无功能影响，但 docstring 关于 frontmatter category 的描述已失真。 | low | plan-260716-skill-anthropic-format.md | 2026-07-16 | open |
| D027 | nudge 触发的 background review（`SkillEvolutionService.maybe_trigger` -> `run_background_review`）未注入 `gateway.source`，其 IngressFacts.source 回落 `api`；与 curator consolidation fork 同根问题。当前无害：nudge 复用既有用户 session_id，`create_session_if_allowed` 空操作不覆盖已存 source，且 policy profile 当前不按 source 分流。但语义不符（nudge 非外部 HTTP），未来若 policy 变为 source-aware 会误判。修复需把父会话 source 经 `maybe_trigger` 透传到 `run_background_review(ingress_source=...)`。 | low | N/A | 2026-07-17 | open |
| D028 | `LocalImageStore` 用绝对 serve URL（`{dashboard_base_url}/chat/images/{id}`，默认 `http://localhost:8201`）替换 `signed_url`，便于 `renderMessageText` safeUrl（限 http/https）直渲染、飞书 `download_url` 自环拉取。但非默认部署下（如经 LAN IP / 反向代理访问）`dashboard_base_url` 与浏览器实际来源不一致，浏览器侧图片 URL 会指向 localhost 导致裂图；飞书侧因后端自环拉取不受影响。更稳健方案：serve URL 改相对路径 `/chat/images/{id}` + `renderMessageText` 允许同源相对 URL + 飞书 `send_markdown_reply` 对本地图片 URL 直接读 `LocalImageStore` 文件而非 HTTP 拉取（解耦 base_url 依赖）。 | low | N/A | 2026-07-17 | open |
| D029 | Plugin hook/CLI 注册端到端流通：原问题 file_loader._scan_sync 未传播 hook_registrations。T7 将 PluginService.scan 改为直接用 3 阶段 API 从 PluginContext 收集 hook_registrations；T9 同型模式收集 cli_command_registrations 并增 PluginScanResult.cli_command_registrations 字段。hooks 与 CLI 命令均端到端流通，list_cli_commands 可用。file_loader._scan_sync 未改（scan 不再调它）。 | medium | plan-260717-plugin-capabilities.md | 2026-07-17 | resolved |

---

## 维护规则

1. 技术债来源：功能迭代、代码扫描、设计评审等渠道发现的问题
2. 代码扫描问题处理：代码扫描发现的问题，如非本次变更新引入，应记录到本文件而非要求立即修复
3. 优先级定义：high-影响核心功能或用户体验，需尽快修复；medium-有替代方案，可排期修复；low-优化项，有空闲时处理
4. 状态流转：open -> in_progress -> resolved，resolved 状态保留 1 个月后归档

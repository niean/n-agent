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

---

## 维护规则

1. 技术债来源：功能迭代、代码扫描、设计评审等渠道发现的问题
2. 代码扫描问题处理：代码扫描发现的问题，如非本次变更新引入，应记录到本文件而非要求立即修复
3. 优先级定义：high-影响核心功能或用户体验，需尽快修复；medium-有替代方案，可排期修复；low-优化项，有空闲时处理
4. 状态流转：open -> in_progress -> resolved，resolved 状态保留 1 个月后归档

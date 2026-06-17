# 技术债追踪

| ID | 描述 | 优先级 | 来源计划 | 发现时间 | 状态 |
|----|------|--------|---------|---------|------|
| D001 | `docker compose config` 会展开本地 `.env` 中的 Provider API Key，运行验收命令时可能在终端输出敏感配置。 | medium | plan-260611-chat-fullscreen.md | 2026-06-11 | open |
| D002 | `tests/test_docker_compose_config.py` 期望 `docker/.env.example` 包含 `http://n-kb:8212`，但当前示例配置为空值，导致全量 pytest 失败。 | medium | plan-260615-provider-active-check.md | 2026-06-15 | open |
| D003 | GatewayConfirmation 持久化：`GatewayCommandService.pending_confirmations` 保持为进程内 dict，多副本部署或进程重启会丢失未确认请求；待未来需要多副本时设计 Domain `GatewayConfirmationRegistry` 端口与 SQLite 实现。 | low | plan-260616-feishu-natural-schedule.md | 2026-06-17 | open |

---

## 维护规则

1. 技术债来源：功能迭代、代码扫描、设计评审等渠道发现的问题
2. 代码扫描问题处理：代码扫描发现的问题，如非本次变更新引入，应记录到本文件而非要求立即修复
3. 优先级定义：high-影响核心功能或用户体验，需尽快修复；medium-有替代方案，可排期修复；low-优化项，有空闲时处理
4. 状态流转：open -> in_progress -> resolved，resolved 状态保留 1 个月后归档

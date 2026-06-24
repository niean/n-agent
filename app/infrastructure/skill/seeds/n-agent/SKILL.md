---
name: n-agent
description: N-Agent 操作手册（首期覆盖定时任务，后续追加 MCP / Gateway 等章节）
version: 1
platforms: []
tags:
  - n-agent
  - manual
---

# N-Agent 操作手册

本 skill 是 N-Agent 自身的操作手册。当 Agent 需要管理受管资源（定时任务、MCP 站点、Gateway 等）时，
先通过 `skill_view("n-agent")` 加载对应章节，再调用相应工具。

## Cron Jobs / 定时任务

### 工具
- `manage_schedule(action, ...)`: create / update / pause / resume / run / remove。
- `schedule_query(action, ...)`: list / get。

### Cron 表达式
- 仅支持标准 5 字段：`分 时 日 月 周`。例如 `0 9 * * *` 表示每天 9:00。
- 不支持 `every 30m`、ISO 一次性时间等扩展语法；如用户表述模糊，先追问而不是猜测。

### Timezone
- 默认 `Asia/Shanghai`；如用户明确指定（"按 UTC"、"美东时间"等），使用对应 IANA 时区名。

### Prompt 自包含
- 任务到点执行时不会附带当前对话上下文，prompt 必须自含目标，例如"提醒我看日报"。
- 不允许在 prompt 中引用 `刚才`、`上面`、`这个任务` 等隐含上下文。

### Delivery target
- 飞书会话内创建任务，默认 `delivery_target="origin"`，系统会自动注入飞书投递目标。
- 定时任务执行时，Agent 只需要返回要通知用户的最终内容；不要要求用户提供飞书 Webhook，也不要声称缺少飞书 IM 发送工具。最终内容会由调度器自动投递到飞书 home chat 或其他已配置目标。

### 删除任务
- 自然语言"删除任务"不会直接删除：`manage_schedule(action="remove", task_id=...)` 返回 confirmation_required，
  并提示用户发送 `/schedule remove <id>` 走确认卡。

### 典型对话样例

用户："每天早上 9 点提醒我看日报。"
Agent: 调用 `manage_schedule(action="create", name="日报提醒", prompt="提醒我看日报", cron_expression="0 9 * * *")`。

用户："把日报提醒改到 10 点。"
Agent: 先 `schedule_query(action="list")` 找到对应任务，再 `manage_schedule(action="update", task_id="...", cron_expression="0 10 * * *")`。

用户："列出我的定时任务。"
Agent: `schedule_query(action="list")`。

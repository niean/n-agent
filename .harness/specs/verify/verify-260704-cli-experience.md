<!-- SUMMARY: CLI 生产级改造（spec-260704-cli-experience）人工/半人工验收清单，仅保留 TUI 交互与流式渲染视觉项 -->
# 验收清单：CLI 生产级改造（人工验证部分）

对应 spec：spec-260704-cli-experience.md（已归档至 completed/）
对应 plan：plan-260704-cli-experience.md（已归档至 completed/）

仅保留必须人工或半人工验证的 TUI 交互与流式渲染项。可脚本化/已自动化的项（入口帮助、子命令 JSON 输出、Chat 非流式、规范/测试/已知边界）不在本清单，由 pytest 与冒烟脚本覆盖。

入口说明：docker pod 内执行 `python -m app.interfaces.cli chat` 进入 REPL；单次消息 `python -m app.interfaces.cli chat "..."`。

---

## 1. REPL 交互

### 1.1 启动与退出
- [x] 1.1.1 REPL 启动：n-agent chat → 进入 > prompt
- [x] 1.1.2 Ctrl+D 退出：REPL 空输入时 Ctrl+D → 退出码 0
- [x] 1.1.3 Ctrl+C 退出：REPL 空输入时 Ctrl+C → 退出码 0

### 1.2 本地 Slash 命令
- [x] 1.2.1 /help：REPL 输入 /help → 输出本地+Gateway 命令帮助，不退出
- [x] 1.2.2 /clear：REPL 输入 /clear → 清屏，不退出
- [x] 1.2.3 /history：REPL 输入 /history → 输出 ~/.n-agent/cli_history 路径

### 1.3 Gateway Slash 命令
- [x] 1.3.1 /sessions：REPL 输入 /sessions → 列出当前会话
- [x] 1.3.2 /status：REPL 输入 /status → 输出 health snapshot

### 1.4 破坏性命令确认
- [x] 1.4.1 /new 确认提示：REPL 输入 /new → 输出 "请确认执行 /new"，提示 /confirm
- [x] 1.4.2 /confirm once：1.4.1 后输入 /confirm once → 执行 /new，输出 "已创建新会话"
- [x] 1.4.3 /cancel：再次 /new 后输入 /cancel → 输出 "已取消"
- [x] 1.4.4 /confirm trust：/new 后输入 /confirm trust → 执行 /new 且后续 /new 不再要求确认

### 1.5 补全与权限
- [x] 1.5.1 历史补全：REPL 输入 / 后 Tab → 弹出 /new /rename /delete ... 补全列表
- [ ] 1.5.2 历史文件权限：ls -la ~/.n-agent/cli_history → 文件 0600，目录 0700

### 1.6 视觉一致性
- [x] 1.6.1 TUI 不乱屏：REPL 中交替发消息 + /help → rich 输出与 prompt 不交错混乱

---

## 2. 流式渲染

- [ ] 2.1 content 增量：chat "写一首五言绝句" → content 分块流式输出，非一次性
- [ ] 2.2 工具调用事件：chat "用 calculator 算 1+2" → 输出 [pending] calculator {...} 后 [success] calculator {...}，再输出 content
- [ ] 2.3 错误渲染：chat "测试"（配置错误模型）→ 红色输出 Error 信息，退出码非 0
- [ ] 2.4 duplicate 事件：同一 event id 重发 → 输出 "duplicate event ignored" warning
- [ ] 2.5 markdown 渲染：chat "用表格列出模型" → 回复中的 markdown 表格被 rich 正确渲染
- [ ] 2.6 Ctrl+C 中断流式：流式输出中 Ctrl+C → 输出 [interrupted]，回到 prompt（不崩溃）

分块流式输出、显示 [pending]，验证时未看到流式效果，结果更像是非流式（分块回放、而非真实流式）
<!-- SUMMARY: Policy Governance 人工端到端验收清单，覆盖 API、Dashboard、Gateway、Schedule、Budget、Sandbox、InformationFlow 和多入口运行时治理 -->
# 验收清单：Policy Governance（人工端到端部分）

对应 spec：spec-260714-policy-governance.md
对应 plan：plan-260714-policy-governance.md

仅保留必须人工端到端验证的项。可脚本化/已自动化的项（Domain Policy 决策表、Application 强制边界、并发 reserve/settle/release、严格 warning、compileall 和架构依赖扫描）由自动化测试覆盖，不在本清单。

前置准备：启动使用独立 SQLite 数据库的 N-Agent 验收实例，设置 `BASE_URL`（例如 `http://127.0.0.1:8201`）；准备可正常调用且支持 tool calling 的测试模型。涉及飞书、Schedule 投递、ACP、Docker Sandbox 或密钥脱敏的项目，分别需要测试飞书应用与 allowlist、home target、ACP 客户端、可用 Docker，以及只用于验收的临时 Provider 和一次性 API key，禁止使用生产密钥。

---

## 1. OpenAI-compatible API

- [ ] 1.1 API selector、多轮续聊与不可信 metadata：先用固定 `X-Session-ID: api-policy-uat` 连续发送“记住验证码 7319”和追问“刚才的验证码是什么”，再不传 header、仅在 body 加入 `"metadata":{"session_id":"metadata-forged-session","execution_mode":"unattended","gateway.platform":"feishu"}` 发送普通消息，最后查看 `GET /chat/sessions/api-policy-uat`、`GET /chat/sessions/metadata-forged-session` 和 session 列表 -> 固定 header 的两轮历史连续且第二轮能回答 7319；伪造 id 不存在，缺省 header 的请求创建新的 `api-` session，body metadata 没有成为 selector、execution mode 或 Gateway 身份。

## 2. Dashboard

- [x] 2.1 Dashboard 多轮与刷新恢复：打开 `$BASE_URL/chat` 创建 session，连续发送两轮有上下文关联的消息，刷新页面后重新选择该 session -> 页面调用专用 `/chat/completions`，两轮消息和回答仍在同一历史中，刷新后内容完整恢复。
- [ ] 2.2 API/Dashboard source 隔离：先执行 `POST /chat/sessions?session_id=dashboard-policy-uat`，再分别用 OpenAI API 的 `X-Session-ID: dashboard-policy-uat` 和 Dashboard `/chat/completions` 的 `X-Session-ID: api-policy-uat` 发消息 -> 两次都返回 HTTP 409，错误码分别为 `api_session_scope_mismatch` 和 `dashboard_session_scope_mismatch`，两个 session 的原有历史均未新增消息。

## 3. Gateway 与受管操作

- [x] 3.1 飞书身份、确认和会话授权：使用 allowlist 内账号在测试群发送普通消息并执行 `/new`，由另一个账号尝试操作该确认卡，再由原账号选择“执行一次”；随后再次执行 `/new` 并选择“本会话信任” -> 普通消息正常回复，非原 actor 操作不执行，原 actor 的一次授权只执行一次，本会话信任仅对同一 actor/session 生效，卡片中不展示敏感工具参数。
- [ ] 3.2 飞书 managed action 与 HTTP 伪造隔离：在飞书测试群设置 home target 后创建并列出一个定时任务；随后从 OpenAI API 在 metadata 中伪造相同 `gateway.platform`、actor 和聊天标识并要求创建或删除定时任务 -> 飞书入口可以按确认和 ownership 规则完成操作，HTTP 请求不能获得飞书 managed tool 权限，既有任务及 home target 不被伪造请求修改。

## 4. Schedule unattended

- [ ] 4.1 正常执行、受限能力和投递：创建一个只返回当前时间的任务和一个要求修改其他任务、写外部记忆或启用 Sandbox 网络的任务，分别在 Dashboard 点击“立即运行”，查看 `/chat/scheduled-tasks/{task_id}/executions` 和测试 home target -> 正常任务记录为成功并只投递一次；受限任务以 unattended 能力运行，不能修改其他任务、扩大记忆写权限或开启网络，执行记录可观察且不存在对应副作用。

## 5. Budget 与 Sandbox

- [ ] 5.1 per-run Budget 耗尽与隔离：在独立实例设置 `N_AGENT_BUDGET_MAX_LLM_CALLS=1`，通过固定 `X-Session-ID` 要求“调用 get_current_time 后解释结果”，随后在同一 session 再发送一条普通问候 -> 第一轮需要第二次 LLM 调用时显示“已达到用量上限，请稍后重试或联系管理员”，不会继续调用 Provider；第二轮拥有新的 run budget，普通问候仍可完成，不继承上一轮已耗尽账户。
- [ ] 5.2 Sandbox grant 与资源边界：在 Docker Sandbox 网络关闭、资源上限较小的独立实例中，请求 `execute_code` 主动访问公网并申请超出 timeout/CPU/memory/callback 上限的执行，再查看 `$BASE_URL/tools/sandbox` 的执行历史 -> 网络或非法扩权被拒绝，合法超限值按服务端上限执行，历史中的实际 backend、时长和状态不超过配置，普通受限代码仍能运行。

## 6. InformationFlow、审计与其它入口

- [ ] 6.1 脱敏和结构化审计：仅在接受一次性 key 的测试 Provider 上设置 `N_AGENT_PROVIDER_API_KEY=uat-policy-secret`，通过 API 要求模型原样复述该字符串，然后检查客户端响应、Usage 页面和服务日志 -> Provider 出站、响应和留存中均不出现 `uat-policy-secret`；Policy 日志是含 `policy/version/decision_kind/reason/run_id/session_id/policy_scope` 的 JSON，且不含 prompt、secret、tool arguments 或完整 trusted claims。
- [ ] 6.2 CLI 与 ACP 共享治理链：执行 `n-agent chat` 完成一轮普通对话和一次需要审批的工具调用，再用 ACP 测试客户端通过 `docker exec -i n-agent-n-agent-1 n-agent acp` 完成 `initialize -> authenticate -> session/new -> session/prompt -> close` -> 两个入口均复用同一 Chat/Tool/Memory Policy 行为，危险工具未经各自审批不会执行，ACP stdout 只有合法 JSON-RPC 帧，创建的会话分别保持 `cli`/`acp` source。

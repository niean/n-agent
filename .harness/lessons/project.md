<!-- SUMMARY: {{项目名称}}开发中的经验教训，AI自主维护 -->
# 项目教训

AI 自主维护，人工可通过提示或建议触发新增/修正。
项目教训绑定{{项目名称}}，不随 Harness 模板提取。

---

### P001: 跨 Compose 项目调用必须显式声明 external network

现象：n-agent 容器调用 `http://nkb.localhost` 或 `http://n-kb:8212` 时，TCP connect 成功但 HTTP 收不到响应，httpx 抛 RemoteProtocolError("Server disconnected without sending a response.")，被 KnowledgeToolExecutor 通用 except 吞成 generic "knowledge search failed"。

根因：n-agent 与 n-kb 由不同 docker compose 项目独立创建，n-agent 默认只连入 `n-agent_default` 网络。Docker Desktop 的内部 DNS 仍能把 `n-kb` 解析到代理 IP（198.18.x.x），TCP 经 NAT 看似可达，但流量没有路由到目标网络，HTTP 响应被丢弃。

教训：跨 Compose 项目消费外部服务时，必须在调用方 compose 文件显式声明被调用方的 network 为 external 并把服务加入；同时 base_url 通过 service name 直连，禁止用 .localhost 这类依赖宿主机 DNS resolver 的占位主机名。诊断 generic 错误前优先检查容器网络拓扑（`docker inspect <container> --format '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}'`），不要只看 base_url 字符串。

来源：bug fix 260614 search_knowledge 功测失败

### P002: tool message content 持久化必须与 LLM 协议一致（string）

现象：第二轮请求时 OpenAI-compatible Provider 返回 400 InvalidParameter，提示 messages.content 期望 string 或对象数组，实际是 dict。Task State 显示 failed iter=0。

根因：app/application/agent_graph.py 中 `execute_tools` 给当前 `working_messages` 拼 tool message 时用 `json.dumps(result_payload)` 是 string；但 `update_memory` 给 `memory_store.append_message` 的 `ConversationMessage.content` 直接传了 dict。下一轮 `load_context` 读出后透传到 `working_messages`，发到 Provider 即触发 400。两条路径对同一概念用了两种格式，单轮工作但跨轮回放失败。

教训：把要发给外部协议的字段，持久化时就按协议形态存（tool message content 必须 string）。同时给 `_message_to_provider` 加兜底序列化，覆盖历史已写入的脏数据，避免老会话踩雷。涉及"在内存与持久化间穿梭再回喂给外部 API"的字段，必须有"出协议"和"入存储"格式一致性的测试。

来源：bug fix 260614 跨轮 tool message 400

### P003: 事件幂等唯一键不得让缺失外部 ID 互相冲突

现象：飞书长连接中 `/new` 新建 session 成功，但后续普通 IM 消息收不到回复。Gateway 返回 duplicate，未进入 ChatCompletionService。

根因：飞书事件有时缺失 message_id，Gateway 传空字符串给 SQLiteGatewaySessionRegistry.mark_event_processed。gateway_processed_events 表将 message_id 定义为 NOT NULL DEFAULT '' 且 UNIQUE(source_type, message_id)，导致不同 event_id 的缺失 message_id 事件都以空字符串冲突，第一次事件后后续事件被误判重复。

教训：外部系统的可选 ID 不应以空字符串参与唯一约束；缺失值应存 NULL，并保留主事件 ID 作为幂等主键。修改 SQLite schema 时要补旧库迁移测试，确认 create_app 启动初始化能升级既有本地数据库。

来源：bug fix 260616 飞书 IM 普通消息被误判 duplicate

### P004: 飞书长连接事件的可选字段必须在入口归一化

现象：飞书长连接收到 IM 事件后没有回复，日志显示 feishu event handler failed，最终异常为 sqlite3.IntegrityError: NOT NULL constraint failed: gateway_conversations.thread_id。

根因：飞书消息事件中的 thread_id 可能为 null，FeishuLongConnectionGateway 直接把 message.get("thread_id", "") 传给 GatewaySessionKey；当 key.thread_id 为 None 时，SQLiteGatewaySessionRegistry 创建 gateway_conversations 写入 TEXT NOT NULL 字段失败。

教训：外部事件入口不能依赖 dict.get 的默认值处理 null；get 只在 key 缺失时生效，字段存在但值为 null 时仍会透传 None。飞书入口所有进入 Domain/SQLite 的可选字符串字段（chat_id/open_id/thread_id/message_id/event_id/content.text）必须先归一化为字符串。

来源：bug fix 260616 飞书 thread_id null 导致长连接处理失败

### P005: 平台主动外发能力不该用 per-message capability 抽象

现象：飞书会话里通过自然语言创建的定时任务到点执行成功（LLM 输出正常），但飞书投递阶段全部 fail-closed 失败：`origin does not support active_text_delivery`。

根因：origin 字段从飞书入口（feishu_long_connection.py）→ Gateway `_build_trusted_metadata` → 工具 `_origin_from_trusted` → ScheduleService → SQLite 链路上，`capabilities=["active_text_delivery"]` 被工具层 `_origin_from_trusted` 丢弃；ScheduleOutboundDelivery 用 `capabilities` 守门，落库的 origin 缺字段就投递失败。同时 trusted_metadata 用 `gateway.source_type` 命名而 origin/outbound 期望的是 `source_type`，工具层也未做映射，导致整个链路双重断点。

教训：把"平台是否支持主动文本外发"这种 platform-级固有能力，放进每条消息 metadata 让中间层逐跳搬运，会变成"任意一环漏传就 fail-closed"的隐式契约。正确做法是按 platform 注册（feishu_client 是否注入）作为单一来源，outbound 只按 `source_type` 路由。所有"携带语义在多层间穿梭、又只在末端校验"的字段，要么去掉中间环节、要么作为类型化值对象（不是裸 dict）保证字段不丢；裸 dict 透传 + 末端 fail-closed 是组合反模式。

来源：bug fix 260617 飞书定时任务投递失败

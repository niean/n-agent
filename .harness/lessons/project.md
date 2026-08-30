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

### P006: 冗余落库上下文字段必须成对迁移

现象：定时任务 `sched-a406eae127164f3a970f63dbfab24c5d` 每 5 分钟执行成功，LLM 输出正常，但飞书投递全部失败，执行记录显示 `delivery_status=failed`、`delivery_error=origin missing platform`。

根因：旧数据里 `origin_json.source_type` 和 `delivery_context_json.source_type` 同时存在。此前迁移只把 `origin_json.source_type` 改为 `platform`，但实际投递路径 `ScheduleOutboundDelivery.deliver` 读取的是 `delivery_target.context`，也就是 `delivery_context_json`。因此 Dashboard/任务详情看到 origin 已经有 platform，但 outbound 仍从 delivery context 读不到 platform。

教训：同一业务上下文如果为了展示/执行分别冗余存两份，字段改名或 schema 迁移必须覆盖所有读取路径对应的列，并用一条回归测试同时断言展示侧字段和执行侧字段。排查时不要只看 `origin_json` 这类语义主字段，要顺着实际调用链确认运行时读的是哪一份持久化数据。

来源：bug fix 260617 飞书消息投递失败 origin missing platform

### P007: Docker Desktop published port 卡死要区分容器内健康与宿主端口健康

现象：执行 `docker/restart.sh` 后，容器内访问 `http://127.0.0.1:8201/health` 返回 200，但宿主 `curl http://nagent.localhost/health` 长时间卡住，最终 nginx 返回 504。

根因：`nagent.localhost` 先进入宿主 nginx，再反代到 Docker published port `127.0.0.1:8201`。Docker Desktop 的 port proxy 在 `docker compose up --force-recreate` 后偶发进入半坏状态：TCP 可建连，但请求没有被正确转发/回写；此时应用和容器网络都正常，重启 service 会刷新 port proxy。

教训：本地 Compose 重启脚本必须分别检查容器内 health、宿主端口 health 和 public nginx health，并给所有 curl 设置硬超时。若容器内 health 通过但宿主端口无响应，应自动 `docker compose restart <service>` 刷新 Docker Desktop port proxy，而不是继续等待 nginx 504 或误判为应用启动慢。

来源：bug fix 260626 restart 后 nagent.localhost health 偶发卡死

### P008: 沙盒审计历史不能复用会话 tool_calls 作为唯一持久源

现象：沙盒废弃/释放后，Dashboard 虽能看到释放记录，但 execute_code 执行历史仍会在删除 Chat Session 或清理会话消息后消失，用户感知为“SQLite 存储了但长期保存没达成”。

根因：上一次只把废弃沙盒生命周期写入 `sandbox_released_history`，执行历史仍从 MemoryStore 的 `tool_calls` 表读取。`tool_calls` 属于 Chat Session 上下文，会被 `MemoryStore.delete_session` 级联删除，不适合作为沙盒审计记录的唯一来源。

教训：审计/运维历史如果要求长期保存，必须有独立于业务会话生命周期的 registry/table，并在 Dashboard 读路径上优先使用该表；兼容旧数据可以合并 legacy 表，但不能继续把 legacy 表当作权威来源。

来源：bug fix 260701 沙盒执行历史长期保存

### P009: 上下文压缩必须持久化"已被摘要"标注，否则跨轮 load 无法恢复压缩后状态

现象：spec 03-prd-specs.md:328 把摘要持久化从 replace 改为 append（保留所有摘要记录供 Dashboard 渲染）后，用户报告压缩后 head 3 消失。第一轮 fix 用"保留全部非摘要消息"恢复 head，但引入 middle + summary 冗余（违背压缩算法）；第二轮 fix 用"head + summary + 之后"丢弃 middle，但同时丢掉了 tail（tail 在 DB 中位于 summary 之前）。

根因：DB 只持久化消息详情（role/content/is_summary），没有标注哪些原始消息已被摘要吸收。压缩算法执行后 in-memory working_messages = head + [summary] + tail，middle 已移除；但 DB 中 middle 仍在（append 语义只 INSERT summary 不 DELETE middle）。跨轮 load 时拿到 [head, middle, tail, summary, new_msgs]，无法区分 middle（应丢弃）和 tail（应保留）--两者都是非摘要消息、都在 summary 之前。错误根因跨 Application（agent_graph.py load/compress）、Infrastructure（sqlite_store.py 持久化）、Domain（context.py 压缩结果）三层。

教训：上下文消息需要动态计算的加载，持久化层必须标注好每条消息的压缩状态。加 is_summarized 字段标记"已被摘要吸收的原始消息"，压缩成功后 mark_messages_summarized(middle_ids)，load 时 WHERE is_summarized=0 过滤。这样 load 拿到的就是 [head, tail, summary, new_msgs]（middle 已过滤），_filter_to_latest_summary 只需处理旧摘要去重。涉及"持久化形态变更"的改动必须有跨轮加载的回归测试，断言 middle 被标记、load 后 working_messages 不含 middle 且保留 head/tail。

来源：bug fix 260711 head 3 被遗弃 -> 260711 is_summarized 标注

### P010: uvicorn 0.30+ 默认 LOGGING_CONFIG 不配置 root logger，应用 INFO 日志会静默

现象：观测页验收 3.1 失败——发送对话后服务日志只出现 uvicorn access log（`INFO: 192.168.65.1:xxx - "POST /v1/chat/completions HTTP/1.1" 200 OK`），完全没有 `API call model=... provider=... in=... out=... total=... latency=...ms` 行。但 usage_records 表有数据、sessions 表 api_call_count 已累加，代码逻辑本身正确。

根因：uvicorn 0.30+ 默认 `LOGGING_CONFIG` 移除了 root logger（`""`）的配置项，只配置 `uvicorn` / `uvicorn.access` 两个独立 logger。应用层 `logger = logging.getLogger("app.application.agent_graph")` 创建后，root logger 无 handler、effective level 回落到 WARNING，`logger.info()` 完全静默。uvicorn.access logger 单独配置了 handler + level=INFO，所以 access log 仍可见，造成"只有 access log 没有 app log"的假象，容易误判为代码逻辑 bug。诊断时容器内 `python -c "import logging; print(logging.getLogger().handlers, logging.getLogger().level)"` 返回 `[] WARNING` 即可确认。

教训：升级 uvicorn 主版本后必须验证应用 logger 可见性。uvicorn 0.30+ 移除了 root logger 配置，HTTP 应用工厂必须显式 `logging.basicConfig(level=logging.INFO, format=...)`（幂等，root logger 已有 handler 时不覆盖），但不能在共享装配模块 import 时配置：CLI/TUI 也会导入 `app.main` 复用 `build_application_services()`，import 副作用会把插件注册、启动扫描等 INFO 日志泄漏到终端 UI。诊断"日志缺失/多余"类问题先按 HTTP、CLI、ACP 入口分别验证 root logger 的 handlers 与 effective level，再检查业务日志。还要避免在多层输出重复日志——call_llm 和 UsageService.record_call 都输出 "API call" 时，前者用原始 usage dict、后者用归一化后的 CanonicalUsage，Anthropic usage 无 `total_tokens` 字段导致前者 `total=0`，重复日志不仅冗余还会暴露归一化不一致的 bug。

来源：bug fix 260712 观测 3.1 API call 日志缺失

### P011: 面向用户/LLM/调试的 JSON 序列化必须 ensure_ascii=False，否则中文变 \uXXXX 字面

现象：Dashboard 对话页"工具调用调试信息"中，工具返回的中文显示为 \uXXXX 转义字面（如 查看天气），看不懂。

根因：tool 消息 content 经双层 json.dumps 产生：工具执行器（skill_service 等）用 `content=json.dumps(payload)`（默认 ensure_ascii=True）返回字符串型 content，agent_graph.py 再 `json.dumps(result_payload)`（默认 ensure_ascii=True）包一层存为 tool message content。前端 chat.js formatDebugJson 对该字符串只 JSON.parse 一次，只能解开外层；内层字符串里的 \uXXXX 字面无法还原，JSON.stringify 后显示字面转义。content 为 dict 的工具经前端单次 parse 可还原，仅字符串型 content 双层编码触发。根因跨 Interfaces（chat.js formatDebugJson）和 Application（agent_graph/skill_service）层。

教训：面向用户/LLM/调试输出的 JSON 序列化统一用 ensure_ascii=False，保证中文原文可读；多层 json.dumps 嵌套时前端单次 JSON.parse 只解一层，内层字符串里的 \uXXXX 字面无法还原，修复需同步改汇聚点（agent_graph 的 tool message content 序列化）和各字符串型 content 生成点（工具执行器）。审计日志等需字节级哈希稳定的场景才用 ensure_ascii=True。相关：P002 tool message content 持久化形态。

来源：bug fix 260716 工具调用调试信息中文被编码

### P012: Dashboard 是跨源调试 UI，ingress source 校验不应阻断跨源会话续发

现象：在 Dashboard Chat 上选中飞书类型的会话发消息，返回 409 `dashboard_session_scope_mismatch`，无法续发；8649dc4 前走 `/v1/chat/completions` 时可正常发送。

根因：commit 8649dc4（Policy领域自治，spec-260714）给 Dashboard 专用 `/chat/completions` 路由加了 `SessionBootstrapReader.describe(x_session_id, expected_source="dashboard")` 强校验，飞书会话 source=feishu != dashboard 触发 `SessionScopeMismatchError` 返回 409。同时 chat.js 从 `/v1/chat/completions` 迁到 `/chat/completions`，`/v1` 也加了 `expected_source="api"`，两条入口都被作用域校验封死。但 Dashboard 会话列表 `/chat/sessions` 展示所有来源会话，operator 期望能续发任意会话进行调试，作用域强校验与 Dashboard 的跨源调试定位冲突。根因跨 Interfaces（dashboard.py）和 Application（session_bootstrap.py）层，叠加一个 spec 设计决策。

教训：Dashboard 是 operator 跨源调试 UI（会话列表展示全部来源会话），其 chat 入口不应按 ingress source 强校验会话作用域，应用 `describe_unchecked(provisional_source=...)` 仅保留"会话必须已存在"校验。`describe(expected_source)` 适用于"ingress 与会话来源必须一致"的对外协议入口（如 `/v1/chat/completions` 限 api），`describe_unchecked` 适用于"接口已自行校验所有权、需跨源续发"的运维/调试入口（Dashboard、Gateway、Schedule）。IngressFacts.source 记录请求实际入口（dashboard），descriptor.source 记录会话原始来源（feishu），两者分离流入 PolicyProfileFacts，当前 system scope provider 忽略 source 不影响策略；`create_session` 用 INSERT OR IGNORE，续发不会覆盖既有会话 source。改动 ingress 作用域校验前必须检查会话列表是否跨源展示，避免"看得到选不了"。相关：spec-260714 第 350 行 Dashboard-scope 决策已被本修复反转。

来源：bug fix 260716 Dashboard 无法给飞书会话发消息

### P013: 外部临时资源（OSS 签名 URL）用于本地展示前必须持久化，不能假定 URL 永久可用

现象：photo-upload 拍照上传的图片，在飞书 IM 可正常预览，但在 Dashboard Chat 框先显示、约 1 小时后变成裂图。

根因：photo-upload Skill（宿主脚本）上传图片到阿里云 OSS 后返回 STS 签名 URL，`URL_LIFETIME_SECONDS=3600`（1 小时过期）。host_terminal_tool_executor 把该签名 URL 作为 `signed_url` 返回给 LLM，LLM 按 Skill 指令输出 `![照片](签名URL)`，该回复被原样存入会话消息并投递飞书。飞书投递链路 `send_markdown_reply` 会在投递瞬间把图片**下载并重传为飞书永久 image_key**，故飞书侧永久可预览；但 Dashboard Chat 直存原始签名 URL，超过 1 小时后 `<img>` 请求返回 403、变裂图。根因跨 4 层：宿主 Skill（photo-upload.py）-> Application（host_terminal_tool_executor 返回 signed_url）-> Infrastructure（消息存储原始 URL）-> Interfaces（Dashboard 直渲染原始 URL）。飞书与 Dashboard 对同一 URL 的"是否重传持久化"处理不对称，是 bug 根源。

教训：外部临时资源（签名 URL、临时 token、短链接）在用于本地展示前必须持久化，不能假定 URL 永久可用。不同渠道对同一资源可能做不同处理（飞书重传为永久 image_key，Dashboard 直存原始 URL），跨渠道展示一致性需在"资源进入会话消息"这一汇聚点统一持久化，而非依赖各渠道各自处理。本次在 host_terminal 工具成功时（URL 仍 fresh）下载图片落地 `LocalImageStore`，返回永久 serve URL 替换 `signed_url`，飞书投递和 Dashboard 展示都用该永久 URL。相关：D028 非默认部署下 base_url 限制。

来源：bug fix 260717 Chat 框飞书可预览图片过期裂图

### P014: Application service 返回类型契约变更必须同步迁移所有 Interface 消费方（CLI + HTTP routes）

现象：终端执行 `n-agent task ls` 直接报 `'TaskListPage' object is not iterable`，命令不可用。

根因：commit 71772ba（任务入口）把 `TaskService.list_tasks` 返回类型从可迭代的 `tuple[Task,...]` 改为分页包装 `TaskListPage(items, next_cursor)`。HTTP 路由 `task_routes.py` 全部正确迁移为 `page = await task_service.list_tasks(...)` 后访问 `page.items`/`page.next_cursor`，但 CLI `app/interfaces/cli/commands/task.py::_cmd_list` 仍 `for t in tasks` 直接迭代 page 对象，`TaskListPage` 未实现 `__iter__` 即报错。根因跨 domain（task.py 定义 TaskListPage）、application（task_service.py 返回 page）、interfaces（cli 漏迁 / http 已迁）三层。CLI 侧无 `task list` 回归测试，迁移时漏改未被发现。

教训：Application service 方法的返回类型是 CLI 与 HTTP routes 共享的契约（见模式十九 _load_xxx_service indirection），改返回类型（尤其可迭代对象 -> 包装对象，如 list/tuple -> Page/Result wrapper）时必须 grep 全部消费方同步迁移到新字段（`.items`/`.data`/`.result`），并给每个 Interface 消费方补一条回归测试（CLI 用 monkeypatch `_load_xxx_service` 返回 Fake，断言 rc==0 且渲染含期望 id）。CLI 子命令测试覆盖不全时，契约变更最易在 CLI 侧漏迁。相关：P006 冗余字段迁移必须覆盖所有读取路径。

来源：bug fix 260719 n-agent task ls 报 TaskListPage not iterable

### P015: Dashboard 前端必须按后端路由实际响应 shape 消费，并补 Node 行为夹具防契约漂移

现象：`n-agent task ls` 能看到 2 个任务（triage 状态），但 Dashboard 任务看板一列卡片都不显示。

根因：后端 `/chat/tasks/board` 返回 `columns` 为对象数组 `[{status, cards, total}, ...]`、`archived` 为布尔标志（`task_routes.py`，且有 `test_task_routes.py` 断言此数组 shape）；但前端 `tasks.js` 把 `state.board.columns` 当作以 status 为键的 dict 取（`columns[col.key]`），数组按字符串索引恒为 `undefined`，导致每列 `items=[]` 不渲染卡片；同时 `state.board.archived` 被当作卡片数组（实为布尔）取 `.length`，archived 开关也只调 `renderBoard()` 不带 `?archived=true` 重拉。前后端 shape 不匹配，且 `tasks.js` 无任何前端行为测试，靠人工验收才发现。根因跨 interfaces 层 3 文件（task_routes.py / tasks.js / management-api.js）。

教训：Dashboard 前端 JS 消费 HTTP 路由时，必须严格按路由实际响应 shape（数组/dict、字段名）解析，不能凭前端臆想的 shape 取值；后端 route 有 Python 合同测试但前端无对应消费测试时，shape 漂移只在浏览器暴露。新增/改动 Dashboard 前端模块时必须补一个无依赖 Node 行为夹具（vm + 最小 DOM mock + 返回真实后端 shape 的 mock api，仿 `security_frontend_harness.js` / `tasks_frontend_harness.js`），断言关键渲染分支（卡片数、列数、开关重拉参数）。archived/toggle 类开关必须走"重拉带查询参数"而非仅本地重渲染。相关：P014 接口消费方契约迁移。

来源：bug fix 260719 Dashboard 任务看板不显示任务

### P016: managed CONFIRM 工具对 unattended worker 有"暴露+执行"双闸门，两闸都依赖 permitted_managed_tools

现象：Task worker 执行任务时，系统提示让它用 task_complete/task_heartbeat/task_show 等 task 工具提交产物，但 worker 会话里根本没有这些工具，worker 只能放弃并把"无法完成"当结果，任务被标记 done（consecutive_failures=0）。

根因：task 工具是 `managed=True, risk_level=CONFIRM, toolset="task"`，TaskAgentExecutor 已把 7 个工具名写入 `trusted_metadata["permitted_managed_tools"]`，但 worker 实际看不到也调不了，因为两道闸门都断了：(1) 暴露闸 `ToolPolicy.can_expose` 在 `SAFE_ONLY`（unattended 模式自动启用）下只放行 SAFE 工具，CONFIRM managed 工具被隐藏，LLM 拿不到 schema；(2) `ChatCompletionService._compute_permitted_managed_tools` 对 `mode != "realtime"` 直接返回空集，把 executor 声明的 permitted_managed_tools 丢弃，`context.permitted_managed_tools` 为空，执行闸 `evaluate_execution` 也放行不了。两闸都依赖 `permitted_managed_tools`，缺一不可。根因跨 domain（tool_policy）、application（chat_service/tool_service）、executor（task_agent_executor）。无 worker 工具面端到端测试，靠人工跑任务才发现。

教训：managed CONFIRM 工具对 unattended worker 有双闸门--暴露闸（can_expose，LLM 是否看到 schema）与执行闸（evaluate_execution，调用是否 ALLOW），两闸都读 `context.permitted_managed_tools`。给 unattended worker 增加_managed 工具时必须同时：(a) executor 把工具名写入 permitted_managed_tools（已有）；(b) `_compute_permitted_managed_tools` 对 unattended 荣誉 executor 声明（不能一律返回空）；(c) `can_expose` 在 SAFE_ONLY 下对 `definition.managed and name in permitted_managed_tools` 放行（managed 是服务端声明、无需交互审批，不违背"unattended 无审批通道"原则，区别于普通 CONFIRM）。改动任一闸门必须配三处单测：can_expose 单元、_compute 传播、list_openai_tools 端到端暴露。相关：P017 trusted_claims 镜像。

来源：bug fix 260719 task worker 看不到 task 工具

### P017: executor 写入 trusted_metadata 的子字典必须镜像进 IngressFacts.trusted_claims

现象：P016 修复后 worker 能看到 task 工具了，但调用 task_show/task_complete 时被拒 `trusted_task_context_missing`，worker 无法读取任务上下文、无法提交结果。

根因：TaskAgentExecutor 把 worker 身份（task_id/run_id/claim_lock/write_origin）组装成 `task` 子字典放入 `trusted_metadata["task"]`，但 `IngressFacts.trusted_claims` 只放了顶级字段（task_id/claim_lock 等），没放 `task` 子字典。生产装配了 `policy_snapshot_factory`，ChatCompletionService 在 `_build_policy_snapshot` 用 `request.ingress_facts.trusted_claims` 构造 snapshot，随后 `trusted_metadata = dict(snapshot.run_context.trusted_claims)` 整体替换 trusted_metadata（chat_service.py:169）--只放在 trusted_metadata 的 `task` 子字典被丢弃，`ToolExecutionContext.trusted_metadata["task"]` 为空，`TaskManagementToolExecutor._origin_from_trusted` 读不到返回 None，工具报 `trusted_task_context_missing`。根因跨 application（chat_service policy snapshot 路径）与 executor（trusted_claims 构造）。

教训：服务端 executor 写入 `trusted_metadata` 的每个键（尤其子字典如 `task`）必须同时镜像进 `IngressFacts.trusted_claims`。原因：有 policy_snapshot_factory 时，ChatCompletionService 用 snapshot.run_context.trusted_claims（源自 IngressFacts.trusted_claims）整体替换 trusted_metadata，trusted_metadata-only 的键会被丢。trusted_claims 是"服务端声明经 snapshot 透传"的权威通道，trusted_metadata 在 snapshot 路径下只是初始值。诊断 managed 工具报 `trusted_*_missing` 时，先检查对应子字典是否同时在 ingress_facts.trusted_claims，而非只看 trusted_metadata。相关：P016 双闸门、模式十二 trusted_claims 镜像陷阱。

来源：bug fix 260719 task worker 调 task 工具报 trusted_task_context_missing

### P018: 状态机扁平化/去概念时必须全栈同步清理，E2E 旧状态断言也要改

现象：Task 状态机从 9 状态扁平化为 Manus 7 状态、移除 assignee/依赖图/swarm 时，逐层改 domain enum/field/policy/registry schema/service/run_service/executor/tools/routes/CLI/前端，但 test fixture（test_task_management_tools 引用 BlockKind、test_task_outbound_delivery 引用 TaskStatus.DONE）与 E2E 脚本（task.sh 断言 `"status": "triage"`）未同步，导致 pytest collection ImportError + E2E FAIL。

根因：状态值/字段/枚举是跨层契约，移除一个枚举值（如 BlockKind/DONE/TRIAGE）会连带所有引用它的测试 fixture 与 E2E 断言。测试与 E2E 脚本也是契约消费方，常被遗漏。

教训：状态机/领域概念扁平化或移除时，全栈清单必须包含：domain(enum+field+VO+port) -> policy -> infrastructure(schema+迁移+方法) -> application(service/run_service/executor/tools) -> interfaces(routes+CLI+前端) -> tests(fixture+断言) -> E2E 脚本(状态断言) -> knowledge(02-architecture/22-file-map/04-data-boundaries)。诊断 collection ImportError 或 E2E 断言 FAIL 时，先 grep 旧枚举值/字段名定位遗漏点。相关：P015 契约漂移、模式十九 CLI/HTTP 契约。

来源：feature 260719 task-flow-simplify（Manus 7 状态扁平化）

### P019: 新增会话来源时 session_id 必须是"前缀+完整 UUID"，禁止"前缀+业务 id"

现象：Task worker 的 execution_session_id 用 `f"task-{task.id}"`，而 task.id = `t_{uuid4().hex[:16]}`（带 `t_` 前缀、截断 16 hex 非完整 UUID），产出 `task-t_ef55165d8f52472c`。用户在会话列表看到该 id 指出不符合规范。双重违规模式十六：后缀非完整 UUID（16 hex + `t_`），且出现 `task-t_` 双前缀。同时模式十六表漏列 task 来源（SessionSource.TASK 枚举存在、知识库表无），枚举与知识脱节。

根因：新增 SessionSource.TASK（第 10 个来源）时，executor 为"同 task 跨 run 稳定复用 execution session、无需持久化"而用 `task-{task.id}` 派生，但 task.id 是业务 id（`t_` 前缀 + 截断 hex），不是 UUID，直接拼接违反模式十六"前缀+UUID"规则。根因跨 Application（task_agent_executor.py）+ Domain（session.py 枚举）+ 知识（05-key-patterns.md 模式十六表），叠加"为稳定性牺牲合规"的设计取舍错误。属 SessionID 命名反复犯错（见 memory feedback-apply-conventions-not-just-quote）。

教训：新增会话来源时三点必须同时做到：① session_id 严格 `{source}-{完整 UUID}`（模式十六），禁止 `{source}-{业务 id}`--业务 id 常自带前缀/非 UUID（如 `t_`/截断 hex），拼接会双前缀且后缀非 UUID；② 需要跨 run 稳定又不想持久化时，用 `uuid5(namespace, business_id)`（str 形式带连字符，与 `schedule-{uuid4()}`/`curator-{uuid4()}` 一致）从业务 id 确定性派生完整 UUID（同 id->同 UUID，stable），而非直接嵌业务 id；③ SessionSource 枚举与模式十六表必须同步更新（新增来源两处都加），避免枚举/知识脱节。UUID 必须用 str 带连字符形式（8-4-4-4-12，如 `d8701f7f-28f9-4875-94c1-20c2f28cc66e`），禁止 `.hex` 无连字符形式--所有既有来源（dashboard/schedule/curator）都是 `f"prefix-{uuid4()}"`（f-string 直接用 uuid 对象走 `__str__`），不用 `.hex`。修 session_id/source/naming 时必须把规则应用到实际 id 生成代码，不能边引用模式十六边 legitimatize 不合规代码。相关：模式十六、P015 契约漂移。

来源：fix 260720 task worker execution_session_id 合规（用户指出 `task-t_...` 不符规范）

### P020: Task worker 无 task_complete intent 时不可默认 COMPLETED，limit/error finish_reason 必须 FAILED

现象：任务 t_acfc2d6e33bf4ccc 预算耗尽（BUDGET_EXHAUSTED），run summary="已达到用量上限，请稍后重试或联系管理员。"，但任务被标 SUCCEEDED。看板显示 succeeded，Chat 框显示"已完成 | 已达到用量上限"，最终结果不一致。worker 实际未调用 task_complete（预算耗尽直接 finalize），却被误判成功完成。

根因：TaskAgentExecutor._build_result_from_chat 在无 intent event（complete_requested/change_proposed）时默认 COMPLETED。但 BUDGET_EXHAUSTED 在 agent_graph.finalize 设 finish_reason="length"（_END_REASON_TO_FINISH_REASON 映射，与 ITERATION_LIMIT 同）、不设 state.error；chat_service 据此返回 finish_reason="length"（非 "error"）。executor 只检查 `finish_reason == "error"` -> FAILED，"length" 漏网 -> 默认 COMPLETED -> SUCCEEDED。根因跨 task_agent_executor.py（默认 COMPLETED）+ agent_graph.py（BUDGET_EXHAUSTED 映射 length、不设 error）+ chat_service.py（finish_reason 传递），executor 与 agent-loop 的 finish_reason 语义脱节。注意：不能改 BUDGET_EXHAUSTED 映射为 "error"--openai_compatible.py 对 finish_reason=="error" 返回 HTTP error body（非消息体），会破坏正常 Chat UX 且 test_llm_policy_integration 断言 "length"。

教训：Task worker 完成判定必须以"worker 是否显式调用 task_complete（intent event）"为准；无 intent 时按 finish_reason 兜底：`error`/`length`（BUDGET_EXHAUSTED/ITERATION_LIMIT）-> FAILED（worker 未完成），`stop` -> COMPLETED（worker 给出最终答案）。禁止无 intent 一律默认 COMPLETED--任何 limit/error 都会被误标成功。修复优先在消费侧（executor）按 finish_reason 分流，不要改 agent-loop 的 EndReason->finish_reason 映射（该映射服务于正常 Chat 的 OpenAI 兼容编码与 UX，改它会破坏 openai_compatible error body 合同）。相关：P018 状态机全栈同步、模式十六。

来源：fix 260720 task t_acfc2d6e33bf4ccc 预算耗尽被误标 SUCCEEDED（用户指出看板/Chat 最终结果不一致）

### P021: goal_mode 必须把 judge 反馈喂回 worker 并在连续否决时早退，不能无脑跑到 max_turns

现象：任务 t_97d317e953b64edc（goal_mode）worker 连续 10 轮都调 task_complete（自认完成）、judge 连续 10 轮判 not achieved，goal_loop 无早退、耗满 goal_max_turns=10 才判 FAILED。用户质疑"明确失败后未标记错误结束、继续耗尽 10 轮"是否符合预期。

根因：`TaskAgentExecutor.run_goal_loop` 的 "Not achieved" 分支无条件 continue 到 max_turns，且未把 judge reason 喂回 worker--worker 每 turn 重复同一失败做法（task_show 读不到 judge 反馈），judge 持续否决，直到 max_turns。跨 task_agent_executor.py（goal_loop 无早退 + 无反馈）+ task_service.py（build_worker_context 进度段未含 judge 反馈）。对齐 Hermes Ralph loop（goals.py）：judge 每轮判、未达成则把反馈喂给下一轮 worker（continuation prompt）、worker 被指示"受阻则停"；turn budget 是兜底。N-Agent 后台任务无用户介入，额外需早退。

教训：goal_mode 多轮判定必须做到两点：① judge 否决后把 reason 持久化为事件（goal_judge_feedback）并经 build_worker_context 进度段透传给下一轮 worker（对齐 Hermes continuation prompt），使 worker 能 adapt 或主动 task_propose_change/task_cancel；② 连续 N 次（GOAL_MAX_CONSECUTIVE_REJECTIONS=2）judge 否决即"明确失败"早退 FAILED，不耗尽 max_turns--max_turns 仅作 max_turns < 阈值时的兜底。禁止"无反馈 + 无早退"的空转循环。相关：P018 状态机全栈同步、P020 worker 完成判定。

来源：fix 260720 task t_97d317e953b64edc goal_mode 耗尽 10 轮才判失败（用户指出明确失败后应早退）


### P022: Task 失败必须区分用户取消/worker 快速失败/系统失败三态，worker 不得用 task_cancel 表达快速失败

现象：任务 t_a742046a521d46eb（"无法用 execute_code 则直接失败"），worker 发现 execute_code 不可用，调 task_cancel 试图"快速失败"，run summary 自称"moved to the CANCELLED terminal state"，但 task.status 实际为 succeeded（run outcome=completed）。worker 摘要"Failed (cancelled)"与状态机 SUCCEEDED 矛盾，本该失败的任务落成成功。

根因：worker 工具集原本含 task_cancel，TASK_GUIDANCE 指示"无法继续时调 task_cancel"。但 task_cancel 不是 terminal intent（_build_result_from_chat 只识别 complete_requested/change_proposed，无 cancel/fail intent）-> 默认 COMPLETED；且 task_cancel->cancel_task->run_service.terminate->dispatcher.cancel(worker_token) 是 worker 自取消自己的 asyncio task，与 worker 正常 COMPLETED 终结 CAS 竞态，COMPLETED 先赢 -> SUCCEEDED。根因跨 task_tools.py（worker 工具集含 cancel）+ task_agent_executor.py（无 fail intent 检测、默认 COMPLETED）+ task_run_service.py（task_cancel 走 terminate 自取消竞态）+ task_service.py（无 fail 方法）+ task_management.py（_handle_cancel 分发）。语义混淆：把 worker 判定快速失败（应 FAILED）与用户取消（CANCELLED）混用 task_cancel。

教训：Task 终态失败必须三态分离：(1) 取消=用户明确指令 only（/task cancel/按钮 -> TERMINATED -> CANCELLED），worker 不得触发；(2) worker 快速失败=worker 判定无法继续、确定性放弃（必需工具不可用/指令禁止兜底）-> 用独立 task_fail intent（写 fail_requested 事件 -> ABORTED -> FAILED 绕过断路器不重试）；(3) 系统失败=crash/timeout/spawn 非确定性 -> FAILED 走断路器（可重试）。worker 工具集不得含 task_cancel；worker 无法继续必须调 task_fail。worker 主动失败（ABORTED）必须绕过断路器直接 FAILED（区别于可重试的系统 FAILED），否则确定性失败被反复重试。terminal intent 检测（_read_latest_intent）必须覆盖 complete_requested/change_proposed/fail_requested 三种。相关：P020 无 intent 默认 COMPLETED、P018 状态机全栈同步、模式二十八。

来源：fix 260720 task t_a742046a521d46eb worker 快速失败误用 task_cancel 致 run COMPLETED->SUCCEEDED（用户明确"取消只指用户指令，worker 快速失败不是取消"）

### P023: LLM Provider 黑名单滞后致内部 key 透传 SDK，定时任务/unattended worker 整体失败

现象：定时任务 sched-a406eae127164f3a970f63dbfab24c5d 执行报错 `AsyncCompletions.create() got an unexpected keyword argument '_policy_snapshot'`，任务无法完成。

根因：`ChatCompletionService.complete` 在 `policy_snapshot_factory` 可用时把 `RunPolicySnapshot` 实例写入 `options["_policy_snapshot"]`（[chat_service.py:190-191]）。该 options 经 `AgentGraphRunner.run` -> `state.run_options` -> `call_llm` 透传到 `OpenAICompatibleProvider.chat`。OpenAI provider 用黑名单 `_INTERNAL_OPTION_KEYS` 过滤内部 key（`_provider_options`），但该集合**漏了 `_policy_snapshot`**（agent_graph 的同名集合有、注释明示"Mirrors the filter in OpenAICompatibleProvider"但镜像失同步），导致 `_policy_snapshot` 经 `**kwargs` 透传给 `client.chat.completions.create()`，SDK 以 unexpected keyword argument 拒绝。同类滞后还漏了 `force_compress`（会话式压缩时置入）与 `max_iterations`（TaskAgentExecutor 传 `options={"max_iterations":20}`）。Anthropic provider 因用白名单 `_ALLOWED_OPTION_KEYS` 天然屏蔽，不受影响，故仅 OpenAI 路径报错。根因跨 chat_service.py（写入内部 key）+ agent_graph.py（权威黑名单，仅用于 usage gen_params 计算，不阻断 provider 调用）+ openai_compatible.py（黑名单滞后）三文件、跨 Application/Infrastructure 分层。

教训：`options` 字典同时承载 generation param 与内部控制 key，Provider 调 SDK 前必须剥离内部 key。`_INTERNAL_OPTION_KEYS` 有三处定义（agent_graph / openai_compatible / anthropic），agent_graph 集合是权威源，新增内部 key 时三处一并更新（Anthropic 白名单无需动）。治理结构脆弱点：agent_graph 的过滤只管 usage 记录、不阻断 provider 调用，故 agent_graph 有该 key 而 OpenAI provider 漏该 key 时不会在任何单测里暴露（除非专门断言 SDK kwargs）。修复=同步黑名单覆盖 `_policy_snapshot`/`force_compress`/`max_iterations`，并补 `test_provider_strips_policy_snapshot_and_internal_control_keys` 断言。长期改进：OpenAI provider 对齐 Anthropic 切白名单，根除黑名单滞后类问题。相关：模式三十三 options 过滤契约、模式五 LLM Adapter。

来源：fix 260726 定时任务 sched-a406eae127164f3a970f63dbfab24c5d 报错 unexpected keyword argument '_policy_snapshot'

### P024: Dashboard Chat 进程消息过滤范围过宽，误隐藏定时任务投递记录

现象：用户在 Dashboard Chat 查看定时任务会话，看不到投递记录（定时任务执行后投递到飞书的内容）。DB 与 `/chat/scheduled-tasks/{id}/executions` API 均有 delivery_status=success 记录，schedule 会话内 assistant 消息（"定点报时：..."，source=schedule）也正常落库，但前端不渲染。

根因：提交 5a43346（"任务: Chat交互支持自然语言-迭代"）为修复 task worker CoT 泄露（"The task requires querying weather..." 作为普通 assistant 气泡显示），在 `chat.js shouldRenderMessage` 增加过滤：`if (message.role === 'assistant' && PROCESS_SOURCES.has(message.source)) return false;`，PROCESS_SOURCES={task,schedule,curator}。该过滤把 schedule 的 assistant 消息一并隐藏，但 schedule 与 task/curator 语义不同：task 经 `ui.task_lifecycle`/`ui.task_result` 卡片对外（assistant 推理是内部 CoT，隐藏合理），curator 为内部维护，而 schedule 没有独立卡片机制--其 assistant 消息就是投递记录本身（`ScheduledAgentExecutor` 把 chat_service 的 assistant 输出作为 `result.output` 投递并落库）。根因跨 chat.js（前端过滤范围过宽）+ chat_service.py（`_PROCESS_MESSAGE_SOURCES` 标记 source=schedule）+ scheduled_agent_executor.py（assistant 输出即投递内容）。过滤按"来源类型"一刀切，未区分"有无独立对外卡片机制"。

教训：Dashboard Chat 隐藏进程来源 assistant 消息时，必须区分"worker 内部 CoT（有独立卡片对外，隐藏）"与"投递记录本身（无独立卡片，必须可见）"。task/curator 隐藏，schedule 可见（空内容由 hasVisibleContent 兜底隐藏中间步）。修改 shouldRenderMessage 的来源过滤范围时，必须同步检查 schedule 的投递可见性--schedule 的 assistant 消息是定时任务对用户/飞书的唯一输出凭证。修复=过滤条件加 `&& message.source !== 'schedule'`，并更新 chat_frontend_harness 断言 schedule 投递记录可见。相关：模式十六 task/schedule/curator 来源、模式三十三（无直接关联，同属进程消息处理）。

来源：fix 260726 Dashboard Chat 看不到定时任务投递记录（用户反馈）

### P025: 查询过滤条件必须有对应写入路径，字段不可跨语义混用

现象：对话右侧边栏制品面板对任务执行会话（如 task-{uuid5}）始终显示"暂无关联制品"，而任务实际产出了 TaskArtifact。同时 publish 流程把错误的 session_id 传给 InformationFlow。

根因：制品面板查询 `GET /chat/artifacts?source_kind=session&source_context_ref={会话ID}`，但任务制品注册时存的是 `source_kind=task_artifact` + `source_context_ref=任务ID`，AND 过滤两维全不匹配；且 `source_kind=session` 这个枚举值全仓无任何创建路径（手动创建硬编码 MANUAL，任务路径用 task_attachment/task_artifact），查询永远命中空集。深层根因是 `source_context_ref` 字段被跨语义混用：注册端写 task_id（provenance），消费端（面板查询、publish 的 InformationFlow.release session_id）却按会话ID理解。根因跨 artifact_service.py（注册写 task_id、publish 当 session_id 用）+ sqlite_artifact_registry.py（AND 过滤）+ chat.js（面板查询）+ artifact_routes.py（接口）+ task_session.py（会话派生）。

教训：新增查询过滤条件前必须确认存在对应的写入路径（grep 枚举值的创建点），无写入路径的过滤等于查空集且无报错，极易误判为"数据缺失"。一个字段不可同时承载 provenance（来源）和 display-key（展示关联）两种语义，consumers 会按各自需要解读导致错配；展示/会话维度应单独建字段（本例新增 source_session_id）与 provenance 字段分离。跨层（Application/Infrastructure/Interfaces）字段语义一致性必须由领域模型的 to_public_view 与端口签名共同守护。修复=注册时经 task_session_resolver 解析 task_execution_session_id 写入 source_session_id，面板改按 source_session_id 查询，存量数据 backfill_session_ids 回填，publish 改取 source_session_id。相关：模式二十二 write-through 注册与 source_session_id 会话关联、模式二十三面板查询。

来源：fix 260804 制品信息展示不对（会话 task-d1d97cd7、任务 e2e-art-...）

### P026: LLM 工具调用参数的空值表示非确定性，无值守任务需在执行器输入边界防御式归一化

现象：每日 10:00 的拍照上传定时任务（无值守 unattended）连续多日成功后，08-04 起两次运行（含一次手动重试）"成功返回"但内容是 `host_terminal` 返回 `host_arguments_invalid` 的道歉，未产出 signed_url，照片未投递。

根因：LLM 调用 `host_terminal` 时把空 argv 表达成 `args: ""`（空字符串）而非 `args: []`（空数组）。`host_terminal_arguments_allowed`（Domain，host_terminal_policy.py）用 `isinstance(args, (list, tuple))` 校验 argv 准入，空字符串不通过 -> `_validate_shape` 返回 `host_arguments_invalid`，Bridge 未被调用。DB tool_calls 证实：成功调用 `args: []`，08-04 失败调用 `args: ""`。根因跨 host_terminal_tool_executor.py（Application 校验）+ host_terminal_policy.py（Domain argv 准入）+ host_terminal_capability.py（Application photo 能力检测，`is_photo_capability_request` 用 `args == []` 严格匹配）+ tool_service.py（Application signed_url 保留，也读 `is_photo_capability_request`）。LLM 输出非确定性，无值守任务无人工干预容错，单次漂移即导致每日任务失败；08-03 及之前模型恰好输出 `[]`，08-04 起漂移为 `""`。

教训：LLM 工具调用参数的"空值表示"存在非确定性（空数组可能被表达成空字符串 `""` 或 `null`），无值守/定时任务对此尤为敏感（无人工干预容错，单次漂移即每日失败）。需在工具执行器 Application 输入边界对语义等价的空表示做防御式归一化（`""`/`null` -> `[]`）；归一化必须覆盖全部消费该字段的点（校验 `_validate_shape`、目标 `HostSkillScriptTarget` 构造、能力检测 `is_photo_capability_request` 在 executor 与 tool_service 两处、审计哈希），否则某消费点仍看到原值导致部分功能（如 photo 结果解析、signed_url 保留）静默失败。归一化仅限空表示，不削弱非空内容的类型/安全校验（非空字符串仍被 `host_terminal_arguments_allowed` 拒绝为 shell 字符串），Policy 白名单仍精确匹配位置参数（`_matches` 校验 `len(rule.positional_args) == len(target.args)`），不绕过授权。Domain 校验函数应保持严格类型契约（只接受 list/tuple），归一化属于 Application 反腐败层职责（把 LLM 的非规范输出翻译为 Domain 期望的规范类型）。相关：P004 入口 null 归一化（同类模式，不同入口）、模式二十四 Host Terminal 双重 Policy、P023 unattended 任务整体失败。

来源：fix 260804 定时任务拍照上传 sched-c6d0e455 返回 host_arguments_invalid（用户反馈）

### P027: 制品 kind/mime 分类必须回退到文件名扩展名，backfill 需覆盖历史数据

现象：会话 dashboard-53f61ffa-21b2-4cbc-b625-f348197bb8da 中 task-output-a.txt、task-output-b.md 制品被标记为 `其它`（OTHER）类型，前端 BINARY_KINDS 含 other 从而无法渲染；同时观感为"制品信息未正确关联"。

根因：task_complete 工具提交的制品 dict 只含 `{name, storage_ref, type}`，无 mime；main.py 注入 `artifact_normalizer=None`；task_run_service 的 `_normalize_artifacts` else 分支把 mime 置为空串；artifact_service.register_from_task_artifact 用 `_kind_from_mime("")` -> OTHER（mime 保持空）。前端 artifacts.js 的 BINARY_KINDS 含 `other`，故无法渲染。根因跨 task_run_service.py（Application 归一化）+ artifact_service.py（Application kind 推断）+ artifacts.js（前端渲染），3 文件 / 2 架构分层（Application + 前端）。"制品未正确关联"与"无法渲染"是同一根因（kind/mime 未从文件名捕获）的两个表现。

教训：制品 kind/mime 分类不能只依赖入参 mime（上游 task_complete 不带 mime、artifact_normalizer 可能未注入），必须回退到文件名扩展名推断（.md->MARKDOWN/.txt->TEXT/.html->HTML/.csv->CSV/.json->JSON 等）；分类错误会同时导致前端 BINARY_KINDS 渲染失败与"制品未关联"观感。新增分类逻辑后必须 backfill 历史数据（启动期 list_artifacts_with_empty_mime 重扫 + _resolve_mime 重推断），否则历史制品仍 OTHER 不可渲染；backfill 须 idempotent 且仅处理空 mime，避免误改已正确分类的制品。相关：P022/P023 backfill 模式、模式十六 write-through 注册、backfill_session_ids 同款 idempotent 游标重扫。

来源：fix 260808 dashboard-53f61ffa 制品 kind/mime 误分类导致无法渲染（用户反馈）

### P028: 单向注册回调必须有反向删除级联，孤儿清理的存活判断不能用 fallback resolver

现象：已删除任务（如 e2e-art-1785716569-85752-att2.txt 所属任务）的制品仍展示在制品工作台列表，期望不展示。

根因：Task 与 Artifact 是两个独立子系统（独立 DB）。注册方向是单向 write-through：TaskService 附件上传 / TaskRunService TaskArtifact 产出经 `artifact_register_callback` 回调 ArtifactService 注册为 Artifact（task -> artifact）。但 TaskService.delete_task 删除任务行（CASCADE 附件/事件）、附件文件、执行会话后，未反向级联清理 artifacts DB，导致 source_context_ref=task_id 的制品残留。叠加 artifacts DB 宿主挂载持久化跨 docker/restart.sh 重启，历史孤儿累积。根因跨 task_service.py（Application delete_task 缺反向回调）+ artifact_service.py（Application 无按 task 清理入口）+ main.py（wiring 未注入删除回调），3 文件 / 2 子系统（Task + Artifact）。

教训：单向注册回调（write-through 注册）必须配对反向删除级联，否则被注册方在源删除后成为孤儿；删除入口 delete_task 须注入 `artifact_delete_callback` best-effort 回调 delete_artifacts_by_source_task（失败 try/except+warning 不阻断主流程，与注册同款 best-effort 旁路语义）。孤儿 backfill 判活不能用 `task_session_resolver`：它对已删除 task 回落确定性 fallback session（非 None），无法区分"不存在"与"已删除"，会误判存活而漏删；须另注入 `task_exists`（task_id -> plain bool）回调。孤儿清理须 fail-safe：task_exists 抛异常时 failed++ 跳过不删（不确定时宁留勿误删）。DB 持久化跨重启的场景，修复须同时覆盖未来（级联）与历史（启动 backfill）两条路径。相关：P027 backfill 模式、模式二十二 write-through 注册 + delete 级联。

来源：fix 260808 已删除任务制品残留制品列表（用户反馈 e2e-art-1785716569-85752-att2.txt）

### P029: 跨层文件名校验必须共享同一规则，ASCII-only allowlist 会拒绝上传层接受的 Unicode 文件名

现象：任务 t_d214a4e52c64494e 的中文附件 `横向-邮箱归属.md`，任务详情页无"制品"链接、附件也不展示在制品工作台列表，疑似不支持中文文件名。

根因：附件上传与制品注册是两个层。TaskService 上传层（Application, task_service.py）用 denylist `_FILENAME_SAFE_RE = ^[^\x00-\x1f/\\<>:"|?*\x7f]+$` 校验文件名，允许 Unicode（中文），stored_name=`{uuid16}_{原文件名}` 保留原文，上传成功、文件落盘。但 register_from_attachment 构造 content_ref=`attachment:{task_id}/{stored_name}` 后调 LocalArtifactContentStore.read（Infrastructure, content_store.py），_parse_ref -> _validate_filename 用 `_FILENAME_RE = ^[A-Za-z0-9._-]+$`（ASCII-only allowlist）校验 stored_name，中文被拒绝抛 ArtifactValidationError，read 在碰到文件系统前就失败、返回 None，制品静默不注册。任务详情页 tasks.js `if (attachment.artifact_id)` 才渲染"制品"链接，注册失败则 artifact_id 缺失、无链接；制品列表也因无 artifact 行而不展示。根因跨 content_store.py（Infrastructure 校验）+ task_service.py（Application 上传校验），2 文件 / 2 架构分层，两层校验规则不一致（一个 denylist 允许 Unicode、一个 allowlist 仅 ASCII）。

教训：跨层文件名/路径校验必须共享同一规则（同 denylist 或同 allowlist），否则上游接受的值下游拒绝、中间静默失败、现象指向"不支持中文"实则校验不一致。LocalArtifactContentStore 的 _FILENAME_RE 应与 TaskService _FILENAME_SAFE_RE 对齐为 denylist（拒绝控制字符/路径分隔符/Windows 保留符，允许 Unicode 字母数字），路径穿越防御由 denylist + exact "."/".." 拒绝 + 嵌套路径 split 检查 + per-component lstat symlink 拒绝共同保证，allowlist 不是穿越防御的必要手段。"无制品链接 + 不展示"是同一根因（注册失败）的两个表现，不要当两个问题查。相关：P027 制品注册失败现象、模式二十二 content_ref 不透明方案 + 文件名校验对齐。

来源：fix 260808 中文附件 横向-邮箱归属.md 未注册为制品（用户反馈任务详情页无制品链接/制品列表不展示）

### P030: Content-Disposition legacy filename 必须 ASCII-only，非 ASCII 文件名用 RFC 5987 filename* 传递

现象：中文附件 `横向-邮箱归属.md` 制品已注册成功（Task 7 修复），制品工作台预览失败，前端提示"加载失败：request_failed"。

根因：artifact 内容/导出响应由 `_safe_content_disposition`（artifact_routes.py）构造 Content-Disposition header，其 legacy `filename="..."` 字段直接放入 sanitized 原文件名，sanitization 只清理 `/\"` 换行等、不处理非 ASCII，中文 `横向-邮箱归属.md` 原样进入 `filename="..."`。Starlette Response.init_headers 对 header value 做 `v.encode("latin-1")`，中文超出 latin-1 范围抛 `UnicodeEncodeError: 'latin-1' codec can't encode characters in position 18-19`，该 `_build_content_response` 调用在路由 try/except 之外、异常未捕获、propagate 到 ASGI -> HTTP 500 裸响应（无 error.code）-> 前端 fallback "request_failed"。同根因在 3 个 HTTP 路由文件重复：artifact_routes._safe_content_disposition（已修复）、task_routes.py 附件下载 inline（`filename="{stored_name}"` stored_name 含中文，同 bug）、published_artifact_routes._sanitize_filename（同 bug 且缺 filename* RFC 5987 fallback）。根因跨 3 文件 / 1 架构分层（HTTP Interface），三份重复的不完整 sanitization 是 bug 温床；修复方式：抽取共享 helper `app/interfaces/http/_content_disposition.py::build_content_disposition`，三个路由统一调用，从结构上消除漂移。

教训：HTTP header value 是 latin-1，Content-Disposition 的 legacy `filename` 参数必须 ASCII-only；非 ASCII 文件名用 RFC 5987 `filename*=UTF-8''<percent-encoded>` 传递真实名，legacy `filename` 回退为 ASCII 占位名（保留扩展名如 `artifact.md` 让旧客户端保留文件类型）。`filename*` 用 `quote(name, safe="")` 全 percent-encode（含 `/` `\`，防 header 注入）。新增 Content-Disposition 构造必须复用共享 helper `build_content_disposition`，禁止再各自实现（已收敛 artifact_routes/task_routes/published_artifact_routes 三处）。`_build_content_response` 之类 header 构造若在路由 try/except 之外，编码异常会变 500 裸响应无 error.code，前端只能 fallback 通用错误，定位需查服务端 traceback。相关：P029 中文文件名（content_ref 校验层）、模式二十二 Content-Disposition ASCII 安全 + 陷阱。

来源：fix 260808 中文制品 横向-邮箱归属.md 预览失败 request_failed（用户反馈）；同根因 task_routes 附件下载 / published_artifact_routes 已通过共享 helper `build_content_disposition` 一并修复

### P031: write-through 投影删除须反向级联删 source of truth 先删，否则启动 backfill 复活投影；级联失败须传播非 best-effort

现象：从制品工作台删除一个 task_attachment 制品后，任务详情页的附件仍存在（未同步删除），用户反馈"这种问题太Low了"。

根因：Task 与 Artifact 双向级联只实现了一半。P028 补齐了 task 删除 -> 制品清理（`artifact_delete_callback` -> delete_artifacts_by_source_task），但制品删除 -> 任务附件清理这一对偶方向缺失：ArtifactService.delete_artifact 注释明确"Does NOT delete source attachment/workspace files"，对 task_attachment 制品只删 artifacts DB 元数据，不删底层 TaskAttachment（tasks DB 记录 + 附件文件）。制品的 content_ref 是 `attachment:` 源引用（非 `item:` owned），_is_owned_ref 为 False，best-effort delete_owned 也跳过，故附件文件/记录原样残留，任务详情页仍展示。更严重：启动期 backfill_attachments 按 task_attachments 表游标重扫幂等注册为 Artifact，残留附件会在下次 docker/restart.sh 重启后把已删制品"复活"回制品列表。根因跨 artifact_service.py（delete_artifact 无反向回调）+ task_service.py（delete_attachment 已存在但未被制品删除调用）+ main.py（wiring 未注入 task_attachment_delete 回调），3 文件 / 2 子系统（Artifact + Task），与 P028 同一 write-through 体系的另一半。

教训：write-through 注册（source -> projection）+ 启动期 backfill（按 source 重扫重建 projection）的组合下，删除 projection 必须反向级联删 source，且 source of truth 须先于 projection metadata 删除——否则 source 残留 + backfill 复活 projection，删除"重启后失效"。这与 P028 方向相反但同体系：P028 是 source(task) 删 -> projection(artifact) 清理（projection 次要，best-effort）；本条是 projection(artifact) 删 -> source(task_attachment) 清理（source 是真相之源，须先删）。级联失败处理两方向不同：source 删失败必须传播异常、不删 projection metadata（best-effort + 继续删 projection 会让 source 残留 + backfill 复活，正是本 bug）；回调返回 False（source 已删，如 P028 级联已先 CASCADE 附件行）时继续删 projection 清理 stale 投影。仅 task_attachment 触发（manual/task_artifact/workspace 不触发，task_artifact 是 worker 产出不归用户管理）。回调注入沿用既有 late-bind 模式（`set_task_attachment_delete_callback`，对齐 `set_task_exists_callback`），ArtifactService 不直接依赖 TaskService（DDD 分层，回调解耦）。相关：P028 单向注册反向级联（task->制品方向）、模式二十二规则 8 制品删除反向级联、规则 6 backfill 复活风险。

来源：fix 260809 制品页删除附件后任务附件未同步删除（用户反馈"太Low了"）

### P032: 前端渲染 bug 必须浏览器实测 getComputedStyle，全局元素规则会经继承卡住特定 class；禁止仅推理 flex 链就下根因结论

现象：制品预览 py/json 最大高度未达预览面板底部，且与 markdown/html/pdf 异构（"这还能异构?!"）。第一次修复把 `.artifacts-shell` 由 `min-height` 改 `height: calc(100vh - 90px)`，推理"flex 高度链内容驱动致 pre 塌缩"，未在浏览器实测就回填知识、报完成。用户复测发现未修复。

根因（真）：styles.css 有一行通用元素规则 `pre { max-height: 320px; ... }`（line 164），作用于所有 `<pre>`。`.artifacts-preview__pre`（line 960）设了 `flex:1; min-height:0` 却未覆盖 `max-height`，故 pre 被全局规则死卡在 320px--无论 shell 用 min-height 还是 height、无论 flex:1 怎么长都超不过 320px。iframe 类（markdown/html/pdf 用 `<iframe>` 不是 `<pre>`，无此上限）能填满，pre 类被卡，遂异构。实测 `getComputedStyle(pre).maxHeight == '320px'`、pre_rect_h=320 而 wrap_rect_h=698。修法仅一行：`.artifacts-preview__pre` 加 `max-height: none` 覆盖全局上限，pre 即 674px 填满。

根因（错，第一次）：误判为 shell min-height/height 致 flex 链内容驱动。实测证伪：shell 改 height 后 pre 仍 320px。

教训：前端渲染/CSS bug 必须用浏览器 `getComputedStyle`/`getBoundingClientRect` 实测计算值再下根因结论，禁止仅推理 flex/盒模型链就修复并回填知识--CSS 特异性与全局元素规则（`pre {}`、`a {}`、`img {}`）会经继承作用于特定 class（当 class 未显式覆盖该属性时），这种"隐性上限"无法靠读 class 规则发现，只能靠 computed style 暴露。项目有浏览器容器（n-agent-browser，playwright + /usr/bin/chromium，可 `docker exec` 跑脚本访问 http://n-agent:8201），渲染 bug 一律走实测。回填知识前必须实测验证修复生效，避免把错误根因写进 knowledge 误导后续。相关：模式二十二规则 9 preview pre max-height 覆盖。

来源：fix 260809 制品预览 py/json 高度不达标、PDF 无法预览（第一次修复错误，用户复测指出未修复）

### P033: LLM 驱动的写工具 E2E 经 /v1/chat/completions 不可行（无 approval_decider，CONFIRM/DANGEROUS 被拒），写工具链路改确定性 HTTP E2E + 工具层单测

现象：计划要求写"五条普通 Chat 自然语言链路 E2E"驱动 artifact_create/update/rollback/publish，经 `/v1/chat/completions` 发自然语言让 LLM 调写工具。实际 LLM 调用写工具后被拒，制品未创建，链路无法成立。

根因：`/v1/chat/completions`（openai_compatible.py）调 `chat_service.complete` 时不注入 `approval_decider`（仅 Dashboard 对话路由经 `dashboard_tool_approval_bridge.create_decider` 注入阻塞式 decider）。agent_graph `_request_tool_approval` 在 decider 为 None 时直接返回 `approval_required`（permission_denied），不阻塞等待--CONFIRM（artifact_create/update/rollback）与 DANGEROUS（artifact_publish）写工具一律被拒。要经 Dashboard 阻塞式审批异步握手（发 NL -> 工具 pending -> POST 审批 -> 继续）才能跑通，但该握手对 E2E 过于脆弱且项目无既有模式。

教训：写工具（CONFIRM/DANGEROUS）的端到端验收不要经 `/v1/chat/completions` 的 LLM 链路（无 decider 必被拒）；改由确定性 HTTP E2E（artifacts.sh 直击 /chat/artifacts* API，含 CAS/Revision/publish 语义）覆盖端点契约 + 工具层单测（test_artifact_tool_executor.py 会话隔离/溯源不可伪造）+ agent_graph 单测（ui.artifact 卡片持久化）覆盖工具层专属行为。LLM 工具选择/guidance 装配由单测（test_main_artifact_wiring/test_prompt_builder）守护。涉及审批门控的写工具 E2E 须先确认调用路由是否注入 approval_decider。

来源：迭代 260809 artifact revision/export，T12 S2 偏离原"自然语言 LLM 链路"改确定性 HTTP 链路

### P034: task_complete 的 workspace: storage_ref 解析到 workspace_root 而非沙箱 cwd，worker 用 open() 写 scratch 会导致制品静默 drop + 任务假成功

现象：Task worker（如 t_d0cb902535d94089）用 execute_code 的 `open()` 在沙箱 cwd（`/scratch/sess-.../call-<uuid>/`）写文件，task_complete 以 `workspace:task3-output.md` 作为 artifacts 的 storage_ref 提交；task_complete 返回 success、任务标记 succeeded 并在 result 声称"submitted it as an artifact"，但 artifacts 表与 task_attachments 表均无该制品记录。

根因：`workspace:{path}` ref 经 LocalArtifactContentStore 解析到 `settings.workspace_root`（容器内 `/workspace`，与 content_store 同根），与 execute_code 沙箱 cwd（ephemeral scratch，每调用独立 `call-<uuid>` 子目录）不同根。worker 用 `open()` 写到 cwd 的文件不在 workspace_root，register_from_task_artifact 的 `content_store.read` 抛 ArtifactContentUnavailableError，回调 catch 后 return None（best-effort，不影响 Task finish 的设计），制品被静默 skip。execute_code 工具描述说"write only to cwd (scratch)"且把 write_file 回调示例只给 web_extract/web_search，prompt 与 task_complete schema 都只说"put a workspace: file ref"，三者都没指出 workspace: ref 必须先用 write_file（回父进程 RPC 写 workspace_root）写入同路径。

教训：opaque 引用方案（如 `workspace:{path}`）的解析根必须与写入路径的可达根一致，且该一致性须在工具描述/prompt 里显式说明并用前置校验守护，不能依赖 worker 推理。best-effort 旁路（register 回调失败不阻塞主流程）对"制品是任务主产出"的场景会产生假成功，致命副作用应改为前置确定性校验（task_complete 提交时 probe workspace ref 可读性，不可读抛 TaskValidationError 让 worker 自纠正用 write_file 或回落 inline content），而非 finalize 后静默 drop。诊断"任务成功但产出缺失"类 bug 时，优先比对工具调用记录里文件实际写入路径（execute_code 结果的 CWD/File path）与 storage_ref 解析根是否同根。修复涉及跨层（Application TaskService.complete -> Infrastructure content_store.probe -> Domain ArtifactContentStore 端口新增 probe）。

来源：bug fix 260810 t_d0cb902535d94089 task_complete workspace: ref 制品未创建

### P035: goal_mode judge 通过 task_show 看到 run 未 finalize 状态会循环否决（批准才 finalize vs 未 finalize 就否决），表现为 goal task 即建即败/无法创建

现象：goal_mode task worker 正确调 task_complete（complete_requested 事件已记录、workspace: 制品 probe 校验通过），但 task 仍 failed。事件序列：complete_requested -> goal_judge_feedback(achieved=false) -> 连续否决 -> finished(failed)；retry 时 worker 识别"engine finalization issue"后 task_fail abort。用户感知为"命令/自然语言都无法创建 task"（任务即建即败）。

根因：goal_mode 的 finalize 由 judge 批准触发（_execute_task -> run_goal_loop -> judge.achieved -> return COMPLETED -> _finalize_run），所以 judge 运行时 run 必然还是 running。但 judge prompt（prompt_builder TASK_GUIDANCE ### Goal Mode Judge）说"if the task is still in progress ... achieved=false"，judge 经 task_show（get_task_detail）读到 run 未 finalize 的状态——task.status="running"、runs[].status="running"/outcome=null/ended_at=null、worker_context 的 ## Identity 段 "status: running"、events 无 finished 事件——套用该指令否决，形成循环依赖。触发因素：前序 fix-bug 给 TASK_GUIDANCE item 5 加"task_complete validates... rejects the call if not"（worker/judge 共享可见），使 judge 更谨慎核查 run 是否真的 finalize，暴露了这个潜在的循环依赖。仅改 prompt 指令无效——LLM 仍基于具体 status 字段否决。

教训：评估器（judge）不得看到被评估对象的待决状态（run 未 finalize），否则形成"批准才 finalize vs 未 finalize 就否决"的死循环。必须从数据层面 redact：judge fork（write_origin=="judge"）的 task_show 返回中移除 task 的 run 生命周期字段（status/current_run_id/claim/heartbeat/failure 等）、runs 数组置空、worker_context 置 null，让 judge 仅基于 complete_requested intent + 可验证结果判定目标达成。prompt 指令对 LLM 不可靠，只能作为辅助，不能替代数据层 redact。诊断"goal task 即建即败"类 bug 时，看 task_events 是否有 goal_judge_feedback 否决且理由含"run still running/not finalized"。修复跨层：Infrastructure TaskManagementToolExecutor._handle_show redact -> Application task_service.get_task_detail 返回字段 -> judge LLM。

来源：bug fix 260811 goal_mode judge 循环否决导致 goal task 即建即败

### P036: tool input_schema 未展开 items 子字段导致 LLM retry-guess（artifact_update text_patch 连续构造错误），表现为制品修改过程消息爆炸

现象：task 制品在任务结束后通过 dashboard 自然语言修改成功，但修改过程暴露大量过程消息：3 次 artifact_update retry（错误 "text_patch op must have explicit mode" / "mode must be 'first' or 'all'"）+ 2 次 artifact_read（修改前预读 + 修改后验证读）+ 8 条冗长逐步 assistant 消息。用户感知为"修改过程中暴露了太多的过程消息"。

根因：artifact_update 的 text_patch input_schema 只声明 `{"type":"array","items":{"type":"object"},"description":"1..100 search/replace operations"}`，未展开 items 的 search/replace/mode 字段定义。LLM 不知道 op 结构，靠 description 猜测，连续 3 次构造错误 op（缺 mode / mode 值错 / 多余字段），被运行时校验 _validate_text_patch 拒绝后 retry。运行时校验（infrastructure artifact_management.py）正确但无法引导 LLM 一次构造正确——schema 是 LLM 的契约，必须独立完整。叠加 prompt TASK_GUIDANCE 未指示"优先 content""跳过预读""不验证读""简洁报告"，LLM 还做了不必要的 artifact_read（preliminary + verify）和冗长逐步汇报。

教训：给 LLM 的 tool input_schema 必须完整展开复合字段的子结构（array items 的 properties/required/enum/minLength 等），不能只给空 `{"type":"object"}` 让运行时校验兜底；schema 是 LLM 构造参数的契约，运行时校验只防穿透不负责引导。诊断"LLM 反复 retry 同一工具"类问题时，先查该工具 input_schema 是否对复合字段（array of object）展开了子字段定义。修复跨层：Application artifact_tools.py（schema 展开 search/replace/mode + required + additionalProperties:false + minItems/maxItems）+ Application prompt_builder.py TASK_GUIDANCE（优先 content、跳过预读/验证读、简洁报告）+ Infrastructure artifact_management.py _validate_text_patch（运行时校验，已存在，不替代 schema）。

来源：bug fix 260811 task 制品修改过程消息过多（schema 未展开致 text_patch retry + 多余 artifact_read）

### P037: 同资源的变更接口必须返回与读接口一致的视图，前端直接赋值响应会因精简形状丢字段

现象：编辑 markdown 制品后预览不自动更新（需手动刷新浏览器才更新），版本列表也有类似不刷新问题。

根因：content PATCH（JSON + multipart）返回 _write_result_to_dict（精简：artifact_id/revision_id/revision_number/name/kind/size/checksum/publish_sync_state，缺 id/current_revision_id/mime/updated_at），而 GET detail 与 metadata-only PATCH 返回 _artifact_view（完整视图）。前端 saveText 把 PATCH 响应直接赋给 state.detail，导致 detail.id=undefined -> renderMarkdownHtml 调 fetchExport(detail.id=undefined) -> /chat/artifacts/undefined/export 404 -> 预览不可用；同时 saveText 未调 loadRevisions，版本列表不刷新。手动刷新浏览器触发 loadDetail(id) 用真实 id 重新 GET 完整视图，预览恢复，故表现为"手动刷新才更新"。

教训：同一资源的变更（mutation）接口必须返回与读（read）接口一致的视图形状，因为前端常把变更响应直接赋给本地 state（state.detail = updated）。若变更返回精简形状缺了前端依赖的字段（如 id），下游功能（预览 fetchExport(detail.id)、版本 modal 标记当前版本、下次保存 CAS token）会静默失效。诊断"编辑后需手动刷新才更新"类前端问题时，先查变更接口响应形状是否与读接口一致、前端是否直接赋值。修复跨层：Interfaces artifact_routes.py（content PATCH JSON + multipart 改返回 _artifact_view，与 GET 一致）+ frontend artifacts.js（saveText 赋值完整视图 + 失效并重载 revisions；openRevisionsModal 打开时重拉）。

来源：bug fix 260811 制品编辑后预览/版本不自动刷新（content PATCH 响应缺 id 致 fetchExport 404 + 未重载 revisions）

### P038: 隔离子 Agent 的 ingress 身份必须绑定子会话，模型占位符也必须走统一解析

现象：实时父 Agent 成功调用 `delegate_agents`，但两个子成员都在几十毫秒内同时失败，结果仅显示 `delegation_child_execution_error`，没有发生真实 LLM 推理。

根因：ChildAgentExecutor 的请求使用 `delegation-<uuid>` 隔离会话，却把父会话 ID 写入 `IngressFacts.session_id`，被 ChatCompletionService 的策略快照入口以 `policy_context_invalid: ingress session mismatch` 拒绝。修复该错位后又发现 DelegationRunService 把子模型硬编码为 `default`；该字符串不是统一模型解析支持的占位符，会原样发给 Provider 并被拒绝。ChildAgentExecutor 还会把 `finish_reason=error` 的错误文本误判为成功摘要。

教训：派生执行上下文必须区分“本次执行身份”和“父级来源”：request.session_id 与 IngressFacts.session_id 必须都绑定隔离子会话，父会话只能放入 trusted claims 作为谱系信息。跨子系统调用 LLM 时必须使用项目统一占位符（当前为 `N-Agent`）或显式解析后的活动模型，禁止自行发明 `default`。捕获异常的安全边界应保留可观测日志或测试复放入口，同时必须按 finish_reason 判断结果，不能仅以非空 content 判成功。

来源：bug fix 260822 dashboard-d250c0b0 子 Agent 委派失败

### P039: 不持久化消息不等于不创建会话，内部会话必须在创建时显式携带业务标题

现象：委派子 Agent 会生成 `delegation-<uuid>` 隔离会话，但会话列表中的标题长期停留为 `New Session`，与普通会话和委派成员的任务语义不一致。

根因：ChildAgentExecutor 为避免把内部控制消息写入会话记录而设置 `persist_messages=False`；ChatCompletionService 仍必须为策略快照创建会话，但自动标题生成被消息持久化开关一并跳过。结果是“会话已创建、消息不落库、标题也无人更新”。

教训：会话存在性、消息持久化和标题来源是三个独立维度，不能用一个 `persist_messages` 开关隐式控制全部行为。服务端已知业务标题的内部执行（如委派成员）应通过类型化请求字段在创建会话时显式传入标题；复用历史默认标题时可以修复为业务标题，但必须保留用户已重命名或其他非默认标题。修复需同时覆盖调用方传递、会话服务创建/兼容修复，以及 `persist_messages=False` 的回归测试。

来源：bug fix 260822 delegation-cf976103 子 Agent 隔离会话未正确命名

### P040: capability 工具集不是工具暴露列表，签发时必须剥离 FORBIDDEN 工具

现象：task worker 调用 `delegate_agents` 稳定返回 `delegation_invalid: delegation policy denied the request`，同参数 realtime 源却成功；静态重建 Policy 请求评估结果为 ALLOW，无法从代码推断拒绝原因。

根因：TaskDelegationAdapter.grant_delegate_tool 把 `delegate_agents` 加入 worker 的 granted_tools（工具暴露列表，父要用它发起委派），TaskAgentExecutor 又把同一列表原样作为 parent_allowed_tools 传入 sign_task_capability；DelegationPolicy 检查 3(a) 禁止 parent_allowed_tools 与 FORBIDDEN_CHILD_TOOLS 相交（delegate_agents 在列），task 源委派自授权起即必然 DENY。realtime 源因 Dashboard 不设置 granted_tools（空集）而未触发。

教训：同一份工具名列表在不同语义下含义不同：granted_tools 是"本 Agent 可调用的工具"，capability 的 parent_allowed_tools 是"可授予子 Agent 的工具"，两者集合关系是子集收紧而非等价。capability 签发点是做剥离的正确位置（对 realtime/task 两个 adapter 统一生效），修复需配"含 FORBIDDEN 工具输入 -> 签发结果不含"的回归测试。当静态分析与运行时行为矛盾且所有可重建输入都评估为 ALLOW 时，应在拒绝点加一次性诊断日志（capability 摘要+逐项子检查结果）拿运行时证据，而不是继续枚举假设。

来源：bug fix 260823 dashboard-bf805e6d task 源委派被 policy 拒绝

### P041: 文本形态的 JSON 委派结果也必须在持久化前按字段脱敏

现象：子 Agent 返回 `{"secret":"…","credential":"…"}` 后，Dashboard 父会话展示了原始凭证值。

根因：InformationFlow 的精确值脱敏只知道运行配置中登记的 secret（通常仅 provider API key）；委派结果作为纯文本摘要写入数据库，未解析 JSON 并复用结构化字段脱敏，`credential` 也不在默认字段列表中。

教训：对模型返回的 JSON 文本，不能把它当作不透明字符串。应在第一个可持久化边界解析 object/list 并应用结构化凭证字段脱敏；在结果释放给父 Agent 时再作一次防御性过滤。测试必须断言原值既不在持久化摘要中，也不在父侧投影中。

来源：bug fix 260823 dashboard-1e0276ec-5180-461f-93dd-dc60789e3d72 委派结果敏感字段未脱敏

### P042: "自适应宽度"需求下 min(100%, Npx) 的常量上限在宽视口等价于固定宽度，必须用严格递增函数

现象：`.message-stack` / `.chat-composer` 用 `width: min(100%, 820px)` 实现"自适应"后，1440px 及以上视口的所有侧栏状态实测宽度全部为 820.00px，收起侧栏只让两侧留白变宽、内容宽度完全不动，被判为未达需求并触发同一 Task 内第二次迭代。

根因：`min(100%, Npx)` 是分段函数，只有在父内容盒 < N 时才随可用宽度变化；一旦可用宽度普遍超过 N，函数进入常量段。而侧栏开合恰好只在可用宽度较大的区间内切换（本项目每个侧栏列 280px + 16px gap = 296px），因此常量段覆盖了全部目标状态。写规范时把"有上限"当成了"自适应"，把窄屏兜底当成了主区间行为。

教训：需求说"随可用宽度自适应"时，先把它形式化为"在操作区间内宽度必须是可用宽度的严格递增函数"，再选函数形式，不要先选 CSS 写法。仿射衰减 `min(100%, calc(A + k*100%), Cap)`（本项目 A=240px, k=0.5, Cap=1360px）在主区间保持斜率 k 的严格递增、占比平滑衰减，只在超宽屏进入饱和。验收必须按视口 x 侧栏状态的笛卡尔积断言相邻档差值下界（本项目 >= 100px），单纯断言"等于目标值"无法暴露常量段。期望值要由运行时实测的父内容盒宽度推导，不写死像素基线——`body.sidebar-expanded` 会改变 `.main-content` 宽度，写死基线在另一侧栏状态下不可复现。

来源：feature 260824 chat-adaptive-width 第二次迭代，需求追加"或固定宽度"后 820px 方案被否决

### P043: 前端静态资源烘焙在镜像内，改完 styles.css/js 必须 docker/restart.sh 重建才能浏览器实测

现象：修改 `app/interfaces/http/static/styles.css` 后直接用 Playwright 打开 `http://localhost:8201/chat/` 实测，几何值与修改前完全一致；`curl http://localhost:8201/static/styles.css` 返回 200 但内容仍是旧声明。

根因：`docker/Dockerfile:21` 是 `COPY app ./app`，`docker/docker-compose.yml` 未对 `app/` 做 volume 挂载，因此容器提供的静态资源是构建期烘焙的副本，与宿主工作区文件无关。宿主编辑器改动不会反映到已运行容器，且服务健康检查、页面路由、HTTP 状态码全部正常，故障表现为"CSS 没生效"而不是任何报错。

教训：任何前端文件改动后的浏览器实测，前置步骤是 `sh docker/restart.sh` 重建镜像，而不只是确认 `/health` 可用。实测脚本应先用 `curl -fsS http://localhost:8201/static/<file> | grep <新声明片段>` 断言容器提供的资源已包含本次改动，再读取几何值；否则 RED/GREEN 结果都在测旧代码，GREEN 会假失败、RED 会假通过。

来源：feature 260824 chat-adaptive-width Phase 4 S6 GREEN 检查前发现容器提供旧 CSS

### P044: 前端数量/枚举校验必须用单一来源常量 + 严格顶层 envelope，禁止魔法数字 + 弱顶层 shape 检查

现象：Dashboard `/security` 页面加载失败，一直显示"策略加载失败"+"重试"。后端 `_POLICY_METADATA` 新增了第 11 个 Policy（`delegation`），`/chat/policies` 实际返回 11 项 Policy，但前端 `security.js` validator 同时有 `policies.length !== 10`（魔法数字）和 `EXPECTED_KEYS` 数组（10 项，缺 `delegation`），任一校验失败都抛 `policy_load_failed`。Node 行为夹具 `security_frontend_harness.js` 的 `validPayload()` 也只构造 10 项 Policy（与硬编码常量同步），所以 6 项前端测试全绿，浏览器实页全红。

根因：跨 3 文件 / 2 层（interfaces 静态 JS + 测试）。(1) 数量校验用魔法数字 10，没从唯一来源常量 `EXPECTED_KEYS.length` 派生；后端新增第 N 项 Policy 时，validator 与夹具必须同步手改 3 处常量（validator `10`、夹具 `meta.length`、夹具断言 `=== 10`），任何一处漏改都让前端彻底白屏。(2) 顶层 envelope 未做 `sameKeys(payload, ['profile_version', 'policies'])` 严格校验，只校验子字段是否存在，导致后端万一多加一个顶层字段（如 `extra`）前端也放行。(3) Node 行为夹具是"自参照"的——夹具的 `validPayload` 与被测 validator 共享同一个魔法数字，测试只验证"夹具 == validator"，不验证"夹具 == 真实后端 shape"，夹具漂移被掩盖。根因跨 `policy_dashboard_service.py`（后端新增 11 项）/`security.js`（validator 10）/`security_frontend_harness.js`（夹具 10）/`test_security_frontend.py`（静态合同仅检查前 5 个 key）。

教训：(1) 数量/枚举类校验必须用单一来源常量派生——`if (policies.length !== EXPECTED_KEYS.length) throw`，不写 `!== 10` 这类魔法数字；新增/删除 Policy 时只改 `EXPECTED_KEYS` 一处，数量校验自动跟随。(2) 顶层 envelope 必须用 `sameKeys(payload, [...expected])` 严格校验字段集合，不只校验"子字段是否各自有效"；这一调用必须位于读取任一 payload 字段之前。(3) 行为夹具的 `validPayload` 应从后端真实 `_POLICY_METADATA` 等权威元数据派生（如直接 import 或在编译期生成），不手维护与被测代码同源的常量数组；若必须手维护，须补一条 Python 静态合同用正则锁住源文件里的 `EXPECTED_KEYS` 完整顺序、`policies.length !== EXPECTED_KEYS.length` 派生形式、`sameKeys(payload, ['profile_version', 'policies'])` 调用存在——把"夹具"和"源"分离的护栏建在源端（regex 直接读源），避免夹具漂移时测试仍绿。(4) Dashboard 前端任一 JS validator 增改后，必须先 RED（夹具 + 静态合同双红）再 GREEN，并经 Playwright 真浏览器实测确认页面渲染数量符合契约（参考 P015/P032/P043）。相关：P015 契约漂移（JS 消费错 shape）、P032 浏览器实测根因、P043 静态资源烘焙重建。

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


<!-- SUMMARY: N-Agent 的迭代需求池，记录 Agent 初版需求、Chat 修复、System Prompt、模型名隐藏和 /chat 交互优化需求 -->
# 产品需求 - 迭代演进

## 约束
本文仅供自然人使用，未经人工确认、禁止AI阅读或修改。

## 需求列表
无论何时，你都必须遵循DDD分层规范。

[20260611]
- FR
    - Agent：参考Hermes，写一个智能体Agent，本次先做一个MVP、之后会逐步扩展。明确要求使用流行的代码框架
        - 框架：Python语言，DDD规范，DockerCompose部署，FastAPI、LangGraph，SQLite
        - 功能：Agent Loop，LLM Adapter，Tool Registry，Memory/Context，Chat Dashboard
        - 接口：支持OpenAI-Compatible协议的HttpAPI，兼容Open-WebUI，流式优先、支持工具调用；支持OpenAI tool-calling协议
        - 模型：LLM Adapter，支持多Provider
        - 工具：支持可插拔工具框架，留好权限和风险控制的接口
        - 记忆：持久化会话记录，支持长任务上下文续跑
    - Chat：Issue，点击Send按钮，没有任何反应
    - LLM：System，参考Hermes、设置system提示词，特别的：你的名字叫 N-Agent(Niean's Agent MVP)
    - LLM：Model，对外的OpenAI-Compatible接口不暴露底层真实模型名，所有模型统一展示为N-Agent
    - Chat：参考Open-WebUI的对话功能，完善下`/chat`页面和交互

[20260612]
- NFR
    - 源码：整理DDD文档
    - 治理：部署相关的文件，移动到目录 docker/

[20260614]
- FR
    - 知识：[KF]新增知识检索功能，接入方式http api，能被Chat、任务等使用。本次新增1个知识站点`N-KB`，定位`通用知识`，接口定义参见代码 /Users/niean/code/github.com/niean/n-kb/app/interfaces/http
        - 审阅：Spec spec-260614-kb-tool.md，发现其中的严重问题
        - 功测：错误，提示 knowledge search failed
    - 前端：重构前端页面，左导按照领域功能分菜单。前端规范，参见 .harness/framework/guides/10-guidelines-fe.md；真实样例，参考 /Users/niean/code/github.com/niean/n-kb/app/interfaces/http/static
    - 模型：页面需要展示真实Provider(管理员视角)，而当前展示N-Agent为脱敏后的信息(系普通用户视角)，需要提供两套接口
    - 模型：Dash支持编辑模型Provider
    - 会话：Dash支持编辑会话Title和删除
    - 会话：调试信息，默认收起到右侧，支持点击展开，留下的空间给到对话区

[20260615]
- NFR
    - 源码：LangGraph实现的AgentLoop
- FR
    - 模型：Active状态使用绿色对号图标
    - 模型：新增、修改模型，改为弹出框页面
    - 工具：[KF]完善Tool抽象，除运行时外的其它能力
    - 工具：工具列表，Schema查看改为弹出框
    - 交互：[KF]支持飞书Bot入口。除了Dashboard还应该支持CLI等方式，Gateway。参考HermesAgent，设计并实现
        - 迭代：飞书接入，使用长连接，会话列表也应包含飞书Session
    - 工具：[KF]新增MCP站点管理，前端系管理页面，后端按照DDD分层实现
        - 迭代：管理页面，MCP站点管理独立为左导二级菜单
    - 工具：MCP支持stdio类型
    - 对话：对话框中，调试信息默认收起不展开，可点击展开
    - 对话：调试信息展开时，会话会变宽，期望的效果是会话宽度不变

[20260616]
- Issue
    - 交互：飞书，收不到飞书IM发来的消息了，新建session能成功
- FR
    - 任务：[KF]新增任务功能。要求：①严格遵循DDD分层架构，②优先使用流行代码库，③借鉴Hermes-Agent的实现/Users/niean/code/github.com/niean/hermes-agent
        - 审阅：Spec spec-260615-scheduled-tasks.md，发现其中的严重问题
        - 审阅：Plan plan-260615-scheduled-tasks.md，发现其中的严重问题
        - 迭代：Dashboard，任务页面无法加载页面，概览页面有没任务
    - 交互：飞书接入，补全功能，如支持卡片审批，/new、/rename、/delete 等破坏性Gateway 命令，等。参考HermesAgent的实现/Users/niean/code/github.com/niean/hermes-agent
        - 审阅：Spec spec-260616-feishu-gateway-safety.md
    - 交互：飞书，N-Agent收到消息后、给一个提示，比如给用户的飞书消息加一个表情(参考HermesAgent的飞书交互)
    - 工具：[KF]实现Skill子系统。参考HermesAgent的实现/Users/niean/code/github.com/niean/hermes-agent
        - 待办：Skill Hub / 远程拉取 / GitHub 同步、plugin namespace、prerequisites/collect_secrets、skill_run 子进程或 docker 沙盒、飞书 Gateway 直接调起、watcher 自动同步
        - 审阅：发现其中的严重问题，Spec spec-260616-skill-subsystem.md
        - 审阅：发现其中的严重问题，Plan plan-260616-skill-subsystem.md
        - 迭代：Dashboard，左导`工具`下二级菜单 知识、MCP、Skill、Plugin
        - 知识：页面调整，只保留知识检索、去掉N-KB依赖，知识检索采用`工具列表`一样的组件构成
        - MCP：页面调整，自上而下依次为 MCP站点、MCP工具，MCP工具采用`工具列表`一样的组件构成、筛选其中类型为mcp的工具
        - 内置：页面调整，工具列表只展示类型为builtin的工具；skills_list、skill_view建议调整类型为builtin
    - 工具：通过自然语言(来源如飞书IM消息)、管理定时任务，补全对应的Agent工具
        - 审阅：Spec spec-260616-feishu-natural-schedule.md，发现其中的严重问题

[20260617]
- Issue
    - 交互：飞书消息投递失败
    - 任务：定时任务sched-a406eae127164f3a970f63dbfab24c5d，只有第1个周期执行了，之后都是skipped_missed，且调度周期是15分钟(不是预期的5分钟)
- NFR
    - 源码：AgentCore，整理DDD文档
- FR
    - 工具：manage_schedule，agent管理工具，需要暴露到Dashboard上
    - 知识：[KF]检索支持多KB。抽象知识库SPI，支持多种KB类型，包括N-KB、Ragflow；Dashboard支持KB的增删改查
    - 平台：[KF]抽象平台功能，纳管飞书IM，后续还要支持钉钉、微信等IM；Dashboard上，左导增加一级菜单`平台`，展示平台信息(如platform、session id、thread等) 
        - 审阅：发现严重问题，Spec spec-260617-platform-aggregate.md
    - 前端：Dashboard，左导一级菜单，平台放到模型之下、健康之上，健康改名为观测
    - 前端：弹出框支持ESC键退出
    - 模型：[KF]支持Anthropic的API协议，按照DDD规范、做好领域抽象。参考DDD文档`## AgentCore`章节，也参考HermesAgent的实现/Users/niean/code/github.com/niean/hermes-agent
        - 审阅：发现严重问题，spec-260617-anthropic-provider.md
    - 对话：一轮对话、多次调用工具时，结果放到一个`工具调用调试信息`气泡中（当前是每个工具调用 一个气泡）
    - 工具：新增内置工具web_fetch => 天气查看Skill冒烟成功

[20260624]
- Issue
    - 任务：sched-a406eae127164f3a970f63dbfab24c5d，session_missing
        - 参考Hermes的做法：飞书Chat支持设置sethome，定时任务通知自动锁定home chat。HermesAgent源码 /Users/niean/code/github.com/niean/hermes-agent

[20260626]
- FR
    - 记忆：[KF]支持外置Memory。分为全局(1个)、外置(多个)两级，支持通过Dashboard做CRUD。参考HermesAgent的实现/Users/niean/code/github.com/niean/hermes-agent
    - 记忆：修改Agent系统提示词，去掉MVP相关表述（当前N-Agent已经是正式产品）
        - 记忆：外置记忆，查看操作 使用弹出框
        - 记忆：外置记忆，编辑操作 使用弹出框
        - 记忆：外置记忆，启用列只展示启用状态(和知识库管理列表的启用保持一致)，启停操作放到编辑弹出框
        - 记忆：外置记忆，删除按钮 去除危险提示（下划线）
        - 记忆：外置记忆，新增`详情`字段、放到`类型`后，展示记忆内容（设置最大字符数256，超出后展示...）
        - 记忆：系统提供者，`启用`列只展示启用状态，跟外置记忆保持一致；新增操作列，编辑按钮支持启停设置等；新增`详情`字段，放到`类型`后，要求跟外置记忆保持一致
        - 记忆：外置记忆，新增外部记忆时，要囊括编辑能力，如编辑内容、允许开启等
        - 记忆：外置记忆，编辑框要支持编辑内容
        - 记忆：外置记忆，查看、编辑的弹出框，页面要素要保持一致，唯一区别系是否可编辑和保存
    - 对话：Chat支持外置记忆管理。默认开启全局(builtin)，不开启外置记忆，每个Session使用独立的记忆配置，每个Session只允许设置一个外置记忆、且首轮后不允许修改(影响前缀缓存命中)
    - 对话：停用状态的外置记忆，也会被展示出来，不符合预期
    - 对话：对话框，外置记忆默认收起，点击展开图标后可编辑，再点击收起图标后恢复到默认的收起

[20260627]
- NFR
    - 源码：整理Memory体系，输出到DDD文档的`## Memory`章节，要求言简意赅
    - 源码：Context Frame 的真实样例

[20260628]
- FR
    - 记忆：实现G1 retrieved memory 实际召回。创建新的spec文件，原始需求来自spec-260628-memory-context-gaps-vs-hermes.md
        - 审阅：发现严重问题，spec-260628-retrieved-memory-prefetch.md。外部 provider如mem0（服务端事实库）、holographic（HRR 向量库）、honcho（用户建模 dialectic 库），数据不是 Markdown 文本、无法做"静态快照字符串"，只能通过 query 走向量/语义检索拿回相关片段。类似情况，本次spec要能优雅支持
    - 记忆：修复G2 流式 SSE 路径的 `<memory-context>` scrubber 缺失，原始需求来自spec-260628-memory-context-gaps-vs-hermes.md
    - 记忆：修复G3 `on_pre_compress` 钩子缺失，原始需求来自spec-260628-memory-context-gaps-vs-hermes.md
    - 记忆：修复G4 sync_turn真实落地，原始需求来自spec-260628-memory-context-gaps-vs-hermes.md
    - 记忆：修复G5`on_session_switch` / `on_session_end` 钩子缺失，原始需求来自spec-260628-memory-context-gaps-vs-hermes.md
    - 记忆：修复G6：trust 评分 / 时间衰减 / 矛盾检测缺失，原始需求来自spec-260628-memory-context-gaps-vs-hermes.md
        - 审阅：发现严重问题，spec-260628-memory-g6-trust-decay-contradiction.md
    - 记忆：修复G7`on_delegation` + subagent skip_memory 缺失
    - 记忆：修复G8 provider 失败熔断缺失
    - 记忆：修复G10 单外部 provider 互斥约束未明确，原始需求来自spec-260628-memory-context-gaps-vs-hermes.md

[20260629]
- NFR
    - 治理：外置记忆，更名为系统、文件、检索记忆(从载体出发)，检索记忆Provider互斥、只能有1个Active
- FR
    - 记忆：支持非本地Markdown文本的外部provider，如holographic（HRR 向量库）、mem0（服务端事实库）、honcho（用户建模 dialectic 库），只能通过query走向量/语义检索拿回相关片段。参考HermesAgent的实现/Users/niean/code/github.com/niean/hermes-agent
        - 审阅：发现严重问题，spec-260628-external-query-providers.md
        - 修复：Dashboard，记忆页面的系统提供者、外置记忆被改没了
    - 记忆：完善和验收HoloGraphic记忆功能
        - 迭代：记忆管理，检索记忆Dashboard支持CRUD，样式要跟`文件记忆`完全对齐
        - 迭代：对话页勾选区，纳管检索记忆，默认不勾、用户可主动勾选，跟文件记忆保持一致；纳管检索、文件记忆可同时选中不冲突，但同类Provider只允许选择1个(类型如系统记忆、文件记忆、holographic)
        - 迭代：我的对话框勾选了holographic，对话过程中没用上，首轮对话后holographic的勾选被去掉了
    - 对话：对话框，外置记忆在UI上做分组，如系统、文件、检索，分组效果可以是一个简洁的、不突兀的圆角矩形
    - 对话：历史对话框忠实展示当时的外置记忆Provider，不受active筛选条件影响，检索Provider不正确
    - 记忆：完善和验收mem0记忆功能
        - 检索记忆 Dashboard 是否支持 mem0 的 CRUD、样式与"文件记忆"对齐
        - 对话页勾选区是否纳管 mem0、默认不勾、同类 provider 互斥
        - 实际对话中首轮后 mem0 勾选是否被去掉 / 是否真正生效（这是 holographic 已暴露的 bug，mem0 很可能同样存在）

[20260630]
- FR
    - 记忆：外置记忆描述改为标题Tips
    - 记忆：完善和验收honcho记忆
        - 补充：使用云端honcho，workspace、apikey、peer已经申请好
        - 迭代：记忆管理页，检索记忆列表增加列`召回模式`，放到详情后、列宽10%(从详情出)，每行Provider正确展示列取值，同时列名称添加Tips、展示三种模式的含义
    - 记忆：优化holographic召回模式，同时支持上下文注入、tools安全调用
        - 审阅：发现严重问题，spec-260630-holographic-recall-mode.md
        - 已交付 2026-06-30：recall_mode 三模式（hybrid默认/context/tools）+ 拆出 fact_search 只读检索工具收敛信任边界 + 修复 active provider 编辑静默不生效
    - 记忆：检索记忆，编辑时无法修改名称，需要支持改名
    - 沙盒：[KF]实现执行沙盒Sandbox，至少包括Local和Docker
        - 要求：参照HermesAgent，源码/Users/niean/code/github.com/niean/hermes-agent
        - 审阅：发现严重问题(20个)，spec-260630-tool-sandbox.md
        - 审阅：发现严重问题(20个)，plan-260630-tool-sandbox.md
        - 约束：相比HermesAgent，spec做了哪些裁剪、折中，明确List出来、写到spec文件
        - 迭代：沙盒Dashboard，活跃沙盒、待确认、已确认代码、执行历史改用List表格，页面元素遵从HE
        - 迭代：沙盒Dashboard，配置sector改用表格，docker类型为一行、local类型也可为一行，公共列
        - 验收：①飞书IM端到端测试，发一条让LLM调execute_code的消息，验证confirmation card → 用户点确认 → execute_confirmed 的完整链路；
        - 验收：②飞书里连续两次发同样的代码请求，第二次应直接执行（无确认卡），fast-path免再确认；
        - 验收：③Dashboard管控操作，如释放沙盒；
        - 验收：④回调工具在沙盒内真实可用；
        - 验收：⑤沙盒内network隔离；
        - 验证：⑥workspace只读保护；
        - 验证：⑦执行超时强杀；
        - 验证：⑧空闲回收；
        - 验证：⑨Session沙盒生命周期跟随Session；

[20260701]
- FR
    - 沙盒：实现执行沙盒Sandbox，验收和收尾
        - 前端：概述，补全沙盒Sandbox信息
        - 沙盒：待确认、已确认、执行历史的代码，操作列新增代码按钮、点击后弹框展示代码，弹框风格要跟`记忆`页的弹框保持一致
        - 飞书：执行一次，点击后Disable、防止错误点两次
        - 沙盒：执行历史，操作列支持查看详情
        - 沙盒：配置，docker干掉最长存活、保留空闲回收
        - 沙盒：活跃沙盒，要展示pod唯一标志如name；新增废弃沙盒列表，放在活跃沙盒下方，展示已经废弃的沙盒，列表风格跟活跃沙盒一致
        - 沙盒：废弃沙盒，标注废弃的原因，如手动释放、空闲到期
        - 沙盒：沙盒列表，类型列放到Pod名称列之前，包括活跃、废弃
        - 沙盒：执行历史，操作支持删除
        - 沙盒：废弃沙盒，历史数据会被删除，希望长期保存
        - 沙盒：沙盒，Pod名称列改名为`沙盒标识`，解除对docker类型的耦合
        - 沙盒：执行历史，代码合并到详情弹出框展示，干掉代码按钮、详情放到删除右侧
        - 沙盒：Dashboard，刷新按钮只刷新UI容器、不要刷新整个页面
    - 沙盒：记忆、沙盒调整为一级菜单，跟工具并列
    - 沙盒：沙盒execute_code抽象为通用领域能力，支持从Chat、Http、CLI、IM等`所有通道`；核心哲学是execute_code不需要确认，沙盒即安全边界。要求参照HermesAgent，源码/Users/niean/code/github.com/niean/hermes-agent。
        - 审阅：发现并修复严重问题(20个)，spec-260701-execute-code-direct.md
        - 验证：Chat、CLI、飞书IM、OpenWebUI，execute_code执行验证通过

[20260702]
- FR
    - 会话：会话列表，来源格式明确为`{一级}[/{二级}]`，其中一级包括dashboard、api、cli、gw、schedule，二级有gw/feishu；会话ID的前缀，应该体现出且对应第一级
    - 会话：会话列表，查看按钮改为弹出框，风格跟`记忆`弹出框保持一致
    - 前端：左导菜单折叠状态控制，刷新浏览器时默认使用上次状态、没有上次状态则默认展开，一级、二级均如此；一级菜单展开时，二级菜单应使用上次状态
        - 工具：二次菜单展开后，`工具`持续是选中样式，如果此时选中其它一级菜单、造成困扰。请修复`工具`样式
    - 沙盒：execute_code支持定时任务入口。看下HermesAgent是怎么做的，源码/Users/niean/code/github.com/niean/hermes-agent(结论：定时任务走ChatCompletion能触发execute_code)
    - 文档：按照DDD原则，整理Sandbox模型、执行链路等到DDD文档的 ##Sandbox章节
    - 任务：修复模型占位符问题
    - 任务：补全任务详情页(URL可达)，任务列表点`立即执行`后弹框、告知触发成功，弹框上详情按钮跳转任务详情页

[20260703]
- FR
    - 前端：左导，任务下、会话上增加分割符，隔离业务和平台大类(能力呈现要分类)
    - 工具：Skill走"目录扫描 + SQLite元数据"，跟MCP等未对齐(SQLite 做CRUD)，对齐之
    - 工具：[KF]实现Plugin功能。Follow Hermes plugins生态、可零成本移植。
        - 要求：参照HermesAgent，源码/Users/niean/code/github.com/niean/hermes-agent
        - 审阅：发现并修复严重问题(20个)，spec-260703-plugin-subsystem.md
        - 审阅：发现并修复严重问题(20个)，plan-260703-plugin-subsystem.md(需求spec-260703-plugin-subsystem.md)
        - 迭代：Plugins，详情、配置使用弹出框交互，风格跟记忆弹出框保持一致
        - 迭代：飞书IM，tool_call_id为空、导致无结果
        - 验证：CLI，python -m app.interfaces.cli plugin list
        - 验证：Dashboard UI，Http API
        - 验证：Chat、飞书IM，端到端测试
        - 验证：HermesAgent插件平移，零改造

[20260704]
- NFR
    - 知识：Dashboard，知识工具干掉按钮`刷新描述`
    - 网关：Dashboard，平台改名为网关(Gateway)，Dashboard、代码、文档等所有地方彻底修改
    - 知识：ragflow-kb，probe失败，帮修复(原因：Ragflow资源被限制、无法启动)
- FR
    - TUI：[KF]实现生产级TUI功能，对标Hermes
        - 要求：参照HermesAgent实现，源码/Users/niean/code/github.com/niean/hermes-agent
        - 审阅：发现并修复严重问题(20个)，spec-260704-cli-experience.md
        - 验收：我应该如何验收TUI(CLI)？给出功能验收的列表
    - TUI：TUI补全领域命令。包括领域能力管理子命令（provider/knowledge/mcp/schedule/sandbox/memory/platform CRUD CLI）、运维子命令（doctor/config/logs）、sessions browse picker
        - 要求：参照HermesAgent实现，源码/Users/niean/code/github.com/niean/hermes-agent
        - 审阅：发现并修复严重问题(20个)，spec-260704-cli-commands.md
            - < spec文件已修改，请审阅
            - < 确认。plan写好后，需要我再次确认
            - < Write file(特别是plan) 已经出错N次了，请记录`项目教训`避免再犯
        - 审阅：发现并修复严重问题(20个)，plan-260704-cli-commands.md(需求spec-260704-cli-commands.md)
            - < plan文件已修改，请审阅
        - 验收：生成验收事项和文件，spec/verify/verify-260704-cli-commands.md，要求 ①只包含端到端的人工、半人工验收项 ②分组List ③去掉视觉效果格式如加粗强调emoji表情
        - 迭代：新增的cmd，大部分只能单次执行；要求所有命令必须支持chat REPL slash cmd
        - 迭代：cmd输出结果格式，默认json，可通过flag指定 --json(no-op) --form --yaml
        - 迭代：chat slash cmd输入第一个字母后就没有动态提示了，二级子命令则能多单次补全
        - 文档：README.md，声明`单次执行cmd`都有对应的`chat slash cmd`，只介绍chat slash cmd
    - ACP：[KF]支持ACP服务端(Agent Client Protocol)，注意 N-Agent在Pod部署、访问发起方VsCode插件在宿主
        - 要求：参照HermesAgent实现，源码/Users/niean/code/github.com/niean/hermes-agent
        - 配置：vscode配置 docker exec -i n-agent-n-agent-1 n-agent acp
        - 审阅：发现并修复严重问题(20+个)，spec-260704-acp-server.md
            - < spec文件已修改，请审阅
            - < 确认。plan写好后，需要我再次确认
        - 审阅：发现并修复严重问题(20+个)，plan-260704-acp-server.md(需求spec-260704-acp-server.md)
            - < plan文件已修改，请审阅
        - 验收：生成验收事项和文件，spec/verify/verify-260704-acp-server.md，要求 ①只包含端到端的人工验收项，禁止已自动验收项 ②分组List ③禁用视觉效果格式如加粗强调emoji表情
        - 迭代：acp的session id 25e0b02ecf8c46daa7c7bf4baca34dbd，违反命名规范、修正之

[20260705]
- NFR
    - 入口：入口适配器抽象和改名，对齐DDD规范
    - 平台：Dashboard平台，全面干掉CLI(Local)、不要这个概念/抽闲了，混淆太严重
    - 会话：来源gw/feishu改为feishu，代码、文档、数据库都要修改
    - 对话：Chat，默认关闭系统builtin记忆
    - 前端：所有确认框、提示框，垂直方向从顶部对齐、改为居中
        - 迭代：所有的删除确认框，右下角按钮顺序修改为先取消后确认，默认选中取消
    - 前端：列表，操作按钮命名和顺序调整，①命名标准化，Probe->探活、立即执行->执行、激活->启用、配置->编辑、查看->详情；然后，②顺序调整，从左到右先变更类再查询类，典型顺序如 `启用|停用、探活、刷新，执行，编辑、删除、详情`
    - 前端：列表，删除非必要的刷新按钮，新建按钮改名为新增
    - 沙盒：废弃沙盒，新增操作列，支持删除按钮
    - 任务：刷新、新建任务按钮，放到任务列表上，同时`新建任务`改名为`新增`
- FR
    - 会话：Dashboard，会话列表操作支持删除、放到查看前，风格跟沙盒保持一致
    - ACP：验收和迭代
        - 迭代：ACP，用户消息session/prompt等走GatewayService → ChatCompletionService链路，ACP协议生命周期保留在ACP适配器，对齐飞书IM、TUI

[20260706]
- FR
    - 文档：TUI，整理一次会话的完整执行链路，简洁输出
    - ACP：相比Hermes，ACP还缺少哪些功能？明确List出来

[20260707]
- HE
    - HE：修改Harness Workflow，①Phase2 spec生成后，引入Third模型、审阅修改spec文件，然后交回给主流程模型加载和审阅；②Phase3 plan生成后，引入Third模型、审阅修改plan文件，然后交回给主流程模型加载和审阅。我希望在现有的 .harness框架上做修改，且希望IDE无关(claudecode codex均能支持)，Third审阅交互在Codex IDE可见
    - HE：修改Harness Workflow iterate-feature、refine-feature，任务最后一个Phase结束后，允许执行自定义hook命令、未定义hook则跳过；hook放在 .harness/framework/hooks/after-finish.sh
- FR
    - 对话：[KF]多模态支持图片输入，vision_analyze，覆盖入口包括 API、飞书IM、ACP等
        - 参照：HermesAgent源码/Users/niean/code/github.com/niean/hermes-agent
        - 审阅：发现并修复严重问题(20+个)，spec-260706-multimodal-vision.md
            - < spec文件已修改，请审阅
            - < 确认。plan写好后，需要我再次确认
        - 审阅：发现并修复严重问题(20+个)，plan-260706-multimodal-vision.md(需求spec-260706-multimodal-vision.md)
            - < plan文件已修改，请审阅
        - 验收：生成验收事项和文件，spec/verify/verify-260706-multimodal-vision.md，要求 ①只包含端到端的人工验收项，禁止已自动验收项 ②分组List ③禁用视觉效果格式如加粗强调emoji表情
    - 对话：多模态支持图片输入，验收、优化
        - 对话：Chat，我发送的照片，AI回复后就看不到了；发送按钮，去掉蓝底色、改为普通底色
        - 对话：Chat，外部记忆改名为记忆，点击记忆按钮后弹出Popover气泡卡片、选择记忆类型
        - 对话：Chat，图片在对话框的渲染，小蓝边太宽了、很难看
        - 对话：Chat，图片在对话框中，支持点击后弹出框预览
    - 会话：会话列表，来源字段大多hardcode，请修改为公共枚举、方便治理
    - 沙盒：支持Terminal命令执行，危险命令确认(对标Hermes)
        - 参照：HermesAgent源码/Users/niean/code/github.com/niean/hermes-agent
        - 验收：生成验收事项和文件，spec/verify/verify-260707-terminal-in-sandbox.md，要求 ①只包含端到端的人工验收项，禁止已自动验收项 ②分组List ③禁用视觉效果格式如加粗强调emoji表情

[20260708]
- HE
    - HE：补充`人工验收`到plan落盘后，独立旁路、不影响自动化流程
- FR
    - 沙盒：支持Terminal命令执行，验收、优化
        - 沙盒：Dashboard执行历史，区分`工具名称`execute_code|terminal，放在code_hash列之后、状态列之前
        - 沙盒：Dashboard执行历史，授权工具放到详情、表格删除该列，同时`耗时(ms)`列修正为右对齐、状态列修正为左对齐
    - 插件：支持多分类目录，对标Hermes功能
        - 参照：HermesAgent源码/Users/niean/code/github.com/niean/hermes-agent
    - 对话：Chat框，记忆popover按钮 图标改为`大脑`形状
    - 左导：记忆，一级菜单图标、改为和Chat记忆popover按钮一致的`大脑`形状，大小控制遵循左导菜单规范

[20260709]
- HE
    - HE：修改Third Review，总结时要给出具体的修改数量，现状是笼统的20+
    - 文档：短期记忆上下文，压缩原理
- FR
    - 记忆：上下文实现短期记忆压缩，从而节省Token消耗
        - 参照：HermesAgent源码/Users/niean/code/github.com/niean/hermes-agent
        - 迭代：改造N-Agent上下文压缩，对齐HermesAgent增量压缩方法；N-Agent中，摘要信息也可以持久化，只要做好标签/逻辑隔离。增量压缩：本次摘要 = llm_summary(上次摘要 + 新增消息)
    - 记忆：上下文实现短期记忆压缩，验收、优化
        - 对话：支持通过 ①slash命令 ②对话时的要求，这两种方式触发消息压缩
        - 会话：持久化messages表保留所有摘要记录，上下文只使用最新的摘要，Dashboard Chat渲染所有历史摘要、方便用户观测压缩行为
        - 对话：Dashboard Chat，摘要渲染改用`工具调用调试信息`的样式，默认折叠可展开、黄底色等，标题`对话摘要`

[20260711]
- HE
    - HE：参考iterate-feature P7，给fix-bug工作流P5、补齐after-hook
- NFR
    - 需求：对比N-Agent和Hermes-Agent，看下N-Agent还有哪些功能完全缺失、功能不完备，分析结果就到spec/active/文档即可。Hermes-Agent源码/Users/niean/code/github.com/niean/hermes-agent
- FR
    - 技能：将skill索引主动注入到system prompt，避免高频发生的skill_list
    - 记忆：mem0支持自动预取+工具调用双轨制，自动预取又分静态(放入system prompt)、动态(放入user msg前缀)
    - 左导：点击一级菜单后，URL要切换到对应的新路径上，现在有问题的至少有对话、观测
    - 观测：[KF]实现Token、上下文等统计信息，对标Hermes-Agent
        - 参照：HermesAgent源码/Users/niean/code/github.com/niean/hermes-agent
        - 观测：重构观测页面，页面布局自上而下是整体总览卡片(所有会话总和)、会话表格(翻页、每页20条)，会话列表操作列支持`详情`按钮，点击跳转会话观测详情页面
        - 观测：会话详情，上下文分布并未正常展示分类条形图
        - 观测：会话列表，点击详情后跳转到一个新开的Tab、而不是在原页面
        - 观测：会话详情，API调用历史的模型使用率N-Agent，并非真实调用的模型名称；观测页面暂定对管理员开放，展示真实模型名称、而不是归一化后的N-Agent，你可以记录两个名称、分场景展示不同名称
        - 观测：会话详情，API调用历史，在模型列后新增一列`调起类型`，取值如 user、tool、消息压缩；取值可以探讨下
       - 观测：会话详情，API调用历史，增加操作列、支持详情按钮，点击后弹框，内容包括输入、输出，Json格式展示
       - 观测：会话详情，API调用历史，详情展示工具定义字段
       - 观测：会话详情，API调用历史-详情，除了工具定义、输入消息、输出消息 上下文还包括其它哪些内容？展示在详情弹出框
       - 观测：会话详情，API调用历史-详情，取值去掉加粗等特殊效果，如`记录ID: #57`中的`#57`、`输入: 5,583`中的`5,583`、`估算成本: $0.000000`中的`$0.000000`
       - 观测：会话详情，API调用历史-详情，Token 用量 (Usage)、成本 (Cost)，两部分合并为一项，放到第一位置；调用元信息 (Meta)、生成参数 (Generation Params)，两部分合并为一项、放到第二位置，`生成参数`作为一项、加入到调用元信息下

[20260712]
- FR
    - 记忆：消息压缩Bugfix，修复summary位置错误，tail保护从20调整为10、默认关闭tail_budget
    - 会话：会话列表，在ID列后增加`对话轮数`列
    - 观测：新增缓存命中率指标，覆盖到整体总览卡片(放到缓存Token之后)、会话详情卡片(放到缓存Token之后)、会话详情之API调用历史(放到缓存列之后)
    - 观测：缓存指标拆分为缓存读、缓存写，涉及整体总览卡片、会话详情卡片、会话详情列表
    - 观测：新增指标`归一化Token总量`，归一化到标注输入维度，`Tn = Ti + Tic*0.2 + To*5`，其中Ti系标准输入、Tic输入缓存单价1-2折(选2)、To输出单价4-6倍(选5)
    - 观测：FE样式优化和功能完善
        - 迭代：会话详情，API调用历史-详情，工具定义 (Capability Context)默认收起、点击展开，收起/展开图标不要太显眼
        - 迭代：修改会话详情卡片，使跟整体总览卡片保持一致
        - 迭代：会话详情，API调用历史-详情，文本框右上角支持悬浮复制按钮
        - 迭代：上下文压缩记录，跟`API调用历史`的样式做成一致
        - 迭代：FE不展示`成本`项(数据结构上不删除)，包括整体总览卡片、会话列表、会话详情卡片、API调用历史列表
        - 迭代：会话详情-API调用历史列表，延迟列标题改为`延迟(ms)`，列取值去掉ms单位、变为纯数字(千分位展示法)
        - 迭代：会话列表的上一页、下一页改为左右图标符号
        - 迭代：会话详情，修改API调用历史、上下文压缩记录的表格翻页样式，使跟整体页的会话列表保持一致
        - 迭代：应用日志，完整打印LLM API调用的输入、输出；日志中的汉字被unicode编码、看不懂，希望日志展示的是汉字原文
        - 迭代：观测-会话列表，数据列头也采用右对齐、跟数据取值的对齐方式保持一致，包括API 调用、归一化 Token两列。进一步规范，FE列表数字列，列头名称、数据取值均右对齐，数据取值采用千分位法展示
        - 迭代：观测-会话-会话详情，上下文压缩记录列表增加操作列、支持详情(弹框)，展示压缩后、压缩前的消息对比
    - 前端：左导菜单&弹出框样式调优
        - 弹框：跳出弹出框后，焦点只允许在弹出框，背后的页面不能再操作(当前还可以滚动)
        - 左导：左导菜单，选中二级菜单时，去除对应一级菜单的选中效果，二级菜单选中效果去掉左侧边深色强调
        - 观测：观测菜单二级化调整，增加左导二级菜单`会话`、`组件`；会话，路径/observations/sessions，承接现有的`观测`页面；组件，路径系/observations/modules，承接现有的`健康`页面。修改后，观测一级菜单不再承接路由(类似工具)

[20260713]
- FR
    - 上下文：[KF]Context拆分子域，并独立Graph节点
    - 工具：拆分ToolPolicy，先做ACP。集中式Policy改为领域自治，保留共性抽象、放到Shared Kernel
        - 飞书：支持 ToolPolicy 工具审批，审批方式跟`破坏性 Slash Command`保持一致
        - TUI：支持 ToolPolicy 工具审批，审批方式跟`破坏性 Slash Command`保持一致(如果有)；实现方式，参考ACP和飞书支持ToolPolicy

[20260714]
- FR
    - 任务：执行弹框，风格调整为一致(如对齐编辑弹框)
    - 任务：执行按钮，弹框确认后再触发执行
    - 任务：详情页面，使用本Tab、不再新建Tab
    - 任务：任务详情，干掉右上角的按钮(刷新 执行)，返回按钮样式、改为`会话详情`页样式
    - 任务：任务页面，整体总览修改样式、参考`会话`页，主要是标题、Sector风格，卡片风格维持现状
    - 平台：健康状态，参照`安全-策略`的页面样式修改，特别是各Sector的数据展示样式
    - 左导：平台、观测间，增加分隔符
    - 安全：[KF]Policy领域自治，包括Turn、Context、LLM、Tool、Memory、Sandbox、Gateway、Schedule、Budget、InformationFlow 十类 Policy
        - Spec：spec-260714-policy-governance.md
        - Plan：plan-260714-policy-governance.md
        - 待办：配置暂由代码默认值和 Settings 提供，不新增 Dashboard 策略管理或 SQLite Policy 表，但保留未来按团队、项目、个人租户解析独立 Policy 的稳定接入点
    - 安全：新增安全一级菜单页面展示Policy，放到`观测`下方，分Sector、表格展示领域Policy策略

[20260715]
- FR
    - 工具：新增host_terminal由宿主执行命令，terminal维持Sandbox执行；进而，迁移`拍照上传`Skill到N-Agent


---

[待办]
- HE
- NFR
    - 知识：UDS，详细原理、Go样例
    - 治理：IAM，安全护栏
    - 治理：能力分层，平台是提供方视角(管理员)、业务是使用方视角(用户)。以Skill为例，Skill系平台能力、Workflow是业务能力，两者复用领域层
    - 前端：使用Element UI，重构前端代码，要求①保持功能一致、②最大限度的使用Element UI组件库(减少自己写的代码)。Element UI的项目规范，参考 /Users/niean/code/git.zuoyebang.cc/odin/odin-fe
- FR
    - 工具：Skill自进化，参考HermesAgent的实现/Users/niean/code/github.com/niean/hermes-agent
    - 架构：MoA，对标Hermes
    - 插件：lifecycle hooks、CLI subcommand、pip entry points、plugin override builtin，plugin依赖声明
    - 租户：引入租户概念，如团队、项目、个人，租户间特定资源隔离
    - 管理：秘钥Store(类似平台)

---

[待验证]
- 工具：MCP支持stdio类型

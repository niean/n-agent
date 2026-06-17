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
- FR
    - 工具：manage_schedule，agent管理工具，需要暴露到Dashboard上
    - 知识：[KF]检索支持多KB。抽象知识库SPI，支持多种KB类型，包括N-KB、Ragflow；Dashboard支持KB的增删改查
    - 平台：[KF]抽象平台功能，纳管当前的飞书IM，后续还要支持钉钉、微信等IM；Dashboard上，左导增加一级菜单`平台`，展示平台信息(如platform、session id、thread等) 
        - 审阅：发现严重问题，Spec spec-260617-platform-aggregate.md
    - 前端：Dashboard，左导一级菜单，平台放到模型之下、健康之上，健康改名为观测



---

[待办]
- Issue
- NFR
    - 前端：使用Element UI，重构前端代码，要求①保持功能一致、②最大限度的使用Element UI组件库(减少自己写的代码)。Element UI的项目规范，参考 /Users/niean/code/git.zuoyebang.cc/odin/odin-fe
    - 源码：AgentCore
    - 治理：IAM，安全护栏
- FR
    - 工具：Sandbox
    - 工具：相比Hermes，还缺少如下功能
        不实现 plugin system。
        不实现 agent-loop 特殊工具，比如 delegate_task、memory、todo。
        不实现 pre/post tool hook。

---

[待验证]
- 工具：MCP支持stdio类型

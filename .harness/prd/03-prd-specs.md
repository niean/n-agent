<!-- SUMMARY: N-Agent 的迭代需求池，记录 Agent MVP、Chat 修复、System Prompt、模型名隐藏和 /chat 交互优化需求 -->
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
- FR
    - 模型：Active状态使用绿色对号图标
    - 模型：新增、修改模型，改为弹出框页面
    - 工具：[KF]Tool完善抽象，除运行时外的其它能力
    - 工具：工具列表，Schema查看改为弹出框
    - 交互：[KF]支持飞书Bot入口。除了Dashboard还应该支持CLI等方式，Gateway。参考HermesAgent，设计并实现
        - 迭代：飞书接入，使用长连接，会话列表也应包含飞书Session
    - 工具：[KF]新增MCP站点管理能力，前端系管理页面，后端按照DDD分层实现
        - 迭代：管理页面，MCP站点管理独立为左导二级菜单
    - 工具：MCP支持stdio类型


---

[待办]
- Issue
- NFR
- FR
    - 交互：飞书接入，支持卡片审批、Drive评论、跨频道主动投递，以及/new、/rename、/delete 等破坏性Gateway 命令
    - RunTime：Sandbox(docker)
    - 定时任务
    - 治理：IAM，安全护栏
    - 交互：飞书IM接入
    - 工具：相比Hermes，还缺少如下功能
        不连接 MCP server，不做 mcp_{server}_{tool} 动态发现。
        不实现 Skill 目录扫描、skills_list、skill_view。
        不实现 plugin system。
        不实现 toolset enable/disable 组合解析。
        不实现 agent-loop 特殊工具，比如 delegate_task、memory、todo。
        不实现 pre/post tool hook。

---

[待验证]
- 会话：Title自动生成

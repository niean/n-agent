# N-Agent

## 关键设计
- 框架：DDD规范，DockerCompose部署；Python语言，uvicorn、FastAPI、LangGraph库，SQLite组件
- 功能：Agent Loop，LLM Adapter，Tool Registry，Memory/Context，Chat Dashboard
- 接口：支持OpenAI-Compatible协议的HttpAPI，兼容Open-WebUI，流式优先、支持工具调用；支持OpenAI tool-calling协议
- 模型：LLM Adapter，支持多Provider
- 工具：支持可插拔工具框架，留好权限和风险控制的接口
- 记忆：持久化会话记录，支持长任务上下文续跑

## 迭代规划
本次 MVP 是完整 Agent 能力的第一阶段，设计必须保证后续能力可以在现有边界内继续演进，而不是推倒重来。参考 Hermes-Agent 当前能力，本项目在 MVP 之后需要逐步补齐以下能力。

后续能力差距：
- 交互入口: Hermes-Agent 有 CLI、TUI、Telegram、Discord、Slack、WhatsApp、Signal、Email 等入口；当前 MVP 只有 OpenAI-compatible HTTP 和简单 Dashboard。
- 工具体系: Hermes-Agent 有终端、文件写入、patch、搜索、浏览器、Web 搜索、抓取、MCP、视觉、语音、图片生成、日历、Feishu、Google、Home Assistant、代码执行等工具；当前 MVP 只有时间、计算、目录列表和文本读取。
- 开发 Agent 能力: Hermes-Agent 能通过受控终端、文件 patch、代码搜索、浏览器和子任务委派完成真实开发任务；当前 MVP 不包含 Shell、写文件和 patch。
- Provider 生态: Hermes-Agent 支持 OpenAI、Anthropic、Bedrock、Gemini、Mistral、OpenRouter、Nous、Codex Responses、Copilot、Kimi、MiniMax、本地兼容端点等；当前 MVP 只实现 OpenAI-compatible Provider。
- Memory 与学习闭环: Hermes-Agent 有长期记忆、session search、skills 创建和改进、用户画像、周期性知识持久化提醒；当前 MVP 只有 SQLite 会话、工具调用、任务状态和启发式摘要。
- Context 管理: Hermes-Agent 有上下文压缩、prompt caching、上下文文件、子目录提示、工具结果裁剪、会话搜索和手动压缩反馈；当前 MVP 只定义简单 summary。
- 安全与权限: Hermes-Agent 有工具审批、guardrails、路径安全、URL safety、凭据隔离、网站策略、命令 allowlist、DM pairing 和安全文档；当前 MVP 只有风险等级和路径边界。
- 自动化与后台任务: Hermes-Agent 有 cron scheduler、scheduled automations、平台投递和后台任务管理；当前 MVP 不包含定时任务。
- 多 Agent 与并行能力: Hermes-Agent 支持 delegate tool、parallel workstreams、mixture of agents；当前 MVP 明确不做多 Agent。
- MCP、插件和 Skills 生态: Hermes-Agent 支持 MCP、plugins、Skills Hub、agentskills.io、skill provenance、skill usage、skill sync；当前 MVP 只有 Tool Registry。
- 可观测性与运维: Hermes-Agent 有 doctor、logs、insights、usage、cost、debug、dashboard、gateway status；当前 MVP 只有基础 health 和 Dashboard 会话/工具调用查看。
- 部署与运行环境: Hermes-Agent 支持 local、Docker、SSH、Singularity、Modal、Daytona、Vercel Sandbox、Termux、Windows 等；当前 MVP 只是本地 FastAPI 服务。

后续迭代优先级建议：
1. 受控 Shell、文件写入、patch、审批流和 workspace 安全，补齐真实开发 Agent 能力。
2. Toolset、MCP、工具可用性检查、schema sanitizer、工具输出限流和工具结果持久化，补齐可扩展工具生态。
3. Context compressor、session search、长期 Memory、摘要压缩和任务恢复，补齐长上下文和跨会话连续性。
4. 多 Provider、Provider fallback、模型能力检测、usage/cost 统计，补齐模型生态与运维基础。
5. CLI/TUI 和更完整 Dashboard，补齐开发者交互体验。
6. Cron、后台任务、通知投递和失败重试，补齐自动化能力。
7. Skills、自我改进 loop、用户画像和经验沉淀，补齐 self-improving 能力。
8. 子 Agent、并行执行、delegate 和结果汇总，补齐多 Agent 能力。
9. 多平台 Messaging Gateway、远程运行环境和部署安装体系，补齐产品化运行能力。

本次实现计划只能覆盖 MVP 验收标准，不应提前实现上述完整能力；但代码结构、领域模型、端口和依赖方向必须为这些能力留出清晰扩展点。

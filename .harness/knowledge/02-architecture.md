<!-- SUMMARY: Agent MVP 与后续完整 Agent 能力的 DDD 架构边界、依赖方向和核心模块原则 -->
# 架构与模块边界

## 架构定位

本项目的 Agent MVP 是后续持续迭代到完整 Agent 能力的架构基线，不是一次性 demo。MVP 只实现当前验收范围，但领域模型、端口和模块边界必须支持后续扩展 Provider、工具生态、长期 Memory、权限审批、多 Agent、自动化任务、可观测性和部署运行环境。

## 分层

项目严格遵循领域驱动设计 DDD，采用外层依赖内层的方向：Interfaces -> Application -> Domain。Infrastructure 只实现 Domain 定义的端口，并在应用启动时注入。

- Domain 层：定义 Agent、Session、Message、Tool、Provider、Memory 等核心领域模型和值对象，定义 LLMProvider、ToolExecutor、MemoryStore、Summarizer 等端口协议。
- Application 层：编排用例和 Agent Runtime。LangGraph 属于本层，只负责状态图和运行流程编排。
- Infrastructure 层：实现外部依赖细节，包括 OpenAI-compatible Provider、SQLite store、内置工具 handler、配置加载等。
- Interfaces 层：实现 FastAPI、OpenAI-compatible API、Dashboard 和协议转换。

## 模块边界

- Domain 不依赖 FastAPI、LangGraph、SQLite、OpenAI SDK 或任何 Infrastructure 具体实现。
- Application 依赖 Domain 端口和 LangGraph，不 import Infrastructure 具体类。
- Infrastructure 可以依赖 Domain 端口并实现它们，不能反向要求 Domain 了解具体存储、模型 SDK 或工具 handler。
- Interfaces 只调用 Application 用例，只做请求/响应、SSE、错误映射和 Dashboard 协议转换。
- Interfaces 不直接访问 SQLite，不直接执行工具 handler，不承载工具权限、Agent Loop、摘要策略等业务规则。
- 应用启动入口负责依赖组装，将 Infrastructure 实现注入 Application 服务。

## 核心模块

- Agent Runtime：内部运行机制，负责加载上下文、调用 LLM、执行工具、更新 Memory、判断结束条件。运行流程可使用 LangGraph 表达，但领域状态和规则不能被 LangGraph 类型污染。
- LLM Adapter：模型 Provider 端口，屏蔽 OpenAI-compatible、Claude、Ollama、OpenRouter 等 Provider 差异。
- Tool Registry：工具定义、schema、风险等级、权限要求和执行入口。Agent 实际可执行工具只来自服务端注册表。
- Memory/Context：通过 MemoryStore 与 Summarizer 端口访问，会话、消息、工具调用、任务状态和摘要的持久化细节属于 Infrastructure。
- OpenAI-compatible API：对外兼容 Open-WebUI 的协议层，不等同于内部 Agent 模型。
- Chat Dashboard：调试和演示入口，查看会话、流式输出、工具调用、摘要和任务状态，不替代 Open-WebUI。

## 演进边界

后续完整 Agent 能力应在现有边界内迭代，不推倒重来。优先演进方向包括：

1. 受控 Shell、文件写入、patch、审批流和 workspace 安全。
2. Toolset、MCP、工具可用性检查、schema sanitizer、工具输出限流和工具结果持久化。
3. Context compressor、session search、长期 Memory、摘要压缩和任务恢复。
4. 多 Provider、Provider fallback、模型能力检测、usage/cost 统计。
5. CLI/TUI 和更完整 Dashboard。
6. Cron、后台任务、通知投递和失败重试。
7. Skills、自我改进 loop、用户画像和经验沉淀。
8. 子 Agent、并行执行、delegate 和结果汇总。
9. 多平台 Messaging Gateway、远程运行环境和部署安装体系。

本次 MVP 实现计划不得提前实现上述完整能力；只保留清晰扩展点。
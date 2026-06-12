# N-Agent


## 关键设计
- 框架：DDD分层，DockerCompose部署；Python+uvicorn/FastAPI/LangGraph库，SQLite
- 功能：Agent Loop，LLM Adapter，Tool Registry，Memory/Context，Chat Dashboard
- 交互：支持OpenAI-Compatible风格Http API，流式
- 模型：LLM Adapter，支持多Provider
- 知识：LlamaIndex(rag)，Qdrant(vdb)，Ollama+BGE-M3(embedding)
- 工具：支持tool-calling，支持可插拔工具框架，safe默认暴露、dangerous不暴露，预留权限和风控接口
- 记忆：SQLite 持久化 session、message、tool call、task state、summary，支持长任务上下文续跑


## 实施要点
- 模型：System提示词定义身份
- 接口：Models接口不暴露底层真实模型名，统一展示N-Agent


## 架构设计
参见DDD领域图[docs/ddd-domain-model.md](docs/ddd-domain-model.md)。


## 迭代规划
- 工具：开发能力 补齐受控 Shell、文件写入、patch、代码搜索、浏览器/Web 搜索和审批流
- 工具：生态扩展 Toolset、MCP、工具可用性检查、schema sanitizer、工具输出限流和结果持久化
- 记忆：补齐上下文压缩、session search、长期记忆、摘要压缩和任务恢复
- 模型：扩展多 Provider，fallback，模型能力检测
- 交互：完善 CLI/TUI/Dashboard、cron、后台任务、通知投递和失败重试
- 观测：usage/cost、日志、doctor 和运行状态观测
- 演进：逐步引入 Skills、自我改进、多 Agent/并行执行、多平台入口和远程运行环境


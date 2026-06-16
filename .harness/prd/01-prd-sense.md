<!-- SUMMARY: N-Agent 的产品定位、目标用户、体验原则和范围判断准则 -->
# 01-prd-sense.md -- N-Agent

产品设计核心理念与判断准则，供 AI Agent 功能决策时参照。

---

## 产品定位

N-Agent 是一款类似Hermes的Agent产品。

## 产品愿景

让个人开发者能在本地以低成本运行、观察和逐步扩展一个具备工具调用、记忆和可演进架构边界的 Agent 服务。

---

## 核心用户

| 角色 | 诉求 | 优先级 |
|------|------|--------|
| 本地开发者 | 通过 Open-WebUI、curl 或 Dashboard 快速验证 Agent 对话、工具调用和记忆链路 | P0 |
| Agent 能力验证者 | 观察 session、summary、task state、tool calls 等运行状态，定位 Agent 行为问题 | P0 |
| 后续扩展开发者 | 在清晰 DDD 边界内扩展 Provider、工具、Memory、权限和多入口能力 | P1 |

核心场景是本地运行一个兼容 OpenAI Chat Completions 协议的 Agent 服务，并通过 `/chat` Dashboard 调试会话、工具调用和记忆状态。

---

## 体验原则

- 协议兼容优先：对外优先兼容 OpenAI-compatible API，降低 Open-WebUI 和现有客户端接入成本。
- 本地可观察：Dashboard 必须能帮助开发者观察会话、摘要、任务状态和工具调用，不替代 Open-WebUI。
- 工具安全默认：服务端 Tool Registry 决定可执行工具，文件工具限制在 workspace 内，危险能力默认不暴露。
- 架构可演进：当前阶段只实现既定闭环，但 Domain 端口、Application 编排和 Infrastructure 实现必须便于后续扩展。
- 最小可用闭环：优先保证对话、工具、记忆、流式响应和部署链路可运行，再增加复杂能力。

---

## 产品判断准则

按优先级排序：

1. OpenAI-compatible 对话链路是否能稳定服务 Open-WebUI、curl 和 Dashboard。
2. Agent Runtime 是否保持清晰边界，避免 Provider、SQLite、FastAPI 或 LangGraph 细节污染 Domain。
3. 工具调用、会话持久化和调试信息是否可观察、可追踪、可恢复。
4. 新能力是否符合当前阶段范围，是否只保留扩展点而非提前实现完整平台能力。

优先级低的未来能力即使技术可行，也应推迟到对应迭代，避免破坏当前阶段的简洁性和可验证性。

---

## 不做什么

- 不在当前阶段提前实现完整多 Agent 编排、后台任务平台、Cron、远程运行环境或消息网关。
- 不让 Dashboard 替代 Open-WebUI 或承载生产级权限系统。
- 不让客户端传入的 tools 绕过服务端 Tool Registry 和风险等级控制。
- 不把系统提示词、Provider SDK 对象、SQLite row 或 FastAPI 请求对象作为内部领域模型。

---

## 维护

当产品定位、目标用户、范围、Dashboard 定位或 Agent 能力优先级发生变化时更新本文件；实现事实变化先回填 knowledge，再由人工确认是否调整 PRD。
# N-Agent

N-Agent 是一套 Python Agent Runtime，面向 Open-WebUI、本地 CLI、Dashboard 和外部消息平台。它通过 FastAPI 提供 OpenAI-compatible API，使用 LangGraph 编排 Agent TurnLoop，并按 DDD 管理模型、上下文、工具、记忆及扩展能力。

## 关键能力

- 对话：支持 `/v1/chat/completions` 同步与 SSE 流式输出。
- 模型：通过 `LLMProvider` 端口适配 OpenAI-compatible、Anthropic 等 Provider，并支持运行时切换。
- 上下文与记忆：组装 Provider 可见的消息和工具，使用 SQLite 保存会话，支持上下文压缩与外部记忆。
- 工具：统一管理 Tool、Knowledge、MCP、Plugin、Skill，并通过 `ToolPolicy` 控制暴露、审批和执行。
- 沙盒：在受控环境中提供 `execute_code` 和 `terminal`。
- 自动化与入口：支持 Schedule、CLI/TUI、飞书 Gateway、ACP 和 Dashboard。

## DDD 架构

```text
核心子域：TurnLoop / Context / LLM / Memory / Tool
支撑子域：Knowledge / MCP / Plugin / Skill / Sandbox / Schedule
          Gateway / Platform / Usage & Observation
共享内核：Policy 决策契约
外部边界：Storage / Model Provider
```

依赖方向：

```text
Interfaces -> Application -> Domain
Infrastructure -----------> Domain
```

- Domain 定义领域模型、值对象、策略和端口，不依赖框架或具体实现。
- Application 编排对话用例、TurnLoop 和各子域服务；LangGraph 仅是运行时实现。
- Infrastructure 实现 Provider、SQLite、HTTP/MCP、工具和沙盒等适配器。
- Interfaces 提供 OpenAI-compatible API、Dashboard、CLI、ACP 和平台协议适配。

核心流程：

```text
Interface / Gateway
  -> ChatCompletionService
  -> AgentGraphRunner.prepare_context
  -> ContextService 组装 ProviderContext(messages, tools)
  -> LLM 调用 <-> ToolService 受控执行
  -> Memory / Usage / External Memory 更新
  -> JSON、SSE 或入口协议响应
```

## 快速运行

要求 Python 3.11+；推荐使用 Docker Compose：

```bash
cd docker
docker compose up --build
```

服务默认监听 `http://localhost:8201`，Dashboard 位于 `/chat/`。

常用配置使用 `N_AGENT_` 前缀：

```env
N_AGENT_PROVIDER_BASE_URL=<openai-compatible-base-url>
N_AGENT_PROVIDER_API_KEY=<api-key>
N_AGENT_PROVIDER_MODEL=<model>
N_AGENT_SQLITE_PATH=/app/locals/sessions.db
N_AGENT_WORKSPACE_ROOT=/workspace
```

## CLI

`n-agent` 是命令行入口。管理命令既可单次执行，也可在 Chat REPL 中以对应 slash 命令使用；详细参数以 `--help` 为准。

```bash
n-agent chat "你好"               # 单次流式对话
n-agent chat                      # 进入 REPL
n-agent provider list --form      # 单次管理命令
```

REPL 示例：`/provider list`、`/knowledge list`、`/mcp list`、`/schedule list`、`/sandbox list-active`、`/skill list`、`/plugin list`。本地控制命令包括 `/help`、`/clear`、`/history`、`/confirm`、`/cancel` 和 `/exit`。

## ACP 远程接入

N-Agent 内置 ACP stdio 服务端。Docker 中的客户端配置示例：

```json
{
  "command": "docker",
  "args": ["exec", "-i", "n-agent-n-agent-1", "n-agent", "acp"]
}
```

Kubernetes 中可改为：

```json
{
  "command": "kubectl",
  "args": ["exec", "-i", "pod/<pod-name>", "--", "n-agent", "acp"]
}
```

容器部署需配置工作区映射：

```env
N_AGENT_ACP_HOST_WORKSPACE_ROOT=/Users/<user>/projects
N_AGENT_ACP_CONTAINER_WORKSPACE_ROOT=/workspace
```

映射规则：宿主路径替换为容器路径；容器路径原样使用；空 cwd 使用容器根目录；不可映射时拒绝创建 session。容器名通过 `docker compose ps` 获取，Pod 名通过 `kubectl get pods -l app=n-agent` 获取。

```bash
docker exec -it n-agent-n-agent-1 n-agent acp --check   # 检查 ACP 依赖
docker exec -it n-agent-n-agent-1 n-agent acp --setup   # 查看 Provider 配置提示
```

ACP stdout 仅承载 JSON-RPC，日志与诊断写入 stderr。

## 项目文档

- [DDD 领域模型](.harness/knowledge/06-domain-model.md)
- [架构边界](.harness/knowledge/02-architecture.md)
- [数据边界](.harness/knowledge/04-data-boundaries.md)
- [关键实现模式](.harness/knowledge/05-key-patterns.md)
- [源码文件映射](.harness/knowledge/22-file-map.md)

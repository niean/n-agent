# N-Agent

N-Agent 是面向 Open-WebUI、本地调试和多平台入口的 Python Agent Runtime。它通过 FastAPI 提供 OpenAI-compatible API，内部用 LangGraph 编排 Agent Loop，并按 DDD 分层维护模型、工具、记忆、知识库、MCP、Skill、平台网关和沙盒能力。

## 关键能力

- 对话：`/v1/chat/completions` 支持同步与 SSE 流式输出。
- 模型：`LLMProvider` 端口屏蔽 Provider 差异，Dashboard 支持多 Provider CRUD 与 active 热切换。
- 工具：服务端 Tool Registry 管理工具定义、来源、toolset、风险等级和授权上下文。
- 记忆：SQLite 持久化 session、message、tool call、task state、summary；外部记忆按 builtin、multi-project、external-query 三槽管理。
- 知识：`search_knowledge` 通过 Knowledge SPI 检索已注册 N-KB/Ragflow 后端，`kb_id` 必填。
- MCP：MCP site 注册、探测、刷新，并将远端工具同步为本地动态工具定义。
- Skill：本地 `SKILL.md` 包扫描、启停、查看，并通过 `skills_list` / `skill_view` 暴露给 LLM。
- Sandbox：`execute_code` 是 SAFE 工具，无确认卡片；安全边界由 Docker/Local sandbox、workspace 只读、scratch 可写和 callback allowlist 保证。
- Platform/Gateway：CLI、飞书等入口统一映射为 Gateway 会话，再复用 Chat Runtime。
- Dashboard：提供对话、会话、记忆、工具、沙盒、模型、任务、平台和健康视图。

## DDD 分层

```text
Interfaces -> Application -> Domain
Infrastructure -> Domain
```

- Domain：定义 Agent、Session、Tool、Provider、Memory、Knowledge、MCP、Skill、Platform/Gateway、Sandbox 等模型和值对象，以及端口协议。
- Application：编排 Chat、Agent Loop、Prompt、工具调度、Provider 管理、Knowledge/MCP/Skill/Memory/Sandbox 用例。
- Infrastructure：实现 OpenAI-compatible Provider、SQLite registry/store、工具 handler、HTTP/MCP/Feishu/Sandbox adapter。
- Interfaces：提供 FastAPI、OpenAI-compatible API、Dashboard、CLI 和平台协议适配。

Domain 不依赖 FastAPI、LangGraph、SQLite、OpenAI SDK 或具体工具实现；LangGraph 只是 Application 层的 Runtime Loop 实现细节。

## 核心流程

```text
OpenAI API / Dashboard / CLI / Gateway
  -> ChatCompletionService
  -> AgentGraphRunner(load_context -> call_llm -> execute_tools -> update_memory -> finalize)
  -> LLMProvider / ToolService / MemoryStore
  -> ChatCompletion 或 SSE 事件
```

## 运行

Docker Compose 是默认运行方式，服务端口为 `8201`。

```bash
cd docker
docker compose up --build
```

常用配置使用 `N_AGENT_` 前缀：

```env
N_AGENT_PROVIDER_BASE_URL=<openai-compatible-base-url>
N_AGENT_PROVIDER_API_KEY=<api-key>
N_AGENT_PROVIDER_MODEL=<model>
N_AGENT_SQLITE_PATH=/app/locals/sessions.db
N_AGENT_WORKSPACE_ROOT=/workspace
```

## CLI

`n-agent` 是命令行入口。所有功能命令均可在 Chat REPL 中以 slash 命令（`/cmd`）使用；单次执行命令（`n-agent <cmd>`，如 `n-agent provider list`、`n-agent skill list`）与 REPL slash 命令一一对应，仅作为 shell 自动化/脚本场景的快捷入口。本文档仅介绍 chat slash cmd，单次执行命令的参数与 slash 命令完全一致。

### 单次消息

```bash
n-agent chat "你好"                      # 流式输出回复
n-agent chat "你好" --no-stream          # 非流式整体输出
```

### REPL交互

```bash
n-agent chat                            # 进入 > prompt
```

REPL 内支持 Slash 命令，Tab 补全命令名，Ctrl+D / Ctrl+C 退出。

本地命令：

| 命令 | 说明 |
|------|------|
| /help | 显示命令帮助 |
| /exit | 退出 REPL |
| /clear | 清屏 |
| /history | 显示历史文件路径 |
| /confirm once | 确认上一个破坏性命令（仅一次） |
| /confirm trust | 信任当前会话，后续破坏性命令不再确认 |
| /cancel | 取消上一个破坏性命令 |

Gateway 命令（转发到 gateway 处理）：

| 命令 | 说明 |
|------|------|
| /new | 新建会话（需 /confirm） |
| /rename | 重命名会话 |
| /delete | 删除会话（需 /confirm） |
| /switch | 切换会话 |
| /sethome | 设置主会话 |
| /tools | 列出工具 |
| /models | 列出模型 |

Management 命令（本地直接调用 service）：

| 命令 | 说明 |
|------|------|
| /provider list\|get\|create\|update\|delete\|activate | Provider 管理 |
| /knowledge list\|get\|create\|update\|delete\|probe | 知识库管理 |
| /mcp list\|get\|create\|update\|delete\|probe\|refresh\|tools\|toggle | MCP 站点管理 |
| /schedule list\|get\|create\|update\|pause\|resume\|run\|delete\|executions | 定时任务管理 |
| /sandbox list-active\|list-released\|list-history\|release\|delete-history\|config | 沙盒管理 |
| /memory list-providers\|create-provider\|activate-provider\|... | 外部记忆管理（见 --help） |
| /platform list\|get\|sessions | 平台管理 |
| /skill list\|view \<name\> | Skill 查看 |
| /plugin list\|view \<name\> | Plugin 查看 |
| /status | 本地健康快照 |
| /sessions [--browse [--pick \<id\>]] | 列出当前会话的 sessions |
| /doctor [--probe] | 健康检查 |
| /config [--section] | 运行配置（脱敏） |
| /logs sandbox\|tools\|scheduled\|runs | 日志查询 |

任何 management 命令追加 `--help` 查看详细参数（如 `/provider create --help`）。

### 输出格式

所有 management/查询命令默认输出 JSON，可通过 flag 切换：

| Flag | 说明 |
|------|------|
| (默认) | JSON 格式，适合脚本解析 |
| `--json` | 显式 JSON（与默认一致，向后兼容） |
| `--form` | 人类可读形式：表格(list) / key-value(dict) / markdown(skill view) / doctor report |
| `--yaml` | YAML 格式 |

```bash
n-agent provider list              # JSON 数组
n-agent provider list --form       # 表格
n-agent provider list --yaml       # YAML
n-agent provider get p1 --form     # key-value 详情
n-agent doctor --form              # 彩色状态表
```

例外（不适用格式 flag）：

- `chat`：流式输出，格式 flag 不适用
- `sessions --browse`：交互式 picker，格式 flag 不适用（非交互模式仍可用）

例外（REPL 不支持，需从 shell 执行）：

- `/sessions --browse` 交互式 picker 与 REPL 的 prompt_toolkit 会话冲突，REPL 中强制降级为表格输出；如需 picker，从 shell 执行 `n-agent sessions --browse`。
- `/chat` 即 REPL 本身，递归无意义。

## ACP 远程接入

N-Agent 内置 ACP（Agent Client Protocol）stdio 服务端，支持 VsCode/Zed 等 ACP 兼容客户端通过 `docker exec` 或 `kubectl exec` 接入容器内的 Agent。容器内运行 `n-agent acp`，stdout 承载 JSON-RPC 帧，所有日志走 stderr。

### VsCode ACP Client 配置（Docker）

```json
{
  "command": "docker",
  "args": ["exec", "-i", "n-agent-n-agent-1", "n-agent", "acp"]
}
```

容器名 `n-agent-n-agent-1` 由 compose project `n-agent` + service `n-agent` + replica `1` 拼接，实际名称可用 `docker compose ps` 确认。

### VsCode ACP Client 配置（K8s）

```json
{
  "command": "kubectl",
  "args": ["exec", "-i", "pod/n-agent-xxxx-yyyy", "--", "n-agent", "acp"]
}
```

Pod 名由 K8s 动态生成（Deployment + ReplicaSet + Pod hash），部署后用 `kubectl get pods -l app=n-agent` 查询实际名称并填入。生产环境建议用 ServiceAccount + 自动注入脚本生成客户端配置，避免手工维护 Pod 名。

### 路径映射

ACP cwd 来自宿主/editor，N-Agent 文件工具运行在容器/Pod。容器部署必须配置两个环境变量（写入 docker-compose `environment` 或 K8s Pod env）：

```env
N_AGENT_ACP_HOST_WORKSPACE_ROOT=/Users/<user>/projects
N_AGENT_ACP_CONTAINER_WORKSPACE_ROOT=/workspace
```

映射规则：

1. cwd 在 host root 下时，替换前缀为 container root。
2. cwd 已在 container root 下时原样使用。
3. cwd 为空时使用 container root。
4. cwd 不可映射时 `session/new` 拒绝并返回协议错误，不回退到 `Path.cwd()`。

未配置 host root 时，所有宿主 cwd 都不可映射，session/new 会拒绝。开发环境通常将 host root 设为宿主项目目录、container root 设为容器内挂载点（与 docker-compose volumes 挂载源一致）。

### 辅助命令

```bash
docker exec -it n-agent-n-agent-1 n-agent acp --check    # 验证 ACP 依赖可导入
docker exec -it n-agent-n-agent-1 n-agent acp --setup    # 输出 provider 配置提示
```

`--setup` 不进入 JSON-RPC 主循环，仅向 stderr 输出 provider 创建/激活步骤。首次部署未配置 Provider 时用此命令查看引导。

## 文档

- DDD 领域模型：[.harness/knowledge/06-domain-model.md](.harness/knowledge/06-domain-model.md)
- 架构边界：[.harness/knowledge/02-architecture.md](.harness/knowledge/02-architecture.md)
- 数据边界：[.harness/knowledge/04-data-boundaries.md](.harness/knowledge/04-data-boundaries.md)
- 关键模式：[.harness/knowledge/05-key-patterns.md](.harness/knowledge/05-key-patterns.md)
- 文件映射：[.harness/knowledge/22-file-map.md](.harness/knowledge/22-file-map.md)

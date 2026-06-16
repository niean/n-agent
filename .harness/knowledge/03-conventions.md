<!-- SUMMARY: N-Agent 的实现约定，包括 Python/DDD 分层、测试、错误处理、安全、Docker Compose 和文件管理规则 -->
# 约定与约束（实现细节）

本文件是项目实现规范约定的权威来源，`.harness/PROJECT.md` "项目规范"各节为摘要引用，以本文件为准。

---

# 一、编码约定

## Python 与包结构

- Python 版本：3.11+
- 应用包：`app/`
- 测试包：`tests/`
- 源码按 DDD 分层目录组织：`domain/`、`application/`、`infrastructure/`、`interfaces/`
- `app/main.py` 是唯一负责组装 Infrastructure 具体实现的位置
- 新增业务能力时优先在 Domain 定义模型/端口，再由 Application 编排，最后由 Infrastructure/Interfaces 实现外部细节

## DDD 依赖方向

- Domain 层禁止 import FastAPI、LangGraph、SQLite、OpenAI SDK 或 `app.infrastructure`
- Application 层禁止 import `app.infrastructure`
- Interfaces 层禁止 import SQLite 和任何 `app.infrastructure` 模块
- Infrastructure 可以 import Domain 端口并实现它们
- Interfaces 只调用 Application services，不直接执行工具 handler，不直接访问 SQLite
- DDD 边界由 `tests/test_architecture_boundaries.py` 静态测试守护

## OpenAI-compatible API

- `/v1/chat/completions` 只作为外部协议适配，不作为内部领域模型
- 请求字段可接受 OpenAI Chat Completions 常见字段；未知字段可忽略或进入 provider options
- 流式输出必须为 `text/event-stream`，使用 `data: {...}` chunk，并以 `data: [DONE]` 结束
- 文本增量输出到 `choices[0].delta.content`
- 工具调用增量输出到 `choices[0].delta.tool_calls`
- 工具执行结果不通过 OpenAI SSE side-channel 暴露，写入内部消息和 tool_calls 后进入下一轮 LLM 输入

## LangGraph

- LangGraph 只存在于 Application 层 `app/application/agent_graph.py`
- 图节点至少包含：`load_context`、`call_llm`、`execute_tools`、`update_memory`、`finalize`
- Application 层将运行结果转换为 `ChatEvent`，Interfaces 层只负责 SSE/JSON 编码
- Domain 模型不得暴露 LangGraph 类型

## 工具体系

- Agent 实际可执行工具只来自服务端 Tool Registry
- `ToolDefinition` 不包含 handler，handler 属于 Infrastructure
- `safe` 工具默认允许执行
- `confirm` 工具默认拒绝自动执行，返回 `permission_denied`
- `dangerous` 工具默认不暴露给 LLM，也不可自动执行
- 内置 safe 工具：`get_current_time`、`calculator`、`list_directory`、`read_text_file`
- 文件工具必须通过真实路径解析限制在 workspace 根目录内，拒绝路径穿越和软链接逃逸
- calculator 使用 AST 白名单，只允许安全算术表达式

---

# 二、配置约定

## 环境变量

配置模型位于 `app/config.py`，使用 `N_AGENT_` 前缀：

- `N_AGENT_PROVIDER_BASE_URL`
- `N_AGENT_PROVIDER_API_KEY`
- `N_AGENT_PROVIDER_MODEL`
- `N_AGENT_SQLITE_PATH`
- `N_AGENT_WORKSPACE_ROOT`
- `N_AGENT_AGENT_ITERATION_LIMIT`

Docker Compose 项目隔离使用：

- `COMPOSE_PROJECT_NAME=n-agent`

## Docker Compose 默认值

只考虑 Docker Compose 运行时，推荐容器内路径：

- `N_AGENT_SQLITE_PATH=/app/locals/sessions.db`
- `N_AGENT_WORKSPACE_ROOT=/workspace`

宿主机目录通过 `docker/docker-compose.yml` volume 映射到容器路径，避免容器内状态丢失。
本项目面向 Docker Desktop 本地访问时使用端口映射 `8201:8201`，不使用 `network_mode: host`。

## 密钥

- `.env` 可存放本地真实 Provider API Key，但不得提交
- `.env.example` 只保留占位值或空值，不写真实密钥
- 任何日志、测试和文档都不得输出真实 API Key

---

# 三、质量约定

## 测试

- 测试命令：`python -m pytest -v`
- 当前全量测试覆盖 33 项
- 新增 Domain、Application、Infrastructure、Interfaces 能力时必须补对应测试
- 涉及 DDD 边界变更时必须运行 `tests/test_architecture_boundaries.py`
- 涉及 Docker Compose 变更时必须运行 `tests/test_docker_compose_config.py` 和 `docker compose config`

## 错误处理

- Provider 调用失败：非流式返回 OpenAI-compatible error payload；流式输出 error chunk 后发送 `[DONE]`
- 工具不存在：返回 tool error 结果，允许 Agent 尝试恢复
- 权限拒绝：返回 `permission_denied`，不执行 handler
- 迭代上限：Agent finalize，并写入 task_state last_error
- SQLite 写入失败：当前请求失败，不静默丢状态

## 验收命令

```bash
python -m pytest -v
docker compose -f docker/docker-compose.yml config >/dev/null
curl http://127.0.0.1:8201/health
curl http://127.0.0.1:8201/v1/models
```

---

# 四、文件管理约定

- 不主动创建 README，除非用户明确要求
- 不自主删除项目文件
- `locals/`、`logs/`、`data/`、`.pytest_cache/`、`*.pyc`、`*.egg-info/` 是本地运行、测试或构建产物，应由 `.gitignore` 忽略，不需要提交
- `docker/restart-nagent.sh` 是本地 Docker Compose 重建辅助脚本
- `docker/docker-compose.yml` 当前在 `.gitignore` 中，修改部署配置前需确认提交边界
- `.harness/prd/` 是 AI-READONLY，不能自动修改
- `.harness/knowledge/` 是实现后知识回填目标，可按 Harness 流程更新

---

# 五、安全约定

- 文件工具默认只能访问配置的 workspace 根目录
- Shell、写文件、patch、网络抓取等工具不属于默认工具集
- Docker Compose 挂载的 workspace 是文件工具边界，不应挂载过大的敏感目录
- Provider API Key 只通过环境变量注入，不写入镜像
- Docker build 可使用国内 PyPI 镜像加速，但不得在镜像层写入密钥

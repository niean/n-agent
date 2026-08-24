# PROJECT.md -- N-Agent

N-Agent 是一款类 Hermes 的本地 Agent 产品，让个人开发者以低成本运行、观察和逐步扩展具备工具调用、记忆与可演进 DDD 架构边界的 Agent 服务。

---

# Harness 框架适配

本节为 Harness 框架提供项目级配置，框架文件通过 `.harness/PROJECT.md` 直接引用。

## 知识库目录

首次加载时需建立 SUMMARY 索引的目录：
- `.harness/knowledge/`
- `.harness/prd/`（除 .harness/prd/03-prd-specs.md）
- `.harness/lessons/`

## 任务类型加载矩阵

首次加载时，根据任务类型选择性读取知识库文件（所有文件首行 SUMMARY 始终必读）：

| 任务类型 | 必读（完整读取） | 按需读取 |
|---------|----------------|---------|
| 功能需求 | .harness/knowledge/01-overview.md, .harness/knowledge/02-architecture.md, .harness/knowledge/22-file-map.md, .harness/prd/01-prd-sense.md, .harness/prd/02-prd-baseline.md | .harness/knowledge/03-conventions.md, .harness/knowledge/04-data-boundaries.md, .harness/knowledge/05-key-patterns.md, .harness/knowledge/21-glossary.md |
| 功能精调 | .harness/knowledge/01-overview.md, .harness/knowledge/22-file-map.md | .harness/knowledge/02-architecture.md, .harness/knowledge/03-conventions.md, .harness/knowledge/04-data-boundaries.md, .harness/knowledge/05-key-patterns.md, .harness/knowledge/21-glossary.md |
| Bug修复 | .harness/knowledge/01-overview.md, .harness/knowledge/03-conventions.md, .harness/knowledge/22-file-map.md | .harness/knowledge/02-architecture.md, .harness/knowledge/04-data-boundaries.md, .harness/knowledge/05-key-patterns.md, .harness/knowledge/21-glossary.md |
| 治理/扫描 | .harness/knowledge/01-overview.md, .harness/knowledge/03-conventions.md, .harness/knowledge/22-file-map.md | .harness/knowledge/02-architecture.md, .harness/knowledge/05-key-patterns.md |
| 文档维护 | .harness/knowledge/01-overview.md, .harness/knowledge/22-file-map.md | 读取目标文件引用链上的 knowledge/ 和 prd/ 文件 |

## 知识回填文件映射

知识回填的回填目标：
- 架构变化 -> .harness/knowledge/02-architecture.md
- 新术语 -> .harness/knowledge/21-glossary.md
- 数据结构/存储变化 -> .harness/knowledge/04-data-boundaries.md
- 新源文件 -> .harness/knowledge/22-file-map.md
- 新跨文件模式 -> .harness/knowledge/05-key-patterns.md
- 产品方向调整 -> 提示用户，人工更新 .harness/prd/01-prd-sense.md

## 教训库加载路径

本项目教训库分布在两个位置：
- `.harness/framework/lessons/general.md`（Harness 通用教训）
- `.harness/lessons/project.md`（项目教训）

## 构建与测试

### 构建
```bash
# N-Agent 为 Python 应用，无独立构建步骤；开发态从 pyproject.toml 安装依赖
python -m pip install -e .[dev]
# 服务构建/启动由 Docker Compose 一键完成
sh docker/restart.sh
```

### 单元测试
单元测试执行策略：
- 用户明确要求时：必须执行
- 标准化单元测试允许直接在宿主开发环境执行
- 其他场景：跳过

```bash
cd /Users/niean/code/github.com/niean/n-agent
python -m pytest
```

### E2E 测试
E2E 测试执行策略：
- 所有 E2E 测试必须在 Docker 环境进行，禁止在宿主机直接执行
- 除标准化单元测试 `python -m pytest` 外，禁止在宿主机直接执行 N-Agent Python 代码
- 禁止在宿主机直接启动 N-Agent 服务
- E2E 测试前必须通过 `docker/restart.sh` 重建并启动服务，后续直接通过 `tests/e2e/run.sh` 全部 CLI E2E测试

```bash
cd /Users/niean/code/github.com/niean/n-agent 
# 重建并启动服务
sh docker/restart.sh
# 全部 CLI E2E
sh tests/e2e/run.sh
```

## 扫描维度

代码扫描使用的维度及规则来源。下表路径均相对于 `.harness/knowledge/` 目录：

| # | 维度 | 规则来源 |
|---|------|---------|
| 1 | DDD 依赖方向（application/domain/infrastructure/interfaces/utils 严格单向） | .harness/knowledge/03-conventions.md "DDD 依赖方向" |
| 2 | 任务安全策略分类（A/B/C 三类） | .harness/knowledge/03-conventions.md "任务安全策略分类" |
| 3 | 测试规范（TDD 严格 S1-S5、单测/E2E 边界、覆盖率） | .harness/knowledge/03-conventions.md "测试" |
| 4 | Dashboard 前端（modal/alert/按钮/菜单/时间渲染） | .harness/knowledge/03-conventions.md "Dashboard 前端" |
| 5 | 错误处理（业务/系统/校验三类异常、统一响应格式） | .harness/knowledge/03-conventions.md "错误处理" |

可选（涉及文件删除时）：

| # | 维度 | 规则来源 |
|---|------|---------|
| 1 | 文件删除治理（禁止自主删除，治理/升级场景需用户确认） | .harness/framework/FRAMEWORK.md "文件与文档" |

## 项目知识索引

| 文件 | 何时查阅 |
|------|---------|
| .harness/prd/01-prd-sense.md | 功能迭代前，确认产品定位和判断准则 |
| .harness/knowledge/01-overview.md | 任务开始时，了解项目概览（技术栈/入口/核心流程） |
| .harness/knowledge/02-architecture.md | 修改/扩展 DDD 子域、添加新工具/策略或新增委派/Artifact/Browser 等子域时 |
| .harness/knowledge/03-conventions.md | 代码风格、任务安全分类、测试/错误处理约定、Dashboard 前端规范不清楚时 |
| .harness/knowledge/04-data-boundaries.md | 新增/修改 SQLite schema、配置项、协议字段、localStorage 键、Delegation 7 表结构时 |
| .harness/knowledge/05-key-patterns.md | 复用既有跨文件模式（DDD 边界、ToolPolicy 审批、Skill 自进化、Artifact 写穿/发布/删除、Activated Skills 三层过滤、多 Agent 委派）时 |
| .harness/knowledge/06-domain-model.md | 需要快速了解 DDD 业务架构、子域、核心流程、关键模型和外部边界时 |
| .harness/knowledge/21-glossary.md | 对术语不清楚时 |
| .harness/knowledge/22-file-map.md | 确定功能对应源文件时 |
| .harness/prd/02-prd-baseline.md | 确认功能需求与产品约束时 |
| .harness/lessons/project.md | 用户指令或当前根因与 SUMMARY 高度相关时按需读取 |

---

# 项目规范

## 代码生成

以下各节（代码生成、架构边界、质量守护、安全规范）为快速参考摘要，权威定义见 .harness/knowledge/03-conventions.md。

- Python 分层：application/domain/infrastructure/interfaces/utils 严格单向依赖，详见 02-architecture
- 错误处理：异常分业务/系统/校验三类，统一错误响应格式
- 测试：TDD 严格顺序 S1→S5（spec/红/实现/绿/重构），单测宿主机、E2E 必须在 Docker
- 文件管理：禁止创建 docs/ 目录；新建文档放 .harness/knowledge/ 或 specs/plans
- Dashboard 前端：禁止原生 alert/confirm；modal/按钮/菜单/时间渲染统一规范

## 架构边界

- DDD 边界与依赖方向（17 个领域 Policy）：application 编排用例、domain 模型、infrastructure 适配、interfaces 协议，详见 02-architecture "DDD 依赖方向"
- ToolPolicy 审批：飞书/CLI 等工具必须经过 Policy 评估才能暴露给 LLM
- Host Terminal 宿主执行边界：宿主侧密钥与只读权威文件由 Host Terminal 子域隔离
- Skill 自进化写入治理：provenance 记录每次写入的来源，避免无审计变更
- Activated Skills 选择流：前端按会话持久化 + chat_service 归一 + context_service 实时求交 + prompt_builder 注入 ## Activated Skills + _INTERNAL_OPTION_KEYS 隔离 Provider
- 多 Agent 委派：禁止递归、单一层级；capability 防伪造 + delegation- 前缀 child 会话隔离 + 父级预算 reserve/ledger 恢复权威
- Artifact 制品工作台：write-through 持久化 + publish 封口 + 公私路由隔离 + delete 双向级联 task_attachment

## 质量守护

- TDD 严格 S1-S5 顺序，红绿重构闭环，RED 证据必须保留在 plan/verify
- 三方审阅不跳过：spec/plan Review Loop 后必须跑 Third Review 防止模型偏见
- 任务安全策略 A/B/C：A 类不变量只读禁配、B 类启动期 env-only、C 类运行时 Dashboard 可编辑+热重载
- 教训库沉淀：跨 2+ 源文件根因或实现与原假设偏离时自动写 .harness/lessons/project.md
- 技术债闭环：新引入技术债必须当场解决或登记 plans/debt-tracker.md，禁止拖延
- 知识回填：架构/术语/数据/文件/模式变化必须回填到 knowledge/ 对应章节

## 安全规范

- 任务安全策略按 A/B/C 三类管理：A 类安全不变量只读禁止配置、B 类启动期绑定 env-only、C 类运行时可配 Dashboard 编辑+热重载（权威定义见 .harness/knowledge/03-conventions.md "任务安全策略分类"）

---

# 项目附录

## 仓库结构

```
AGENTS.md              -- AI 入口（纯路由）
CLAUDE.md              -- Claude Code 入口
.harness/
  PROJECT.md           -- 项目规范入口（本文件）
  framework/           -- 通用能力（详见 FRAMEWORK.md "Framework 目录结构"）
  knowledge/           -- AI 知识库（01~05 认知约束类, 21~22 工具索引类）
  prd/                 -- 产品文档（AI只读：01-prd-sense、02-prd-baseline、03-prd-specs）
  lessons/
    project.md         -- 项目教训（AI自主维护）
  specs/               -- 设计文档
    active/
    completed/
    verify/            -- 人工端到端验收文件
  plans/               -- 实现计划
    active/
    completed/
    debt-tracker.md    -- 技术债追踪
app/                          -- Python 应用代码
  main.py                     -- FastAPI 入口
  config.py                   -- 全局配置（Pydantic Settings）
  browser_host_runtime.py     -- 浏览器宿主运行时
  application/                -- 应用服务层（用例编排）
  domain/                     -- 领域模型（DDD 聚合/实体/值对象/事件）
  infrastructure/             -- 基础设施适配
    artifact/                 -- Artifact 制品存储
    browser/                  -- 浏览器工具
    context/                  -- Context 装配
    feishu/                   -- 飞书适配
    host_terminal/            -- 宿主终端
    knowledge/                -- 知识库客户端（SPI）
    llm/                      -- LLM Provider（OpenAI/Anthropic/...）
    mcp/                      -- MCP 适配
    memory/                   -- 记忆（外部）
    plugin/                   -- 插件
    policy/                   -- 策略引擎（17 个领域 Policy）
    registry/                 -- 注册表
    sandbox/                  -- 沙箱
    schedule/                 -- 调度
    session/                  -- 会话
    skill/                    -- 技能（含 seeds）
    task/                     -- Task 引擎
    tools/                    -- 工具
    usage/                    -- 用量统计
  interfaces/                 -- 协议适配
    http/                     -- FastAPI 路由 + 静态资源（Dashboard）
    cli/                      -- CLI 命令
  utils/                      -- 通用工具
```

## 知识层级关系

```
Layer 0   AGENTS.md -> FRAMEWORK.md（通用规范+注册表） + PROJECT.md（项目配置+规则摘要）
Layer 1   framework/agents/（5个角色: Orchestrator/Designer/Planner/Coder/Reviewer）
Layer 1.5 framework/workflows/（迭代功能/修复Bug/迭代文档 + harness-ops/治理类）
Layer 2   framework/skills/（harness/ 核心Skill + harness-ops/ 运维Skill + superpowers/ 方法论）
Layer 3   framework/skills/harness/subskills/（扫描模板）
数据层    knowledge/（权威知识） + prd/（产品文档，AI只读） + guides/（方法论） + lessons/（教训）
辅助层    specs/（设计文档） + plans/（执行计划+技术债）
```

引用方向：Layer 0 -> Layer 1/1.5 -> Layer 2 -> Layer 3 -> 数据层。PROJECT.md 摘要引用 knowledge/03-conventions.md（权威源）。

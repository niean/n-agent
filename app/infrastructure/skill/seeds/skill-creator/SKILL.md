---
name: skill-creator
description: Guide creating Anthropic-format skills (创建 Anthropic 格式 Skill 的指引). Use when creating, editing, or evaluating skills via skill_manage.
metadata:
  tags: "skill,creation,guide"
  version: "1"
  source: "agent"
---

# Skill 创建指引 (skill-creator)

本 Skill 指导如何按 Anthropic Agent Skills 开放格式创建和管理 n-agent Skill。当需要通过 `skill_manage` 创建、编辑或评估 Skill 时，先加载本指引。

## 1. Anthropic Agent Skills 格式要点

每个 Skill 是一个目录，根目录必须有 `SKILL.md` 文件，使用 YAML frontmatter 声明元数据。

### frontmatter 字段

顶层只允许以下白名单字段：

- `name`: 必填，英文 kebab-case（如 `deploy-staging`），不超过 64 字符，必须与 Skill 目录名精确匹配
- `description`: 必填，英文第三人称说明"做什么 + 何时用"，并在括号内附中文 alias，不超过 1024 字符，不含尖括号 `<` `>`
- `license`: 可选，许可证标识
- `allowed-tools`: 可选，工具名列表或逗号分隔字符串
- `metadata`: 可选，扩展字段映射，key/value 均为 string
- `compatibility`: 可选，兼容性说明字符串，不超过 500 字符

扩展字段（如 `version`、`platforms`、`tags`、`author`、`related_skills`、`setup_help`、`required_env_vars`）不要放在顶层，必须下沉到 `metadata`，值为 string；list 用逗号分隔字符串表示。

### description 写法

description 决定 Skill 是否被选用，必须包含英文用途说明和括号中文 alias。

好：`Deploy to staging (部署到预发). Use when the user asks to release a build to the staging environment.`
坏：`部署到预发`（缺英文 what/when）；`Does X. Use when...`（缺中文 alias）

### 目录结构（progressive disclosure）

```
my-skill/
  SKILL.md          # 入口，简洁可读，不超过 500 行
  references/       # 详细参考文档
  scripts/          # 可执行脚本
  assets/           # 静态资源
```

`SKILL.md` 正文应简洁聚焦，超过 500 行时把细节拆到 `references/`、`scripts/`、`assets/` 子目录，通过 `skill_view(name, file_path=...)` 按需加载。

## 2. n-agent 创建流程

1. 调用 `skill_view("skill-creator")` 学习本规范
2. 调用 `skill_manage(action="create", name="<english-kebab-name>", content=<SKILL.md 全文>)` 创建
3. 创建后可用 `skill_view("<name>")` 验证内容；用 `skills_list` 确认可见

`skill_manage` 的 `edit`/`patch` 用于修改已有 Skill：

- `edit`: 用新 `content` 整体替换 SKILL.md
- `patch`: 用 `old_string`/`new_string` 做精确局部替换

注意：`edit` 不能改 `name`（name 与目录绑定）；`patch` 若触及 frontmatter 会重新校验格式。

## 3. 命名约定

- `name`: 英文 kebab-case，动词-名词或名词短语，如 `deploy-staging`、`db-migration`
- 不要使用 `anthropic`/`claude` 等保留词
- 中文仅出现在 description 的括号 alias 和正文，不作为 name

## 4. 示例

### 合规 Skill

```
---
name: deploy-staging
description: Deploy the current build to the staging environment (部署当前构建到预发环境). Use when the user asks to release or roll out a build to staging.
metadata:
  tags: "deploy,release"
  platforms: "linux,macos"
---

# Deploy to Staging

步骤：1. 构建镜像 2. 推送 3. 滚动更新...
```

### 反例

- 顶层放 `version: 1`：应下沉到 `metadata.version: "1"`
- description 缺中文 alias：会被格式校验拒绝
- name 用大写或下划线 `Deploy_Staging`：必须 kebab-case
- 正文超 500 行未拆分：应拆到 references/

## 5. 校验

`skill_manage` 创建/编辑时会自动校验格式，硬错误（非法 name、description 缺中文 alias、未知顶层字段、metadata 非 string->string）会拒绝写入并返回 `format_invalid:<reason>`。可迁移的旧顶层字段只告警不阻断（写入时自动下沉 metadata）。扫描时格式问题标记为 `format_warning`，不阻断 Skill 进入 registry。

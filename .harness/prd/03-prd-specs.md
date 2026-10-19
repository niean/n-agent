<!-- SUMMARY: N-Agent 的迭代需求池，未经人工确认、禁止AI阅读或修改 -->
# 产品需求 - 迭代演进

## 约束
本文仅供自然人使用，未经人工确认、禁止AI阅读或修改。

## 要求
- 编码：必须严格遵循DDD分层规范；
- 文档：必须说人话，保持极简；
- 前端：FE样式参考 /Users/niean/code/git.zuoyebang.cc/odin/odin-fe；

## 需求列表

[20260901]
- HE
    - HE：提取项目Harness Diff，去除项目特化后、更新到模板。Diff至少包括：三方审阅新增provider claude-code。Harness模板仓库：/Users/niean/code/github.com/niean/harness-tpl
- FR
    - 安全：参考观测、将安全拆分为左导二级菜单，至少包括会话、记忆、沙盒
    - 会话：会话，新增顶导菜单`安全`、放到`观测`右侧，页面复用`左导安全/会话-顶导会话`，独立路由、顶导（遵循同页面多入口规范）

[20260905]
- FR
    - 概览：任务数，显示0、不符合预期
    - 定时任务：任务列表，第一列改名为`任务名称`，并按第一列正序排序。三方审阅用 claude-code

[20260907]
- HE
    - HE：Multica，WF 迭代功能，未展示P3-P6的消息、只展示了P7结果
- FR
    - 定时任务：任务列表，第二列改名、从`调度`改为`调度表达式`
    - 会话：左导会话-顶导安全，去掉sector`整体概览`
    - 左导：定时任务菜单位置调整，放到`会话`后、`任务`前。使用Workflow:迭代功能
    - 左导：分割线调整，将定时任务-任务间的分割线、调整到对话-会话之间。使用Workflow:精调功能

[20260909]
- FR
    - 安装：我正在新Mac上安装N-Agent，希望能够将本机N-Agent的配置数据、一键导入到新机器(支持多台多次导入)。你帮我出一个配置初始化的解决方案，然后给出配置信息列表，等我Check没问题后、落地实施。

[20260910]
- HE
    - HE：调度者检查Agent声明，包括角色、能力、约束

[20260912]
- FR
    - 安装：拍照上传执行失败

[20260913]
- NFR
    - 测试：将现存 12 个非委派会话固化为用户视角 E2E 回归数据集，重大变更后一键全量回归，由 LLM Judge 自动判定；测试数据逻辑隔离、一键准确删除（不漏删、不误删）。除委派类型的会话外，还需剔除掉 Feishu机器人chat、Sandbox执行feishu、Schedule定点报时、Schedule拍照上传 这几个对话
- FR
    - 记忆：文件记忆，编辑modal风格和项目规范的不一致，如点击空白处会导致modal退出，请按项目标准规范、修改modal
    - 插件：插件执行后，未打印Chat消息
    - 对话：对话框，第一条消息太靠上，导致时间hover只能展示出一半
    - 执行：通过对话框，无法关闭浏览器的`实时视图`，我希望能够主动关闭浏览器使用

[20260914]
- NFR
    - 测试：对话E2E测试
        - 优化：会话E2E测试，失败后支持重试、最多重试2次，只要1次通过、就算通过；据此，修改代码逻辑
        - 问题：对话E2E测试 task-aec1ab40-b7ce-54af-9f4f-9c993b6e0714 未被清理，找出原因、彻底修复

[20260915]
- NFR
    - 测试：对话E2E测试优化，支持会话并行跑、并行度设置为3
    - 测试：对话E2E测试优化，隔离测试数据、定期安全删除
    - 测试：E2E测试，清除掉Task任务 相关的测试，主要在task.sh?

[20260916]
- HE
    - HE：E2E测试标准化，抽象为Skill，加入到迭代功能、精调功能、修复Bug工作流中，并支持配置化（如是否开启、执行命令、超时时间等），失败后重试和阻塞
    - HE：如无明确命令要求，AI不做浏览器视觉测试或结果验收（由人来做更快）
- FR
    - 定时任务：顶导标准化，改造为管理、观测、安全（类似`会话`顶导）
    - 定时任务：观测页，干掉任务过滤，表格采用项目标准样式
    - 定时任务：观测页面组件样式优化。干掉列表头上的`刷新`按钮；干掉列`错误`；新增列`操作`，支持`详情`按钮，点击弹出modal、modal使用项目标准样式，将执行时间、状态、输出、投递、错误等信息放到详情modal中；`投递`列不再展示成功或失败的详情，`输出`列不再展示`查看`按钮(已在详情modal中)

[20260917]
- HE
    - HE：知识库、流程分离，知识库存储在模块Git，流程给多个Git使用、存储在流程平台，流程要耦合知识库结构规范。按照这个思路，prd、spec、plan、lesson属于知识库还是流程呢？一个项目涉及多模块Git协同开发时，prd、spec、plan跟项目走(一个)还是分解到各个模块Git？

[20260923]
- NFR
    - 测试：修复所有的测试失败

[20261019]
- FR
    - 任务：支持语音播放say。语音播放`乐乐，你在学习呢`，提示 host_arguments_invalid，会话ID feishu-de413dea-a5c7-483d-a6ba-fe8e4f1f361c

---
以下事项尚未确定，AI不应该阅读、修改、或当做任务目标。

[待办]
- HE
    - HE：VSCode+插件模式，补全Token观测
    - HE：三方审阅，结果存储纳入Harness流程规范，如放到specs/third/目录下
    - HE：阶段结果，未及时同步到Multica
- NFR
    - 观测：按照业界通行做法，给N-Agent补全内部观测，尽量遵循OpenTelemetry规范。需要的配套观测组件，也要枚举下，本机已安装Grafana
    - 知识：UDS，详细原理、Go样例
    - 集成：N-Agent套装，集成N-KB；发布镜像，完成一键冷启动
    - 任务：LLM请求未命中缓存读，分析原因
- FR
    - 会话：会话恢复 => 上下文隔离 => 功能隔离
    - 任务：持久化执行环境，让任务拥有快照、可恢复(如执行`sh docker/restart.sh`后自动恢复)
    - 沙盒：远程沙盒，包括云沙盒、私有VM沙盒
    - 工具：Computer Use(MacOS)
    - 管理：接入配置，秘钥Store(类似平台)
    - 产品：对标Manus，连接器
    - 产品：资源隔离，支持Tenant|WorkSpace|Project(如E2E对话测试隔离)
    - 产品：对标DSH，发现值得参考的技术架构、产品功能
    - 调度：支持MoA
- 行研
    - 范围：2026年7月下旬至10月中旬，以下为公开发布及趋势判断，非独立实测
    - 产品：Meta Muse、OpenAI Dots，常驻Agent配备独立电脑和浏览器，离线持续推进目标、遇到决策再找人。参考：[Muse](https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent)、[Dots](https://learn.chatgpt.com/docs/dots)
    - 产品：Manus 2.0，事件触发自动化、专属云电脑、可人工编辑的创作工作台；Cue为Agent配置邮箱、电话、钱包和电脑，支持群聊协作（早期开放）。参考：[Manus 2.0](https://manus.im/blog/introducing-manus-2-0)
    - 产品：Asana AI Teammates共享项目计划、上下文和治理；Salesforce Agentforce结合模型推理与确定性规则，面向具体岗位落地。参考：[Asana](https://forum.asana.com/t/agentic-work-management-is-here-your-easy-button-for-ai-productivity-across-every-team/1155667)、[Agentforce](https://www.salesforce.com/ap/news/press-releases/2026/09/14/ph-salesforce-expands-agentforce-with-a-new-portfolio-of-ai-agents-built-for-high-value-work/)
    - 技术：OpenAI Agents API托管Harness、会话编排、压缩和恢复（公测）；异步工具调用、执行中纠偏、子Agent委派，让长任务可持续沟通和调整。参考：[OpenAI更新](https://developers.openai.com/api/docs/changelog)
    - 技术：Claude Browser Use结合页面结构与元素引用；Skills API正式化，支持按需压缩、对话中增改工具、仓库文件管理Agent资源。参考：[Claude更新](https://platform.claude.com/docs/en/release-notes/overview)
    - 安全：NVIDIA OpenShell执行运行时策略，Sentry在独立硬件信任域监控和隔离越界行为，安全边界下沉到模型和Harness之外。参考：[NVIDIA](https://nvidianews.nvidia.com/news/open-agent-safety-platform)
    - 科研：Anthropic报告Claude Agent搜索生物数据、筛选候选，再由人工实验验证，形成探索、假设、验证闭环。参考：[Claude科研](https://www.anthropic.com/news/claude-discovers-novel-enzyme-system)
    - 思路：单次任务转向持续责任；执行环境成为核心能力；多Agent需要分工、共享状态和预算；Harness影响成本和完成质量；自由推理结合确定性流程；交付物支持人工接管、编辑后继续迭代
    - 借鉴：N-Agent优先研究持久任务与恢复、事件触发、可审计记忆与技能、独立执行边界、人工接管体验

[待验证]
- 上下文
    - 消息时序，各角色是否OK
    - 工具描述，字节是否稳定
    - 缓存命中
- 工具
    - MCP支持stdio类型


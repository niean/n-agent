<!-- SUMMARY: N-Agent 的迭代需求池，未经人工确认、禁止AI阅读或修改 -->
# 产品需求 - 迭代演进

## 约束
本文仅供自然人使用，未经人工确认、禁止AI阅读或修改。

## 要求
- 编码：必须严格遵循DDD分层规范；
- 文档：必须说人话，保持极简；
- 前端：FE样式参考 /Users/niean/code/git.zuoyebang.cc/odin/odin-fe；

## 需求列表

[20261019]
- FR
    - 任务：支持语音播放say。语音播放`XX，你在学习呢`，提示 host_arguments_invalid，会话ID feishu-de413dea-a5c7-483d-a6ba-fe8e4f1f361c

[20261024]
- FR
    - 观测：[KF]补全OTel可观测。按照业界规范，给N-Agent补全内部观测，要求遵循OTel规范。你需要设计好各种观测信号的用法、暴露方式，包括Log、Trace、Metric、Profile，我也希望借助这次任务、完整认识Agent观测
        - 观测：OTel，页面FE样式规范化，修改页面样式，使全方面遵循项目的FE样式&组件规范
        - 观测：Log，Grafana Logs大盘没数据
        - 观测：Profile，用Grafana大盘代替Pyroscope，后端存储继续使用Pyroscope
        - 观测：Trace，链路日志数据存在Jaeger内存，未使用外部持久化存储如ES/CK
        - 观测：OTel，外部链接名称改为Metric、Log、Trace、Profile，Metric、Log、Profile地址对应到Grafana大盘
        - 观测：Trace，/static http的日志很多、扰乱了Agent执行逻辑信息，看下如何优化
        - 观测：Trace/Log，增加执行摘要信息，方便对齐对话
        - 观测：Trace，大部分http请求只有1个span，不涉及Agent执行过程、可干掉；疑似Bug，对话页面的http请求和`invoke_agent n-agent`没有串上
        - 其它：Nginx，增加本地域名和路由，grafana.localhost => Grafana，jaeger.localhost => Jaeger，pyroscope.localhost => Pyroscope，注意ws等特殊情况。Nginx配置地址 /Users/niean/install/nginx
        - 观测：OTel，Metric、Log、Trace、Profile的外部链接，支持用弹出框自定义配置，要遵循项目FE规范
        - 观测：Log，关联的Jaeger地址不对
        - 观测：Grafana，默认主题色改为Light，时段改为-30m
        - 观测：Log，每行日志的信息太少、只有少数结构化字段，我希望每条日志都要有上下文现场(先不要考虑性能、存储成本等问题)
        - 观测：Trace，组件从Jaeger更换到Grafana Tempo，进而实现metric 用 exemplar 串 trace、trace 串 Loki 日志。评估是否可行
        - 观测：Log/Trace中，`xx other`如chat other、execute_tool other，为什么展示other、而不是真实的取值
        - HE：抽象N-Agent的观测规范，至少包括信号承载组件(如Jaeger)、打点位置(如Trace覆盖Agent执行过程)、信号和标准字段等。对于OTel规范已涵盖的，简洁介绍、同时说明出自OTel。要求必须说人话，保持极简
        - 观测：Trace，Http根Span补全Log
        - 观测：Trace/Log，补全Http的请求和回执信息，不能以安全+白名单之名、阉割关键信息，全面检查并修复、不要casebycase

[20261027]
- FR
    - 会话：Graph，移除执行前的模型列表请求list_models，Provider配置由配管来保障

[20261029]
- FR
    - 观测：访问nkb实现Trace串联。背景是，N-KB正在按照N-Agent的规范，接入OTel观测能力
        - 蒸馏：Httpx的Trace传递规范，核心是Header traceparent和tracestate

[20261031]
- FR
    - 观测：定时任务，补全定时任务应用层根Span；全面检查，补全AgentRun的入口层根Span
        - 迭代：定时任务、任务、GW等入口，补全请求和结果payload。chat已出过类似问题，你要找出原因(规范错误、还是模型不长记性)、加上教训，避免OTel Trace再犯相似问题
    - 观测：Grafana，examplar当前只关联LLM指标上，我希望examplar能体现出AgentRun入口差异，如Chat、GW、定时任务、任务等。如何增加metric及examplar、以及如何修改Grafana大盘？请实施
    - 测试：E2E，结果关联Trace，方便人工审阅
        - 观测：mem0，调用路径存在Trace埋点缺口，请修复；其它外置memory，也要查漏补缺
        - 任务：对于有明确上游的任务，任务的首次执行、自动重试、进程恢复 都应该复用上游TraceID；上游包括 对话、CMD、API、人工点击、定时触发等。只有缺少上下文的任务，才允许创建根 Trace（且要显式标注为异类）
        - 测试：E2E，Config Bundle移出E2E测试，可归类为install或migrate测试；测试结果打平展示在消息框，列`分类|用例|结果|TraceID|链接`，分类有对话、制品、OTel => 结果可以放到 locals/e2e-reports/${测试ID}/result.md
        - 对话：会话，筛选是否缺少类型 `Skill自进化`、是否跟`Curator`地位相似？答：否，Skill自进化没有独立会话、复用sessionid
        - 对话：标题生成，是否能从LLM、改为本地算法生成？答：是。标题改为本地算法生成，LLM异步生成需在.env中开启配置

[20261101]
- FR
    - 执行：[KF]新增持久化AgentRun执行账本，关联业务，和Session一起、承载执行记录
        - 背景：对话，如何关联Trace信号？比如，记录对话和Trace对应关系，业界有没有成熟产品实践？结论是缺少AgentRun概念、无法稳定承载Trace关联
        - 概念：对齐AgentRun概念，对话Turn -> AgentRun，对话只是Agent的一种业务场景；任务、定时任务、技能维护也有AgentRun
    - 对话：Turn底部增加Run详情。回复消息的底部放一个轻量入口，形如`${结果，如已完成} · ${耗时，如18秒} · ${工具，如调用 3 个工具}     [详情](打开侧栏、展示Run详情) [Trace](链接到Trace查看Grafana大盘)`
        - 迭代：①footer默认隐藏，鼠标hover到消息卡片时展示；②Run详情页面再视觉结构化一点，spec-261101-add-turn-run-footer.md
        - 迭代：参考Codex的风格，footer和气泡分离，放到气泡下方、默认隐藏+鼠标hover到消息卡片时展示，只保留详情、Trace两个带悬浮提示的图标按钮
        - 迭代：气泡的时间信息，从左上/右上角、移动到footer右侧(参考Codex)；原非Turn回复消息没有footer，需要补一个相同样式的。
            - 澄清：左侧消息的时间放在 footer 最后一项并左对齐(当前是右对齐)，右侧消息保持现状

[20261102]
- FR
    - 对话：对话框泄露了2条过程消息，不符合预期(之前做过约束、默认不展示过程消息)。会话 dashboard-f381858e-9bf8- gb 49c7-94e0-e4173d6d49ff，过程消息 `我来帮您查询天气。先看看是否有您之前保存的常用地点，同时加载天气查询技能。`，`已确认您的偏好：未指定城市时默认查询**北京**。正在获取实时天气。`
    - 对话：traceid=ca0df63a6f12466ccfac9fd35629e757、spanid=320f35438eec33fd，记忆加载`memory.prefetch mem0`多次超时报错，修复下
    - 对话：对话框，第一条消息和顶部挨得太近了，需要流出Gap

[20261103]
- NFR
    - 观测：统一观测语义，以OTel标准为主，明确自定义规范
    - 对话：修复首轮后mem0记忆丢失问题。现象是，第1轮user msg少了mem0记忆，导致llm重新做了一次mem0检索

[20261104]
- FR
    - 观测：长度限制，调整到1024000，当前有太多有效信息被截断了
    - 对话：执行详情，当没有选中执行时，按时间正序、展示会话的所有执行列表，每一项都对应链接，点击链接可加载对应的执行详情
        - 迭代：对话的执行详情，执行列表样式优化、链接只加在最后一列，同时干掉首列编号；干掉按钮返回执行列表，改用Trace下方、放`返回列表`的文字链接
    - 对话：修复定时任务user消息重复组装


[20261105]
- FR
    - 定时任务：结果投递，默认只投递到定时任务对话框、不投递飞书IM信道，同时支持在FE上修改、放开飞书IM投递(可能需要修改接口)
        - 飞书：支持群聊模式。包括投递定时任务的结果到飞书群，群里@机器人后N-Agent自动发现群ID、并放到飞书IM信道的选择列表中（FE布局较差，记录TODO将来修复）
    - 定时任务：通过自带对话框信道、增删改定时任，当前只支持飞书IM信道、未支持自带对话框
    - 技能：修复健康状态。photo-and-upload，健康状态显示archived，不符合预期
    - 观测：Metric补全缓存Token，包括读/写缓存Token、缓存命中率、归一化Token等指标。先按照业界规范、定义观测语义，然后再补全埋点统计，最后加到Grafana大盘。归一化Token是本项目特有的定义，应该有一个计算规则，提取出来、做成观测语义契约
        - 迭代：新增的Token指标契约，更新到 locals/blog/design-agent-observability.md

[20261106]
- FR
    - 技能：新增wake-codex技能操作不便，先遇到skill注册不自动问题(需在Dashboard手动触发扫描)，后遇到本机执行脚本注册/更新不自动问题(需要改配置文件host-terminal-policy.yaml、没有Dashboard操作入口)
    - 任务：spanid=96337564aa1678d7，任务失败原因 `judge returned unparseable result`，但有效工作是达成的。帮优化judge result，避免这种低级错误
    - 观测：Profile，当前按服务和进程采样，使用 py-spy → Pyroscope；尚未接入 OTel Profiles
        - 结论：OTel Profiles尚处于Alpha阶段，暂不修改当前Profile信号链路
    - 评测：如何增加 evaluate 能力？当前的E2E测试中Agent相关的，是否能转为evaluate的一部分
        - 结论：E2E测试可以作为smoke eval
    - 观测：[KF]引入Langfuse组件，尽量复用OTel组件(如Collector)；现有的OTel能力全部保留，跟Langfuse并存
        - 迭代：Langfuse组件的内存设置太大了、容易打爆宿主，帮缩小、满足N-Agent常规使用即可(数据量不大)
        - 迭代：Langfuse 原生接收 OTLP/HTTP，但现有字段仍需映射，才能正确展示 Agent、模型、工具及其输入输出 => 将字段映射、更新到观测语义
        - 迭代：OTel页增加Langfuse链接，Dashboard 观测-OTel页/外部，增加 Langfuse链接(http://langfuse.localhost/project/n-agent-local-project/sessions)
        - 迭代：OTel到Langfuse语义字段映射，缺少rag吗？按照Langfuse的能力，对N-Agent已具备的功能做映射

[20261108]
- FR
    - 观测：E2E测试，增加OTel/Langfuse对比校验，包括指标数值、Trace链路等
    - 前端：左导收起时hover提示，一级菜单图标要有hover，告知用户这是什么菜单

[20261109]
- FR
    - 成本：补全Langfuse中的cost分析。补全N-Agent/N-KB在Langfuse中的cost分析，服务端统一计价，经OTel同源分发到Langfuse和现有观测栈。
        - spec: spec-261109-model-cost-observability.md
        - 迭代：Dashboard 会话-观测页，增加费用cost统计，包括整体概览、会话列表(在`归一化 Token`后新增`费用`列)；观测-会话页，也新增费用cost统计，包括会话总览、API 调用历史(在`归一化`后新增`费用`列)。FE样式，要严格遵循项目规范，相同组件Follow现有样式

[20261110]
- FR
    - 评测：[KF]建立evaluation子域，统一执行评估、管理版本和保存结果。补全N-Agent/N-KB在Langfuse中的score分析，是否需要N-Agent具备正式的evaluate能力、还是托管到Langfuse？是否需要服务端统一评估分数，经OTel同源分发到Langfuse和现有观测栈？我希望实现一套符合业界标准的evaluate方案
        - spec: spec-261109-design-evaluation.md
        - 代码审阅：审阅本次修改的代码Diff，重点检查①架构和代码遵循项目规范、②功能符合spec/plan定义，最多列出30个问题。spec: spec-261109-design-evaluation.md，plan: plan-261110-evaluation-closed-loop.md
        - 功能补全：参考Langfuse，对当前的`评估`能力做增删改，spec spec-261112-evaluation-workbench.md 
        - 迭代：按照项目FE规范，优化评测相关的FE

---
以下事项尚未确定，AI不应该阅读、修改、或当做任务目标。

[待办]
- FR
    - 观测：Agent作为业务流程的上游或下游节点时，有哪些关键的观测语义、是否有业务标准？类似Google提出的RPC服务黄金四指标，我希望Agent应用也有这样的黄金指标
    - 架构：定义应用Runtime SPI和语义标准，让Agent应用可以自由切换Runtime、无需修改业务代码。借鉴Claude Managed Agents的解耦思路，分离Session、Harness与Sandbox，用户拥有Session数据权限，Agent团队负责Harness，执行环境团队负责Sandbox，业务团队则只需专注于Agent应用逻辑
    - 上下文：参考CC微压缩、完整压缩，优化现有压缩方案
    - 会话：会话恢复 => 上下文隔离 => 功能隔离
    - 任务：持久化执行环境，让任务拥有快照、可恢复(如执行`sh docker/restart.sh`后自动恢复)
    - 沙盒：远程沙盒，包括云沙盒、私有VM沙盒
    - 工具：Computer Use(MacOS)
    - 管理：接入配置，秘钥Store(类似平台)
    - 产品：对标Manus，连接器
    - 产品：资源隔离，支持Tenant|WorkSpace|Project(如E2E对话测试隔离)
    - 产品：对标DSH，发现值得参考的技术架构、产品功能
    - 调度：支持MoA
- HE
    - HE：三方审阅，在spec/plan基础上，增加代码Diff审阅
        - 代码审阅：审阅本次修改的代码Diff，重点检查①架构和代码遵循项目规范、②功能符合spec/plan定义，最多列出10个问题。=> 将代码Diff审阅抽象为独立Skill，可组合到工作流Phase，约定好审阅输入参数(如变更的文件)，约定审阅结果的目录(./codes/)、文件(follow spec/plan)、内容格式规范；负责审阅的Provider产出意见，但不直接修改
    - HE：三方审阅，spec/plan审阅从直接修改、改为给出建议，从而保留现场和解耦
    - HE：VSCode+插件模式，补全Token观测
    - HE：三方审阅，结果存储纳入Harness流程规范，如放到specs/third/目录下
    - HE：阶段结果，未及时同步到Multica
- NFR
    - 知识：UDS，详细原理、Go样例
    - 集成：N-Agent套装，集成N-KB；发布镜像，完成一键冷启动
    - 任务：LLM请求未命中缓存读，分析原因

[待验证]
- 上下文
    - 消息时序，各角色是否OK
    - 工具描述，字节是否稳定
    - 缓存命中
- 工具
    - MCP支持stdio类型


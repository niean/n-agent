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


---
以下事项尚未确定，AI不应该阅读、修改、或当做任务目标。

[待办]
- HE
    - HE：三方审阅，从直接修改、改为给出建议，从而解耦
    - HE：VSCode+插件模式，补全Token观测
    - HE：三方审阅，结果存储纳入Harness流程规范，如放到specs/third/目录下
    - HE：阶段结果，未及时同步到Multica
- NFR
    - 知识：UDS，详细原理、Go样例
    - 集成：N-Agent套装，集成N-KB；发布镜像，完成一键冷启动
    - 任务：LLM请求未命中缓存读，分析原因
- FR
    - 观测：nkb补全观测，确保trace可连通
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


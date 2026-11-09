<!-- SUMMARY: Agent 行业行研（2026年7月下旬至10月中旬），产品/技术/安全/科研动态与 N-Agent 借鉴方向 -->
# 行业研究：Agent 趋势

## 范围

2026年7月下旬至10月中旬，以下为公开发布及趋势判断，非独立实测。

## 产品

- Meta Muse、OpenAI Dots，常驻Agent配备独立电脑和浏览器，离线持续推进目标、遇到决策再找人。参考：[Muse](https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent)、[Dots](https://learn.chatgpt.com/docs/dots)
- Manus 2.0，事件触发自动化、专属云电脑、可人工编辑的创作工作台；Cue为Agent配置邮箱、电话、钱包和电脑，支持群聊协作（早期开放）。参考：[Manus 2.0](https://manus.im/blog/introducing-manus-2-0)
- Asana AI Teammates共享项目计划、上下文和治理；Salesforce Agentforce结合模型推理与确定性规则，面向具体岗位落地。参考：[Asana](https://forum.asana.com/t/agentic-work-management-is-here-your-easy-button-for-ai-productivity-across-every-team/1155667)、[Agentforce](https://www.salesforce.com/ap/news/press-releases/2026/09/14/ph-salesforce-expands-agentforce-with-a-new-portfolio-of-ai-agents-built-for-high-value-work/)

## 技术

- OpenAI Agents API托管Harness、会话编排、压缩和恢复（公测）；异步工具调用、执行中纠偏、子Agent委派，让长任务可持续沟通和调整。参考：[OpenAI更新](https://developers.openai.com/api/docs/changelog)
- Claude Browser Use结合页面结构与元素引用；Skills API正式化，支持按需压缩、对话中增改工具、仓库文件管理Agent资源。参考：[Claude更新](https://platform.claude.com/docs/en/release-notes/overview)

## 安全

- NVIDIA OpenShell执行运行时策略，Sentry在独立硬件信任域监控和隔离越界行为，安全边界下沉到模型和Harness之外。参考：[NVIDIA](https://nvidianews.nvidia.com/news/open-agent-safety-platform)

## 科研

- Anthropic报告Claude Agent搜索生物数据、筛选候选，再由人工实验验证，形成探索、假设、验证闭环。参考：[Claude科研](https://www.anthropic.com/news/claude-discovers-novel-enzyme-system)

## 思路与借鉴

- 思路：单次任务转向持续责任；执行环境成为核心能力；多Agent需要分工、共享状态和预算；Harness影响成本和完成质量；自由推理结合确定性流程；交付物支持人工接管、编辑后继续迭代
- 借鉴：N-Agent优先研究持久任务与恢复、事件触发、可审计记忆与技能、独立执行边界、人工接管体验

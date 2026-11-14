# 转换 E2E 数据集

agent-e2e-v1 从现有 E2E 中提取现有评估执行器能完整执行和评分的业务题。首版仅包含 1 道图片识别题，用于冒烟回归，不能代表 Agent 总体质量或作为总体发布质量结论。原 E2E 全部保留。

agent-e2e-scenarios 是 schema 2 场景数据集（转换规则 e2e-conversion-v2），按场景契约声明执行步骤、注册断言、生命周期规则与必需指标覆盖，由场景实验执行器消费。版本 1（R1）包含 4 道题：multimodal-vision-chat（图片）、plugin-hello-chat（hello 插件）、sandbox-execute-chat（sandbox 执行）、skill-weather-chat（天气实时抓取）。图片题输入内联 data URL messages（与 agent-e2e-v1 相同形态，执行层直接透传），内联字节与 fixture 同源；其中 skill-weather-chat 为 live_smoke 来源，source.source_captured_at 记录采集时间；该题不添加源样例中不存在的 skill 调用要求，必需指标只覆盖原 rubric 条款。live 来源的通过判定依赖正式端口采集的真实工具证据，回答文本本身不构成证据。

版本 2（R2）累计 7 道题：R1 四题加上 memory-file-chat（文件记忆）、memory-mem0-chat（mem0 记忆，合成测试事实写入专用命名空间）、rag-nkb-chat（N-KB 检索引用）。记忆题通过 options.external_memory_enabled 逻辑别名声明绑定，首轮前由服务端绑定独立记忆空间并登记资源 intent；rag-nkb-chat 增加 kb_source_and_quote 确定性断言：检索证据只从正式端口 search_knowledge 成功记录派生（backend n_kb），条目须正文非空且带 document_revision，回答须满足原 rubric 两条路径之一——原文引用（回答含检索原文连续片段）或标题+链接（均须来自真实检索结果），并禁止 web_fetch/browser_navigate/browser_click；不检索到可用原文时如实失败或 blocked，不以模型回答正确代替真实检索。比较分组按 comparison_scope、execution.profile、fixture 与 bindings 材料划分，不同分组的结论不混同；派生固定题另用 case_id 并记录 source.derived_from，不顶替 live 源题。执行详情接口随结果返回比较分组及不可比原因，工作台执行详情展示。

版本 3（R3）累计 10 道题：R2 七题加上 browser-login-pod（browser 通道，browser_state 断言核对打开/关闭状态）、task-artifact-chat（task 通道，task_terminal 与 artifact_set 断言）、task-artifact-cmd（task_api 通道，task_terminal、file_content 与 artifact_set 断言）。任务题的执行资源（任务、工作区文件、制品）随执行单完成门释放，制品按删除传播级联 task_attachment。

版本 4（R4）累计 12 道题：R3 十题加上 cli-weather-chat 与 acp-weather-chat，两题此前因 HTTP 评估无法验证入口协议而被排除，随场景执行器接入真实 CLI 与 ACP 通道解除排除。cli-weather-chat 经真实 n-agent CLI 子进程执行，cli_channel 断言核对来源、Gateway 会话绑定、每步退出码与输出；acp-weather-chat 经 n-agent acp stdio 入口走 initialize→authenticate→session/new→session/prompt，protocol_sequence 断言核对四阶段，tool_succeeded 核对 web_fetch 真实执行。通道会话证据按 retain_evidence_default 保留，进程资源及时释放；rubric 中协议/工具条款由确定性断言权威核验，agent.scenario_quality 只评回答内容（Judge 证据槽不含协议/工具证据，见债务 D112）。

## 数据集

- 入口：evaluations/datasets/agent-e2e-v1/manifest.json，版本 1。
- 场景集：evaluations/datasets/agent-e2e-scenarios/1/manifest.json（版本 1，R1 四题）、2/manifest.json（版本 2，R2 七题）、3/manifest.json（版本 3，R3 十题）与 4/manifest.json（版本 4，R4 十二题），schema 2。
- 来源：tests/e2e/conversations/multimodal-vision-chat.json 的 m1，manifest.source 记录源文件 SHA-256；不继承原会话 ID。
- 输入：保留原提问和原图片。fixtures/chat-multimodal-1.png 是源 PNG 的逐字节副本；messages 中内联同一图片的 data URL，现有执行器可直接发送，不需要额外加载附件。
- 标准：保留原人工参考描述“正确描述图片内容：简洁的 App/网页界面截图，含标题\"有什么我能帮你的吗？\"与\"对话/工作\"分段选择器（对话为选中态）”，通过 expected_output.expectation 进入 Judge 的 reference。
- 评分：使用 agent.answer_correctness 的实验定义，以参考描述核对回答；不以模型的完成自述替代质量评分，不声称验证工具实际执行或入口协议。场景集使用 agent.scenario_assertions（确定性断言）与 agent.scenario_quality（逐轮语义评分）。

图片及参考描述已人工核对。数据集 hash 冻结输入、来源标记和标准；fixture_sha256 校验附件。改变题目、图片或标准应创建新版本，不覆盖已发布版本。源 E2E 后续变化时，来源一致性测试会提示重新审核，不自动更新基准。

## 转换范围

| 源 E2E | 决定 | 原因 |
|---|---|---|
| multimodal-vision-chat | 纳入 | 原图片可由 messages 透传，原人工参考描述可用于正确性评分 |
| acp-weather-chat | 版本 4 纳入 | 原 HTTP 评估不能验证 ACP 协议流程；场景执行器接入真实 ACP 通道后以 protocol_sequence/tool_succeeded 断言核验 |
| cli-weather-chat | 版本 4 纳入 | 原 HTTP 评估不能验证 CLI 入口；场景执行器接入真实 CLI 通道后以 cli_channel 断言核验 |
| skill-weather-chat | 排除 | 缺少 web_fetch 实际抓取证据，天气文本本身不能证明实时查询 |
| plugin-hello-chat | 排除 | 缺少 hello 插件实际调用证据，不能用问候文本代替 |
| sandbox-execute-chat | 排除 | 缺少 execute_code 成功与 stdout 证据 |
| rag-nkb-chat | 排除 | 缺少 N-KB 后端、相关原文与禁止工具证据；不虚构检索 qrels |
| memory-file-chat | 排除 | HTTP 执行器不透传 external_memory_enabled，不能保证启用原文件记忆 |
| memory-mem0-chat | 排除 | 同上，且原题依赖外部个人记忆 |
| browser-login-pod | 版本 3 纳入 | 原 HTTP 评估缺少浏览器状态证据；场景执行器接入 browser 通道后以 browser_state 断言核验 |
| task-artifact-chat | 版本 3 纳入 | 原 HTTP 执行器无后台任务/制品证据；场景执行器接入 task 通道后以 task_terminal/artifact_set 断言核验 |
| task-artifact-cmd | 版本 3 纳入 | 同上；经 task_api 通道以 task_terminal/file_content/artifact_set 断言核验 |

部署、观测、协议连通和评估投递类 Shell E2E 继续验证系统链路，不转换为模型回答质量题。

## 运行前提与入口

在 Docker 中运行；目标模型必须支持图片输入，已配置 N_AGENT_EVALUATION_ENABLED、N_AGENT_EVALUATION_AGENT_BASE_URL 和独立 Judge Provider，评估 worker 可工作。运行时选择的目标模型由现有服务配置决定，数据集不切换模型或启用工具。

当前 Dockerfile 不自动打包 evaluations/，Compose 也未挂载该目录。先从项目根目录将数据集拷入容器的 /tmp（也可自行只读挂载数据集）。{app_container} 替换为业务容器 ID 或名称，experiment-v1 须与注册表中的实验评分定义版本一致。总闸关闭、执行器未接线或 Judge 未配置时，先完成部署配置。

```sh
docker cp evaluations/datasets/agent-e2e-v1 {app_container}:/tmp/agent-e2e-v1
docker exec {app_container} n-agent evaluate run \
  --dataset /tmp/agent-e2e-v1 \
  --name e2e-vision-before \
  --evaluator agent.answer_correctness@experiment-v1
```

变更后保持同一数据集和评分定义，使用另一个实验名称运行。通过评估工作台查看实验、回答与正确性结果，对比前后差异。工具清单、系统提示词、目标模型和 Judge 等配置应记录并核对；只有目标改动，才适合将差异归因于该改动。

上例未传 --policy：命令等待评分作业终态并返回实验报告，不计算发布门禁；退出码 0 只表示命令完成，不等于回答正确。只有显式提供 --policy 时才使用门禁退出码：0（gate_pass）、1（gate_fail）、3（inconclusive），操作或基础设施失败返回 2。error、skipped、无评分或未运行不得视为质量通过，应查看实际评分结果；单题 smoke 不足以形成总体发布质量结论。

图片内联随输入进入证据快照，受既有图片校验和 2 MiB 快照上限约束；超限或缺证据的跳过结果不算通过。

## 清理边界

图片题不要求业务写操作，不声明 allowed_side_effects 或 cleanup_rule。现有实验执行器会创建隔离会话，但不自动删除；not_required 只表示没有声明业务清理规则，不表示会话已经删除。

实验完成后，从该实验业务 Run 关联的真实 Session ID 定位会话，经会话管理删除；必要时可使用既有 DELETE /chat/sessions/{session_id}。只删除本次实验实际产生且已核实归属的会话，不沿用源 source_session_id，也不按宽泛前缀批量删除。原 E2E 的清理程序未被此数据集继承。

## 验证范围

tests/application/test_evaluation_e2e_dataset.py 验证 loader、来源和附件 hash、HTTP 多模态消息、实验快照及 correctness Judge 参考槽位。HTTP 和 Judge 使用测试替身；这些测试证明数据集可被既有链路消费，不代表真实目标模型已答对图片题。真实模型实验见项目人工验收清单。

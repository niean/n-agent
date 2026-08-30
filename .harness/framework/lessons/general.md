<!-- SUMMARY: 通用教训：仅与Harness框架相关、不绑定具体语言/框架/项目，AI自主维护 -->
# 通用教训

AI 自主维护，人工可通过提示或建议触发新增/修正。
通用教训仅收录与 Harness 框架相关的经验，不绑定具体语言、框架或项目，随 Harness 模板提取复用。

---

### L001: Write 工具调用必须确认 file_path 和 content 参数完整

现象：使用 Write 工具写入 plan/spec 等长内容文件时，连续多次报 `InputValidationError: The required parameter file_path is missing, The required parameter content is missing`，导致任务流程中断、需要重新组织内容重写。

根因：在大段 thinking 后触发 Write 工具调用时，模型在思考过程中决定"调用 Write 写入文件 X"，但实际生成工具调用 JSON 时参数序列化丢失，file_path 和 content 两个必填参数均为空。连续重试相同调用模式不会自动修复参数缺失，只会重复报错。问题在 content 越长（如完整 plan 文件含多个 Task 的代码块）时越容易触发。

教训：
1. 调用 Write 前，在 thinking 中显式确认两个必填参数都有明确值：file_path（绝对路径）和 content（完整文本）；不要在 thinking 仅决定"要写文件"就触发调用
2. 若 content 很长（超过 ~3000 字），改为分段写入：先 Write 创建文件含首段内容，再用 Edit 工具 append 追加后续段落，降低单次调用的 content 体积
3. 第一次 Write 报参数缺失错误后，禁止原样重试；先在 thinking 中重新组织完整 content 文本，再发起调用
4. 优先用 Edit 修改已有文件（仅传 diff，体积小）；只有创建新文件或完全重写时才用 Write

来源：2026-07-04 plan-260704-cli-commands.md 写入时连续 3 次 Write 调用参数缺失，第 4 次才成功

---

### L002: 守护机制的开销必须与威胁模型对齐并在真实工作区量化

现象：third-review runner 从"单次 codex exec"重构为带全仓快照守护后，spec/plan 审阅耗时从分钟级恶化到超时失败（900 秒 watchdog 期限内审阅无法完成）。

根因：快照守护对整个工作区做递归 find 并对每个文件 spawn 两次 git hash-object（内容哈希+记录哈希）。真实工作区有 2.4 万个文件（.venv 占 2.1 万，Git 跟踪仅 804），单次快照约 6.5 分钟，provider 前后共 4 次快照约 26 分钟，审阅尚未开始即耗尽时间。守护要防的威胁（越界写入、并发修改、归因）只需要"HEAD diff 集合 + 未忽略未跟踪集合 + index/HEAD 快照"即可覆盖，全量哈希防的是小概率边缘场景，付出的是每次审阅的固定巨额开销。

教训：
1. 新增守护/校验机制前先列威胁模型，逐项选择最小代价实现；"越全面越好"的全量方案在真实仓库规模下不可行
2. 集合粒度检测（git diff --name-only / ls-files --others 集合本身变化）配合只对集合内文件做内容哈希，可把全量守护降为个位数文件开销且语义几乎无损
3. 守护类脚本必须在真实项目工作区（含 .venv、node_modules 等大型忽略目录）量化耗时，临时小仓库测不出规模问题

来源：2026-08-23 third-review run-review.sh 快照守护性能分析与重设计，全仓 24335 文件 vs 跟踪 804 文件

---

### L003: 后台子 shell 中的长 sleep 会被孤儿化且可能持有调用方管道

现象：调用方以 `$(...)` 捕获 runner 输出时，runner 正常退出后调用方仍阻塞最长 900 秒；每次运行还泄漏一个 sleep 进程。

根因：`sh` 中 `( sleep 900; ... ) &` 的后台子 shell 被 `kill $pid` 时，只终止子 shell 本身，正在执行的 sleep 子进程成为孤儿继续运行；该孤儿继承了启动时的 stdout 管道，命令替换要等所有管道写入端关闭才返回，于是调用方阻塞到 sleep 自然结束。

教训：
1. 后台子 shell 一律显式重定向输出（>/dev/null 2>&1），保证被孤儿化时不持有调用方的管道
2. 期限类 watchdog 用 1 秒步进循环（while + sleep 1 + 计数）替代单次长 sleep，把孤儿存活上界从整个期限压到 1 秒
3. 排查"命令已退出但调用方挂住"时，先 ps 查孤儿 sleep 进程是否持有输出管道

来源：2026-08-23 third-review run-review.sh watchdog/monitor 孤儿 sleep 导致 $(...) 捕获阻塞 900 秒

---

### L004: Third Review 的 approved/0 项 可能是 provider 静默失败的兜底结果

现象：third-review runner 返回"状态: approved / 修改数量: 0 项"，看起来审阅通过，实际 provider 一个字都没审——stdout 为空，stderr 是 `failed to initialize in-process app-server client: Operation not permitted`。

根因：两层叠加。一是 runner 在格式非法时走"目标变更兜底"，只比对目标文件哈希，文件未变即判 approved/0 项，provider 的真实 stdout 落在会被 EXIT trap 删除的临时目录里，从不回显；二是 codex 这类 provider 需要创建 PATH alias 和本地 app-server，放进受限沙箱或后台任务执行会初始化失败，且失败时仍可能以退出码 0 结束。

教训：
1. 收到 approved/0 项时，必须同时看 stderr 有没有 `structured summary was invalid; using target-change fallback`；出现这句就说明五行摘要不是 provider 给的，不能当审阅通过
2. provider 类命令在前台、非沙箱环境执行；后台任务或沙箱内跑到的"失败"是环境结论，不是审阅结论
3. 需要看 provider 原文时，用 `HARNESS_THIRD_REVIEW_CODEX_BIN` 指向只做 tee 的包装脚本，保留 runner 的边界检查，不改框架文件
4. 判断 runner 成败要取 runner 自己的退出码；`cmd | tail` 后的 `$?` 是 tail 的退出码，不能作为成功证据
5. `git hash-object` 是纯内容哈希，对 untracked/gitignored 文件同样有效，目标变更检测不因文件未被追踪而失效

来源：2026-08-24 spec-260824-chat-composer-skill 三方审阅首轮被误报 approved，前台非沙箱重跑后真实结果为 fixed/24 项

---

### L005: Provider 已具备文件工具时不要叠加第二套补丁协议

现象：Claude Code 能正常使用 `ark-code-latest` 完成审阅，却在 provider 将结构化 `old_text + occurrence + new_text` 修改顺序应用到目标文档时失败，报 `edit occurrence is out of range`，导致整个三方审阅以 65 退出。

根因：Claude Code 本身已经具备 Read/Edit 文件能力，但适配器禁用工具后又实现了“嵌入全文、生成 JSON edits、解析 occurrence、重建全文、原子替换”的第二套写入机制。多条修改按原始文档生成、按已变更文档顺序应用时容易互相遮蔽；额外协议增加了故障面，却没有加强 runner 已有的目标文件与关联 spec 边界。

教训：
1. Provider 的职责应与其原生能力对齐：coding agent 直接读取和修改 runner 指定的目标文件，并输出统一五字段摘要；不要在 provider 中翻译成另一套补丁语言
2. 安全边界由 runner 负责，provider 只配置最小工具面；Claude Code 使用 Read/Edit + acceptEdits，Codex 使用 workspace-write，二者保持同构
3. stderr 中的模型识别诊断不等于调用失败；必须结合 provider 退出码和 stdout 判断。`ark-code-latest` 即使输出 `unrecognized_model` 诊断，仍可成功调用并完成文件编辑

来源：2026-08-31 claude-code third-review plan 审阅 occurrence 解析失败，参照 codex provider 改为直接编辑

---

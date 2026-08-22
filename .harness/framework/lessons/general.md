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

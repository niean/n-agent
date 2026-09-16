---
name: run-e2e-tests
description: 在 Harness 工作流验收阶段按统一配置执行端到端测试。
---

# Skill: 执行端到端测试

从项目根目录依次调用 `sh .harness/framework/scripts/get-config.sh tests.e2e.enabled`、`tests.e2e.command` 和 `tests.e2e.timeoutSeconds`。不得直接读取或解析 `.harness/harness.json`。

- `enabled=false` 时立即输出 `e2e: disabled`，不得读取其余键、检查命令或启动进程。
- 任一 getter 失败时输出 `e2e: failed`，保留 getter 诊断并以非零退出；不得猜测默认值或跳过。
- 启用时命令必须非空且不含 NUL、回车或换行；否则输出 `e2e: failed` 并以非零退出。
- 启用时调用 `python3 .harness/framework/skills/harness/run-e2e-tests/scripts/run-e2e.py "$command" "$timeout"`。运行器从仓库根目录以独立进程组启动命令；超时后 TERM、等待 5 秒、再 KILL 全组。
- 成功输出 `e2e: executed`；普通非零退出输出 `e2e: failed`；超时输出 `e2e: timed_out`。调用方将后两者及配置失败视为验收不通过。

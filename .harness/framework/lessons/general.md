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

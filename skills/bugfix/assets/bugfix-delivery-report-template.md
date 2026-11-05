# 后端 Bugfix 交付报告

## 1. 交付结论

- **结果**：`COMPLETED | NEEDS_REAPPROVAL | BLOCKED`
- **问题判定**：`CONFIRMED_BUG`
- **方案**：`<proposal_id and version>`
- **审批记录**：`<approval reference>`（`RUN_DIR/approval.txt` 与 `verify-approval` 的 `OK`）
- **Base / Final SHA**：`<sha> / <sha>`
- **范围检查**：`verify-scope` 结果 `PASS/FAIL` + `out_of_scope`/`preexisting_touched` 摘要
- **审查模式**：`FULL | LITE | WAIVED_BY_USER | NOT_RUN`

## 2. 问题原因

- **根本原因**：`<root cause>`
- **证据链**：`<evidence summary>`

## 3. 已实施修复

| 文件/符号 | 实际修改 | 与获批方案的对应关系 |
|---|---|---|
| `<path:symbol>` | `<change>` | `<proposal item>` |

## 4. 验证证据

| 层级 | 命令/检查 | 退出码 | 结果 | 证明内容 |
|---|---|---:|---|---|
| `<L0-L6>` | `<command>` | `<code>` | `PASS/FAIL/NOT_RUN/NOT_AVAILABLE` | `<claim>` |

表内每一行的命令、退出码、结果和错误指纹必须来自 `RUN_DIR/validation-record.json`（由 `bugfix_state.py run-plan` / `run-validation` 写入），不得手工填写或事后补写。抄表时只取记录的 `command`、`exit_code`、`result`、`proves` 与 `fingerprint` 字段；`output_tail` 已经截过尾部，不要把完整日志贴进报告。

## 5. 系统 Code Review

- **结论**：`APPROVE | APPROVE_WITH_FOLLOW_UPS | REQUEST_CHANGES | BLOCKED`
- **与审查模式的一致性**：`FULL` 接受 `APPROVE/APPROVE_WITH_FOLLOW_UPS`；`LITE` 只接受 `APPROVE_WITH_FOLLOW_UPS`；`WAIVED_BY_USER` 时必须写出用户批准原文位置与未复核维度，不得写成本地审查通过；`NOT_RUN` 时本小节其余项一律填 `NOT_VERIFIED`。
- **根因已消除**：`Yes/No`
- **审批范围已遵守**：`Yes/No`
- **保留行为评估**：`<assessment>`
- **报告校验**：`<system codekit:code-review report and snapshot/diff digest evidence>`

## 6. 发布、回滚与人工验收

- **发布建议**：`<RPD-aligned advice>`
- **回滚与数据修复**：`<steps/limitations>`
- **未运行项和剩余风险**：`<items>`
- **人工验收项**：`<only what automation cannot prove>`

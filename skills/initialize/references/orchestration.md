# Initialize 编排

## Phase 0 清单

只能通过 `scripts/controller.py start` 启动。任何写入前完成并记录：

1. `git rev-parse --show-toplevel` 的绝对仓库根。
2. 已规范化的语言集合与 `--embeddings`、`--force`、`--allow-partial`、`--commit`。
3. `HEAD` SHA、branch，以及 staged、unstaged、untracked 的 NUL-safe 状态快照。
4. 所有可能托管文件的存在性、内容摘要和运行前 dirty 状态。
5. 本地 GitNexus runner、全局 GitNexus、Graphify Agent Skill/CLI 的版本和 help 能力。
6. UTC run-id、项目根 `.codekit/initialize/<run-id>/` 运行目录、result 清单与 `.codekit/initialize.lock`。

托管集合至少包括 `.gitignore`、`.graphifyignore`、`AGENTS.md`、`CLAUDE.md`、将复制或更新的 `.standard/**/*.md`；Worker 后续申报的路径加入本次 managed set。

锁文件内容必须包含 schema、run-id、root、创建时间和运行清单路径。用独占创建取得锁；已有锁即停止并报告，不覆盖。结束时只有锁内 run-id 与当前清单一致才可释放。崩溃遗留锁由用户确认后人工恢复。

## Phase 1 容量规划

- GitNexus 由控制器同步执行（`controller.py analyze`），不占用子 Agent 槽位：先在等待窗口内运行它，或先派发两个 Sub-agent 再运行。
- 可用并发槽位不少于 2：同时启动 `initialize-graphify`、`initialize-context-scanner`。
- 只有 1 个可用槽位：按顺序创建两个指定名称的独立 Subagent，前一路返回后再创建下一路；不能改由主 Agent代做。
- 无法创建指定名称的 Subagent：状态为 `BLOCKED`，不得以主 Agent扫描、普通工具摘要或自然语言结果替代。

不要为了凑固定 DAG 创建额外 Worker。`initialize-graphify` 与 `initialize-context-scanner` 的角色定义位于 `../agents/`，必须使用它们的名称和独立会话；不要让任何分析器与 Phase 3 的上下文写入并发。

## 阶段状态

Manifest 状态只允许以下迁移：

```text
preparing -> prepared -> gated -> reconciled -> finished
                         |          |
                         +----------+-> aborted
```

内部模块不得跳过前置状态。Gate 或 reconcile 失败时，控制器必须调用 `abort` 并释放属于当前 run-id 的锁。

| 阶段 | required | 失败效果 |
|---|---:|---|
| baseline/lock | 是 | 立即停止，零业务写入 |
| standards/ignore | 是 | 停止分析收敛 |
| GitNexus（工具存在） | 是 | 阻断上下文收敛和提交 |
| Graphify（工具存在） | 是 | 阻断上下文收敛和提交 |
| `initialize-context-scanner` | 是 | 阻断上下文生成 |
| context-generation | 是 | 阻断验证/提交 |
| validation | 是 | 阻断提交 |

工具不存在时对应分析 Worker 返回 `skipped`，整体为 `partial`。只有 `--allow-partial` 可以继续 Phase 3；partial 永远不是 success，默认不得提交。协作会话不可用时 Worker 必须返回 `status: skipped`、`required: true`、`error.code: collab-unavailable`，整体为 `blocked`；该状态不可由 `--allow-partial` 放行。

## 恢复与停止

- Worker 可对瞬态执行错误重试一次；索引损坏按 tool adapter 的保守路径处理。
- Barrier 的输入路径或 JSON 错误保持当前 run 和锁，控制器输出 `recoverable: true` 及期望目录；修复后重试同一个 run。不得因为这类错误重新 `start` 一个完整 initialize，除非用户明确要求。
- 越界写入先停止，不自动删除或回滚用户文件。Gate、reconcile 或最终验证失败时，manifest 统一进入终态 `aborted` 并释放当前 run 的锁；最终报告仍保留 `status: failed` 作为失败证据。
- 上下文写入前出现 required failure，不生成或更新 `AGENTS.md`、`CLAUDE.md`。
- 任何退出路径都输出 run-id、manifest、锁状态和可恢复动作；阶段之间的宿主中断使用 `controller.py abort` 恢复。成功 `finish` 释放锁后删除本次 `.codekit` 状态；若目录中有用户文件则只删除本次 run 子目录。

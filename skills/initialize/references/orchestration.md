# Initialize 编排

## Phase 0 清单

`controller.py start` 之前先实测宿主的协作能力（`$SKILLS_ROOT/_shared/scripts/probe-collab.py --host <宿主>`）：默认只读、不写仓库也不改宿主配置，在 Codex 上以模型可见 prompt 中的派发工具为准（`codex features list` 的 flag 状态不算证据）。`unavailable` 时先向用户申请授权，同意后带 `--enable` 重跑该脚本（只改一个键，复验失败逐字节回滚），无论写没写都以醒目的重启提示结束本轮，不进入 `start`，因此零业务写入、零锁；`unknown` 不构成阻断证据，照常继续并在派发失败时按 `collab-unavailable` 停止。

只能通过 `scripts/controller.py start` 启动。任何写入前完成并记录：

1. `git rev-parse --show-toplevel` 的绝对仓库根。
2. 已规范化的语言集合，连同它的来源（`language_source`：`explicit` 来自 `--language`，`detected` 来自仓库实测）与实测依据原文（`language_basis`）；两者都拿不到时 `start` 在第 1 项之后、任何写入之前就报错退出，manifest 与锁都不会创建。其余参数为 `--embeddings`、`--force`、`--allow-partial`、`--allow-global-writes`、`--strict-tools`、`--commit`、`--no-git-hooks`（冻结为 `options.graph_git_hooks`，默认 `true`）。
3. `HEAD` SHA、branch，以及 staged、unstaged、untracked 的 NUL-safe 状态快照。
4. 所有可能托管文件的存在性、内容摘要和运行前 dirty 状态。
5. 本地 GitNexus runner、全局 GitNexus、Graphify Agent Skill/CLI 的版本和 help 能力。
6. UTC run-id、项目根 `.codekit/initialize/<run-id>/` 运行目录、result 清单与 `.codekit/initialize.lock`。

托管集合至少包括 `.gitignore`、`.graphifyignore`、`AGENTS.md`、`CLAUDE.md`、将复制或更新的 `.standard/**/*.md`；Worker 后续申报的路径加入本次 managed set。

锁文件内容必须包含 schema、run-id、root、创建时间和运行清单路径。用独占创建取得锁；结束时只有锁内 run-id 与当前清单一致才可释放。同一仓库允许重复 initialize：每次 `start` 重新冻结基线，渲染后内容未变的托管文件跳过落盘，所以无变更的重复执行不产生新 diff。

锁冲突不是无条件停止。控制器读取锁内 manifest，按持有者是否可能仍在工作分类：manifest 缺失、不可解析或已处于 `finished`/`aborted` 终态时，该 run 不可能再触碰锁（所有退出路径都先释放锁再写终态 phase），遗留锁自动回收，回收事实写入 stderr 与新 manifest 的 `warnings`。phase 仍是中间态时该 run 可能活着，不抢占：停止并给出 owning run-id、`started_at`、manifest 路径与可执行的 `controller.py abort --manifest <path> --reason "release stale initialize lock"`，由用户确认后回收。

## Phase 1 容量规划

- GitNexus 与 Graphify 都由控制器同步执行（`controller.py analyze`，随后 `controller.py graphify`），不占用子 Agent 槽位：在 Scanner 的等待窗口内按这一顺序跑完两路。
- 只有一个必需的派发对象：`initialize-context-scanner`。宿主有 1 个及以上可用槽位即可并行推进两路控制器步骤。
- 无法创建指定名称的 Subagent：状态为 `BLOCKED`，不得以主 Agent扫描、普通工具摘要或自然语言结果替代。

不要为了凑固定 DAG 创建额外 Worker。`initialize-context-scanner` 的角色定义位于 `../agents/`，必须使用它的名称和独立会话；不要让任何分析器与 Phase 3 的上下文写入并发。

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

工具不可用时对应分析 Worker 返回 `skipped`，整体为 `partial`，**门禁自动接受并保留锁**：这种情况不需要 `--allow-partial` 也能继续 Phase 3（`--strict-tools` 可恢复 fail-closed）。需要 `--allow-partial` 的只剩「用户明确拒绝那次仓库外写入」与 embedding 这类前提性降级——注册本身先真写一次，不预先拦人（见 [tool-adapters.md](tool-adapters.md) 的「全局注册」）；partial 永远不是 success，默认不得提交。协作会话不可用时 Worker 必须返回 `status: skipped`、`required: true`、`error.code: collab-unavailable`，整体为 `blocked`；该状态不可由 `--allow-partial` 放行。

「工具不存在」包含四态，处置不同（判据见 [tool-adapters.md](tool-adapters.md)）：`ok`、本机确实未安装（`not-installed`）、已安装但当前会话读不到（`not-readable`，宿主按路径拒绝访问）、可读但跑不起来（`not-executable`，例如 venv 的基础解释器落在读不到的目录）。后两态在 Codex 桌面端是常态，且**改 `PATH` 无效**——它的沙箱是按路径拒绝文件系统访问，不是缺 `PATH`。所以不要把这类 partial 直接当结论交付：主 Agent 必须先按 `resolution_hint` 说明属于哪一态，并给出真出路（重跑安装器把工具物化到 CodeKit 工具链根，或用 `CODEKIT_GITNEXUS_RUNNER` / `CODEKIT_GRAPHIFY_RUNNER` 指定当前会话真能执行的 runner）。**但不要因此中止运行**：这类降级由门禁自动接受、锁保留，流程继续到 finish 并如实报告缺口；用户不需要为了继续而重跑一次。

## 恢复与停止

- Worker 可对瞬态执行错误重试一次；索引损坏按 tool adapter 的保守路径处理。
- `registry-denied`（退出码 77、`recoverable: true`）**不是失败**：先按宿主提权通道重跑控制器给出的 `retry_command`（它只做那次注册写入，argv 与原命令一致），成功后重跑 `analyze` 续上同一个 run；重试仍被拒才询问用户，用户同意则带 `--allow-global-writes` 重跑，拒绝则带 `--allow-partial` 接受 `partial/registry-missing`。绝不因此 `abort`、绝不释放锁、也不要用「预先传 flag」代替这一步。
- Barrier 的输入路径或 JSON 错误保持当前 run 和锁，控制器输出 `recoverable: true` 及期望目录；修复后重试同一个 run。不得因为这类错误重新 `start` 一个完整 initialize，除非用户明确要求。
- 越界写入先停止，不自动删除或回滚用户文件。Gate、reconcile 或最终验证失败时，manifest 统一进入终态 `aborted` 并释放当前 run 的锁；最终报告仍保留 `status: failed` 作为失败证据。
- 上下文写入前出现 required failure，不生成或更新 `AGENTS.md`、`CLAUDE.md`。
- 任何退出路径都输出 run-id、manifest、锁状态和可恢复动作；阶段之间的宿主中断使用 `controller.py abort` 恢复。成功 `finish` 释放锁后删除本次 `.codekit` 状态；若目录中有用户文件则只删除本次 run 子目录。

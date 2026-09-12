# 最终验证与报告

## 最终状态

- `success`：所有 required 阶段成功，所有结果与 artifact 复核有效，无越界写入。
- `partial`：仅存在允许的 skipped，且用户显式 `--allow-partial` 后完成上下文；不能称为成功。`required_for_initialize` 验证因白名单程序未安装而 `skipped` 时同样降级为 `partial`（此时即使 gate 为 success 也不得记为 success），并禁止提交。gitnexus 的 `partial/registry-missing` 属于同一档：索引与 artifact 有效，但仓库未注册进 GitNexus 全局 registry，`list`/`query`/`context`/`impact`/`trace`/`cypher`/`check`/`detect-changes` 与 MCP 图查询全部不可用；报告必须原样列出该限制并给出修复方式（获准后带 `--allow-global-writes` 重跑）。
- `failed`：任一 required failure、工具存在但运行失败、artifact 无效、上下文生成/验证失败或越界写入。失败时 manifest 的 phase 为 `aborted`，只有 `final_report.status` 保留 `failed`；不会持久化未定义的 `failed` phase。

## 验证

1. 重新解析所有 JSON/YAML/managed blocks，确认没有缺失、重复或失配 marker。
2. 检查 `.graphifyignore` 用户内容仍存在，Graphify sidecar 与当前 root/version/digest/mode 一致。
3. 检查 `AGENTS.md` 与 `CLAUDE.md` 仅由主 Agent在 Barrier 后更新，引用路径均存在。
4. 由 `controller.py finish` 自行执行 reconcile 记入 manifest 的每一条 `required_for_initialize` 仓库验证命令（不经过 shell，禁用重定向、管道、命令替换与通配符，程序需在控制器白名单内或为仓库内明确路径），真实退出码写入 `final_report.validations`，`validation_source` 为 `executed`。reconcile 已用同一白名单预检这些命令：白名单外程序、禁用 shell 语法或仓库脚本缺失属可恢复的契约错误，在写入托管文件前中止；白名单内但本机未安装的程序记为 `skipped`（`reason: tool-missing`）并写入 `validation_warnings`，整体降级为 `partial`。主 Agent 不得预先自行执行这些命令并把结果当作提交依据：`--validation-result` 属外部验证逃生阀，必须同时传 `--allow-attested-validations`，且 `validation_source` 记为 `attested`。
5. 重新执行 Git 状态，计算相对 Phase 0 的新增改动，检查未申报和越界路径。
6. 成功 finish 后确认锁已释放并删除本次 `.codekit/initialize/<run-id>/`；若 `.codekit` 因存在用户文件不能整体删除，只保留用户文件并报告清理结果。
6. 确认 manifest 已按 `prepared -> gated -> reconciled` 顺序迁移；任一失败路径为 `aborted`，Worker result 摘要仍匹配原文件。
7. 展示精确文件列表、status、diff stat 和关键 diff；默认不 stage、不 commit。

## Commit gate

仅当以下条件全部为真才响应显式 `--commit`：

- overall status 是 success（不是 partial）；
- 用户显式传入 `--commit`；
- validators 全部执行且通过，没有 `skipped`，且验证证据由控制器自己执行得到（`validation_source` 为 `executed`）；存在 skipped 时 `commit_status` 为 `blocked-skipped-validations`；
- 没有未申报/越界写入；
- 每个将 stage 的托管文件在 Phase 0 都不是 dirty；
- stage 列表是运行清单中的精确文件，不包含整个目录或通配符。

提交由 `controller.py finish` 在释放锁前执行，使用路径限定的 commit，避免夹带运行前已暂存的无关变更。主 Agent不在控制器外重复执行 `git add` 或 `git commit`。

任一将 stage 的文件运行前 dirty，即使仅更新了 managed block，也禁止自动提交并保留工作树供用户审阅。

使用 `--allow-attested-validations` 时，即使自报的验证结果全部成功，也不能放行提交：`commit_status` 为 `blocked-unverified-validations`，需用户审阅 diff 后自行提交。

## 报告字段

报告 run-id、root、languages、各阶段及 gitnexus 步骤（控制器执行）/ `initialize-graphify` / `initialize-context-scanner` 状态、partial 原因、artifact 验证、变更文件、validators（含来源、命令、退出码与输出尾部）、`validation_warnings`（skipped 验证原样展示）、commit decision、锁释放结果。上下文截断或渲染告警（`context_warnings`，如 AGENTS.md 条目超行预算被裁剪）必须原样展示，不得静默丢弃。不要声称 skipped 的分析已完成，不隐藏 Graphify chunk/健康警告。

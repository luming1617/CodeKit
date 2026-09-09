# Worker 返回契约

每一路写一个 JSON result。时间使用 UTC ISO-8601，路径使用相对仓库根的 POSIX 路径。

## 固定角色映射

以下三个任务必须由对应的独立全局 Subagent 执行，不能由主 Agent或其他角色代替：

| task | canonical agent | 结果责任 |
|---|---|---|
| `gitnexus` | `initialize-gitnexus` | GitNexus 状态探测、索引/复用和 skills artifact |
| `graphify` | `initialize-graphify` | Graphify 缓存校验、full/update 和 chunk 状态 |
| `context-scanner` | `initialize-context-scanner` | 只读仓库事实、命令候选和证据路径 |

角色正文位于 `skills/initialize/agents/`，并由 `initialize/SKILL.md` 的强制调用规则指定跨平台启动方式。

```yaml
schema_version: 1
run_id: "20260908T120000Z-abc123"
task: gitnexus
status: success | skipped | failed | partial
required: true
runner: gitnexus
command: [gitnexus, analyze, --skills, --skip-agents-md, --verbose]
exit_code: 0
changed_files: []
allowed_paths: [.gitnexus/]
artifacts:
  - path: .gitnexus/meta.json
    validation: json-valid
warnings: []
started_at: "2026-09-08T12:00:00Z"
finished_at: "2026-09-08T12:00:01Z"
duration_ms: 1000
error:
  code: null
  message: null
```

`error.code` 使用控制器集中定义的枚举；未知或拼写错误的 code 必须拒绝。当前受支持的 code 包括 `tool-missing`、`index-current`、`no-change`、`collab-unavailable`、`chunk-failures`、`execution-failed`、`artifact-invalid`、`out-of-scope`、`internal-error`，成功结果的 code 必须为 `null`。

## 状态一致性

- `success`：`exit_code == 0`，required artifacts 均有效，无越界/未申报写入。
- `skipped`：runner 不存在、能力不支持或无需重复工作；`exit_code` 为 null，并在 `error.code` 给出原因。`index-current`/`no-change` 是完整复用，不降低整体状态；`tool-missing` 等不完整跳过使整体为 partial。
- `partial`：Worker 自身明确存在不完整结果；不得用于掩盖失败 chunk。命令进程可以是 0 退出，只要 result 明确说明不完整原因；真实失败仍使用 `failed`。
- `failed`：非零退出、无效 artifact、超出允许写入或内部失败阈值。
- Graphify 超过半数 semantic chunks 失败时整个 Worker 必须 `failed`。

## 写入范围

- GitNexus：`.gitnexus/`；只有实际 help 探测确认 `--skills` 的输出路径后，才增加具体的 GitNexus skill 路径。禁止声明整个 `.claude/` 或 `.agents/`。
- Graphify：`graphify-out/`。
- `initialize-context-scanner`：空集合；Worker 仍必须把 result JSON 写入 manifest 同目录的 run state 目录，result 持久化不计入仓库 changed files。
- 主 Agent准备阶段：`.gitignore`、`.graphifyignore`、选定的 `.standard/` 文件。

每个 Worker result 必须写入与 `run-manifest.json` 相同的 `.codekit/initialize/<run-id>/` 目录（包括 fallback 临时 state 目录），不能写到 `/tmp` 的其他位置或只返回一个未落盘的自然语言摘要。路径错误由 Barrier 作为可恢复输入错误处理，保持同一个 run 和锁。
- 主 Agent上下文阶段：`AGENTS.md`、`CLAUDE.md` 和成功 Graphify 的 `graphify-out/.codekit-state.json`。

路径必须先规范化；拒绝绝对路径、`..`、symlink 逃逸和仅靠字符串前缀伪装的 sibling 路径。这类畸形路径声明是契约违规：控制器把它作为 `errors` 返回并继续校验其余项，不会中断整个 run，也不会阅读声明外的文件。
这些范围由控制器内置的 task policy 决定。Worker 返回的 `allowed_paths` 只是声明其实际使用的权限子集，不能授权新路径。

完整的 GitNexus 结果必须携带可解析的 `.gitnexus/meta.json`。完整的 Graphify 结果必须携带 `graphify-out/graph.json`、`graphify-out/manifest.json` 和 `graphify-out/.graphify_root`；`success` 及 `index-current`/`no-change` 复用都适用。必要 artifact 的校验类型由控制器内置策略决定，Worker 返回的 `validation` 只能作为说明，不能降低 JSON、非空节点或 root-match 校验。

## 主 Agent独立复核

1. result schema、run-id、task 唯一性、状态/exit code 一致。
2. `changed_files` 全部位于 `allowed_paths`。
3. Phase 0 Git 基线与当前 `git status` 的差集没有未申报路径。被 ignore 的产物目录（`.gitnexus/`、`graphify-out/`、GitNexus skills 目录）按 Phase 0 的**顶层条目快照**与控制器授予的**前缀**比较：缓存文件的增减、计数或 mtime 变化不视为越界写入，未预期的新顶层条目仍然失败。
4. artifact 存在、可读，`json-valid` 必须可解析；图/manifest 节点不得为空。
5. 带 root 的 artifact/sidecar 必须解析到当前仓库根。
6. `partial`、`failed` 保持原状态；不得按“失败 Worker 数量”聚合。`collab-unavailable` 是编排阻断，不是普通 partial，且不得由 `--allow-partial` 放行。

校验器输出 `success | partial | failed` 和每项证据。required failure、越界写入或 artifact root 不一致均为 failed。

# 工具 Adapter

所有探测由 `initialize-gitnexus` 或 `initialize-graphify` 使用已安装 runner 完成；initialize 不通过 `npx`、`bunx`、`pnpm dlx`、pip 或其他网络安装器下载工具。

## GitNexus

优先级：项目本地 `.gitnexus/run.cjs`，其次 `PATH` 中的 `gitnexus`。先执行相应 `status` 和 `analyze --help`，以实际输出决定参数。

| 状态 | 动作 |
|---|---|
| 无索引 | 普通 analyze |
| current | skipped，复用有效索引 |
| stale | 普通 analyze，复用内部 parser cache |
| 损坏 | 普通 analyze 重试；仍失败才考虑 `--force` |
| 显式 `--force` | force analyze |
| runner 不存在 | skipped；不下载 |

首选参数：`analyze --skills --skip-agents-md --verbose`，但必须通过 `<SKILL_DIR>/scripts/run-gitnexus.py` 执行。initialize 默认隔离 GitNexus 的全局 registry（使用一次性 `GITNEXUS_HOME`），因为它不属于本次仓库写入范围；adapter 会输出 warning，只有显式设置 `CODEKIT_GITNEXUS_USE_GLOBAL_REGISTRY=1` 才允许写全局 registry。默认不加 `--embeddings`，只有用户显式要求才加。

若不支持 `--skip-agents-md`：

1. 支持 `--index-only` 时运行纯索引，GitNexus skills 标为 skipped；
2. 两者都不支持时可运行旧版 analyze，但它必须在 Phase 3 前完成，并将实际上下文写入列为越界；
3. 不因此把 Graphify 无条件改为串行，只阻止上下文文件并发写入。

当 `--skills` 可用但 `.claude/skills` 或 `.agents/skills` 目标目录不可写时，准备阶段记录 `gitnexus_skill_mode=community-only`；若 help 支持 `--skip-skills`，Worker 追加该 flag，跳过标准 skills mirror 但保留 `--skills` 的社区 skills。若不支持，Worker 必须明确失败。

## Graphify

Agent Skill 与 Shell CLI 是不同接口：

- Codex：`$graphify . --update --no-viz`
- Claude/OpenCode：`/graphify . --update --no-viz`
- Shell：调用 `graphify extract`、`graphify update`、`graphify cluster-only` 等实际子命令；不得执行 `graphify .`。`--no-viz` 在 CLI adapter 中等价为不执行 HTML export。

混合仓库或没有明确可用的语义 backend/model/key 时，Shell 默认使用 `graphify extract . --out . --code-only`。这是本地 AST 模式，不依赖 LLM API key；语义/full 模式必须先完成 backend、model 和 key 预检，并由用户明确要求。

Graphify 能力由 `prepare.py` 写入 manifest 的 `tools.graphify.supports`：`ast_process_pool` 是使用 Graphify 可执行文件 shebang 对应 Python 的真实 `ProcessPoolExecutor(max_workers=2)` 探测，`max_workers` 来自 CLI help。Worker 必须把缺失或未知能力按 `false` 处理；AST 进程池不支持且 `max_workers` 支持时追加 `--max-workers 1`，否则返回明确失败，不得静默改变命令语义。

有效缓存必须同时满足：

- `graphify-out/graph.json`、`manifest.json` 可解析且 graph 节点非空；
- `.graphify_root` 规范化后等于当前仓库根；
- `.codekit-state.json` schema、root、Graphify 版本、ignore digest、mode 均匹配；
- `last_status == "success"`。

决策：无有效 graph/manifest 时 full；上述状态全部匹配时 update；root/版本/ignore/mode 变化或上次 partial/failed 时 full；显式 `--force` 时 full force。

成功后由主 Agent（不是 Worker）原子写入；`ignore_digest` 只摘要 CodeKit managed Graphify ignore block：

```json
{
  "schema_version": 1,
  "root": "/absolute/project/root",
  "graphify_version": "0.9.56",
  "ignore_digest": "sha256:...",
  "mode": "default",
  "last_status": "success"
}
```

sidecar 的 digest 基于最终 CodeKit managed Graphify ignore block 和保留的用户规则计算。Graphify partial/failed 不得盖写为 success。

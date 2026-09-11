# 工具 Adapter

所有探测由控制器的 `analyze` 步骤（GitNexus）或 `initialize-graphify` 使用已安装 runner 完成；initialize 不通过 `npx`、`bunx`、`pnpm dlx`、pip 或其他网络安装器下载工具。

## GitNexus

### Runner 解析（决定索引是否会每次全量重建）

只使用**真实安装**的 GitNexus，命令前缀取 manifest 的 `tools.gitnexus.command`：

1. 环境变量 `CODEKIT_GITNEXUS_RUNNER`（可含参数，如 `node /path/run.cjs`）；
2. `PATH` 上的 `gitnexus`。

**不得**回落到仓库内 `.gitnexus/run.cjs`：该 shim 在没有真实安装时按
`gitnexus@latest` 走 npx/bunx/pnpm dlx，每次解析到的版本、Node 版本和依赖树都
可能不同，GitNexus 会据此判定「分析器身份变化」并强制全量重建（大仓库上以
分钟计），同时也违反本 Skill「不通过 npx/dlx 下载」的约束。`runner` 为
`unresolved` 时按 tool missing 处理并返回 `skipped`，把 `resolution_hint` 写进
warnings，不要自行拼 `npx` 兜底。

### 模式镜像（避免模式漂移触发重建）

`analyze` 的 `--pdg` 是 opt-in，索引一旦用一种模式建立，另一种模式的运行会让
GitNexus 强制重建。因此参数必须镜像 manifest 里记录的既有索引模式：

- `gitnexus_index.pdg` 为 `true` → 追加 `--pdg`；为 `false`/`null` → 不追加。
- `--embeddings` 只在输入显式要求时追加：它控制**是否新建**向量，不是「是否保留」。
- `gitnexus_index.present` 为 `false`（无既有索引）时按 GitNexus 默认模式分析，
  不要主动引入 `--pdg` 或 `--embeddings`。

### embedding 策略

索引里已有向量而 runner 缺本地 embedding 栈时，一次全量重建会在「重算已有
向量」这一步直接 exit 1。按 manifest 的 `gitnexus_embedding_mode` 执行：

| 模式 | 动作 |
|---|---|
| `preserve` | 正常分析，保留既有向量（GitNexus 默认行为） |
| `drop` | 追加 `--drop-embeddings`，并在 warnings 声明语义检索已失效 |
| `install-authorized` | 先执行 `gitnexus embeddings install`（全局写入，已获用户授权），再执行 analyze |

`install-authorized` 时通过 `<SKILL_DIR>/scripts/run-gitnexus.py --use-global-registry`
执行：用户已授权全局写入，注册应落在真实 `GITNEXUS_HOME`，而不是一次性临时目录。

| 状态 | 动作 |
|---|---|
| 无索引 | 普通 analyze |
| current | skipped，复用有效索引 |
| stale | 普通 analyze，复用内部 parser cache |
| 损坏 | 普通 analyze 重试；仍失败才考虑 `--force` |
| 显式 `--force` | force analyze |
| runner 不存在 | skipped；不下载 |

基准参数是 `analyze --skills --skip-agents-md --verbose`，再按上面的模式镜像与
embedding 策略追加 flag，并且必须通过 `<SKILL_DIR>/scripts/run-gitnexus.py` 执行。
默认隔离 GitNexus 的全局 registry（一次性 `GITNEXUS_HOME`），因为它不属于本次
仓库写入范围；此时 adapter 会输出 warning。用户显式传入 `--allow-global-writes`
（manifest `options.allow_global_writes` 为 `true`）时，控制器改用
`run-gitnexus.py --use-global-registry`，让注册落在真实 `GITNEXUS_HOME`；该目录
不可写时 adapter 仍降级为临时 registry 并给出 warning，不视为失败。

若不支持 `--skip-agents-md`：

1. 支持 `--index-only` 时运行纯索引，GitNexus skills 标为 skipped；
2. 两者都不支持时可运行旧版 analyze，但它必须在 Phase 3 前完成，并将实际上下文写入列为越界；
3. 不因此把 Graphify 无条件改为串行，只阻止上下文文件并发写入。

当 `--skills` 可用但 `.claude/skills` 或 `.agents/skills` 目标目录不可写时，准备阶段记录 `gitnexus_skill_mode=community-only`；若 help 支持 `--skip-skills`，控制器追加该 flag，跳过标准 skills mirror 但保留 `--skills` 的社区 skills。若不支持，控制器必须明确失败。

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

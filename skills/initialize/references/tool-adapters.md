# 工具 Adapter

所有探测由控制器的 `analyze` 步骤（GitNexus）或 `initialize-graphify` 使用已安装 runner 完成；initialize 不通过 `npx`、`bunx`、`pnpm dlx`、pip 或其他网络安装器下载工具。

## 解析顺序与四态（两个工具共用）

解析顺序固定为：`CODEKIT_<TOOL>_RUNNER` → 安装器写出的 `toolchain.json` → `PATH` → CodeKit 已知目录。
**清单优先于 `PATH`**：即使当前会话里恰好有一个能用的旧 shim，也按安装器记录的绝对 argv 执行，
结果才跨宿主确定。实现是 `skills/_shared/scripts/codekit_runtime.py`（7 个技能与安装器共用同一份候选
目录与状态判定），命令行入口 `skills/_shared/scripts/resolve-runner.py`。

`prepare.py` 把结论写进 `resolution_hint` 与 `tools.<tool>.state`，四态各有不同处置，不要一律当成
「未安装」：

| 状态 | 判据 | 处置 |
|---|---|---|
| `ok` | 入口可 stat、可读，探测命令（`--version` / `--help`）成功 | 正常执行 |
| `not-installed` | 所有候选入口都不存在（`stat` 抛 ENOENT/ENOTDIR） | 让用户重跑安装器并勾选该组件；不得用 `npx`/`pipx` 兜底 |
| `not-readable` | `stat` 成功但内容读或父目录列举抛 EACCES/EPERM | 重跑安装器（提权/非沙箱会话），让工具重建到工具链根。**改 `PATH` 无效** |
| `not-executable` | 入口可读，但探测命令失败；或 venv 的 `pyvenv.cfg` 指向的 `home`/`executable` 不可达 | 同上；`available=false`，按 `tool-missing` 跳过 |

**为什么「改 PATH」永远无效**：受限宿主的沙箱是**按路径拒绝文件系统访问**，不是缺 `PATH`。实测
Codex 桌面端的 `PATH` 里用户级条目是全的（含 `%APPDATA%\Roaming\npm` 与 Python 目录），但
`shutil.which` 需要 `stat` 候选文件，而 `stat` 被拒；`%APPDATA%\npm` 下的入口连 `stat` 都拿不到
EACCES，`%LOCALAPPDATA%\Programs\Python` 整个目录不可达。真正的出路是让工具落在**已实测可读且可
执行**的 CodeKit 工具链根 `<home>/.local/share/CodeKit/`（`~\.local\share\CodeKit` 实测 read/exec
均 ok、仅写入被拒），这由安装器负责：

1. 重跑安装器并勾选该组件（`install.cmd install` / `install.sh install`）——它会把工具物化到
   工具链根并写出 `toolchain.json`；
2. 用 `CODEKIT_GITNEXUS_RUNNER` / `CODEKIT_GRAPHIFY_RUNNER` 指定一个当前会话真能读到且能执行的
   runner。

**技能侧永不在运行期修复工具链**：该根在沙箱内写入被拒，所以技能只能「读 → 校验 → 降级 → 报告要
重跑什么」。任何让 `prepare.py` 写工具链根的想法都是错的。

「可读但跑不起来」必须落在 `available=false` 并保持 **`tool-missing`（`skipped`）**，
不得记成 `execution-failed`：入口存在就假设它能跑，会把「本机跑不了」误报成「工具坏了」。
GitNexus 的 `analyze` 步骤与 Graphify Worker 都以 manifest 的 `available` 为准；`available=false` 时
同样不触发 embedding 与全局注册前提——向用户要一次针对跑不起来的工具的授权没有意义。

**这类降级不打断运行**：门禁判 `partial` 但不 abort、不释放锁、也不要求 `--allow-partial`，
运行继续到 finish，报告逐条点名（`worker_gate.degradations`）。`--strict-tools` 供 CI 恢复
fail-closed。

## GitNexus

### Runner 解析（决定索引是否会每次全量重建）

只使用**真实安装**的 GitNexus，命令前缀取 manifest 的 `tools.gitnexus.command`：

1. 环境变量 `CODEKIT_GITNEXUS_RUNNER`（可含参数，如 `node /path/run.cjs`）；
2. `PATH` 上的 `gitnexus`；
3. CodeKit 已知安装目录里的 `gitnexus`（`source` 记为 `declared`）——工具链根内的私有
   目录（Windows `%APPDATA%\npm`）、CodeKit 私有 venv 的 `Scripts/`（bin/）等。

**不得**回落到仓库内 `.gitnexus/run.cjs`：该 shim 在没有真实安装时按
`gitnexus@latest` 走 npx/bunx/pnpm dlx，每次解析到的版本、Node 版本和依赖树都
可能不同，GitNexus 会据此判定「分析器身份变化」并强制全量重建（大仓库上以
分钟计），同时也违反本 Skill「不通过 npx/dlx 下载」的约束。`runner` 为
`unresolved`，或入口解析到了但 `available` 为 `false`（本会话跑不起来）时，按
tool missing 处理并返回 `skipped`，把 `resolution_hint` 写进 warnings，不要自行拼
`npx` 兜底。这两态的区别与处置见上文「解析失败的三态」。

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

### 全局注册（查询路径的前提）

GitNexus 把索引写在仓库内 `.gitnexus/`，却把仓库注册在
`<GITNEXUS_HOME>/registry.json`（`GITNEXUS_HOME` 未设置时取 `~/.gitnexus`），
并且**只用后者**解析仓库：

- 走仓库内索引：`analyze`、`status`、`doctor`；
- 走全局注册表：`list`、`query`、`context`、`impact`、`trace`、`cypher`、
  `check`、`detect-changes`、`index`，以及全部 MCP 图查询。

因此「索引有效」和「查询可用」是两件事：只跑 analyze 而不注册时，`status --json`
报 up-to-date，查询却一律报 `No indexed repositories found`。而 adapter 默认隔离
registry（一次性 `GITNEXUS_HOME`，进程退出即删除），恰好会造成这个状态，所以：

| 情形 | 行为 |
|---|---|
| 已注册 | 无需任何全局写入，不询问用户 |
| 未注册且用户授权（`--allow-global-writes`） | 经 `run-gitnexus.py --use-global-registry` 执行，注册落在真实 `GITNEXUS_HOME`；授权目录不可写时 adapter 降级为临时 registry 并 warning |
| 未注册且未授权、未传 `--allow-partial` | `start` 在零业务写入前失败（`PrepareError`），主 Agent 必须询问用户后重跑 |
| 未注册且未授权、已传 `--allow-partial` | 不写注册表，gitnexus 阶段记为 `partial/registry-missing`（整体 partial、禁止提交） |
| 已授权但注册仍未落地 | `failed`，不得记 success |

控制器在**复用路径**（`status=current`）也会补注册：那时 analyze 不运行，否则仓库会
长期停在「索引有效但查询全废」。补注册走 `gitnexus index <root>`——它只登记既有
`.gitnexus/`，不重新分析，幂等且开销小。注册状态由 `gitnexus_registry_state(root)`
在宿主环境下实测，写入 result 的 `registry` 字段；gate 会独立重测一次，缺失即拒绝
complete 结论。`--use-global-registry` 不可写时会降级为临时 registry 并输出 warning，
这仍是失败信号而不是可接受的降级。

若不支持 `--skip-agents-md`：

1. 支持 `--index-only` 时运行纯索引，GitNexus skills 标为 skipped；
2. 两者都不支持时可运行旧版 analyze，但它必须在 Phase 3 前完成，并将实际上下文写入列为越界；
3. 不因此把 Graphify 无条件改为串行，只阻止上下文文件并发写入。

当 `--skills` 可用但 `.claude/skills` 或 `.agents/skills` 目标目录不可写时，准备阶段记录 `gitnexus_skill_mode=community-only`；若 help 支持 `--skip-skills`，控制器追加该 flag，跳过标准 skills mirror 但保留 `--skills` 的社区 skills。若不支持，控制器必须明确失败。

## Graphify

### Runner 解析（Worker 必须用绝对路径，不得回落裸名）

命令前缀取 manifest 的 `tools.graphify.runner`（解析顺序与 GitNexus 对称）：

1. 环境变量 `CODEKIT_GRAPHIFY_RUNNER`（`runner_type` 为 `override`）；
2. 安装器写出的 `toolchain.json`（`source` 为 `toolchain`）；
3. `PATH` 上的 `graphify`（`source` 为 `path`）；
4. CodeKit 已知安装目录里的 `graphify`（`source` 为 `declared`）——工具链根与历史落点（Windows
   `%APPDATA%\npm\graphify.cmd`）与 CodeKit 私有 venv 的 `Scripts/graphify.exe`。

Worker 执行 `extract` / `update` / `cluster-only` 时**必须用这个绝对路径**。裸名
`graphify` 只在宿主 shell 的 `PATH` 上成立，而把用户级 `PATH` 丢掉的宿主（Codex
桌面端的 shell 只继承机器级 `PATH`）解析不到它：探测已成功的工具会因此变成
`execution-failed`，比诚实报 `tool-missing` 更难排查。`runner_type` 为
`unresolved`、或入口解析到了但 `available` 为 `false`（本会话跑不起来，例如沙箱
拒绝读取 venv 里的解释器）时，一律按 tool missing 处理并返回 `skipped`，把
`resolution_hint` 写进 warnings，不要自行拼 `npx` / `pipx` 兜底。这一态不要报
`execution-failed`——它不是「工具坏了」，而是当前会话读不到；处置见上文
「解析失败的三态」。

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

### 仓库 git 钩子与宿主可见性

`graphify hook install` 装的是**本仓库**的 `post-commit`/`post-checkout` 钩子和 merge driver（写 `.git/hooks` 与仓库级 `git config`，不产生 Git 可见变更），与智能体的 PreToolUse 钩子是两回事。它由 `controller.py finish` 默认执行，`--no-git-hooks` 才跳过；能力位 `tools.graphify.supports.git_hooks` 由 `--help` 是否列出 `hook install` 实测得到，缺失时记为 `skipped + unsupported` 并提示升级。回滚命令是 `graphify hook uninstall`，`graphify hook status` 可随时复核。

initialize **不**调用任何 `graphify install` / `uninstall`：`claude`/`windows` 平台目标会改写 `~/.claude/CLAUDE.md`，与本次运行的单写者约束冲突。宿主注册归安装器（`scripts/installer.py`）。平台目标里只有 `agents`（别名 `skills`）既不写 CLAUDE.md 也不装 agent 钩子，其落点 `~/.agents/skills/graphify` 正是 Qoder 桌面 IDE 扫描的共享技能目录；`codex` 落在 `~/.codex/skills`，Qoder 不读。

# 上下文与 managed blocks

## Ignore 文件

`.gitignore` 仅维护：

```text
# codekit:initialize:ignore:start
.codekit/
.gitnexus/
.graphifyignore
.claude/skills/gitnexus*/
.agents/skills/gitnexus*/
# codekit:initialize:ignore:end
```

这六条是冻结的 canonical 列表，准备阶段每次原样重写，不随本次 Worker 产出变化：托管块的形状一旦在两次运行之间改变，就会翻转块内条目的 git 可见性，使 baseline 与门禁的对比失去意义。GitNexus skills 是本地生成物，只按 `gitnexus*` 窄 glob 忽略，不要忽略 `.gitattributes`、整个 `.claude/`、整个 `.agents/` 或整个 `.claude/skills/`。

`.graphifyignore` 保留用户内容，只维护：

```text
# codekit:initialize:graphify-ignore:start
.codekit/
.standard/
AGENTS.md
CLAUDE.md
.gitnexus/
.claude/
.agents/
graphify-out/
# codekit:initialize:graphify-ignore:end
```

## `initialize-context-scanner` contract

`initialize-context-scanner` 只读返回 JSON：

```yaml
schema_version: 1
root: /absolute/project/root
overview: 一句可由证据支持的仓库用途
structure:
  - path: scripts/
    description: 安装与验证脚本
commands:
  - purpose: 校验技能布局
    command: python3 scripts/validate-skill-layout.py
    evidence: AGENTS.md
    required_for_initialize: true
evidence:
  - path: README.md
warnings: []
```

不得猜测命令，不得把图摘要当唯一证据；所有事实要有仓库内路径。只有适合在初始化后执行、不会产生外部副作用的必要验证才标记 `required_for_initialize: true`。Scanner 不写文件。

reconcile 前逐字段校验该 contract：`structure` 条目必须有非空 `path` 与 `description`，且 `path` 在仓库内真实存在；`commands` 条目必须有非空 `purpose` 与 `command`，`evidence` 可为一个路径或路径列表且每条必须真实存在；`required_for_initialize: true` 的命令必须带 evidence。缺字段或证据不存在时以可读的契约错误中止 reconcile，不会到渲染阶段才抛 `KeyError`。

### 仓库验证白名单

`required_for_initialize: true` 的命令由 `controller.py finish` 亲自执行：不经 shell，禁止重定向、管道、命令替换与通配符，可执行程序必须是下列白名单之一，或仓库内明确路径的脚本（`./…`、`scripts/…`）。白名单外的程序（`git`、`curl`、`docker`、`npx` 等）即使本机已安装也会被拒绝：

```text
# initialize 仓库验证白名单，必须与 scripts/_common.py 的 VALIDATION_PROGRAMS 逐项一致
bash
cargo
cc
cmake
deno
dtl
gcc
go
gradle
just
make
mvn
node
nox
npm
pnpm
pre-commit
py
pytest
python
python3
sh
tox
yarn
zig
```

Python 启动方式按宿主区分，与安装器入口（`INSTALL.md`）的规则一致：macOS/Linux 用 `python3 …`，Windows 用 `py -3 …`。**干净 Windows 上 `python3` 是微软商店别名桩**：`which` 能找到它，但执行后以非零码退出，会被记为 `validation failed` 而不是 `skipped`，所以 Windows 上不要发 `python3 …`。

Scanner 在标记 `required_for_initialize: true` 之前必须先用 `command -v <program>` 自检（Windows 用 `Get-Command`）：程序不在白名单或本机未安装时，保留命令但把该字段置为 `false`。reconcile 用同一白名单预检每个 required 命令，程序不在白名单、含禁用 shell 语法或仓库脚本缺失时以可恢复的契约错误在写入任何托管文件之前中止（修复 context 后重试同一个 run）；白名单内但未安装的程序在 finish 记为 `skipped` 并写入 `validation_warnings`，整体降级为 `partial` 且禁止提交。

## 规范索引

复制 `<SKILL_DIR>/standard/01-global/**/*.md`、每个选定语言的 `02-stack/<language>/**/*.md`、`03-project/**/*.md`。保留目标专属文件；内置的非 index 文件可覆盖，已有 `00-instructions.md` 只更新：

```text
<!-- codekit:initialize:index-files:start -->
<!-- codekit:initialize:index-files:end -->
```

索引只列真实存在的同目录 Markdown 文件。内置 `standard/` 模板是空占位，空索引由 `prepare.py` 写入种子文案，内容固定为：声明 `Security > Project > Stack > Global`、按场景渐进读取，以及 `_common.REPLY_LANGUAGE_RULE` 的仓库级回复语言锚点（默认简体中文，用户只用 `@skill`/斜杠命令触发、本轮没有自然语言时同样适用）。

## AGENTS.md 与 CLAUDE.md

主 Agent是唯一写者。保留非 CodeKit 内容和其他工具 managed block。

- `AGENTS.md`：最多四个简洁事实章节、60 行预算（standards block 不计），随后维护 `codekit:initialize:standards` block；路径全部来自实际 `.standard/`。每类条目最多 20 条，超出预算的条目从尾部裁剪并写入 `context_warnings`，不把 Scanner 的超长输出当作整个运行的失败。
- `CLAUDE.md`：只在同名 standards block 中生成存在的 `@.standard/.../00-instructions.md` imports。
- 两个文件分别渲染；禁止互相复制。两者的 standards block 都带上同一行 `_common.REPLY_LANGUAGE_RULE`（回复语言默认简体中文，`@skill`/斜杠命令等无自然语言触发时同样适用）——它来自共享常量而不是互相拷贝，是目标仓库每个会话都会加载的唯一语言信号。

managed block 若仅有单边 marker、重复 marker 或顺序错误，停止并报告，不能猜测覆盖范围。写入使用同目录临时文件加原子替换，内容末尾保持单个换行。

补充：Scanner 不写仓库内容；按 Worker contract 将唯一的结构化 result JSON 持久化到 manifest 同目录的 run state 目录。

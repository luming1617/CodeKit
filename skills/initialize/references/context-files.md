# 上下文与 managed blocks

## Ignore 文件

`.gitignore` 仅维护：

```text
# codekit:initialize:ignore:start
.codekit/
.gitnexus/
graphify-out/
.graphifyignore
# codekit:initialize:ignore:end
```

不要默认忽略 `.gitattributes`、整个 `.claude/` 或 `.agents/`。若 GitNexus skills 是本地生成物，只加入探测到的具体路径。

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

## 规范索引

复制 `<SKILL_DIR>/standard/01-global/**/*.md`、每个选定语言的 `02-stack/<language>/**/*.md`、`03-project/**/*.md`。保留目标专属文件；内置的非 index 文件可覆盖，已有 `00-instructions.md` 只更新：

```text
<!-- codekit:initialize:index-files:start -->
<!-- codekit:initialize:index-files:end -->
```

索引只列真实存在的同目录 Markdown 文件，声明 `Security > Project > Stack > Global` 和按场景渐进读取。

## AGENTS.md 与 CLAUDE.md

主 Agent是唯一写者。保留非 CodeKit 内容和其他工具 managed block。

- `AGENTS.md`：最多四个简洁事实章节、60 行预算（standards block 不计），随后维护 `codekit:initialize:standards` block；路径全部来自实际 `.standard/`。每类条目最多 20 条，超出预算的条目从尾部裁剪并写入 `context_warnings`，不把 Scanner 的超长输出当作整个运行的失败。
- `CLAUDE.md`：只在同名 standards block 中生成存在的 `@.standard/.../00-instructions.md` imports。
- 两个文件分别渲染；禁止互相复制。

managed block 若仅有单边 marker、重复 marker 或顺序错误，停止并报告，不能猜测覆盖范围。写入使用同目录临时文件加原子替换，内容末尾保持单个换行。

补充：Scanner 不写仓库内容；按 Worker contract 将唯一的结构化 result JSON 持久化到 manifest 同目录的 run state 目录。

<!-- codekit:initialize:project-context:start -->
## Repository Guide


### Overview

CodeKit is a multi-platform AI development plugin package that distributes seven AI-assisted development workflow skills, installer tooling, and Claude/Codex/OpenCode plugin metadata.

### Structure

- `skills/`: Seven workflow skill directories, each centered on SKILL.md and root-level openai.yaml metadata.
- `skills/initialize/`: Initialize workflow definitions, worker roles, standards, controller scripts, and initialize tests.
- `scripts/`: Python installer, uninstaller, component registry/selector, state/configuration helpers, and validation utilities.
- `.claude-plugin/`: Claude plugin and marketplace metadata.
- `.codex-plugin/`: Codex plugin and marketplace metadata.
- `.opencode/`: OpenCode package metadata and plugin implementation.
- `install.sh`: macOS/Linux bootstrap entrypoint for dependency preparation and installation.
- `uninstall.sh`: macOS/Linux entrypoint for the independent uninstall orchestrator.
- `README.md`: Repository overview, supported platforms, workflow commands, and installation examples.
- `GUIDE.md`: User guide covering prerequisites, installation, configuration, initialization, and workflows.
- `INSTALL.md`: Detailed installer design, component registry, lifecycle, and extension documentation.
- `lefthook.yml`: Pre-commit and pre-push hook definitions for version synchronization and plugin/skill validation.
- `plugin-version.json`: Configurable plugin version source used by package manifests and synchronization tooling.

### Commands and Verification

- Validate every skill has a matching SKILL.md and Chinese root-level openai.yaml metadata.: `python3 scripts/validate-skill-layout.py` (evidence: `scripts/validate-skill-layout.py`, `skills/README.md`, `lefthook.yml`)
- Check the canonical installer component registry without installing components.: `python3 scripts/component-registry.py --check` (evidence: `scripts/component-registry.py`, `INSTALL.md`)
- Install or upgrade selected CodeKit components from the local repository source.: `bash install.sh install -y` (evidence: `README.md`, `INSTALL.md`, `install.sh`)
- Uninstall selected CodeKit components.: `bash uninstall.sh -y` (evidence: `README.md`, `INSTALL.md`, `uninstall.sh`)
- Synchronize plugin-version.json into the Claude, Codex, and OpenCode package manifests.: `python3 scripts/sync-plugin-version.py` (evidence: `scripts/sync-plugin-version.py`, `lefthook.yml`, `plugin-version.json`)
- Build the GitNexus repository analysis and skill artifacts after installation.: `gitnexus analyze --skills --verbose` (evidence: `GUIDE.md`, `skills/initialize/SKILL.md`)
<!-- codekit:initialize:project-context:end -->

<!-- codekit:initialize:standards:start -->
## AI 开发规范

规范目录：`.standard/`。这是规范索引，不要预先加载全部规范。
优先级：Security > Project > Stack > Global。

| 类别 | 先读取 | 详细文件 |
|---|---|---|
| 全局规范 | `.standard/01-global/00-instructions.md` | `.standard/01-global/api.md`, `.standard/01-global/git.md`, `.standard/01-global/security.md`, `.standard/01-global/unit-test.md` |
| 技术栈规范 | `.standard/02-stack/python/00-instructions.md` | `.standard/02-stack/python/01-test.md` |

进入某个场景时，先读取对应的 `00-instructions.md`，再只读取该场景所需的详细规范文件。
<!-- codekit:initialize:standards:end -->

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **marketplace** (1654 symbols, 3093 relationships, 102 execution flows).

> Index stale? Run `node .gitnexus/run.cjs analyze --index-only` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? Bootstrap with `npx`, `bunx`, or `pnpm dlx` — e.g. `bunx gitnexus@latest analyze` (npm 11 npx crash; #1939).

## Always Do

- **MUST run impact before editing.** Use `impact({target: "symbolName", direction: "upstream"})` or `node .gitnexus/run.cjs impact "symbolName" --direction upstream --repo .`; report callers, processes, and risk. Never substitute grep for graph analysis.
- **MUST analyze graph changes before committing.** Use `detect_changes({scope: "all"})` (MCP) or `node .gitnexus/run.cjs detect-changes --scope all --repo .` (CLI fallback). `partial: true` or `truncated: true` is not a clean check — a zero means unseen, not unaffected; re-run it. For regression review: `detect_changes({scope: "compare", base_ref: "main"})` or `node .gitnexus/run.cjs detect-changes --scope compare --base-ref "main" --repo .`.
- MUST warn on HIGH/CRITICAL `risk` pre-edit; never use `riskSharedAxes` to waive a HIGH/CRITICAL `risk` warning. Compare File/symbol: MCP File omits axes; Graph-RAG expands File.
- **MUST treat `risk: UNKNOWN` as unresolved, not as low.** An empty caller set is not evidence the symbol is unused — it can also mean the callers are not resolvable by the index (plain-object property access, dynamic dispatch, cross-language calls). `impact` pairs `UNKNOWN` with a `riskNote` saying so. Confirm with a text search before treating the symbol as safe to change or delete; do not proceed on the strength of a zero.
- **MUST use `query({search_query: "concept"})` for concepts/flows, `context({name: "symbolName"})` for a named symbol, or `impact` for blast radius, on read-only callers, dependencies, imports, or execution flow.** Graph first; text search only for empty/`UNKNOWN`/literals.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method before MCP/CLI impact analysis.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis, and never read `UNKNOWN` as an all-clear — it means the walk could not answer, which is the one verdict that requires confirming by other means.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit before MCP/CLI graph change analysis.

## Resources

| Resource | Use for |
| --- | --- |
| `gitnexus://repo/marketplace/context` | Codebase overview, check index freshness |
| `gitnexus://repo/marketplace/clusters` | All functional areas |
| `gitnexus://repo/marketplace/processes` | All execution flows |
| `gitnexus://repo/marketplace/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
| --- | --- |
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->

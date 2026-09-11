<!-- codekit:initialize:project-context:start -->
## Repository Guide


### Overview

CodeKit 是一个面向 Claude、OpenCode、Codex、Qoder 和 Qoder CN 的 AI 开发插件包，仓库包含七个工作流技能、安装与卸载工具、平台插件元数据以及初始化索引与上下文脚本。

### Structure

- `skills/`: 包含 bugfix、code-impl、code-review、initialize、product-doc、release-doc、technical-doc 七个以 SKILL.md 和 openai.yaml 为核心的工作流技能。
- `skills/initialize/`: 初始化工作流定义、Context Scanner/GitNexus/Graphify 角色、标准模板、控制器脚本和初始化测试。
- `skills/initialize/scripts/`: 初始化状态机的准备、门禁验证、结果 reconcile、GitNexus 适配与公共辅助脚本。
- `skills/initialize/tests/`: 覆盖初始化脚本、worker contract、路径边界、结果门禁和端到端状态流转的 Python 测试。
- `scripts/`: 安装器、卸载器、组件注册与选择、插件 manifest/技能布局校验、全局 agent 管理和版本同步脚本。
- `.claude-plugin/`: Claude 插件及 marketplace 的 JSON 元数据。
- `.codex-plugin/`: Codex 插件及 marketplace 的 JSON 元数据。
- `.opencode/`: OpenCode npm 包元数据及动态注册 skills 命令的插件实现。
- `.qoder-plugin/`: Qoder / Qoder CN 插件 manifest；两个发行版共用同一个用户级配置根，安装一次即对 CLI 与桌面 IDE 同时生效。
- `.standard/`: 当前仓库已存在的全局规范和 Python 技术栈规范索引及正文。
- `install.sh`: macOS/Linux 安装或升级入口，调用 scripts 下的安装编排逻辑。
- `uninstall.sh`: macOS/Linux 卸载入口，调用独立卸载编排逻辑。
- `install.ps1` / `install.cmd`: Windows 安装或升级入口（`install.cmd` 是 PowerShell 的 CMD 包装），只做 preflight 检测后转交同一套 Python 编排逻辑。
- `uninstall.ps1` / `uninstall.cmd`: Windows 卸载入口。
- `scripts/installer_platform.py`: 唯一的平台差异来源（npm 前缀、venv 布局、状态目录、临时目录、命令入口、特权与交互判定），安装、卸载、状态检测都读它。
- `.agents/plugins/marketplace.json`: Codex 实际读取的 marketplace 清单（`.codex-plugin/` 只提供插件自身元数据，两者都必需）。

### Commands and Verification

- 校验每个技能的 SKILL.md、根级 openai.yaml、agent 元数据和资源引用。: `python3 scripts/validate-skill-layout.py` (evidence: `scripts/validate-skill-layout.py`, `skills/README.md`, `lefthook.yml`)
- 校验 Claude、Codex、OpenCode、Qoder 插件 manifest 的 JSON、名称、版本、marketplace 源和入口文件。: `python3 scripts/validate-plugin-manifests.py` (evidence: `scripts/validate-plugin-manifests.py`, `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`, `.opencode/package.json`, `.qoder-plugin/plugin.json`)
- 校验安装器组件注册表的 ID、分组顺序和描述字段。: `python3 scripts/component-registry.py --check` (evidence: `scripts/component-registry.py`, `INSTALL.md`)
- 运行初始化控制器、结果契约、路径边界和端到端状态流转测试。: `python3 skills/initialize/tests/test_initialize_scripts.py` (evidence: `skills/initialize/tests/test_initialize_scripts.py`, `skills/initialize/scripts/controller.py`)
- 运行 bugfix 技能脚本和提案契约测试。: `python3 skills/bugfix/tests/test_bugfix_skill_scripts.py` (evidence: `skills/bugfix/tests/test_bugfix_skill_scripts.py`, `skills/bugfix/scripts/validate_bugfix_proposal.py`)
- 运行 product-doc 技能脚本测试。: `python3 skills/product-doc/tests/test_product_doc_skill_scripts.py` (evidence: `skills/product-doc/tests/test_product_doc_skill_scripts.py`, `skills/product-doc/scripts/validate_prd.py`)
- 运行 technical-doc 控制器测试。: `python3 skills/technical-doc/tests/test_tech_doc_controller.py` (evidence: `skills/technical-doc/tests/test_tech_doc_controller.py`, `skills/technical-doc/scripts/tech_doc_controller.py`)
- 运行 technical-doc 模板契约测试。: `python3 skills/technical-doc/tests/test_template_contract.py` (evidence: `skills/technical-doc/tests/test_template_contract.py`, `skills/technical-doc/references/technical-doc-template.md`)
- 从本地仓库安装或升级选定的 CodeKit 组件。: `bash install.sh install -y`（Windows: `install.cmd install -y`） (evidence: `install.sh`, `install.cmd`, `install.ps1`, `README.md`, `INSTALL.md`)
- 卸载选定的 CodeKit 组件。: `bash uninstall.sh -y`（Windows: `uninstall.cmd -y`） (evidence: `uninstall.sh`, `uninstall.cmd`, `uninstall.ps1`, `README.md`, `INSTALL.md`)
- 把 plugin-version.json 的版本同步到各平台 manifest。: `python3 scripts/sync-plugin-version.py` (evidence: `scripts/sync-plugin-version.py`, `plugin-version.json`, `lefthook.yml`)
- 运行安装器、共享存储适配层和 agent 同步的回归测试（pre-push 门禁）。: `python3 -m unittest discover -s scripts -p 'test_*.py'` (evidence: `scripts/test_installer_qoder.py`, `scripts/test_installer_sources.py`, `scripts/test_installer_agents.py`, `lefthook.yml`)
- 构建 GitNexus 仓库分析和技能索引。: `gitnexus analyze --skills --verbose` (evidence: `GUIDE.md`, `skills/initialize/SKILL.md`)
- 使用代码模式提取 Graphify 知识图谱到仓库输出目录。: `graphify extract . --out . --code-only` (evidence: `skills/initialize/SKILL.md`, `skills/initialize/agents/initialize-graphify.md`)
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

This project is indexed by GitNexus as **CodeKit** (2479 symbols, 5203 relationships, 185 execution flows).

> Index stale? Run `node .gitnexus/run.cjs analyze --index-only` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? Bootstrap with `npx`, `bunx`, or `pnpm dlx` — e.g. `bunx gitnexus@latest analyze` (npm 11 npx crash; #1939).

## Always Do

- **MUST run impact before editing.** Use `impact({target: "symbolName", direction: "upstream"})` or `node .gitnexus/run.cjs impact "symbolName" --direction upstream --repo .`; report callers, processes, and risk. Never substitute grep for graph analysis.
- **MUST analyze graph changes before committing.** Use `detect_changes({scope: "all"})` (MCP) or `node .gitnexus/run.cjs detect-changes --scope all --repo .` (CLI fallback). `partial: true` or `truncated: true` is not a clean check — a zero means unseen, not unaffected; re-run it. For regression review: `detect_changes({scope: "compare", base_ref: "master"})` or `node .gitnexus/run.cjs detect-changes --scope compare --base-ref "master" --repo .`.
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
| `gitnexus://repo/CodeKit/context` | Codebase overview, check index freshness |
| `gitnexus://repo/CodeKit/clusters` | All functional areas |
| `gitnexus://repo/CodeKit/processes` | All execution flows |
| `gitnexus://repo/CodeKit/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
| --- | --- |
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus-cli/SKILL.md` |
| Work in the Scripts area (476 symbols) | `.claude/skills/gitnexus-area-scripts/SKILL.md` |
| Work in the Tests area (202 symbols) | `.claude/skills/gitnexus-area-tests/SKILL.md` |

<!-- gitnexus:end -->

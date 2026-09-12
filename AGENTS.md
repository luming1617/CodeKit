<!-- codekit:initialize:project-context:start -->
## Repository Guide


### Overview

CodeKit 是以插件形式分发给 Claude、Codex、OpenCode、Qoder 与 Qoder CN 的 AI 开发套件仓库：7 个 AI 工作流技能（skills/）、一套纯 Python 的安装/卸载/校验工具链（scripts/ 与 install.sh、install.ps1 入口）和四份平台插件元数据，仓库自身没有运行时服务、数据库或部署流水线，只有 lefthook.yml 定义的 git 钩子门禁。

### Structure

- `skills/`: 七个 AI 工作流技能目录（bugfix、code-impl、code-review、initialize、product-doc、release-doc、technical-doc）；实测 7 个 SKILL.md、7 个同级 openai.yaml 与 26 个 agents/*.md 角色定义，目录约定见 skills/README.md。
- `skills/initialize/`: 初始化工作流本体：SKILL.md（Phase 0-4 流程与参数）、openai.yaml、agents/（initialize-context-scanner 与 initialize-graphify 两个 subagent 角色正文）、references/（context-files、worker-contract、orchestration、tool-adapters、final-report 五份契约）与 standard/（01-global、02-stack、03-project 规范模板）。
- `skills/initialize/scripts/`: initialize 状态机的确定性实现：_common.py（仓库验证白名单 VALIDATION_PROGRAMS、禁用 shell 语法正则、错误码枚举、托管块标记、git 基线）、prepare.py（Phase 0 冻结基线与 GitNexus/Graphify 能力探测）、controller.py（start/analyze/gate/reconcile/finish CLI）、reconcile.py（上下文 contract 校验与 AGENTS.md/CLAUDE.md 渲染）、validate-result.py（Worker 结果与 artifact 门禁、自跑仓库验证）、run-gitnexus.py 与 gitnexus-step.py（GitNexus argv 拼装与全局 registry 复核）。
- `skills/initialize/tests/`: 控制器、worker contract、路径边界、结果门禁与端到端状态流转测试；在系统临时目录中 git init 仓库并用隔离的 GITNEXUS_HOME 运行。
- `skills/bugfix/`: Bug 诊断技能：bugfix-investigator/challenger/implementer 三个 agent、scripts/bugfix_state.py 与 scripts/validate_bugfix_proposal.py 提案门禁，以及 tests/test_bugfix_skill_scripts.py。
- `skills/code-impl/`: 需求实现技能：impl-planner/implementer/debugger 三个 agent、scripts/code_impl_controller.py 任务 DAG 编排控制器，以及 tests/test_code_impl_controller.py。
- `skills/code-review/`: 多维代码审查技能：review-context/review-evidence/review-failure/review-specialist/review-traceability 五个 agent、scripts/freeze_review_snapshot.py 快照与 scripts/validate_review_report.py 报告门禁，以及 tests/test_review_contracts.py。
- `skills/product-doc/`: PRD 生成与审查技能：product-extractor/product-writer/product-reviewer 三个 agent、scripts/review_state.py 与 scripts/validate_prd.py 契约校验，以及 tests/test_product_doc_skill_scripts.py。
- `skills/release-doc/`: 发布方案技能：release-critic/dependencies/refiner/reviewer/writer 五个 agent 与 references/（release-doc-template.md、release-review-contract.md、check-standard.md、release-reviewer-prompt.md）；本技能不含 scripts/ 与 tests/，没有本地可执行门禁。
- `skills/technical-doc/`: 技术方案技能：tech-architect/tech-context/tech-critic/tech-refiner/tech-reviewer 五个 agent、scripts/tech_doc_controller.py 阶段控制器，以及 tests/test_tech_doc_controller.py 与 tests/test_template_contract.py（模板与校验器字段一致性）。
- `skills/_shared/prompts/`: 跨技能共享提示片段：path-convention.md（SKILL_DIR/SKILLS_ROOT 绝对路径解析与派发清单）与 subagent-mandate.md，被各技能 SKILL.md 以 _shared/prompts/ 路径引用。
- `scripts/`: 纯 Python 工程工具链：installer.py（约 1915 行的安装主入口）与 installer_config/platform/state/sources/agents/qoder.py 分层模块、uninstaller.py、component-registry.py（组件注册表与 --check 自校验）、component-selector.py、manage-global-agents.py、sync-plugin-version.py、validate-skill-layout.py、validate-plugin-manifests.py，以及 4 个 test_installer_*.py 回归测试。
- `install.sh`: POSIX 安装入口：只做系统依赖检测，最后 exec python3 调用 scripts/installer.py；Windows 等价入口是 install.ps1（launcher 用 py -3 而非 python3，并强制 -X utf8）与 6 行的 install.cmd 包装；uninstall.sh、uninstall.ps1、uninstall.cmd 是对应卸载入口，调用 scripts/uninstaller.py。
- `.claude-plugin/`: Claude 插件元数据：plugin.json 与 marketplace.json；同类的 .qoder-plugin/plugin.json 是 Qoder 与 Qoder CN 共用的 manifest（两个发行版共享同一用户级配置根）。
- `.codex-plugin/`: Codex 插件自身元数据（plugin.json、marketplace.json）；Codex 实际解析的 marketplace 条目位于 .agents/plugins/marketplace.json，两处文件同时必需。
- `.opencode/`: OpenCode 分发：package.json（name codekit、version 1.0.0、type module、main plugins/codekit.js）、opencode.json 与 plugins/codekit.js、plugins/graphify.js，用于动态注册 skills 命令。
- `lefthook.yml`: git 钩子门禁定义：pre-commit 先同步 plugin-version.json 版本进各 manifest 再校验 manifest；pre-push 校验五个 marketplace/plugin 文件存在、技能布局、安装器 unittest 套件与 skills/*/tests 全部套件（钩子内部以 python3 调用脚本）。
- `.standard/`: 本 run 的 Phase 0 刚写入的规范库：01-global/（00-instructions.md、api.md、git.md、security.md、unit-test.md）与 02-stack/python/（00-instructions.md、01-test.md）；模板来源是 skills/initialize/standard/，当前尚未被 git 跟踪。
- `AGENTS.md`: 上下文文件（与 CLAUDE.md 成对）：由 CodeKit 托管块管理、主 Agent 是唯一写者；两者在本 run 的 Phase 0 基线中已存在，其中 AGENTS.md 处于 dirty 状态且尚未按本次扫描结果更新。

### Commands and Verification

- 校验每个技能的 SKILL.md、openai.yaml、agent 元数据与资源引用完整性（脚本只读取 skills/ 并打印结果，实测入口为 225 行纯校验）: `py -3 scripts/validate-skill-layout.py` (evidence: `scripts/validate-skill-layout.py`, `lefthook.yml`, `skills/README.md`)
- 校验 Claude、Codex、OpenCode、Qoder 插件 manifest 的 JSON 语法、插件名、版本一致性与被引用文件是否存在: `py -3 scripts/validate-plugin-manifests.py` (evidence: `scripts/validate-plugin-manifests.py`, `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`, `.opencode/package.json`, `.qoder-plugin/plugin.json`, `lefthook.yml`)
- 校验安装器组件注册表的 ID、分组顺序与渠道字段（--check 在 validate_registry 后直接返回，不导出注册表）: `py -3 scripts/component-registry.py --check` (evidence: `scripts/component-registry.py`, `INSTALL-STRATEGY.md`, `INSTALL.md`)
- 运行安装器、平台层、全局 agent 同步与 Qoder 安装的回归测试（pre-push 门禁等价；测试创建系统临时目录并 fork sys.executable 子进程）: `py -3 -m unittest discover -s scripts` (evidence: `scripts/test_installer_agents.py`, `scripts/test_installer_contracts.py`, `scripts/test_installer_qoder.py`, `scripts/test_installer_sources.py`, `lefthook.yml`)
- 运行 initialize 控制器、worker contract、路径边界、结果门禁与端到端状态流转测试（会在临时目录 git init 仓库）: `py -3 -m unittest discover -s skills/initialize/tests` (evidence: `skills/initialize/tests/test_initialize_scripts.py`, `skills/initialize/scripts/controller.py`, `lefthook.yml`)
- 运行 bugfix 技能脚本与提案契约测试: `py -3 -m unittest discover -s skills/bugfix/tests` (evidence: `skills/bugfix/tests/test_bugfix_skill_scripts.py`, `skills/bugfix/scripts/validate_bugfix_proposal.py`, `skills/bugfix/scripts/bugfix_state.py`)
- 运行 code-impl 任务编排控制器测试: `py -3 -m unittest discover -s skills/code-impl/tests` (evidence: `skills/code-impl/tests/test_code_impl_controller.py`, `skills/code-impl/scripts/code_impl_controller.py`)
- 运行 code-review 快照与报告契约测试: `py -3 -m unittest discover -s skills/code-review/tests` (evidence: `skills/code-review/tests/test_review_contracts.py`, `skills/code-review/scripts/freeze_review_snapshot.py`, `skills/code-review/scripts/validate_review_report.py`)
- 运行 product-doc 技能脚本测试（PRD 校验与审查状态）: `py -3 -m unittest discover -s skills/product-doc/tests` (evidence: `skills/product-doc/tests/test_product_doc_skill_scripts.py`, `skills/product-doc/scripts/validate_prd.py`, `skills/product-doc/scripts/review_state.py`)
- 运行 technical-doc 阶段控制器与模板契约测试: `py -3 -m unittest discover -s skills/technical-doc/tests` (evidence: `skills/technical-doc/tests/test_tech_doc_controller.py`, `skills/technical-doc/tests/test_template_contract.py`, `skills/technical-doc/references/technical-doc-template.md`)
- 把 plugin-version.json 的版本同步进四个平台 manifest（会改写 .claude-plugin、.codex-plugin、.opencode、.qoder-plugin 下的文件，属于仓库内写入，故不作为无副作用验证）: `py -3 scripts/sync-plugin-version.py` (evidence: `scripts/sync-plugin-version.py`, `plugin-version.json`, `lefthook.yml`)
- Windows 下从本地仓库安装或升级选中的 CodeKit 组件（写用户级配置目录、可能联网与安装 CLI，仅在人工明确要求时执行）: `install.cmd install -y` (evidence: `install.cmd`, `install.ps1`, `scripts/installer.py`, `INSTALL.md`)
- 卸载选中的 CodeKit 组件（删除用户级配置与插件目录）: `uninstall.cmd -y` (evidence: `uninstall.cmd`, `uninstall.ps1`, `scripts/uninstaller.py`, `README.md`)
- 构建 GitNexus 代码索引与 GitNexus skill 产物（参数来自 gitnexus-step.py 的 ANALYZE_BASE；写 .gitnexus/ 与全局 registry，gitnexus 不在仓库验证白名单内，本 run 由 controller.py analyze 派发）: `gitnexus analyze --skills --skip-agents-md --verbose` (evidence: `skills/initialize/scripts/gitnexus-step.py`, `skills/initialize/SKILL.md`, `GUIDE.md`)
- 用 Graphify 以本地 AST 模式抽取代码知识图谱到 graphify-out/（外部工具，不在仓库验证白名单内，由 initialize-graphify subagent 执行）: `graphify extract . --out . --code-only` (evidence: `skills/initialize/references/tool-adapters.md`, `skills/initialize/agents/initialize-graphify.md`)
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

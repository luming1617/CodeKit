<!-- codekit:initialize:project-context:start -->
## Repository Guide


### Overview

CodeKit 是以插件形式分发给 Claude、Codex、OpenCode、Qoder 与 Qoder CN 的 AI 开发套件仓库：7 个 AI 工作流技能目录（skills/，实测 7 个 SKILL.md、7 个同级 openai.yaml、26 个 agents/*.md）、一套仅依赖 Python 标准库的安装/卸载/校验工具链（scripts/ 与 install.sh、install.ps1、install.cmd 入口）和平台插件元数据目录；仓库自身没有运行时服务、数据库、依赖清单或部署流水线，唯一的门禁是 lefthook.yml 定义的 git 钩子。

### Structure

- `skills/`: 七个 AI 工作流技能目录（bugfix、code-impl、code-review、initialize、product-doc、release-doc、technical-doc），每个技能含 SKILL.md 与同级 openai.yaml；目录约定见 skills/README.md，实测校验输出为 7 skills / 26 agents。
- `skills/initialize/`: 初始化技能本体：SKILL.md（Phase 0-4 流程）、openai.yaml、agents/（initialize-context-scanner 与 initialize-graphify 两个 subagent 角色正文）、references/（context-files、worker-contract、orchestration、tool-adapters、final-report 五份契约）、standard/（01-global 与 02-stack 多语言规范模板）与 tests/。
- `skills/initialize/scripts/`: initialize 状态机的确定性实现，共 7 个脚本：_common.py（VALIDATION_PROGRAMS 白名单、VALIDATION_FORBIDDEN 禁用 shell 正则、错误码枚举、托管块标记、git 基线与 registry 探测）、prepare.py（Phase 0 冻结基线与工具探测）、controller.py（start/gate/analyze/reconcile/finish/abort 六个子命令）、reconcile.py（context 契约校验与 AGENTS.md/CLAUDE.md 渲染）、validate-result.py（worker 结果与 artifact 门禁、亲自执行 required_for_initialize 命令）、run-gitnexus.py 与 gitnexus-step.py。
- `skills/initialize/tests/`: 控制器、worker 契约、路径边界、结果门禁与端到端状态流转测试（test_initialize_scripts.py）；实测其在 tempfile.TemporaryDirectory 中 git init 仓库，并用隔离的 GITNEXUS_HOME 环境变量运行，不写本仓库。
- `skills/bugfix/`: Bug 诊断技能：agents/ 三个角色、scripts/bugfix_state.py 与 scripts/validate_bugfix_proposal.py 提案门禁、references/、assets/ 与 tests/test_bugfix_skill_scripts.py。
- `skills/code-impl/`: 需求实现技能：agents/ 三个角色（Planner、Implementer、Debugger）、scripts/code_impl_controller.py 任务 DAG 编排、references/ 与 tests/test_code_impl_controller.py。
- `skills/code-review/`: 多维代码审查技能：agents/ 五个角色、scripts/freeze_review_snapshot.py 快照与 scripts/validate_review_report.py 报告门禁、references/ 与 tests/test_review_contracts.py。
- `skills/product-doc/`: PRD 生成与审查技能：agents/ 三个角色、scripts/review_state.py 与 scripts/validate_prd.py 契约校验、references/ 与 tests/test_product_doc_skill_scripts.py。
- `skills/release-doc/`: 发布方案技能：agents/ 五个角色与 references/ 模板；实测本技能目录没有 scripts/ 也没有 tests/，即没有本地可执行门禁脚本。
- `skills/technical-doc/`: 技术方案技能：agents/ 五个角色、scripts/tech_doc_controller.py 阶段控制器、references/ 模板与 tests/（test_tech_doc_controller.py、test_template_contract.py）。
- `skills/_shared/prompts/`: 跨技能共享提示片段：path-convention.md（SKILL_DIR/SKILLS_ROOT 解析与派发清单）、subagent-mandate.md、output-language.md（回复语言规则），被各技能 SKILL.md 引用。
- `scripts/`: 纯 Python 工程工具链：installer.py（安装状态机主编排，1915 行）、uninstaller.py（卸载编排，复用 installer 助手）、component-registry.py（组件注册表与 --check）、component-selector.py（终端交互选择器，UI 写 /dev/tty、结果写 stdout）、installer_agents/config/platform/qoder/sources/state 六个分层模块、manage-global-agents.py（全局 Markdown subagent 安装卸载）、sync-plugin-version.py、validate-skill-layout.py、validate-plugin-manifests.py，以及 4 个 test_installer_*.py。
- `install.sh`: POSIX 安装入口：只做系统依赖检测（git、Node.js ≥ 24、Python3 ≥ 3.10、npm、curl/wget），最后 exec python3 调用 scripts/installer.py；Windows 等价入口为 install.ps1（launcher 用 py -3 并强制 -X utf8）与转发到它的 6 行 install.cmd；uninstall.sh、uninstall.ps1、uninstall.cmd 是对应卸载入口。
- `.claude-plugin/`: Claude 插件元数据：plugin.json 与 marketplace.json（marketplace 条目 source 必须为 ./，且需含 codekit 条目与非空 description）。
- `.codex-plugin/`: Codex 插件元数据：plugin.json 与 marketplace.json，与 .claude-plugin/ 同构，由 validate-plugin-manifests.py 校验名称与版本一致性。
- `.qoder-plugin/`: Qoder 与 Qoder CN 共用的插件 manifest：实测目录内只有 plugin.json，没有 marketplace.json（两个发行版共享同一用户级配置根）。
- `lefthook.yml`: git 钩子门禁：pre-commit 先运行 sync-plugin-version.py 并 git add 版本文件（权威清单在 sync-plugin-version.py 的 TARGET_FILES）、再校验 manifest；pre-push 校验技能布局、安装器 unittest 套件与 skills/*/tests 全部套件；marketplace/plugin 文件的存在性检查已由 validate-plugin-manifests.py 负责（钩子里的内联重复检查已删除）；钩子按平台解析解释器（Windows 用 `py -3`，其余用 `python3`）。
- `.standard/`: 本 run Phase 1 刚写入的规范库：01-global/（00-instructions.md、api.md、git.md、security.md、unit-test.md）与 02-stack/python/（00-instructions.md、01-test.md）；两个 00-instructions.md 为规范索引，带托管 index-files 块，模板来源是 skills/initialize/standard/。
- `README.md`: 套件定位与一键安装入口说明；配套文档为 GUIDE.md（268 行使用指南）与 INSTALL-STRATEGY.md（302 行组件安装策略与安装行为说明），skills/README.md（61 行技能目录约定）。
- `AGENTS.md`: CodeKit 托管的上下文文件（与 CLAUDE.md 成对，主 Agent 是唯一写者）；两者在本 run Phase 0 基线中均已存在且 dirty=false，其中既有托管块内容与当前工作树不符，本次扫描未采信（见 warnings）。

### Commands and Verification

- 校验每个技能的 SKILL.md、openai.yaml、agent 元数据与资源引用完整性（脚本只读取 skills/ 并打印结果，实测输出 validated 7 CodeKit skills (26 agents, ...)，退出码 0）: `py -3 scripts/validate-skill-layout.py` (evidence: `scripts/validate-skill-layout.py`, `lefthook.yml`, `skills/README.md`)
- 校验安装器组件注册表的 ID、分组顺序与渠道字段（--check 在 validate_registry 通过后直接 return 0，不打印也不导出注册表，实测退出码 0）: `py -3 scripts/component-registry.py --check` (evidence: `scripts/component-registry.py`, `INSTALL-STRATEGY.md`)
- 校验 Claude、Codex、Qoder 与 OpenCode 插件 manifest 的 JSON 语法、插件名、版本一致性与被引用文件是否存在（本 run 实测失败：missing manifest: .opencode\package.json，退出码 1，根因是当前分支把 .opencode/ 整体暂存删除，而非 initialize 造成，故不作为本次初始化的必过门禁）: `py -3 scripts/validate-plugin-manifests.py` (evidence: `scripts/validate-plugin-manifests.py`, `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`, `.qoder-plugin/plugin.json`, `lefthook.yml`)
- 运行安装器、平台层、全局 agent 同步与 Qoder 安装的回归测试（pre-push test-installer 的等价命令；测试会在系统临时目录建目录并 fork sys.executable 子进程，属仓库外写入，故未标记为初始化必过验证，本 run 也未实测其通过状态）: `py -3 -m unittest discover -s scripts` (evidence: `scripts/test_installer_agents.py`, `scripts/test_installer_contracts.py`, `scripts/test_installer_qoder.py`, `scripts/test_installer_sources.py`, `lefthook.yml`)
- 运行 initialize 控制器、worker contract、路径边界、结果门禁与端到端状态流转测试（在系统临时目录 git init 仓库并使用隔离 GITNEXUS_HOME；会 fork git 子进程，故不作为无副作用的初始化必过验证）: `py -3 -m unittest discover -s skills/initialize/tests` (evidence: `skills/initialize/tests/test_initialize_scripts.py`, `skills/initialize/scripts/controller.py`, `lefthook.yml`)
- 运行 bugfix 技能脚本与提案契约测试: `py -3 -m unittest discover -s skills/bugfix/tests` (evidence: `skills/bugfix/tests/test_bugfix_skill_scripts.py`, `skills/bugfix/scripts/validate_bugfix_proposal.py`, `skills/bugfix/scripts/bugfix_state.py`)
- 运行 code-impl 任务编排控制器测试: `py -3 -m unittest discover -s skills/code-impl/tests` (evidence: `skills/code-impl/tests/test_code_impl_controller.py`, `skills/code-impl/scripts/code_impl_controller.py`)
- 运行 code-review 快照与报告契约测试: `py -3 -m unittest discover -s skills/code-review/tests` (evidence: `skills/code-review/tests/test_review_contracts.py`, `skills/code-review/scripts/freeze_review_snapshot.py`, `skills/code-review/scripts/validate_review_report.py`)
- 运行 product-doc 技能脚本测试（PRD 校验与审查状态机）: `py -3 -m unittest discover -s skills/product-doc/tests` (evidence: `skills/product-doc/tests/test_product_doc_skill_scripts.py`, `skills/product-doc/scripts/validate_prd.py`, `skills/product-doc/scripts/review_state.py`)
- 运行 technical-doc 阶段控制器与模板契约测试: `py -3 -m unittest discover -s skills/technical-doc/tests` (evidence: `skills/technical-doc/tests/test_tech_doc_controller.py`, `skills/technical-doc/tests/test_template_contract.py`, `skills/technical-doc/scripts/tech_doc_controller.py`)
- 把 plugin-version.json 的版本同步进四个平台 manifest（会改写 .claude-plugin、.codex-plugin、.opencode、.qoder-plugin 下的文件并属于仓库内写入，因此不作为无副作用验证；且当前 .opencode/ 已被暂存删除）: `py -3 scripts/sync-plugin-version.py` (evidence: `scripts/sync-plugin-version.py`, `plugin-version.json`, `lefthook.yml`)
- Windows 下从本地仓库安装或升级选中的 CodeKit 组件（写用户级配置目录、可能联网与安装第三方 CLI，仅在人工明确要求时执行）: `install.cmd install -y` (evidence: `install.cmd`, `install.ps1`, `scripts/installer.py`, `INSTALL-STRATEGY.md`)
- 卸载选中的 CodeKit 组件（删除用户级配置与插件目录，属外部副作用）: `uninstall.cmd -y` (evidence: `uninstall.cmd`, `uninstall.ps1`, `scripts/uninstaller.py`, `README.md`)
- 构建 GitNexus 代码索引与 GitNexus skill 产物（写 .gitnexus/ 与全局 registry；gitnexus 不在仓库验证白名单内，本 run 由 controller.py analyze 派发执行，实测本次已产出 gitnexus-result.json）: `gitnexus analyze --skills --skip-agents-md --verbose` (evidence: `skills/initialize/scripts/gitnexus-step.py`, `skills/initialize/SKILL.md`, `skills/initialize/references/tool-adapters.md`)
- 用 Graphify 以本地 AST 模式抽取代码知识图谱到 graphify-out/（外部工具，不在仓库验证白名单内，由 initialize-graphify subagent 执行，实测本次已产出 graphify-result.json）: `graphify extract . --out . --code-only` (evidence: `skills/initialize/references/tool-adapters.md`, `skills/initialize/agents/initialize-graphify.md`)
<!-- codekit:initialize:project-context:end -->

<!-- codekit:initialize:standards:start -->
## AI 开发规范

规范目录：`.standard/`。这是规范索引，不要预先加载全部规范。
优先级：Security > Project > Stack > Global。

回复语言：面向用户的说明、状态、判断和报告默认使用简体中文，用户只用 `@skill`/斜杠命令触发、本轮没有自然语言时同样适用；代码、命令、路径、参数、字段名与状态字面量保持原文，产物文件正文按各自模板与契约的语言要求执行。

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

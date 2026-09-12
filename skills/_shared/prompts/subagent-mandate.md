# Subagent 强制调用规则（CodeKit Skill 共享规范）

> 唯一事实源。各 Skill 的 `SKILL.md` 只声明自己的角色清单与不可降级的门禁，派发方式按本文档执行。

## 装配现实（先读这段，避免按不存在的语法派发）

CodeKit 的角色文件**不通过 plugin manifest 分发**：`.claude-plugin/plugin.json` 与 `.codex-plugin/plugin.json` 只声明插件本身，`skills/<skill>/agents/*.md` 由 `scripts/manage-global-agents.py` 安装成**全局裸名**角色：

| 宿主 | 安装位置 |
|------|----------|
| Claude Code | `~/.claude/agents/<name>.md` |
| OpenCode | `~/.config/opencode/agents/<name>.md` |
| Codex | `~/.codex/agents/<name>.toml` |

因此：角色标识就是裸名 `<name>`（例如 `tech-context`）。**不存在 `@agent-<plugin>:<name>` 这类插件作用域角色名**，不要按它派发，也不要声称“若来自插件则改用作用域名”。

Skill 自身的调用名按宿主区分：

| 宿主 | Skill 调用形式 |
|------|----------------|
| Claude Code | `/codekit:<skill> <args>` |
| Codex | `$<skill> <args>` |
| OpenCode | `/<skill> <args>` |

## 硬性门禁

列出的角色都是**硬性阶段门禁，不是可选建议**。进入对应阶段前，主 Agent 必须创建指定名称的独立 Sub-agent，并记录调用名称、输入快照（digest）、独立会话标识和返回状态；本 Skill 自带确定性控制器时，这些登记必须写入状态文件，其退出码优先于主 Agent 的自述。

主 Agent 自己完成该阶段、普通串行自检、或只写“建议调用”均不合规。无法创建指定 Sub-agent 时，阶段状态必须为 `BLOCKED`，不得伪造角色产出；若本 Skill 定义了降级模式，则必须显式登记降级并降低交付状态，不得静默降级。

**该门禁只约束主 Agent（Orchestrator）**：被派发的角色 Sub-agent 一律不再派发下一级 Sub-agent，也绝不因自身缺少派发能力而返回 `BLOCKED`；它只需按自己的角色契约完成本阶段并返回结构化结果。

## 输出语言

唯一事实源是 `$SKILLS_ROOT/_shared/prompts/output-language.md`；派发任何角色前必须读取该文件，并按其中的规则段执行。

- **主 Agent 必须把规则段原文追加进每个派发正文。** 角色文件里的语言条款由 `scripts/manage-global-agents.py` 在安装时注入，但插件缓存、旧版本角色文件和宿主内置的通用 subagent 都可能不带该条款——只在 SKILL.md 里声明不足以约束到子代理，因为子代理看不到 SKILL.md。这条要求不属于上文禁止注入的“派发门禁”。
- 语言规则只约束自然语言。契约字段名、枚举与状态字面量（如 `BLOCKED`、`CONFIRMED`、`APPROVE`）、JSON key、脚本参数、退出码一律保持英文，门禁脚本按这些标识符判定，翻译会直接把阶段门禁打挂。
- 产物文件（PRD、TDD、RPD、审查报告、Worker result JSON）的正文语言以对应模板与契约的要求为准，不以本节的会话语言为准；两者冲突时以模板与契约为准。

## 按宿主派发

- **Claude Code：** 用宿主的子代理派发能力，把角色名精确指定为全局安装的裸名 `<name>`；不要接受宿主自动路由到别的角色。`--agent` / `agent` 是把**整个会话**切换为该角色，不能替代 Skill 内部的子代理派发。
- **Codex：** 把 `Spawn one subagent named <name> ...`（或等价的显式 `spawn_agent` 调用，`agent_type` 填角色名）作为**主 Agent 发给宿主的派发指令**，而不是写进子代理的任务正文。传给子代理的 `message` 只包含该角色自身的任务与契约（Input Contract、冻结快照 digest、要读取的角色文件与 `SKILL_DIR` 绝对路径等）；**不得出现任何“再派发 / Spawn one subagent”字样，也不要连同 `fork_context` 把主 Agent 的强制派发门禁注入子代理**——否则子代理会因无法满足只属于主 Agent 的门禁而返回 `BLOCKED`。按阶段顺序等待全部必需结果；不得依赖自动路由，也不得只说“请分析/请设计/请审查”。
- **OpenCode：** 通过命令或调度入口指定 `agent: "<name>"` 并显式设置 `subagent: true`，强制创建后台子会话；旧版仅支持 `subtask: true` 时使用该兼容字段。仅依赖 `mode: subagent` 或在当前主会话内执行，不满足强制调用要求。

## 复用与轮次

需要“每轮全新实例”的角色（终审 Reviewer、交叉验证 Evidence Verifier 等），同一轮内不得复用实例；并行派发时每一路使用独立会话并等待返回。合并结果前必须消解冲突、保留各自证据来源，并标出无法裁决的事实。

# Subagent 强制调用规则（CodeKit Skill 共享规范）

> 唯一事实源。各 Skill 的 `SKILL.md` 只声明自己的角色清单与不可降级的门禁，派发方式按本文档执行。

## 装配现实（先读这段，避免按不存在的语法派发）

CodeKit 的角色文件**不通过 plugin manifest 分发**：`.claude-plugin/plugin.json` 与 `.codex-plugin/plugin.json` 只声明插件本身，`skills/<skill>/agents/*.md` 由 `scripts/manage-global-agents.py` 安装成**全局裸名**角色：

| 宿主 | 安装位置 |
|------|----------|
| Claude Code | `~/.claude/agents/<name>.md` |
| OpenCode | `~/.config/opencode/agents/<name>.md` |
| Codex | `~/.codex/agents/<name>.toml` |
| Qoder | `~/.qoder/agents/<name>.md`（CLI 与桌面 IDE 共享同一个配置根） |
| Qoder CN | `~/.qoder-cn/agents/<name>.md` |

因此：角色标识就是裸名 `<name>`（例如 `tech-context`）。**不存在 `@agent-<plugin>:<name>` 这类插件作用域角色名**，不要按它派发，也不要声称“若来自插件则改用作用域名”。

Skill 自身的调用名按宿主区分：

| 宿主 | Skill 调用形式 |
|------|----------------|
| Claude Code | `/codekit:<skill> <args>` |
| Codex | `$<skill> <args>` |
| OpenCode | `/<skill> <args>` |
| Qoder | `/codekit:<skill> <args>` |
| Qoder CN | `/codekit:<skill> <args>` |

## 硬性门禁

列出的角色都是**硬性阶段门禁，不是可选建议**。进入对应阶段前，主 Agent 必须创建指定名称的独立 Sub-agent，并记录调用名称、输入快照（digest）、独立会话标识和返回状态；本 Skill 自带确定性控制器时，这些登记必须写入状态文件，其退出码优先于主 Agent 的自述。

主 Agent 自己完成该阶段、普通串行自检、或只写“建议调用”均不合规。无法创建指定 Sub-agent 时，阶段状态必须为 `BLOCKED`，不得伪造角色产出；若本 Skill 定义了降级模式，则必须显式登记降级并降低交付状态，不得静默降级。

**该门禁只约束主 Agent（Orchestrator）**：被派发的角色 Sub-agent 一律不再派发下一级 Sub-agent，也绝不因自身缺少派发能力而返回 `BLOCKED`；它只需按自己的角色契约完成本阶段并返回结构化结果。

## 输出语言

唯一事实源是 `$SKILLS_ROOT/_shared/prompts/output-language.md`；派发任何角色前必须读取该文件，并按其中的规则段执行。

- **主 Agent 必须把规则段原文追加进每个派发正文。** 角色文件里的语言条款由 `scripts/manage-global-agents.py` 在安装时注入，但插件缓存、旧版本角色文件和宿主内置的通用 subagent 都可能不带该条款——只在 SKILL.md 里声明不足以约束到子代理，因为子代理看不到 SKILL.md。这条要求不属于上文禁止注入的“派发门禁”。
- 语言规则只约束自然语言。契约字段名、枚举与状态字面量（如 `BLOCKED`、`CONFIRMED`、`APPROVE`）、JSON key、脚本参数、退出码一律保持英文，门禁脚本按这些标识符判定，翻译会直接把阶段门禁打挂。
- 产物文件（PRD、TDD、RPD、审查报告、Worker result JSON）的正文语言以对应模板与契约的要求为准，不以本节的会话语言为准；两者冲突时以模板与契约为准。

## 派发能力前置检查（每次运行一次，先于任何业务写入）

任何 Skill 在第一次派发角色之前，主 Agent 必须实测宿主是否**真的**暴露派发工具：

```bash
<PYTHON> "$SKILLS_ROOT/_shared/scripts/probe-collab.py" --host <claude|codex|opencode|qoder|qodercn>
```

- `--host` 按你自己所在的宿主显式填写；脚本按环境变量猜宿主只作兜底，猜错会误阻断一次本可完成的运行。
- 结论三态：`available` / `unavailable` / `unknown`。**只有 `unavailable` 可以阻断**——`unknown`（命令跑不通、宿主 CLI 不在 PATH、超时）不是"没有能力"的证据，照常派发，派发真失败时再按 `collab-unavailable` 停止。
- `unavailable` 时按顺序处理，不得跳步，也不得先写完业务产物再补报告：
  1. 向用户说明缺的是宿主的子代理工具，并**申请授权**修改宿主配置；
  2. 用户同意后运行同一脚本加 `--enable`（传 `--enable` 本身就是这次同意的记录，脚本不再询问）。它只改一个键，写完自己复验，复验不过逐字节回滚；
  3. 用户不同意时不写任何配置，按 `collab-unavailable` 停止；
  4. 无论写没写，最后都要给出**醒目的重启提示**（引用块或加粗，不得降级成脚注或并进摘要）：宿主只在启动时读取配置，正在运行的会话拿不到新工具，**本轮不能继续**，需完全退出并重启该 Agent 应用后重跑本 Skill。
- 实测只有 Codex 有可开启的多代理开关（`[features] multi_agent_v2`）。Claude Code / OpenCode / Qoder 的派发工具由宿主内置，`--enable` 对它们返回 `refused` 且不写任何配置——不要为这些平台编造配置项，也不要把"没有开关"报成"已开启"。
- 本 Skill 自带确定性控制器时（如 initialize），这项检查排在控制器第一次写入之前，结论连同实测命令、退出码与看到的工具名一起写进运行证据；没有控制器的 Skill 在阶段 0 之前完成。

## 按宿主派发

- **Claude Code：** 用宿主的子代理派发能力，把角色名精确指定为全局安装的裸名 `<name>`；不要接受宿主自动路由到别的角色。`--agent` / `agent` 是把**整个会话**切换为该角色，不能替代 Skill 内部的子代理派发。
- **Codex：** 把 `Spawn one subagent named <name> ...`（或等价的显式 `spawn_agent` 调用，`agent_type` 填角色名）作为**主 Agent 发给宿主的派发指令**，而不是写进子代理的任务正文。传给子代理的 `message` 只包含该角色自身的任务与契约（Input Contract、冻结快照 digest、要读取的角色文件与 `SKILL_DIR` 绝对路径等）；**不得出现任何“再派发 / Spawn one subagent”字样，也不要连同 `fork_context` 把主 Agent 的强制派发门禁注入子代理**——否则子代理会因无法满足只属于主 Agent 的门禁而返回 `BLOCKED`。按阶段顺序等待全部必需结果；不得依赖自动路由，也不得只说“请分析/请设计/请审查”。
  - 能力是否存在只看**模型可见的派发工具**：`codex features list` 可以在工具面为空时仍报 `multi_agent stable true`，宿主自述同理。工具面缺失即 `collab-unavailable`，按各 Skill 的阻断路径停止，不得改由主 Agent 代做。
  - 补齐该能力有两条路：**安装器**在勾选 Codex 插件时自动开启（一次/机器，之后技能侧探测直接得到 `available`）；技能侧则必须先取得用户授权，再用 `probe-collab.py --enable` 写入。两条路都改变不了"宿主只在启动时读配置"这一点——本轮改完仍拿不到工具，所以写完只能停下提示重启，不得当作已经修好。
- **OpenCode：** 通过命令或调度入口指定 `agent: "<name>"` 并显式设置 `subagent: true`，强制创建后台子会话；旧版仅支持 `subtask: true` 时使用该兼容字段。仅依赖 `mode: subagent` 或在当前主会话内执行，不满足强制调用要求。
- **Qoder / Qoder CN：** 派发工具由宿主内置，用宿主的子代理/任务能力创建独立会话，并把 `subagent_type`（或等价字段）填成全局安装的裸名 `<name>`；两个发行版共用同一套角色安装路径（`<配置根>/agents/<name>.md`，配置根分别是 `~/.qoder` 与 `~/.qoder-cn`），所以语法一致。`--enable` 对它们返回 `refused`：没有可写的开关，不要为它们编造配置项。

## 复用与轮次

需要“每轮全新实例”的角色（终审 Reviewer、交叉验证 Evidence Verifier 等），同一轮内不得复用实例；并行派发时每一路使用独立会话并等待返回。合并结果前必须消解冲突、保留各自证据来源，并标出无法裁决的事实。

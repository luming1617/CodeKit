# CodeKit Skill 目录说明

每个技能目录以 `SKILL.md`（流程与元数据）为核心，并固定包含 `openai.yaml`（界面元数据），其余
`agents/`、`references/`、`assets/`、`scripts/`、`standard/` 按技能需要选配。

## openai.yaml：技能界面元数据

`skills/<skill>/openai.yaml` 是 **Codex（OpenAI 客户端）专用的可选元数据**，不属于 Agent Skills
通用标准的必需项；模型执行任务时不读取它，Claude Code 与 OpenCode 也完全忽略它。它只决定技能在
ChatGPT / Codex 界面中如何展示，以及被选中时预填什么提示词。

```yaml
interface:
  display_name: "界面显示名称"
  short_description: "界面一行简介"
  default_prompt: "使用 $<skill> ……（选中技能时预填的默认提示词）"
```

| 字段 | 作用 |
|---|---|
| `interface.display_name` | 技能在选择器/侧栏中显示的名称 |
| `interface.short_description` | 列表中的一行简介 |
| `interface.default_prompt` | 选中技能时预填到输入框的提示词模板 |

统一约定（`scripts/validate-skill-layout.py` 在 pre-push 强校验）：

1. 7 个技能**全部**提供该文件，位置与 `SKILL.md` **同级**。
2. 三个字段一律使用**简体中文**；代码、命令、路径、参数占位符与技术标识符保留原文。
3. `default_prompt` 必须以 `$<skill>`（技能目录名，等于 `SKILL.md` 的 `name`）开头调用，并带
   `SKILL.md` 中 `argument-hint` 声明的参数。
4. `default_prompt` 的权限边界必须与 `SKILL.md` 一致，例如 bugfix/code-review 强调审批前只读、
   release-doc 禁止真实发布。
5. `SKILL.md` 的 `name` 必须与目录名一致；仓库内**不得**提交 `skills/<skill>/agents/openai.yaml`。

**路径换算**：Codex 官方只读取 `skills/<skill>/agents/openai.yaml`。仓库把源文件与 `SKILL.md`
同级存放以保持一致和可读，安装时由 `install.sh` 的 `_stage_skill_interface_files` 在生成的兼容
市场源里复制出 Codex 约定路径，因此无需人工维护两份文件。

## agents/：全局 subagent 规范源

`skills/<skill>/agents/*.md` 是 CodeKit 各个 Skill 使用的独立 subagent 定义，按所属 Skill 存放在
`skills/<skill>/agents/`，统一使用短前缀命名，避免不同 Skill 的同名角色互相覆盖。该目录只放
Markdown 角色文件。安装时：

- Claude Code：复制 Markdown 到 ~/.claude/agents/；
- OpenCode：复制 Markdown 到 ~/.config/opencode/agents/；
- Codex：根据 Markdown 的 name、description 和正文生成 ~/.codex/agents/<name>.toml，因为 Codex 的自定义 agent 配置格式是 TOML。

安装器 `scripts/manage-global-agents.py` 只匹配 `*/agents/*.md`，`openai.yaml` 不参与全局安装。

## 角色清单

| Skill         | 全局 agent                                                                              |
|---------------|-----------------------------------------------------------------------------------------|
| bugfix        | bugfix-investigator、bugfix-challenger、bugfix-implementer                              |
| code-impl     | impl-planner、impl-implementer、impl-debugger                                           |
| code-review   | review-context、review-traceability、review-specialist、review-evidence、review-failure |
| product-doc   | product-extractor、product-writer、product-reviewer                                     |
| release-doc   | release-dependencies、release-writer、release-critic、release-refiner、release-reviewer |
| technical-doc | tech-context、tech-architect、tech-critic、tech-refiner、tech-reviewer                  |
| initialize    | initialize-gitnexus、initialize-graphify、initialize-context-scanner                    |

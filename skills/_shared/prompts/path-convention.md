# 路径解析约定（CodeKit Skill 共享规范）

> 唯一事实源。各 Skill 的 `SKILL.md` 只保留本 Skill 特有的不变式，其余按本文档解析路径；
> 修改派发规则或路径约定时改这一份，不要回到 7 个 Skill 里逐字同步。

## 两个路径变量

| 变量 | 含义 | 解析方式 |
|------|------|----------|
| `SKILL_DIR` | 当前 Skill 目录的绝对路径 | 本文档（或调用方 Skill 的 `SKILL.md`）所在目录；插件安装时为 `<plugin-cache>/skills/<skill>` |
| `SKILLS_ROOT` | 全部 Skill 的公共父目录 | `dirname(SKILL_DIR)`，即 `<plugin-cache>/skills`；`_shared/` 就在它下面 |

- `SKILL_DIR` 只用于定位本 Skill 的 `references/` 资源、`agents/` 角色规范源文件和 `scripts/` 门禁脚本；`SKILLS_ROOT` 只用于定位 `_shared/` 共享规范。
- 在命令行、Agent 提示或任何外部进程中引用这些路径时统一使用 `$SKILL_DIR` / `$SKILLS_ROOT`，并在调用前解析为绝对路径。
- 不要假设当前工作目录、当前插件目录或仓库根目录就是 Skill 目录。正文内联的资源描述（例如 `references/check-standard.md`）按“相对 `SKILL_DIR`”理解，由主 Agent 读取时解析。

## 派发子代理时必须显式给出

1. `SKILL_DIR` 的绝对路径；
2. 本 Skill 要读取的角色规范文件名（例如 `agents/<role>.md`）及其绝对路径；
3. 本阶段输入输出文档的绝对路径与固定文件名；
4. 运行状态文件（`state.json`）的绝对路径，如果本 Skill 自带确定性控制器。

不得让子代理自行猜测资源文件、脚本或输入文档的位置，也不得依赖它的隐式上下文。需要交接大段内容时传**路径 + digest**，由子代理自己读文件；主 Agent 不搬运正文。

## 相对链接纪律

- Skill 之间不得用 `../../<other-skill>/...` 互相引用文件：单包整体安装时看似可用，按 Skill 安装或缓存路径变化时即断链。跨 Skill 的产物只通过固定文件名（`product-doc.md`、`technical-doc.md`、`release-doc.md`）在 `<feature-directory>` 内交接。
- 共享文本一律放 `skills/_shared/prompts/`，用 `$SKILLS_ROOT/_shared/...` 引用；`scripts/validate-skill-layout.py` 会拒绝逃逸出本 Skill 目录的相对链接。
- `skills/_shared/` 不含 `SKILL.md`，也不含 `agents/` 子目录：它只放共享规范文本，避免被当成 Skill 装载或被全局 agent 安装脚本抓取。

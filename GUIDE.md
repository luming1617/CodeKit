# CodeKit AI 开发套件使用指南

CodeKit 是一套基于 Claude / OpenCode / Codex / Qoder 的 AI 开发套件，通过"规范 + 技能 + 工具链"，让 AI 能按团队标准独立完成研发闭环——从读 PRD、写方案、发布准备、编码到 CR，全程 AI 主导，人类定目标、审结果。

---

## 安装

### 前置要求

| 条件 | 说明 |
|------|---------|
| Node.js >= 24 | 只检测，不自动安装；不满足时提示手动安装方式并中止 |
| Python3 >= 3.10 | 同上（graphify 要求 Python >= 3.10） |
| npm | 随 Node.js 提供 |
| curl 或 wget | 任一存在即可，用于官方脚本与发布包下载 |
| git | 存在即可（安装器读取 marketplace 提交号并写入全局 git 配置，不再校验版本） |
| 编程智能体（Claude / Codex / OpenCode / Qoder / Qoder CN） | 勾选对应插件时才自动安装 CLI（npm 优先，可回退 brew/cask；Qoder 与 Qoder CN 用官方脚本）；五者都未安装时仅提示并继续 |
| SSH Key（访问 GitLab） | `ssh -T git@<your-git-host>` |

> 安装器**不会**代装任何系统级工具（包括 Homebrew）；Homebrew 仅在已安装时作为可选安装通道使用。

> `<your-git-host>` 指本仓库所在的 Git 主机。在本仓库内运行 `bash install.sh` 时，安装后的配置指引会自动打印实际主机名。

### 一键安装

在**本仓库根目录**执行：

```bash
bash install.sh
```

安装内容：rtk（节省 token 60-90%）、GitNexus（代码智能）、Graphify（知识图谱）、gh（GitHub CLI）、glab（GitLab CLI）、钉钉 CLI（`dws`）、飞书 CLI（`lark-cli`）、CodeKit 插件（Claude + OpenCode + Codex + Qoder + Qoder CN，插件分别包含对应平台的全局 subagent；Qoder 插件同时服务其 CLI 与桌面 IDE）。安装前脚本只**检测** git（存在即可）、Node.js ≥ 24、Python3 ≥ 3.10、npm 与 curl/wget，任一不满足即醒目列出并中止，不代装。编程智能体 CLI 不在此阶段强制准备：没有可用 CLI 时只提示，勾选平台插件后才自动安装对应 CLI。

> CodeKit 的 7 个技能同时支持 **Claude**、**OpenCode**、**Codex**、**Qoder** 和 **Qoder CN**，可以同时安装，互不冲突。可选组件统一按 `npm → brew → apt → pipx/venv → 发布包/官方脚本` 的优先级探测（rtk 在 brew 之外优先取 `rtk-<target triple>.tar.gz` 发布资产，而不是 `curl | bash`）：Claude、Codex、OpenCode 优先走 npm（统一 `registry.npmmirror.com` 镜像源），macOS 可回退 Homebrew/cask（Homebrew 统一走**中科大镜像**并关闭其自动更新）；Qoder 与 Qoder CN 使用官方安装脚本。脚本与 release 这类 GitHub 下载统一走 `gh-proxy`，GitHub 版本探测也只走该代理。Qoder 与 Qoder CN 的 CLI 和桌面 IDE 共用同一个用户级配置根，安装一次两端生效。
>
> 脚本以当前仓库作为插件市场源（相对定位）；组件注册表和交互选择器位于 `scripts/`，安装与卸载必须在完整仓库中执行。

### 升级 / 卸载

在本仓库根目录执行：

```bash
# 安装或升级所有可选组件
bash install.sh

# 卸载
bash uninstall.sh
```

### 手动安装 OpenCode 插件

如果你使用 OpenCode 但一键脚本未检测到，可以手动配置：

编辑 `~/.config/opencode/opencode.json`，在 `plugin` 数组中添加本仓库 `.opencode/` 目录的绝对路径：

```json
{
  "plugin": [
    "codekit@file:///path/to/marketplace/.opencode"
  ]
}
```

重启 OpenCode 后，技能以裸命令提供（如 `/initialize`），OpenCode 下不带插件前缀。

手动配置直接使用仓库的 `.opencode/` 目录；如果需要与一键安装完全一致的旧版 npm 包目录结构，请运行 `bash install.sh install`，由安装器生成兼容包源。

### 手动安装 Qoder 插件

Qoder（国际版）与 Qoder CN 的 CLI、桌面 IDE 共用同一个用户级配置根，因此一条命令即可让两端同时拿到插件：

```bash
# Qoder CN：把本仓库作为本地插件源安装到共享配置根
qodercn plugins install /path/to/CodeKit --scope user --json

# 国际版把命令换成 qoder，配置根为 ~/.qoder
qoder plugins install /path/to/CodeKit --scope user --json
```

安装结果写在 `<配置根>/plugins/installed_plugins_v2.json`，启用状态写在 `<配置根>/settings.json` 的 `enabledPlugins`；技能实体缓存在 `<配置根>/plugins/cache/local/codekit/<版本>/`。重启 Qoder CLI 与 Qoder IDE 后，用 `/codekit:<skill>` 调用。

> 配置根可通过 `QODER_CONFIG_DIR` / `QODERCN_CONFIG_DIR` 覆盖，安装器会沿用同一个根；WSL 内安装只影响 WSL 侧的 Linux 根，Windows 侧的 `C:\Users\<name>\.qoder-cn` 需要单独安装。

### 手动安装 Codex 插件

如果你使用 OpenAI Codex，一键脚本会通过官方插件市场机制安装 CodeKit 插件（`codex plugin marketplace add` + `codex plugin add codekit@co-daming`）。由于安装器需要先生成兼容旧版目录结构的市场源，建议直接从本仓库运行一键脚本并选择 Codex 组件：

```bash
bash install.sh
```

Codex 中通过 `$<skill-name>` 调用，例如 `$initialize`、`$technical-doc`。Codex 也会根据 skill 描述自动匹配任务（隐式调用）。

> **升级说明（旧版用户）**：本套件前身（keel）曾以散装方式安装到 `~/.agents/skills/keel-*`，现已全部迁移为插件机制。重新运行一键安装/升级脚本会自动清理旧的散装技能并安装插件。技能调用名同步去掉 `keel-` 前缀：`$keel-initialize` → `$initialize`。

手动卸载：

```bash
codex plugin remove codekit@co-daming
codex plugin marketplace remove co-daming
```

---

## 安装后配置

安装完成后，脚本会打印完整的配置步骤。以下是各工具的认证方式：

### 1. GitLab CLI（glab）

在 GitLab Web 界面创建 Personal Access Token（`<your-git-host>` 为本仓库所在的 Git 主机）：
> <https://<your-git-host>/-/user_settings/personal_access_tokens>
> → 新建 Token，勾选 `api`、`read_user`、`read_repository` 权限，复制生成的 Token

```bash
glab auth login --hostname <your-git-host> --token <your-token>
```

### 2. GitHub CLI（gh）

```bash
gh auth login
```

### 3. 代码智能（GitNexus / Graphify）

GitNexus 与 Graphify 为代码知识图谱工具（推荐安装，可在组件选择器中取消勾选）。在项目根目录执行以下终端命令：

```bash
gitnexus analyze --embeddings --skills --verbose  # GitNexus：构建代码知识图谱索引
touch .graphifyignore   # 按 .gitignore 语法配置 Graphify 忽略规则
```

然后按所使用的智能体输入对应命令，初始化当前仓库知识图谱：

```text
# Claude / OpenCode / Qoder
/graphify .

# Codex
$graphify .
```

### 4. 钉钉 CLI / 飞书 CLI

两项 CLI 都由安装器通过 npm 用户态安装（`set_npm` 注入的 `npm_config_registry` 指向 `https://registry.npmmirror.com`）。安装后按需登录：

```bash
dws auth login
lark-cli config init
lark-cli auth login --recommend
```

安装器的结果汇总会显示对应命令路径，卸载时会同时清理用户 npm 前缀和默认 npm 前缀中的包。

### 5. rtk（节省 token）

rtk 是 CLI 代理工具，自动压缩 `git status`、`git diff`、`cargo test` 等命令输出，减少 LLM token 消耗 60-90%。安装时会按已安装的 AI 工具自动执行 `rtk init -g`；如果安装了 Codex，另执行 `rtk init -g --codex`。

重启 Claude / OpenCode / Codex 后自动生效。验证：

```bash
rtk --version
rtk gain          # 查看节省统计
```

---

## 快速开始

### 第一步：初始化项目（每个仓库只需一次）

进入项目目录，启动 Claude 或 OpenCode，执行：

```
/codekit:initialize
```

`initialize` 自动识别技术栈，强制调用 GitNexus、Graphify、Context Scanner 三个独立 Subagent，挂载规范并生成或更新 `AGENTS.md`、`CLAUDE.md`，为后续开发建立完整上下文。

### 第二步：开始需求开发或问题排查

见下方流程章节。

---

## 命令速查

| 分类 | 命令 | 作用 |
|------|------|------|
| 初始化 | `/codekit:initialize` | 强制调用三路 Subagent，挂载规范、索引仓库并生成上下文文件 |
| 需求 | `/codekit:product-doc` | 整理并审查产品需求文档 |
| 方案 | `/codekit:technical-doc` | 仅基于 PRD 生成后端技术方案 |
| 发布 | `/codekit:release-doc` | 基于 PRD + TDD 生成后端发布方案 |
| 编码 | `/codekit:code-impl` | 多 Agent 并行 TDD 开发 |
| 审查 | `/codekit:code-review` | 5 个维度并行 Code Review |
| 排查 | `/codekit:bugfix` | 基于 PRD/TDD/RPD 系统化排查并修复问题 |

> **Codex 调用方式：** Codex 中使用 `$<skill-name>` 调用对应技能，如 `$initialize`（旧版为 `$keel-initialize`，已去掉前缀）。Codex 也会根据 skill 描述自动匹配任务（隐式调用）。
>
> **Qoder 调用方式：** Qoder 与 Qoder CN 沿用 `/codekit:<skill>` 前缀，如 `/codekit:initialize`、`/codekit:technical-doc`；CLI 与桌面 IDE 读取同一份插件注册表，安装一次即可在两端调用。

---

## 需求开发流程

从 PRD 到代码交付的完整链路：

```bash
/codekit:product-doc <product-material-source> [--feature-directory <feature-directory>]
/codekit:technical-doc <feature-directory>
/codekit:release-doc <feature-directory>
/codekit:code-impl <feature-directory>
/codekit:code-review <feature-directory>
```

```
product-doc → technical-doc → release-doc → code-impl → code-review
   （输入）       （阶段 2）       （阶段 2）      （阶段 2）       （阶段 2）
```

`product-doc` 不带 `feature-directory` 时创建新需求；带有效目录时按需求变更更新原 PRD，在文档内只追加 `修改、新增、删除` 台账与变更历史行，上一版正文另存为同目录 `history/` 下的独立快照（不得内嵌进 PRD）。独立评审的轮次、视角路由与 Finding 闭环由 `skills/product-doc/scripts/` 下的两个门禁脚本判定（状态枚举与上限见 `skills/product-doc/references/product-review-contract.md`），任一脚本非 0 时只能交付阻塞报告。目录缺少有效 `product-doc.md` 时会先提示并等待确认，再按新需求处理。

`code-review` 的 `<feature-directory>` 可省略：上面的需求开发链路里应始终传目录，只在单独审查 PR、MR、分支或本地 Diff 时省略。省略时它递归收集仓库根目录 `docs/` 及其子目录下的 `product-doc.md`、`technical-doc.md`、`release-doc.md`；仍找不到有效文档则记录 `DOCUMENTS: NOT_AVAILABLE` 并降级为代码与证据驱动的审查，结论不得为 `APPROVE`（最多 `APPROVE_WITH_FOLLOW_UPS`）。`bugfix` 和 `code-impl` 调用它时还会随 Handoff 显式声明 `review_mode: FULL | LITE | WAIVED` 与文档可得性，两边共用同一套降级规则。

---

## 问题排查流程

```bash
# 系统化根因分析（需求目录可选，错误材料必传）
/codekit:bugfix [<feature-directory>] <error-description>
```

`bugfix` 严格走“只读调查 → 人工审批 → 实施验证”两阶段流程：

> 明确问题 → 多源取证 → 模式分析 → 关联假设（≥2 个独立源交叉验证）→ 验证假设 → 修复确认

AI 会跨越业务日志、进程状态、中间件指标、源代码等多个证据源自行找路，不需要人教它"该去哪查"。

门禁由两个脚本兜底，不依赖 Agent 自述：`validate_bugfix_proposal.py` 校验提案结构与审批语义，`bugfix_state.py` 负责冻结快照、绑定审批口令、检查写入越界和真实执行验证命令（以 `shell=False` 运行并记录退出码与错误指纹）。两者退出码不通过时流程必须停止。审批前的提案、批准原文和验证记录都写在已被 ignore 的 `.codekit/bugfix/<run-id>/`，不会污染工作树。当下游 `codekit:code-review` 拿不到有效 PRD/TDD/RPD 时，本次审查降级为 `LITE` 模式，最多只能得到 `APPROVE_WITH_FOLLOW_UPS`。

---

---

## 故障排查

| 问题 | 解决方案 |
|------|---------|
| `plugin add` 失败 | 确认在本仓库根目录运行安装脚本（本地源）；改用远端地址安装的，确认 SSH Key 已配置：`ssh -T git@<your-git-host>` |
| `glab` 认证失败 | `glab auth login --hostname <your-git-host>` |
| `gh` 认证失败 | `gh auth login` |
| rtk 未生效（token 未减少） | Claude/OpenCode 运行 `rtk init -g --show`；Codex 运行 `rtk init -g --codex`；重启对应 AI 工具 |
| 命令找不到（Claude） | `claude plugin list` 确认 CodeKit 插件已安装 |
| 命令找不到（OpenCode） | 检查 `~/.config/opencode/opencode.json` 中 `plugin` 数组是否包含 CodeKit |
| OpenCode 插件未生效 | 修改 opencode.json 后需重启 OpenCode |
| 命令找不到（Codex） | `codex plugin list --marketplace co-daming` 确认插件已安装；新会话生效 |
| 命令找不到（Qoder / Qoder CN） | `qodercn plugins list --json` 确认存在 `codekit@local`；子命令为 `qoder` 时同理 |
| Qoder 插件未生效 | 插件与全局 subagent 写入共享配置根，需重启 Qoder CLI 与 Qoder IDE 后生效 |
| Qoder 只装到一端 | 无需重复安装：CLI 与 IDE 共用 `~/.qoder`（CN 为 `~/.qoder-cn`），一次安装两端可见 |
| PATH 未生效 | `source ~/.zshrc` 或重启终端 |
| 安装日志 | `cat ~/.local/state/CodeKit/install.log` |

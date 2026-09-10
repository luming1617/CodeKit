# install.sh 说明文档

> **CodeKit Plugin — 一键安装脚本**（`install.sh`，版本见根目录 `plugin-version.json`）
>
> 本文档描述 `install.sh` 的完整执行流程、组件清单、各阶段逻辑与扩展方式。
> 脚本设计目标：**Python 管理注册表与交互界面、用户态安装（无 sudo）、bash 3.x 兼容（macOS 3.2+）、macOS/Linux 双平台**。

---

## 目录

1. [脚本定位](#1-脚本定位)
2. [安装与调用方式](#2-安装与调用方式)
3. [命令行参数](#3-命令行参数)
4. [依赖组件清单](#4-依赖组件清单)
5. [安装组件清单（注册表）](#5-安装组件清单)
6. [总体执行流程](#6-总体执行流程)
7. [各阶段详解](#7-各阶段详解)
8. [各组件安装策略](#8-各组件安装策略)
9. [卸载流程](#9-卸载流程)
10. [安装后插件目录变化（五平台）](#10-安装后插件目录变化五平台)
11. [状态文件与日志](#11-状态文件与日志)
12. [关键助手函数速查](#12-关键助手函数速查)
13. [如何新增一个组件](#13-如何新增一个组件)
14. [行为与退出码约定](#14-行为与退出码约定)

---

## 1. 脚本定位

`install.sh` 是 CodeKit Plugin 的**一键安装器**，负责把一整套 AI 开发工具链安装到用户环境，包括：

- 编程智能体（Claude / Codex / OpenCode / Qoder / Qoder CN，前置检查保障至少安装一个）
- 内部协作工具（git-ai、rtk）
- 代码智能（GitNexus、Graphify）
- git 仓库管理（gh、glab）
- 智能体插件（Claude 插件、OpenCode 插件、Codex 插件、Qoder 插件、Qoder CN 插件）

核心原则：

| 原则 | 说明 |
|------|------|
| **职责分离** | `install.sh` 编排安装流程，`scripts/component-registry.py` 管理组件注册，`scripts/component-selector.py` 管理交互界面 |
| **本地源优先** | 在仓库内执行时，以 `install.sh` 所在目录（当前仓库）作为插件市场源，相对定位、不写死远端地址；检测不到本地源时保留为空，打印提示并兜底跳过相关组件，不影响其他操作 |
| **用户态安装** | 全部装到 `~/.local/`、`~/.npm-global/` 等用户目录，不用 `sudo` |
| **幂等** | 已装组件自动识别并显示 `latest`，默认不勾选；勾选到 `install_*` 时按"已装即升级/刷新"处理，因此重复执行同一命令不会重复安装，未勾选的组件不被改动 |
| **交互式选择** | 提供可视化多选界面，用户可自由勾选要装的组件 |
| **快速失败** | 前置依赖缺失时立即退出，避免半途失败 |

> **npm 镜像源**：所有 npm 安装（Claude / Codex / OpenCode 的 CLI、`gitnexus`、钉钉 CLI、飞书 CLI）统一附加 `--registry=https://registry.npmmirror.com`，飞书的 `npx skills add` 同样指定该源；`gitnexus`、钉钉 CLI、飞书 CLI 的**版本探测**也走该镜像的 `/<包名>/latest`。避免直连 `registry.npmjs.org` 超时。唯一来源是 `scripts/installer_config.py` 的 `NPM_REGISTRY`。

---

## 2. 安装与调用方式

### 本地执行（推荐）

在**本仓库根目录**执行（脚本以当前仓库作为插件市场源）：

```bash
bash install.sh install [-y]
bash uninstall.sh [-y]
```

`install.sh` 和 `uninstall.sh` 都依赖同仓库的 Python 安装/卸载编排器及组件辅助脚本，不再支持只传输单个脚本的 `curl ... | bash` 模式；需要远程安装或卸载时，请先克隆或下载完整仓库再执行。

---

## 3. 命令行参数

| 参数 | 说明 |
|------|------|
| `install` | 安装缺失组件、升级有更新的组件；已最新组件默认不勾选，手动勾选时重装（原 `upgrade` 语义已合并至此） |
| `uninstall.sh` | 选择并卸载 CodeKit 组件 |
| `-y`, `--yes` | 免交互模式：安装缺失/有更新的组件，或卸载当前已安装组件 |
| `-h`, `--help` | 打印用法说明并退出 |

> 传入已取消的 `upgrade` 会直接报错退出，并提示改用 `install`。

**子命令缺省规则**：未指定子命令时一律默认 `install`（不再依据版本记录文件 `${CodeKit_VERSION_FILE}` 推断动作）。

---


## 4. 依赖组件清单

在一切之前执行。缺失时用 Homebrew 自动安装，安装失败即**退出流程**。

| 检查项       | 行为                                                                                                                                                  | 失败处理 |
|--------------|-------------------------------------------------------------------------------------------------------------------------------------------------------|----------|
| Homebrew     | 未装则用**中科大镜像脚本**自动安装（`mirrors.ustc.edu.cn/misc/brew-install.sh`，非交互模式）；已装则执行 `brew update`；安装/更新前注入中科大镜像源（`HOMEBREW_BREW_GIT_REMOTE` / `HOMEBREW_API_DOMAIN` / `HOMEBREW_ARTIFACT_DOMAIN`） | **安装失败**即**退出流程**；**更新失败**仅警告并继续（多为瞬时网络问题，不影响后续安装） |
| git          | 未装则 `brew install git` 自动安装；低于 2.50.0 则 `brew upgrade git` 升级                                                                           | 安装/升级后仍不满足 ≥ 2.50.0 即**退出流程** |
| Node.js      | 未装则 `brew install node` 自动安装；低于 20 则尝试 brew 升级                                                                                         | 安装后仍不满足 ≥ 20 即**退出流程** |
| Python3      | 未装则 `brew install python3` 自动安装（brew 默认 `python@3.x` 的官方别名）                                                                           | 安装失败即**退出流程** |
| curl         | 未装则 `brew install curl` 自动安装（brew 的 curl 为 keg-only，装后按 opt 路径兜底挂载）                                                              | 安装失败即**退出流程** |
| 编程智能体   | 检查 Claude / Codex / OpenCode / Qoder / Qoder CN 的 CLI，以及 macOS/Linux 上对应 Desktop 应用、启动器和常见安装目录；Qoder 另把共享配置根视为可用 | 五者均不存在时以醒目提示结束流程（退出码 0），不再询问或自动安装 agent |

> 前置检查顺序刻意"依赖优先、智能体最后"（Homebrew → git → Node.js → Python3 → curl → 编程智能体）：智能体安装在无 cask 场景会回退 `npm install -g`（统一走 npmmirror 镜像源），需 Node 先就位。


---

## 5. 安装组件清单

组件的唯一注册源是 `scripts/component-registry.py` 中的 `COMPONENTS` 和 `GROUPS`。Python 启动时校验组件 ID、重复项、分组引用、分组顺序及 TSV 安全性；`install.sh` 的 `_load_component_registry` 再把导出的四列 TSV 加载到运行期数组，并校验每个组件都存在 `check_*`、`install_*`、`uninstall_*` 处理函数。

| # | 组件 ID           | 展示名称                          | 分组     |
|---|-------------------|-----------------------------------|----------|
| 1 | `git_ai`          | git-ai (代码追踪·推荐)            | `ai`     |
| 2 | `rtk`             | rtk (节省token·推荐)              | `ai`     |
| 3 | `claude_plugin`   | CodeKit Plugin (Claude)           | `skills` |
| 4 | `opencode_plugin` | CodeKit Plugin (OpenCode)         | `skills` |
| 5 | `codex_plugin`    | CodeKit Plugin (Codex)            | `skills` |
| 6 | `qoder_plugin`    | CodeKit Plugin (Qoder)            | `skills` |
| 7 | `qodercn_plugin`  | CodeKit Plugin (Qoder CN)         | `skills` |
| 8 | `gitnexus`        | GitNexus (代码结构分析·推荐)      | `intel`  |
| 9 | `graphify`        | Graphify (代码知识图谱·推荐)      | `intel`  |
| 10 | `gh`              | GitHub CLI (推荐)                 | `third`  |
| 11 | `glab`             | GitLab CLI (推荐)                 | `third`  |
| 12 | `dingtalk_cli`    | 钉钉 CLI                          | `third`  |
| 13 | `feishu_cli`      | 飞书 CLI                          | `third`  |

- **`GROUPS`** 定义交互选择器中的分组标题及顺序（`ai`→AI 开发套件、`skills`→CodeKit Skills、`intel`→代码智能、`third`→三方工具）。
- **双层守卫**：Python 校验注册数据自身；Bash 校验加载结果、下标数组一致性及处理函数是否齐全。
- **表驱动调用**：每个组件对应 `check_<组件>` / `install_<组件>` / `uninstall_<组件>`，由 `_dispatch` 按名字拼接分发，代码中无需逐组件调用。




### 5.1 安装来源与方式

| 组件                | 安装来源                                                                | 安装方式（首选 → 回退）                                                                                                                                                                                    | 安装落点                                                                                     |
|---------------------|-------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------|
| `git_ai`            | 官方安装脚本 `https://usegitai.com/install.sh`                           | `curl -fsSL ... \| bash`（官方无 Homebrew formula/tap，此为唯一自动化渠道，无回退）；安装器不额外写入 Git-AI 配置                                                                       | `~/.git-ai/bin/git-ai`                                                     |
| `rtk`               | Homebrew formula `rtk`；回退源 `rtk-ai/rtk` 官方脚本                      | `brew install\|upgrade rtk` → 失败回退 `curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh \| sh` → 成功后按已安装智能体执行 `rtk init -g`，Codex 另执行 `rtk init -g --codex`                | brew 的 bin 目录；配置写入对应全局目录                                             |
| `claude_plugin`     | 当前仓库根目录生成的兼容市场源（`CODEKIT_MARKET_DIR`，重建旧版 `plugins/codekit` 布局） | `claude plugin marketplace add <兼容市场源>` → `claude plugin marketplace update co-daming` 刷新索引 → 未装则 `claude plugin install codekit@co-daming --scope user`、已装则 `claude plugin update codekit@co-daming` → `_record_marketplace_sha`；本地源为空即提示跳过，无回退 | `~/.claude/plugins/known_marketplaces.json`、`~/.claude/plugins/installed_plugins.json`、`~/.claude/settings.json` |
| `opencode_plugin`   | 安装状态目录生成的兼容 npm 包源（`opencode-source`） | 首装：把条目 `codekit@file://<安装状态目录>/opencode-source` 写入 `~/.config/opencode/opencode.json` 的 `plugin` 数组（`_json_edit`：node 优先、python3 兜底）；已配置：强制刷新兼容包源（无该子命令时仅提示已配置） | `~/.config/opencode/opencode.json`                                                            |
| `codex_plugin`      | 当前仓库根目录生成的兼容市场源（重建旧版 `plugins/codekit` 布局）       | 清理旧散装 `~/.agents/skills/keel-*` → `codex plugin marketplace add <兼容市场源>` →（已装且市场源为 Git 地址时 `codex plugin marketplace upgrade co-daming`）→ `codex plugin add codekit@co-daming` → `_record_marketplace_sha`；本地源为空即提示跳过，无回退 | 插件缓存 `~/.codex/plugins/cache/`，启用状态写 `~/.codex/config.toml`                           |
| `qoder_plugin`      | 安装状态目录生成的 Qoder 插件源（`qoder-source/codekit`，仅 `.qoder-plugin/` + `skills/`） | CLI 具备 `plugins install --scope user --json` 时执行 `<cli> --config-dir ~/.qoder plugins install <源> --scope user --json`；否则直接写共享注册表与缓存（Desktop-only 兜底） | `~/.qoder/plugins/installed_plugins_v2.json`、`~/.qoder/settings.json`、`~/.qoder/agents/` |
| `qodercn_plugin`    | 安装状态目录生成的 Qoder 插件源（`qoder-source/codekit`，与 Qoder 共用同一份构建） | 同上，命令换为 `qodercn`、配置根换为 `~/.qoder-cn` | `~/.qoder-cn/plugins/installed_plugins_v2.json`、`~/.qoder-cn/settings.json`、`~/.qoder-cn/agents/` |
| `gitnexus`          | 官方 npm 渠道包 `gitnexus`（无 Homebrew formula）                          | `_set_npm`（前缀指向 `~/.npm-global`）→ `npm install -g gitnexus@latest --registry=https://registry.npmmirror.com`（用户态、免 sudo）→ 成功后 `gitnexus setup`；无回退                                                                                | `~/.npm-global/bin/gitnexus`                                                                  |
| `dingtalk_cli`      | npm 包 `dingtalk-workspace-cli`（官方 DingTalk Workspace CLI）              | 安装前检查 `unzip`，缺失时先通过当前平台包管理器安装 → `_set_npm`（前缀指向 `~/.npm-global`）→ `npm install -g dingtalk-workspace-cli@latest --registry=https://registry.npmmirror.com`；无自动认证配置                                                                                                                 | `~/.npm-global/bin/dws`                                                                       |
| `feishu_cli`        | npm 包 `@larksuite/cli`（官方 Lark/Feishu CLI）                              | `_set_npm`（前缀指向 `~/.npm-global`）→ `npm install -g @larksuite/cli@latest --registry=https://registry.npmmirror.com` → `npx --registry=https://registry.npmmirror.com skills add larksuite/cli -y -g --agent <已安装的智能体>`；显式限定 Claude Code、Codex、OpenCode，避免 PromptScript 导致整批失败                                                                 | `~/.npm-global/bin/lark-cli`                                                                  |
| `graphify`          | 官方 PyPI 渠道包 `graphifyy`（无 Homebrew formula）                          | `pipx install graphifyy`（已装则 `pipx upgrade graphifyy`）→ 失败回退独立 venv：`python3 -m venv` + `bin/pip install -U graphifyy` + 软链到 `~/.local/bin` → 按已检测智能体执行 `graphify install --platform <agent>` → `graphify hook install` | pipx 托管环境，或 `~/.local/share/CodeKit/venvs/graphify` + `~/.local/bin/graphify`             |
| `gh`                | Homebrew formula `gh`                                                        | `_brew_install_or_upgrade gh`（已装则 `brew upgrade gh`）；无回退                                                                                                                                            | brew 的 bin 目录                                                                              |
| `glab`              | Homebrew formula `glab`                                                      | `_brew_install_or_upgrade glab`（已装则 `brew upgrade glab`）；无回退                                                                                                                                        | brew 的 bin 目录                                                                              |

> 五个插件类组件的来源都是**当前仓库本地路径**（脚本相对自身位置探测根目录下的清单，并在安装时生成兼容源），不写死远端地址；若脚本目录保留 `scripts/` 但缺少根目录插件文件，这五项会各自告警并跳过（见 [8.1](#81-marketplace-本地源marketplacelocal)）。
> 更细的失败处理与升级路径见 [第 8 节](#8-各组件安装策略)。

### 5.2 版本探测与升级判定

检查阶段由 `_component_version` 统一探测已装版本，再与最新版比对；`_check_up_to_date` 用 `_ver_ge`（逐段数字比较）判定，**已装版本不低于最新版**即置 `up_to_date`，避免本地构建领先于已发布版本时误提示降级。

| 组件                | 已装版本探测                             | 最新版来源                                                    | 判定要点                                                                             |
|---------------------|------------------------------------------|---------------------------------------------------------------|--------------------------------------------------------------------------------------|
| `git_ai`            | `_git_ai_cmd` 解析路径后 `git-ai --version` | 上游无公开版本源，**不查询**                                   | 探测到即直接置 `up_to_date`（选择器标注"勾选可重装"，重装脚本即刷新到最新）           |
| `rtk`               | `rtk --version`                           | GitHub API `repos/rtk-ai/rtk/releases/latest` 的 `tag_name`    | 查询失败（超时 5s）时置 `up_to_date`，不提示升级                                      |
| `claude_plugin`     | 状态文件 `marketplace_sha`（显示取前 7 位） | 本地仓库 `git rev-parse HEAD`（`_get_marketplace_latest_sha`）  | 安装状态以 `claude plugin list` 含 `codekit@co-daming` 为准；SHA 相同→最新，不同→有更新 |
| `opencode_plugin`   | 同上 `marketplace_sha`                    | 同上 `HEAD`                                                    | 安装状态以 `opencode.json` 含 `codekit@` 为准                                         |
| `codex_plugin`      | 同上 `marketplace_sha`                    | 同上 `HEAD`                                                    | 安装状态以 `codex plugin list --marketplace co-daming` 首列匹配为准                    |
| `qoder_plugin` / `qodercn_plugin` | 共享注册表 `installed_plugins_v2.json` 的 `version`（读不到时回落 `plugin-version.json`） | 同左：重装即升级，不查询远端 | 安装状态以共享注册表含 `codekit@local` 且缓存目录真实存在为准，**不依赖 CLI 是否在 PATH** |
| `gitnexus`          | `gitnexus --version`                      | npmmirror `gitnexus/latest` 的 `version`                       | 语义化版本比对                                                                        |
| `dingtalk_cli`      | `dws version`                   | npmmirror `dingtalk-workspace-cli/latest` 的 `version`          | 语义化版本比对                                                                        |
| `feishu_cli`        | `lark-cli --version`                      | npmmirror `@larksuite/cli/latest` 的 `version`                  | 语义化版本比对                                                                        |
| `graphify`          | `graphify --version`                      | PyPI `https://pypi.org/pypi/graphifyy/json` 的 `version`        | 语义化版本比对                                                                        |
| `gh`                | `gh --version`                            | GitHub API `repos/cli/cli/releases/latest`                     | 语义化版本比对                                                                        |
| `glab`              | `glab version`                            | GitLab API `projects/34675721/releases/permalink/latest`        | 语义化版本比对                                                                        |

每个组件的状态由三组动态变量保存（通过 `eval` 拼接变量名实现）：

- `_status_<组件>`：状态（`not_checked` / `not_installed` / `installed` / `up_to_date`）
- `_ver_<组件>`：当前已装版本
- `_latest_<组件>`：远端最新版本

> 三态勾选规则（见 [7.5](#75-摘要与交互选择show_summary_and_confirm)）：`not_installed` 与 `installed`（有新版）默认勾选，`up_to_date` 默认不勾选，但用户主动勾选仍会重装/升级。

---


## 6. 总体执行流程

入口为 `main`（文件末尾 `main "$@"`）。`install` 主流程（原 `upgrade` 已合并进来）：

```
parse_args          # 解析参数，确定 ACTION
  ↓
setup_logging       # 初始化日志目录与文件
detect_os           # 检测操作系统（仅支持 macOS/Linux）
detect_arch         # 检测 CPU 架构（amd64/arm64）
ensure_user_bin     # 创建并临时挂载用户级 bin 目录
  ↓
run_preflight               # 前置检查：Homebrew → git(≥2.50.0) → Node.js(≥20) → Python3 → curl → 编程智能体（含原硬性依赖，失败退出/结束）
check_all_components        # 逐个探测各组件的当前版本与最新版本（表驱动分发）
show_summary_and_confirm    # 打印摘要 + 交互选择组件（未安装→默认勾选；有新版→显示"当前 → 最新"由用户勾选；无新版→显示 latest）
  ↓
run_selected_installs       # 依次处理选中的组件：勾选即决定（未安装→安装，已安装→升级/重装刷新；单组件失败不中断）
configure_git               # 配置全局 git 选项
save_installed_versions     # 写入版本记录文件
[可选] setup_shell_path     # 将用户级 bin 永久写入 shell 配置
  ↓
print_next_steps            # 打印带作用说明的安装后操作表格
render_recap                # 所有安装与配置操作完成后打印最终结果汇总
```

`uninstall` 流程：

```
show_uninstall_confirmation  # 二次确认（-y 跳过）
  ↓
run_uninstalls               # 逐项卸载 + 清理配置与 PATH
```

> 安装与升级共用同一条 `install` 流程（本节第一个流程图）：一次运行里既能补装缺失组件，也能勾选升级已装组件（见 [7.5](#75-摘要与交互选择)、[7.6](#76-执行安装与升级run_selected_installs)）。

---

## 7. 各阶段详解

### 7.1 参数解析（`parse_args`）

- `install.sh` 负责安装入口，`uninstall.sh` 负责卸载入口；两者均支持 `-y`/`-h`。
- `scripts/installer.py` 负责安装/升级，`scripts/uninstaller.py` 独立负责卸载；两个 shell 入口通过共享基础能力复用状态检测和组件定义。
- 传入已取消的 `upgrade`：报错提示改走 `install` 并以退出码 1 退出。
- 未知参数：打印用法并以退出码 1 退出。
- 子命令缺省时固定取 `install`（不再按版本记录文件推断）。

### 7.2 初始化

| 函数 | 作用 |
|------|------|
| `setup_logging` | 创建状态目录，写入启动时间戳到日志 |
| `detect_os` | `uname -s`，非 Darwin/Linux 报错退出 |
| `detect_arch` | `uname -m` 归一化为 `amd64`/`arm64`（供二进制下载选包） |
| `ensure_user_bin` | 创建 `~/.local/bin`、`~/.npm-global/bin` 并临时加入当前会话 PATH |

### 7.3 前置检查（`run_preflight`）

依次执行前置依赖检查和 `preflight_agents`，详见 [5.1](#51-前置检查run_preflight)。原硬性依赖（git ≥ 2.50.0 / Node.js ≥ 20 / Python3 / curl）按 macOS/Linux 平台分别补齐。前置检查同时识别 CLI 和 Desktop agent；如果五者均不存在，会以醒目提示结束插件安装，不再让用户选择或自动安装 agent。Qoder 与 Qoder CN 的 CLI 和桌面 IDE 共用同一个用户级配置根，只要该根、Desktop 或 CLI 任一处出现过即视为平台可用。全局 agent 在选择对应的 Claude、OpenCode、Codex 或 Qoder 插件时同步安装；该阶段只在 install 执行，卸载不执行。

全局 subagent 的统一源文件位于各 Skill 的 `skills/<skill>/agents/*.md`。Claude Code 原样安装到 `~/.claude/agents/`，OpenCode 原样安装到 `~/.config/opencode/agents/`，Codex 则生成同名的 `~/.codex/agents/*.toml`（正文映射到 `developer_instructions`），Qoder / Qoder CN 原样安装到与桌面 IDE 共享的 `<配置根>/agents/`（裸名，CLI 与 IDE 两端都能调度）。`initialize` 随插件安装 `initialize-gitnexus`、`initialize-graphify`、`initialize-context-scanner`，运行时由 `SKILL.md` 强制调用。安装器具备幂等性；同名非 CodeKit 文件会先备份到 `~/.local/state/CodeKit/agent-backups/`。

> 各 Skill 与 `SKILL.md` 同级的 `openai.yaml` **不是** subagent 源，也不参与上述全局安装：它是 Codex 的技能界面元数据，随技能目录整体分发，路径换算见第 10 章。

卸载对应的 Claude、OpenCode、Codex 或 Qoder 插件时，会同步删除该平台带 CodeKit 管理标记的 agent 文件；用户自行创建的 agent 与备份文件会保留。Claude 与 Qoder 的插件缓存由 CLI 和 Desktop 共享，因此即使对应 CLI 不存在也会完成缓存清理。

### 7.4 组件检查（`check_all_components`）

遍历 `COMPONENTS`，通过 `_dispatch check <组件>` 表驱动调用各 `check_<组件>`。每个检查函数：

1. 判断组件是否已安装；
2. 读取当前版本（统一走 `_component_version`）；
3. 尽力查询最新版本（npm / PyPI / GitLab API / GitHub API；marketplace 版本取**本地仓库 `HEAD`**，无本地仓库源时为空并由调用方兜底）；
4. 比对得出状态：
   - 未安装 → `not_installed`
   - 已安装且 `_check_up_to_date` 判定「已装版本 ≥ 最新版」→ `up_to_date`（用 `_ver_ge` 逐段数字比较，本地版本更新时不会被误判为有新版而提示降级）
   - 已安装但落后于最新版 → `installed`（选择器显示 `当前 → 最新`，是否升级由用户勾选决定）

> 最新版本探测失败（拿不到 `_latest_` 或版本号无法解析）时保持 `installed` 且 latest 为空，显示层按「已是最新」处理，不会自动建议升级。

> 版本探测的唯一入口是 `_component_version`，兜底文案（`unknown`/`N/A`）由调用方提供。

### 7.5 摘要与交互选择（`show_summary_and_confirm`）

1. 打印前置依赖摘要（Node/Python/Git/Homebrew/OS）。
2. 探测当前 shell 的配置文件，判断 PATH 是否已配置过。
3. **初始化默认勾选**（安装与升级共用同一条流程）：
   - `not_installed` → 勾选（补齐缺失组件）
   - `installed` 且有新版 → 勾选，行尾显示 `(组件有更新，勾选后升级 当前版本 → 最新版本)`
   - `up_to_date` / 已装但探测不到新版 → 不勾选，行尾显示 `(<版本> latest)`
   - `git_ai` 特例：上游无公开版本源，行尾显示 `(<版本> latest · 无公开版本源, 勾选可重装)`，勾选即重装刷新
4. `-y` 或无交互终端：直接采用默认值（补齐缺失组件、升级有更新的组件，不重装已最新组件）；否则进入交互选择器。

选择器行尾状态文案统一由 `_comp_status_tag` 生成；安装阶段每个组件的表头（`Installing` / `Upgrading 当前 → 最新`）由 `run_selected_installs` 按同一套状态另算。

**交互选择器**（`scripts/component-selector.py` + `_run_python_selector`）：

- `install.sh` 把组件序号、名称、分组、状态文案和默认勾选状态写入临时 TSV 清单，再由 `_run_python_selector` 调用 Python。
- Python 脚本统一负责安装/卸载界面的 Unicode 列宽、备用屏、原始终端模式、按键读取和整帧刷新；不依赖第三方包。
- Python 确认后仅向 stdout 返回 `selected=<逗号分隔下标>;path=<0|1>`，Bash 校验结果后更新 `SELECTED_INDICES` 和 PATH 选项；`q` 与 Ctrl+C 分别以专用退出码返回，并在退出前恢复终端。
- `python3 scripts/component-selector.py --mode <install|uninstall> --manifest <文件> --render` 可在不访问 `/dev/tty` 的情况下输出无颜色单帧，供快照检查。

| 输入 | 行为 |
|------|------|
| `↑` / `↓` | 上下移动光标定位组件 |
| `空格` | 勾选/取消当前光标所在组件 |
| `回车` | 确认当前选择（一次性提交） |
| `a` | 全选 |
| `n` | 全部取消 |
| `p` | 切换"是否写入 PATH"选项 |
| `q` | 退出安装流程 |

确认后 `_finalize_selection` 汇总选中下标到 `SELECTED_INDICES`；若无任何选中则直接退出。

### 7.6 执行安装与升级（`run_selected_installs`）

遍历 `SELECTED_INDICES`，先按 check 阶段状态决定表头文案，随后一律交给 `_dispatch install <组件>`：

| 状态 | 表头 | 含义 |
|------|------|------|
| `not_installed` | `--- Installing: <名称> ---` | 首次安装 |
| 其他（已装且被用户勾选） | `--- Upgrading: <名称> <当前> → <最新> ---` | 升级 / 重装刷新 |

**勾选即决定**：选择器对缺失和有更新的组件预勾选、对已最新组件默认不勾选；用户确认后，所有已勾选组件都会进入对应的安装、升级或重装路径，因此不再做"已装即跳过"的短路判断。

各组件 `install_*` 在已装场景下的升级动作：

| 组件 | 升级方式 |
|------|----------|
| `glab` / `gh` / `rtk` | `_brew_install_or_upgrade`：已装则 `brew upgrade`；无新版时 `brew` 报错，脚本降级为提示 `already latest` |
| `git_ai` | 官方安装脚本总是拉取最新版，勾选即重装刷新 |
| `gitnexus` | `npm install -g gitnexus@latest --registry=https://registry.npmmirror.com` |
| `dingtalk_cli` / `feishu_cli` | 分别执行 `npm install -g dingtalk-workspace-cli@latest --registry=https://registry.npmmirror.com` / `npm install -g @larksuite/cli@latest --registry=https://registry.npmmirror.com` |
| `graphify` | pipx 已托管 → `pipx upgrade graphifyy`；否则重建 venv 后 `pip install -U` |
| `claude_plugin` | `claude plugin update codekit@co-daming`（未装则 `install --scope user`），并记录新的 marketplace SHA |
| `opencode_plugin` | `opencode plugin <module> -g -f` 强制替换为当前仓库版本并更新配置 |
| `codex_plugin` | 已装且市场源为 Git 时先 `codex plugin marketplace upgrade` 刷新快照，再 `codex plugin add` 更新到快照版本；本地目录源实时读取，跳过快照刷新 |
| `qoder_plugin` / `qodercn_plugin` | 重新安装到 `<根>/plugins/cache/local/codekit/<版本>/`（同版本重装即整体替换），不使用 `plugins update`，也不注册 marketplace |

**单个组件失败只告警、不中断**整体流程（便于部分成功）。

### 7.7 安装后处理

| 步骤 | 函数 | 说明 |
|------|------|------|
| 配置 git | `configure_git` | 关闭 `addIgnoredFile` 提示、开启 `push.autoSetupRemote` |
| 记录版本 | `save_installed_versions` | 把所有组件版本写入版本记录文件 |
| 写 PATH | `setup_shell_path` | 仅当用户在交互中勾选了 `p` 时执行 |

### 7.8 最终结果与安装后操作

- `render_recap`：等待所有组件安装、全局配置和版本记录完成后，逐项打印安装/卸载结果；未选择的组件显示 `未选择`，安装路径和安装版本显示 `---`。
- `print_next_steps`：按已装组件**条件化**打印带作用说明的后续配置表格（重启智能体应用、PATH、glab/gh 登录、GitNexus/Graphify 用法、`/codekit:initialize` 等）。

---

## 8. 各组件安装策略

| 组件 | 首选方式 | 回退方式 | 备注 |
|------|----------|----------|------|
| `git_ai` | 官方安装脚本 | — | 无 Homebrew formula/tap，官方仅提供 `curl \| bash` 安装脚本（唯一自动化渠道）；安装器不额外写入配置 |
| `rtk` | Homebrew | 官方 curl 安装脚本 | 装好后执行 `rtk init -g`，Codex 另执行 `rtk init -g --codex` |
| `claude_plugin` | `claude plugin marketplace add <安装状态目录>/marketplace-source/codekit`（兼容市场根） | — | 记录版本；本地源为空时提示并跳过；本地源不配置自动更新（打印手动刷新方式） |
| `opencode_plugin` | 写入 `opencode.json` 的 plugin 数组 | — | 条目为 `codekit@file://<安装状态目录>/opencode-source`（兼容 npm 包入口）；用 node（优先）或 python3 编辑 JSON |
| `codex_plugin` | `codex plugin marketplace add <安装状态目录>/marketplace-source/codekit`（兼容市场根） | — | 官方插件市场机制；`codex plugin add codekit@co-daming` |
| `qoder_plugin` / `qodercn_plugin` | Qoder CLI `plugins install <源> --scope user --json` | 直写共享配置根（注册表 + 缓存 + `enabledPlugins`） | CLI 与桌面 IDE 共用同一个根，安装一次两端生效；只支持 `user` 作用域 |
| `gitnexus` | npm 用户态安装（`npm install -g gitnexus@latest --registry=https://registry.npmmirror.com`） | — | 无 Homebrew formula，走官方 npm 渠道；装好后执行 `gitnexus setup` |
| `graphify` | pipx（`pipx install graphifyy`） | 独立 venv + 软链 | 无 Homebrew formula，走官方 PyPI 渠道（包名 `graphifyy`）；venv 位于 `~/.local/share/CodeKit/venvs/graphify`。不使用裸 `pip install`，因 brew python 受 PEP 668 保护会拒绝直接写入系统环境 |
| `gh` | Homebrew | — | |
| `glab` | Homebrew | — | |
| `dingtalk_cli` | npm 用户态安装（`npm install -g dingtalk-workspace-cli@latest --registry=https://registry.npmmirror.com`） | — | 命令为 `dws`；卸载时同时清理用户态和默认 npm 前缀 |
| `feishu_cli` | npm 用户态安装（`npm install -g @larksuite/cli@latest --registry=https://registry.npmmirror.com`） | — | 命令为 `lark-cli`；卸载时同时清理用户态和默认 npm 前缀 |

> 编程智能体（Claude / Codex / OpenCode）在前置检查阶段安装（见 [5.1](#51-前置检查run_preflight)），不属于组件选择器条目。

### 8.1 Marketplace 本地源（`MARKETPLACE_LOCAL`）

插件市场相关组件（`claude_plugin` / `opencode_plugin` / `codex_plugin` / `qoder_plugin` / `qodercn_plugin`）不再依赖写死的远端仓库地址：

- 脚本启动时按自身位置相对定位当前仓库：`SCRIPT_DIR` 下存在根目录的 `.claude-plugin/marketplace.json`、`.codex-plugin/marketplace.json`、`.claude-plugin/plugin.json`、`.codex-plugin/plugin.json`、`.opencode/package.json`、`.opencode/plugins/codekit.js`、`.qoder-plugin/plugin.json`、`plugin-version.json` 和 `skills/` 时，认定 `MARKETPLACE_LOCAL` 为仓库根目录；
- 安装时在 `~/.local/state/CodeKit/marketplace-source/codekit` 生成兼容的旧版插件源，在 `~/.local/state/CodeKit/opencode-source` 生成兼容的旧版 OpenCode npm 包，在 `~/.local/state/CodeKit/qoder-source/codekit` 生成 Qoder 插件源。Claude/Codex 注册前者，OpenCode 使用后者，Qoder 使用第三个源；版本追踪仍取本地仓库 `HEAD`；
- 无插件本地源（例如脚本目录保留 `scripts/` 但缺少根目录插件文件）：`MARKETPLACE_LOCAL` 保留为空，`main` 开头统一提示，相关组件各自告警并兜底跳过，**不影响其他组件**；
- 本地源安装的 marketplace 不配置自动更新（无远端轮询对象），脚本打印刷新方式：仓库内 `git pull` 后重新运行 `bash install.sh install`，由脚本重建兼容源并刷新插件；
- 历史兼容：已按旧远端地址注册的市场仍可被检测（按市场名识别）与卸载。

### 8.2 Codex 插件安装明细（`codex_plugin`）

- **市场源**：安装状态目录下的兼容市场根（由仓库根目录的 `.codex-plugin/` 和 `skills/` 重建；Codex 读取其中的 `.codex-plugin/marketplace.json`，条目 `path: "./"` 保持插件与市场根同目录）；本地源为空时提示并跳过注册
- **前置条件**：本机已安装 `codex` CLI，否则跳过并告警
- **迁移清理**：安装前删除旧散装机制遗留在 `~/.agents/skills/` 的 `keel-*` 技能（历史产品名为 keel；老版本通过远端 raw 逐个下载）

**安装流程**（`install_codex_plugin`）：

1. 注册市场：`codex plugin marketplace add <安装状态目录>/marketplace-source/codekit`（兼容市场根；若检测到旧路径下同名市场，会先移除旧注册再切换到兼容源；本地源为空则提示并跳过）
2. 已装该插件时先刷新市场快照：`codex plugin marketplace upgrade co-daming`（仅当市场源是 Git 地址；兼容源为本地目录时跳过刷新，下一次 `install.sh` 会先重建兼容源）
3. 安装/更新插件：`codex plugin add codekit@co-daming`（对已装插件即更新到快照版本；插件安装到 `~/.codex/plugins/cache/`，启用状态写入 `~/.codex/config.toml`）
4. 记录 marketplace SHA（`_record_marketplace_sha`）

**技能调用名**：插件内技能目录名即调用名（无插件前缀）——`$initialize`、`$technical-doc` 等 7 个（旧散装机制为 `$keel-initialize`，迁移后去掉前缀）。技能亦支持按 description 隐式触发。

**失败与版本策略**：

- 市场注册失败（本地路径注册失败且市场未注册）即告警返回非 0；`codex plugin add` 失败同样告警。
- 市场升级状态复用 marketplace 的仓库 SHA（`_get_marketplace_latest_sha`，取本地仓库 `HEAD`）：检查阶段通过 `codex plugin list --marketplace co-daming` 探测安装状态，再比对已存 SHA 与本地 `HEAD` SHA 判定 `up_to_date` / `installed`；这与插件包版本相互独立。
- 插件内容版本由根目录 `plugin-version.json` 的 `version` 参数统一管理；pre-commit 钩子会将它同步到 Claude、Codex、OpenCode 和 Qoder 清单，版本变更后驱动 Codex 缓存刷新。

### 8.3 Qoder 插件安装明细（`qoder_plugin` / `qodercn_plugin`）

Qoder（国际版）与 Qoder CN 是两个独立发行版，各有各的用户级配置根；但**同一发行版的 CLI 与桌面 IDE 共用同一个根**，因此一次安装两端同时生效：

| 平台 | 组件 ID | 默认配置根 | env 覆盖 | CLI 候选 |
|---|---|---|---|---|
| Qoder | `qoder_plugin` | `~/.qoder` | `QODER_CONFIG_DIR` | `qoder`、`qodercli` |
| Qoder CN | `qodercn_plugin` | `~/.qoder-cn` | `QODERCN_CONFIG_DIR` | `qodercn`、`qoderclicn` |

- **安装源**：`~/.local/state/CodeKit/qoder-source/codekit`，只含 `.qoder-plugin/plugin.json` 与 `skills/`。不生成根级 `agents/`（插件内 agents 在 Qoder 里是 `codekit:<名>` 命名空间名，而 CodeKit 技能按**裸名**调度 subagent，故改由全局 agent 目录提供）；也不做 `openai.yaml → agents/` 转写，该转写只服务 Codex/OpenCode。
- **两条等价路径**：探测到可用 CLI 时执行 `<cli> --config-dir <根> plugins install <源> --scope user --json`；只有桌面 IDE（或 CLI 过旧）时直接写共享存储——复制插件到 `<根>/plugins/cache/local/codekit/<版本>/`，在 `<根>/plugins/installed_plugins_v2.json` 登记 `codekit@local`，并把 `<根>/settings.json` 的 `enabledPlugins` 置真。两条路径产出同一份注册表。
- **能力探测三条件**：命令存在且不是 IDE 启动器（路径不含 `.app/Contents/`）、`--version` 可取到版本号、`plugins install --help` 同时包含 `--json` 与 `--scope`；任一不满足即降级为直写模式。
- **读侧容错**：桌面 IDE 写入的条目可能带 `lastUpdated`/`userVisible`/`enabled` 等额外键、无版本子目录的 `installPath`、6 或 9 位小数的时间戳。CodeKit 只增删自己的条目，其余键与其他插件原样保留；注册表或 settings 的 JSON 非法时报错终止，**绝不覆盖用户数据**。
- **状态判定不依赖 CLI**：`present` / `component_version` / 可升级判定都直接读共享注册表，只装了桌面 IDE 的机器同样能被检测、升级并出现在 recap 中。
- **不使用 Qoder marketplace，也不执行 `plugins update`**：升级方式是在新版本仓库里重新运行 `bash install.sh install`；同版本重装会整体替换缓存目录（新增文件生效、已删除文件消失），等价于升级。
- **跨操作系统不共享**：WSL 内安装只写 Linux 侧的 `~/.qoder-cn`，Windows 上的 Qoder IDE 读 `C:\Users\<name>\.qoder-cn`。安装器检测到这种另一侧的根时会提示一行，不跨根写入。

### 8.4 插件附带的全局 subagent 安装明细

安装源是各 Skill 目录下的 `skills/<skill>/agents/*.md`，每个文件对应一个独立角色。`initialize` 的三个 Worker 角色也在此目录中定义，并由其 `SKILL.md` 强制调用。选择插件时，安装器调用 `scripts/manage-global-agents.py --tool`，只同步对应平台（Qoder 系额外传 `--config-root`，跟随共享配置根的 env 覆盖）：

| 工具 | 全局目录 | 文件格式 |
|---|---|---|
| Claude Code | `~/.claude/agents/` | 原样复制 Markdown |
| OpenCode | `~/.config/opencode/agents/` | 原样复制 Markdown |
| Codex | `~/.codex/agents/` | 从同名 Markdown 生成 TOML，正文映射到 `developer_instructions` |
| Qoder | `~/.qoder/agents/` | 原样复制 Markdown（裸名，CLI 与桌面 IDE 共享） |
| Qoder CN | `~/.qoder-cn/agents/` | 原样复制 Markdown（裸名，CLI 与桌面 IDE 共享） |

安装具备幂等性：内容未变化时不改文件；发现同名且不是 CodeKit 管理的文件时，先备份到 `~/.local/state/CodeKit/agent-backups/<tool>/`。缺少本地 agent 源时只告警并跳过，不影响其他组件。

---

## 9. 卸载流程

`run_uninstalls` 按序清理（每项均容错，失败只告警）：

1. 移除 CodeKit 插件（Claude：先 `claude plugin uninstall codekit@co-daming` 卸载插件本体，再 `claude plugin marketplace remove` 移除市场注册）
2. 从 `opencode.json` 移除 CodeKit 插件
3. 移除 Codex 插件与市场注册（`codex plugin remove codekit@co-daming` + `codex plugin marketplace remove co-daming`，含清理旧散装 `keel-*` 技能）
4. 移除 Qoder / Qoder CN 插件（CLI 可用时先 `plugins uninstall codekit`，随后无条件清理共享配置根中的注册项、`enabledPlugins` 键与缓存目录）
5. 卸载 git-ai（含 hooks 清理、删除 `~/.git-ai`、清理 shell 中的 PATH 行）
6. 移除 CodeKit 全局 subagents（Claude/OpenCode/Codex/Qoder/Qoder CN，仅清理受管文件）
7. 卸载 rtk、glab、gh（brew）、GitNexus、钉钉 CLI、飞书 CLI（npm）、Graphify（pipx/pip + venv）
8. 撤销全局 git 配置
9. 移除各 shell 配置中 `# Added by CodeKit Plugin` 的 PATH 行
10. 删除版本记录文件

> 说明：
> - **不卸载**智能体本体（Claude/Codex/OpenCode/Qoder/Qoder CN）与依赖（git/Homebrew/curl/Python3/Node.js），仅卸载插件与可选组件。
> - `${CodeKit_STATE_DIR}` 与 `~/.npm-global` 目录会保留，需手动删除。

---

## 10. 安装后插件目录变化（五平台）

> 实测环境：Claude 2.1.259 / Codex 0.149.0 / OpenCode 1.18.20 / Qoder CN 1.1.48。各平台均为官方插件机制，以下目录行为均为本机实测观察。

总览：

| 平台 | 市场注册位置 | 插件实体位置 | 启用状态位置 |
|------|-------------|-------------|-------------|
| Claude | `~/.claude/plugins/known_marketplaces.json` + `settings.json` | `~/.claude/plugins/cache/co-daming/codekit/<版本>/` | `~/.claude/settings.json` 的 `enabledPlugins` |
| Codex | `~/.codex/config.toml` 的 `[marketplaces.*]` 段 | `~/.codex/plugins/cache/co-daming/codekit/<版本>/` | `~/.codex/config.toml` 的 `[plugins.*]` 段 |
| OpenCode | `~/.config/opencode/opencode.json` 的 `plugin` 数组 | `~/.config/opencode/node_modules/codekit/` | `~/.config/opencode/package.json` 的 `dependencies` |
| Qoder | 本地目录安装，无市场注册（`codekit@local`） | `~/.qoder/plugins/cache/local/codekit/<版本>/` | `~/.qoder/settings.json` 的 `enabledPlugins` |
| Qoder CN | 本地目录安装，无市场注册（`codekit@local`） | `~/.qoder-cn/plugins/cache/local/codekit/<版本>/` | `~/.qoder-cn/settings.json` 的 `enabledPlugins` |

### 10.1 Claude

安装后 `~/.claude/` 下的变化：

```
~/.claude/
├── settings.json                          # 新增：extraKnownMarketplaces.co-daming（市场源）
│                                          # 新增：enabledPlugins."codekit@co-daming": true（启用状态）
└── plugins/
    ├── known_marketplaces.json            # 市场注册：source / installLocation / lastUpdated
    ├── installed_plugins.json             # 插件安装记录：installPath / version / gitCommitSha
    └── cache/
        └── co-daming/
            └── codekit/
                └── <版本>/                 # 插件目录完整副本
                    ├── .claude-plugin/
                    │   ├── plugin.json
                    │   └── marketplace.json   # 市场与插件同目录，随插件一并复制
                    └── skills/
                        └── <技能名>/
                            ├── SKILL.md            # 7 个技能
                            ├── openai.yaml         # 界面元数据（Claude 不读取）
                            └── agents/openai.yaml  # 安装器按 Codex 约定派生
```

关键行为：

- **本地目录市场源不会被复制**：`known_marketplaces.json` 中 `source.source` 为 `"directory"`，`installLocation` 指向安装状态目录中的兼容市场源；该源由仓库根目录在每次安装时重建
- **版本策略**：插件版本由根目录 `plugin-version.json` 的 `version` 参数统一配置；安装器和卸载器的日志及状态记录均沿用该版本，兼容源也使用该版本
- **卸载残留**：`claude plugin uninstall` 只在 `cache/` 对应版本目录中追加 `.orphaned_at` 标记，不会物理删除；彻底清理需手动删除 `plugins/cache/co-daming/`

### 10.2 Codex

安装后 `~/.codex/` 下的变化：

```
~/.codex/
├── config.toml                            # 新增：[marketplaces.co-daming]
│                                          #         source_type = "local"
│                                          #         source = "~/.local/state/CodeKit/marketplace-source/codekit"
│                                          #         （兼容市场根，原地引用，不复制）
│                                          # 新增：[plugins."codekit@co-daming"]
│                                          #         enabled = true
└── plugins/
    └── cache/
        └── co-daming/
            └── codekit/
                └── <版本>/                 # 插件目录完整副本
                    ├── .codex-plugin/
                    │   ├── plugin.json      # version 来自 plugin-version.json
                    │   └── marketplace.json # 市场与插件同目录，随插件一并复制
                    └── skills/
                        └── <技能名>/
                            ├── SKILL.md
                            ├── openai.yaml             # 仓库源位置（与 SKILL.md 同级）
                            └── agents/openai.yaml      # Codex 实际读取的界面元数据
```

关键行为：

- 市场清单读取**兼容市场根**下的 `.codex-plugin/marketplace.json`（Codex 官方 repo-scoped 位置）；`plugin.json` 与 `marketplace.json` 同置于 `.codex-plugin/`
- **技能界面元数据**：仓库把 `openai.yaml` 与 `SKILL.md` 同级存放（保证 `agents/` 只放 subagent 源）；Codex 官方只读取 `skills/<名>/agents/openai.yaml`，因此 `_prepare_install_sources` 重建兼容源时调用 `_stage_skill_interface_files` 复制出该路径。仓库内不提交 `agents/openai.yaml`；任一技能缺 `openai.yaml` 时安装直接失败
- 版本目录名取自 `.codex-plugin/plugin.json` 的 `version`；该值由 `plugin-version.json` 配置并由 pre-commit 钩子同步，版本号递增驱动 Codex 插件缓存刷新
- 与 Claude 相同：`codex plugin remove` 不清理 `cache/` 子目录，彻底清理需手动删除 `plugins/cache/co-daming/`

### 10.3 OpenCode

OpenCode 将 `~/.config/opencode/` 本身当作一个 npm 包管理，插件安装即 npm 依赖安装（启动时由 Bun 自动执行）：

```
~/.config/opencode/
├── opencode.json                          # 新增：plugin: ["codekit@file://<仓库绝对路径>"]
├── package.json                           # opencode 自动维护：把插件写入 dependencies
├── package-lock.json / bun.lock           # 依赖锁文件（自动生成）
└── node_modules/
    └── codekit/                              # 本仓库作为 npm 包整体安装
        ├── package.json                   # name: codekit；main: plugins/codekit/.opencode/plugins/codekit.js
        └── plugins/codekit/
            ├── .opencode/plugins/codekit.js  # 兼容包内插件入口：解析技能并注册裸命令
            └── skills/<技能名>/SKILL.md      # 兼容包内的 7 个技能（openai.yaml 随目录复制，OpenCode 不读取）
```

关键行为：

- 插件条目是 npm 包说明符：本地安装为 `codekit@file://<安装状态目录>/opencode-source`（旧式远端为 `codekit@git+<url>`）
- 安装器把根目录的 `.opencode/` 和 `skills/` 重建到兼容包的 `plugins/codekit/` 下；包 `main` 仍指向 `plugins/codekit/.opencode/plugins/codekit.js`，启动时解析 7 个技能并注册为裸命令
- `node_modules`、`package.json`、锁文件等由该目录的 `.gitignore` 忽略

### 10.4 Qoder / Qoder CN

`~/.qoder`（国际版）与 `~/.qoder-cn`（CN 版）结构完全一致；下面以 CN 版为例，两个发行版互不共享配置根：

```
~/.qoder-cn/
├── settings.json                          # 新增：enabledPlugins."codekit@local": true（启用状态）
├── agents/                                # 新增：CodeKit 全局 subagent（裸名，CLI 与 IDE 共用）
│   └── <角色名>.md
└── plugins/
    ├── installed_plugins_v2.json          # 共享注册表：scope / installPath / version / installedAt
    └── cache/
        └── local/
            └── codekit/
                └── <版本>/                # 插件目录完整副本
                    ├── .qoder-plugin/
                    │   └── plugin.json    # version 来自 plugin-version.json
                    └── skills/
                        └── <技能名>/
                            ├── SKILL.md            # 7 个技能
                            ├── openai.yaml         # 界面元数据（Qoder 不读取）
                            └── agents/*.md         # 技能内 subagent（Qoder 不提升为插件级 agent）
```

关键行为：

- **CLI 与桌面 IDE 读写同一份注册表**：`installed_plugins_v2.json` 与 `settings.json` 由两端共同维护，所以 CodeKit 只面向配置根安装一次，两端都能看到 `codekit@local`
- 本地目录安装的插件固定登记为 `codekit@local`（`local` 是本地目录安装的隐式市场名），不注册 Qoder marketplace
- `installed_plugins_v2.json` 中其他插件（如桌面 IDE 自带的 `*-@qoderapp-bundler` 条目）与 `userVisible`/`lastUpdated` 等未知键一律保留
- 卸载按 `installPath` 定位，仅当该路径确实位于 `<根>/plugins/cache/` 之内才删除目录，越界路径只清理登记项而不删文件

### 10.5 源包结构合规性（对照官方规范）

| 平台 | 官方规范要求 | 本仓库实现 | 结论 |
|------|-------------|-----------|------|
| Claude | 插件根含 `.claude-plugin/plugin.json`；技能为 `skills/<名>/SKILL.md`；市场清单在**市场根** `.claude-plugin/marketplace.json`（必填 `name`/`owner`/`plugins`，`source` 相对路径以 `./` 开头） | 安装器从仓库根目录重建兼容插件根：`.claude-plugin/plugin.json` + `.claude-plugin/marketplace.json`（`source: "./"`）+ `skills/`（7 技能） | ✅ 符合 |
| Codex | 插件根含 `.codex-plugin/plugin.json`；市场清单在**市场根** `.codex-plugin/marketplace.json`，条目用 `source.path`（以 `./` 开头） | 安装器从仓库根目录重建兼容插件根：`.codex-plugin/plugin.json`（版本来自 `plugin-version.json`）+ `.codex-plugin/marketplace.json`（`path: "./"`）+ `skills/` | ✅ 符合 |
| Qoder / Qoder CN | 插件根含 `.qoder-plugin/plugin.json`（必填 `name`，`version` 等可选）；技能为 `skills/<名>/SKILL.md`；用户级插件状态存于共享配置根 | 安装器从仓库根目录重建插件根：`.qoder-plugin/plugin.json`（版本来自 `plugin-version.json`）+ `skills/`（7 技能），按 `user` 作用域登记到 `<根>/plugins/installed_plugins_v2.json` | ✅ 符合 |
| OpenCode | 插件为导出插件函数的 JS/TS 模块，以 npm 包形式经配置 `plugin` 数组分发 | `.opencode/package.json`（`name: codekit`，`main: plugins/codekit.js`）；安装器生成旧版兼容 npm 包并将其 `main` 改为 `plugins/codekit/.opencode/plugins/codekit.js`；`codekit.js` 导出 `codekitPlugin` 异步函数 | ✅ 符合 |

> 注：Claude 与 Codex 的插件缓存目录都会原样复制整个插件目录（包括其他平台的清单目录，如 Claude 缓存中会含 `.codex-plugin/`、`.opencode/`），属无害冗余。

---

## 11. 状态文件与日志

| 路径 | 用途 |
|------|------|
| `~/.local/state/CodeKit/install.log` | 安装日志（所有 `info`/`warn`/`error`/`success` 均落盘，带 `[INFO]` 等可 grep 标签） |
| `~/.local/state/CodeKit/installed_versions` | 各组件版本记录（存在即代表"已安装过"，影响默认动作） |
| `~/.local/state/CodeKit/config` | 持久化配置（历史版本遗留文件，当前版本不再写入） |
| `~/.local/bin/` | 用户级可执行文件目录 |
| `~/.npm-global/` | npm 用户态全局安装前缀 |
| `~/.local/share/CodeKit/venvs/graphify` | Graphify 的 venv 安装目录（pipx 缺失时的兜底策略） |
| `~/.claude/agents/`、`~/.config/opencode/agents/`、`~/.codex/agents/`、`~/.qoder/agents/`、`~/.qoder-cn/agents/` | CodeKit 全局 subagent；Codex 目录使用由 Markdown 生成的 TOML，Qoder 系目录与桌面 IDE 共享 |
| `~/.qoder/`、`~/.qoder-cn/` | Qoder CLI 与桌面 IDE 共享的配置根（`plugins/installed_plugins_v2.json`、`settings.json`）；可用 `QODER_CONFIG_DIR` / `QODERCN_CONFIG_DIR` 覆盖 |
| `~/.local/state/CodeKit/agent-backups/` | 同名非 CodeKit agent 被安装器替换前的备份 |
| `plugin-version.json` | 插件版本唯一配置源；pre-commit 钩子同步到 `.claude-plugin/plugin.json`、`.codex-plugin/plugin.json`、`.opencode/package.json` 和 `.qoder-plugin/plugin.json` |
| `~/.agents/skills/` | **旧散装机制**的 Codex skills 目录（历史遗留）：当前版本**不再写入**，仅在安装/卸载时清理其中遗留的 `keel-*` 条目；插件机制下技能实体位于 `~/.codex/plugins/cache/`（见第 10 章） |

> 日志输出采用**双通道**：终端用干净符号（✓/✗/⚠），日志文件保留 `[OK]`/`[ERROR]` 等标签。文件描述符 3 固定指向终端，保证管道中仍能向屏幕输出。

---

## 12. 关键助手函数速查

| 函数 | 作用 |
|------|------|
| `_cmd_tee` | 执行命令并把输出同时写日志与终端，返回命令真实退出码（`PIPESTATUS[0]`） |
| `_setup_brew_mirrors` | 注入 Homebrew 中科大镜像源（`HOMEBREW_BREW_GIT_REMOTE` / `HOMEBREW_API_DOMAIN` / `HOMEBREW_ARTIFACT_DOMAIN`），供 `brew update`/`brew install` 加速 |
| `_set_npm` | 设置 npm 用户态前缀并加入 PATH |
| `_load_component_registry` | 调用 `scripts/component-registry.py` 校验并加载组件注册表，同时检查三类处理函数是否齐全 |
| `_dispatch <前缀> <组件>` | 表驱动分发调用 `check_<组件>` / `install_<组件>` / `uninstall_<组件>` |
| `_component_version <组件>` | 组件版本探测唯一入口 |
| `_check_up_to_date <组件>` | 已装版本 ≥ 最新版时把状态改写为 `up_to_date`（避免本地更新时提示降级） |
| `_comp_status_tag <组件>` | 生成选择器行尾状态文案：`(not installed)` / `(x → y, 勾选可升级)` / `(v latest)` |
| `_brew_install_or_upgrade <formula> [probe]` | 未装则 `brew install`，已装则 `brew upgrade`（无新版时提示 already latest 并保持已装可用） |
| `_json_edit <node脚本> <py脚本>` | 用 node（优先）或 python3 编辑 JSON |
| `_sed_inplace <脚本> <文件>` | 跨平台原地 sed（先 BSD 后 GNU） |
| `_git_ai_cmd` | 解析 git-ai 可执行路径（兼容 `~/.git-ai/bin`） |
| `run_preflight` | 前置检查入口：Homebrew → git（≥ 2.50.0）→ Node.js（≥ 20）→ Python3 → curl → 编程智能体（含原硬性依赖；缺失时 brew 自动安装，失败退出流程；唯一例外：brew update 失败仅警告并继续） |
| `agent_presence <claude\|codex\|opencode\|qoder\|qodercn>` | 统一探测对应 CLI、Desktop 应用、Linux 启动器和常见安装目录；Qoder 系由 `scripts/installer_qoder.py` 提供探测，并把共享配置根也算作平台可用 |
| `render_recap` | 所有安装与配置操作完成后打印安装/卸载结果汇总 |
| `_run_python_selector <install\|uninstall>` | 生成临时组件清单，调用 `scripts/component-selector.py`，校验并回填选择结果 |
| `_marketplace_remote_host` | 从当前仓库 `origin` 推导 Git 主机名（供 git-ai `allow_repositories` 与安装后 glab 指引使用；无本地源时无输出） |
| `_prepare_install_sources` | 从仓库根目录重建 Claude/Codex 市场源、OpenCode 兼容 npm 包和 Qoder 插件源 |
| `installer_qoder.py` | Qoder 平台适配层：共享根解析、注册表与 settings 读写、CLI 能力探测、直写安装与卸载、平台可用性探测 |
| `_stage_skill_interface_files` | 在生成的兼容源里把 `skills/<名>/openai.yaml` 派生为 Codex 读取的 `agents/openai.yaml`，并校验每个技能都具备该文件 |
| `_get_marketplace_latest_sha` | 取本地仓库 `HEAD` 作为 marketplace 版本（无本地源时为空，调用方兜底） |
| `_extract_ver` | 从版本字符串提取语义化版本号 |
| `_ver_ge` | 点分版本号大小比较（`$1 >= $2` 返回 0，bash 3.x 兼容），用于 git ≥ 2.50.0 等版本门槛 |
| `_track_change <路径>` | 记录本次运行变更的文件/目录（去重），供安装后检测汇总打印 |
| `_verify_dep <名> <命令>` | 验证汇总表中的依赖组件行（Homebrew/git/Node.js/Python3/curl） |
| `report_plugins_and_changes` | 打印各平台插件安装目录与本次变更的文件/目录 |
| `_set_status` / `_get_status` 等 | 读写组件状态/版本/最新版（`eval` 动态变量名） |

---

## 13. 如何新增一个组件

得益于表驱动设计，新增组件只需 **4 步**：

1. 在 `scripts/component-registry.py` 的 `COMPONENTS` 中追加一个 `Component(...)`；新增分组时同时更新 `GROUPS`。
2. 编写 `check_<组件>` 函数：探测是否已装、当前版本、最新版本，写入状态（已装时用 `_check_up_to_date` 判定是否落后）。
3. 编写 `install_<组件>` 函数：实现安装逻辑及已安装时的升级/刷新路径（用户态、可回退）。
4. 编写 `uninstall_<组件>` 函数：只移除该组件管理的内容，并保留用户资产。

先运行 `python3 scripts/component-registry.py --check`；之后 `check_all_components`、`run_selected_installs`、`run_uninstalls` 和交互选择器会自动识别新组件，无需改动分发逻辑。缺少任一处理函数时，`install.sh` 会在执行组件检查前快速失败。

> 若该组件有独立版本命令，可在 `_component_version` 中补一个 `case` 分支。

---

## 14. 行为与退出码约定

| 场景 | 行为 |
|------|------|
| Homebrew 安装失败 | 退出流程（非 0） |
| brew update 失败 | 警告并继续（瞬时网络问题，不影响后续安装） |
| 硬性依赖（git ≥ 2.50.0 / Node.js ≥ 20 / Python3 / curl）安装失败或装后仍不满足要求 | 退出流程（非 0） |
| 无编程智能体且用户选择退出 | 结束流程（退出码 0） |
| 无编程智能体且非交互模式（`-y`/无 tty） | 默认安装 Claude |
| 传入已取消的 `upgrade` 子命令 | 报错提示改用 `install`，退出码 1 |
| 未知参数 | 打印用法，退出码 1 |
| `-h`/`--help` | 打印用法，退出码 0 |
| 单组件安装失败 | 告警并继续安装其余组件 |
| 无任何组件被选中 | 打印提示并退出码 0 |
| 卸载未确认（非 `-y` 且无交互终端） | 拒绝执行，退出码 1 |

> 脚本以 `bash 3.x` 为最低兼容目标（macOS 自带 3.2），不使用关联数组、`mapfile` 等高版本特性。

---

*文档版本：插件版本见根目录 `plugin-version.json`。如脚本更新，请同步维护本文档。*

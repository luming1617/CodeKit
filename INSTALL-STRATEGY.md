# CodeKit 安装策略

本文档是安装策略的**人类可读镜像**。唯一事实源是 `scripts/component-registry.py`：
所有安装、卸载、状态检测与智能体 CLI 安装代码都读取同一张表，不得在别处再写一份通道定义。
改动通道只需改该模块，并同步更新本文档。

## 1. 两阶段模型

| 阶段 | 时机 | 行为 |
|---|---|---|
| Preflight（强依赖） | 正式安装前 | **只检测，不代装**。任一不满足即醒目提示并中止，不会继续安装 |
| 可选组件安装 | 选择组件后 | 按组件的通道表与全局优先级探测安装 |

强依赖是操作系统级工具，安装器**不会**替用户安装（不再自动装 Homebrew、Node、Python、git、curl），
只打印缺失项、当前版本、要求版本和各平台安装方式。

这套检测三平台共用一份实现（`scripts/installer_preflight.py`，由 `Installer.run` 在组件动作
之前调用）。入口脚本只做一件无法委托的事：解析出一个满足下限的系统 Python 再转发过去。

### 1.1 Preflight 门槛

| 依赖 | 门槛 | 说明 |
|---|---|---|
| Node.js | **>= 24.0.0** | 全局门槛取 24 LTS 主版本；gitnexus 另做组件级校验（见 §4） |
| npm | 存在即可 | 随 Node 提供，是绝大多数组件的首选通道 |
| python3 | **>= 3.10** | 只用于运行安装器本身（首次安装由它写下内置运行时，见 §4.2）。下限唯一声明在 `installer_config.MINIMUM_PYTHON`，两个入口脚本各自复述一次（入口必须自己证明解释器合格，这是唯一无法委托的检查），漂移由 `scripts/test_installer_preflight.py` 兜住；组件侧已与系统 Python 脱钩，graphify 的 `python>=3.10` 依赖比对的是内置运行时的版本 |
| git | **存在即可** | 不再校验版本；仅用于读取 marketplace 提交号与写入全局 git 配置 |
| curl / wget | **任一存在** | 用于脚本与发布包下载 |

Windows 上这两条探测方式与 POSIX 不同（详见 §8）：

- Python 走 `py -3`（其次 `python`）。**不能用 `python3`**：干净 Windows 上该名字是
  微软商店的别名桩，执行后无输出，会被误判成"已安装"。
- `curl` 必须按「真实可执行文件」解析。PowerShell 5.1 内置 `curl`/`wget` 别名指向
  `Invoke-WebRequest`，按别名调用会绕过安装器真正需要的 `curl.exe`。

中止时一次性列出**全部**缺失项及其安装命令（示例）：

```
Node.js  >= 24.0.0   当前: 未安装
  安装方式: brew install node@24 | nvm install 24 | fnm install 24
  Debian/Ubuntu: https://deb.nodesource.com/setup_24.x
```

> 变更说明：历史上 git 因 git-ai 的 hook 要求被强制为 `>= 2.50.1`。git-ai 已从可选组件中移除，
> 该版本门槛随之取消。

## 2. 通道优先级（所有平台统一）

代码按下列顺序**探测**，取第一个「已声明且当前可用」的通道，不做逐层试错：

```
npm  →  brew  →  brew_cask  →  apt  →  winget  →  pipx  →  venv  →  release  →  script  →  powershell
```

`release` 排在 `script` 之前：发布资产按精确文件名解析并做摘要校验，而 `script` 是把远程
脚本管道给 shell；同一组件同时声明两者时，先走可校验的那条路。

`winget` 与 `powershell` 是 Windows 专用通道，可用性探测在其他平台恒为假，因此只是占住
对应原生通道的位置，不改变 macOS/Linux 的选择结果。

**落点约束**：`Component.entry_kind` 决定一个组件可以落在哪里（`gitnexus` / `graphify`，见 §4.2）。空值表示宿主自己的用户级目录；非空表示由 CodeKit 工具链根接管，此时候选通道被**过滤**成该 `entry_kind` 允许的那一根根内通道（`ENTRY_KIND_CHANNELS`）——不是把根内通道排到前面，而是根外通道根本不在候选里，所以根内通道失败之后不存在「退到根外产出一份受限宿主读不到的副本」这条路。运行时不可用时该组件如实失败。`validate_registry`（`--check` 与 pre-commit 都跑）会拒绝漏配 `entry_kind` 的 `venv` 声明，也拒绝根接管组件混入根外通道。

| 通道 | target | 可用性判定 | 安装 / 升级语义 |
|---|---|---|---|
| `npm` | 官方 npm 包名（白名单） | `npm` 存在 | `npm install -g <pkg>@latest`（含升级）；落点按 `entry_kind` 分：空值装进用户态前缀，`npm-private` 装进 `<工具链根>/npm` 并给人类 shell 建一个可选转发入口 |
| `brew` | formula | `brew` 存在 | 已装且由 brew 管理则 `brew upgrade`，否则 `brew install`；命令已存在但不在 brew 管理下时**让位给下一通道**（避免装出第二份拷贝）；强制中科大镜像并关闭 brew 自动更新 |
| `brew_cask` | cask | `brew` 存在 | 已装且由 brew 管理则 `brew upgrade --cask`，否则 `brew install --cask`；同样让位条件；镜像环境同 `brew` |
| `apt` | 发行版包名 | Linux 且有包管理器 | `apt-get install -y`（需 sudo，见 §3）；包不存在则跳到下一通道 |
| `winget` | winget 包 ID | **Windows 且 `winget` 存在** | 语义与 `brew` 对齐：命令缺失才 `winget install`；命令已存在时先用 `winget list` 确认由 winget 管理，再由 `winget upgrade` 升级，**"已是最新"返回非零（实测 `0x8A15002B`）不算失败**，否则会降级到 release 装出第二份；不在 winget 管理下则让位给下一通道。非交互必须带 `--accept-package-agreements --accept-source-agreements --silent --disable-interactivity`，否则 winget 会挂起等待输入 |
| `venv` | PyPI 发行名 | **内置运行时可用且能建 venv**（`<运行时> -c "import venv, ensurepip"`） | 用内置运行时 `-m venv --copies` 建到 `<工具链根>/venvs/<命令>`，再 `pip install -U <发行名>`；始终可重装。落点必然在根内，因此**只有 `entry_kind` 非空的组件可以声明它** |
| `release` | `Component.release` | `curl` 或 `wget` 存在 | 解析最新版 → 按模板展开资产名 → 下载 → 校验（如可）→ 解包 → 安装到用户 bin 目录；GitHub 地址优先走 gh-proxy。模板除 `{version}/{os}/{arch}/{ext}` 外还支持按 Rust target triple 命名的 `{target}` |
| `script` | 官方安装脚本 URL | **非 Windows** 且存在可用的 POSIX shell | 下载后经 stdin 交给 shell 执行；GitHub 地址优先走 gh-proxy（见 §2.3）。Windows 上禁用该通道：Git for Windows 会把 `bash` 放进 PATH，只判断"有没有 shell"会让安装器用 Git Bash 跑 Unix 脚本（见 §8.3） |
| `powershell` | Windows 专用 `.ps1` URL | **Windows 且存在 powershell** | 下载后经 stdin 交给 PowerShell 执行（`-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command -`） |

> `script` 的可用性判定从「有 curl/wget」改为「有 POSIX shell」：脚本下载本身由
> `urllib` 完成，真正的前置是解释器。Windows 上 `curl.exe` 存在但没有 bash，旧判定会
> 让组件误入 `script` 通道后失败。`powershell` 同理严格限定 Windows：`pwsh` 在
> macOS/Linux 上也可能存在，但那些 `.ps1` 是 Windows 专用安装脚本。


### 2.1 npm 白名单（防同名李鬼）

`npm` 通道的 target 必须在 `OFFICIAL_NPM_PACKAGES` 白名单内，否则注册表校验直接失败。
原因是以下常见名字在 npm 上并非官方项目，裸名安装会静默装错包：

| 名字 | npm 上实际是什么 | 本项目组件 |
|---|---|---|
| `gh` | node-gh/gh（停更） | 官方 GitHub CLI（改用 brew/apt/release） |
| `rtk` | cliffano 的 release 工具 | Rust Token Killer（改用 brew/script） |
| `glab` | npm 不存在 | 官方 GitLab CLI（改用 brew/apt/release） |
| `@git-ai/cli` | code-ba 的提交信息生成器 | 已移除，不再安装 |

### 2.2 同一通道的两种失败语义（brew 为例）

brew 通道返回「是否已接管该组件」，而不是「命令是否存在」，避免把失败路径跑第二遍，
也避免组件的升级永远卡在错误的通道上：

1. 命令不存在 → 走 `brew install`；失败即返回失败，降级到下一通道。
2. 命令存在且由 brew 管理 → 走 `brew upgrade`；失败时命令仍可用，**不降级**（否则会
   在 script/release 再装一份把 brew 的拷贝遮住），只告警。
3. 命令存在但不在 brew 管理下（例如官方脚本装的 rtk）→ **直接让位**给下一通道，
   不执行 `brew install`/`brew upgrade`；真正能完成升级的是当初安装它的那个通道。

### 2.3 镜像与代理（国内网络）

| 目标 | 镜像 / 代理 | 注入位置 |
|---|---|---|
| npm / npx | `https://registry.npmmirror.com` | `installer_config.NPM_REGISTRY` → `set_npm` |
| Homebrew | `https://mirrors.ustc.edu.cn/…`（`brew.git`、`homebrew-core.git`、`homebrew-cask.git`、`homebrew-bottles`、`homebrew-bottles/api`） | `installer_config.HOMEBREW_MIRROR_ENV` → `Installer.brew` 写入进程环境 |
| GitHub 下载（脚本 / release 资产） | `https://gh-proxy.com/<原地址>`，失败回退直连 | `installer_config.download_urls` |
| GitHub 版本探测 | 同上，只走 gh-proxy（不再等一次必然超时的直连） | `installer_config.probe_urls` |

Homebrew 镜像同时把 `HOMEBREW_NO_AUTO_UPDATE=1` 写入环境：组件是否需要升级由安装器
自行判断，不需要每次 `brew install/upgrade` 前再同步一遍 homebrew/brew、core 与 cask。

## 3. sudo 行为（apt 通道）

- 已是 root：直接调用包管理器，不加 `sudo`。
- 非 root：使用 `sudo`。先探测 `sudo -n true`：
  - 免密可用 → 直接执行；
  - 需要密码且当前是交互 TTY → 允许 `sudo` 提示一次；
  - 需要密码且非交互（CI）→ **跳过 apt 通道**并提示原因，降级到下一通道，不阻塞等待。
- 所有特权命令记录到安装日志；`apt-get update` 每次运行只执行一次。

## 4. 组件策略表

`requires` 为该组件自身的硬前置，不满足时**只跳过该组件**并给出指引，不影响其他组件。

| 组件 | 分组 | 命令 | npm | brew | apt | winget | venv | script | release | requires |
|---|---|---|---|---|---|---|---|---|---|---|
| rtk | ai | `rtk` | — | `rtk` | — | — | — | 官方 install.sh | GitHub `rtk-ai/rtk`（`rtk-{target}.{ext}`） | — |
| gitnexus | intel | `gitnexus` | `gitnexus` | — | — | — | — | — | — | **node >= 24.11.0** |
| graphify | intel | `graphify` | — | — | — | — | `graphifyy` | — | — | python >= 3.10 |
| gh | third | `gh` | — | `gh` | `gh` | `GitHub.cli` | — | — | GitHub `cli/cli` | — |
| glab | third | `glab` | — | `glab` | `glab` | `GLab.GLab` | — | — | GitLab `34675721` | — |
| dingtalk_cli | third | `dws` | `dingtalk-workspace-cli` | — | — | — | — | — | — | — |
| feishu_cli | third | `lark-cli` | `@larksuite/cli` | — | — | — | — | — | — | — |

`entry_kind`（落点）：`gitnexus = npm-private`、`graphify = python-venv`，其余为空，落在宿主自己的
用户级目录。前两者由 CodeKit 工具链根接管，见 §4.2。

Windows 侧的三处差异：

- **发布资产令牌补了 `windows`**：gh 用 `gh_{version}_windows_{arch}.zip`，glab 用
  `glab_{version}_windows_{arch}.zip`（两者都是**全小写** `windows`，已对真实资产核对），
  rtk 用 Rust 三元组 `rtk-x86_64-pc-windows-msvc.zip` 且扩展名是 `zip`（无 Windows arm64 资产）。
- **解压工具不再写成静态 `requires`**：钉钉的 `install.js` 在 win32 分支用 PowerShell 的
  `Expand-Archive` 解压 zip，Windows 上并不需要 `unzip`；把它写成 `requires=("unzip",)`
  会让整个组件在 Windows 上被误判为不可安装。该前置由 `installer.ensure_unzip()` 按平台
  判断，macOS/Linux 缺失时仍会尝试补装。
- **`gh`/`glab` 在 Windows 上没有 brew/apt**，靠 `winget` + `release` 两条通道覆盖。

插件组件（claude/opencode/codex/qoder/qodercn）不声明通道，其 CLI 前置按下表安装。

### 4.1 组件级版本校验（gitnexus）

全局 Node 门槛是 `>= 24.0.0`，但 gitnexus 的 npm `engines` 是 `^22.18.0 || >=24.11.0`。
由于 npm 默认**只把 engines 当警告**，安装器必须在调用 npm 之前显式校验：Node 24.0–24.10 直接跳过
gitnexus 并提示升级到 24.11+，其余组件照常安装。

### 4.2 工具链根与内置运行时（gitnexus / graphify）

`gitnexus` 与 `graphify` 的落点不遵循「装到用户级默认目录」的惯例，而是**由 CodeKit 自持工具链根接管**（注册表里声明 `entry_kind`，安装器的 `TOOLCHAIN_COMPONENTS` 由它派生）。原因是受限宿主（实测 Codex 桌面端）的沙箱**按路径拒绝访问**——`%APPDATA%\npm` 下的入口连 `stat` 都拿不到（EACCES），`%LOCALAPPDATA%\Programs\Python` 整个目录不可达，而 `<home>/.local/share/CodeKit` 可读可执行（仅写入被拒）。

| 关注点 | 规则 |
|---|---|
| 根路径 | `<home>/.local/share/CodeKit`，三系统同一路径（`installer_platform.toolchain_root`），根内布局全部由它派生 |
| 内置运行时 | python-build-standalone 的 `install_only` 资产，按 OS/arch 固定 tag 与 sha256，解到 `runtime/<tag>/` 并原子发布；发布前必须通过「`import venv, ensurepip` + 建一次 venv」冒烟。**`RUNTIME_SPECS` 就是支持矩阵**（`supported_runtime_targets()`）：矩阵外的组合（如 `windows/arm64`）如实报「不支持」并点名矩阵，不悄悄改用工具链根外的解释器 |
| gitnexus | `entry_kind = npm-private`：走 npm 通道但装进**私有前缀** `<根>/npm`（`npm_config_prefix` 临时指向该前缀再还原），运行表达式记为绝对 `[<node>, <cli.js>]`；人类 shell 另留一份转发垫片，CodeKit 代码路径不依赖它。**解释器就是宿主 node，这是有意的边界**：venv 会 re-exec 记在自身之外的基础解释器，所以 Python 必须纳管；`node + 绝对脚本` 没有这条链，实测宿主 `node.exe` 可达，且 gitnexus 的原生模块全是 N-API（`prebuilds/<platform>-<arch>`、`bin/napi-v6`），跨 major 稳定 |
| graphify | `entry_kind = python-venv`：venv 建在 `<根>/venvs/<命令>`，**基础解释器必须是内置运行时**（`--copies`）；系统 Python 建出的 venv 其 `pyvenv.cfg` 会指向被拒绝的目录，启动必失败（实测 rc=101）。它是唯一的候选通道，因此运行时不可用时该组件**如实失败**，不会退到根外产出一份宿主读不到的副本 |
| 清单 | `<根>/toolchain.json`（POSIX 下 `0600`）由安装器独占写入，**整份重写但覆盖面不止本次勾选**：本次目标 + 本机实测已装的托管组件 + 清单里已有记录的，否则只升级其中一个工具就会把另一个的条目抹掉，技能侧退回按命令名猜路径。字段只保留有读者的（`schema_version` 与每工具的 `state`/`runner`/`version`/落点目录），运行时的 tag 由 `current.json` 承载。它是**提示而非权威**，技能侧逐条复验后才采信（解析顺序与复验规则见 `skills/_shared/scripts/codekit_runtime.py` 的 `resolve_tool`） |
| 落点即状态 | 根接管组件的「已安装」与版本一律按**落点实测**判定（`installer_toolchain.entry_state`，与清单条目同一个判据），不按命令名走 PATH：受限宿主里 PATH 条目齐全而 `stat` 被拒，按名字解析两个方向都会读错 |
| 宿主注册表达式 | 各宿主拿到的启动表达式**以实测为准，不强行统一**：Claude / Codex 的 MCP 条目由 `gitnexus setup` 自己决定，CodeKit 只保证 Qoder 的注册与 `toolchain.json` 里那一条。报告要写清「这个宿主实际是什么」，而不是假设四家一样 |
| 自动迁移 | 安装器检测到「已装但落点不在根内」的组件会就地重物化，**不需要用户重新勾选**；重建会让旧 venv 与 `graphify-out/` 缓存作废 |
| 升级 | 运行时按 tag 并列保留上一个可用版本；tag 升级 = 一次新下载 + 重建全部 venv，属 CodeKit 的维护职责（改 `RUNTIME_SPECS` 的 pin），不是重跑安装器的副作用 |
| 卸载 | 默认保留工具链根（同机多仓库共用一份运行时），`uninstall.sh --purge` / `uninstall.cmd --purge` 才移除 |
| 引导限制 | 系统 Python 只用来运行安装器，且**安装器不复用内置运行时运行自己**（运行时正是要修复的对象，自引用会让它损坏后无法修复） |

## 5. 智能体 CLI 通道

| 平台 | 组件 | npm | brew / cask | winget | script（sh） | powershell |
|---|---|---|---|---|---|---|
| Claude Code | `claude_plugin` | `@anthropic-ai/claude-code` | cask `claude-code` | `Anthropic.ClaudeCode` | — | — |
| Codex | `codex_plugin` | `@openai/codex` | cask `codex` | — | — | — |
| OpenCode | `opencode_plugin` | `opencode-ai` | formula `opencode` | — | — | — |
| Qoder | `qoder_plugin` | — | — | — | `https://qoder.com/install` | `https://qoder.com/install.ps1` |
| Qoder CN | `qodercn_plugin` | — | — | — | `https://static.qoder.com.cn/qoder-cli-cn/install.sh` | `https://static.qoder.com.cn/qoder-cli-cn/install.ps1` |

智能体 CLI 只在该平台插件被勾选时安装；插件安装以 CLI 为前置。

Qoder 系官方按平台分发安装脚本（macOS/Linux 是 `install.sh`，Windows 是 `install.ps1`），
两条通道互斥，所以同一张表可以两平台通用。安装失败时的手动提示也按平台给出：Windows 上
不会再提示 `curl … | bash`（Windows 没有 bash）。

> **Codex 的 marketplace 清单位置（跨平台修正）**：Codex 只在
> `<marketplace-root>/.agents/plugins/marketplace.json` 找清单，放在
> `.codex-plugin/marketplace.json` 会被判为 `marketplace root does not contain a
> supported manifest`。仓库现在两处都提供：`.agents/plugins/` 是 Codex 读的清单，
> `.codex-plugin/` 提供插件自身的元数据（`plugin.json`），二者都需要。

> **OpenCode 的配置文件选择**：OpenCode 按 `opencode.json` → `opencode.jsonc` →
> `.opencode/opencode.json` → `.opencode/opencode.jsonc` 的顺序取第一个存在的文件，
> 而 `opencode plugin -g` 写的也是这个结果。安装器因此不再固定写 `opencode.json`，
> 改用 `installer_config.opencode_config_file()` 解析出同一份文件，避免同一个插件
> 同时出现在两份配置里；读取容忍 JSONC 的注释与尾随逗号，写回时按标准 JSON 落盘
> （与 OpenCode 自身 `plugin -g` 的行为一致）。

## 6. 探测与幂等

1. 已安装且版本满足 → 结束，不重装。
2. 否则按 `CHANNEL_ORDER` 取第一个「组件已声明 + 当前可用」的通道执行。
3. 通道执行后校验命令是否可用；不可用则记录真实错误并继续下一通道。brew/cask 另有
   「让位」判定（见 §2.2）：命令虽可用但不由 brew 管理时不算成功，继续下一通道，
   让当初安装它的通道完成升级。
4. 通道失败不重复执行同一动作（不会把失败路径再跑一遍），单个组件失败不阻断其余
   组件；结果在收尾汇总表中呈现。

## 7. 卸载

卸载按同一张表反向执行：npm 用 `npm uninstall -g`，brew/apt 用对应包管理器移除，
`winget uninstall` 移除 winget 接管的组件；graphify 删除私有 venv 与命令入口，并顺带清理
历史版本用 pipx 装过的那一份（`pipx uninstall`）。卸载是幂等的：组件不存在时不调用破坏性操作。

Windows 上的对应关系：包管理器分支为 `winget uninstall --id <id> --exact --silent
--accept-source-agreements --disable-interactivity`；shell 配置清理为空（没有可写的 rc 文件）；
venv 与发布包的清理要同时尝试 `<name>.exe` 与 `<name>.cmd` 两种落盘形态。

winget 的 uninstall **不会自己提权**，而 gh 是机器级 MSI：非管理员会话里 msiexec 被 Windows
Installer 直接拒绝（事件 11730，Error 1730），winget 再把 1603 包成不透明的 `0x8A150030`。
卸载器按 §3 的 sudo 策略只对这条命令提权：非管理员先静默试一次（glab 这类用户态包就此成功，
不打扰用户），确实失败且当前会话弹得出 UAC 时才用 `platform_layer.run_elevated` 重跑同一条
命令（退出码经临时文件回传）；弹不出 UAC（CI、无桌面）或用户点了「否」时，直接给出
「请在管理员终端重新运行卸载」，不再抛退出码。

`git config --global --unset <key>` 对不存在的键返回 **5**（"you try to unset an option
which does not exist"）：安装中断、用户手工改过、重复卸载都会命中。卸载器把 0 与 5 都视为
「配置本来就干净」，只有其他退出码才告警，避免每次卸载都亮一条红字。

## 8. Windows 平台

### 8.1 入口

| 平台 | 安装 | 卸载 |
|---|---|---|
| macOS / Linux | `bash install.sh` | `bash uninstall.sh` |
| Windows | `install.cmd`（或 `install.ps1`） | `uninstall.cmd`（或 `uninstall.ps1`） |

`install.cmd` 只是 `install.ps1` 的 CMD 包装。`.ps1` 与 `.cmd` 在仓库中保持 **CRLF**，
且 `.ps1` 必须带 **UTF-8 BOM**：PowerShell 5.1 会把无 BOM 的文件按 ANSI 解析，中文提示
会变乱码。两者与 `install.sh` 职责对称：只解析一个满足下限的系统 Python 并转发给 Python
编排器。系统依赖预检、参数解析与用法说明都在 Python 侧（`installer_preflight.py` 与各
编排器的 argparse），四个入口脚本共用 `scripts/entrypoint.sh` / `scripts/entrypoint.ps1`
这一份解释器解析逻辑，不再各自实现一遍版本比较与指引文案。

在 Git Bash 里跑 `install.sh` 会被显式拒绝，并提示改用 `install.cmd` —— Windows 不通过
MSYS/MINGW 走 POSIX 入口，避免出现"能跑但 PATH / 前缀全是错的"半可用状态。

### 8.2 平台适配层

`scripts/installer_platform.py` 是唯一的平台差异来源；安装、卸载、状态检测都读它，
不再各自硬编码 POSIX 假设。

| 项目 | macOS / Linux | Windows |
|---|---|---|
| 状态与日志目录 | `$XDG_STATE_HOME/CodeKit` | `%LOCALAPPDATA%\CodeKit` |
| **工具链根**（运行环境） | `<home>/.local/share/CodeKit` | 同左，**三系统同一路径**（见 §4.2） |
| npm 前缀（历史组件：钉钉/飞书 CLI） | `~/.npm-global`（避免 sudo 写系统目录） | 沿用 `npm prefix -g`，默认 `%APPDATA%\npm` |
| npm 私有前缀（工具链托管的 gitnexus） | `<工具链根>/npm` | 同左，与宿主平台无关 |
| npm 可执行目录 | `<prefix>/bin` | `<prefix>` 本身（**没有 `bin/` 子目录**） |
| CodeKit 命令目录 | `~/.local/bin` | 复用 npm 全局目录（已在 PATH 上，无需再改环境变量） |
| venv 布局 | `bin/pip`、`bin/<cmd>` | `Scripts/pip.exe`、`Scripts/<cmd>.exe` |
| 命令入口 | 符号链接 | `.cmd` 转发脚本（建符号链接需要管理员或开发者模式） |
| 临时目录 | `$TMPDIR` / `/tmp` | `tempfile.gettempdir()` |
| shell 配置 | 写 `.bashrc` / `.zshrc` / fish | **不写**，改为校验 PATH 并打印指引 |
| 特权判定 | `os.geteuid() == 0` | `IsUserAnAdmin` |
| PyPI 通道 | 根接管的组件（graphify）的候选通道由 `entry_kind` 过滤成只剩 `venv`，因此**没有**退到根外的第二条路；运行时不可用时该组件如实失败。三平台同规则（见 §4.2） | 同左 |

> Windows 上 CodeKit 不再自造 `~/.local/bin`：Node 安装包已经把 `%APPDATA%\npm` 写进
> 用户 PATH，另起目录反而要多维护一份 PATH 配置。PowerShell profile 的路径是本地化的、
> 还可能被 OneDrive 重定向，猜路径不可靠，所以入口脚本只校验、不写入。

### 8.3 四个必须显式处理的 Windows 行为

| 行为 | 症状 | 处理 |
|---|---|---|
| `CreateProcess` 不展开 PATHEXT | `subprocess.run(["npm", …])` 抛 `WinError 2`（npm 其实是 `npm.CMD`） | 调用前把 `argv[0]` 过一遍 `shutil.which()`；给全路径可以直接启动，**不需要 `shell=True`** |
| stdout 默认 ANSI 代码页 | 输出 `▶`、`╔` 等字符时 `UnicodeEncodeError` | 入口脚本传 `-X utf8`；`Logger` 初始化时再 `sys.stdout.reconfigure(encoding="utf-8")` 兜底 |
| 子进程文本模式默认 ANSI 解码 | CLI 的 UTF-8 输出解码失败，读线程异常，**捕获到的 stdout 静默为空**（于是"判断是否已安装"之类的分支会走错） | 所有 `subprocess` 调用统一 `encoding="utf-8", errors="replace"` |
| 没有 `/dev/tty` | 用 `Path("/dev/tty").exists()` 判断可交互性时恒为假，交互选择器被静默跳过 | 改用 `sys.stdin.isatty() and sys.stdout.isatty()`；选择器仍不可用时**显式打印默认选择**，不再静默采用默认值 |
| 安装器只改注册表 PATH | winget / 官方 `.ps1` 装完后命令在注册表里，但**已运行的进程看不到**：安装器把成功通道判为失败，再降级到下一通道装出第二份拷贝 | `platform_layer.refresh_process_path()` 在 `Installer.__init__` 与每次 winget/远程脚本成功后，把用户级 + 机器级 PATH 合并进当前进程 |
| Git for Windows 把 `bash` 放进 PATH | `script` 通道在 Windows 上被误判为可用，退化成用 Git Bash 执行 Unix 安装脚本，把文件装进 MSYS 目录树；失败提示还会给出 `curl … \| bash` | `script` 通道严格限定非 Windows（与 `powershell` 严格限定 Windows 对称）；`manual_note` 在 Windows 上不再输出 bash 一行式 |
| `winget uninstall` 不会自己提权 | 机器级 MSI（gh）在非管理员会话里卸不掉：msiexec 被 MSI 拒绝（事件 11730，Error 1730），winget 只回一个不透明的 `0x8A150030`；同一台机器上 glab（用户态 Inno）却能直接卸掉 | `platform_layer.run_elevated()` 只对该条命令经 UAC 提权重跑（静默试探→提权重试的顺序保证用户态包不弹窗）；弹不出 UAC 或用户点「否」时给「管理员终端重跑」的可操作提示（见 §7） |

### 8.4 Windows 文件语义

| 操作 | POSIX | Windows | 处理 |
|---|---|---|---|
| `os.replace(目录 → 已存在目录)` | 可替换空目录 | **失败**（PermissionError） | 目标先删除再替换；`replace_stages` 与 Qoder 缓存的回滚路径都要先清半成品 |
| `os.replace(文件 → 已存在文件)` | 可替换 | 可替换（MOVEFILE_REPLACE_EXISTING） | 无需处理 |
| `shutil.rmtree` 含只读文件 | 成功 | **失败**（PermissionError） | 适配层 `rmtree` 先清只读位再重试 |
| 长嵌套路径 | 无限制 | 默认 260 字符上限 | 状态目录选 `%LOCALAPPDATA%\CodeKit` 这类短路径；长用户名/深检出场景需实测 |

### 8.5 交互选择器

`scripts/component-selector.py` 把渲染与键盘状态机同终端能力分开：`render()` 和状态机
（`interactive_select`）平台无关、两端共用，终端差异收敛到一个后端。

| 能力 | POSIX 后端 `PosixTerminal` | Windows 后端 `WindowsConsole` |
|---|---|---|
| 打开终端 | `os.open("/dev/tty")` | `CONOUT$`（等价物：始终指向真实控制台） |
| raw 模式 | `termios` + `tty.setraw` | 不需要；`msvcrt.getwch()` 本身按字符读 |
| 取键 | 读字节 + 解析 ANSI 序列 | `getwch()`；方向键是前导字节 `\x00`/`\xe0` + 扫描码 |
| 写画面 | 写 `/dev/tty` | `WriteConsoleW`（宽字符通道） |
| ANSI 转义 | 终端原生支持 | 需 `SetConsoleMode(ENABLE_VIRTUAL_TERMINAL_PROCESSING)` |
| 可注册信号 | `SIGTERM`、`SIGHUP` | `SIGTERM`、`SIGBREAK` |

两个后端归一化成同一组逻辑事件（`up`/`down`/`toggle`/`all`/`none`/`path`/`confirm`/`quit`），
所以同一个键盘状态机在两端行为一致，`--render` 快照测试也完全共用。

两处实现细节值得记住：

- **画面必须走 `WriteConsoleW`，不能 `open("CONOUT$", "w", encoding="utf-8")` 直接写**：
  那样是往控制台句柄送 UTF-8 字节，而控制台按自身代码页（中文系统 GBK）解释，中文会乱码。
- **输出目标是控制台而不是 stdout/stderr**：调用方用管道捕获选择器的 stdout 取机器可读
  结果，画面若走 stdout 就会被吞掉；`CONOUT$` 与 POSIX 的 `/dev/tty` 一样绕过重定向。

拿不到可用终端时（例如没有控制台的会话），`open_backend()` 返回 `None`，选择器返回退出码 2，
调用方据此**显式降级**为默认选择并打印清单 —— 静默采用默认值比报错更糟，用户会以为自己在
交互，实际已经被决定。


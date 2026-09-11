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

### 1.1 Preflight 门槛

| 依赖 | 门槛 | 说明 |
|---|---|---|
| Node.js | **>= 24.0.0** | 全局门槛取 24 LTS 主版本；gitnexus 另做组件级校验（见 §4） |
| npm | 存在即可 | 随 Node 提供，是绝大多数组件的首选通道 |
| python3 | **>= 3.10** | 安装器自身运行为 Python；graphify 要求 Python >= 3.10 |
| git | **存在即可** | 不再校验版本；仅用于读取 marketplace 提交号与写入全局 git 配置 |
| curl / wget | **任一存在** | 用于脚本与发布包下载 |

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
npm  →  brew  →  brew_cask  →  apt  →  pipx  →  venv  →  script  →  release
```

| 通道 | target | 可用性判定 | 安装 / 升级语义 |
|---|---|---|---|
| `npm` | 官方 npm 包名（白名单） | `npm` 存在 | `npm install -g <pkg>@latest`（含升级） |
| `brew` | formula | `brew` 存在 | 已装则 `brew upgrade`，否则 `brew install` |
| `brew_cask` | cask | `brew` 存在 | `brew install --cask` / `brew upgrade --cask` |
| `apt` | 发行版包名 | Linux 且有包管理器 | `apt-get install -y`（需 sudo，见 §3）；包不存在则跳到下一通道 |
| `pipx` | PyPI 发行名 | `pipx` 存在（缺失则先按 apt→brew 装 pipx） | `pipx install` / `pipx upgrade` |
| `venv` | PyPI 发行名 | `python3 -m venv` 可用 | 独立 venv + 用户级软链接，始终可重装 |
| `script` | 官方安装脚本 URL | `curl` 或 `wget` 存在 | 下载后交给 shell 执行 |
| `release` | `Component.release` | `curl` 或 `wget` 存在 | 解析最新版 → 下载资产 → 校验（如可）→ 解包 → 安装到 `~/.local/bin` |

### 2.1 npm 白名单（防同名李鬼）

`npm` 通道的 target 必须在 `OFFICIAL_NPM_PACKAGES` 白名单内，否则注册表校验直接失败。
原因是以下常见名字在 npm 上并非官方项目，裸名安装会静默装错包：

| 名字 | npm 上实际是什么 | 本项目组件 |
|---|---|---|
| `gh` | node-gh/gh（停更） | 官方 GitHub CLI（改用 brew/apt/release） |
| `rtk` | cliffano 的 release 工具 | Rust Token Killer（改用 brew/script） |
| `glab` | npm 不存在 | 官方 GitLab CLI（改用 brew/apt/release） |
| `@git-ai/cli` | code-ba 的提交信息生成器 | 已移除，不再安装 |

## 3. sudo 行为（apt 通道）

- 已是 root：直接调用包管理器，不加 `sudo`。
- 非 root：使用 `sudo`。先探测 `sudo -n true`：
  - 免密可用 → 直接执行；
  - 需要密码且当前是交互 TTY → 允许 `sudo` 提示一次；
  - 需要密码且非交互（CI）→ **跳过 apt 通道**并提示原因，降级到下一通道，不阻塞等待。
- 所有特权命令记录到安装日志；`apt-get update` 每次运行只执行一次。

## 4. 组件策略表

`requires` 为该组件自身的硬前置，不满足时**只跳过该组件**并给出指引，不影响其他组件。

| 组件 | 分组 | 命令 | npm | brew | apt | pipx/venv | script | release | requires |
|---|---|---|---|---|---|---|---|---|---|
| rtk | ai | `rtk` | — | `rtk` | — | — | 官方 install.sh | — | — |
| gitnexus | intel | `gitnexus` | `gitnexus` | — | — | — | — | — | **node >= 24.11.0** |
| graphify | intel | `graphify` | — | — | — | `graphifyy` | — | — | python >= 3.10 |
| gh | third | `gh` | — | `gh` | `gh` | — | — | GitHub `cli/cli` | — |
| glab | third | `glab` | — | `glab` | `glab` | — | — | GitLab `34675721` | — |
| dingtalk_cli | third | `dws` | `dingtalk-workspace-cli` | — | — | — | — | — | unzip |
| feishu_cli | third | `lark-cli` | `@larksuite/cli` | — | — | — | — | — | — |

插件组件（claude/opencode/codex/qoder/qodercn）不声明通道，其 CLI 前置按下表安装。

### 4.1 组件级版本校验（gitnexus）

全局 Node 门槛是 `>= 24.0.0`，但 gitnexus 的 npm `engines` 是 `^22.18.0 || >=24.11.0`。
由于 npm 默认**只把 engines 当警告**，安装器必须在调用 npm 之前显式校验：Node 24.0–24.10 直接跳过
gitnexus 并提示升级到 24.11+，其余组件照常安装。

## 5. 智能体 CLI 通道

| 平台 | 组件 | npm | brew / cask | script |
|---|---|---|---|---|
| Claude Code | `claude_plugin` | `@anthropic-ai/claude-code` | cask `claude-code` | — |
| Codex | `codex_plugin` | `@openai/codex` | cask `codex` | — |
| OpenCode | `opencode_plugin` | `opencode-ai` | formula `opencode` | — |
| Qoder | `qoder_plugin` | — | — | `https://qoder.com/install` |
| Qoder CN | `qodercn_plugin` | — | — | `https://static.qoder.com.cn/qoder-cli-cn/install.sh` |

智能体 CLI 只在该平台插件被勾选时安装；插件安装以 CLI 为前置。

## 6. 探测与幂等

1. 已安装且版本满足 → 结束，不重装。
2. 否则按 `CHANNEL_ORDER` 取第一个「组件已声明 + 当前可用」的通道执行。
3. 通道执行后校验命令是否可用；不可用则记录真实错误并继续下一通道。
4. 单个组件失败不阻断其余组件；结果在收尾汇总表中呈现。

## 7. 卸载

卸载按同一张表反向执行：npm 用 `npm uninstall -g`，brew/apt 用对应包管理器移除，
pipx/venv 用 `pipx uninstall` 并删除私有 venv 与软链接。卸载是幂等的：组件不存在时不调用破坏性操作。

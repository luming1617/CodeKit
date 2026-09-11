# CodeKit — Plugin

> 把 AI 从"辅助写代码"升级为"独立跑完整研发流程"

**CodeKit** 是一套基于 Claude / OpenCode / Codex / Qoder 的 AI 开发套件。它把团队研发规范、工程方法论、工具链封装成 AI 能直接执行的技能（skills），让开发者只需要一条命令就能触发完整工作流：读 PRD、写方案、生成发布方案、并行编码和多维 CR，全程 AI 主导，人类定目标、审结果。

> 同时支持 **Claude**、**OpenCode**、**Codex**、**Qoder** 和 **Qoder CN**（均通过官方插件机制分发），可以同时安装，7 个技能在这些平台上用法完全相同。
>
> Qoder 与 Qoder CN 的 CLI 和桌面 IDE 共用同一个用户级配置根（`~/.qoder`、`~/.qoder-cn`），因此**插件只需安装一次，CLI 与 IDE 同时生效**；调用方式与 Claude 一致，带 `/codekit:` 前缀。

---

## 一键安装

在**本仓库根目录**执行：

```bash
bash install.sh
```

安装内容：编程智能体 CLI（Claude / Codex / OpenCode / Qoder / Qoder CN，勾选对应插件时自动补装；插件安装以 CLI 为前置，仅有 Desktop 不能替代） · CodeKit 插件（Claude / OpenCode / Codex / Qoder / Qoder CN，分别包含对应平台的 subagent） · GitNexus · Graphify · gh · glab · 钉钉 CLI · 飞书 CLI · rtk(节省token)

> 安装前脚本只**检测**系统依赖，不会替你安装：git（存在即可）、Node.js ≥ 24、Python3 ≥ 3.10、npm、curl/wget 任一。任一不满足会醒目列出全部缺失项、当前版本与手动安装方式并中止。可选组件按统一优先级 `npm → brew → apt → pipx/venv → 官方脚本/发布包` 探测安装，详见 [INSTALL-STRATEGY.md](INSTALL-STRATEGY.md)。

> 安装前会检查 Claude、Codex、OpenCode、Qoder、Qoder CN 的 **CLI 本体**；五者 CLI 均不存在时只给出醒目提示并继续流程（不会阻断，退出码 0），也不会自动安装任何 CLI——只有在用户勾选了某个平台插件时，安装器才会先自动安装该平台 CLI 再安装插件。Desktop 应用、启动器与 Qoder 共享配置根不计入 CLI 前置，仅用于 Qoder 平台可用性判断与后续提示。

> 脚本以当前仓库（`install.sh` 所在目录）作为 CodeKit 插件市场源，相对定位、不依赖远端地址；仓库根目录包含插件元数据、`.opencode/` OpenCode 包和 `skills/`，安装时会重建兼容的插件源布局。组件注册表和交互选择器位于 `scripts/`，因此安装与卸载都需要完整仓库，不能只通过 `curl ... | bash` 执行单个 `install.sh`。

> 在本地仓库执行时，选择 Claude、OpenCode、Codex 或 Qoder 插件会同步安装对应平台的全局 agent；Codex 目录中的 TOML 文件由同名 Markdown 自动生成，Qoder 写入与桌面 IDE 共享的 `<配置根>/agents/`。已有非 CodeKit 同名 agent 会先备份，不会静默覆盖。

> 每个技能目录还包含与 `SKILL.md` 同级的 `openai.yaml`（Codex 界面显示名称、简介与默认提示词，统一中文）。Codex 实际读取的是 `skills/<skill>/agents/openai.yaml`，安装时由该源文件自动派生，无需手工维护两份。

---

## 能做什么

### 需求开发：一个需求进，完成实现与审查

```bash
/codekit:product-doc <product-material-source>           # 整理并审查 PRD，保存到 feature-directory
/codekit:technical-doc <feature-directory>              # 基于 PRD 写后端技术方案
/codekit:release-doc <feature-directory>                # 基于 PRD + TDD 写发布方案
/codekit:code-impl <feature-directory>                  # 创建分支并多 Agent 并行 TDD 开发
/codekit:code-review [<feature-directory>]              # 5 个维度并行 Code Review（目录可省略）
```

> 实测案例：现货下单校验改造，一个需求 链接进，11 个子任务、14 个 commit、18 个文件修改，全部由 AI 完成；合并请求由团队现有流程提交。

### 问题排查：多源取证，证据链根因分析

```bash
/codekit:bugfix [<feature-directory>] <error-description>  # 系统化根因分析（目录可省略）
```

> 实测案例：C++ 压测崩溃排查，AI 跨越业务日志、dmesg、源码、ps、nm/addr2line 多个证据源，定位到 use-after-free 并发 Bug，全程 4 条指令。

---

## 命令列表

| 命令 | 作用 |
|------|------|
| `/codekit:initialize` | 强制调用三路 Subagent，初始化规范、索引、知识图谱与 AI 上下文 |
| `/codekit:product-doc` | 整理并审查产品需求文档 |
| `/codekit:technical-doc` | 仅基于 PRD 生成后端技术方案 |
| `/codekit:release-doc` | 基于 PRD + TDD 生成后端发布方案 |
| `/codekit:code-impl` | 多 Agent 并行 TDD 开发 |
| `/codekit:code-review` | 5 个维度并行 Code Review |
| `/codekit:bugfix` | 基于 PRD/TDD/RPD 系统化排查并修复问题 |

---

## 文档

- **[使用指南 GUIDE.md](GUIDE.md)** — 安装配置、认证步骤、完整流程、故障排查
- **方案文档（harness）** — CodeKit 体系设计文档（独立仓库分发，本仓库内无此资源，请向团队获取）

---

## 升级 / 卸载

在本仓库根目录执行：

```bash
# 安装或升级
bash install.sh

# 卸载
bash uninstall.sh
```

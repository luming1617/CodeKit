#!/usr/bin/env python3
"""CodeKit installer orchestrator.

The shell entry point is intentionally small. This module owns the installer
state machine, component dispatch, package-manager commands, and installation
result handling. Third-party CLIs remain external commands; Python provides
the lifecycle and error boundary around them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Union

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import installer_config as config  # noqa: E402

# 导入期门禁：残缺检出必须以单行受控错误结束，而不是让下面的 import 抛出
# ModuleNotFoundError。这也是卸载器复用的同一份 helper 清单。
try:
    config.require_helper_files(SCRIPT_DIR)
except ValueError as exc:
    print(f"  ✗  {exc}", file=sys.stderr)
    raise SystemExit(1)

import installer_agents as agents  # noqa: E402
import installer_platform as platform_layer  # noqa: E402
import installer_qoder as qoder  # noqa: E402
import installer_sources as source_builder  # noqa: E402
import installer_state as detector  # noqa: E402

CODEX_MARKETPLACE = "co-daming"
CODEX_PLUGIN = "codekit"
CLAUDE_MARKETPLACE = "co-daming"
CLAUDE_PLUGIN = "codekit"


class InstallerError(RuntimeError):
    """An expected installer failure with a user-facing message."""


@dataclass
class RunResult:
    returncode: int
    stdout: str = ""


class Logger:
    def __init__(self, state_dir: Path) -> None:
        """初始化安装日志器，并确保状态目录已经存在。"""
        platform_layer.configure_stdout_encoding()
        self.state_dir = state_dir
        self.log_path = state_dir / "install.log"
        self.use_color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        state_dir.mkdir(parents=True, exist_ok=True)

    def _color(self, value: str, code: str) -> str:
        """在交互终端中为关键状态增加颜色，重定向输出保持纯文本。"""
        return f"\033[{code}m{value}\033[0m" if self.use_color else value

    @staticmethod
    def command_text(command: Sequence[str]) -> str:
        """以可复制且安全的形式展示外部命令。"""
        return shlex.join([str(item) for item in command])

    def _log(self, level: str, message: str) -> None:
        """将带级别的消息追加到日志文件；日志不可写时不阻断主流程。"""
        try:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"[{level}] {message}\n")
        except OSError:
            pass

    def info(self, message: str = "") -> None:
        """输出普通信息，并同步写入安装日志。"""
        print(message)
        self._log("INFO ", message)

    def section(self, message: str) -> None:
        """输出一个醒目的阶段标题，避免安装步骤挤在连续命令输出中。"""
        text = f"\n━━━ {message} ━━━"
        print(self._color(text, "1;36"))
        self._log("STEP ", message)

    def command_start(self, command: Sequence[str]) -> None:
        """只显示命令开始信息；命令的逐行输出写入文件而不刷屏。"""
        text = f"  · 执行：{self.command_text(command)}"
        print(self._color(text, "2"))
        self._log("CMD  ", text)

    def command_finished(self, command: Sequence[str], returncode: int, output: str = "") -> None:
        """显示外部命令的单行结果，并在失败时给出最后一条错误线索。"""
        command_text = self.command_text(command)
        if returncode == 0:
            self.success(f"完成：{command_text}")
            return

        detail = next(
            (
                line.strip()
                for line in reversed(output.splitlines())
                if line.strip()
                and not line.lstrip().startswith(("npm warn", "npm notice"))
            ),
            "",
        )
        if len(detail) > 180:
            detail = f"{detail[:177]}..."
        suffix = f"：{detail}" if detail else ""
        self.error(f"失败（退出码 {returncode}）：{command_text}{suffix}")

    def warn(self, message: str) -> None:
        """输出警告信息，并同步写入安装日志。"""
        print(self._color(f"  ⚠  {message}", "0;33"))
        self._log("WARN ", message)

    def error(self, message: str) -> None:
        """输出错误信息，并同步写入安装日志。"""
        print(self._color(f"  ✗  {message}", "0;31"))
        self._log("ERROR", message)

    def success(self, message: str) -> None:
        """输出成功信息，并同步写入安装日志。"""
        print(self._color(f"  ✓  {message}", "0;32"))
        self._log("OK   ", message)

    def command_output(self, line: str) -> None:
        """仅记录外部命令的原始输出，避免安装过程被依赖工具刷屏。"""
        self._log("CMD  ", line.rstrip("\n"))


class Installer:
    # 外部命令超时后返回的退出码，与 GNU timeout(1) 保持一致。
    TIMEOUT_RETURNCODE = 124

    def __init__(self, auto_yes: bool) -> None:
        """创建一次安装会话，并初始化路径、状态和结果记录。"""
        self.auto_yes = auto_yes
        self.home = Path.home()
        # 平台差异（npm 前缀、venv 布局、状态目录、临时目录、交互能力）全部由
        # 适配层给出，安装/卸载/状态检测三处不再各自硬编码 POSIX 假设。
        self.platform = platform_layer.platform_for()
        self.os_name = self.platform.os_name
        # Windows 上 winget 与官方 .ps1 安装器只把 PATH 写进注册表，不会更新已经运行的
        # 进程；先同步一次，状态检测才能看到用户此前装好的工具。
        platform_layer.refresh_process_path()
        probed_prefix = platform_layer.probe_npm_prefix() if self.platform.is_windows else ""
        self.npm_prefix = self.platform.npm_prefix(self.home, probed_prefix)
        self.npm_bin_dir = self.platform.npm_bin_dir(self.npm_prefix)
        self.bin_dir = self.platform.bin_dir(self.home, self.npm_bin_dir)
        self.state_dir = self.platform.state_dir(self.home)
        self.version_file = self.state_dir / "installed_versions"
        self.logger = Logger(self.state_dir)
        self.changed_paths: list[str] = []
        self.states: dict[str, detector.ComponentState] = {}
        self.selected: list[str] = []
        self.install_records: list[tuple[str, str, str]] = []
        self.marketplace_local = self._detect_marketplace()
        self.market_dir: Optional[Path] = None
        self.opencode_source_dir: Optional[Path] = None
        self.qoder_source_dir: Optional[Path] = None
        self.context = detector.Context(
            home=self.home,
            marketplace_local=self.marketplace_local,
            version_file=self.version_file,
        )

    @property
    def components(self):
        """返回注册表中的组件定义，供检测、选择和分发共用。"""
        return detector.registry.COMPONENTS

    @property
    def plugin_version(self) -> str:
        """读取并校验仓库根目录中的插件版本唯一配置源。"""
        try:
            return config.validate_plugin_version(REPO_DIR / "plugin-version.json")
        except (OSError, ValueError) as exc:
            raise InstallerError(f"Could not read plugin version: {exc}") from exc

    def _detect_marketplace(self) -> Optional[Path]:
        """检查当前仓库是否具备生成兼容插件源所需的完整文件。"""
        required = (
            # Codex 只认 <root>/.agents/plugins/marketplace.json；`.codex-plugin/`
            # 提供的是插件自身元数据，两者都必须存在。
            REPO_DIR / ".agents" / "plugins" / "marketplace.json",
            REPO_DIR / ".claude-plugin" / "marketplace.json",
            REPO_DIR / ".codex-plugin" / "marketplace.json",
            REPO_DIR / ".claude-plugin" / "plugin.json",
            REPO_DIR / ".codex-plugin" / "plugin.json",
            REPO_DIR / ".opencode" / "plugins" / "codekit.js",
            REPO_DIR / ".opencode" / "package.json",
            REPO_DIR / ".qoder-plugin" / "plugin.json",
            REPO_DIR / "plugin-version.json",
            REPO_DIR / "skills",
        )
        return REPO_DIR if all(path.exists() for path in required) else None

    def track(self, path: Union[Path, str]) -> None:
        """记录本次运行可能修改的路径，并去重以便安装后汇总展示。"""
        value = str(path)
        if value and value not in self.changed_paths:
            self.changed_paths.append(value)

    def prepend_path(self, path: Path) -> None:
        """把用户态工具目录放到当前进程 PATH 首位，供后续命令立即可用。"""
        value = str(path)
        current = os.environ.get("PATH", "")
        if not current.split(os.pathsep)[:1] == [value]:
            os.environ["PATH"] = f"{value}{os.pathsep}{current}"

    def ensure_user_bin(self) -> None:
        """创建用户级 bin 目录，并让 npm 全局 bin 与本地 bin 立即生效。"""
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.npm_bin_dir.mkdir(parents=True, exist_ok=True)
        self.prepend_path(self.bin_dir)
        self.prepend_path(self.npm_bin_dir)

    def set_npm(self) -> None:
        """统一下发 npm 镜像源，并在需要时切换到用户态前缀。

        macOS/Linux 把前缀指到用户目录（`~/.npm-global`），避免 sudo 写系统目录；
        Windows 沿用 npm 自己的前缀，它已经被 Node 安装包写进 PATH，覆写反而要
        额外维护一份 PATH 配置。
        """
        if not self.platform.is_windows:
            os.environ["npm_config_prefix"] = str(self.npm_prefix)
        os.environ["npm_config_registry"] = config.NPM_REGISTRY
        self.prepend_path(self.npm_bin_dir)

    @staticmethod
    def _resolve_command(command: Sequence[str]) -> list[str]:
        """把 argv[0] 解析成可执行文件的全路径。

        Windows 的 CreateProcess 不按 PATHEXT 展开裸命令名，`["npm", ...]` 会直接抛
        WinError 2（npm 实际是 npm.CMD）；换成全路径后可以直接启动，既不必动用
        `shell=True`（避免引号与注入风险），也不用关心具体是 .exe 还是 .cmd。
        """
        resolved = [str(item) for item in command]
        if resolved and not any(separator in resolved[0] for separator in (os.sep, "/", "\\")):
            found = platform_layer.resolve_exe(resolved[0])
            if found:
                resolved[0] = found
        return resolved

    def run_command(
        self,
        args: Sequence[str],
        *,
        check: bool = False,
        cwd: Optional[Path] = None,
        timeout: Optional[float] = None,
        quiet: bool = False,
    ) -> RunResult:
        """执行外部命令并显示摘要；完整输出写入安装日志。

        timeout 到期后终止子进程并返回 124，避免挂起的 CLI 让安装/卸载无限阻塞。
        quiet 用于「先试一次、失败就换策略」的探针式调用：终端不打印开始/结束行，
        免得留下一条其实不算失败的红字；完整输出照样写进安装日志。
        """
        command = self._resolve_command(args)
        if not quiet:
            self.logger.command_start(command)
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd) if cwd else None,
                env=os.environ.copy(),
                stdin=None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                **platform_layer.text_kwargs(),
            )
        except OSError as exc:
            self.logger.command_finished(command, 127, str(exc))
            if check:
                raise InstallerError(f"could not run {' '.join(command)}: {exc}") from exc
            return RunResult(127)
        timed_out = False
        try:
            output_text, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            output_text, _ = process.communicate()
        output_text = output_text or ""
        for line in output_text.splitlines(True):
            self.logger.command_output(line)
        returncode = self.TIMEOUT_RETURNCODE if timed_out else process.returncode
        if timed_out:
            self.logger.warn(
                f"命令超时（{timeout:g}s），已终止：{self.logger.command_text(command)}"
            )
        if not quiet:
            self.logger.command_finished(command, returncode, output_text)
        if check and returncode != 0:
            raise InstallerError(f"command failed ({returncode}): {' '.join(command)}")
        return RunResult(returncode, output_text)

    def run_capture(self, args: Sequence[str], *, timeout: float = 15) -> RunResult:
        """静默执行探测命令并返回结果，失败或超时统一返回失败结果。"""
        try:
            result = subprocess.run(
                self._resolve_command(args),
                env=os.environ.copy(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
                **platform_layer.text_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return RunResult(1)
        return RunResult(result.returncode, result.stdout.strip())

    def run_with_input(self, args: Sequence[str], content: str, env: Optional[dict[str, str]] = None) -> RunResult:
        """向外部命令写入文本输入并显示摘要；完整输出写入安装日志。"""
        command = self._resolve_command(args)
        self.logger.command_start(command)
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        try:
            result = subprocess.run(
                command,
                input=content,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=merged_env,
                check=False,
                **platform_layer.text_kwargs(),
            )
        except OSError:
            self.logger.command_finished(command, 127)
            return RunResult(127)
        for line in result.stdout.splitlines(True):
            self.logger.command_output(line)
        self.logger.command_finished(command, result.returncode, result.stdout)
        return RunResult(result.returncode, result.stdout)

    def command_exists(self, command: str) -> bool:
        """判断命令是否能通过当前进程 PATH 找到。"""
        return shutil.which(command) is not None

    def command_version(self, command: str, args: Sequence[str]) -> str:
        """调用统一的状态检测逻辑读取命令版本；命令不存在时返回空字符串。"""
        return detector.command_version(command, list(args))

    def refresh_path(self) -> None:
        """把注册表 PATH 同步进本进程，让刚装好的命令立即可见（Windows）。"""
        added = platform_layer.refresh_process_path()
        if added:
            self.logger.info(f"PATH 已刷新，新增 {len(added)} 个目录：{added[0]}")

    def set_brew_mirror(self) -> None:
        """把 Homebrew 切到中科大镜像并关闭自动更新。

        镜像写入进程环境：brew 及其所有子进程都会继承，因此这一步对安装与卸载
        路径、以及 `brew list` 这类探测命令同样生效。
        """
        os.environ.update(config.HOMEBREW_MIRROR_ENV)

    def brew(self, *args: str, capture: bool = False) -> RunResult:
        """调用 Homebrew，并统一注入中科大镜像与关闭自动更新的环境。

        capture=True 用于 `brew list` 这类只读本地状态的探测，不产生终端输出。
        """
        self.set_brew_mirror()
        command = ["brew", *args]
        return self.run_capture(command) if capture else self.run_command(command)

    def brew_manages(self, name: str, *, cask: bool = False) -> bool:
        """判断 name 是否由 Homebrew 自己管理；本地查询，不触发安装动作。"""
        args = ["list", "--cask", name] if cask else ["list", name]
        return self.brew(*args, capture=True).returncode == 0

    def brew_install_or_upgrade(self, formula: str, probe: Optional[str] = None) -> bool:
        """经 Homebrew 安装或升级一个 formula，并返回该通道是否已接管该组件。

        只有「命令缺失」或「命令由 brew 自己管理」时 brew 才是正确的通道：命令已存在
        但不由 brew 管理（例如官方脚本装的 rtk）时直接让位，否则只会装出第二份拷贝，
        而原来的 script/release 通道才是能真正完成升级的那个。
        """
        probe = probe or formula
        if not self.command_exists(probe):
            result = self.brew("install", formula)
            return result.returncode == 0 and self.command_exists(probe)
        if not self.brew_manages(formula):
            self.logger.info(f"{formula} 不在 Homebrew 管理下，跳过 brew 通道交给原通道升级。")
            return False
        result = self.brew("upgrade", formula)
        if result.returncode != 0:
            # brew 管着这个命令、只是这次升级没成功：命令仍可用，不降级到其他通道，
            # 否则会在 script/release 里再装一份把 brew 的拷贝遮住。
            self.logger.warn(f"{formula} 升级未成功，保持现有 Homebrew 安装。")
        return True

    def linux_package_manager(self) -> Optional[str]:
        """返回 Linux 包管理器；入口传入的环境变量优先。"""
        if self.os_name != "Linux":
            return None
        configured = os.environ.get("CODEKIT_PACKAGE_MANAGER", "")
        if configured in {"apt-get", "dnf", "yum", "pacman", "zypper", "apk"}:
            return configured
        for candidate in ("apt-get", "dnf", "yum", "pacman", "zypper", "apk"):
            if shutil.which(candidate):
                return candidate
        return None

    def linux_package_command(self, action: str, package: str) -> Optional[list[str]]:
        """构造 Linux 包管理命令，不把系统包操作误用于 macOS。"""
        manager = self.linux_package_manager()
        if not manager:
            return None
        if manager == "apt-get":
            return ["apt-get", action, "-y", package]
        if manager in {"dnf", "yum"}:
            return [manager, action, "-y", package]
        if manager == "pacman":
            return ["pacman", "-R" if action == "remove" else "-S", "--noconfirm", package]
        if manager == "zypper":
            return ["zypper", "--non-interactive", "remove" if action == "remove" else "install", package]
        return ["apk", "del" if action == "remove" else "add", package]

    def run_linux_package(self, action: str, package: str) -> RunResult:
        """以 root 或 sudo 执行 Linux 包操作，并复用统一日志输出。"""
        command = self.linux_package_command(action, package)
        if not command:
            return RunResult(127)
        if not platform_layer.is_admin():
            if not self.command_exists("sudo"):
                self.logger.error("Linux package operation requires root privileges or sudo.")
                return RunResult(127)
            command.insert(0, "sudo")
        return self.run_command(command)

    def linux_package_installed(self, package: str) -> bool:
        """判断目标 Linux 包是否由当前系统包管理器管理。"""
        manager = self.linux_package_manager()
        if not manager:
            return False
        probes = {
            "apt-get": ["dpkg-query", "-W", "-f=${Status}", package],
            "dnf": ["rpm", "-q", package],
            "yum": ["rpm", "-q", package],
            "pacman": ["pacman", "-Q", package],
            "zypper": ["rpm", "-q", package],
            "apk": ["apk", "info", "-e", package],
        }
        result = self.run_capture(probes[manager])
        if manager == "apt-get":
            return result.returncode == 0 and "install ok installed" in result.stdout.lower()
        return result.returncode == 0

    def fetch_text(self, url: str) -> str:
        """获取远程脚本文本；GitHub 地址优先走 gh-proxy，失败再回退直连。"""
        for candidate in config.download_urls(url):
            try:
                request = urllib.request.Request(candidate, headers={"User-Agent": "CodeKit-installer"})
                with urllib.request.urlopen(request, timeout=config.REMOTE_FETCH_TIMEOUT) as response:
                    return response.read().decode("utf-8")
            except (OSError, UnicodeError):
                continue
        return ""

    def agent_notice_box(self, lines: Sequence[str]) -> str:
        """按终端显示宽度渲染提示边框，避免中英文混排时边框错位。"""
        width = max(detector.terminal_width(line) for line in lines) + 4
        border = "═" * (width - 2)
        rows = "\n".join(
            "║" + detector.terminal_pad(f"  {line}", width - 2) + "║"
            for line in lines
        )
        return f"╔{border}╗\n{rows}\n╚{border}╝"

    def preflight_agents(self) -> bool:
        """只检查五个平台的 CLI，缺失时提示后续由勾选的插件自动安装 CLI。"""
        available = [
            (entry.key, detector.agent_cli_presence(entry.key, self.home))
            for entry in agents.AGENT_CLIS
        ]
        available = [(key, source) for key, source in available if source]
        if available:
            detail = ", ".join(f"{key}: {source}" for key, source in available)
            self.logger.success(f"\n \n Coding agent CLI(s) detected: {detail}")
            return True
        self.logger.warn("未检测到任何编程智能体 CLI。")
        self.logger.info(
            "\n" + self.agent_notice_box(
                (
                    "未检测到编程智能体 CLI：Claude Code / Codex / OpenCode / Qoder / Qoder CN",
                    "在选择器中勾选某个平台的插件后，安装器会先自动安装该平台 CLI，再安装插件。",
                    "未勾选任何平台插件时，安装器不会自动安装 CLI。",
                )
            )
        )
        return True

    def prepare_sources(self) -> None:
        """从本地仓库构建 Claude/Codex/OpenCode/Qoder 使用的兼容插件源。"""
        if not self.marketplace_local:
            return
        try:
            (
                self.market_dir,
                self.opencode_source_dir,
                self.qoder_source_dir,
            ) = source_builder.prepare(self.marketplace_local, self.state_dir)
        except (OSError, ValueError) as exc:
            raise InstallerError(f"Could not prepare compatibility plugin sources: {exc}") from exc

    def load_states(self) -> None:
        """检测所有注册组件的安装状态，并缓存结果供选择器和执行阶段使用。"""
        self.logger.info("Checking all components...")
        try:
            self.states = {item.component_id: item for item in detector.all_states(self.context)}
        except (OSError, RuntimeError, ValueError) as exc:
            raise InstallerError(f"Component state detection failed: {exc}") from exc

    def agent_cli_status(self, platform_key: str) -> tuple[str, str]:
        """返回平台 CLI 的发现来源与版本；同一次运行内复用探测结果。"""
        cache = getattr(self, "agent_cli_cache", None)
        if cache is None:
            cache = self.agent_cli_cache = {}
        if platform_key not in cache:
            source = detector.agent_cli_presence(platform_key, self.home)
            version = agents.cli_version(platform_key, self.home) if source else ""
            cache[platform_key] = (source, version)
        return cache[platform_key]

    def refresh_agent_cli_status(self, platform_key: str) -> tuple[str, str]:
        """安装 CLI 后丢弃缓存，让后续状态展示读取最新探测结果。"""
        cache = getattr(self, "agent_cli_cache", None)
        if cache is not None:
            cache.pop(platform_key, None)
        return self.agent_cli_status(platform_key)

    def component_tag(self, component_id: str) -> str:
        """生成安装组件选择器中的状态提示，插件额外区分 CLI 本体的状态与版本。"""
        item = self.states[component_id]
        entry = agents.component_agent(component_id)
        if entry is not None:
            return self.plugin_component_tag(item, entry)
        installed_version = item.installed_version or "unknown"
        if component_id == "gh":
            # gh --version 会附带日期、平台和构建信息，选择器只保留三段版本号。
            installed_version = config.extract_version(installed_version) or installed_version
        if item.status == "not_installed":
            return " (组件未安装，勾选后安装)"
        if item.status == "installed":
            return f" (组件有更新，勾选后升级 {installed_version} → {item.latest_version})"
        return f" (组件已最新 {installed_version}，勾选后重装)"

    def plugin_component_tag(self, item, entry) -> str:
        """插件条目同时显示插件状态/版本与对应平台 CLI 的状态/版本。"""
        source, cli_version = self.agent_cli_status(entry.key)
        if source:
            cli_text = f"CLI 已安装 {cli_version or 'unknown'}"
        else:
            cli_text = f"CLI 未安装（勾选后自动安装 {entry.display_name} CLI）"
        plugin_version = item.installed_version or "unknown"
        if item.status == "not_installed":
            return f" (插件未安装 · {cli_text}，勾选后安装插件)"
        if item.status == "installed":
            if item.latest_version and item.latest_version != plugin_version:
                return f" (插件有更新 {plugin_version} → {item.latest_version} · {cli_text}，勾选后升级)"
            # 版本未变但插件源内容已更新（本地 marketplace 换了提交）：只播报
            # 插件版本，绝不把提交号当作版本号展示。
            return f" (插件有更新 {plugin_version} · {cli_text}，勾选后升级)"
        return f" (插件已最新 {plugin_version} · {cli_text}，勾选后重装)"

    def shell_config(self) -> Optional[tuple[Path, str]]:
        """返回需要写入 PATH 的 shell 配置；Windows 上没有这样的文件。"""
        return self.platform.shell_config(self.home)

    def path_configured(self) -> bool:
        """检查当前 shell 配置是否已经包含 CodeKit 用户工具目录。

        Windows 没有可写的 shell 配置，改为检查 npm 全局目录是否已在 PATH 上
        （Node 安装包通常会写入），避免把「配置缺失」误报成需要处理。
        """
        configured = self.shell_config()
        if configured is None:
            return self.tool_dir_on_path()
        path, line = configured
        try:
            return line in path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False

    def tool_dir_on_path(self) -> bool:
        """判断本平台需要的用户工具目录是否已经出现在 PATH 中。"""
        entries = {os.path.normcase(os.path.normpath(value)) for value in os.environ.get("PATH", "").split(os.pathsep) if value}
        for directory in (self.bin_dir, self.npm_bin_dir):
            if os.path.normcase(os.path.normpath(str(directory))) not in entries:
                return False
        return True

    def cli_available_for(self, component_id: str) -> bool:
        """判断组件是否没有 CLI 前置要求，或该前置已经满足。"""
        entry = agents.component_agent(component_id)
        if entry is None:
            return True
        return bool(self.agent_cli_status(entry.key)[0])

    def announce_default_selection(self, default: Sequence[str]) -> None:
        """显式播报默认选择，替代不可用的交互选择器。

        静默采用默认值比报错更糟：用户会以为自己在交互，实际已经被决定。这里把
        与选择器等价的清单（分组、状态标签、勾选标记）直接打印出来。
        """
        self.logger.warn(
            "交互式选择器暂不可用（需要 termios 与 /dev/tty），本次使用默认选择。"
        )
        previous_group = ""
        lines = ["\n  本次安装选择（● = 勾选，○ = 跳过）："]
        for index, item in enumerate(self.components, 1):
            title = detector.registry.GROUPS[item.group]
            if item.group != previous_group:
                if previous_group:
                    lines.append("")
                lines.append(f"  {title}")
                previous_group = item.group
            mark = "●" if item.component_id in default else "○"
            lines.append(
                f"   {mark} {index:>2}. {item.description}{self.component_tag(item.component_id)}"
            )
        self.logger.info("\n".join(lines))

    def select_components(self) -> list[str]:
        """按安装状态生成默认选择，并在交互模式下调用选择器回填用户选择。"""
        default = [
            item.component_id
            for item in self.components
            if self.states[item.component_id].status != "up_to_date"
            # 缺 CLI 的插件不默认勾选：自动安装 CLI 必须来自用户的显式勾选。
            and self.cli_available_for(item.component_id)
        ]
        path_selected = not self.path_configured()
        # 非交互执行必须可用于 CI：采用默认选择，并跳过终端选择器。
        if self.auto_yes or not self.platform.can_interact():
            self.setup_path_selected = path_selected
            self.selected = default
            if not self.auto_yes:
                self.announce_default_selection(default)
            return default

        # 选择器只消费临时 TSV；finally 保证异常、取消和正常返回都会清理它。
        manifest = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="codekit-selector-", suffix=".tsv", delete=False)
        manifest_path = Path(manifest.name)
        try:
            for index, component in enumerate(self.components, 1):
                fields = (
                    str(index), component.description, component.group,
                    detector.registry.GROUPS[component.group],
                    self.component_tag(component.component_id),
                    "1" if component.component_id in default else "0",
                )
                manifest.write("\t".join(value.replace("\t", " ") for value in fields) + "\n")
            manifest.close()
            configured = self.shell_config()
            result = subprocess.run(
                [sys.executable, str(SCRIPT_DIR / "component-selector.py"), "--mode", "install", "--manifest", str(manifest_path), "--path-selected", "1" if path_selected else "0", "--shell-config", str(configured[0]) if configured else ""],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                **platform_layer.text_kwargs(),
            )
        finally:
            manifest_path.unlink(missing_ok=True)
        if result.returncode == 2:
            # 选择器拿不到可用终端（例如没有控制台的 Windows 会话）：显式降级，
            # 不把缺少终端当成失败。
            self.setup_path_selected = path_selected
            self.selected = default
            self.announce_default_selection(default)
            return default
        if result.returncode == 10:
            raise SystemExit(0)
        if result.returncode == 130:
            raise KeyboardInterrupt
        if result.returncode != 0:
            raise InstallerError(f"Interactive selector failed: {result.stderr.strip()}")
        payload = result.stdout.strip()
        if not payload.startswith("selected=") or ";path=" not in payload:
            raise InstallerError("Interactive selector returned an invalid result.")
        selected_text, path_text = payload.removeprefix("selected=").split(";path=", 1)
        indices = [int(value) for value in selected_text.split(",") if value]
        self.setup_path_selected = path_text == "1"
        self.selected = [self.components[index - 1].component_id for index in indices]
        return self.selected

    def marketplace_sha(self) -> str:
        """读取本地 marketplace 仓库 HEAD，用于判断插件源是否需要刷新。"""
        return detector.marketplace_sha(self.context)

    def record_marketplace_sha(self) -> None:
        """把当前 marketplace 提交号保存到版本状态文件，供下次检测比较。"""
        sha = self.marketplace_sha()
        if sha:
            config.set_key_value(self.version_file, "marketplace_sha", sha)
            self.logger.success(f"Marketplace version recorded: {sha[:7]}")

    def component(self, component_id: str):
        """按 ID 读取策略表中的组件定义；未知组件返回 None。"""
        return detector.registry.component_by_id(component_id)

    def requirements_met(self, component) -> tuple[bool, str]:
        """校验组件的硬前置（如 node>=24.11.0），返回 (是否满足, 原因)。"""
        for requirement in component.requires:
            name, separator, minimum = requirement.partition(">=")
            if name == "node":
                current = self.command_version("node", ["-v"])
                if not separator or not current or not config.version_greater_or_equal(current, minimum):
                    return False, f"需要 Node.js >= {minimum}，当前 {current or '未安装'}"
                continue
            if name == "python":
                current = f"{sys.version_info.major}.{sys.version_info.minor}"
                if not separator or not config.version_greater_or_equal(current, minimum):
                    return False, f"需要 Python >= {minimum}，当前 {current}"
                continue
            if not self.command_exists(name):
                return False, f"需要命令 {name}"
        return True, ""

    def channel_available(self, kind: str) -> bool:
        """判断通道在当前平台是否可用；只做探测，不触发安装。"""
        if kind == "npm":
            return self.command_exists("npm")
        if kind in {"brew", "brew_cask"}:
            return self.command_exists("brew")
        if kind == "apt":
            # apt 通道的 target 是 Debian/Ubuntu 包名，只在该包管理器下可用。
            return self.linux_package_manager() == "apt-get"
        if kind == "winget":
            # winget 只在 Windows 上存在；其他平台的同名命令即使有也不应使用。
            return self.platform.is_windows and self.command_exists("winget")
        if kind == "pipx":
            return (
                self.command_exists("pipx")
                or self.command_exists("brew")
                or self.linux_package_manager() is not None
            )
        if kind == "venv":
            return self.run_capture(
                [sys.executable, "-c", "import venv, ensurepip"]
            ).returncode == 0
        if kind == "script":
            # 官方 .sh 安装脚本只在类 Unix 环境里跑。Git for Windows 也会把 bash 放进
            # PATH，所以不能只判断"有没有 shell"：否则 Windows 上会退化成用 Git Bash
            # 执行 Unix 安装脚本，把文件装进 MSYS 目录树而不是 Windows 的 npm 目录。
            return not self.platform.is_windows and bool(
                platform_layer.remote_script_command("bash")
            )
        if kind == "powershell":
            # 严格限定 Windows：pwsh 在 macOS/Linux 上也可能存在，但那些 .ps1
            # 是 Windows 专用的安装脚本，不能跨平台执行。
            return self.platform.is_windows and bool(
                platform_layer.remote_script_command("powershell")
            )
        if kind == "release":
            return self.command_exists("curl") or self.command_exists("wget")
        return False

    def manual_note(self, component) -> str:
        """生成组件的手动安装指引，供所有通道都失败时提示。"""
        channels = component.channels
        # Windows 上没有可用的 Unix shell，`curl … | bash` 这条提示会把人引到不存在的
        # 解释器上；按 Windows 真正可用的通道给指引。
        if self.platform.is_windows:
            if "winget" in channels:
                return f"winget install --id {channels['winget']} --exact"
            if "npm" in channels:
                registry = " ".join(config.NPM_REGISTRY_ARGS)
                return f"npm install -g {channels['npm']} {registry}"
            if "powershell" in channels:
                return (
                    "powershell -NoProfile -ExecutionPolicy Bypass -Command "
                    f"\"irm {channels['powershell']} | iex\""
                )
            if component.release is not None:
                return (
                    f"从 {component.release.repo} 的 Releases 下载对应的 Windows 资产"
                    f"（{component.release.binary}）手工安装并加入 PATH"
                )
            return "请参考官方文档"
        if "script" in channels:
            return f"curl -fsSL {config.github_proxy_url(channels['script'])} | bash"
        if "powershell" in channels:
            return (
                "powershell -NoProfile -ExecutionPolicy Bypass -Command "
                f"\"irm {channels['powershell']} | iex\""
            )
        if "npm" in channels:
            registry = " ".join(config.NPM_REGISTRY_ARGS)
            return f"npm install -g {channels['npm']} {registry}"
        if "winget" in channels:
            return f"winget install --id {channels['winget']} --exact"
        if "brew" in channels:
            return f"brew install {channels['brew']}（建议先按 INSTALL.md 配置中科大镜像）"
        if "apt" in channels:
            return f"sudo apt-get install -y {channels['apt']}"
        return "请参考官方文档"

    def install_via_channels(self, component_id: str) -> bool:
        """按全局通道优先级，选用第一个可用通道安装组件。"""
        component = self.component(component_id)
        if component is None:
            return False
        met, reason = self.requirements_met(component)
        if not met:
            self.logger.error(f"{component.description} 已跳过：{reason}")
            return False
        for kind in detector.registry.CHANNEL_ORDER:
            if kind == "release":
                if component.release is None:
                    continue
                label = component.release.repo
            else:
                label = component.channels.get(kind, "")
                if not label:
                    continue
            if not self.channel_available(kind):
                continue
            self.logger.info(f"使用 {kind} 通道安装 {component.description}（{label}）")
            if self.install_channel(component, kind):
                return True
            self.logger.warn(f"{kind} 通道未成功，尝试下一通道。")
        self.logger.error(
            f"{component.description} 安装失败；请手动安装：{self.manual_note(component)}"
        )
        return False

    def install_channel(self, component, kind: str) -> bool:
        """执行单个通道的安装动作，并以组件命令校验最终可用性。"""
        target = component.channels.get(kind, "")
        if kind == "npm":
            return self.install_npm_package(target, component.command)
        if kind == "brew":
            return self.brew_install_or_upgrade(target, component.command)
        if kind == "brew_cask":
            return self.install_brew_cask(target, component.command)
        if kind == "apt":
            return self.install_apt_package(target, component.command)
        if kind == "winget":
            return self.install_winget_package(target, component.command)
        if kind == "pipx":
            return self.install_pipx_package(target, component.command)
        if kind == "venv":
            return self.install_venv_package(target, component.command)
        if kind == "script":
            return self.run_remote_script(target) and self.command_exists(component.command)
        if kind == "powershell":
            return self.run_remote_script(target, shell="powershell") and self.command_exists(component.command)
        if kind == "release":
            return self.install_release(component)
        return False

    def install_npm_package(self, package: str, command: str) -> bool:
        """使用用户态 npm 前缀安装或升级一个全局 CLI，镜像源由 set_npm 注入的环境变量提供。"""
        self.set_npm()
        result = self.run_command(["npm", "install", "-g", f"{package}@latest"])
        return result.returncode == 0 and self.command_exists(command)

    def install_brew_cask(self, cask: str, command: str) -> bool:
        """经 Homebrew cask 安装或升级；cask 不在 brew 管理下时让位给下一通道。"""
        if not self.command_exists(command):
            result = self.brew("install", "--cask", cask)
            return result.returncode == 0 and self.command_exists(command)
        if not self.brew_manages(cask, cask=True):
            self.logger.info(f"{cask} 不在 Homebrew cask 管理下，跳过该通道交给原通道升级。")
            return False
        result = self.brew("upgrade", "--cask", cask)
        if result.returncode != 0:
            self.logger.warn(f"{cask} 升级未成功，保持现有 Homebrew cask 安装。")
        return True

    def install_apt_package(self, package: str, command: str) -> bool:
        """经 sudo 安装 Debian/Ubuntu 包；包不在源中时返回 False 以便降级。"""
        if self.run_capture(["apt-cache", "show", package]).returncode != 0:
            self.logger.warn(f"apt 源中不存在 {package}，跳过 apt 通道。")
            return False
        if self.run_linux_package("install", package).returncode != 0:
            return False
        return self.command_exists(command)

    def winget_manages(self, package: str) -> bool:
        """判断该包是否由 winget 自己管理；本地查询，不触发安装。"""
        result = self.run_capture(
            [
                "winget", "list", "--id", package, "--exact",
                "--accept-source-agreements", "--disable-interactivity",
            ],
            timeout=180,
        )
        return result.returncode == 0 and package.lower() in result.stdout.lower()

    def install_winget_package(self, package: str, command: str) -> bool:
        """经 winget 安装或升级一个组件。

        非交互场景必须显式接受包协议与源协议，否则 winget 会挂起等待输入。语义与 brew
        通道对齐：命令缺失才 install；命令已存在时先确认由 winget 管理再 upgrade，且
        upgrade 返回"已是最新"（实测退出码 43）不算失败 —— 否则会降级到下一通道装出
        第二份拷贝。
        """
        common = (
            "--id", package, "--exact", "--silent",
            "--accept-package-agreements", "--accept-source-agreements",
            "--disable-interactivity",
        )
        if not self.command_exists(command):
            result = self.run_command(["winget", "install", *common], timeout=1200)
            # winget 只改注册表：不刷新会把"装好了"误判成"通道失败"。
            self.refresh_path()
            return result.returncode == 0 and self.command_exists(command)
        if not self.winget_manages(package):
            self.logger.info(f"{package} 不在 winget 管理下，跳过 winget 通道交给原通道升级。")
            return False
        result = self.run_command(["winget", "upgrade", *common], timeout=1200)
        self.refresh_path()
        if result.returncode != 0:
            self.logger.warn(
                f"{package} 升级未返回成功（退出码 {result.returncode}），保持现有 winget 安装。"
            )
        return True

    def ensure_pipx(self) -> bool:
        """确保 pipx 可用；缺失时按发行版包管理器 → Homebrew 补装。"""
        if self.command_exists("pipx"):
            return True
        if self.linux_package_manager() and self.run_linux_package(
            "install", "pipx"
        ).returncode == 0 and self.command_exists("pipx"):
            return True
        if self.command_exists("brew") and self.brew_install_or_upgrade("pipx"):
            return True
        return self.command_exists("pipx")

    def install_pipx_package(self, package: str, command: str) -> bool:
        """通过 pipx 安装或升级一个 PyPI 发行包。"""
        if not self.ensure_pipx():
            return False
        action = "upgrade" if self.command_exists(command) else "install"
        result = self.run_command(["pipx", action, package])
        return result.returncode == 0 and self.command_exists(command)

    def install_venv_package(self, package: str, command: str) -> bool:
        """把 PyPI 包装进独立 venv，并把它的命令暴露到用户 bin 目录。"""
        venv = self.platform.venv_dir(self.home, command)
        if venv.exists():
            platform_layer.rmtree(venv, ignore_errors=True)
        pip = self.platform.venv_pip(venv)
        if self.run_command([sys.executable, "-m", "venv", str(venv)]).returncode != 0:
            return False
        if not pip.exists() or self.run_command([str(pip), "install", "-U", package]).returncode != 0:
            return False
        executable = self.platform.venv_exe(venv, command)
        if not executable.exists():
            return False
        # Windows 上不能靠符号链接暴露 venv 命令，适配层会改写成 .cmd 转发脚本。
        self.platform.expose_command(self.bin_dir, command, executable)
        return self.command_exists(command)

    def release_arch(self) -> str:
        """把平台机器名归一化为发布资产的架构标记。"""
        return {
            "x86_64": "x86_64",
            "amd64": "x86_64",
            "aarch64": "arm64",
            "arm64": "arm64",
        }.get(platform.machine().lower(), "")

    def release_asset_name(self, spec, version: str) -> str:
        """按 release 模板展开当前平台的资产文件名。"""
        arch = self.release_arch()
        return spec.asset.format(
            version=version,
            os=spec.os_tokens.get(self.os_name, ""),
            arch=spec.arch_tokens.get(arch, ""),
            ext=spec.ext_tokens.get(self.os_name, ""),
            target=spec.target_tokens.get(f"{self.os_name}-{arch}", ""),
        )

    def fetch_json_url(self, url: str):
        """获取发布接口 JSON；GitHub 地址优先走 gh-proxy，失败再回退直连。"""
        for candidate in config.download_urls(url):
            try:
                request = urllib.request.Request(
                    candidate,
                    headers={"Accept": "application/json", "User-Agent": "CodeKit-installer"},
                )
                with urllib.request.urlopen(request, timeout=config.REMOTE_FETCH_TIMEOUT) as response:
                    return json.load(response)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return None

    def resolve_release(self, spec) -> Optional[tuple[str, str, str]]:
        """解析最新发布版本，返回 (版本, 下载地址, sha256)；失败返回 None。"""
        kind, _, ident = spec.repo.partition(":")
        if kind == "github":
            document = self.fetch_json_url(
                f"https://api.github.com/repos/{ident}/releases/latest"
            )
            if not document:
                return None
            version = str(document.get("tag_name", "")).lstrip("v")
            asset_name = self.release_asset_name(spec, version)
            for asset in document.get("assets", []):
                if asset.get("name") == asset_name:
                    digest = str(asset.get("digest", "")).split(":", 1)[-1]
                    return version, str(asset.get("browser_download_url", "")), digest
            return None
        if kind == "gitlab":
            document = self.fetch_json_url(
                f"https://gitlab.com/api/v4/projects/{ident}/releases/permalink/latest"
            )
            if not document:
                return None
            version = str(document.get("tag_name", "")).lstrip("v")
            asset_name = self.release_asset_name(spec, version)
            links = (document.get("assets") or {}).get("links", [])
            for link in links:
                if link.get("name") == asset_name:
                    return version, str(link.get("direct_asset_url", "")), ""
            return None
        return None

    def download_release(self, url: str, digest: str) -> Optional[Path]:
        """下载发布资产并校验摘要；GitHub 地址优先走 gh-proxy，失败再回退直连。"""
        options = [
            "--fail", "--location", "--connect-timeout", "10", "--speed-limit", "51200",
            "--speed-time", "20", "--retry", "2", "--retry-max-time", "180",
            "--continue-at", "-", "--progress-bar",
        ]
        fd, name = tempfile.mkstemp(prefix="codekit-release-", dir=self.platform.temp_dir())
        os.close(fd)
        temporary = Path(name)
        result = RunResult(1)
        for candidate in config.download_urls(url):
            # --continue-at - 会续传上一个地址留下的半成品，切换地址前必须清空。
            temporary.write_bytes(b"")
            result = self.run_command(["curl", *options, "-o", str(temporary), candidate])
            if result.returncode == 0:
                break
        if result.returncode != 0:
            temporary.unlink(missing_ok=True)
            return None
        if digest and hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
            temporary.unlink(missing_ok=True)
            self.logger.error("发布包摘要校验失败，已删除下载文件。")
            return None
        return temporary

    def extract_release_binary(self, archive: Path, binary: str) -> Optional[Path]:
        """从 tar.gz/zip 发布包中定位目标可执行文件。

        Windows 构建的可执行文件带 ``.exe``，因此按平台文件名与裸名各找一次，
        这样同一个发布说明在 macOS/Linux 与 Windows 上都能命中。
        """
        directory = Path(tempfile.mkdtemp(prefix="codekit-extract-"))
        try:
            if zipfile.is_zipfile(archive):
                with zipfile.ZipFile(archive) as package:
                    package.extractall(directory)
            else:
                with tarfile.open(archive) as package:
                    try:
                        package.extractall(directory, filter="data")
                    except TypeError:
                        # Python < 3.12 has no extraction filter.
                        package.extractall(directory)
        except (OSError, tarfile.TarError, zipfile.BadZipFile, ValueError):
            return None
        for name in dict.fromkeys((self.platform.binary_filename(binary), binary)):
            for candidate in directory.rglob(name):
                if candidate.is_file():
                    return candidate
        return None

    def install_release(self, component) -> bool:
        """按 release 通道解析、下载并安装组件的最新发布包。"""
        resolved = self.resolve_release(component.release)
        if not resolved or not resolved[1]:
            self.logger.warn(f"未能解析 {component.description} 的发布包地址。")
            return False
        version, url, digest = resolved
        self.logger.info(f"下载 {component.description} {version}：{url}")
        archive = self.download_release(url, digest)
        if archive is None:
            return False
        try:
            binary = self.extract_release_binary(archive, component.release.binary)
        finally:
            archive.unlink(missing_ok=True)
        if binary is None:
            return False
        target = self.platform.release_target(self.bin_dir, component.release.binary)
        target.unlink(missing_ok=True)
        shutil.move(str(binary), str(target))
        if not self.platform.is_windows:
            target.chmod(0o755)
        self.track(target)
        return self.command_exists(component.command)

    def run_remote_script(self, url: str, shell: str = "bash", env: Optional[dict[str, str]] = None) -> bool:
        """下载远程安装脚本后交给平台原生的 shell 执行。

        shell="powershell" 用于 Windows 专用的 .ps1 安装器（Qoder 等官方只提供
        .ps1），脚本同样经 stdin 喂入，避免落盘与执行策略的额外处理。
        """
        script = self.fetch_text(url)
        if not script:
            return False
        command = platform_layer.remote_script_command(shell)
        if not command:
            return False
        if self.run_with_input(list(command), script, env).returncode != 0:
            return False
        # 官方 .ps1 安装器同样把 PATH 写进注册表，刷新后才能校验命令可用性。
        self.refresh_path()
        return True

    def configure_rtk(self) -> bool:
        """为已存在的 AI 编程工具注册 rtk 的全局 token 优化配置。"""
        results = []
        if self.command_exists("claude"):
            results.append(self.run_command(["rtk", "init", "-g", "--auto-patch"]))
        if self.command_exists("codex"):
            # Codex 使用 AGENTS.md/RTK.md 指令集，不支持 Claude 的
            # --auto-patch 模式；必须使用 rtk 的独立 Codex 初始化模式。
            results.append(self.run_command(["rtk", "init", "-g", "--codex"]))
        if self.command_exists("opencode"):
            results.append(self.run_command(["rtk", "init", "-g", "--opencode", "--auto-patch"]))
        return all(result.returncode == 0 for result in results)

    def install_rtk(self) -> bool:
        """按策略表安装 rtk（brew → 官方脚本），再写入各智能体配置。"""
        self.logger.info("Installing rtk (Rust Token Killer)...")
        if not self.install_via_channels("rtk"):
            return False
        return self.configure_rtk()

    def install_gitnexus(self) -> bool:
        """按策略表安装 gitnexus（npm），并执行其初始化命令。"""
        if not self.install_via_channels("gitnexus"):
            return False
        return self.run_command(["gitnexus", "setup"]).returncode == 0

    def ensure_unzip(self) -> bool:
        """确保钉钉 CLI 的安装脚本可以调用解压工具。

        Windows 上不需要：钉钉的 install.js 在 win32 分支改用 PowerShell 的
        Expand-Archive 解压 zip，Windows 也没有默认的 unzip 命令。
        """
        if self.platform.is_windows or self.command_exists("unzip"):
            return True

        self.logger.info("unzip not found; installing it before DingTalk CLI...")
        if self.os_name == "Darwin":
            installed = self.command_exists("brew") and self.brew_install_or_upgrade(
                "unzip"
            )
        elif self.os_name == "Linux":
            installed = (
                self.run_linux_package("install", "unzip").returncode == 0
                and self.command_exists("unzip")
            )
        else:
            installed = False

        if not installed:
            self.logger.error("unzip is required to install the DingTalk CLI.")
        return installed

    def install_dingtalk_cli(self) -> bool:
        """按策略表安装钉钉 CLI，并在安装前准备其所需的 unzip 命令。"""
        if not self.ensure_unzip():
            return False
        return self.install_via_channels("dingtalk_cli")

    def install_graphify(self) -> bool:
        """按策略表安装 graphify（pipx → 独立 venv），再为已装智能体注册。"""
        if not self.install_via_channels("graphify"):
            return False
        platforms = [
            platform
            for command, platform in (
                ("claude", "claude"),
                ("codex", "codex"),
                ("opencode", "opencode"),
            )
            if self.command_exists(command)
        ]
        if not platforms:
            platforms = ["claude"]
        results = [
            self.run_command(["graphify", "install", "--platform", platform])
            for platform in platforms
        ]
        results.append(self.run_command(["graphify", "hook", "install"]))
        return all(result.returncode == 0 for result in results)

    def install_feishu_cli(self) -> bool:
        """按策略表安装飞书 CLI，并向已装智能体同步 skills。

        skills 由第三方提供（`npx skills add` 需要 clone GitHub 仓库），只是 CLI 之外
        的附加项：同步失败不影响 CLI 本身可用，所以只告警、不判组件失败。技能全局库
        （`-g` 的落盘位置）已有这些 skills 时直接跳过，升级路径不再依赖 GitHub 可达。
        """
        if not self.install_via_channels("feishu_cli"):
            return False

        # `skills add -g` 会自动探测当前目录中的所有 Agent。若探测到
        # PromptScript，skills CLI 会因其不支持全局安装而让整批技能失败。
        # 只传递本安装器管理的目标，避免不兼容的 Agent 参与安装。
        agents = [
            agent
            for command, agent in (
                ("claude", "claude-code"),
                ("codex", "codex"),
                ("opencode", "opencode"),
            )
            if self.command_exists(command)
        ]
        if not agents:
            self.logger.warn("No supported coding agent found; skipped Feishu skills.")
            return True
        if self.feishu_skills_installed():
            self.logger.info("飞书 skills 已安装，跳过同步。")
            return True
        result = self.run_command(
            [
                "npx",
                "skills",
                "add",
                "larksuite/cli",
                "-y",
                "-g",
                "--agent",
                *agents,
            ]
        )
        if result.returncode != 0:
            self.logger.warn(
                "飞书 skills 未同步（飞书 CLI 本身已就绪）；可稍后手动重试："
                "npx skills add larksuite/cli -y -g"
            )
        return True

    def feishu_skills_installed(self) -> bool:
        """判断 skills CLI 的全局技能库中是否已装飞书 skills。

        `npx skills add larksuite/cli -g` 把技能写进 `~/.agents/skills`（配一个
        `~/.agents/.skill-lock.json` 记录来源），与具体智能体无关，因此这一处就是
        「已装」的权威依据。
        """
        return any((self.home / ".agents" / "skills").glob("lark-*"))

    def platform_agents_plan(self, action: str, tool: str) -> Optional[tuple[list[str], Path]]:
        """构造平台全局 subagent 的同步/清理命令与目标目录；无可执行动作时返回 None。"""
        helper = SCRIPT_DIR / "manage-global-agents.py"
        if not helper.is_file():
            return None
        args = [sys.executable, str(helper), action, "--home", str(self.home), "--tool", tool]
        if action == "install":
            source = self.marketplace_local / "skills" if self.marketplace_local else None
            if source is None or not source.is_dir():
                return None
            args.extend(["--source", str(source)])
        flags, agents_dir = agents.global_agent_plan(tool, self.home)
        args.extend(flags)
        return args, agents_dir

    def run_platform_agents(self, action: str, tool: str) -> bool:
        """同步或清理某平台的全局 subagent；安装与卸载共用同一实现与路径解析。"""
        try:
            plan = self.platform_agents_plan(action, tool)
        except ValueError as exc:
            self.logger.error(str(exc))
            return False
        if plan is None:
            return True
        args, agents_dir = plan
        result = self.run_command(args)
        if result.returncode == 0:
            self.track(agents_dir)
        return result.returncode == 0

    def install_platform_agents(self, tool: str) -> bool:
        """随对应插件同步该平台的全局 subagent。"""
        return self.run_platform_agents("install", tool)

    def ensure_agent_cli(self, platform_key: str) -> bool:
        """安装插件前确保平台 CLI 可用；缺失时自动安装 CLI 本体。"""
        entry = agents.agent_cli(platform_key)
        if entry is None:
            return False
        source, _version = self.agent_cli_status(platform_key)
        if source:
            self.logger.info(f"{entry.display_name} CLI already available: {source}")
            return True
        if not agents.supports_install(entry, self.os_name):
            self.logger.error(
                f"未检测到 {entry.display_name} CLI，且当前平台没有可用的自动安装渠道。"
            )
            return False

        self.logger.section(f"安装智能体 CLI：{entry.display_name}")
        self.logger.info(
            f"未检测到 {entry.display_name} CLI，先自动安装 CLI，随后再安装对应插件。"
        )
        # 官方脚本把 CLI 放进配置根下的 entry/bin 目录，只有先补进 PATH，
        # 后续的可用性校验和插件安装才能在同一进程里直接调用它。
        for directory in reversed(agents.cli_extra_paths(entry, self.home)):
            self.prepend_path(directory)
        installed = False
        for kind, target in agents.channel_plan(entry):
            if not self.channel_available(kind):
                continue
            if kind == "npm":
                self.set_npm()
                self.run_command(["npm", "install", "-g", f"{target}@latest"])
            elif kind == "brew":
                self.brew_install_or_upgrade(target, entry.commands[0])
            elif kind == "brew_cask":
                self.install_brew_cask(target, entry.commands[0])
            elif kind == "winget":
                self.install_winget_package(target, entry.commands[0])
            elif kind == "script":
                self.run_remote_script(target)
            elif kind == "powershell":
                self.run_remote_script(target, shell="powershell")
            if detector.agent_cli_presence(platform_key, self.home):
                installed = True
                break
        if not installed:
            note = agents.fallback_note(entry, windows=self.platform.is_windows)
            self.logger.error(
                f"{entry.display_name} CLI 安装失败；请手动执行：{note}，然后重新运行安装。"
            )
            return False
        source, version = self.refresh_agent_cli_status(platform_key)
        self.logger.success(
            f"{entry.display_name} CLI 已就绪：{version or 'unknown'} ({source})"
        )
        return True

    def announce_plugin_install(self, component_id: str) -> None:
        """播报插件的真实安装版本与落盘位置，与 Qoder 的收尾提示保持一致。"""
        version = detector.installed_plugin_version(component_id, self.context)
        path = detector.component_install_path(component_id, self.context)
        self.logger.success(
            f"{detector.component_description(component_id)} v{version} 已安装（{path}）"
        )

    def install_claude_plugin(self) -> bool:
        """注册本地 Claude marketplace，并按已安装状态执行插件安装或更新。"""
        if not self.market_dir:
            return False
        # 勾选插件但缺少 CLI 时先自动安装 CLI，再安装插件。
        if not self.ensure_agent_cli("claude"):
            return False
        # 先同步全局 subagent：失败即中止，避免“插件已写入并启用、组件却上报失败”的半安装状态。
        if not self.install_platform_agents("claude"):
            return False
        if self.run_command(["claude", "plugin", "marketplace", "add", str(self.market_dir)]).returncode != 0:
            return False
        self.track(self.home / ".claude" / "plugins" / "known_marketplaces.json")
        self.run_command(["claude", "plugin", "marketplace", "update", CLAUDE_MARKETPLACE])
        listing = self.run_capture(["claude", "plugin", "list"]).stdout
        package = f"{CLAUDE_PLUGIN}@{CLAUDE_MARKETPLACE}"
        result = self.run_command(["claude", "plugin", "update", package]) if package in listing else self.run_command(["claude", "plugin", "install", package, "--scope", "user"])
        if result.returncode != 0:
            return False
        self.track(self.home / ".claude" / "plugins" / "installed_plugins.json")
        self.record_marketplace_sha()
        self.announce_plugin_install("claude_plugin")
        return True

    def install_opencode_plugin(self) -> bool:
        """把本地 OpenCode 插件源写入全局配置；已存在时尝试强制刷新插件。"""
        if not self.opencode_source_dir:
            return False
        if not self.ensure_agent_cli("opencode"):
            return False
        if not self.install_platform_agents("opencode"):
            return False
        # 写 opencode 自己解析到的那份配置：它按 opencode.json → opencode.jsonc
        # 的顺序取第一个存在的文件，写错文件会让同一个插件出现在两份配置里。
        path = config.opencode_config_file(self.home / ".config" / "opencode")
        # 必须用 file URI 而不是直接拼路径：Windows 盘符路径含冒号与反斜杠，
        # `file://C:\...` 会被解析成主机名为盘符的 UNC 路径。
        plugin = f"codekit@{self.opencode_source_dir.as_uri()}"
        try:
            if config.opencode_has_plugin(path):
                result = self.run_command(["opencode", "plugin", plugin, "-g", "-f"]) if self.run_capture(["opencode", "plugin", "--help"]).returncode == 0 else RunResult(0)
            else:
                config.add_opencode_plugin(path, plugin)
                result = RunResult(0)
        except ValueError:
            return False
        if result.returncode != 0:
            return False
        self.track(path)
        self.announce_plugin_install("opencode_plugin")
        return True

    def install_codex_plugin(self) -> bool:
        """注册本地 Codex marketplace，清理旧技能残留后添加 CodeKit 插件。"""
        if not self.market_dir:
            return False
        if not self.ensure_agent_cli("codex"):
            return False
        if not self.install_platform_agents("codex"):
            return False
        # 旧版本可能把兼容技能放进 ~/.agents；安装前清掉旧生成物，避免重复加载。
        for path in (self.home / ".agents" / "skills").glob("keel-*"):
            if path.is_dir():
                shutil.rmtree(path)
        added = self.run_command(["codex", "plugin", "marketplace", "add", str(self.market_dir)]).returncode == 0
        if not added and CODEX_MARKETPLACE in self.run_capture(["codex", "plugin", "marketplace", "list"]).stdout:
            added = True
        if not added:
            return False
        self.track(self.home / ".codex" / "config.toml")
        result = self.run_command(["codex", "plugin", "add", f"{CODEX_PLUGIN}@{CODEX_MARKETPLACE}"])
        if result.returncode != 0:
            return False
        self.record_marketplace_sha()
        self.announce_plugin_install("codex_plugin")
        return True

    def qoder_run(self, args: Sequence[str], timeout: float = 20.0) -> tuple[int, str]:
        """执行 Qoder CLI 命令；到期即终止子进程，避免安装/卸载无限阻塞。"""
        result = self.run_command(args, timeout=timeout)
        return result.returncode, result.stdout

    def install_qoder_plugin(self, platform_key: str) -> bool:
        """安装 Qoder 插件；CLI 与 Desktop 共享配置根，一次安装两端生效。"""
        descriptor = qoder.PLATFORMS[platform_key]
        stage = self.qoder_source_dir
        if not stage or not stage.is_dir():
            return False
        # 插件以 CLI 为前置：CLI 缺失时先自动安装，再写共享注册表。
        if not self.ensure_agent_cli(platform_key):
            return False
        try:
            root = qoder.resolve_config_root(descriptor, self.home)
        except ValueError as exc:
            raise InstallerError(str(exc)) from exc
        if qoder.foreign_os_roots(descriptor) and not (root / "plugins").is_dir():
            self.logger.warn(
                "Qoder keeps a separate configuration root per OS; this install "
                f"only reaches {root}, so a Qoder running on Windows stays unaware."
            )
        # 先同步全局 subagent：失败即中止且不写注册表，避免出现
        # “插件已写入并启用、但组件上报失败”的半安装状态。
        if not self.install_platform_agents(platform_key):
            return False
        command = qoder.capable_cli(descriptor)
        try:
            if command:
                qoder.install_via_cli(command, root, stage, runner=self.qoder_run)
            else:
                # Desktop-only machines get the same registry the CLI would write.
                qoder.install_direct(root, stage, self.plugin_version)
            self.track(qoder.registry_path(root))
            self.track(qoder.settings_path(root))
            state = qoder.inspect(descriptor, self.home)
        except ValueError as exc:
            raise InstallerError(str(exc)) from exc
        if not state.installed or not state.enabled:
            raise InstallerError(
                f"{qoder.PLUGIN_ID} is not enabled in {qoder.registry_path(state.root)} "
                "after install; the shared Qoder registry was not updated"
            )
        self.logger.success(
            f"{descriptor.display_name} v{state.version} is available to both the "
            f"Qoder CLI and the Qoder desktop IDE ({detector.display_path(state.root, self.home)})"
        )
        return True

    def install_component(self, component_id: str) -> bool:
        """按组件注册 ID 分发安装处理器，并隔离单个组件的失败。"""
        handlers = {
            "rtk": self.install_rtk,
            "claude_plugin": self.install_claude_plugin,
            "opencode_plugin": self.install_opencode_plugin,
            "codex_plugin": self.install_codex_plugin,
            "qoder_plugin": lambda: self.install_qoder_plugin("qoder"),
            "qodercn_plugin": lambda: self.install_qoder_plugin("qodercn"),
            "gitnexus": self.install_gitnexus,
            "dingtalk_cli": self.install_dingtalk_cli,
            "feishu_cli": self.install_feishu_cli,
            "graphify": self.install_graphify,
            "gh": lambda: self.install_via_channels("gh"),
            "glab": lambda: self.install_via_channels("glab"),
        }
        handler = handlers.get(component_id)
        if not handler:
            return False
        try:
            return bool(handler())
        except (OSError, ValueError, InstallerError) as exc:
            self.logger.error(f"{component_id} failed: {exc}")
            return False

    def run_installs(self) -> None:
        """按用户选择逐项执行安装/升级/重装。"""
        for component_id in self.selected:
            status = self.states[component_id].status
            action = "安装" if status == "not_installed" else "升级" if status == "installed" else "重装"
            label = detector.component_description(component_id)
            self.logger.section(f"{action}：{label}")
            # 单个组件失败不会中断其他组件，最终状态由收尾阶段统一呈现。
            ok = self.install_component(component_id)
            if ok:
                self.logger.success(f"{label}已完成")
            else:
                self.logger.error(f"{label}未完成，请查看日志：{self.logger.log_path}")
            self.install_records.append((component_id, action, "1" if ok else "0"))

    def configure_git(self) -> None:
        """写入 CodeKit 需要的全局 Git 选项，并记录 Git 配置文件路径。"""
        self.run_command(["git", "config", "--global", "advice.addIgnoredFile", "false"])
        self.run_command(["git", "config", "--global", "push.autoSetupRemote", "true"])
        self.track(self.home / ".gitconfig")

    def save_versions(self) -> None:
        """收集组件版本、插件源提交号和插件版本，原子写入状态文件。"""
        values = [
            (component_id, detector.component_version(component_id, self.context) or "N/A")
            for component_id in (
                "glab",
                "gh",
                "rtk",
                "gitnexus",
                "dingtalk_cli",
                "feishu_cli",
                "graphify",
                "claude_plugin",
                "opencode_plugin",
                "codex_plugin",
                "qoder_plugin",
                "qodercn_plugin",
            )
        ]
        sha = config.read_key_value(self.version_file, "marketplace_sha")
        config.write_versions(self.version_file, values, self.plugin_version, sha)
        self.track(self.version_file)

    def setup_shell_path(self) -> None:
        """向当前 shell 配置追加一次性 PATH 配置，已存在时保持文件不变。

        Windows 没有可写的 shell 配置（PowerShell profile 路径本地化且可能被
        OneDrive 重定向），改为校验 PATH 并在缺失时打印手动指引。
        """
        configured = self.shell_config()
        if configured is None:
            if not self.tool_dir_on_path():
                self.logger.warn(self.platform.path_hint(self.home, self.npm_bin_dir))
            return
        path, line = configured
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        if line in old:
            return
        config.atomic_write(path, old.rstrip("\n") + f"\n\n# Added by CodeKit Plugin\n{line}\n")
        self.track(path)

    def print_next_steps(self) -> None:
        """用醒目的表格打印后续操作及其作用。"""
        rows = [("重启智能体应用", "让本次安装的插件、技能和配置立即生效")]
        configured = self.shell_config()
        if configured is None:
            rows.append(("新开一个终端", "让本次安装的用户级命令立即生效"))
        else:
            shell_path = configured[0]
            try:
                shell_path_text = f"~/{shell_path.relative_to(self.home)}"
            except ValueError:
                shell_path_text = str(shell_path)
            rows.append(
                (f"source {shell_path_text}", "让本次安装的用户级命令立即在当前终端生效")
            )
        if self.command_exists("gitnexus"):
            rows.append(("仓库根目录执行：gitnexus analyze --embeddings --skills --verbose", "对当前仓库建立gitnexus索引"))
        if self.command_exists("claude"):
            rows.append(("Claude 中输入：/codekit:initialize", "初始化当前仓库"))
        if self.command_exists("opencode"):
            rows.append(("OpenCode 中输入：/initialize", "初始化当前仓库"))
        if self.command_exists("codex"):
            rows.append(("Codex 中输入：$initialize", "初始化当前仓库"))
        qoder_labels = [
            descriptor.display_name.removeprefix("CodeKit Plugin (").removesuffix(")")
            for descriptor in (
                qoder.PLATFORMS[key]
                for key in ("qoder", "qodercn")
                if detector.agent_presence(key, self.home, self.os_name)
            )
        ]
        for label in qoder_labels:
            rows.append(
                (
                    f"{label} 中输入：/codekit:initialize",
                    "初始化当前仓库",
                )
            )
        # if qoder_labels:
        #     rows.append(
        #         (
        #             "重启 " + "、".join(qoder_labels) + " 的 CLI 与 IDE",
        #             "让新安装的插件和全局 subagent 被重新加载",
        #         )
        #     )

        if self.command_exists("gh"):
            rows.append(("gh auth login", "推荐浏览器登录；Git 操作选择 HTTPS，并确认使用 GitHub 凭据"))
        if self.command_exists("glab"):
            rows.append(("glab auth login", "GitLab.com 直接回车；自托管 GitLab 输入实例 URL"))
        if self.command_exists("dws"):
            rows.append(("dws auth login", "登录钉钉并完成 CLI 认证"))
        if self.command_exists("lark-cli"):
            rows.append(("lark-cli config init", "创建或选择飞书应用并保存 App ID 和 App Secret"))
            rows.append(("lark-cli auth login --recommend", "登录飞书，启用飞书 CLI 的业务操作"))

        widths = [
            max(detector.terminal_width(header), *(detector.terminal_width(row[index]) for row in rows))
            for index, header in enumerate(("安装后操作", "作用"))
        ]
        border = "  ╔" + "╤".join("═" * (width + 2) for width in widths) + "╗"
        separator = "  ╟" + "┼".join("─" * (width + 2) for width in widths) + "╢"
        bottom = "  ╚" + "╧".join("═" * (width + 2) for width in widths) + "╝"
        lines = ["\n\n\n  ⚠ 安装后操作（建议按顺序执行）", border]
        headers = ("安装后操作", "作用")
        lines.append("  ║ " + " │ ".join(detector.terminal_pad(value, widths[index]) for index, value in enumerate(headers)) + " ║")
        lines.append(separator)
        for row in rows:
            lines.append("  ║ " + " │ ".join(detector.terminal_pad(value, widths[index]) for index, value in enumerate(row)) + " ║")
        lines.extend((bottom, f"\n日志：{self.logger.log_path}"))
        self.logger.info("\n".join(lines))

    def run(self) -> int:
        """执行完整安装生命周期：前置检查、安装、配置和结果收尾。"""
        self.logger.info(f"\n \n === CodeKit Plugin Install v{self.plugin_version} ===")
        if self.os_name not in {"Darwin", "Linux", "Windows"}:
            raise InstallerError(f"Unsupported OS: {self.os_name}. Only macOS/Linux/Windows.")
        detector.registry.validate_registry()
        self.ensure_user_bin()
        if not self.marketplace_local:
            self.logger.warn("Marketplace repo not detected; plugin components may be skipped.")
        self.prepare_sources()
        if not self.preflight_agents():
            return 0
        self.load_states()
        self.select_components()
        if not self.selected:
            self.logger.info("No components selected. Nothing to do.")
            try:
                detector.render_recap("install", (), self.context)
            except (OSError, ValueError, RuntimeError) as exc:
                self.logger.warn(f"Could not render installation recap: {exc}")
            return 0
        self.run_installs()
        self.configure_git()
        self.save_versions()
        if getattr(self, "setup_path_selected", False):
            self.setup_shell_path()
        self.print_next_steps()
        try:
            detector.render_recap("install", ("\t".join(row) for row in self.install_records), self.context)
        except (OSError, ValueError, RuntimeError) as exc:
            self.logger.warn(f"Could not render installation recap: {exc}")
        return 0


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """解析安装器命令行参数，并拒绝已合并到 install 的旧 upgrade 命令。"""
    parser = argparse.ArgumentParser(description="CodeKit Plugin installer")
    parser.add_argument("action", nargs="?", default="install", choices=("install", "upgrade"))
    parser.add_argument("-y", "--yes", action="store_true", help="proceed with default selections")
    args = parser.parse_args(argv)
    if args.action == "upgrade":
        parser.error("'upgrade' was merged into 'install'; run install instead")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    """作为进程入口运行安装器，并将取消或预期异常转换为退出码。"""
    args = parse_args(argv)
    try:
        return Installer(args.yes).run()
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except (InstallerError, OSError, ValueError, RuntimeError) as exc:
        print(f"  ✗  {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

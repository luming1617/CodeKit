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
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Union

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import installer_config as config  # noqa: E402
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
    def __init__(self, auto_yes: bool) -> None:
        """创建一次安装会话，并初始化路径、状态和结果记录。"""
        self.auto_yes = auto_yes
        self.home = Path.home()
        xdg_state = Path(os.environ.get("XDG_STATE_HOME", str(self.home / ".local" / "state"))).expanduser()
        self.state_dir = xdg_state / "CodeKit"
        self.bin_dir = self.home / ".local" / "bin"
        self.npm_prefix = self.home / ".npm-global"
        self.version_file = self.state_dir / "installed_versions"
        self.logger = Logger(self.state_dir)
        self.changed_paths: list[str] = []
        self.states: dict[str, detector.ComponentState] = {}
        self.selected: list[str] = []
        self.install_records: list[tuple[str, str, str]] = []
        self.os_name = platform.system()
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
        (self.npm_prefix / "bin").mkdir(parents=True, exist_ok=True)
        self.prepend_path(self.bin_dir)
        self.prepend_path(self.npm_prefix / "bin")

    def set_npm(self) -> None:
        """将 npm 全局安装前缀切换到用户目录，避免使用 sudo 写系统目录。"""
        os.environ["npm_config_prefix"] = str(self.npm_prefix)
        self.prepend_path(self.npm_prefix / "bin")

    def run_command(self, args: Sequence[str], *, check: bool = False, cwd: Optional[Path] = None) -> RunResult:
        """执行外部命令并显示摘要；完整输出写入安装日志。"""
        command = [str(item) for item in args]
        self.logger.command_start(command)
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd) if cwd else None,
                env=os.environ.copy(),
                stdin=None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self.logger.command_finished(command, 127, str(exc))
            if check:
                raise InstallerError(f"could not run {' '.join(command)}: {exc}") from exc
            return RunResult(127)
        output: list[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            output.append(line)
            self.logger.command_output(line)
        returncode = process.wait()
        output_text = "".join(output)
        self.logger.command_finished(command, returncode, output_text)
        if check and returncode != 0:
            raise InstallerError(f"command failed ({returncode}): {' '.join(command)}")
        return RunResult(returncode, output_text)

    def run_capture(self, args: Sequence[str], *, timeout: float = 15) -> RunResult:
        """静默执行探测命令并返回结果，失败或超时统一返回失败结果。"""
        try:
            result = subprocess.run(
                [str(item) for item in args],
                env=os.environ.copy(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return RunResult(1)
        return RunResult(result.returncode, result.stdout.strip())

    def run_with_input(self, args: Sequence[str], content: str, env: Optional[dict[str, str]] = None) -> RunResult:
        """向外部命令写入文本输入并显示摘要；完整输出写入安装日志。"""
        command = [str(item) for item in args]
        self.logger.command_start(command)
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        try:
            result = subprocess.run(
                [str(item) for item in args],
                input=content,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=merged_env,
                check=False,
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

    def brew(self, *args: str) -> RunResult:
        """通过统一命令执行器调用 Homebrew。"""
        return self.run_command(["brew", *args])

    def brew_install_or_upgrade(self, formula: str, probe: Optional[str] = None) -> bool:
        """安装或升级一个 Homebrew formula，并用 probe 命令确认最终可用性。"""
        probe = probe or formula
        if self.command_exists(probe):
            self.logger.info(f"{formula} already installed; upgrading via brew...")
            self.brew("upgrade", formula)
        else:
            self.brew("install", formula)
        if not self.command_exists(probe):
            self.brew("install", formula)
        return self.command_exists(probe)

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
        if os.geteuid() != 0:
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
        """获取远程脚本文本；网络或编码错误时返回空字符串供调用方回退。"""
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "CodeKit-installer"})
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8")
        except (OSError, UnicodeError):
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
        """检查至少一个 CLI 或 Desktop agent，缺失时显眼提示并结束。"""
        available = [
            (name, detector.agent_presence(name, self.home, self.os_name))
            for name in ("claude", "codex", "opencode", "qoder", "qodercn")
        ]
        available = [(name, source) for name, source in available if source]
        if available:
            detail = ", ".join(f"{name}: {source}" for name, source in available)
            self.logger.success(f"Coding agent(s) detected: {detail}")
            return True
        self.logger.error(
            "\n" + self.agent_notice_box(
                (
                    "⚠ 未检测到 Claude、Codex、OpenCode、Qoder 或 Qoder CN",
                    "请先安装一个 CLI 或 Desktop agent，再重新运行 CodeKit。",
                    "本次插件安装流程已结束。",
                )
            )
        )
        return False

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

    def component_tag(self, component_id: str) -> str:
        """生成安装组件选择器中的状态提示。"""
        item = self.states[component_id]
        installed_version = item.installed_version or "unknown"
        if component_id == "gh":
            # gh --version 会附带日期、平台和构建信息，选择器只保留三段版本号。
            installed_version = config.extract_version(installed_version) or installed_version
        if item.status == "not_installed":
            return " (组件未安装，勾选后安装)"
        if item.status == "installed":
            return f" (组件有更新，勾选后升级 {installed_version} → {item.latest_version})"
        if component_id == "git_ai":
            return f" (组件已最新 {installed_version} · 无公开版本源，勾选后重装)"
        return f" (组件已最新 {installed_version}，勾选后重装)"

    def shell_config(self) -> tuple[Path, str]:
        """根据用户 shell 返回配置文件路径及需要写入的 PATH 配置行。"""
        shell = Path(os.environ.get("SHELL", "/bin/bash")).name
        if shell == "fish":
            return self.home / ".config" / "fish" / "config.fish", 'fish_add_path -g "${HOME}/.local/bin" "${HOME}/.npm-global/bin"'
        if shell == "zsh":
            return self.home / ".zshrc", 'export PATH="${HOME}/.local/bin:${HOME}/.npm-global/bin:${PATH}"'
        return self.home / ".bashrc", 'export PATH="${HOME}/.local/bin:${HOME}/.npm-global/bin:${PATH}"'

    def path_configured(self) -> bool:
        """检查当前 shell 配置是否已经包含 CodeKit 用户工具目录。"""
        path, line = self.shell_config()
        try:
            return line in path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False

    def select_components(self) -> list[str]:
        """按安装状态生成默认选择，并在交互模式下调用选择器回填用户选择。"""
        default = [
            item.component_id
            for item in self.components
            if self.states[item.component_id].status != "up_to_date"
        ]
        path_selected = not self.path_configured()
        # 非交互执行必须可用于 CI：采用默认选择，并跳过终端选择器。
        if self.auto_yes or not Path("/dev/tty").exists():
            self.setup_path_selected = path_selected
            self.selected = default
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
            path, _ = self.shell_config()
            result = subprocess.run(
                [sys.executable, str(SCRIPT_DIR / "component-selector.py"), "--mode", "install", "--manifest", str(manifest_path), "--path-selected", "1" if path_selected else "0", "--shell-config", str(path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            )
        finally:
            manifest_path.unlink(missing_ok=True)
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

    def git_ai_command(self) -> Optional[str]:
        """返回 git-ai 可执行文件路径；未安装时返回 None。"""
        return detector.git_ai_command(self.home)

    def git_ai_asset(self) -> Optional[str]:
        """根据当前操作系统和 CPU 架构计算 git-ai 发布包名称。"""
        os_name = {"Darwin": "macos", "Linux": "linux"}.get(self.os_name)
        arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine().lower())
        return f"git-ai-{os_name}-{arch}" if os_name and arch else None

    def fetch_git_ai_binary(self) -> Optional[Path]:
        """下载并校验匹配平台的 git-ai 二进制；校验失败时删除临时文件。"""
        asset = self.git_ai_asset()
        if not asset:
            return None
        try:
            request = urllib.request.Request("https://api.github.com/repos/git-ai-project/git-ai/releases/latest", headers={"Accept": "application/json", "User-Agent": "CodeKit-installer"})
            with urllib.request.urlopen(request, timeout=30) as response:
                document = json.load(response)
            digest = next((item.get("digest", "").split(":", 1)[-1] for item in document.get("assets", []) if item.get("name") == asset), "")
        except (OSError, ValueError, json.JSONDecodeError):
            digest = ""
        if not digest:
            return None
        fd, name = tempfile.mkstemp(prefix="git-ai-", dir=os.environ.get("TMPDIR", "/tmp"))
        os.close(fd)
        temporary = Path(name)
        url = f"https://github.com/git-ai-project/git-ai/releases/latest/download/{asset}"
        options = ["--fail", "--location", "--connect-timeout", "10", "--speed-limit", "51200", "--speed-time", "20", "--retry", "2", "--retry-max-time", "180", "--continue-at", "-", "--progress-bar"]
        result = self.run_command(["curl", *options, "-o", str(temporary), url])
        if result.returncode != 0:
            temporary.write_bytes(b"")
            self.run_command(["curl", *options, "-o", str(temporary), f"https://gh-proxy.com/{url}"])
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
            temporary.unlink(missing_ok=True)
            return None
        return temporary

    def run_remote_script(self, url: str, shell: str = "bash", env: Optional[dict[str, str]] = None) -> bool:
        """下载远程安装脚本后交给指定 shell 执行，并返回脚本是否成功。"""
        script = self.fetch_text(url)
        return bool(script) and self.run_with_input([shell], script, env).returncode == 0

    def install_git_ai(self) -> bool:
        """优先使用带 SHA-256 校验的 git-ai 二进制安装，失败后回退官方脚本。"""
        self.logger.info("Installing git-ai...")
        self.prepend_path(self.home / ".git-ai" / "bin")
        binary = self.fetch_git_ai_binary()
        installed = False
        if binary:
            installed = self.run_remote_script("https://usegitai.com/install.sh", env={"GIT_AI_LOCAL_BINARY": str(binary)})
            binary.unlink(missing_ok=True)
        if not installed:
            installed = self.run_remote_script("https://usegitai.com/install.sh")
        if not detector.git_ai_command(self.home):
            self.logger.warn(f"git-ai install failed — check log: {self.logger.log_path}")
            return False
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
        """优先通过 Homebrew 安装/升级 rtk，失败后回退到官方安装脚本。"""
        self.logger.info("Installing rtk (Rust Token Killer)...")
        if self.command_exists("brew") and self.brew_install_or_upgrade("rtk") and self.command_exists("rtk"):
            return self.configure_rtk()
        installed = self.run_remote_script("https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh", "sh") and self.command_exists("rtk")
        if installed:
            # Homebrew 和官方脚本两条安装路径都必须执行相同的初始化，
            # 否则无 brew 的 Linux 环境只安装二进制、不安装任何 agent 配置。
            installed = self.configure_rtk()
        return installed

    def install_gitnexus(self) -> bool:
        """使用用户态 npm 前缀安装最新版 gitnexus，并执行其初始化命令。"""
        if not self.install_npm_component("gitnexus"):
            return False
        return self.run_command(["gitnexus", "setup"]).returncode == 0

    def install_npm_component(self, component_id: str) -> bool:
        """使用用户态 npm 前缀安装或升级一个全局 npm CLI 组件。"""
        definition = detector.NPM_COMPONENTS.get(component_id)
        if not definition:
            return False
        self.set_npm()
        package = definition["package"]
        command = definition["command"]
        result = self.run_command(["npm", "install", "-g", f"{package}@latest"])
        return result.returncode == 0 and self.command_exists(command)

    def ensure_unzip(self) -> bool:
        """确保钉钉 CLI 的安装脚本可以调用 unzip。"""
        if self.command_exists("unzip"):
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
        """安装钉钉 CLI，并在安装前准备其所需的 unzip 命令。"""
        if not self.ensure_unzip():
            return False
        return self.install_npm_component("dingtalk_cli")

    def install_graphify(self) -> bool:
        """优先用 pipx 安装 graphifyy，失败时使用独立 venv 和用户级软链接回退。"""
        installed = False
        if self.command_exists("pipx"):
            result = self.run_command(["pipx", "install", "graphifyy"]) if not self.command_exists("graphify") else self.run_command(["pipx", "upgrade", "graphifyy"])
            installed = result.returncode == 0 and self.command_exists("graphify")
        if not installed:
            venv = self.home / ".local" / "share" / "CodeKit" / "venvs" / "graphify"
            if venv.exists():
                shutil.rmtree(venv)
            pip = venv / "bin" / "pip"
            if self.run_command([sys.executable, "-m", "venv", str(venv)]).returncode == 0 and pip.exists() and self.run_command([str(pip), "install", "-U", "graphifyy"]).returncode == 0:
                target = self.bin_dir / "graphify"
                target.unlink(missing_ok=True)
                target.symlink_to(venv / "bin" / "graphify")
                installed = self.command_exists("graphify")
        if not installed:
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
        """安装飞书 CLI，并只向支持全局安装的 CodeKit 智能体同步 skills。"""
        if not self.install_npm_component("feishu_cli"):
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
        result = self.run_command(
            ["npx", "skills", "add", "larksuite/cli", "-y", "-g", "--agent", *agents]
        )
        return result.returncode == 0

    def install_platform_agents(self, tool: str) -> bool:
        """随对应插件同步该平台的全局 subagent。"""
        helper = SCRIPT_DIR / "manage-global-agents.py"
        if not helper.is_file():
            return True
        if not self.marketplace_local:
            return True
        source = self.marketplace_local / "skills"
        if not source.is_dir():
            return True
        args = [sys.executable, str(helper), "install", "--source", str(source)]
        args.extend(["--home", str(self.home), "--tool", tool])
        try:
            # Qoder keeps its global agents inside the root shared with the IDE.
            flags, agents_dir = qoder.agent_location(tool, self.home)
        except ValueError as exc:
            self.logger.error(str(exc))
            return False
        args.extend(flags)
        result = self.run_command(args)
        if result.returncode == 0:
            path = agents_dir or {
                "claude": self.home / ".claude" / "agents",
                "opencode": self.home / ".config" / "opencode" / "agents",
                "codex": self.home / ".codex" / "agents",
            }[tool]
            self.track(path)
        return result.returncode == 0

    def install_claude_plugin(self) -> bool:
        """注册本地 Claude marketplace，并按已安装状态执行插件安装或更新。"""
        if not self.command_exists("claude") or not self.market_dir:
            return False
        if self.run_command(["claude", "plugin", "marketplace", "add", str(self.market_dir)]).returncode != 0:
            return False
        self.track(self.home / ".claude" / "plugins" / "known_marketplaces.json")
        self.run_command(["claude", "plugin", "marketplace", "update", CLAUDE_MARKETPLACE])
        listing = self.run_capture(["claude", "plugin", "list"]).stdout
        package = f"{CLAUDE_PLUGIN}@{CLAUDE_MARKETPLACE}"
        result = self.run_command(["claude", "plugin", "update", package]) if package in listing else self.run_command(["claude", "plugin", "install", package, "--scope", "user"])
        if result.returncode != 0 or not self.install_platform_agents("claude"):
            return False
        self.track(self.home / ".claude" / "plugins" / "installed_plugins.json")
        self.record_marketplace_sha()
        return True

    def install_opencode_plugin(self) -> bool:
        """把本地 OpenCode 插件源写入全局配置；已存在时尝试强制刷新插件。"""
        if not self.command_exists("opencode") or not self.opencode_source_dir:
            return False
        path = self.home / ".config" / "opencode" / "opencode.json"
        plugin = f"codekit@file://{self.opencode_source_dir}"
        try:
            if config.opencode_has_plugin(path):
                result = self.run_command(["opencode", "plugin", plugin, "-g", "-f"]) if self.run_capture(["opencode", "plugin", "--help"]).returncode == 0 else RunResult(0)
            else:
                config.add_opencode_plugin(path, plugin)
                result = RunResult(0)
        except ValueError:
            return False
        if result.returncode != 0 or not self.install_platform_agents("opencode"):
            return False
        self.track(path)
        return True

    def install_codex_plugin(self) -> bool:
        """注册本地 Codex marketplace，清理旧技能残留后添加 CodeKit 插件。"""
        if not self.command_exists("codex") or not self.market_dir:
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
        if result.returncode != 0 or not self.install_platform_agents("codex"):
            return False
        self.record_marketplace_sha()
        return True

    def qoder_run(self, args: Sequence[str], timeout: float = 20.0) -> tuple[int, str]:
        """通过安装器日志执行 Qoder CLI 命令；超时由外部命令自身控制。"""
        result = self.run_command(args)
        return result.returncode, result.stdout

    def install_qoder_plugin(self, platform_key: str) -> bool:
        """安装 Qoder 插件；CLI 与 Desktop 共享配置根，一次安装两端生效。"""
        descriptor = qoder.PLATFORMS[platform_key]
        stage = self.qoder_source_dir
        if not stage or not stage.is_dir():
            return False
        if not detector.agent_presence(platform_key, self.home, self.os_name):
            self.logger.warn(f"No {descriptor.display_name} CLI or desktop IDE detected.")
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
        command = qoder.capable_cli(descriptor)
        try:
            if command:
                qoder.install_via_cli(command, root, stage, runner=self.qoder_run)
            else:
                # Desktop-only machines get the same registry the CLI would write.
                qoder.install_direct(root, stage, self.plugin_version)
            self.track(qoder.registry_path(root))
            self.track(qoder.settings_path(root))
            if not self.install_platform_agents(platform_key):
                return False
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

    def install_brew_component(self, formula: str) -> bool:
        """按平台安装或升级 gh/glab：macOS 用 brew，Linux 用发行版包管理器。"""
        if self.os_name == "Darwin" or self.command_exists("brew"):
            return self.brew_install_or_upgrade(formula, formula)
        if self.os_name == "Linux":
            if not self.linux_package_manager():
                return False
            if self.command_exists(formula) and not self.linux_package_installed(formula):
                return True
            return self.run_linux_package("install", formula).returncode == 0 and self.command_exists(formula)
        return False

    def install_component(self, component_id: str) -> bool:
        """按组件注册 ID 分发安装处理器，并隔离单个组件的失败。"""
        handlers = {
            "git_ai": self.install_git_ai,
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
            "gh": lambda: self.install_brew_component("gh"),
            "glab": lambda: self.install_brew_component("glab"),
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
                "git_ai",
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
        """向当前 shell 配置追加一次性 PATH 配置，已存在时保持文件不变。"""
        path, line = self.shell_config()
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        if line in old:
            return
        config.atomic_write(path, old.rstrip("\n") + f"\n\n# Added by CodeKit Plugin\n{line}\n")
        self.track(path)

    def print_next_steps(self) -> None:
        """用醒目的表格打印后续操作及其作用。"""
        shell_path, _ = self.shell_config()
        try:
            shell_path_text = f"~/{shell_path.relative_to(self.home)}"
        except ValueError:
            shell_path_text = str(shell_path)
        rows = [
            ("重启智能体应用", "让本次安装的插件、技能和配置立即生效"),
            (f"source {shell_path_text}", "让本次安装的用户级命令立即在当前终端生效"),
        ]
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
                    "初始化当前仓库；CLI 与 Desktop 共享同一配置根，安装一次两端生效",
                )
            )
        if qoder_labels:
            rows.append(
                (
                    "重启 " + "、".join(qoder_labels) + " 的 CLI 与 IDE",
                    "让新安装的插件和全局 subagent 被重新加载",
                )
            )

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
        self.logger.info(f"=== CodeKit Plugin Install v{self.plugin_version} ===")
        if platform.system() not in {"Darwin", "Linux"}:
            raise InstallerError(f"Unsupported OS: {platform.system()}. Only macOS/Linux.")
        required_helpers = (
            SCRIPT_DIR / "component-registry.py",
            SCRIPT_DIR / "installer_config.py",
            SCRIPT_DIR / "installer_state.py",
            SCRIPT_DIR / "installer_sources.py",
            SCRIPT_DIR / "installer_qoder.py",
            SCRIPT_DIR / "component-selector.py",
        )
        missing = [str(path) for path in required_helpers if not path.is_file()]
        if missing:
            raise InstallerError(f"Installer helper missing: {', '.join(missing)}")
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

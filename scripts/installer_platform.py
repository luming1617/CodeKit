#!/usr/bin/env python3
"""Host-platform adapter for the CodeKit installer.

Everything that differs between macOS, Linux and Windows is described once
here, so the installer, the uninstaller and the state detector read one
description of the host instead of each hardcoding POSIX paths.

Three Windows behaviours drive most of this module, all verified on a real
host:

* ``CreateProcess`` does not apply ``PATHEXT`` to a bare command name, so
  ``subprocess.run(["npm", ...])`` raises ``WinError 2`` even though
  ``npm.CMD`` is on ``PATH``.  Passing the full path from ``shutil.which()``
  does work, and avoids the quoting hazards of ``shell=True``.
* Python decodes child output with the ANSI code page (GBK on zh-CN) unless an
  encoding is given.  The agent CLIs emit UTF-8, so the reader thread dies and
  the captured stdout is silently empty.
* ``sys.stdout`` is encoded with the ANSI code page too, which cannot represent
  the box-drawing glyphs this installer prints.

The module is deliberately stdlib-only and side-effect free on import so the
Windows branches stay unit-testable from a Linux CI host via a fake
:class:`Platform`.
"""

from __future__ import annotations

import ctypes
import os
import platform as platform_module
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Tuple


MACOS = "macos"
LINUX = "linux"
WINDOWS = "windows"

# 子进程输出统一按 UTF-8 解码；非法字节用替换字符兜底，绝不让解码失败冒到
# 调用方（那会丢掉整段输出，比多几个替换字符严重得多）。
SUBPROCESS_ENCODING = "utf-8"


def os_key(system: Optional[str] = None) -> str:
    """把 ``platform.system()`` 归一化为适配层的平台键。"""
    name = (system or platform_module.system()).lower()
    if name == "darwin":
        return MACOS
    if name == "windows":
        return WINDOWS
    return LINUX


def is_windows(system: Optional[str] = None) -> bool:
    """当前（或给定）平台是否为 Windows。"""
    return os_key(system) == WINDOWS


def platform_for(system: Optional[str] = None) -> "Platform":
    """构造指定（默认当前）宿主平台的适配对象。"""
    name = system or platform_module.system()
    return Platform(key=os_key(name), os_name=name)


def resolve_exe(name: str) -> Optional[str]:
    """返回可执行文件的完整路径。

    Windows 上这是启动 ``.cmd`` shim 的唯一可靠方式：CreateProcess 不认裸命令
    名的 PATHEXT，而给全路径（含 ``.cmd``）可以直接启动，因此无需 ``shell=True``。
    macOS/Linux 上等价于一次普通 PATH 查找，调用方可以无条件使用。
    """
    found = shutil.which(name)
    return str(found) if found else None


def text_kwargs() -> dict:
    """子进程文本模式参数，强制 UTF-8 解码。"""
    return {"text": True, "encoding": SUBPROCESS_ENCODING, "errors": "replace"}


def configure_stdout_encoding() -> None:
    """把本进程的标准输出切到 UTF-8。

    Windows 默认用 ANSI 代码页编码 stdout，``▶``、``╔`` 这类字符会直接抛
    ``UnicodeEncodeError``。终端本身（``_WindowsConsoleIO``）没问题，但输出被
    重定向到管道或文件时必定踩到。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding=SUBPROCESS_ENCODING, errors="replace")
        except (OSError, ValueError):
            pass


def is_admin() -> bool:
    """当前进程是否具备管理员/root 权限。"""
    if is_windows():
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            return False
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid and geteuid() == 0)


def rmtree(path: Path, *, ignore_errors: bool = False) -> None:
    """删除目录树，并处理 Windows 的只读位。

    ``shutil.rmtree`` 在 Windows 上遇到只读文件会抛 ``PermissionError``；
    POSIX 上删除只读文件只要求父目录可写，因此不会触发。这里先清掉只读位再
    重试一次。
    """

    def onerror(func, target, exc_info):  # type: ignore[no-untyped-def]
        error = exc_info[1]
        if isinstance(error, PermissionError):
            try:
                os.chmod(target, stat.S_IWRITE)
                func(target)
                return
            except OSError:
                pass
        if not ignore_errors:
            raise error

    shutil.rmtree(path, onerror=onerror)


def remote_script_command(shell: str = "bash") -> Tuple[str, ...]:
    """把远程安装脚本交给平台原生的 shell 执行。

    脚本一律通过 stdin 喂给 shell，避免落盘后再顾虑执行策略。
    """
    if shell == "powershell":
        executable = resolve_exe("powershell") or resolve_exe("pwsh")
        if not executable:
            return ()
        return (
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            "-",
        )
    executable = resolve_exe(shell)
    return (executable,) if executable else ()


def run_powershell_script(script: str, timeout: float = 300) -> int:
    """把 PowerShell 脚本从 stdin 交给 powershell 执行，返回退出码。"""
    command = remote_script_command("powershell")
    if not command:
        return 127
    try:
        completed = subprocess.run(
            list(command),
            input=script,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
            **text_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return 127
    return completed.returncode


def probe_npm_prefix(timeout: float = 15) -> str:
    """读取 npm 自己的全局前缀；失败时返回空字符串。

    Windows 上安装器不再强制覆写前缀，因此需要问 npm 要真实值。
    """
    executable = resolve_exe("npm")
    if not executable:
        return ""
    try:
        completed = subprocess.run(
            [executable, "prefix", "-g"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            **text_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    text = (completed.stdout or "").strip()
    return text.splitlines()[0].strip() if text else ""


@dataclass(frozen=True)
class Platform:
    """一个宿主的平台差异集合；所有路径都相对传入的 home 计算。"""

    key: str
    os_name: str

    @property
    def is_windows(self) -> bool:
        return self.key == WINDOWS

    # ---- 目录布局 ---------------------------------------------------

    def state_dir(self, home: Path) -> Path:
        """安装器自身的状态与日志目录。

        Windows 用 ``%LOCALAPPDATA%``：路径短，能避开 MAX_PATH 限制。
        """
        if self.is_windows:
            base = os.environ.get("LOCALAPPDATA") or str(Path(home) / "AppData" / "Local")
            return Path(base) / "CodeKit"
        configured = os.environ.get("XDG_STATE_HOME")
        root = Path(configured).expanduser() if configured else Path(home) / ".local" / "state"
        return root / "CodeKit"

    def default_npm_prefix(self, home: Path) -> Path:
        """未探测到 npm 前缀时的兜底目录。"""
        if self.is_windows:
            appdata = os.environ.get("APPDATA")
            return (Path(appdata) if appdata else Path(home) / "AppData" / "Roaming") / "npm"
        return Path(home) / ".npm-global"

    def npm_prefix(self, home: Path, probed: str = "") -> Path:
        """npm 全局前缀。

        macOS/Linux 固定在用户目录下，避免 sudo 写系统目录；Windows 沿用 npm
        自己的前缀（``%APPDATA%\\npm``），因为它已被 Node 安装包写进 PATH，
        另起目录反而要额外维护一份 PATH 配置。
        """
        if self.is_windows:
            cleaned = (probed or "").strip()
            return Path(cleaned) if cleaned else self.default_npm_prefix(home)
        return self.default_npm_prefix(home)

    def npm_bin_dir(self, prefix: Path) -> Path:
        """npm 全局可执行文件目录；Windows 上就是前缀根目录（无 ``bin/``）。"""
        return Path(prefix) if self.is_windows else Path(prefix) / "bin"

    def bin_dir(self, home: Path, npm_bin: Path) -> Path:
        """CodeKit 自装可执行文件的目标目录（发布包、venv 命令）。

        Windows 复用 npm 全局目录：它已在 PATH 上，放真实 ``.exe`` 进去可直接
        调用，省掉一份需要用户改环境变量的 PATH 配置。
        """
        if self.is_windows:
            return Path(npm_bin)
        return Path(home) / ".local" / "bin"

    def venv_dir(self, home: Path, command: str) -> Path:
        """私有 venv 的落地目录。"""
        return Path(home) / ".local" / "share" / "CodeKit" / "venvs" / command

    def venv_pip(self, venv: Path) -> Path:
        """venv 内的 pip 可执行文件。"""
        if self.is_windows:
            return Path(venv) / "Scripts" / "pip.exe"
        return Path(venv) / "bin" / "pip"

    def venv_exe(self, venv: Path, command: str) -> Path:
        """venv 暴露的命令可执行文件。"""
        if self.is_windows:
            return Path(venv) / "Scripts" / f"{command}.exe"
        return Path(venv) / "bin" / command

    def binary_filename(self, binary: str) -> str:
        """发布包内可执行文件的名字；Windows 构建一律带 ``.exe``。"""
        if self.is_windows and not binary.lower().endswith(".exe"):
            return f"{binary}.exe"
        return binary

    def shim_name(self, name: str) -> str:
        """npm 与 CodeKit 在该平台上暴露命令时使用的入口文件名。

        Windows 上两者生成的入口都是 ``.cmd``；发布包解出的真实可执行文件用
        :meth:`binary_filename` 取 ``.exe``。
        """
        return f"{name}.cmd" if self.is_windows else name

    def release_target(self, bin_dir: Path, binary: str) -> Path:
        """发布包可执行文件的落地路径。"""
        return Path(bin_dir) / (self.binary_filename(binary) if self.is_windows else binary)

    def expose_command(self, bin_dir: Path, name: str, target: Path) -> Path:
        """把 bin_dir 外的可执行文件暴露成 ``name``。

        macOS/Linux 用符号链接；Windows 上创建符号链接需要管理员或开发者模式，
        因此写一个 ``.cmd`` 转发脚本 —— 这也是 npm 和 virtualenv 自己的做法。
        Windows 上给 ``venv`` 里的 ``.exe`` 建符号链接同样不可行：那些启动器靠
        自身所在目录找到同 venv 的 ``python.exe``，挪走就会失效。
        """
        bin_dir = Path(bin_dir)
        bin_dir.mkdir(parents=True, exist_ok=True)
        if self.is_windows:
            shim = bin_dir / f"{name}.cmd"
            shim.unlink(missing_ok=True)
            shim.write_text(f'@echo off\r\n"{target}" %*\r\n', encoding="utf-8")
            return shim
        link = bin_dir / name
        link.unlink(missing_ok=True)
        link.symlink_to(target)
        return link

    def temp_dir(self) -> str:
        """临时文件目录；``/tmp`` 在 Windows 上并不存在。"""
        return tempfile.gettempdir()

    # ---- 能力探测 ---------------------------------------------------

    def can_interact(self) -> bool:
        """是否存在可以驱动交互选择器的终端。

        POSIX 的 ``/dev/tty`` 在 Windows 上不存在，用它做判断会让交互模式永远
        静默失效（用户以为在选择，其实用了默认值）。
        """
        return bool(sys.stdin.isatty() and sys.stdout.isatty())

    def signal_names(self) -> Tuple[str, ...]:
        """可安全注册的终止信号名；Windows 没有 SIGHUP。"""
        return ("SIGTERM",) if self.is_windows else ("SIGTERM", "SIGHUP")

    @property
    def supports_symlinks(self) -> bool:
        """创建符号链接是否需要额外的管理员/开发者模式权限。"""
        return not self.is_windows

    def preferred_pypi_channel(self, declared: Mapping[str, str]) -> str:
        """从组件声明的 PyPI 通道里挑首选通道。

        Windows 上优先 pipx：它在用户目录生成 shim，不依赖创建符号链接所需的
        管理员或开发者模式。
        """
        order = ("pipx", "venv") if self.is_windows else ("venv", "pipx")
        for kind in order:
            if kind in declared:
                return kind
        return ""

    def shell_config(self, home: Path, environ: Optional[Mapping[str, str]] = None) -> Optional[Tuple[Path, str]]:
        """需要写入 PATH 的 shell 配置文件与配置行。

        Windows 返回 ``None``：Node 安装包已经把 npm 全局目录写进用户 PATH，
        而 PowerShell profile 路径是本地化的、还可能被 OneDrive 重定向，猜路径
        不可靠，因此改为校验后打印指引。
        """
        if self.is_windows:
            return None
        env = os.environ if environ is None else environ
        shell = Path(env.get("SHELL", "/bin/bash")).name
        home = Path(home)
        if shell == "fish":
            return (
                home / ".config" / "fish" / "config.fish",
                'fish_add_path -g "${HOME}/.local/bin" "${HOME}/.npm-global/bin"',
            )
        if shell == "zsh":
            return home / ".zshrc", 'export PATH="${HOME}/.local/bin:${HOME}/.npm-global/bin:${PATH}"'
        return home / ".bashrc", 'export PATH="${HOME}/.local/bin:${HOME}/.npm-global/bin:${PATH}"'

    def shell_rc_files(self, home: Path) -> Tuple[Path, ...]:
        """卸载时需要清理的 shell 配置文件；Windows 上没有。"""
        if self.is_windows:
            return ()
        home = Path(home)
        return (
            home / ".bashrc",
            home / ".bash_profile",
            home / ".zshrc",
            home / ".zprofile",
            home / ".config" / "fish" / "config.fish",
        )

    def path_hint(self, home: Path, npm_bin: Path) -> str:
        """工具目录不在 PATH 上时，打印给用户的手动指引。"""
        if self.is_windows:
            return (
                f"请把 {npm_bin} 加入用户环境变量 Path"
                "（设置 → 系统 → 系统信息 → 高级系统设置 → 环境变量）"
            )
        return f'请把 export PATH="{home}/.local/bin:{npm_bin}:$PATH" 追加到 shell 配置'

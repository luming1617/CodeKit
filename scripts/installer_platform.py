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
from typing import Mapping, Optional, Sequence, Tuple


MACOS = "macos"
LINUX = "linux"
WINDOWS = "windows"

# 子进程输出统一按 UTF-8 解码；非法字节用替换字符兜底，绝不让解码失败冒到
# 调用方（那会丢掉整段输出，比多几个替换字符严重得多）。
SUBPROCESS_ENCODING = "utf-8"

# UAC 被拒绝或根本弹不出对话框时 Windows 给出的错误码（ERROR_CANCELLED）。
ELEVATION_CANCELLED = 1223


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


def resolve_argv(args: Sequence[str]) -> list[str]:
    """把 argv[0] 解析成可执行文件全路径，其余参数原样保留。

    npm/venv 落地的入口在 Windows 上是 ``.cmd``，裸命令名会直接抛 WinError 2；
    已经是路径形式（含分隔符）的 argv[0] 原样返回，不做二次查找。
    """
    resolved = [str(item) for item in args]
    if resolved and not any(separator in resolved[0] for separator in (os.sep, "/", "\\")):
        found = resolve_exe(resolved[0])
        if found:
            resolved[0] = found
    return resolved


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


def has_contents(path: Path) -> bool:
    """判断路径是否还留着内容，供「卸载残留」判定使用。

    空目录不算残留：它不会出现在宿主的待清理文件列表里，也没必要为它报失败。
    符号链接按存在处理，因为链接本身就是要清掉的东西。
    """
    if path.is_symlink() or path.is_file():
        return True
    try:
        next(path.iterdir())
    except (OSError, StopIteration):
        return False
    return True


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


def powershell_literal(value: str) -> str:
    """把任意字符串包成 PowerShell 单引号字面量。"""
    return "'" + str(value).replace("'", "''") + "'"


def run_elevated(command: Sequence[str], timeout: float = 600) -> int:
    """经 UAC 提权执行单条命令，返回该命令的真实退出码。

    机器级 MSI 的卸载需要管理员权限，而 ``winget uninstall`` 不会自己提权：实测
    msiexec 被 Windows Installer 直接拒绝（事件 11730，Error 1730），winget 把
    1603 包成 ``0x8A150030`` 返回。因此这里按 §3 的 sudo 策略只对**这一条命令**
    提权，而不是把整个卸载器提权 —— 卸载器的其余动作全是用户态操作。

    提权经 PowerShell 的 ``Start-Process -Verb RunAs``；子进程退出码写进临时文件，
    因为 ShellExecute 拉起的进程在父进程里拿不到可靠的 ``ExitCode``。用户在 UAC
    上点「否」或当前会话弹不出对话框时返回 :data:`ELEVATION_CANCELLED`。
    """
    if not is_windows():
        return 127
    shell = resolve_exe("powershell") or resolve_exe("pwsh")
    executable = resolve_exe(command[0])
    if shell is None or executable is None:
        return 127
    with tempfile.TemporaryDirectory() as directory:
        code_file = Path(directory) / "exit-code.txt"
        steps = Path(directory) / "elevated.ps1"
        # 先在父进程把文件建好：提权进程新建的文件属主是 Administrators，非提权令牌里
        # 这个 SID 是 deny-only，读回来会 PermissionError。写进一个已存在的用户文件
        # 没有这个限制（管理员令牌同时带着用户 SID）。
        code_file.write_text("", encoding="ascii")
        # 子脚本只由提权后的进程执行；CJK 路径要靠 BOM 才不会被 PS 5.1 按 ANSI 解析。
        steps.write_text(
            "\n".join(
                (
                    "& " + " ".join(powershell_literal(item) for item in (executable, *command[1:])),
                    f"Set-Content -LiteralPath {powershell_literal(str(code_file))}"
                    " -Value $LASTEXITCODE -Encoding ascii",
                )
            ),
            encoding="utf-8-sig",
        )
        launcher = (
            f"Start-Process -FilePath {powershell_literal(shell)} -Verb RunAs -Wait -PassThru"
            " -ErrorAction Stop -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass',"
            f" '-File', {powershell_literal(str(steps))})"
        )
        try:
            completed = subprocess.run(
                [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", launcher],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
                **text_kwargs(),
            )
        except (OSError, subprocess.SubprocessError):
            return 127
        try:
            recorded = code_file.read_text(encoding="ascii", errors="replace").strip()
        except OSError:
            recorded = ""
        if recorded.startswith("-"):
            recorded = recorded[1:]
        if recorded.isdigit():
            return int(recorded)
        # 没有退出码 = 提权那一步就没跑起来（UAC 被拒、无交互桌面等）。
        return ELEVATION_CANCELLED if completed.returncode != 0 else 127


def refresh_process_path(environ: Optional[dict] = None) -> Tuple[str, ...]:
    """把注册表里的用户/系统 PATH 合并进当前进程，返回新增的目录。

    Windows 安装器（winget、官方 .ps1 等）只写注册表，不会改变**已经运行**的进程；
    不刷新就会出现"安装成功但 command_exists 仍为假"：安装器把成功通道误判为失败，
    再降级到下一个通道装出第二份拷贝。
    """
    if not is_windows():
        return ()
    import winreg

    env = os.environ if environ is None else environ
    current = env.get("PATH", "")
    known = {
        os.path.normcase(os.path.normpath(entry))
        for entry in current.split(os.pathsep)
        if entry
    }
    added: list[str] = []
    for hive, key in (
        (winreg.HKEY_CURRENT_USER, "Environment"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
    ):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value, _ = winreg.QueryValueEx(handle, "Path")
        except OSError:
            continue
        for raw in str(value).split(os.pathsep):
            entry = os.path.expandvars(raw.strip())
            if not entry:
                continue
            normalized = os.path.normcase(os.path.normpath(entry))
            if normalized in known:
                continue
            known.add(normalized)
            added.append(entry)
    if added:
        env["PATH"] = os.pathsep.join(entry for entry in (current, *added) if entry)
    return tuple(added)


def machine_path_entries() -> Tuple[str, ...]:
    """只返回机器级（HKLM）PATH 的目录；非 Windows 返回空元组。

    判断某个命令「无需用户级 PATH 也能被找到」只能看注册表的机器级 Path：当前
    进程的 ``PATH`` 已经把用户级与机器级合并了，从它看不出命令的来源。Codex
    桌面端的 shell 只继承机器级 PATH，因此安装器的落地自检需要这份数据。
    """
    if not is_windows():
        return ()
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ) as handle:
            value, _ = winreg.QueryValueEx(handle, "Path")
    except OSError:
        return ()
    entries = []
    for raw in str(value).split(os.pathsep):
        entry = os.path.expandvars(raw.strip())
        if entry:
            entries.append(entry)
    return tuple(entries)


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

    def toolchain_root(self, home: Path) -> Path:
        """CodeKit 自持工具链根：运行期唯一必须可读（并在沙箱内可执行）的目录。

        宿主沙箱是按路径拒绝访问的：Codex 桌面版拒绝 `%APPDATA%` 与
        `%LOCALAPPDATA%\\Programs`，却放行用户数据目录下的这一处（已实测）。因此
        凡是要被技能在会话里启动的东西，都必须落在这里，而不是 npm 全局目录或
        系统 Python 的安装目录。
        """
        return Path(home) / ".local" / "share" / "CodeKit"

    def runtime_root(self, home: Path) -> Path:
        """内置 Python 运行时的存放根；各版本按 tag 并列保留，便于回退。"""
        return self.toolchain_root(home) / "runtime"

    def runtime_dir(self, home: Path, tag: str) -> Path:
        """某个 tag 的运行时的落地目录。"""
        return self.runtime_root(home) / tag

    def runtime_current(self, home: Path) -> Path:
        """记录当前生效 tag 的指针文件；与 ``toolchain.json`` 相互校验。"""
        return self.runtime_root(home) / "current.json"

    def runtime_python(self, runtime_dir: Path) -> Path:
        """运行时里的解释器规范位置。

        python-build-standalone 的 ``install_only`` 包解开后顶层是 ``python/``，
        Windows 直接在该目录下，POSIX 在 ``bin/``。解包代码负责归一成这个形状，
        这里只描述「应该在哪」。
        """
        if self.is_windows:
            return Path(runtime_dir) / "python" / "python.exe"
        return Path(runtime_dir) / "python" / "bin" / "python3"

    def npm_prefix_private(self, home: Path) -> Path:
        """CodeKit 私有 npm 前缀。

        gitnexus 只装在 ``%APPDATA%\\npm`` 时在沙箱内既不可读也不可执行（实测
        stat 即 EACCES），因此改用它自己的前缀：目录落在可读根下，启动时用
        ``<绝对 node> <绝对 cli.js>``，不再依赖 npm 生成的 ``.cmd`` 垫片。
        """
        return self.toolchain_root(home) / "npm"

    def toolchain_manifest(self, home: Path) -> Path:
        """安装器写给技能读的工具链清单。"""
        return self.toolchain_root(home) / "toolchain.json"

    def venv_dir(self, home: Path, command: str) -> Path:
        """私有 venv 的落地目录。"""
        return self.toolchain_root(home) / "venvs" / command

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

    def shell_path_configs(
        self, home: Path, environ: Optional[Mapping[str, str]] = None
    ) -> Tuple[Tuple[Path, str], ...]:
        """需要写入 PATH 的全部 shell 配置，而不只是当前登录 shell 的交互配置。

        agent（Claude / Codex / OpenCode / Qoder）派生的 shell 多为 login 或
        非交互 bash，只读 ``~/.profile``，不读 ``~/.bashrc``；安装时的
        ``$SHELL`` 又可能指向 zsh。只写当前 shell 的交互配置会让 ``gitnexus``
        这类用户态 CLI 在 agent 里不可见，命令因而回落到 ``npx @latest``，
        导致已建索引的分析器身份漂移并触发全量重建。

        已存在的 ``~/.bash_profile`` 只追加、不创建：一旦创建它会覆盖
        ``~/.profile``（bash login shell 读前者就不再读后者），反而丢掉
        ``.bashrc`` 的加载。
        """
        if self.is_windows:
            return ()
        env = os.environ if environ is None else environ
        shell = Path(env.get("SHELL", "/bin/bash")).name
        home = Path(home)
        export = 'export PATH="${HOME}/.local/bin:${HOME}/.npm-global/bin:${PATH}"'
        configs: list[tuple[Path, str]] = [
            (home / ".bashrc", export),
            (home / ".profile", export),
        ]
        if (home / ".bash_profile").exists():
            configs.append((home / ".bash_profile", export))
        if shell == "zsh" or (home / ".zshrc").exists():
            configs.append((home / ".zshrc", export))
        if shell == "zsh" or (home / ".zprofile").exists():
            configs.append((home / ".zprofile", export))
        fish = home / ".config" / "fish" / "config.fish"
        if shell == "fish" or fish.exists():
            configs.append(
                (fish, 'fish_add_path -g "${HOME}/.local/bin" "${HOME}/.npm-global/bin"')
            )
        return tuple(configs)

    def shell_rc_files(self, home: Path) -> Tuple[Path, ...]:
        """卸载时需要清理的 shell 配置文件；Windows 上没有。

        含 ``~/.profile``：安装会往 login shell 配置里写 PATH，卸载必须对称清理。
        """
        if self.is_windows:
            return ()
        home = Path(home)
        return (
            home / ".bashrc",
            home / ".bash_profile",
            home / ".profile",
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

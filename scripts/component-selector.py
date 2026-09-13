#!/usr/bin/env python3
"""Interactive terminal component selector used by install.sh.

The shell writes a tab-separated component manifest and this program owns all
terminal rendering and key handling.  The final selection is emitted as a
single machine-readable line on stdout; the UI itself is written to /dev/tty.

Rendering is platform-independent and stays available everywhere (`--render`);
only the interactive mode needs POSIX terminal control, so `termios`/`tty` are
imported defensively instead of crashing the module on Windows.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import signal
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, List, Optional, Tuple

try:  # POSIX-only; only the interactive mode needs these.
    import termios
    import tty
except ImportError:  # pragma: no cover - Windows
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]

import installer_platform as platform_layer  # noqa: E402


ESC = "\033["
RESET = f"{ESC}0m"
BOLD = f"{ESC}1m"
GREEN = f"{ESC}0;32m"
RED = f"{ESC}0;31m"
CYAN = f"{ESC}0;36m"

# 进入/退出备用屏幕并隐藏/恢复光标；两个平台的后端共用同一组序列。
ENTER_ALT_SCREEN = f"{ESC}?1049h{ESC}?25l"
LEAVE_ALT_SCREEN = f"{ESC}?25h{ESC}?1049l"

# 单字符按键到逻辑事件的映射；两个后端归一化到同一组事件，状态机因此共用。
KEY_ALIASES = {
    " ": "toggle",
    "a": "all",
    "A": "all",
    "n": "none",
    "N": "none",
    "p": "path",
    "P": "path",
    "q": "quit",
    "Q": "quit",
}


@dataclass
class Component:
    index: int
    name: str
    group: str
    group_title: str
    tag: str
    selected: bool


def display_width(value: str) -> int:
    """Return the terminal column width of a Unicode string."""
    return sum(
        0
        if unicodedata.combining(char)
        else 2
        if unicodedata.east_asian_width(char) in {"W", "F"}
        else 1
        for char in value
    )


def load_components(path: Path) -> List[Component]:
    """读取并校验安装器生成的 TSV 清单，转换为终端选择器组件对象。"""
    components: List[Component] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw:
            continue
        fields = raw.split("\t")
        if len(fields) != 6:
            raise ValueError(f"invalid selector manifest line {line_number}")
        index, name, group, group_title, tag, selected = fields
        if selected not in {"0", "1"}:
            raise ValueError(f"invalid selected state on manifest line {line_number}")
        components.append(
            Component(
                index=int(index),
                name=name,
                group=group,
                group_title=group_title,
                tag=tag,
                selected=selected == "1",
            )
        )
    if not components:
        raise ValueError("selector manifest contains no components")
    expected = list(range(1, len(components) + 1))
    if [component.index for component in components] != expected:
        raise ValueError("selector component indices must be contiguous and 1-based")
    return components


def banner() -> str:
    """返回安装模式使用的 CodeKit 终端标题图案。"""
    return "\n".join(
        (
            f"{GREEN} ██████╗ ██████╗ ██████╗ ███████╗██╗  ██╗██╗████████╗",
            " ██╔════╝██╔═══██╗██╔══██╗██╔════╝██║ ██╔╝██║╚══██╔══╝",
            " ██║     ██║   ██║██║  ██║█████╗  █████╔╝ ██║   ██║   ",
            " ██║     ██║   ██║██║  ██║██╔══╝  ██╔═██╗ ██║   ██║   ",
            " ╚██████╗╚██████╔╝██████╔╝███████╗██║  ██╗██║   ██║   ",
            f"  ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝╚═╝  ╚═╝╚═╝   ╚═╝   {RESET}",
            "                     Plugin",
        )
    )


def render(
    components: List[Component],
    mode: str,
    cursor: int = 1,
    path_selected: bool = False,
    shell_config: str = "",
    show_banner: bool = True,
    color: bool = True,
) -> str:
    """根据组件状态渲染当前选择器画面，可选择隐藏颜色用于快照测试。"""
    color_prefix = RED if mode == "uninstall" else GREEN
    title = "Select Components to Uninstall" if mode == "uninstall" else "Select Components"
    lines: List[str] = []
    if mode == "install" and show_banner:
        lines.append(banner())
    lines.extend((f"{color_prefix}===== {title} ====={RESET}", ""))

    previous_group = ""
    for component in components:
        if component.group != previous_group:
            if previous_group:
                lines.append("")
            lines.append(f"  {CYAN}{component.group_title}{RESET}")
            previous_group = component.group

        active = component.index == cursor
        pointer = f"{color_prefix}{BOLD}▶{RESET}" if active else " "
        mark = f"{color_prefix}[•]{RESET}" if component.selected else "[ ]"
        name = f"{BOLD}{component.name}{RESET}" if active else component.name
        padding = " " * max(1, 30 - display_width(component.name))
        lines.append(
            f"  {pointer} {component.index:2d}  {mark}  {name}{padding}{component.tag}"
        )

    if mode == "install":
        mark = f"{GREEN}[•]{RESET}" if path_selected else "[ ]"
        path_tag = "" if path_selected else " (already configured)"
        lines.extend(
            (
                "",
                f"  {BOLD}p{RESET}  {mark}  Setup PATH in shell config "
                f"({shell_config or 'not detected'}){path_tag}",
                "",
                f"  {CYAN}状态{RESET}  组件未安装→默认勾选安装    组件有更新→默认勾选升级    "
                "组件已最新→默认不勾选（勾选后重装）",
                f"  {CYAN}提示{RESET}  插件条目区分「插件状态+版本」和「CLI 本体状态+版本」；"
                "CLI 未安装的平台插件默认不勾选，勾选后会先自动安装该平台 CLI，再安装插件。",
                f"  {BOLD}↑/↓{RESET} 定位  {BOLD}Space{RESET} 勾选/取消  "
                f"{BOLD}a{RESET} 全选  {BOLD}n{RESET} 全不选  {BOLD}p{RESET} 切换 PATH  "
                f"{BOLD}Enter{RESET} 确认  {BOLD}q{RESET} 退出",
            )
        )
    else:
        lines.extend(
            (
                "",
                f"  {CYAN}状态{RESET}  组件未安装→默认不勾选（不处理）    "
                "组件已安装→默认勾选（确认后卸载）",
                f"  {BOLD}↑/↓{RESET} 定位  {BOLD}Space{RESET} 勾选/取消  "
                f"{BOLD}a{RESET} 全选  {BOLD}n{RESET} 全不选  "
                f"{BOLD}Enter{RESET} 确认卸载  {BOLD}q{RESET} 退出",
            )
        )

    frame = "\n".join(lines)
    if not color:
        for code in (RESET, BOLD, GREEN, RED, CYAN):
            frame = frame.replace(code, "")
    return frame


def read_key(stream: BinaryIO) -> str:
    """读取一个 POSIX 终端按键，并将 ANSI 方向键和控制键归一化为逻辑事件。"""
    first = stream.read(1)
    if first == b"":
        raise EOFError
    if first in {b"\r", b"\n"}:
        return "confirm"
    if first == b"\x03":
        raise KeyboardInterrupt
    if first == b"\x1b":
        second = stream.read(1)
        if second in {b"[", b"O"}:
            third = stream.read(1)
            return {b"A": "up", b"B": "down"}.get(third, "other")
        return "other"
    return KEY_ALIASES.get(first.decode("ascii", "replace"), "other")


class TerminalBackend:
    """选择器需要的终端能力：写画面、读按键、问尺寸、声明可注册的信号。"""

    def open(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def write(self, text: str) -> None:
        raise NotImplementedError

    def read_key(self) -> str:
        raise NotImplementedError

    def size(self) -> Tuple[int, int]:
        raise NotImplementedError

    def signals(self) -> Tuple[int, ...]:
        raise NotImplementedError


class PosixTerminal(TerminalBackend):
    """POSIX 后端：直接打开 /dev/tty，用 termios 进 raw 模式。"""

    def open(self) -> None:
        self._fd = os.open("/dev/tty", os.O_RDWR)
        self._stream = os.fdopen(self._fd, "r+b", buffering=0)
        self._saved_attrs = termios.tcgetattr(self._fd)
        tty.setraw(self._fd)
        self.write(ENTER_ALT_SCREEN)

    def close(self) -> None:
        try:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved_attrs)
            self.write(LEAVE_ALT_SCREEN)
        finally:
            self._stream.close()

    def write(self, text: str) -> None:
        self._stream.write(text.encode())
        self._stream.flush()

    def read_key(self) -> str:
        return read_key(self._stream)

    def size(self) -> Tuple[int, int]:
        try:
            size = os.get_terminal_size(self._fd)
            return size.columns, size.lines
        except OSError:
            fallback = shutil.get_terminal_size(fallback=(80, 40))
            return fallback.columns, fallback.lines

    def signals(self) -> Tuple[int, ...]:
        return (signal.SIGTERM, signal.SIGHUP)


class WindowsConsole(TerminalBackend):
    """Windows 后端：CONOUT$ 写画面 + msvcrt 取键，并显式开启 ANSI 转义。

    Windows 没有 /dev/tty，但 ``CONOUT$`` 与它等价：始终指向真实控制台，因此画面
    不会被调用方的管道吞掉，stdout 仍然只留给机器可读的结果行。PowerShell/cmd
    默认不解释 ANSI 转义，必须自己开 ENABLE_VIRTUAL_TERMINAL_PROCESSING。
    """

    ENABLE_PROCESSED_OUTPUT = 0x0001
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
    ENABLE_WRAP_AT_EOL_OUTPUT = 0x0008

    def open(self) -> None:
        import msvcrt

        self._msvcrt = msvcrt
        self._stream = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._handle = ctypes.c_void_p(msvcrt.get_osfhandle(self._stream.fileno()))
        self._saved_mode = self._enable_virtual_terminal()
        self.write(ENTER_ALT_SCREEN)

    def _enable_virtual_terminal(self) -> Optional[int]:
        """开启 ANSI 转义解释并返回原模式；系统不支持时返回 None。"""
        try:
            mode = ctypes.c_uint()
            if not self._kernel32.GetConsoleMode(self._handle, ctypes.byref(mode)):
                return None
            original = mode.value
            wanted = (
                original
                | self.ENABLE_PROCESSED_OUTPUT
                | self.ENABLE_VIRTUAL_TERMINAL_PROCESSING
                | self.ENABLE_WRAP_AT_EOL_OUTPUT
            )
            if not self._kernel32.SetConsoleMode(self._handle, wanted):
                return None
        except (AttributeError, OSError, ValueError):
            return None
        return original

    def close(self) -> None:
        try:
            self.write(LEAVE_ALT_SCREEN)
            if self._saved_mode is not None and self._handle is not None:
                self._kernel32.SetConsoleMode(self._handle, self._saved_mode)
        finally:
            self._stream.close()

    def write(self, text: str) -> None:
        """经 WriteConsoleW 写宽字符。

        不能用 ``open("CONOUT$", "w", encoding="utf-8")`` 直接写：那样是往控制台
        句柄送 UTF-8 字节，而控制台按自身代码页（中文系统是 GBK）解释，中文会变
        乱码。WriteConsoleW 走 Unicode 通道，同时 ANSI 转义仍会被 VT 解析。
        """
        if self._handle is not None:
            written = ctypes.c_ulong()
            buffer = ctypes.create_unicode_buffer(text)
            if self._kernel32.WriteConsoleW(
                self._handle, buffer, len(text), ctypes.byref(written), None
            ):
                return
        # 没有可用控制台句柄时退回普通文本流。
        self._stream.write(text)
        self._stream.flush()

    def read_key(self) -> str:
        """读取一个按键；方向键在 Windows 上是前导字节 + 扫描码两字节。"""
        char = self._msvcrt.getwch()
        if char == "\x03":
            raise KeyboardInterrupt
        if char in ("\x00", "\xe0"):
            return {"H": "up", "P": "down"}.get(self._msvcrt.getwch(), "other")
        if char in ("\r", "\n"):
            return "confirm"
        return KEY_ALIASES.get(char, "other")

    def size(self) -> Tuple[int, int]:
        fallback = shutil.get_terminal_size(fallback=(80, 40))
        return fallback.columns, fallback.lines

    def signals(self) -> Tuple[int, ...]:
        names = ["SIGTERM"]
        if hasattr(signal, "SIGBREAK"):
            names.append("SIGBREAK")
        return tuple(getattr(signal, name) for name in names)


def open_backend() -> Optional[TerminalBackend]:
    """打开当前平台可用的终端后端；没有可用终端时返回 None。"""
    if termios is not None and tty is not None and os.path.exists("/dev/tty"):
        candidates: List[TerminalBackend] = [PosixTerminal()]
    elif platform_layer.is_windows():
        candidates = [WindowsConsole()]
    else:
        candidates = []
    for backend in candidates:
        try:
            backend.open()
        except OSError:
            continue
        return backend
    return None


def interactive_select(
    components: List[Component],
    mode: str,
    path_selected: bool,
    shell_config: str,
    backend: TerminalBackend,
) -> Optional[Tuple[List[int], bool]]:
    """在备用屏幕上循环处理选择，退出前恢复信号处理器。

    终端本身的进入/退出由后端负责（见 ``open_backend``/``close``），这里只跑键盘
    状态机，因此同一份渲染与交互逻辑在 POSIX 与 Windows 上完全共用。
    """
    previous_handlers = {sig: signal.getsignal(sig) for sig in backend.signals()}

    def interrupt(_signum, _frame) -> None:
        """把终止信号转换为统一的 KeyboardInterrupt 清理路径。"""
        raise KeyboardInterrupt

    cursor = 1
    try:
        for sig in previous_handlers:
            signal.signal(sig, interrupt)
        while True:
            _columns, height = backend.size()
            frame = render(
                components,
                mode,
                cursor,
                path_selected,
                shell_config,
                show_banner=height >= 31,
            )
            # raw 模式与备用屏幕下没有 ONLCR 转换；必须显式回车，否则只有 LF 会
            # 导致光标下移但不回到行首，后续每一行都会逐步向右漂移。
            backend.write(f"{ESC}2J{ESC}H" + frame.replace("\n", "\r\n") + "\r\n")
            key = backend.read_key()
            if key == "up":
                cursor = max(1, cursor - 1)
            elif key == "down":
                cursor = min(len(components), cursor + 1)
            elif key == "toggle":
                components[cursor - 1].selected = not components[cursor - 1].selected
            elif key == "all":
                for component in components:
                    component.selected = True
            elif key == "none":
                for component in components:
                    component.selected = False
            elif key == "path" and mode == "install":
                path_selected = not path_selected
            elif key == "confirm":
                return (
                    [component.index for component in components if component.selected],
                    path_selected,
                )
            elif key == "quit":
                return None
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def parse_args() -> argparse.Namespace:
    """解析交互选择器的模式、manifest 和渲染选项。"""
    parser = argparse.ArgumentParser(description="CodeKit terminal component selector")
    parser.add_argument("--mode", choices=("install", "uninstall"), required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--path-selected", choices=("0", "1"), default="0")
    parser.add_argument("--shell-config", default="")
    parser.add_argument(
        "--render",
        action="store_true",
        help="render one plain-text frame to stdout without reading the terminal",
    )
    return parser.parse_args()


def main() -> int:
    """加载清单并执行单帧渲染或交互选择，输出机器可读的选择结果。"""
    # 渲染帧含 `▶`/`╔` 等字符，Windows 默认的 ANSI 代码页编码不了它们；
    # 与此同时调用方一律按 UTF-8 读取本脚本的 stdout。
    platform_layer.configure_stdout_encoding()
    args = parse_args()
    try:
        components = load_components(args.manifest)
    except (OSError, ValueError) as exc:
        print(f"selector: {exc}", file=sys.stderr)
        return 2

    path_selected = args.path_selected == "1"
    if args.render:
        print(
            render(
                components,
                args.mode,
                path_selected=path_selected,
                shell_config=args.shell_config,
                color=False,
            )
        )
        return 0
    # 交互模式需要真实终端：POSIX 走 /dev/tty，Windows 走 CONOUT$ + msvcrt。
    # 拿不到终端时返回受控错误码，由调用方决定是否降级。
    backend = open_backend()
    if backend is None:
        print(
            "selector: interactive selection is unavailable on this platform "
            "(no usable terminal); use --render for a plain-text frame",
            file=sys.stderr,
        )
        return 2

    try:
        result = interactive_select(
            components, args.mode, path_selected, args.shell_config, backend
        )
    except (KeyboardInterrupt, EOFError):
        return 130
    except OSError as exc:
        print(f"selector: could not use terminal: {exc}", file=sys.stderr)
        return 2
    finally:
        backend.close()
    if result is None:
        return 10
    selected, path_selected = result
    print(f"selected={','.join(str(index) for index in selected)};path={int(path_selected)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

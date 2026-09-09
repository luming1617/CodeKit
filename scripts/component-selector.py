#!/usr/bin/env python3
"""Interactive terminal component selector used by install.sh.

The shell writes a tab-separated component manifest and this program owns all
terminal rendering and key handling.  The final selection is emitted as a
single machine-readable line on stdout; the UI itself is written to /dev/tty.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import sys
import termios
import tty
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, List, Optional, Tuple


ESC = "\033["
RESET = f"{ESC}0m"
BOLD = f"{ESC}1m"
GREEN = f"{ESC}0;32m"
RED = f"{ESC}0;31m"
CYAN = f"{ESC}0;36m"


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
    """读取一个终端按键，并将 ANSI 方向键和控制键归一化为逻辑事件。"""
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
    return {
        b" ": "toggle",
        b"a": "all",
        b"A": "all",
        b"n": "none",
        b"N": "none",
        b"p": "path",
        b"P": "path",
        b"q": "quit",
        b"Q": "quit",
    }.get(first, "other")


def interactive_select(
    components: List[Component], mode: str, path_selected: bool, shell_config: str
) -> Optional[Tuple[List[int], bool]]:
    """在备用屏幕和 raw 模式下循环处理选择，退出前恢复终端和信号处理器。"""
    tty_fd = os.open("/dev/tty", os.O_RDWR)
    with os.fdopen(tty_fd, "r+b", buffering=0) as terminal:
        descriptor = terminal.fileno()
        original = termios.tcgetattr(descriptor)
        previous_handlers = {
            sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)
        }

        def interrupt(_signum, _frame) -> None:
            """把终止信号转换为统一的 KeyboardInterrupt 清理路径。"""
            raise KeyboardInterrupt

        cursor = 1
        try:
            for sig in previous_handlers:
                signal.signal(sig, interrupt)
            tty.setraw(descriptor)
            terminal.write(f"{ESC}?1049h{ESC}?25l".encode())
            terminal.flush()
            while True:
                try:
                    height = os.get_terminal_size(descriptor).lines
                except OSError:
                    height = shutil.get_terminal_size(fallback=(80, 40)).lines
                frame = render(
                    components,
                    mode,
                    cursor,
                    path_selected,
                    shell_config,
                    show_banner=height >= 31,
                )
                # tty.setraw() 关闭了输出的 ONLCR 转换；必须显式回车，否则只有 LF
                # 会导致光标下移但不回到行首，后续每一行都会逐步向右漂移。
                frame_with_crlf = frame.replace("\n", "\r\n")
                terminal.write(f"{ESC}2J{ESC}H{frame_with_crlf}\r\n".encode())
                terminal.flush()
                key = read_key(terminal)
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
            termios.tcsetattr(descriptor, termios.TCSADRAIN, original)
            terminal.write(f"{ESC}?25h{ESC}?1049l".encode())
            terminal.flush()
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
    if not os.path.exists("/dev/tty"):
        print("selector: /dev/tty is unavailable", file=sys.stderr)
        return 2

    try:
        result = interactive_select(
            components, args.mode, path_selected, args.shell_config
        )
    except (KeyboardInterrupt, EOFError):
        return 130
    except OSError as exc:
        print(f"selector: could not use terminal: {exc}", file=sys.stderr)
        return 2
    if result is None:
        return 10
    selected, path_selected = result
    print(f"selected={','.join(str(index) for index in selected)};path={int(path_selected)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

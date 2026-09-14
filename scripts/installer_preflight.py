#!/usr/bin/env python3
"""System-dependency preflight for the CodeKit installer.

One question, asked once, for macOS, Linux and Windows alike: which hard
prerequisites are missing or too old, and what exactly should the user run to
fix each one.  The bash and PowerShell entry points used to answer it
separately -- two copies of the version comparison, the per-OS guidance strings,
the summary and the abort -- even though the answer does not depend on which
shell asked.  The entry points now only resolve an interpreter and hand over.

Python itself is deliberately *not* checked here: every entry point must prove
that the interpreter it is about to start meets the floor, because that is the
one prerequisite which cannot be verified by the program being started.  The
floor is declared once, as ``installer_config.MINIMUM_PYTHON``.

This module is a library (``installer.py`` calls :func:`check`); nothing runs it
as a program.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence

import installer_config as config
import installer_platform as platform_layer

# Node 24.0.0：gitnexus 的 engines 要求 24.11+，但 npm 只把 engines 当警告，所以真正的
# 下限由组件侧的 `requires` 把关；这里只拦住明显太旧的 Node。
NODE_MINIMUM = "24.0.0"

# 探测一个候选命令，返回 (是否找到, 版本行)；找不到时版本行为空串。
Probe = Callable[[str, Sequence[str]], "tuple[bool, str]"]

_OS_KEYS = (platform_layer.MACOS, platform_layer.LINUX, platform_layer.WINDOWS)


def _by_os(build: Callable[[str], str]) -> dict[str, str]:
    """按平台预生成指引文案，让查表逻辑保持成一行。"""
    return {key: build(key) for key in _OS_KEYS}


def _node_guidance(os_key: str) -> str:
    if os_key == platform_layer.MACOS:
        return "brew install node@24 | nvm install 24 | https://nodejs.org/en/download"
    if os_key == platform_layer.WINDOWS:
        return (
            "winget install OpenJS.NodeJS.LTS | nvm-windows: "
            "https://github.com/coreybutler/nvm-windows | https://nodejs.org/en/download"
        )
    return (
        "nvm/fnm 安装 24 LTS | Debian/Ubuntu: https://deb.nodesource.com/setup_24.x | "
        "https://nodejs.org/en/download"
    )


def _git_guidance(os_key: str) -> str:
    if os_key == platform_layer.MACOS:
        return "brew install git | xcode-select --install"
    if os_key == platform_layer.WINDOWS:
        return "winget install Git.Git | https://git-scm.com/install/windows"
    return "发行版包管理器安装 git（Debian/Ubuntu: sudo apt-get install -y git）"


def _download_guidance(os_key: str) -> str:
    if os_key == platform_layer.MACOS:
        return "brew install curl（macOS 通常自带 curl）"
    if os_key == platform_layer.WINDOWS:
        return "Windows 10 1803+ 自带 curl.exe；缺失时 winget install curl.curl"
    return "发行版包管理器安装 curl 或 wget（Debian/Ubuntu: sudo apt-get install -y curl）"


@dataclass(frozen=True)
class Dependency:
    """一项系统硬依赖；``commands`` 是「任一存在即可」的候选命令。"""

    label: str
    commands: tuple[str, ...]
    version_args: tuple[str, ...]
    minimum: str  # 空字符串表示「存在即可」
    guidance: Mapping[str, str]


DEPENDENCIES: tuple[Dependency, ...] = (
    Dependency(
        "Node.js", ("node",), ("-v",), NODE_MINIMUM, _by_os(_node_guidance)
    ),
    Dependency(
        "npm",
        ("npm",),
        ("--version",),
        "",
        _by_os(lambda key: f"与 Node.js 一同安装：{_node_guidance(key)}"),
    ),
    Dependency("git", ("git",), ("--version",), "", _by_os(_git_guidance)),
    Dependency(
        "curl/wget", ("curl", "wget"), ("--version",), "", _by_os(_download_guidance)
    ),
)


@dataclass
class Finding:
    """一项不满足的依赖，以及照做就能修好的命令。"""

    label: str
    required: str
    current: str
    guidance: str


def default_probe(command: str, version_args: Sequence[str]) -> tuple[bool, str]:
    """解析 ``command`` 并读它的第一行版本；命令不存在时返回 ``(False, "")``。"""
    executable = platform_layer.resolve_exe(command)
    if not executable:
        return False, ""
    try:
        completed = subprocess.run(
            [executable, *version_args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
            check=False,
            **platform_layer.text_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return True, ""
    return True, next(
        (
            line.strip()
            for line in (completed.stdout or "").splitlines()
            if line.strip()
        ),
        "",
    )


def findings(platform, probe: Optional[Probe] = None) -> list[Finding]:
    """实测每项硬依赖，只返回不满足的那些。

    ``probe`` 缺省时才查模块属性（而不是把函数对象绑成默认值），这样替换默认探测器的
    测试能真的生效。

    「命令在、版本读不出来」按不满足处理（并在 current 里写明 unknown）：宁可在
    这里挡住，也不要把一次注定失败的安装跑完。
    """
    probe = probe or default_probe
    problems: list[Finding] = []
    for dependency in DEPENDENCIES:
        guidance = dependency.guidance.get(platform.key, "")
        present = False
        version = ""
        for command in dependency.commands:
            present, version = probe(command, dependency.version_args)
            if present:
                break
        required = f">= {dependency.minimum}" if dependency.minimum else "存在即可"
        if not present:
            problems.append(Finding(dependency.label, required, "未安装", guidance))
            continue
        if dependency.minimum and (
            not version
            or not config.version_greater_or_equal(version, dependency.minimum)
        ):
            problems.append(
                Finding(dependency.label, required, version or "unknown", guidance)
            )
    return problems


def check(installer) -> bool:
    """把每一项缺失连同修复命令报出来；返回是否继续安装。

    强依赖只检测不安装：安装系统软件需要平台包管理器和管理员权限，超出安装器的职责，
    也会在受管机器上做出用户没同意的改动。
    """
    problems = findings(installer.platform)
    for finding in problems:
        installer.logger.error(f"{finding.label} 不满足安装要求")
        installer.logger.info(f"     · 要求：{finding.required}")
        installer.logger.info(f"     · 当前：{finding.current}")
        installer.logger.info(f"     · 安装：{finding.guidance}")
    if problems:
        installer.logger.error(
            f"检测到 {len(problems)} 项系统依赖不满足要求，安装已中止。"
            "请按上面的「安装」提示补齐后重新运行。"
        )
        return False
    installer.logger.success("系统依赖检查通过（git、Node.js >= 24、npm、curl/wget）。")
    return True

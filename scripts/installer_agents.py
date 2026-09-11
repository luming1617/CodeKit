#!/usr/bin/env python3
"""Coding-agent CLI specs for the CodeKit installer.

Only the CLI surface of each supported agent platform is described here. The
installer treats a platform's CLI as the prerequisite for that platform's
plugin, so a desktop application on its own never satisfies an install: when a
plugin is selected and its CLI is missing, the CLI is installed first.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import installer_config as config  # noqa: E402
import installer_qoder as qoder  # noqa: E402


# The channel table lives in component-registry.py (single source of truth for
# both optional components and coding-agent CLIs).
registry = config.load_module(SCRIPT_DIR / "component-registry.py", "codekit_component_registry")


@dataclass(frozen=True)
class AgentCLI:
    """One agent platform plus the channel table the installer probes."""

    key: str
    display_name: str
    component_id: str
    commands: tuple[str, ...]
    channels: Mapping[str, str]

    @property
    def npm_package(self) -> str:
        """Official npm package name for this CLI, or an empty string."""
        return self.channels.get("npm", "")

    @property
    def script_url(self) -> str:
        """Official installer-script URL for this CLI, or an empty string."""
        return self.channels.get("script", "")

    @property
    def powershell_url(self) -> str:
        """Windows-only PowerShell installer-script URL, or an empty string."""
        return self.channels.get("powershell", "")


def _qoder_entry(platform_key: str, display_name: str) -> AgentCLI:
    """Build a Qoder entry from the platform descriptor that owns its names."""
    descriptor = qoder.PLATFORMS[platform_key]
    return AgentCLI(
        key=platform_key,
        display_name=display_name,
        component_id=descriptor.component_id,
        commands=descriptor.cli_commands,
        channels=registry.AGENT_CLI_CHANNELS[platform_key],
    )


# Key, display name, plugin component and PATH commands for the npm/brew agents.
_AGENT_META: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("claude", "Claude Code", "claude_plugin", ("claude",)),
    ("codex", "Codex", "codex_plugin", ("codex",)),
    ("opencode", "OpenCode", "opencode_plugin", ("opencode",)),
)

AGENT_CLIS: tuple[AgentCLI, ...] = tuple(
    AgentCLI(key, display_name, component_id, commands, registry.AGENT_CLI_CHANNELS[key])
    for key, display_name, component_id, commands in _AGENT_META
) + (
    _qoder_entry("qoder", "Qoder"),
    _qoder_entry("qodercn", "Qoder CN"),
)

AGENT_CLIS_BY_KEY = {entry.key: entry for entry in AGENT_CLIS}
AGENT_CLIS_BY_COMPONENT = {entry.component_id: entry for entry in AGENT_CLIS}


def agent_cli(platform_key: str) -> Optional[AgentCLI]:
    """Return the CLI spec for a platform key, if the installer supports it."""
    return AGENT_CLIS_BY_KEY.get(platform_key)


def component_agent(component_id: str) -> Optional[AgentCLI]:
    """Return the CLI spec behind a plugin component, if there is one."""
    return AGENT_CLIS_BY_COMPONENT.get(component_id)


# 非 Qoder 平台全局 subagent 的默认目录；与 manage-global-agents.py 共用这一事实源。
DEFAULT_AGENT_DIRECTORIES = {
    "claude": (".claude", "agents"),
    "opencode": (".config", "opencode", "agents"),
    "codex": (".codex", "agents"),
}


def default_agent_directory(tool: str, home: Path) -> Path:
    """返回非 Qoder 平台全局 subagent 的默认目录。"""
    parts = DEFAULT_AGENT_DIRECTORIES.get(tool)
    if parts is None:
        raise ValueError(f"unknown agent tool: {tool}")
    return Path(home).joinpath(*parts)


def global_agent_plan(
    tool: str,
    home: Path,
    config_root: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> tuple[list[str], Path]:
    """返回同步/清理某平台全局 subagent 的额外参数与目标目录。

    Qoder 系共用共享配置根：显式 config_root 优先，其次环境变量，最后默认目录。
    """
    if tool not in qoder.PLATFORMS:
        return [], default_agent_directory(tool, home)
    if config_root is not None:
        root = Path(config_root).expanduser()
        return ["--config-root", str(root)], root / "agents"
    return qoder.agent_location(tool, home, environ)


def cli_command_path(
    entry: AgentCLI,
    home: Path,
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    """Return the platform CLI's executable path; desktop installs never count."""
    environ = os.environ if environ is None else environ
    if entry.script_url:
        # Qoder's installer drops the CLI inside the shared configuration root,
        # which is not on PATH until a shell reads the snippet it writes.
        return qoder.cli_path(entry.key, home, environ)
    for command in entry.commands:
        found = shutil.which(command, path=environ.get("PATH"))
        if found and not qoder.looks_like_desktop_launcher(found):
            return found
    return ""


def cli_presence(
    platform_key: str,
    home: Path,
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    """Describe where a platform CLI was found; an empty string means missing."""
    entry = agent_cli(platform_key)
    if entry is None:
        return ""
    return qoder.describe_cli(entry.commands, cli_command_path(entry, home, environ))


def cli_version(
    platform_key: str,
    home: Path,
    environ: Optional[Mapping[str, str]] = None,
    timeout: float = 15.0,
) -> str:
    """Read the CLI's own version; an empty string when it cannot be probed."""
    entry = agent_cli(platform_key)
    if entry is None:
        return ""
    path = cli_command_path(entry, home, environ)
    if not path:
        return ""
    try:
        completed = subprocess.run(
            [path, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    first_line = completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else ""
    return config.extract_version(first_line) or first_line


def channel_plan(entry: AgentCLI) -> list[tuple[str, str]]:
    """Return the CLI's declared channels in global priority order."""
    return [
        (kind, entry.channels[kind])
        for kind in registry.CHANNEL_ORDER
        if kind in entry.channels
    ]


def install_plan(entry: AgentCLI, os_name: Optional[str] = None) -> list[Sequence[str]]:
    """Return install commands for an agent CLI, ordered by the channel table."""
    plan: list[Sequence[str]] = []
    for kind, target in channel_plan(entry):
        if kind == "npm":
            plan.append(("npm", "install", "-g", f"{target}@latest"))
        elif kind == "brew":
            plan.append(("brew", "install", target))
        elif kind == "brew_cask":
            plan.append(("brew", "install", "--cask", target))
    return plan


def supports_install(entry: AgentCLI, os_name: Optional[str] = None) -> bool:
    """Report whether any install channel exists for this platform."""
    return bool(entry.channels)


def cli_extra_paths(entry: AgentCLI, home: Path) -> tuple[Path, ...]:
    """Return directories the installer must prepend so the new CLI is usable."""
    if not entry.script_url:
        return ()
    root = qoder.resolve_config_root(qoder.PLATFORMS[entry.key], home)
    return (root / "entry", root / "bin")


def fallback_note(entry: AgentCLI, *, windows: bool = False) -> str:
    """Describe the manual install command to show when automation fails.

    ``windows=True`` 时只给出 Windows 上真实可用的命令：Qoder 系在 Windows 只发布
    ``install.ps1``，``curl | bash`` 那条提示会把用户引到并不存在的 bash 上。
    """
    if windows:
        if entry.npm_package:
            registry_args = " ".join(config.NPM_REGISTRY_ARGS)
            return f"npm install -g {entry.npm_package} {registry_args}"
        winget_id = entry.channels.get("winget", "")
        if winget_id:
            return f"winget install --id {winget_id} --exact"
        if entry.powershell_url:
            return (
                "powershell -NoProfile -ExecutionPolicy Bypass -Command "
                f"\"irm {entry.powershell_url} | iex\""
            )
        return ""
    if entry.script_url:
        return f"curl -fsSL {entry.script_url} | bash"
    if entry.npm_package:
        registry_args = " ".join(config.NPM_REGISTRY_ARGS)
        return f"npm install -g {entry.npm_package} {registry_args}"
    brew_target = entry.channels.get("brew") or entry.channels.get("brew_cask")
    if brew_target:
        action = "brew install --cask" if "brew_cask" in entry.channels else "brew install"
        return f"{action} {brew_target}"
    return ""

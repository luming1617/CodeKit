#!/usr/bin/env python3
"""Coding-agent CLI specs for the CodeKit installer.

Only the CLI surface of each supported agent platform is described here. The
installer treats a platform's CLI as the prerequisite for that platform's
plugin, so a desktop application on its own never satisfies an install: when a
plugin is selected and its CLI is missing, the CLI is installed first.
"""

from __future__ import annotations

import os
import platform
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


@dataclass(frozen=True)
class AgentCLI:
    """One agent platform plus every channel the installer can install it from."""

    key: str
    display_name: str
    component_id: str
    commands: tuple[str, ...]
    brew_cask: str = ""
    brew_formula: str = ""
    npm_package: str = ""
    script_url: str = ""


# Official Qoder CLI channels; both distributions publish one script for macOS
# and Linux, so no per-OS branch is needed for them.
QODER_CLI_SCRIPTS = {
    "qoder": "https://qoder.com/install",
    "qodercn": "https://static.qoder.com.cn/qoder-cli-cn/install.sh",
}


def _qoder_entry(platform_key: str, display_name: str) -> AgentCLI:
    """Build a Qoder entry from the platform descriptor that owns its names."""
    descriptor = qoder.PLATFORMS[platform_key]
    return AgentCLI(
        key=platform_key,
        display_name=display_name,
        component_id=descriptor.component_id,
        commands=descriptor.cli_commands,
        script_url=QODER_CLI_SCRIPTS[platform_key],
    )


AGENT_CLIS: tuple[AgentCLI, ...] = (
    AgentCLI(
        key="claude",
        display_name="Claude Code",
        component_id="claude_plugin",
        commands=("claude",),
        brew_cask="claude-code",
        npm_package="@anthropic-ai/claude-code",
    ),
    AgentCLI(
        key="codex",
        display_name="Codex",
        component_id="codex_plugin",
        commands=("codex",),
        brew_cask="codex",
        npm_package="@openai/codex",
    ),
    AgentCLI(
        key="opencode",
        display_name="OpenCode",
        component_id="opencode_plugin",
        commands=("opencode",),
        brew_formula="opencode",
        npm_package="opencode-ai",
    ),
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
    path = cli_command_path(entry, home, environ)
    if not path:
        return ""
    name = Path(path).name
    command = next((item for item in entry.commands if item == name), entry.commands[0])
    return f"{command} ({path})"


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


def install_plan(entry: AgentCLI, os_name: str) -> list[Sequence[str]]:
    """Return the install channels to try, in order, for the current platform."""
    plan: list[Sequence[str]] = []
    if os_name == "Darwin":
        if entry.brew_cask:
            plan.append(("brew", "install", "--cask", entry.brew_cask))
        if entry.brew_formula:
            plan.append(("brew", "install", entry.brew_formula))
    if entry.npm_package:
        plan.append(
            ("npm", "install", "-g", f"{entry.npm_package}@latest", *config.NPM_REGISTRY_ARGS)
        )
    return plan


def supports_install(entry: AgentCLI, os_name: Optional[str] = None) -> bool:
    """Report whether any install channel exists for this platform."""
    os_name = os_name or platform.system()
    return bool(install_plan(entry, os_name) or entry.script_url)


def cli_extra_paths(entry: AgentCLI, home: Path) -> tuple[Path, ...]:
    """Return directories the installer must prepend so the new CLI is usable."""
    if not entry.script_url:
        return ()
    root = qoder.resolve_config_root(qoder.PLATFORMS[entry.key], home)
    return (root / "entry", root / "bin")


def fallback_note(entry: AgentCLI) -> str:
    """Describe the manual install command to show when automation fails."""
    if entry.script_url:
        return f"curl -fsSL {entry.script_url} | bash"
    if entry.npm_package:
        registry = " ".join(config.NPM_REGISTRY_ARGS)
        return f"npm install -g {entry.npm_package} {registry}"
    return ""

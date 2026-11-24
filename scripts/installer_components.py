#!/usr/bin/env python3
"""Plugin registration and global-subagent integration for the CodeKit installer.

Owns the per-platform plugin registrars (Claude / Codex / OpenCode / Qoder), the
shared plugin-install skeleton, and the global subagent sync that both install
and uninstall reuse. Functions take the installer session as their first
argument — the same convention as ``installer_toolchain`` and
``installer_channels`` — so ``Installer`` keeps thin delegates.

This module is a library: ``installer.py`` imports it; nothing runs it as a
program.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

import installer_agents as agents
import installer_codex as codex
import installer_config as config
import installer_platform as platform_layer
import installer_qoder as qoder
from installer_platform import InstallerError, RunResult

SCRIPT_DIR = Path(__file__).resolve().parent

# 插件源目录在会话对象上的属性名：两个 Qoder 发行版共用同一份本地源。
PLUGIN_SOURCE_ATTR = {
    "claude": "market_dir",
    "codex": "market_dir",
    "opencode": "opencode_source_dir",
    "qoder": "qoder_source_dir",
    "qodercn": "qoder_source_dir",
}


def codex_can_run_rtk(installer) -> bool:
    """Codex 会话能否执行 ``rtk``：只有桌面端会丢掉用户级 PATH。

    装了桌面版且 ``rtk`` 不在机器级 PATH 上时，``rtk init -g --codex`` 写出的
    ``RTK.md`` 前缀约束在 Codex 里永远无法执行；CLI 从终端启动时会继承完整
    PATH，因此不跳过它的注册。
    """
    path = codex.config_path(installer.home)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return True
    if not codex.desktop_app_present(text):
        return True
    return installer.visible_without_user_path("rtk")


# ---- 全局 subagent 同步（安装与卸载共用） ----------------------------


def platform_agents_plan(installer, action: str, tool: str) -> Optional[tuple[list[str], Path]]:
    """构造平台全局 subagent 的同步/清理命令与目标目录；无可执行动作时返回 None。"""
    helper = SCRIPT_DIR / "manage-global-agents.py"
    if not helper.is_file():
        return None
    args = [sys.executable, str(helper), action, "--home", str(installer.home), "--tool", tool]
    if action == "install":
        source = installer.marketplace_local / "skills" if installer.marketplace_local else None
        if source is None or not source.is_dir():
            return None
        args.extend(["--source", str(source)])
    flags, agents_dir = agents.global_agent_plan(tool, installer.home)
    args.extend(flags)
    return args, agents_dir


def run_platform_agents(installer, action: str, tool: str) -> bool:
    """同步或清理某平台的全局 subagent；安装与卸载共用同一实现与路径解析。"""
    try:
        plan = installer.platform_agents_plan(action, tool)
    except ValueError as exc:
        installer.logger.error(str(exc))
        return False
    if plan is None:
        return True
    args, agents_dir = plan
    result = installer.run_command(args)
    if result.returncode == 0:
        installer.track(agents_dir)
    return result.returncode == 0


def install_platform_agents(installer, tool: str) -> bool:
    """随对应插件同步该平台的全局 subagent。"""
    return installer.run_platform_agents("install", tool)


# ---- 插件安装骨架与平台注册器 ----------------------------------------


def install_plugin_component(installer, component_id: str) -> bool:
    """插件组件的统一安装骨架：CLI 前置 → 全局 subagent → 平台注册 → 播报。

    四个平台的差别只在 `_register_<platform>_plugin` 这一段，其余顺序与前置检查
    完全一致。抄四遍的结果是任何一处新增前置都要记得改四个地方。
    """
    entry = agents.component_agent(component_id)
    if entry is None:
        return False
    source_attr = PLUGIN_SOURCE_ATTR.get(entry.key)
    if source_attr is None or not getattr(installer, source_attr):
        return False
    registrar = installer._plugin_registrar(entry.key)
    if registrar is None:
        return False
    # 勾选插件但缺少 CLI 时先自动安装 CLI，再安装插件。
    if not installer.ensure_agent_cli(entry.key):
        return False
    # 先同步全局 subagent：失败即中止且不写注册表，避免“插件已写入并启用、组件却
    # 上报失败”的半安装状态。
    if not installer.install_platform_agents(entry.key):
        return False
    if not registrar():
        return False
    installer.announce_plugin_install(component_id)
    return True


def _plugin_registrar(installer, tool: str) -> Optional[Callable[[], bool]]:
    """按平台键取注册实现；Qoder 两个发行版共用同一份（共享配置根）。"""
    if tool in qoder.PLATFORMS:
        return lambda: installer._register_qoder_plugin(tool)
    return {
        "claude": installer._register_claude_plugin,
        "codex": installer._register_codex_plugin,
        "opencode": installer._register_opencode_plugin,
    }.get(tool)


def register_claude_plugin(installer) -> bool:
    """注册本地 Claude marketplace，并按已安装状态执行插件安装或更新。"""
    if installer.run_command(["claude", "plugin", "marketplace", "add", str(installer.market_dir)]).returncode != 0:
        return False
    installer.track(installer.home / ".claude" / "plugins" / "known_marketplaces.json")
    installer.run_command(["claude", "plugin", "marketplace", "update", config.MARKETPLACE_NAME])
    listing = installer.run_capture(["claude", "plugin", "list"]).stdout
    package = f"{config.PLUGIN_NAME}@{config.MARKETPLACE_NAME}"
    result = installer.run_command(["claude", "plugin", "update", package]) if package in listing else installer.run_command(["claude", "plugin", "install", package, "--scope", "user"])
    if result.returncode != 0:
        return False
    installer.track(installer.home / ".claude" / "plugins" / "installed_plugins.json")
    installer.record_marketplace_sha()
    return True


def register_opencode_plugin(installer) -> bool:
    """把本地 OpenCode 插件源写入全局配置；已存在时尝试强制刷新插件。"""
    # 写 opencode 自己解析到的那份配置：它按 opencode.json → opencode.jsonc
    # 的顺序取第一个存在的文件，写错文件会让同一个插件出现在两份配置里。
    path = config.opencode_config_file(installer.home / ".config" / "opencode")
    # 必须用 file URI 而不是直接拼路径：Windows 盘符路径含冒号与反斜杠，
    # `file://C:\...` 会被解析成主机名为盘符的 UNC 路径。
    plugin = f"codekit@{installer.opencode_source_dir.as_uri()}"
    try:
        if config.opencode_has_plugin(path):
            result = installer.run_command(["opencode", "plugin", plugin, "-g", "-f"]) if installer.run_capture(["opencode", "plugin", "--help"]).returncode == 0 else RunResult(0)
        else:
            config.add_opencode_plugin(path, plugin)
            result = RunResult(0)
    except ValueError:
        return False
    if result.returncode != 0:
        return False
    installer.track(path)
    return True


def register_codex_plugin(installer) -> bool:
    """注册本地 Codex marketplace，清理旧技能残留后添加 CodeKit 插件。"""
    # 旧版本可能把兼容技能放进 ~/.agents；安装前清掉旧生成物，避免重复加载。
    for path in (installer.home / ".agents" / "skills").glob("keel-*"):
        if path.is_dir():
            platform_layer.rmtree(path)
    added = installer.run_command(["codex", "plugin", "marketplace", "add", str(installer.market_dir)]).returncode == 0
    if not added and config.MARKETPLACE_NAME in installer.run_capture(["codex", "plugin", "marketplace", "list"]).stdout:
        added = True
    if not added:
        return False
    installer.track(installer.home / ".codex" / "config.toml")
    result = installer.run_command(["codex", "plugin", "add", f"{config.PLUGIN_NAME}@{config.MARKETPLACE_NAME}"])
    if result.returncode != 0:
        return False
    installer.record_marketplace_sha()
    return True


def register_qoder_plugin(installer, platform_key: str) -> bool:
    """把插件写进 Qoder 的共享配置根，并确认注册表里确实已安装且启用。"""
    descriptor = qoder.PLATFORMS[platform_key]
    try:
        root = qoder.resolve_config_root(descriptor, installer.home)
    except ValueError as exc:
        raise InstallerError(str(exc)) from exc
    if qoder.foreign_os_roots(descriptor) and not (root / "plugins").is_dir():
        installer.logger.warn(
            "Qoder keeps a separate configuration root per OS; this install "
            f"only reaches {root}, so a Qoder running on Windows stays unaware."
        )
    command = qoder.capable_cli(descriptor)
    try:
        if command:
            qoder.install_via_cli(command, root, installer.qoder_source_dir, runner=installer.qoder_run)
        else:
            # Desktop-only machines get the same registry the CLI would write.
            qoder.install_direct(root, installer.qoder_source_dir, installer.plugin_version)
        installer.track(qoder.registry_path(root))
        installer.track(qoder.settings_path(root))
        state = qoder.inspect(descriptor, installer.home)
    except ValueError as exc:
        raise InstallerError(str(exc)) from exc
    if not state.installed or not state.enabled:
        raise InstallerError(
            f"{qoder.PLUGIN_ID} is not enabled in {qoder.registry_path(state.root)} "
            "after install; the shared Qoder registry was not updated"
        )
    return True
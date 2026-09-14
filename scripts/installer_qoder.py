#!/usr/bin/env python3
"""Qoder and Qoder CN platform support for the CodeKit installer.

Qoder's CLI and desktop IDE share one user-level configuration root, so the
installed-plugin registry inside that root is read by both surfaces.  This
module treats the shared root as the only source of truth: detection never
requires a command on PATH, and installation writes the registry directly when
no capable CLI is available.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import installer_config as config  # noqa: E402
from installer_platform import has_contents, rmtree  # noqa: E402


PLUGIN_NAME = "codekit"
# Local-directory installs are registered under the implicit `local` marketplace.
CACHE_MARKETPLACE = "local"
PLUGIN_ID = f"{PLUGIN_NAME}@{CACHE_MARKETPLACE}"
REGISTRY_FILE = "installed_plugins_v2.json"


class QoderError(ValueError):
    """A shared Qoder file is unusable, or an install could not be verified."""


@dataclass(frozen=True)
class Platform:
    """One Qoder distribution plus every external name that identifies it."""

    key: str
    component_id: str
    display_name: str
    cli_commands: tuple[str, ...]
    cli_bin_env: str
    config_env: str
    config_dirname: str
    macos_apps: tuple[str, ...]
    linux_executables: tuple[str, ...]
    linux_desktop_files: tuple[str, ...]
    linux_package_dirs: tuple[str, ...]


PLATFORMS: dict[str, Platform] = {
    "qoder": Platform(
        key="qoder",
        component_id="qoder_plugin",
        display_name="CodeKit Plugin (Qoder)",
        cli_commands=("qoder", "qodercli"),
        cli_bin_env="QODER_CLI_BIN",
        config_env="QODER_CONFIG_DIR",
        config_dirname=".qoder",
        macos_apps=("Qoder IDE.app", "Qoder.app"),
        linux_executables=("qoder-ide", "qoder"),
        linux_desktop_files=("qoder-ide.desktop", "qoder.desktop", "com.qoder.ide.desktop"),
        linux_package_dirs=(
            "/usr/share/qoder-ide/qoder-ide",
            "/opt/Qoder/resources",
        ),
    ),
    "qodercn": Platform(
        key="qodercn",
        component_id="qodercn_plugin",
        display_name="CodeKit Plugin (Qoder CN)",
        cli_commands=("qodercn", "qoderclicn"),
        cli_bin_env="QODERCN_CLI_BIN",
        config_env="QODERCN_CONFIG_DIR",
        config_dirname=".qoder-cn",
        macos_apps=("Qoder CN IDE.app", "Qoder CN.app"),
        linux_executables=("qoder-cn-ide", "qoder-cn"),
        linux_desktop_files=(
            "qoder-cn-ide.desktop",
            "qoder-cn.desktop",
            "com.qoder.cn.ide.desktop",
        ),
        linux_package_dirs=(
            "/usr/share/qoder-cn-ide/qoder-cn-ide",
            "/opt/Qoder CN/resources",
        ),
    ),
}


COMPONENT_PLATFORMS = {item.component_id: item.key for item in PLATFORMS.values()}


@dataclass(frozen=True)
class InstallState:
    """What the shared configuration root says about the CodeKit plugin."""

    root: Path
    installed: bool
    enabled: bool
    version: str
    install_path: str
    leftover: bool = False


def empty_registry() -> dict[str, Any]:
    """Return the document an unconfigured shared root implies (never shared)."""
    return {"version": 2, "plugins": {}}


def platform_for_component(component_id: str) -> Optional[Platform]:
    """Return the Qoder platform served by a component id, if any."""
    key = COMPONENT_PLATFORMS.get(component_id)
    return PLATFORMS[key] if key else None


def run(args: Sequence[str], timeout: float = 20.0) -> tuple[int, str]:
    """Run a probe or CLI command; failures and timeouts become a failed result."""
    try:
        completed = subprocess.run(
            [str(item) for item in args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return completed.returncode, completed.stdout.strip()


def utc_timestamp() -> str:
    """Format a timestamp the way Qoder writes them (UTC, milliseconds, ``Z``)."""
    value = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    return value.replace("+00:00", "Z")


def resolve_config_root(
    descriptor: Platform,
    home: Path,
    environ: Optional[Mapping[str, str]] = None,
) -> Path:
    """Return the configuration root shared by the CLI and the desktop IDE."""
    environ = os.environ if environ is None else environ
    configured = (environ.get(descriptor.config_env) or "").strip()
    if not configured:
        return Path(home) / descriptor.config_dirname
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = Path(os.path.abspath(str(path)))
    if not path.is_dir():
        # Never create a root the user pointed at by hand: that would hide a
        # typo by installing into an empty directory nothing else reads.
        raise QoderError(
            f"{descriptor.config_env} points at a missing directory: {path}"
        )
    return path


def registry_path(root: Path) -> Path:
    """Return the shared installed-plugin registry path."""
    return Path(root) / "plugins" / REGISTRY_FILE


def settings_path(root: Path) -> Path:
    """Return the shared user settings path."""
    return Path(root) / "settings.json"


def read_registry(root: Path) -> dict:
    """Load the shared registry while keeping keys CodeKit does not own."""
    path = registry_path(root)
    try:
        document = config.read_json(path, default=empty_registry())
    except ValueError as exc:
        raise QoderError(f"unreadable Qoder plugin registry: {exc}") from exc
    if not isinstance(document, dict):
        raise QoderError(f"Qoder plugin registry must contain a JSON object: {path}")
    plugins = document.get("plugins")
    if plugins is None:
        document["plugins"] = {}
    elif not isinstance(plugins, dict):
        raise QoderError(
            f"Qoder plugin registry field 'plugins' must be an object: {path}"
        )
    return document


def plugin_entries(
    document: Mapping[str, Any], plugin_id: str = PLUGIN_ID
) -> list[dict[str, Any]]:
    """Read one plugin's install records, tolerating both object and list forms."""
    plugins = document.get("plugins")
    records = plugins.get(plugin_id) if isinstance(plugins, Mapping) else None
    if isinstance(records, Mapping):
        return [dict(records)]
    if isinstance(records, list):
        return [dict(item) for item in records if isinstance(item, Mapping)]
    return []


def _write_json(path: Path, document: Any) -> bool:
    try:
        return config.write_json(path, document)
    except ValueError as exc:
        raise QoderError(f"could not write {path}: {exc}") from exc


def register_plugin(root: Path, target: Path, version: str) -> None:
    """Add or refresh the CodeKit registry entry, preserving unknown keys."""
    document = read_registry(root)
    previous = plugin_entries(document)
    entry: dict[str, Any] = dict(previous[0]) if previous else {}
    stamp = utc_timestamp()
    entry.update(
        {
            "scope": "user",
            "installPath": str(target),
            "version": version,
            "installedAt": entry.get("installedAt") or stamp,
        }
    )
    if previous:
        entry["lastUpdated"] = stamp
    document["plugins"][PLUGIN_ID] = [entry]
    _write_json(registry_path(root), document)


def read_settings(root: Path) -> dict:
    """Load the shared settings document."""
    path = settings_path(root)
    try:
        document = config.read_json(path, default={})
    except ValueError as exc:
        raise QoderError(f"unreadable Qoder settings: {exc}") from exc
    if not isinstance(document, dict):
        raise QoderError(f"Qoder settings must contain a JSON object: {path}")
    return document


def enabled_flags(document: Mapping[str, Any], root: Path) -> Optional[dict]:
    """Return the ``enabledPlugins`` map, rejecting a document of the wrong shape."""
    flags = document.get("enabledPlugins")
    if flags is None:
        return None
    if not isinstance(flags, dict):
        raise QoderError(
            f"Qoder settings field 'enabledPlugins' must be an object: "
            f"{settings_path(root)}"
        )
    return flags


def plugin_enabled(root: Path, plugin_id: str = PLUGIN_ID) -> bool:
    """Report whether the shared settings still enable a plugin."""
    flags = enabled_flags(read_settings(root), root)
    if flags is None:
        return True
    # The desktop IDE may omit the key entirely; only an explicit false disables.
    return flags.get(plugin_id, True) is not False


def set_plugin_enabled(
    root: Path, enabled: Optional[bool], plugin_id: str = PLUGIN_ID
) -> bool:
    """Set or drop one ``enabledPlugins`` entry and report whether it changed."""
    document = read_settings(root)
    flags = enabled_flags(document, root)
    if flags is None:
        if enabled is None:
            return False
        flags = {}
        document["enabledPlugins"] = flags
    if enabled is None:
        if plugin_id not in flags:
            return False
        flags.pop(plugin_id)
    elif flags.get(plugin_id) is enabled:
        return False
    else:
        flags[plugin_id] = bool(enabled)
    return _write_json(settings_path(root), document)


# ---------------------------------------------------------------------------
# GitNexus registration.
#
# GitNexus ships its own editor target table, but Qoder is only half covered by
# it: the MCP entry goes to ``~/.qoder.json`` -- a file neither the Qoder CLI
# nor the desktop IDE reads (the real user-scope surface is ``mcpServers`` in
# the shared settings document) -- and the hook table has no Qoder target at
# all.  Everything below is therefore owned by CodeKit, not delegated upstream.
# ---------------------------------------------------------------------------

GITNEXUS_MCP_NAME = "gitnexus"
# Identifies entries CodeKit wrote; foreign hooks are never touched.
GITNEXUS_HOOK_NEEDLE = "gitnexus-hook"
GITNEXUS_HOOK_TIMEOUT_SECONDS = 10
GITNEXUS_HOOK_EVENTS: tuple[tuple[str, str, str], ...] = (
    ("PreToolUse", "Grep|Glob|Bash", "Enriching with GitNexus graph context..."),
    ("PostToolUse", "Bash", "Checking GitNexus index freshness..."),
)
GITNEXUS_HOOK_SCRIPTS: tuple[str, ...] = (
    "gitnexus-hook.cjs",
    "hook-db-lock-probe.cjs",
    "hook-lock.cjs",
    "resolve-analyze-cmd.cjs",
    "win-rm-list-json.ps1",
)


def skills_root(root: Path) -> Path:
    """Return the per-root skill directory Qoder scans on both surfaces."""
    return Path(root) / "skills"


def hook_script_dir(root: Path) -> Path:
    """Return CodeKit's own copy of the GitNexus hook scripts for one root."""
    return Path(root) / "hooks" / "gitnexus"


def register_mcp_server(root: Path, name: str, payload: Mapping[str, Any]) -> bool:
    """Merge one ``mcpServers`` entry, keeping foreign servers and keys."""
    document = read_settings(root)
    servers = document.get("mcpServers")
    if servers is None:
        servers = {}
        document["mcpServers"] = servers
    elif not isinstance(servers, dict):
        raise QoderError(
            f"Qoder settings field 'mcpServers' must be an object: {settings_path(root)}"
        )
    servers[name] = dict(payload)
    return _write_json(settings_path(root), document)


def remove_mcp_server(root: Path, name: str) -> bool:
    """Drop one ``mcpServers`` entry; the key itself goes when it turns empty."""
    document = read_settings(root)
    servers = document.get("mcpServers")
    if not isinstance(servers, dict) or name not in servers:
        return False
    servers.pop(name)
    if not servers:
        document.pop("mcpServers", None)
    return _write_json(settings_path(root), document)


def _hook_map(document: dict[str, Any], root: Path) -> dict[str, Any]:
    """Return the mutable ``hooks`` object, rejecting a document of the wrong shape."""
    hooks = document.get("hooks")
    if hooks is None:
        hooks = {}
        document["hooks"] = hooks
    elif not isinstance(hooks, dict):
        raise QoderError(
            f"Qoder settings field 'hooks' must be an object: {settings_path(root)}"
        )
    return hooks


def _mentions_hook_needle(entry: Any) -> bool:
    """True when a hook group is one CodeKit wrote for GitNexus."""
    try:
        return GITNEXUS_HOOK_NEEDLE in json.dumps(entry, ensure_ascii=False)
    except (TypeError, ValueError):
        return False


def register_gitnexus_hooks(root: Path, script: Path) -> bool:
    """Write GitNexus hook entries shaped like Claude Code's own.

    Previous CodeKit entries are replaced (a moved script must stop firing) and
    every foreign group is preserved.
    """
    command = f'node "{Path(script).as_posix()}"'
    document = read_settings(root)
    hooks = _hook_map(document, root)
    for event, matcher, status_message in GITNEXUS_HOOK_EVENTS:
        entries = hooks.get(event)
        if entries is None:
            entries = []
        elif not isinstance(entries, list):
            raise QoderError(
                f"Qoder settings hook event '{event}' must be a list: "
                f"{settings_path(root)}"
            )
        desired: dict[str, Any] = {
            "matcher": matcher,
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "timeout": GITNEXUS_HOOK_TIMEOUT_SECONDS,
                    "statusMessage": status_message,
                }
            ],
        }
        kept = [entry for entry in entries if not _mentions_hook_needle(entry)]
        kept.append(desired)
        hooks[event] = kept
    return _write_json(settings_path(root), document)


def remove_gitnexus_hooks(root: Path) -> bool:
    """Strip every CodeKit GitNexus hook group, keeping foreign hooks."""
    document = read_settings(root)
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event, entries in list(hooks.items()):
        if not isinstance(entries, list):
            continue
        kept = [entry for entry in entries if not _mentions_hook_needle(entry)]
        if len(kept) != len(entries):
            changed = True
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event)
    if changed and not hooks:
        document.pop("hooks", None)
    return _write_json(settings_path(root), document) if changed else False


def install_gitnexus_hook_scripts(root: Path, source_dir: Path) -> Path:
    """Copy the bundled hook scripts into this root so Qoder needs no Claude install."""
    source = Path(source_dir)
    missing = [name for name in GITNEXUS_HOOK_SCRIPTS if not (source / name).is_file()]
    if "gitnexus-hook.cjs" in missing:
        raise QoderError(f"no GitNexus hook script to install under {source}")
    target = hook_script_dir(root)
    target.mkdir(parents=True, exist_ok=True)
    for name in GITNEXUS_HOOK_SCRIPTS:
        if (source / name).is_file():
            shutil.copy2(source / name, target / name)
    return target / "gitnexus-hook.cjs"


def mirror_skill(root: Path, source: Path) -> Path:
    """Copy a prepared skill directory into this root's own skills directory."""
    source = Path(source)
    if not source.is_dir():
        raise QoderError(f"skill source directory is missing: {source}")
    target = skills_root(root) / source.name
    staging = skills_root(root) / f".{source.name}.codekit.{os.getpid()}"
    rmtree(staging, ignore_errors=True)
    try:
        staging.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, staging, dirs_exist_ok=True)
        if target.exists() or target.is_symlink():
            rmtree(target, ignore_errors=True)
            target.unlink(missing_ok=True)
        os.replace(staging, target)
    except OSError as exc:
        raise QoderError(f"could not mirror {source} into {target}: {exc}") from exc
    rmtree(staging, ignore_errors=True)
    return target


def remove_mirrored_skill(root: Path, name: str) -> bool:
    """Delete one mirrored skill directory from a Qoder root."""
    target = skills_root(root) / name
    if not target.exists():
        return False
    if target.is_dir() and not target.is_symlink():
        rmtree(target, ignore_errors=True)
    else:
        target.unlink(missing_ok=True)
    return not target.exists()


def existing_roots(descriptors: Iterable[Platform], home: Path) -> list[Path]:
    """Return the config roots of platforms that are actually present."""
    roots: list[Path] = []
    for descriptor in descriptors:
        try:
            root = resolve_config_root(descriptor, home)
        except (QoderError, OSError):
            continue
        if root.is_dir() and root not in roots:
            roots.append(root)
    return roots


def plugin_cache_root(root: Path) -> Path:
    """Return the cache directory holding locally installed Qoder plugins."""
    return Path(root) / "plugins" / "cache" / CACHE_MARKETPLACE / PLUGIN_NAME


def cached_copies(root: Path) -> list[Path]:
    """列出插件缓存中仍有内容的 CodeKit 副本目录。

    插件按 ``cache/<marketplace>/<plugin>`` 落盘，卸载后目录本身通常还在：CLI
    只在里面写一个 ``.orphaned_at`` 标记就当作删除了。残留判定因此以磁盘为准，
    不走注册表——注册表恰恰是 CLI 第一个清掉的东西。
    """
    try:
        marketplaces = sorted((Path(root) / "plugins" / "cache").iterdir())
    except OSError:
        return []
    return [
        candidate
        for marketplace in marketplaces
        if has_contents(candidate := marketplace / PLUGIN_NAME)
    ]


def remove_cached_copies(root: Path) -> None:
    """物理删除插件缓存里的 CodeKit 副本。

    ``plugins uninstall`` 是软删除：清掉注册表条目后在缓存目录写入
    ``.orphaned_at``，目录留着等它自己回收（实测 ``plugins list`` 也不回收）。
    卸载器必须自己删，否则用户会看到「卸载成功」但 skill 文件仍在，宿主还会把
    它们逐条列成待确认的残留。
    """
    for target in cached_copies(root):
        if target.is_dir() and not target.is_symlink():
            rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)


def remove_stale_versions(cache_root: Path, version: str) -> None:
    """Delete superseded plugin copies and interrupted staging directories."""
    if not cache_root.is_dir():
        return
    for candidate in sorted(cache_root.iterdir()):
        if candidate.name == version:
            continue
        if candidate.is_dir() and not candidate.is_symlink():
            # 缓存里的只读文件会让 shutil.rmtree 在 Windows 上抛 PermissionError。
            rmtree(candidate, ignore_errors=True)
        else:
            candidate.unlink(missing_ok=True)


def install_direct(root: Path, stage: Path, version: str) -> Path:
    """Copy the plugin source into the shared cache and register it.

    This is the path taken when only the desktop IDE is installed: the registry
    and settings files are the same ones the CLI reads, so both surfaces pick
    the plugin up without a CLI round trip.
    """
    cache_root = plugin_cache_root(root)
    target = cache_root / version
    staging = cache_root / f".{version}.codekit.{os.getpid()}.{uuid.uuid4().hex}"
    backup = cache_root / f".{version}.previous.{os.getpid()}"
    have_backup = False
    try:
        staging.mkdir(parents=True, exist_ok=True)
        shutil.copytree(Path(stage), staging, dirs_exist_ok=True)
        if target.exists() or target.is_symlink():
            os.replace(target, backup)
            have_backup = True
        os.replace(staging, target)
    except (OSError, ValueError) as exc:
        rmtree(staging, ignore_errors=True)
        if have_backup:
            # Windows 上 os.replace 不能覆盖已存在的目录，回滚前先清掉半成品。
            rmtree(target, ignore_errors=True)
            os.replace(backup, target)
        raise QoderError(f"could not write the Qoder plugin cache: {exc}") from exc
    if have_backup:
        rmtree(backup, ignore_errors=True)
    remove_stale_versions(cache_root, version)
    register_plugin(root, target, version)
    set_plugin_enabled(root, True)
    return target


def json_payload(output: str) -> Optional[dict]:
    """Parse a CLI JSON document, ignoring any banner lines around it."""
    text = (output or "").strip()
    if not text:
        return None
    for candidate in (text, text[text.find("{"): text.rfind("}") + 1]):
        try:
            document = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(document, dict):
            return document
    return None


def failure_text(document: Mapping[str, Any]) -> str:
    """Extract a human-readable error from a CLI JSON result."""
    for key in ("error", "message", "reason"):
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, Mapping):
            nested = failure_text(dict(value))
            if nested:
                return nested
    return json.dumps(document, ensure_ascii=False)[:200]


def find_cli(
    descriptor: Platform, environ: Optional[Mapping[str, str]] = None
) -> tuple[str, str]:
    """Return the first configured Qoder command name and its path."""
    environ = os.environ if environ is None else environ
    for command in descriptor.cli_commands:
        found = shutil.which(command, path=environ.get("PATH"))
        if found:
            return command, str(found)
    return "", ""


def looks_like_desktop_launcher(path: str) -> bool:
    """Reject IDE launchers that share the CLI command name."""
    normalized = path.replace("\\", "/").lower()
    return "/contents/" in normalized or "/.app/" in normalized


def _entry_candidates(directory: Path, command: str) -> tuple[Path, ...]:
    """列出入口目录中该命令可能出现的文件名。

    Windows 上官方安装器落地的是 ``qoder.cmd``（再转发给
    ``qoder-dispatcher.ps1``），没有无扩展名的入口文件；只按裸命令名查找会漏检。
    """
    if platform.system() != "Windows":
        return (Path(directory) / command,)
    return tuple(
        Path(directory) / f"{command}{suffix}" for suffix in ("", ".cmd", ".bat", ".exe")
    )


def _usable_entry(path: Path) -> bool:
    """判断入口文件是否可以被直接调用。"""
    if not path.is_file():
        return False
    return platform.system() == "Windows" or os.access(path, os.X_OK)


# 入口目录里落地的这个文件只是转发器：真正的 CLI 在别处，后端不存在时它退出 127。
_DISPATCHER_MARKER = "-dispatcher.ps1"


def _is_dispatcher_wrapper(path: str) -> bool:
    """判断入口文件是不是转发给后端 CLI 的 dispatcher。

    官方安装器和 npm 包都只在 ``<root>/entry`` 落地一个转发脚本，卸载 CLI 不会
    删除它，因此入口文件本身不能单独作为“已安装”的证据。
    """
    try:
        head = Path(path).read_text(encoding="utf-8", errors="ignore")[:2000]
    except OSError:
        return False
    return _DISPATCHER_MARKER in head


def _wrapper_backend(
    descriptor: Platform, root: Path, environ: Mapping[str, str]
) -> str:
    """返回 dispatcher 后端 CLI 的路径，解析顺序与 qoder-dispatcher.ps1 一致。"""
    override = environ.get(descriptor.cli_bin_env, "")
    if override and Path(override).is_file():
        return str(override)
    for command in descriptor.cli_commands[1:]:
        found = shutil.which(command, path=environ.get("PATH"))
        if found and not looks_like_desktop_launcher(found):
            return str(found)
        for candidate in _entry_candidates(root / "bin" / command, command):
            if _usable_entry(candidate):
                return str(candidate)
    return ""


def cli_path(
    platform_key: str,
    home: Path,
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    """Return the platform CLI's executable path, ignoring desktop launchers.

    The official installer drops the CLI inside the shared configuration root
    (``<root>/entry``) and only then adds it to PATH from the shell profile, so
    a plain PATH lookup misses a CLI that was just installed in this process.
    That entry file forwards to the real CLI, so it only counts while its
    backend still exists; reporting the backend also keeps ``--version`` probes
    working, which a ``.cmd`` forwarding script would break.
    """
    descriptor = PLATFORMS[platform_key]
    environ = os.environ if environ is None else environ
    _name, found = find_cli(descriptor, environ)
    if (
        found
        and not looks_like_desktop_launcher(found)
        and not _is_dispatcher_wrapper(found)
    ):
        return found
    try:
        root = resolve_config_root(descriptor, home, environ)
    except ValueError:
        return ""
    for directory in (root / "entry", root / "bin"):
        for command in descriptor.cli_commands:
            for candidate in _entry_candidates(directory, command):
                if not _usable_entry(candidate):
                    continue
                if not _is_dispatcher_wrapper(candidate):
                    return str(candidate)
                backend = _wrapper_backend(descriptor, root, environ)
                if backend:
                    return backend
    return ""


def describe_cli(commands: Sequence[str], path: str) -> str:
    """把 CLI 路径渲染为 ``命令 (路径)``；空路径表示未找到。"""
    if not path:
        return ""
    name = Path(path).name
    command = next((item for item in commands if item == name), commands[0])
    return f"{command} ({path})"


def cli_presence(
    platform_key: str,
    home: Path,
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    """Describe the platform CLI's location; an empty string means missing."""
    descriptor = PLATFORMS[platform_key]
    return describe_cli(
        descriptor.cli_commands, cli_path(platform_key, home, environ)
    )


def capable_cli(
    descriptor: Platform,
    environ: Optional[Mapping[str, str]] = None,
    runner: Any = run,
) -> str:
    """Return a CLI path only when it can install a plugin and report JSON."""
    environ = os.environ if environ is None else environ
    _name, path = find_cli(descriptor, environ)
    if not path or looks_like_desktop_launcher(path):
        return ""
    returncode, output = runner([path, "--version"], timeout=8)
    if returncode != 0 or not config.version_tuple(output):
        return ""
    returncode, help_text = runner([path, "plugins", "install", "--help"], timeout=8)
    if returncode != 0 or "--json" not in help_text or "--scope" not in help_text:
        return ""
    return path


def install_via_cli(
    command: str, root: Path, stage: Path, runner: Any = run
) -> str:
    """Install through the Qoder CLI and verify the JSON result it reports."""
    returncode, output = runner(
        [
            command,
            "--config-dir",
            str(root),
            "plugins",
            "install",
            str(stage),
            "--scope",
            "user",
            "--json",
        ],
        timeout=180,
    )
    document = json_payload(output)
    if document is None:
        raise QoderError(
            "`plugins install --json` returned no result "
            f"(exit code {returncode}); the CLI is too old or the install failed"
        )
    if document.get("ok") is not True:
        raise QoderError(f"Qoder plugin install failed: {failure_text(document)}")
    return str(document.get("pluginId") or PLUGIN_ID)


def inspect(
    descriptor: Platform,
    home: Path,
    environ: Optional[Mapping[str, str]] = None,
) -> InstallState:
    """Read plugin state from the shared root; never depends on a CLI."""
    root = resolve_config_root(descriptor, home, environ)
    entries = [
        entry
        for entry in plugin_entries(read_registry(root))
        if str(entry.get("scope", "user")) == "user"
    ]
    entry = entries[0] if entries else None
    install_path = str(entry.get("installPath", "")) if entry else ""
    version = str(entry.get("version", "")) if entry else ""
    installed = bool(install_path) and Path(install_path).expanduser().is_dir()
    return InstallState(
        root=root,
        installed=installed,
        enabled=plugin_enabled(root) if installed else False,
        version=version,
        install_path=install_path,
        leftover=not installed and bool(cached_copies(root)),
    )


def _cache_confined(root: Path, install_path: str) -> Optional[Path]:
    """Resolve an install path only when it sits inside the plugin cache."""
    cache_root = (Path(root) / "plugins" / "cache").resolve()
    try:
        target = Path(install_path).expanduser().resolve()
        target.relative_to(cache_root)
    except (OSError, ValueError):
        return None
    # Guard against a registry entry that points at the cache directory itself.
    return target if len(target.parts) > len(cache_root.parts) else None


def remove_install_path(root: Path, entry: Mapping[str, Any]) -> None:
    """Delete one registered plugin copy, refusing paths outside the cache."""
    raw = entry.get("installPath")
    if not isinstance(raw, str) or not raw:
        return
    target = _cache_confined(root, raw)
    if target is None:
        return
    if target.is_dir() and not target.is_symlink():
        rmtree(target, ignore_errors=True)
    elif target.exists():
        target.unlink(missing_ok=True)


def uninstall(
    descriptor: Platform,
    home: Path,
    environ: Optional[Mapping[str, str]] = None,
    runner: Any = run,
) -> bool:
    """Remove the plugin from the shared root, with or without a CLI present."""
    root = resolve_config_root(descriptor, home, environ)
    # CLI 的卸载是软删除，而且第一个动作就是清掉注册表条目；等它跑完再读注册表
    # 已经找不到 installPath 了，物理删除会被整个跳过。因此先快照安装路径。
    registered = plugin_entries(read_registry(root))
    command = capable_cli(descriptor, environ, runner)
    if command:
        runner(
            [command, "--config-dir", str(root), "plugins", "uninstall",
             PLUGIN_NAME, "--json"],
            timeout=120,
        )
    # 先把登记状态清干净，再动文件：注册表写不进去时插件可能仍被宿主加载，
    # 这时删掉缓存等于毁掉一份正在使用的安装。
    document = read_registry(root)
    plugins = document.get("plugins")
    if isinstance(plugins, dict) and PLUGIN_ID in plugins:
        plugins.pop(PLUGIN_ID)
        _write_json(registry_path(root), document)
    set_plugin_enabled(root, None)
    # 注册表和 settings 都不再引用这个插件，缓存里的副本到此才是真正的垃圾。
    for entry in registered + plugin_entries(read_registry(root)):
        remove_install_path(root, entry)
    remove_cached_copies(root)
    return not plugin_entries(read_registry(root)) and not cached_copies(root)


def desktop_presence(
    descriptor: Platform,
    home: Path,
    os_name: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    """Return where the Qoder desktop IDE was found, or an empty string."""
    os_name = os_name or platform.system()
    if os_name == "Darwin":
        for app in descriptor.macos_apps:
            for base in (Path("/Applications"), Path(home) / "Applications"):
                candidate = base / app
                if candidate.is_dir():
                    return str(candidate)
        return ""
    if os_name != "Linux":
        return ""
    environ = os.environ if environ is None else environ
    for directory in config.linux_application_dirs(home, environ):
        for desktop_file in descriptor.linux_desktop_files:
            candidate = directory / "applications" / desktop_file
            if candidate.is_file():
                return str(candidate)
    for command in descriptor.linux_executables:
        found = shutil.which(command, path=environ.get("PATH"))
        if found:
            return f"{command} ({found})"
    for candidate in map(Path, descriptor.linux_package_dirs):
        if candidate.exists():
            return str(candidate)
    return ""


def agent_presence(
    platform_key: str,
    home: Path,
    os_name: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    """Return how this platform is available: CLI, desktop IDE, or shared root.

    The CLI and the desktop IDE are peers, so a shared configuration root that
    either of them created counts as the platform being present.
    """
    descriptor = PLATFORMS[platform_key]
    command, path = find_cli(descriptor, environ)
    if command:
        return f"{command} ({path})"
    desktop = desktop_presence(descriptor, home, os_name, environ)
    if desktop:
        return desktop
    try:
        root = resolve_config_root(descriptor, home, environ)
    except QoderError:
        return ""
    return str(root) if root.is_dir() else ""


def agent_location(
    tool: str, home: Path, environ: Optional[Mapping[str, str]] = None
) -> tuple[list[str], Optional[Path]]:
    """Return helper-script flags and the global agents directory for a tool."""
    descriptor = PLATFORMS.get(tool)
    if descriptor is None:
        return [], None
    root = resolve_config_root(descriptor, home, environ)
    return ["--config-root", str(root)], root / "agents"


def foreign_os_roots(descriptor: Platform) -> list[Path]:
    """List Windows-side Qoder roots that a WSL installer cannot share."""
    if platform.system() != "Linux":
        return []
    users = Path("/mnt/c/Users")
    try:
        candidates = sorted(users.iterdir()) if users.is_dir() else []
    except OSError:
        return []
    roots = []
    for profile in candidates:
        if profile.name.lower().startswith(("default", "all users", "public", "winnt")):
            continue
        candidate = profile / descriptor.config_dirname
        if candidate.is_dir():
            roots.append(candidate)
    return roots

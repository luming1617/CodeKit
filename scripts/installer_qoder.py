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


def plugin_cache_root(root: Path) -> Path:
    """Return the cache directory holding locally installed Qoder plugins."""
    return Path(root) / "plugins" / "cache" / CACHE_MARKETPLACE / PLUGIN_NAME


def remove_stale_versions(cache_root: Path, version: str) -> None:
    """Delete superseded plugin copies and interrupted staging directories."""
    if not cache_root.is_dir():
        return
    for candidate in sorted(cache_root.iterdir()):
        if candidate.name == version:
            continue
        if candidate.is_dir() and not candidate.is_symlink():
            shutil.rmtree(candidate, ignore_errors=True)
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
        shutil.rmtree(staging, ignore_errors=True)
        if have_backup:
            os.replace(backup, target)
        raise QoderError(f"could not write the Qoder plugin cache: {exc}") from exc
    if have_backup:
        shutil.rmtree(backup, ignore_errors=True)
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
        shutil.rmtree(target, ignore_errors=True)
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
    command = capable_cli(descriptor, environ, runner)
    if command:
        runner(
            [command, "--config-dir", str(root), "plugins", "uninstall",
             PLUGIN_NAME, "--json"],
            timeout=120,
        )
    document = read_registry(root)
    for entry in plugin_entries(document):
        remove_install_path(root, entry)
    plugins = document.get("plugins")
    if isinstance(plugins, dict) and PLUGIN_ID in plugins:
        plugins.pop(PLUGIN_ID)
        _write_json(registry_path(root), document)
    set_plugin_enabled(root, None)
    return not plugin_entries(read_registry(root))


def _linux_application_dirs(
    home: Path, environ: Optional[Mapping[str, str]] = None
) -> tuple[Path, ...]:
    """Return the XDG application directories searched for desktop entries."""
    environ = os.environ if environ is None else environ
    data_home = environ.get("XDG_DATA_HOME")
    bases = [Path(data_home).expanduser()] if data_home else [Path(home) / ".local" / "share"]
    configured = environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    bases.extend(Path(value).expanduser() for value in configured.split(os.pathsep) if value)
    return tuple(dict.fromkeys(bases))


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
    for directory in _linux_application_dirs(home, environ):
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


def supported_platform_keys() -> Iterable[str]:
    """Return the platform keys the installer knows how to drive."""
    return tuple(PLATFORMS)

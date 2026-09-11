#!/usr/bin/env python3
"""Structured configuration helpers used by the CodeKit installer.

The shell installer still owns platform/package-manager commands, while this
module owns data parsing and file updates.  Every write is performed through
an atomic replacement so a failed install does not leave a partially-written
configuration file behind.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import tempfile
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional


VERSION_PATTERN = re.compile(r"(?<!\d)(\d+)(?:\.(\d+)){1,3}(?!\d)")
SEMVER_PATTERN = re.compile(
    r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$"
)
MCP_SECTION_PATTERN = re.compile(r"^\[mcp_servers\.([^\]]+)\]\s*$")

# 直连 registry.npmjs.org 在国内网络下常超时。NPM_REGISTRY 既由 set_npm 写入
# npm_config_registry（本会话所有 npm/npx 子进程生效），也用作版本探测 URL；
# NPM_REGISTRY_ARGS 仅用于面向用户的手动安装指引，因为那些命令在用户 shell 执行、无法继承本进程环境。
NPM_REGISTRY = "https://registry.npmmirror.com"
NPM_REGISTRY_ARGS = ("--registry", NPM_REGISTRY)

# Homebrew 默认在 install/upgrade 前先跑一次 brew update：git 拉 homebrew/brew、
# homebrew-core 与 homebrew-cask，再直连 ghcr.io 取 bottle，国内单次就是分钟级。
# 这里统一切到中科大镜像，并关闭 brew 自身的自动更新（组件是否需要升级由安装器
# 自行判断，不需要 brew 再同步一遍索引）。
HOMEBREW_MIRROR_ENV = {
    "HOMEBREW_NO_AUTO_UPDATE": "1",
    "HOMEBREW_API_DOMAIN": "https://mirrors.ustc.edu.cn/homebrew-bottles/api",
    "HOMEBREW_BOTTLE_DOMAIN": "https://mirrors.ustc.edu.cn/homebrew-bottles",
    "HOMEBREW_BREW_GIT_REMOTE": "https://mirrors.ustc.edu.cn/brew.git",
    "HOMEBREW_CORE_GIT_REMOTE": "https://mirrors.ustc.edu.cn/homebrew-core.git",
    "HOMEBREW_CASK_GIT_REMOTE": "https://mirrors.ustc.edu.cn/homebrew-cask.git",
}

# GitHub 直连（github.com、raw.githubusercontent.com、api.github.com）在国内常
# 不可达或极慢。所有 GitHub 下载与版本探测统一前置该代理，非 GitHub 地址不受影响。
GH_PROXY = "https://gh-proxy.com"

# 判定 GitHub 地址的根域名；子域（api./raw./objects./codeload.）一并覆盖。
GITHUB_HOSTS = ("github.com", "githubusercontent.com")

# 单次远程读取的超时（秒）。GitHub 地址会先试 gh-proxy 再回退直连，
# 因此该值是「每一次尝试」的上限，不是整体预算。
REMOTE_FETCH_TIMEOUT = 20

SCRIPT_DIR = Path(__file__).resolve().parent

# 安装器运行期依赖的辅助模块清单（含 installer.py，供卸载器复用）。
# installer.py / uninstaller.py 在导入这些模块之前先调用 require_helper_files()：
# 否则残缺检出会在模块加载期抛出原始 ModuleNotFoundError，而不是受控的单行错误。
REQUIRED_HELPER_FILES = (
    "component-registry.py",
    "component-selector.py",
    "installer.py",
    "installer_agents.py",
    "installer_config.py",
    "installer_qoder.py",
    "installer_sources.py",
    "installer_state.py",
    "manage-global-agents.py",
)


def require_helper_files(directory: Optional[Path] = None) -> None:
    """校验辅助模块是否齐全，缺失时抛出带绝对路径的 ValueError。"""
    target = Path(directory) if directory is not None else SCRIPT_DIR
    missing = [
        str(target / name)
        for name in REQUIRED_HELPER_FILES
        if not (target / name).is_file()
    ]
    if missing:
        raise ValueError(f"installer helper missing: {', '.join(missing)}")


def load_module(path: Path, name: str) -> Any:
    """按文件路径加载辅助模块，供需要读取策略表的模块共用同一实例。"""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load helper module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def is_github_url(url: str) -> bool:
    """判断 URL 是否指向 GitHub；只看主机名，避免被路径里的域名片段误判。"""
    host = urllib.parse.urlsplit(url).hostname or ""
    return any(host == root or host.endswith(f".{root}") for root in GITHUB_HOSTS)


def github_proxy_url(url: str) -> str:
    """把 GitHub URL 改写为 gh-proxy 前置形式；非 GitHub URL 原样返回。"""
    return f"{GH_PROXY}/{url}" if is_github_url(url) else url


def download_urls(url: str) -> tuple[str, ...]:
    """下载地址的尝试顺序：GitHub 走 gh-proxy 优先，失败再回退直连。"""
    proxied = github_proxy_url(url)
    return (url,) if proxied == url else (proxied, url)


def probe_urls(url: str) -> tuple[str, ...]:
    """版本探测地址的尝试顺序：GitHub 只走 gh-proxy。

    探测失败只会退回“已是最新”，代价是提示不准；与其再等一次必然超时的直连，
    不如把预算留给可用地址，避免拖慢每次安装前的组件状态检查。
    """
    return (github_proxy_url(url),)


def linux_application_dirs(
    home: Path, environ: Optional[Mapping[str, str]] = None
) -> tuple[Path, ...]:
    """返回 XDG 应用目录，覆盖用户级和常见系统级安装位置。"""
    environ = os.environ if environ is None else environ
    configured_home = environ.get("XDG_DATA_HOME")
    bases = (
        [Path(configured_home).expanduser()]
        if configured_home
        else [Path(home) / ".local" / "share"]
    )
    configured_dirs = environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    bases.extend(
        Path(value).expanduser() for value in configured_dirs.split(os.pathsep) if value
    )
    return tuple(dict.fromkeys(bases))


def extract_version(value: str) -> str:
    """Extract the first dotted numeric version from arbitrary command output."""

    match = VERSION_PATTERN.search(value or "")
    return match.group(0) if match else ""


def version_tuple(value: str) -> tuple[int, ...]:
    """将任意版本文本提取为可比较的整数元组。"""
    version = extract_version(value)
    if not version:
        return ()
    return tuple(int(part) for part in version.split("."))


def version_greater_or_equal(left: str, right: str) -> bool:
    """Compare dotted numeric versions, padding missing segments with zero."""

    left_parts = version_tuple(left)
    right_parts = version_tuple(right)
    if not left_parts or not right_parts:
        return False
    length = max(len(left_parts), len(right_parts))
    return left_parts + (0,) * (length - len(left_parts)) >= right_parts + (
        0,
    ) * (length - len(right_parts))


def read_json(path: Path, default: Optional[Any] = None) -> Any:
    """读取 JSON 文件；文件不存在时返回调用方提供的默认值或空对象。"""
    if not path.exists():
        return {} if default is None else default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON file {path}: {exc}") from exc


def atomic_write(path: Path, content: str) -> bool:
    """Atomically write text and return whether the file changed."""

    try:
        old = path.read_text(encoding="utf-8") if path.exists() else None
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"could not read {path}: {exc}") from exc
    if old == content:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
    except OSError as exc:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        raise ValueError(f"could not write {path}: {exc}") from exc
    return True


def write_json(path: Path, document: Any) -> bool:
    """以格式化 JSON 写入文件，并返回内容是否实际发生变化。"""
    return atomic_write(
        path,
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
    )


def read_key_value(path: Path, key: str) -> str:
    """从 key=value 状态文件中读取指定键，不存在时返回空字符串。"""
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"could not read {path}: {exc}") from exc
    prefix = f"{key}="
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix) :]
    return ""


def set_key_value(path: Path, key: str, value: str) -> bool:
    """Set one state-file key while preserving comments and other keys."""

    lines = []
    if path.exists():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"could not read {path}: {exc}") from exc

    prefix = f"{key}="
    replacement = f"{prefix}{value}"
    found = False
    output = []
    for line in lines:
        if line.startswith(prefix):
            if not found:
                output.append(replacement)
                found = True
        else:
            output.append(line)
    if not found:
        output.append(replacement)
    return atomic_write(path, "\n".join(output).rstrip("\n") + "\n")


def remove_key_value(path: Path, key: str) -> bool:
    """从状态文件删除指定键，并在有变化时使用原子写入保存。"""
    if not path.exists():
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"could not read {path}: {exc}") from exc
    prefix = f"{key}="
    output = [line for line in lines if not line.startswith(prefix)]
    if output == lines:
        return False
    content = "\n".join(output).rstrip("\n")
    return atomic_write(path, f"{content}\n" if content else "")


def validate_plugin_version(path: Path) -> str:
    """校验插件版本文件中的 SemVer 字符串，并返回合法版本。"""
    document = read_json(path)
    version = document.get("version") if isinstance(document, dict) else None
    if not isinstance(version, str) or not SEMVER_PATTERN.fullmatch(version):
        raise ValueError(
            f"{path}: version must be a semantic version such as 1.0.0"
        )
    return version


def json_field(document: Any, field: str) -> str:
    """Read a scalar field from JSON supplied by stdin."""

    if not isinstance(document, Mapping):
        return ""
    value = document.get(field)
    return value if isinstance(value, str) else ""


def json_path(document: Any, path: str) -> Any:
    """按点号路径读取嵌套 JSON 值，任一层不存在时返回 None。"""
    current = document
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def json_version_from_stdin(field: str) -> str:
    """从 stdin JSON 的指定字段提取数字版本，输入无效时返回空字符串。"""
    try:
        document = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return ""
    return extract_version(json_field(document, field))


def json_asset_sha_from_stdin(asset_name: str) -> str:
    """从 release JSON 的 assets 数组中读取指定文件的 SHA 摘要。"""
    try:
        document = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(document, Mapping):
        return ""
    assets = document.get("assets", [])
    if not isinstance(assets, list):
        return ""
    for asset in assets:
        if isinstance(asset, Mapping) and asset.get("name") == asset_name:
            digest = asset.get("digest")
            if isinstance(digest, str):
                return digest.split(":", 1)[-1]
    return ""


def opencode_plugins(path: Path) -> list[str]:
    """读取并校验 OpenCode 配置中的 plugin 字符串数组。"""
    document = read_json(path)
    if not isinstance(document, dict):
        raise ValueError(f"OpenCode config must contain a JSON object: {path}")
    plugins = document.get("plugin", [])
    if plugins is None:
        return []
    if not isinstance(plugins, list) or not all(
        isinstance(plugin, str) for plugin in plugins
    ):
        raise ValueError(f"OpenCode config field 'plugin' must be a string array: {path}")
    return plugins


def opencode_has_plugin(path: Path, prefix: str = "codekit@") -> bool:
    """判断 OpenCode 配置中是否存在指定前缀的插件。"""
    return any(plugin.startswith(prefix) for plugin in opencode_plugins(path))


def add_opencode_plugin(path: Path, plugin: str) -> bool:
    """幂等地追加 OpenCode 插件，并以原子方式保存配置文件。"""
    document = read_json(path, default={})
    if not isinstance(document, dict):
        raise ValueError(f"OpenCode config must contain a JSON object: {path}")
    plugins = document.setdefault("plugin", [])
    if not isinstance(plugins, list) or not all(
        isinstance(item, str) for item in plugins
    ):
        raise ValueError(f"OpenCode config field 'plugin' must be a string array: {path}")
    if plugin in plugins:
        return False
    plugins.append(plugin)
    return write_json(path, document)


def remove_opencode_plugins(path: Path, prefix: str = "codekit@") -> bool:
    """移除 OpenCode 配置中指定前缀的全部插件，保留其他插件。"""
    document = read_json(path)
    if not isinstance(document, dict):
        raise ValueError(f"OpenCode config must contain a JSON object: {path}")
    plugins = document.get("plugin")
    if plugins is None:
        return False
    if not isinstance(plugins, list) or not all(
        isinstance(item, str) for item in plugins
    ):
        raise ValueError(f"OpenCode config field 'plugin' must be a string array: {path}")
    remaining = [plugin for plugin in plugins if not plugin.startswith(prefix)]
    if remaining == plugins:
        return False
    if remaining:
        document["plugin"] = remaining
    else:
        document.pop("plugin", None)
    return write_json(path, document)


def toml_mcp_servers(path: Path) -> list[str]:
    """Return MCP server names from Codex TOML.

    Python 3.11+'s tomllib is preferred.  The fallback only needs to inspect
    section headers and keeps this installer usable with older system Python.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"could not read {path}: {exc}") from exc

    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

    if tomllib is not None:
        try:
            document = tomllib.loads(text)
        except (tomllib.TOMLDecodeError, ValueError) as exc:
            raise ValueError(f"invalid TOML file {path}: {exc}") from exc
        servers = document.get("mcp_servers", {})
        return sorted(servers.keys()) if isinstance(servers, dict) else []

    names = []
    for line in text.splitlines():
        match = MCP_SECTION_PATTERN.match(line.strip())
        if match:
            names.append(match.group(1).strip('"\''))
    return sorted(set(names))


def write_versions(
    path: Path,
    values: Iterable[tuple[str, str]],
    script_version: str,
    marketplace_sha: str = "",
) -> bool:
    """生成安装版本状态文件，包含时间戳、组件版本、脚本版本和 marketplace SHA。"""
    timestamp = datetime.now().astimezone()
    lines = [
        "# CodeKit Plugin - Installed Versions",
        f"# Generated: {timestamp.strftime('%Y-%m-%dT%H:%M:%S%z')}",
        f"install_timestamp={int(timestamp.timestamp())}",
    ]
    lines.extend(f"{key}={value}" for key, value in values)
    lines.append(f"script_version={script_version}")
    if marketplace_sha:
        lines.append(f"marketplace_sha={marketplace_sha}")
    return atomic_write(path, "\n".join(lines) + "\n")


def parse_assignments(values: Iterable[str]) -> list[tuple[str, str]]:
    """解析命令行传入的 key=value 列表，并拒绝缺少键名的项。"""
    result = []
    for value in values:
        if "=" not in value:
            raise ValueError(f"invalid key/value assignment: {value!r}")
        key, item = value.split("=", 1)
        if not key:
            raise ValueError(f"invalid empty key in assignment: {value!r}")
        result.append((key, item))
    return result


def build_parser() -> argparse.ArgumentParser:
    """构建配置辅助命令的分层 CLI 解析器。"""
    parser = argparse.ArgumentParser(description="CodeKit installer config helpers")
    subparsers = parser.add_subparsers(dest="command", required=True)

    version = subparsers.add_parser("version")
    version_subparsers = version.add_subparsers(dest="version_command", required=True)
    extract = version_subparsers.add_parser("extract")
    extract.add_argument("value")
    compare = version_subparsers.add_parser("ge")
    compare.add_argument("left")
    compare.add_argument("right")

    plugin_version = subparsers.add_parser("plugin-version")
    plugin_version.add_argument("--file", required=True, type=Path)

    json_parser = subparsers.add_parser("json")
    json_subparsers = json_parser.add_subparsers(dest="json_command", required=True)
    json_version = json_subparsers.add_parser("version")
    json_version.add_argument("--field", required=True)
    json_path_parser = json_subparsers.add_parser("path")
    json_path_parser.add_argument("--path", required=True)
    json_subparsers.add_parser("asset-sha").add_argument("--asset", required=True)
    json_set = json_subparsers.add_parser("set")
    json_set.add_argument("--file", required=True, type=Path)
    json_set.add_argument("--key", required=True)
    json_set.add_argument("--value", required=True)

    opencode = subparsers.add_parser("opencode")
    opencode_subparsers = opencode.add_subparsers(
        dest="opencode_command", required=True
    )
    for name in ("has", "remove"):
        command = opencode_subparsers.add_parser(name)
        command.add_argument("--file", required=True, type=Path)
        command.add_argument("--prefix", default="codekit@")
    add = opencode_subparsers.add_parser("add")
    add.add_argument("--file", required=True, type=Path)
    add.add_argument("--plugin", required=True)

    toml_parser = subparsers.add_parser("toml")
    toml_subparsers = toml_parser.add_subparsers(dest="toml_command", required=True)
    mcp = toml_subparsers.add_parser("mcp-servers")
    mcp.add_argument("--file", required=True, type=Path)

    kv = subparsers.add_parser("kv")
    kv_subparsers = kv.add_subparsers(dest="kv_command", required=True)
    for name in ("get", "set", "remove"):
        command = kv_subparsers.add_parser(name)
        command.add_argument("--file", required=True, type=Path)
        command.add_argument("--key", required=True)
    kv_subparsers.choices["set"].add_argument("--value", required=True)

    versions = subparsers.add_parser("versions")
    versions.add_argument("--file", required=True, type=Path)
    versions.add_argument("--script-version", required=True)
    versions.add_argument("--marketplace-sha", default="")
    versions.add_argument("--set", dest="assignments", action="append", default=[])
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """分发版本、JSON、OpenCode、TOML、键值和版本文件辅助命令。"""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "version":
            if args.version_command == "extract":
                print(extract_version(args.value))
            else:
                raise SystemExit(0 if version_greater_or_equal(args.left, args.right) else 1)
        elif args.command == "plugin-version":
            print(validate_plugin_version(args.file))
        elif args.command == "json":
            if args.json_command == "version":
                print(json_version_from_stdin(args.field))
            elif args.json_command == "path":
                try:
                    document = json.load(sys.stdin)
                except (json.JSONDecodeError, ValueError):
                    document = None
                value = json_path(document, args.path)
                if isinstance(value, bool):
                    print(str(value).lower())
                elif isinstance(value, (str, int, float)):
                    print(value)
            elif args.json_command == "asset-sha":
                print(json_asset_sha_from_stdin(args.asset))
            else:
                document = read_json(args.file)
                if not isinstance(document, dict):
                    raise ValueError(f"JSON document must be an object: {args.file}")
                document[args.key] = args.value
                write_json(args.file, document)
        elif args.command == "opencode":
            if args.opencode_command == "has":
                raise SystemExit(
                    0 if opencode_has_plugin(args.file, args.prefix) else 1
                )
            if args.opencode_command == "add":
                add_opencode_plugin(args.file, args.plugin)
            else:
                remove_opencode_plugins(args.file, args.prefix)
        elif args.command == "toml":
            for server in toml_mcp_servers(args.file):
                print(server)
        elif args.command == "kv":
            if args.kv_command == "get":
                value = read_key_value(args.file, args.key)
                if value:
                    print(value)
            elif args.kv_command == "set":
                set_key_value(args.file, args.key, args.value)
            else:
                remove_key_value(args.file, args.key)
        elif args.command == "versions":
            write_versions(
                args.file,
                parse_assignments(args.assignments),
                args.script_version,
                args.marketplace_sha,
            )
    except ValueError as exc:
        print(f"installer config: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

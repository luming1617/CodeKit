#!/usr/bin/env python3
"""Structured configuration helpers used by the CodeKit installer.

The shell installer still owns platform/package-manager commands, while this
module owns data parsing and file updates.  Every write is performed through
an atomic replacement so a failed install does not leave a partially-written
configuration file behind.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional


VERSION_PATTERN = re.compile(r"(?<!\d)(\d+)(?:\.(\d+)){1,3}(?!\d)")
SEMVER_PATTERN = re.compile(
    r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$"
)
MCP_SECTION_PATTERN = re.compile(r"^\[mcp_servers\.([^\]]+)\]\s*$")

# 直连 registry.npmjs.org 在国内网络下常超时，所有 npm 安装统一走镜像源。
NPM_REGISTRY = "https://registry.npmmirror.com"
NPM_REGISTRY_ARGS = ("--registry", NPM_REGISTRY)


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

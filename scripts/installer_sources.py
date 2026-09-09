#!/usr/bin/env python3
"""Build the compatibility plugin sources used by agent installers."""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Sequence

from installer_config import read_json, validate_plugin_version, write_json


def copy_contents(source: Path, target: Path) -> None:
    """递归复制源目录内容到目标目录，保留普通文件和目录结构。"""
    if not source.is_dir():
        raise ValueError(f"source directory not found: {source}")
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        destination = target / item.name
        if item.is_dir() and not item.is_symlink():
            shutil.copytree(item, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(item, destination)


def stage_skill_interface_files(root: Path) -> int:
    """Copy skill-level openai.yaml files to Codex-compatible agent paths."""

    if not root.is_dir():
        raise ValueError(f"skill source directory not found: {root}")

    staged = 0
    for skill_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        manifest = skill_dir / "openai.yaml"
        target = skill_dir / "agents" / "openai.yaml"
        if manifest.is_file() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(manifest, target)
            staged += 1

    missing = [
        skill_dir.name
        for skill_dir in sorted(path for path in root.iterdir() if path.is_dir())
        if (skill_dir / "SKILL.md").is_file()
        and not (skill_dir / "openai.yaml").is_file()
    ]
    if missing:
        names = ", ".join(missing)
        raise ValueError(f"skills missing openai.yaml: {names}")
    return staged


def update_json_fields(path: Path, fields: dict[str, str]) -> None:
    """读取 JSON 对象、更新指定字段并通过统一写入逻辑保存。"""
    document = read_json(path)
    if not isinstance(document, dict):
        raise ValueError(f"JSON document must be an object: {path}")
    document.update(fields)
    write_json(path, document)


def remove_path(path: Path) -> None:
    """删除文件、软链接或目录；目标不存在时保持幂等。"""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def replace_stages(replacements: Sequence[tuple[Path, Path]]) -> None:
    """Replace final directories with staged directories and restore on error."""

    backups: list[tuple[Path, Path]] = []
    installed: list[Path] = []
    try:
        for temporary, destination in replacements:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                backup = destination.with_name(
                    f".{destination.name}.old.{os.getpid()}.{uuid.uuid4().hex}"
                )
                os.replace(destination, backup)
                backups.append((destination, backup))
            os.replace(temporary, destination)
            installed.append(destination)
    except OSError as exc:
        for destination in reversed(installed):
            remove_path(destination)
        for destination, backup in reversed(backups):
            if backup.exists() or backup.is_symlink():
                os.replace(backup, destination)
        raise ValueError(f"could not replace staged plugin sources: {exc}") from exc
    else:
        for _, backup in backups:
            remove_path(backup)


def prepare(source: Path, state_dir: Path) -> tuple[Path, Path]:
    """校验仓库、构建临时兼容源，并以可回滚方式替换最终安装目录。"""
    source = source.resolve()
    state_dir = state_dir.resolve()
    required = (
        source / ".claude-plugin",
        source / ".codex-plugin",
        source / ".opencode",
        source / "skills",
        source / ".opencode" / "package.json",
        source / "plugin-version.json",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise ValueError(f"marketplace source is incomplete: {', '.join(missing)}")

    plugin_version = validate_plugin_version(source / "plugin-version.json")
    market_parent = state_dir / "marketplace-source"
    market_stage = market_parent / "codekit"
    opencode_stage = state_dir / "opencode-source"
    market_parent.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    market_tmp = Path(tempfile.mkdtemp(prefix=".codekit.tmp.", dir=market_parent))
    opencode_tmp = Path(tempfile.mkdtemp(prefix=".opencode-source.tmp.", dir=state_dir))
    try:
        for directory in (
            market_tmp / ".claude-plugin",
            market_tmp / ".codex-plugin",
            market_tmp / ".opencode" / "plugins",
            market_tmp / "skills",
            opencode_tmp / "plugins" / "codekit" / ".opencode" / "plugins",
            opencode_tmp / "plugins" / "codekit" / "skills",
        ):
            directory.mkdir(parents=True, exist_ok=True)

        copy_contents(source / ".claude-plugin", market_tmp / ".claude-plugin")
        copy_contents(source / ".codex-plugin", market_tmp / ".codex-plugin")
        copy_contents(source / ".opencode" / "plugins", market_tmp / ".opencode" / "plugins")
        copy_contents(source / "skills", market_tmp / "skills")

        shutil.copy2(source / ".opencode" / "package.json", opencode_tmp / "package.json")
        copy_contents(
            source / ".opencode" / "plugins",
            opencode_tmp / "plugins" / "codekit" / ".opencode" / "plugins",
        )
        copy_contents(
            source / "skills",
            opencode_tmp / "plugins" / "codekit" / "skills",
        )

        stage_skill_interface_files(market_tmp / "skills")
        stage_skill_interface_files(opencode_tmp / "plugins" / "codekit" / "skills")

        update_json_fields(
            market_tmp / ".claude-plugin" / "plugin.json",
            {"version": plugin_version},
        )
        update_json_fields(
            market_tmp / ".codex-plugin" / "plugin.json",
            {"version": plugin_version},
        )
        update_json_fields(
            opencode_tmp / "package.json",
            {
                "version": plugin_version,
                "main": "plugins/codekit/.opencode/plugins/codekit.js",
            },
        )

        # 所有内容先写入临时目录，最后一次性替换，避免中途失败留下半套插件源。
        replace_stages(((market_tmp, market_stage), (opencode_tmp, opencode_stage)))
    except Exception:
        remove_path(market_tmp)
        remove_path(opencode_tmp)
        raise

    return market_stage, opencode_stage


def build_parser() -> argparse.ArgumentParser:
    """构建兼容插件源生成器的命令行解析器。"""
    parser = argparse.ArgumentParser(description="Build CodeKit Plugin compatibility sources")
    parser.add_argument("command", choices=("prepare",))
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    return parser


def main() -> int:
    """执行 prepare 子命令并将构建失败转换为非零退出码。"""
    args = build_parser().parse_args()
    try:
        market_stage, opencode_stage = prepare(args.source, args.state_dir)
    except (OSError, ValueError) as exc:
        print(f"installer sources: {exc}", file=os.sys.stderr)
        return 1
    print(f"{market_stage}\t{opencode_stage}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

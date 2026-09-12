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
from installer_platform import rmtree


# 开发机上跑过测试、被 IDE 打开过就会留下这些缓存：它们不属于插件载荷，装进
# 宿主后没有任何东西会认领它们，宿主却会把插件缓存里的每个文件都当成待确认的
# 「卸载残留」列给用户。清单与仓库 .gitignore 一致，再加上跨平台的系统垃圾。
# 注意 skills 自带可运行的测试（tests/、*.py 都是载荷的一部分），只排除缓存。
STAGING_IGNORES = (
    "__pycache__",
    "*.py[cod]",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    ".venv-wsl",
    "venv",
    ".idea",
    "*.iml",
    "*.ipr",
    "*.iws",
    ".git",
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
)

_ignore_debris = shutil.ignore_patterns(*STAGING_IGNORES)


def copy_contents(source: Path, target: Path) -> None:
    """递归复制源目录内容到目标目录，跳过开发环境的缓存与垃圾文件。"""
    if not source.is_dir():
        raise ValueError(f"source directory not found: {source}")
    target.mkdir(parents=True, exist_ok=True)
    names = sorted(child.name for child in source.iterdir())
    ignored = _ignore_debris(str(source), names)
    for name in names:
        if name in ignored:
            continue
        item = source / name
        destination = target / name
        if item.is_dir() and not item.is_symlink():
            shutil.copytree(item, destination, dirs_exist_ok=True, ignore=_ignore_debris)
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
        path.unlink(missing_ok=True)
    elif path.is_dir():
        # 只读文件在 Windows 上会让 shutil.rmtree 抛 PermissionError，适配层的
        # rmtree 会先清掉只读位再删。
        rmtree(path, ignore_errors=True)


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
                # Windows 上 os.replace 不能覆盖已存在的目录（POSIX 允许替换空目录），
                # 因此回滚前必须先清掉半成品目标，否则回滚本身会再失败一次。
                remove_path(destination)
                os.replace(backup, destination)
        raise ValueError(f"could not replace staged plugin sources: {exc}") from exc
    else:
        for _, backup in backups:
            remove_path(backup)


def prepare(source: Path, state_dir: Path) -> tuple[Path, Path, Path]:
    """校验仓库、构建临时兼容源，并以可回滚方式替换最终安装目录。"""
    source = source.resolve()
    state_dir = state_dir.resolve()
    required = (
        source / ".agents" / "plugins" / "marketplace.json",
        source / ".claude-plugin",
        source / ".codex-plugin",
        source / ".opencode",
        source / ".qoder-plugin",
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
    # Qoder's CLI and desktop IDE read one shared configuration root, so a single
    # staged source installs the plugin for both surfaces at once.
    qoder_parent = state_dir / "qoder-source"
    qoder_stage = qoder_parent / "codekit"
    market_parent.mkdir(parents=True, exist_ok=True)
    qoder_parent.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    market_tmp = Path(tempfile.mkdtemp(prefix=".codekit.tmp.", dir=market_parent))
    opencode_tmp = Path(tempfile.mkdtemp(prefix=".opencode-source.tmp.", dir=state_dir))
    qoder_tmp = Path(tempfile.mkdtemp(prefix=".qoder-source.tmp.", dir=qoder_parent))
    try:
        for directory in (
            market_tmp / ".agents" / "plugins",
            market_tmp / ".claude-plugin",
            market_tmp / ".codex-plugin",
            market_tmp / ".opencode" / "plugins",
            market_tmp / "skills",
            opencode_tmp / "plugins" / "codekit" / ".opencode" / "plugins",
            opencode_tmp / "plugins" / "codekit" / "skills",
            qoder_tmp / ".qoder-plugin",
            qoder_tmp / "skills",
        ):
            directory.mkdir(parents=True, exist_ok=True)

        # Codex 只认 `<root>/.agents/plugins/marketplace.json`；`.codex-plugin/`
        # 提供的是插件自身的元数据，两者都需要。
        copy_contents(source / ".agents", market_tmp / ".agents")
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

        copy_contents(source / ".qoder-plugin", qoder_tmp / ".qoder-plugin")
        # Skill-local agents/ directories stay untouched: Qoder keeps them out of
        # its subagent namespace, and CodeKit installs subagents globally instead.
        copy_contents(source / "skills", qoder_tmp / "skills")

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
            qoder_tmp / ".qoder-plugin" / "plugin.json",
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
        replace_stages(
            (
                (market_tmp, market_stage),
                (opencode_tmp, opencode_stage),
                (qoder_tmp, qoder_stage),
            )
        )
    except Exception:
        remove_path(market_tmp)
        remove_path(opencode_tmp)
        remove_path(qoder_tmp)
        raise

    return market_stage, opencode_stage, qoder_stage


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
        market_stage, opencode_stage, qoder_stage = prepare(args.source, args.state_dir)
    except (OSError, ValueError) as exc:
        print(f"installer sources: {exc}", file=os.sys.stderr)
        return 1
    print(f"{market_stage}\t{opencode_stage}\t{qoder_stage}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

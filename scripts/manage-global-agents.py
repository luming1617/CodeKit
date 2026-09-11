#!/usr/bin/env python3
"""Install and uninstall CodeKit Markdown subagents in global layouts."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Mapping, Optional


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import installer_agents as agents  # noqa: E402
import installer_qoder as qoder  # noqa: E402


MANAGED_MARKER = "managed-by: codekit"
# 平台键与全局 agent 目录的唯一来源：installer_agents 负责目录布局，
# installer_qoder 负责两个 Qoder 发行版的共享配置根。
TOOLS = ("claude", "opencode", "codex", *qoder.PLATFORMS)

# canonical agent 使用 Claude 风格的工具名，OpenCode 的 agent frontmatter 要求
# `tools` 是“工具 id → 布尔值”的映射；未声明的内建工具一律显式关闭，
# 否则只读角色会在 OpenCode 上悄悄获得 Bash/Write 能力。
OPENCODE_TOOL_IDS = {
    "Read": "read",
    "Grep": "grep",
    "Glob": "glob",
    "Bash": "bash",
    "Write": "write",
    "Edit": "edit",
}
OPENCODE_TOOLS = (
    "read",
    "edit",
    "write",
    "glob",
    "grep",
    "list",
    "bash",
    "task",
    "todowrite",
    "question",
    "webfetch",
    "websearch",
    "lsp",
    "skill",
)


def agent_files(source: Path) -> list[Path]:
    """Return canonical agents stored directly under each Skill directory."""
    return sorted(
        path
        for path in source.glob("*/agents/*.md")
        if path.name != "README.md" and not path.parent.parent.name.startswith("_")
    )


def parse_agent(path: Path) -> tuple[str, str, str]:
    """解析 agent Markdown 的 frontmatter，并校验名称、描述和 CodeKit 标记。"""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        raise ValueError(f"missing frontmatter: {path}")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration as exc:
        raise ValueError(f"unterminated frontmatter: {path}") from exc

    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip("'\"")

    name = metadata.get("name", "")
    description = metadata.get("description", "")
    # Compare the frontmatter key, not the whole text: a role that merely mentions the
    # marker in prose must not be adopted (and later deleted) as a CodeKit-managed file.
    if not name or not description or metadata.get("managed-by") != "codekit":
        raise ValueError(f"name, description or 'managed-by: codekit' key missing: {path}")
    body = "\n".join(lines[end + 1:]).strip() + "\n"
    return name, description, body


def collect_agents(source: Path) -> list[tuple[Path, str, str, str]]:
    """解析全部 canonical agent，并拒绝会互相覆盖的重名角色。"""
    collected: list[tuple[Path, str, str, str]] = []
    seen: dict[str, Path] = {}
    for path in agent_files(source):
        name, description, body = parse_agent(path)
        if name in seen:
            raise ValueError(
                f"duplicate agent name {name!r}: {seen[name]} and {path}; global agents "
                "install as bare names, so one would silently overwrite the other"
            )
        seen[name] = path
        collected.append((path, name, description, body))
    return collected


def atomic_write(path: Path, content: str) -> bool:
    """仅在内容变化时通过临时文件原子写入目标，避免留下半文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == content:
        return False
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    os.replace(temporary, path)
    return True


def backup_if_needed(path: Path, backup_root: Path) -> None:
    """覆盖非 CodeKit 管理的同名 agent 前先备份，已管理文件则直接允许刷新。"""
    if not path.exists():
        return
    existing = path.read_text(encoding="utf-8", errors="replace")
    if MANAGED_MARKER in existing or "# Managed by CodeKit" in existing:
        return
    target = backup_root / path.name
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        target = backup_root / f"{path.name}.{stamp}.bak"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    print(f"backed up existing agent: {path} -> {target}")


def codex_toml(name: str, description: str, body: str) -> str:
    """把 canonical Markdown agent 转换成 Codex 使用的 TOML 内容。"""
    # JSON string escaping is also valid TOML basic-string escaping.
    return (
        "# Managed by CodeKit; generated from the canonical Markdown agent.\n"
        f"name = {json.dumps(name, ensure_ascii=False)}\n"
        f"description = {json.dumps(description, ensure_ascii=False)}\n"
        f"developer_instructions = {json.dumps(body, ensure_ascii=False)}\n"
    )


def opencode_markdown(markdown: str) -> str:
    """把 canonical Markdown agent 转换成 OpenCode 可校验的 frontmatter。"""
    lines = markdown.splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        raise ValueError("missing frontmatter")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration as exc:
        raise ValueError("unterminated frontmatter") from exc

    front: list[str] = []
    declared: list[str] = []
    for line in lines[1:end]:
        key, _, value = line.partition(":")
        if key.strip() == "tools":
            declared = [item.strip() for item in value.split(",") if item.strip()]
            continue
        front.append(line)

    unknown = [name for name in declared if name not in OPENCODE_TOOL_IDS]
    if unknown:
        raise ValueError(
            f"agent declares tools OpenCode cannot map: {', '.join(unknown)}"
        )
    allowed = {OPENCODE_TOOL_IDS[name] for name in declared}
    front.append("tools:")
    front.extend(
        f"  {tool}: {'true' if tool in allowed else 'false'}" for tool in OPENCODE_TOOLS
    )
    return "\n".join(["---", *front, "---", *lines[end + 1 :]]).rstrip("\n") + "\n"


def destinations(
    home: Path,
    tools: tuple[str, ...] = TOOLS,
    config_root: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> dict[str, Path]:
    """返回各工具的全局 agent 目录，Qoder 使用共享配置根。"""
    environ = os.environ if environ is None else environ
    qoder_tools = tuple(tool for tool in tools if tool in qoder.PLATFORMS)
    if config_root is not None and len(qoder_tools) > 1:
        raise SystemExit(
            "--config-root selects one Qoder configuration root; pass --tool once"
        )
    return {
        tool: agents.global_agent_plan(tool, home, config_root, environ)[1]
        for tool in tools
    }


def install(
    source: Path,
    home: Path,
    tools: tuple[str, ...] = TOOLS,
    config_root: Optional[Path] = None,
) -> int:
    """将 canonical agent 原子同步到指定工具的全局目录。"""
    parsed = collect_agents(source)
    if not parsed:
        raise ValueError(f"no agent Markdown files found in {source}")

    target_dirs = destinations(home, tools, config_root)
    backup_root = home / ".local" / "state" / "CodeKit" / "agent-backups"
    changed = 0
    # 同一份 Markdown 是事实源：Codex 转成等价 TOML，OpenCode 转成
    # 布尔工具映射，其余工具直接沿用 canonical Markdown。
    for source_file, name, description, body in parsed:
        markdown = source_file.read_text(encoding="utf-8")
        for tool in tools:
            if tool == "codex":
                destination = target_dirs[tool] / f"{name}.toml"
                content = codex_toml(name, description, body)
            elif tool == "opencode":
                destination = target_dirs[tool] / f"{name}.md"
                content = opencode_markdown(markdown)
            else:
                destination = target_dirs[tool] / f"{name}.md"
                content = markdown
            backup_if_needed(destination, backup_root / tool)
            if atomic_write(destination, content):
                changed += 1

    print(
        f"installed {len(parsed)} CodeKit subagents for {', '.join(tools)} "
        f"({changed} files changed)"
    )
    return 0


def _is_managed(path: Path) -> bool:
    """判断文件是否包含 CodeKit 管理标记，防止卸载误删用户文件。"""
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return MANAGED_MARKER in text or "# Managed by CodeKit" in text


def _managed_files(
    home: Path,
    tools: tuple[str, ...] = TOOLS,
    config_root: Optional[Path] = None,
) -> list[Path]:
    """收集指定工具布局中所有由 CodeKit 管理的 agent 文件。"""
    target_dirs = destinations(home, tools, config_root)
    layouts = tuple(
        (target_dirs[tool], "*.toml" if tool == "codex" else "*.md")
        for tool in tools
    )
    return sorted(
        path
        for directory, pattern in layouts
        for path in directory.glob(pattern)
        if _is_managed(path)
    )


def status(
    source: Optional[Path],
    home: Path,
    tools: tuple[str, ...] = TOOLS,
    config_root: Optional[Path] = None,
) -> int:
    """Print a stable state for shell callers: up_to_date, installed, or not_installed."""
    if source is not None and source.is_dir():
        parsed = collect_agents(source)
        if parsed:
            expected = 0
            present = 0
            target_dirs = destinations(home, tools, config_root)
            for _, name, _, _ in parsed:
                for tool in tools:
                    expected += 1
                    suffix = ".toml" if tool == "codex" else ".md"
                    present += int(_is_managed(target_dirs[tool] / f"{name}{suffix}"))
            print("up_to_date" if present == expected else "installed" if present else "not_installed")
            return 0

    print("installed" if _managed_files(home, tools, config_root) else "not_installed")
    return 0


def uninstall(
    home: Path,
    tools: tuple[str, ...] = TOOLS,
    config_root: Optional[Path] = None,
) -> int:
    """删除指定工具中带 CodeKit 标记的 agent 文件，并保留用户文件和备份。"""
    removed = 0
    for path in _managed_files(home, tools, config_root):
        try:
            path.unlink()
        except OSError as exc:
            print(f"could not remove managed agent: {path}: {exc}", file=os.sys.stderr)
            return 1
        removed += 1
    print(f"removed {removed} CodeKit global subagent file(s) for {', '.join(tools)}")
    return 0


def main() -> int:
    """解析 install/status/uninstall 子命令并调用对应的全局 agent 操作。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("install", "status", "uninstall"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--home", default=Path.home(), type=Path)
    parser.add_argument("--tool", choices=TOOLS, action="append", dest="tools")
    parser.add_argument(
        "--config-root",
        type=Path,
        help="explicit Qoder configuration root shared by its CLI and desktop IDE",
    )
    args = parser.parse_args()
    tools = tuple(args.tools) if args.tools else TOOLS
    if args.command == "install":
        if args.source is None or not args.source.is_dir():
            raise SystemExit(f"agent source directory not found: {args.source}")
        return install(args.source, args.home, tools, args.config_root)
    if args.command == "status":
        return status(args.source, args.home, tools, args.config_root)
    return uninstall(args.home, tools, args.config_root)


if __name__ == "__main__":
    raise SystemExit(main())

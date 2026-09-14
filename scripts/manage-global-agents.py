#!/usr/bin/env python3
"""Install and uninstall CodeKit Markdown subagents in global layouts."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Mapping, Optional


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import installer_agents as agents  # noqa: E402
import installer_config as config  # noqa: E402
import installer_platform as platform_layer  # noqa: E402
import installer_qoder as qoder  # noqa: E402


MANAGED_MARKER = "managed-by: codekit"
# Codex 的角色是生成的 TOML，没有 frontmatter，归属只能声明在文件开头的注释里；
# 这份头部注释由 codex_toml 写出、由 is_managed_content 读回，必须同源。
MANAGED_OWNER_COMMENT = "# Managed by CodeKit"
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

# 角色 Subagent 的 system prompt 只有自己的角色文件 + 主 Agent 传入的正文，
# SKILL.md 里的语言约束对它不可见，故安装时把共享规则注入角色正文。
OUTPUT_LANGUAGE_FILE = Path("_shared") / "prompts" / "output-language.md"
OUTPUT_LANGUAGE_RULES = "## 规则"
OUTPUT_LANGUAGE_MAIN_AGENT_SECTION = "## Subagent 附加要求"
OUTPUT_LANGUAGE_HEADING = "## 输出语言（CodeKit 统一规范）"


def output_language_clause(source: Path) -> str:
    """从共享规范取出输出语言规则段，作为注入角色正文的条款。

    只取「## 规则」，不取「Subagent 附加要求」：后者是给主 Agent 的派发纪律，
    注入角色正文会让只读子代理误以为自己必须再派发下一级而返回 BLOCKED。
    """
    path = source / OUTPUT_LANGUAGE_FILE
    if not path.is_file():
        raise ValueError(f"missing shared output language rules: {path}")
    text = path.read_text(encoding="utf-8")
    if OUTPUT_LANGUAGE_RULES not in text:
        raise ValueError(f"{path} has no {OUTPUT_LANGUAGE_RULES!r} section")
    rules = text.split(OUTPUT_LANGUAGE_RULES, 1)[1]
    rules = rules.split(OUTPUT_LANGUAGE_MAIN_AGENT_SECTION, 1)[0].strip()
    return f"{OUTPUT_LANGUAGE_HEADING}\n\n{rules}\n"


def with_output_language(text: str, clause: str) -> str:
    """把输出语言条款追加到角色正文尾部；已含条款时原样返回，避免重复叠加。"""
    if OUTPUT_LANGUAGE_HEADING in text:
        return text
    return text.rstrip("\n") + "\n\n" + clause


def agent_files(source: Path) -> list[Path]:
    """Return canonical agents stored directly under each Skill directory."""
    return sorted(
        path
        for path in source.glob("*/agents/*.md")
        if path.name != "README.md" and not path.parent.parent.name.startswith("_")
    )


def is_managed_content(text: str) -> bool:
    """判断一份内容是否声明了 CodeKit 归属：只看文件头部的归属区。

    Markdown 认 frontmatter 里的 ``managed-by`` 键；Codex 的角色是生成的 TOML，没有
    frontmatter，认首个键赋值之前那段头部注释。两种判据都只在头部生效，绝不按整文
    子串包含判定：角色正文会被原样嵌进 TOML 的 developer_instructions，正文里提过一
    次标记就会把别人的文件认成 CodeKit 自己写的，卸载时直接误删。
    """
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        closing = next(
            (index for index in range(1, len(lines)) if lines[index].strip() == "---"),
            None,
        )
        if closing is None:
            return False
        for line in lines[1:closing]:
            key, _, value = line.partition(":")
            if key.strip() == "managed-by":
                return value.strip().strip("'\"") == "codekit"
        return False
    header: list[str] = []
    for line in lines:
        if line.strip() and not line.lstrip().startswith("#"):
            break
        header.append(line)
    return MANAGED_OWNER_COMMENT in "\n".join(header)


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
    if not name or not description or not is_managed_content(text):
        raise ValueError(f"name, description or {MANAGED_MARKER!r} key missing: {path}")
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


def backup_if_needed(path: Path, backup_root: Path) -> None:
    """覆盖非 CodeKit 管理的同名 agent 前先备份，已管理文件则直接允许刷新。"""
    if not path.exists():
        return
    existing = path.read_text(encoding="utf-8", errors="replace")
    if is_managed_content(existing):
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
        f"{MANAGED_OWNER_COMMENT}; generated from the canonical Markdown agent.\n"
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


def agent_suffix(tool: str) -> str:
    """宿主使用的角色文件后缀：Codex 用生成的 TOML，其余沿用 canonical Markdown。"""
    return ".toml" if tool == "codex" else ".md"


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
    clause = output_language_clause(source)

    target_dirs = destinations(home, tools, config_root)
    # 备份根与安装器的状态目录保持一致：Windows 是 %LOCALAPPDATA%\CodeKit，
    # 不能沿用 POSIX 的 ~/.local/state。
    backup_root = platform_layer.platform_for().state_dir(home) / "agent-backups"
    changed = 0
    # 同一份 Markdown 是事实源：Codex 转成等价 TOML，OpenCode 转成
    # 布尔工具映射，其余工具直接沿用 canonical Markdown。三条路径共用同一份
    # 注入后的正文，避免某个宿主的角色静默丢掉语言约束。
    for source_file, name, description, body in parsed:
        markdown = with_output_language(source_file.read_text(encoding="utf-8"), clause)
        body = with_output_language(body, clause)
        for tool in tools:
            destination = target_dirs[tool] / f"{name}{agent_suffix(tool)}"
            if tool == "codex":
                content = codex_toml(name, description, body)
            elif tool == "opencode":
                content = opencode_markdown(markdown)
            else:
                content = markdown
            backup_if_needed(destination, backup_root / tool)
            if config.atomic_write(destination, content):
                changed += 1

    print(
        f"installed {len(parsed)} CodeKit subagents for {', '.join(tools)} "
        f"({changed} files changed)"
    )
    pruned = prune_removed_agents(parsed, home, tools, config_root)
    for path in pruned:
        print(f"removed retired CodeKit subagent: {path}")
    return 0


def prune_removed_agents(
    parsed: list[tuple[Path, str, str, str]],
    home: Path,
    tools: tuple[str, ...] = TOOLS,
    config_root: Optional[Path] = None,
) -> list[Path]:
    """删除源里已不存在、但曾被 CodeKit 装过的 agent 文件。

    角色下线不会自己从全局目录消失：宿主仍能派发一个 Skill 已经不再编排的角色，而它的
    正文还在指示它执行本应由控制器执行的命令。只碰带 CodeKit 管理标记的文件，且只碰
    本次安装覆盖的工具；删除失败只报告不中断——安装本身已经成功。
    """
    expected = {
        path
        for _, name, _, _ in parsed
        for path in (
            destinations(home, tools, config_root)[tool] / f"{name}{agent_suffix(tool)}"
            for tool in tools
        )
    }
    removed: list[Path] = []
    for path in _managed_files(home, tools, config_root):
        if path in expected:
            continue
        try:
            path.unlink()
        except OSError as exc:
            print(f"could not remove retired managed agent: {path}: {exc}", file=sys.stderr)
            continue
        removed.append(path)
    return removed


def _is_managed(path: Path) -> bool:
    """读取文件并判断其是否由 CodeKit 管理，防止卸载误删用户文件。"""
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return is_managed_content(text)


def _managed_files(
    home: Path,
    tools: tuple[str, ...] = TOOLS,
    config_root: Optional[Path] = None,
) -> list[Path]:
    """收集指定工具布局中所有由 CodeKit 管理的 agent 文件。"""
    target_dirs = destinations(home, tools, config_root)
    layouts = tuple(
        (target_dirs[tool], f"*{agent_suffix(tool)}") for tool in tools
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
                    present += int(
                        _is_managed(target_dirs[tool] / f"{name}{agent_suffix(tool)}")
                    )
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
            print(f"could not remove managed agent: {path}: {exc}", file=sys.stderr)
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

#!/usr/bin/env python3
"""Reconcile indexes and context files after the initialize result barrier."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path, PurePosixPath

from _common import (
    EXPECTED_TASKS,
    IGNORE_END,
    IGNORE_START,
    PROJECT_END,
    PROJECT_START,
    RESERVED_MARKERS,
    STANDARDS_END,
    STANDARDS_START,
    InitializeError,
    atomic_write,
    load_json,
    replace_managed_block,
)

# AGENTS.md stays a short routing document; overflow is trimmed with a warning
# rather than failing the whole run after the workers already succeeded.
CONTEXT_ITEM_LIMIT = 20
CONTEXT_LINE_BUDGET = 60
ReconcileError = InitializeError


class ContextContractError(ReconcileError):
    """A context payload violates the Scanner contract."""


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_repository_path(root: Path, raw: str, label: str) -> None:
    """Accept only existing repository-relative paths as Scanner evidence."""
    relative = PurePosixPath(raw)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ContextContractError(f"{label} must be repository-relative: {raw!r}")
    path = root.joinpath(*relative.parts).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ContextContractError(f"{label} escapes repository: {raw}") from exc
    if not path.exists():
        raise ContextContractError(f"{label} does not exist: {raw}")


def evidence_names(item: dict, label: str) -> list[str]:
    """Normalise a command evidence field, which may be one path or a list."""
    raw = item.get("evidence")
    if raw is None:
        return []
    names = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else None
    if names is None or not all(isinstance(name, str) and name.strip() for name in names):
        raise ContextContractError(f"{label} evidence must be one repository path or a list of them")
    return [name.strip() for name in names]


def require_fields(item: dict, fields: tuple[str, ...], label: str) -> None:
    for field in fields:
        value = item.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ContextContractError(f"{label} entry needs a non-empty {field!r}: {item}")


def validate_context(context: dict, root: Path) -> None:
    """Validate every Scanner fact before it can reach AGENTS.md or the gate.

    The Scanner is a model, so entries are checked field by field here: a missing
    key must fail with a readable contract error instead of raising ``KeyError``
    from the renderer in the middle of Phase 3.
    """
    if context.get("schema_version") != 1:
        raise ContextContractError("unsupported context schema_version")
    for key in ("structure", "commands", "evidence", "warnings"):
        if not isinstance(context.get(key, []), list):
            raise ContextContractError(f"context {key} must be a list")
    if not isinstance(context.get("overview", ""), str):
        raise ContextContractError("context overview must be a string")
    rendered = json.dumps(context, ensure_ascii=False)
    if any(marker in rendered for marker in RESERVED_MARKERS):
        raise ContextContractError("context contains reserved managed-block markers")
    for item in context.get("evidence", []):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ContextContractError("context evidence entries require a path")
        require_repository_path(root, item["path"], "context evidence")
    for item in context.get("structure", []):
        if not isinstance(item, dict):
            raise ContextContractError("context structure entries must be objects")
        require_fields(item, ("path", "description"), "context structure")
        require_repository_path(root, item["path"], "context structure path")
    for item in context.get("commands", []):
        if not isinstance(item, dict):
            raise ContextContractError("context command entries must be objects")
        require_fields(item, ("purpose", "command"), "context command")
        names = evidence_names(item, "context command")
        if item.get("required_for_initialize") is True and not names:
            raise ContextContractError(
                f"required_for_initialize commands need repository evidence: {item['command']}"
            )
        for name in names:
            require_repository_path(root, name, "context command evidence")


replace_block = replace_managed_block


def update_gitignore_for_skills(root: Path, result: dict) -> bool:
    """Ignore only concrete GitNexus skill directories from validated output."""
    if result.get("task") != "gitnexus" or result.get("status") != "success":
        return False
    paths: set[str] = set()
    for raw in result.get("changed_files", []):
        parts = PurePosixPath(raw).parts
        for prefix in ((".claude", "skills"), (".agents", "skills")):
            if len(parts) >= 3 and parts[:2] == prefix and parts[2].startswith("gitnexus"):
                paths.add("/".join(parts[:3]) + "/")
    if not paths:
        return False
    path = root / ".gitignore"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if text.count(IGNORE_START) != 1 or text.count(IGNORE_END) != 1:
        raise ReconcileError("invalid managed .gitignore block")
    body = text.split(IGNORE_START, 1)[1].split(IGNORE_END, 1)[0]
    entries = {line.strip() for line in body.splitlines() if line.strip()}
    updated = replace_managed_block(text, IGNORE_START, IGNORE_END, sorted(entries | paths))
    return atomic_write(path, updated)


def index_directories(root: Path) -> list[Path]:
    directories = [root / ".standard/01-global"]
    stack = root / ".standard/02-stack"
    if stack.is_dir():
        directories.extend(path for path in sorted(stack.iterdir()) if path.is_dir())
    directories.append(root / ".standard/03-project")
    return [path for path in directories if path.is_dir() and any(path.rglob("*.md"))]


def standard_indexes(root: Path) -> list[Path]:
    return [directory / "00-instructions.md" for directory in index_directories(root) if (directory / "00-instructions.md").is_file()]


def render_project_context(context: dict) -> tuple[str, list[str]]:
    """Render the AGENTS.md guide, returning the text plus overflow warnings.

    The Scanner is a model and may return hundreds of entries.  AGENTS.md is a
    short routing document, so surplus entries are dropped with a warning the
    orchestrator can surface, instead of failing Phase 3 after both analyzers
    already succeeded.
    """
    warnings: list[str] = []
    overview = " ".join(str(context.get("overview", "")).split())
    sections: list[tuple[str, list[str]]] = [
        ("Overview", [overview or "Repository context was not provided."]),
    ]

    for title, key in (("Structure", "structure"), ("Commands and Verification", "commands")):
        items = context.get(key, [])
        if not items:
            continue
        if len(items) > CONTEXT_ITEM_LIMIT:
            warnings.append(
                f"context {key} truncated from {len(items)} to {CONTEXT_ITEM_LIMIT} entries"
            )
            items = items[:CONTEXT_ITEM_LIMIT]
        if key == "structure":
            rows = [f"- `{item['path']}`: {item['description']}" for item in items]
        else:
            rows = []
            for item in items:
                names = evidence_names(item, "context command")
                evidence = (
                    " (evidence: " + ", ".join(f"`{name}`" for name in names) + ")"
                    if names
                    else ""
                )
                rows.append(f"- {item['purpose']}: `{item['command']}`{evidence}")
        sections.append((title, rows))

    lines = ["## Repository Guide", ""]
    for title, rows in sections:
        # 3 = blank line, heading, blank line before the rows.
        kept = rows[: max(CONTEXT_LINE_BUDGET - (len(lines) + 3), 0)]
        if len(kept) != len(rows):
            warnings.append(
                f"AGENTS project context '{title}' trimmed from {len(rows)} to {len(kept)} lines "
                f"({CONTEXT_LINE_BUDGET}-line budget)"
            )
        if not kept:
            continue
        lines += ["", f"### {title}", "", *kept]
    return "\n".join(lines), warnings


def render_agents_standards(root: Path, indexes: list[Path]) -> str:
    rows = []
    for index in indexes:
        relative = index.relative_to(root).as_posix()
        directory = index.parent
        details = [
            path.relative_to(root).as_posix()
            for path in sorted(directory.rglob("*.md"))
            if path.name != index.name
        ]
        category = "技术栈规范" if "02-stack" in relative else ("全局规范" if "01-global" in relative else "项目规范")
        rows.append(f"| {category} | `{relative}` | {', '.join(f'`{p}`' for p in details) or '无'} |")
    return "\n".join([
        "## AI 开发规范",
        "",
        "规范目录：`.standard/`。这是规范索引，不要预先加载全部规范。",
        "优先级：Security > Project > Stack > Global。",
        "",
        "| 类别 | 先读取 | 详细文件 |",
        "|---|---|---|",
        *rows,
        "",
        "进入某个场景时，先读取对应的 `00-instructions.md`，再只读取该场景所需的详细规范文件。",
    ])


def render_claude_standards(root: Path, indexes: list[Path]) -> str:
    imports = [f"@{path.relative_to(root).as_posix()}" for path in indexes]
    return "\n".join([
        "## AI 开发规范",
        "",
        "规范目录为 `.standard/`。先读取下列短索引；只有进入对应场景时，才读取索引中的详细规范。",
        "",
        *imports,
    ])


def update_sidecar(root: Path, result: dict) -> str | None:
    if result.get("task") != "graphify" or result.get("status") != "success":
        return None
    ignore = root / ".graphifyignore"
    ignore_text = ignore.read_text(encoding="utf-8")
    start = "# codekit:initialize:graphify-ignore:start"
    end = "# codekit:initialize:graphify-ignore:end"
    if ignore_text.count(start) != 1 or ignore_text.count(end) != 1:
        raise ReconcileError("cannot compute Graphify ignore digest from invalid managed block")
    managed_ignore = start + ignore_text.split(start, 1)[1].split(end, 1)[0] + end
    digest = "sha256:" + hashlib.sha256(managed_ignore.encode("utf-8")).hexdigest()
    sidecar = root / "graphify-out/.codekit-state.json"
    data = {
        "schema_version": 1,
        "root": str(root),
        "graphify_version": result.get("version") or result.get("runner") or "unknown",
        "ignore_digest": digest,
        "mode": result.get("mode", "default"),
        "last_status": "success",
    }
    atomic_write(sidecar, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return sidecar.relative_to(root).as_posix()


def main(argv: list[str] | None = None, *, emit: bool = True) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--context", required=True, type=Path)
    parser.add_argument("--worker-result", action="append", default=[], type=Path)
    args = parser.parse_args(argv)

    manifest = load_json(args.manifest)
    root = Path(manifest["root"]).resolve()
    context = load_json(args.context)
    if Path(context.get("root", "")).resolve() != root:
        raise ContextContractError("context root does not match run manifest")
    validate_context(context, root)
    if manifest.get("phase") != "gated":
        raise ReconcileError("manifest is not in gated phase")
    gate = manifest.get("worker_gate", {})
    if gate.get("status") not in {"success", "partial"}:
        raise ReconcileError("worker gate is not complete")
    if set(gate.get("tasks", [])) != EXPECTED_TASKS or gate.get("errors"):
        raise ReconcileError("worker gate does not contain the required validated tasks")
    if gate.get("status") == "partial" and not manifest.get("options", {}).get("allow_partial"):
        raise ReconcileError("partial worker gate requires --allow-partial")

    verified_results = manifest.get("worker_results", {})
    supplied_results: list[dict] = []
    supplied_tasks: set[str] = set()
    if len(args.worker_result) != len(EXPECTED_TASKS):
        raise ReconcileError("all validated worker results must be supplied")
    for result_path in args.worker_result:
        result = load_json(result_path)
        task = result.get("task")
        receipt = verified_results.get(task, {})
        if receipt.get("sha256") != file_sha(result_path):
            raise ReconcileError(f"worker result changed after gate: {task}")
        if task in supplied_tasks:
            raise ReconcileError(f"duplicate worker result supplied: {task}")
        supplied_tasks.add(task)
        supplied_results.append(result)
    if supplied_tasks != EXPECTED_TASKS:
        raise ReconcileError("supplied worker results do not match the validated task set")

    changed: list[str] = []
    for result in supplied_results:
        if update_gitignore_for_skills(root, result):
            changed.append(".gitignore")
    indexes = standard_indexes(root)
    agents = root / "AGENTS.md"
    agents_text = agents.read_text(encoding="utf-8") if agents.exists() else ""
    project_context, context_warnings = render_project_context(context)
    agents_text = replace_block(agents_text, PROJECT_START, PROJECT_END, project_context)
    agents_text = replace_block(agents_text, STANDARDS_START, STANDARDS_END, render_agents_standards(root, indexes))
    if atomic_write(agents, agents_text):
        changed.append("AGENTS.md")

    claude = root / "CLAUDE.md"
    claude_text = claude.read_text(encoding="utf-8") if claude.exists() else ""
    claude_text = replace_block(claude_text, STANDARDS_START, STANDARDS_END, render_claude_standards(root, indexes))
    if atomic_write(claude, claude_text):
        changed.append("CLAUDE.md")

    for result in supplied_results:
        path = update_sidecar(root, result)
        if path:
            changed.append(path)

    managed = set(manifest.get("managed_paths", []))
    managed.update(changed)
    manifest["managed_paths"] = sorted(managed)
    manifest["context_changed_files"] = sorted(set(changed))
    manifest["context_status"] = "success"
    manifest["context_warnings"] = context_warnings
    manifest["authorized_changed_paths"] = sorted(
        set(manifest.get("authorized_changed_paths", [])) | set(changed)
    )
    manifest["phase"] = "reconciled"
    manifest["validation_commands"] = [
        item for item in context.get("commands", []) if item.get("required_for_initialize") is True
    ]
    # Run state stays private, like the lock file.
    atomic_write(args.manifest, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", mode=0o600)
    if emit:
        print(json.dumps(
            {
                "status": "success",
                "changed_files": sorted(set(changed)),
                "warnings": context_warnings,
            },
            ensure_ascii=False,
        ))
    return 0


if __name__ == "__main__":
    print("initialize reconcile: internal module; use controller.py reconcile", file=sys.stderr)
    raise SystemExit(2)

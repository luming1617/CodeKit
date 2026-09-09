#!/usr/bin/env python3
"""Canonical component registry for the CodeKit installer."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from typing import Dict, List, Sequence


COMPONENT_ID = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class Component:
    component_id: str
    description: str
    group: str


GROUPS: Dict[str, str] = {
    "ai": "── AI 开发套件 ──",
    "skills": "── CodeKit Skills ──",
    "intel": "── 代码智能 ──",
    "third": "── 三方工具 ──",
}


COMPONENTS: Sequence[Component] = (
    Component("git_ai", "git-ai (代码追踪·推荐)", "ai"),
    Component("rtk", "Rust Token Killer (节省token·推荐)", "ai"),
    Component("claude_plugin", "CodeKit Plugin (Claude)", "skills"),
    Component("opencode_plugin", "CodeKit Plugin (OpenCode)", "skills"),
    Component("codex_plugin", "CodeKit Plugin (Codex)", "skills"),
    Component("gitnexus", "GitNexus (代码结构分析·推荐)", "intel"),
    Component("graphify", "Graphify (代码知识图谱·推荐)", "intel"),
    Component("gh", "GitHub CLI (推荐)", "third"),
    Component("glab", "GitLab CLI (推荐)", "third"),
    Component("dingtalk_cli", "钉钉 CLI", "third"),
    Component("feishu_cli", "飞书 CLI", "third"),
)


def validate_registry(
    components: Sequence[Component] = COMPONENTS,
    groups: Dict[str, str] = GROUPS,
) -> None:
    if not components:
        raise ValueError("component registry is empty")
    if not groups:
        raise ValueError("component group registry is empty")

    seen = set()
    group_order = {group: index for index, group in enumerate(groups)}
    previous_group_index = -1
    for component in components:
        if not COMPONENT_ID.fullmatch(component.component_id):
            raise ValueError(f"invalid component id: {component.component_id!r}")
        if component.component_id in seen:
            raise ValueError(f"duplicate component id: {component.component_id}")
        seen.add(component.component_id)
        if component.group not in groups:
            raise ValueError(
                f"unknown group {component.group!r} for {component.component_id}"
            )
        current_group_index = group_order[component.group]
        if current_group_index < previous_group_index:
            raise ValueError("components must follow the declared group order")
        previous_group_index = current_group_index
        for field_name, value in (
            ("description", component.description),
            ("group title", groups[component.group]),
        ):
            if not value or any(character in value for character in "\t\r\n"):
                raise ValueError(
                    f"invalid {field_name} for component {component.component_id}"
                )


def export_tsv(components: Sequence[Component] = COMPONENTS) -> None:
    for component in components:
        print(
            "\t".join(
                (
                    component.component_id,
                    component.description,
                    component.group,
                    GROUPS[component.group],
                )
            )
        )


def export_json(components: Sequence[Component] = COMPONENTS) -> None:
    payload: List[dict] = []
    for component in components:
        item = asdict(component)
        item["group_title"] = GROUPS[component.group]
        payload.append(item)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate or export CodeKit components")
    parser.add_argument(
        "--format",
        choices=("tsv", "json"),
        default="tsv",
        help="registry output format (default: tsv)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate without printing the registry",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        validate_registry()
    except ValueError as exc:
        print(f"component registry: {exc}", file=sys.stderr)
        return 1
    if args.check:
        return 0
    if args.format == "json":
        export_json()
    else:
        export_tsv()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
